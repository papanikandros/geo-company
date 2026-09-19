"""Stage 1 — Impressum extraction (register-website-pipeline-plan.md).

For every company with an OWN website (``website_kind = own``): fetch the home page, find the
imprint link (Impressum, Imprint, Anbieterkennzeichnung, rechtliche Hinweise, legal notice,
Kontakt — shortest matching URL on the same registered domain wins), fetch it and extract
the typed fields German law (§ 5 TMG / § 5 DDG) makes mandatory there: legal name and form,
register court, register type and number, VAT identifier, street, postcode, city. Patterns
and validators follow ``Liohtml/german-impressum-extractor`` (Apache-2.0 / MIT), rewritten
in Python: postcode range, VAT shape ``DE`` + 9 digits, register number 1–6 digits, court
checked against the courts of the register tables.

Per ROW the site verdict ``website_verified`` compares the imprint with the company:
``name+plz`` (distinctive name tokens AND the row's postcode in the imprint), ``name``,
``plz``, ``mismatch``, ``no_impressum`` (site alive, no imprint page found), ``parked``,
``redirect_offdomain`` (ARGUS rule: a redirect to another registered domain is not the
company's site), ``blocked`` (401/403/429/503 — bot protection, not a dead site), ``dead``,
``robots``. Kriesch rule: a domain belongs to the location named
in ITS OWN imprint — the verdict is per row, the extraction per host.

Everything is cached per host in ``web/impressum_cache.parquet`` (fields + the distinct
alphabetic tokens and postcodes of the imprint text, not the text itself), so re-runs and
the DE run never fetch a host twice. Two requests per host at most (home + imprint), one
robots.txt check, 8 s timeout, 300 KB cap per page, descriptive User-Agent.

Write-back (additive debug columns after the contract): ``imp_legal_name, imp_legal_form,
imp_court, imp_register_type, imp_register_no, imp_registration, imp_vat_id, imp_street,
imp_plz, imp_city, imp_score, imp_url`` + ``website_verified``, ``website_verified_at``, and
the contract column ``legal_name`` (Impressum first — stage 2c fills the rest from the
register).
"""
from __future__ import annotations

import concurrent.futures as cf
import functools
import html as _html
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests

from .. import config, paths
from ..register.legalform import legal_form_from_name
from ..resolve import normalize_name
from .discover import _GENERIC, _registered, _robots_allows, name_tokens
from .hygiene import host_of

USER_AGENT = config.WIKIDATA_USER_AGENT.replace("wikidata enrichment", "impressum extraction")
PAGE_LIMIT = 300_000
TEXT_CAP = 12_000          # imprint text kept in the cache (re-extraction without a refetch)
TIMEOUT = 8

# imprint link words, best first (the anchor text or the URL path must contain one)
IMPRINT_WORDS = ["impressum", "imprint", "anbieterkennzeichnung", "rechtliche-hinweise", "rechtliche hinweise",
                 "rechtliches", "legal-notice", "legal notice", "legalnotice", "legal", "kontakt", "contact"]
_LINK_RE = re.compile(r"<a\b[^>]*?href\s*=\s*([\"']?)([^\"'\s>]+)\1[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", re.DOTALL | re.IGNORECASE)
_BLOCK_RE = re.compile(r"</?(?:br|p|div|li|ul|ol|tr|td|th|h[1-6]|section|article|header|footer|address|table|dd|dt)\b[^>]*>",
                       re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_TOKEN_RE = re.compile(r"[a-z]{3,}")

# field patterns (validators: postcode 01001–99998, VAT DE + 9 digits, register number ≤ 6 digits)
_REGISTER_RE = re.compile(r"\b(HRA|HRB|GnR|PR|VR|GsR)\s*[-:.]?\s*(?:Nr\.?\s*)?(\d{1,3}(?:\.\d{3}){1,2}|\d{1,6})(?:\s+([A-Z]{1,3}))?(?![\w.])")
_SUFFIX_STOP = {"HR", "US", "DE", "ST", "AG", "SE", "KG", "EV", "CO", "OF", "IN", "AN", "AM", "ID", "NR", "TEL", "FAX", "UST", "VAT",
                "DER", "DES", "DIE", "UND", "IM", "AUF", "BEI", "MIT", "VON", "ZUR", "ZUM", "GBR", "OHG", "EG", "SEC", "REG", "HRA", "HRB", "GNR"}
_REGISTER_LABEL_RE = re.compile(r"(?:Handelsregister|Register)(?:-|\s)?(?:nummer|nr\.?|Nr\.?)\s*[:.]?\s*(?:(HRA|HRB|GnR|PR|VR|GsR)\s*)?(\d{1,3}(?:\.\d{3}){1,2}|\d{1,6})\s*([A-Z]{1,3})?\b",
                                re.IGNORECASE)
_COURT_RE = re.compile(r"(?:Amtsgericht|Registergericht|Handelsregistergericht|Handelsregister)\s*[:.]?\s*(?:des\s+|beim\s+|in\s+)?(?:Amtsgerichts?\s+)?"
                       r"(?!Amtsgericht|Registergericht|Handelsregister|Nr\b|HR[AB]\b|Abteilung)"
                       r"([A-ZÄÖÜ][A-Za-zäöüß\-]+(?:\s+(?:am|an|der|in|im|a\.|i\.|\(\w+\))\s+[A-ZÄÖÜ][A-Za-zäöüß\-]+|\s+\([A-Za-zäöüß]+\))?)")
_COURT_LINE_RE = re.compile(r"Amtsgericht|Registergericht|Handelsregister|Handesregister|Register(?:-)?gericht|Sitz der Gesellschaft|HR[AB]\b", re.IGNORECASE)
_HEADING_STOP = {"impressum", "imprint", "adresse", "anschrift", "kontakt", "standort", "firmensitz", "hauptsitz", "zentrale", "postanschrift",
                 "adresse und kontakt", "kontaktdaten", "angaben", "anbieter", "herausgeber", "verantwortlich", "betreiber"}
_VAT_RE = re.compile(r"\bDE\s?(\d{3})\s?(\d{3})\s?(\d{3})(?!\d)")
_PLZ_CITY_RE = re.compile(r"(?<![\d\-/])(\d{5})\s+([A-ZÄÖÜ][A-Za-zäöüß\-\.]+(?:\s(?:am|an|der|im|bei|a\.|i\.|d\.)\s?[A-ZÄÖÜ][A-Za-zäöüß\-\.]+|\s[A-ZÄÖÜ][A-Za-zäöüß\-\.]+)?)")
_STREET_RE = re.compile(r"^\s*([A-ZÄÖÜa-zäöüß][\wäöüßÄÖÜ\-\.' ]{1,40}?\s?\d{1,4}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,4}\s?[a-zA-Z]?)?)\s*,?\s*$")
_STREET_STOP = re.compile(r"tel|fax|hrb|hra|ust|vat|@|www|postfach|\d{5}", re.IGNORECASE)
_LABEL_RE = re.compile(r"^(?:firma|firmenname|anbieter|betreiber|herausgeber|verantwortlich(?:er)?|unternehmen|name|angaben gemäß § ?5 (?:tmg|ddg)|diensteanbieter|inhaber)\s*[:\-]?\s*",
                       re.IGNORECASE)
_PARKED_WORDS = ("domain is for sale", "domain kaufen", "diese domain steht zum verkauf", "domain zu verkaufen",
                 "parked", "sedoparking", "this domain", "buy this domain", "domain-parking", "domain parking")
_CITY_STOP = {"telefon", "tel", "fax", "mail", "germany", "deutschland", "web", "www", "http"}

EXTRA_COLUMNS = ["imp_legal_name", "imp_legal_form", "imp_court", "imp_register_type", "imp_register_no",
                 "imp_registration", "imp_vat_id", "imp_street", "imp_plz", "imp_city", "imp_score", "imp_url"]


def cache_parquet(data_root: Path) -> Path:
    p = data_root / "geoextract" / "web" / "impressum_cache.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# --- HTML → links / text ---------------------------------------------------------------------

def imprint_links(html: str, base_url: str) -> list[str]:
    """Candidate imprint URLs on the same registered domain, best first (word rank, then
    shortest URL)."""
    base_host = host_of(base_url) or ""
    found: dict[str, tuple[int, int]] = {}
    for m in _LINK_RE.finditer(html):
        href, text = m.group(2), _ANY_TAG_RE.sub(" ", m.group(3))
        text = _WS_RE.sub(" ", _html.unescape(text)).strip().lower()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, _html.unescape(href)).split("#")[0]
        h = host_of(url)
        if not h or _registered(h) != _registered(base_host):
            continue
        path = urlsplit(url).path.lower()
        rank = None
        for i, w in enumerate(IMPRINT_WORDS):
            if w in text or w.replace(" ", "-") in path or w.replace(" ", "") in path:
                rank = i
                break
        if rank is None:
            continue
        key = (rank, len(url))
        if url not in found or key < found[url]:
            found[url] = key
    return [u for u, _ in sorted(found.items(), key=lambda kv: kv[1])]


def page_text(html: str) -> str:
    """Visible text with line breaks at block boundaries (the extractor works line by line)."""
    t = _TAG_RE.sub(" ", html)
    t = _BLOCK_RE.sub("\n", t)
    t = _ANY_TAG_RE.sub(" ", t)
    t = _html.unescape(t).replace("\r", "")
    lines = [_WS_RE.sub(" ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# --- extraction ------------------------------------------------------------------------------

def _valid_plz(plz: str) -> bool:
    return plz.isdigit() and len(plz) == 5 and 1001 <= int(plz) <= 99998


_NAME_SUFFIX_RE = re.compile(r"\s*[>|–-]\s*(?:impressum|imprint|kontakt|startseite)\s*$", re.IGNORECASE)
_NAME_STOP_RE = re.compile(r"fotolia|shutterstock|adobe stock|istock|pixabay|unsplash|schlichtung|streitbeilegung|verbraucher|google|facebook|youtube|wordpress|"
                           r"@|www\.|https?:|e-?mail|telefon|\btel\b|\bfax\b|internet|homepage|copyright|©|\(c\)|all rights|alle rechte|"
                           r"\bist\b|\bsind\b|\bwird\b|\bwerden\b|website|webseite|\bapp\b|"
                           r"\b(?:llc|inc\.?|ltd\.?|corp\.?)\s*$", re.IGNORECASE)
_LOWER_START_STOP = {"des", "der", "die", "das", "und", "mit", "für", "fuer", "von", "vom", "zur", "zum", "bei", "im", "am", "in", "an",
                     "auf", "aus", "als", "oder", "wir", "sie", "ihr", "ihre", "unser", "unsere", "diese", "dieser", "alle", "bitte"}
_NAME_LEAD_RE = re.compile(r"^(?:(?:HRA|HRB|GnR|PR|VR|GsR)\s*\d+\s*(?:[A-Z]{1,2}\b)?\s*[,:\-–]?\s*)+", re.IGNORECASE)
_HOST_GENERIC = _GENERIC | {"entsorgung", "online", "group", "gruppe", "germany", "deutschland", "international", "industrie",
                            "industries", "technik", "technologie", "logistik", "logistics", "energie", "energy", "solar",
                            "metall", "stahl", "bau", "handel", "consulting", "systems", "system", "service", "services",
                            "recycling", "umwelt", "transport", "spedition", "werft", "werk", "werke", "gmbh", "shop", "web",
                            "immobilien", "holding", "beteiligung", "beteiligungen", "verwaltung", "vermietung", "grundstueck",
                            "grundstuecke", "asset", "management", "invest", "capital", "partner", "partners", "projekt", "projekte",
                            # infrastructure words: a row named "Umspannwerk X" is a site, not a firm to search for
                            "umspannwerk", "gleichrichterwerk", "blockheizkraftwerk", "klaeranlage", "zentralklaeranlage", "pumpwerk",
                            "wasserwerk", "heizwerk", "heizkraftwerk", "trafostation", "tankstelle", "halle", "lager", "lagerhalle",
                            "gewerbegebiet", "industriegebiet", "industriepark", "gewerbepark", "bahnhof", "hafen", "terminal",
                            "kraftwerk", "station", "anlage", "betrieb", "zentrale", "niederlassung", "filiale", "standort",
                            "nord", "sued", "ost", "west", "mitte", "neu", "alt", "gross", "klein", "ober", "unter"}
_NAME_PREFIX_RE = re.compile(r"^(?:impressum|imprint|anbieterkennzeichnung)\s*[-–|:]\s*", re.IGNORECASE)


def _name_candidate(line: str) -> str | None:
    cand = _NAME_PREFIX_RE.sub("", _LABEL_RE.sub("", line)).strip(" :,-–|©®")
    cand = _NAME_SUFFIX_RE.sub("", _NAME_LEAD_RE.sub("", cand)).strip(" :,-–|")
    if not (3 <= len(cand) <= 100) or "|" in cand or cand.lower() in _HEADING_STOP:
        return None
    if cand.split()[0].lower() in _LOWER_START_STOP or cand[0] in "\"'(,.;":
        return None
    if _NAME_STOP_RE.search(cand):
        return None
    if cand.lower().startswith(("amtsgericht", "registergericht", "handelsregister", "ust", "umsatzsteuer", "vertreten", "geschäftsführ")):
        return None
    return cand


@functools.lru_cache(maxsize=4)
def _court_patterns(courts: frozenset[str]) -> list[tuple[re.Pattern, str]]:
    return [(re.compile(r"\b" + re.escape(c) + r"\b", re.IGNORECASE), c) for c in sorted(courts, key=len, reverse=True)]


def extract(text: str, courts: set[str] | None = None, host: str | None = None,
            cities: set[str] | None = None) -> dict:
    """Typed imprint fields from the page text. Empty dict values are None; ``imp_score`` in
    0–1 says how many of the six field groups were found."""
    out: dict = {k: None for k in EXTRA_COLUMNS if k != "imp_url"}
    lines = text.split("\n")
    lower = text.lower()
    # anchor: the imprint block starts at the "Impressum" / "Angaben gemäß" heading if present
    start = 0
    for i, ln in enumerate(lines):
        l = ln.lower()
        if l.startswith("impressum") or l.startswith("angaben gemäß") or l.startswith("angaben gemaess") or l.startswith("anbieterkennzeichnung"):
            start = i
            break
    body = lines[start:]

    # register type + number (+ suffix like "HB") is decided after the legal name (below)
    # court
    low = {c.lower(): c for c in (courts or ())}
    raw_court = None
    if low:   # a known court name anywhere on a line that names the register / court
        court_res = _court_patterns(frozenset(courts))
        for ln in lines:
            if not _COURT_LINE_RE.search(ln):
                continue
            for rx, c in court_res:
                if rx.search(ln):
                    out["imp_court"] = c
                    break
            if out["imp_court"]:
                break
    for mc in _COURT_RE.finditer(text) if not out["imp_court"] else ():
        court = mc.group(1).strip(" .,;")
        court = re.sub(r"\s+\(.*\)$", "", court) if court.split()[-1].startswith("(") and len(court.split()) > 1 else court
        hit = low.get(court.lower()) or low.get(court.split()[0].lower())
        if hit:
            out["imp_court"] = hit
            break
        raw_court = raw_court or court
    if not out["imp_court"] and raw_court and not courts:
        out["imp_court"] = raw_court
    # VAT identifier
    mv = _VAT_RE.search(text)
    if mv:
        out["imp_vat_id"] = "DE" + "".join(mv.groups())
    # postcode + city: first occurrence in the imprint block whose city is a known register
    # city (else the first valid one anywhere) — "ISO 14001 Umwelt" lines must not win
    city_low = {c.lower() for c in (cities or ())}
    hits = []
    for chunk_i, chunk in enumerate((body, lines)):
        for ln in chunk:
            mp = _PLZ_CITY_RE.search(ln)
            if mp and _valid_plz(mp.group(1)):
                city = mp.group(2).strip(" .,")
                words = [w for w in city.split() if w.lower().strip(".:") not in _CITY_STOP]
                if not words:
                    continue
                known = words[0].lower() in city_low or " ".join(words[:2]).lower() in city_low
                hits.append((chunk_i, 0 if known else 1, mp.group(1), " ".join(words[:3])))
    if hits:
        hits.sort(key=lambda h: (h[1], h[0]))
        if not city_low or hits[0][1] == 0:   # with a city list, an unknown "city" is no address
            out["imp_plz"], out["imp_city"] = hits[0][2], hits[0][3]
    # street: the line holding the postcode (before the postcode) or the line before it
    if out["imp_plz"]:
        for i, ln in enumerate(lines):
            if out["imp_plz"] in ln and (out["imp_city"] or "") in ln:
                before = ln.split(out["imp_plz"])[0].strip(" ,")
                cands = [before.split(",")[-1].strip(), before] + ([lines[i - 1]] if i > 0 else []) + ([lines[i - 2]] if i > 1 else [])
                for c in cands:
                    ms = _STREET_RE.match(c)
                    if ms and not _STREET_STOP.search(c):
                        out["imp_street"] = ms.group(1).strip(" ,")
                        break
                break
    # legal name: short lines in the imprint block that carry a legal form; a line sharing a
    # distinctive token with the host label wins (photo credits and partner names lose)
    host_toks = set(_TOKEN_RE.findall((host or "").split(".")[0].replace("-", " ").lower()))
    cands = []
    for ln in body[:120]:
        cand = _name_candidate(ln)
        if cand and legal_form_from_name(cand) and not _COURT_RE.match(cand):
            cands.append(cand)
    host_toks = {t for t in host_toks if len(t) >= 4 and t not in _HOST_GENERIC}
    for cand in cands:
        if host_toks & set(_TOKEN_RE.findall(normalize_name(cand, strip_noise=False))):
            out["imp_legal_name"] = cand
            break
    if not out["imp_legal_name"] and host_toks:   # a short line naming the host label, even without a legal form
        for ln in body[:120]:
            cand = _name_candidate(ln)
            if cand and len(cand) <= 60 and not any(ch.isdigit() for ch in cand) and len(cand.split()) >= 2 \
                    and host_toks & set(_TOKEN_RE.findall(normalize_name(cand, strip_noise=False))):
                out["imp_legal_name"] = cand
                break
    if not out["imp_legal_name"] and cands:
        out["imp_legal_name"] = cands[0]
    if not out["imp_legal_name"] and out["imp_street"]:   # fallback: the line above the street
        for i, ln in enumerate(lines):
            if out["imp_street"] in ln and i > 0:
                cand = _name_candidate(lines[i - 1])
                if cand and not any(ch.isdigit() for ch in cand) and len(cand.split()) >= 2:
                    out["imp_legal_name"] = cand
                break
    if out["imp_legal_name"]:
        out["imp_legal_form"] = legal_form_from_name(out["imp_legal_name"])
    # register token: the first one at or after the legal name's line (parent / partner companies
    # named earlier in the imprint must not win); without a legal name the first one
    name_line = 0
    if out["imp_legal_name"]:
        for i, ln in enumerate(lines):
            if out["imp_legal_name"] in ln:
                name_line = i
                break
    m = None
    for i, ln in enumerate(lines):
        if i < name_line:
            continue
        m = _REGISTER_RE.search(ln) or _REGISTER_LABEL_RE.search(ln)
        if m:
            break
    if m is None:
        m = _REGISTER_RE.search(text) or _REGISTER_LABEL_RE.search(text)
    if m:
        typ = (m.group(1) or "HRB").upper().replace("GNR", "GnR")
        number = m.group(2).replace(".", "")          # "10.234" → "10234"
        out["imp_register_type"], out["imp_register_no"] = typ, number
        suffix = (m.group(3) or "").upper()
        if suffix and suffix not in _SUFFIX_STOP and len(suffix) <= 3:
            out["imp_registration"] = f"{typ} {number} {suffix}"
        else:
            out["imp_registration"] = f"{typ} {number}"
    groups = [out["imp_register_no"], out["imp_court"], out["imp_vat_id"], out["imp_legal_name"], out["imp_plz"], out["imp_street"]]
    out["imp_score"] = round(sum(1 for g in groups if g) / len(groups), 2)
    _ = lower
    return out


def text_tokens(text: str) -> list[str]:
    key = normalize_name(text, strip_noise=False)
    return sorted(set(_TOKEN_RE.findall(key)))


def text_postcodes(text: str) -> list[str]:
    return sorted({p for p in re.findall(r"(?<!\d)(\d{5})(?!\d)", text) if _valid_plz(p)})


# --- fetching --------------------------------------------------------------------------------

def fetch_html(session: requests.Session, url: str) -> tuple[str, str, int] | None:
    """(final url, html, status) of ``url``; None when the request fails. A connection
    failure is retried once after two seconds: under load the resolver and the upstream
    return transient errors that look like a dead host (the DE run of 2026-09-16 marked
    76 % of hosts dead at 32 parallel fetches; 20 of 24 answered on a second try)."""
    r = None
    for attempt in range(2):
        try:
            r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True, headers={"User-Agent": USER_AGENT})
            break
        except requests.exceptions.SSLError:
            return None                       # a certificate problem is not transient
        except requests.RequestException:
            if attempt == 0:
                time.sleep(2.0)
    if r is None:
        return None
    raw = b""
    try:
        for chunk in r.iter_content(16_384):
            raw += chunk
            if len(raw) >= PAGE_LIMIT:
                break
    except requests.RequestException:
        return None
    enc = r.encoding or "utf-8"
    return r.url, raw.decode(enc, errors="replace"), r.status_code


def safe_process_host(session: requests.Session, host: str, courts: set[str] | None = None,
                      cities: set[str] | None = None) -> dict:
    """process_host that never raises: a malformed host or a library error becomes the
    status ``error`` (one bad host must not stop a run of 75 000)."""
    if not isinstance(host, str) or "." not in host or " " in host or len(host) > 253:
        return {"host": host, "status": "error", "final_host": None, "imp_url": None, "tokens": [], "postcodes": [], "text": None}
    try:
        return process_host(session, host, courts, cities)
    except Exception as exc:   # noqa: BLE001 — any failure is recorded, not raised
        return {"host": host, "status": "error", "final_host": None, "imp_url": None, "tokens": [], "postcodes": [],
                "text": None, "imp_legal_name": f"error: {type(exc).__name__}"[:80]}


def process_host(session: requests.Session, host: str, courts: set[str] | None = None,
                 cities: set[str] | None = None) -> dict:
    """Home page → imprint page → fields for one host. ``status`` ∈ ok | no_impressum |
    parked | dead | redirect_offdomain | robots."""
    rec: dict = {"host": host, "status": None, "final_host": None, "imp_url": None, "tokens": [], "postcodes": [], "text": None}
    if not _robots_allows(session, host):
        rec["status"] = "robots"
        return rec
    home, codes = None, []
    for scheme in ("https", "http"):
        got = fetch_html(session, f"{scheme}://{host}/")
        if got and got[2] < 400:
            home = got
            break
        if got:
            codes.append(got[2])
    if home is None:
        rec["status"] = "blocked" if codes and all(c in (401, 403, 429, 503) for c in codes) else "dead"
        return rec
    final_url, home_html, _ = home
    final_host = host_of(final_url) or host
    rec["final_host"] = final_host
    rec["redirected"] = _registered(final_host) != _registered(host)
    # an off-domain redirect is followed ONE step: the landing site's imprint decides (a
    # company on a new domain verifies, a parked or resold domain does not — its imprint
    # names nobody or someone else); the status keeps the redirect visible
    home_text = page_text(home_html)
    if len(home_text) < 2000 and any(w in home_text.lower() for w in _PARKED_WORDS):
        rec["status"] = "parked"
        return rec
    links = imprint_links(home_html, final_url)
    imprint_text, imprint_url = None, None
    for url in links[:2]:
        got = fetch_html(session, url)
        if got and got[2] < 400:
            imprint_text, imprint_url = page_text(got[1]), got[0]
            break
    if imprint_text is None:
        # single-page sites keep the imprint on the home page: accept it when the block is there
        if "impressum" in home_text.lower() or "angaben gemäß" in home_text.lower():
            imprint_text, imprint_url = home_text, final_url
        else:
            rec["status"] = "redirect_offdomain" if rec["redirected"] else "no_impressum"
            rec["tokens"], rec["postcodes"] = text_tokens(home_text), text_postcodes(home_text)
            return rec
    rec["status"] = "ok_redirected" if rec["redirected"] else "ok"
    rec["imp_url"] = imprint_url
    rec["text"] = imprint_text[:TEXT_CAP]
    rec.update(extract(imprint_text, courts, host, cities))
    rec["tokens"], rec["postcodes"] = text_tokens(imprint_text), text_postcodes(imprint_text)
    return rec


# --- verdict per row -------------------------------------------------------------------------

def _as_list(v) -> list:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    return list(v)


OK_STATUS = ("ok", "ok_redirected")


def verdict(rec: dict, name: object, postcode: object) -> str:
    """Per-row verdict; a redirected host whose landing imprint does not name the company
    keeps the verdict ``redirect_offdomain``."""
    if rec.get("status") not in OK_STATUS:
        return rec.get("status") or "dead"
    toks = name_tokens(name)
    have = set(_as_list(rec.get("tokens")))
    hits = [t for t in toks if t in have]
    name_ok = bool(hits) and len(hits) >= min(2, len(toks))
    plz = str(postcode).strip() if isinstance(postcode, str) else ""
    plz_ok = bool(plz) and (plz == rec.get("imp_plz") or plz in set(_as_list(rec.get("postcodes"))))
    if name_ok and plz_ok:
        return "name+plz"
    if name_ok:
        return "name"
    if rec.get("status") == "ok_redirected":
        return "redirect_offdomain"
    if plz_ok:
        return "plz"
    return "mismatch"


# --- run -------------------------------------------------------------------------------------

def ensure_columns(gdf: pd.DataFrame) -> None:
    for c in EXTRA_COLUMNS:
        if c not in gdf.columns:
            gdf[c] = pd.array([None] * len(gdf), dtype="Float64" if c == "imp_score" else "string")
    for c in ("website_verified", "website_verified_at", "legal_name", "website_replaced", "website_replaced_source"):
        if c not in gdf.columns:
            gdf[c] = pd.array([None] * len(gdf), dtype="string")


def keep_replaced(gdf: pd.DataFrame, i) -> None:
    """Audit trail before a discovery route overwrites an UNVERIFIED website: the old URL
    and where it came from are kept in ``website_replaced`` / ``website_replaced_source``
    (a verified site is never overwritten — the routes only see unverified rows)."""
    old = gdf.at[i, "website"] if "website" in gdf.columns else None
    if isinstance(old, str) and old.strip():
        gdf.at[i, "website_replaced"] = old
        src = gdf.at[i, "website_source"] if "website_source" in gdf.columns else None
        gdf.at[i, "website_replaced_source"] = src if isinstance(src, str) else None


def write_row(gdf: pd.DataFrame, i, rec: dict, v: str, today: str) -> None:
    """Verdict + imprint fields of host record ``rec`` into row ``i`` (shared by the imprint
    stage and the discovery routes that verify through it)."""
    gdf.at[i, "website_verified"] = v
    gdf.at[i, "website_verified_at"] = today
    if rec.get("status") == "ok_redirected" and v in ("name+plz", "name") and isinstance(rec.get("final_host"), str):
        keep_replaced(gdf, i)
        gdf.at[i, "website"] = f"https://{rec['final_host']}"      # the company moved domains: keep the site it forwards to
        gdf.at[i, "website_host"] = rec["final_host"]
    if rec.get("status") in OK_STATUS:
        for c in EXTRA_COLUMNS:
            val = rec.get(c)
            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                gdf.at[i, c] = val
        if v in ("name+plz", "name") and rec.get("imp_legal_name"):
            gdf.at[i, "legal_name"] = rec["imp_legal_name"]
            if pd.isna(gdf.at[i, "legal_form"]) and rec.get("imp_legal_form"):
                gdf.at[i, "legal_form"] = rec["imp_legal_form"]


def known_courts(data_root: Path) -> set[str]:
    return _known(data_root, "court")


def known_cities(data_root: Path) -> set[str]:
    return _known(data_root, "city")


def _known(data_root: Path, col: str) -> set[str]:
    out: set[str] = set()
    for p in (data_root / "geoextract" / "register").glob("hr_companies_*.parquet"):
        try:
            out |= set(pd.read_parquet(p, columns=[col])[col].dropna().unique())
        except Exception:   # noqa: BLE001 — a table without the column just adds nothing
            pass
    return {str(c).strip() for c in out if c and len(str(c).strip()) >= 3}


_W: dict = {}


def _pool_init(courts: set[str], cities: set[str]) -> None:
    _W["session"] = requests.Session()
    _W["session"].headers.update({"User-Agent": USER_AGENT})
    _W["courts"], _W["cities"] = courts, cities


def _pool_chunk(hosts: list[str]) -> list[dict]:
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        recs = list(ex.map(lambda h: safe_process_host(_W["session"], h, _W["courts"], _W["cities"]), hosts))
    for r in recs:
        r["checked_at"] = time.strftime("%Y-%m-%d")
    return recs


def fetch_many(todo: list[str], courts: set[str], cities: set[str], workers: int, on_progress, chunk: int = 40):
    """Fetch + extract ``todo`` hosts with ``workers`` PROCESSES (4 threads each); yields
    lists of records chunk by chunk (CPU work spreads over cores, network latency overlaps)."""
    import os as _os
    n_proc = max(1, min(workers, _os.cpu_count() or 1))
    chunks = [todo[k:k + chunk] for k in range(0, len(todo), chunk)]
    with cf.ProcessPoolExecutor(max_workers=n_proc, initializer=_pool_init, initargs=(courts, cities)) as ex:
        for recs in ex.map(_pool_chunk, chunks):
            yield recs
            on_progress(len(recs))


def _merge_cache(cache: pd.DataFrame, new: list[dict]) -> pd.DataFrame:
    cache = pd.concat([cache, pd.DataFrame(new)], ignore_index=True).drop_duplicates("host", keep="last")
    for c in cache.columns:
        if c not in ("tokens", "postcodes"):
            cache[c] = cache[c].astype("string") if c != "imp_score" else pd.to_numeric(cache[c], errors="coerce")
    return cache


def fetch_hosts(data_root: Path, hosts: list[str], workers: int = 8, tag: str = "impressum") -> dict[str, dict]:
    """Imprint records for ``hosts`` through the shared host cache (fetching only the unknown
    ones); returns host → record."""
    cache_p = cache_parquet(data_root)
    cache = pd.read_parquet(cache_p) if cache_p.exists() else pd.DataFrame(columns=["host", "status"])
    known = {r["host"]: r for r in cache.to_dict("records")}
    todo = sorted({h for h in hosts if h not in known})
    if todo:
        courts, cities = known_courts(data_root), known_cities(data_root)
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        t0, new, done = time.time(), [], [0]

        def prog(k: int) -> None:
            done[0] += k
            if done[0] % 200 < k or done[0] == len(todo):
                print(f"[{tag}] {done[0]}/{len(todo)} hosts fetched ({time.time() - t0:.0f} s)")

        for recs in fetch_many(todo, courts, cities, max(1, workers // 4), prog):
            new.extend(recs)
            if len(new) >= 500:
                cache = _merge_cache(cache, new); cache.to_parquet(cache_p, index=False); new = []
        cache = _merge_cache(cache, new)
        cache.to_parquet(cache_p, index=False)
        known = {r["host"]: r for r in cache.to_dict("records")}
    return {h: known[h] for h in hosts if h in known}


def run(data_root: Path, scope: str, industrial_only: bool = True, limit: int | None = None,
        workers: int = 8, refetch: bool = False, reextract: bool = False) -> pd.DataFrame:
    import geopandas as gpd

    from .. import export
    src = paths.merged_parquet(data_root, scope, "4326")
    gdf = gpd.read_parquet(src)
    need = (gdf["website_kind"] == "own") & gdf["website_host"].notna() & gdf["name"].notna()
    if industrial_only:
        need &= gdf["is_industrial"].fillna(False)
    rows = gdf[need]
    if limit:
        rows = rows.head(limit)
    hosts = sorted(set(rows["website_host"].astype(str)))
    cache_p = cache_parquet(data_root)
    cache = pd.read_parquet(cache_p) if cache_p.exists() else pd.DataFrame(columns=["host", "status", "final_host", "imp_url", "tokens", "postcodes", "text", "checked_at"] + [c for c in EXTRA_COLUMNS if c != "imp_url"])
    if "text" not in cache.columns:
        cache["text"] = None
    courts, cities = known_courts(data_root), known_cities(data_root)
    if reextract and len(cache):   # rules changed: re-run the extractor on the cached imprint texts
        recs_re = cache.to_dict("records")
        n_re = 0
        for r in recs_re:
            if r.get("status") in OK_STATUS and isinstance(r.get("text"), str) and r["text"]:
                r.update(extract(r["text"], courts, r["host"], cities))
                n_re += 1
        cache = pd.DataFrame(recs_re)
        print(f"[impressum] re-extracted {n_re} cached imprints")
    known = {} if refetch else {r["host"]: r for r in cache.to_dict("records")}
    todo = [h for h in hosts if h not in known]
    print(f"[impressum] {scope}: {len(rows)} own-website rows, {len(hosts)} hosts, {len(todo)} to fetch")
    t0, done, new = time.time(), [0], []

    def _save() -> None:
        nonlocal cache, new
        if not new:
            return
        cache = _merge_cache(cache, new)
        cache.to_parquet(cache_p, index=False)
        new = []

    def prog(k: int) -> None:
        done[0] += k
        if done[0] % 200 < k or done[0] == len(todo):
            print(f"[impressum] {done[0]}/{len(todo)} hosts ({time.time() - t0:.0f} s)")

    # ``workers`` = fetch threads in total: processes of 4 threads each (the interpreter lock
    # made a single process CPU-bound at ≈ 3 hosts/s on the DE run)
    for recs in fetch_many(todo, courts, cities, max(1, workers // 4), prog):
        new.extend(recs)
        if len(new) >= 500:
            _save()                          # a crash or a stop loses at most 500 hosts
    _save()
    recs = {r["host"]: r for r in cache.to_dict("records")}
    # write back per row
    ensure_columns(gdf)
    today = time.strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    for i, r in rows.iterrows():
        rec = recs.get(str(r["website_host"]))
        if rec is None:
            continue
        v = verdict(rec, r["name"], r["address_postcode"])
        counts[v] = counts.get(v, 0) + 1
        write_row(gdf, i, rec, v, today)
    ok = cache[cache["host"].isin(hosts)]
    print(f"[impressum] verdicts {counts}; hosts by status {ok['status'].value_counts().to_dict()}; "
          f"register number on {int(ok['imp_register_no'].notna().sum())} hosts, legal name on {int(ok['imp_legal_name'].notna().sum())}")
    for p in [*export.write_merged(gdf, data_root, scope), export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf


__all__ = ["EXTRA_COLUMNS", "extract", "imprint_links", "page_text", "process_host", "run", "verdict"]
