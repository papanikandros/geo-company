"""Stage 3b — domain guessing with verification (register-website-pipeline-plan.md).

For a NAMED company without an own website, build candidate domains from its normalised
name key (``schwarzwaldmilch.de``, ``schwarzwald-milch.de``, ``.com``, ``…-gmbh.de``),
resolve them with DNS (≈ 0.1 KB per lookup — the one network step that is cheap enough for
the metered line), and verify every resolving host with ONE page fetch (≤ 150 KB, robots.txt
respected, 8 s timeout): the page text must contain the distinctive name tokens AND the
company's postcode or city (``website_verified = name+plz``); name tokens alone are accepted
only when at least two distinctive tokens match (``name``). Cross-second-level-domain
redirects are rejected (ARGUS rule). Accepted hosts are written as ``website`` with
``website_source = guess`` and ``website_verified_at``. Everything is cached per host in
``web/guess_cache.parquet`` so re-runs never fetch twice.
"""
from __future__ import annotations

import concurrent.futures as cf
import html
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import requests

from .. import config, paths
from ..resolve import normalize_name
from .hygiene import host_of

_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_GENERIC = {"gmbh", "und", "der", "die", "das", "von", "fuer", "mit", "bei", "am", "im", "an",
            "shop", "service", "center", "haus", "praxis", "buero", "stadt", "bremen", "hamburg",
            "kiosk", "apotheke", "baeckerei", "restaurant", "hotel", "cafe", "bar", "imbiss",
            "friseur", "salon", "studio", "markt", "laden", "werkstatt", "gaststätte", "gaststaette"}


def cache_parquet(data_root: Path) -> Path:
    p = data_root / "geoextract" / "web" / "guess_cache.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def name_tokens(name: object) -> list[str]:
    key = normalize_name(name, strip_noise=True)
    return [t for t in key.split() if len(t) >= 3 and t not in _GENERIC and not t.isdigit()]


def candidates(name: object, legal_form: object = None) -> list[str]:
    """Candidate hosts for a company name; empty when the name is too generic or too long."""
    toks = name_tokens(name)
    if not toks or len("".join(toks)) < 5 or len("".join(toks)) > 40 or max(len(t) for t in toks) < 5:
        return []
    joined, hyphen = "".join(toks), "-".join(toks)
    out = [f"{joined}.de", f"{hyphen}.de"] if hyphen != joined else [f"{joined}.de"]
    if len(toks) > 2:
        out.append("".join(toks[:2]) + ".de")
    out.append(f"{joined}.com")
    lf = legal_form.lower() if isinstance(legal_form, str) else ""
    if "gmbh" in lf or "gmbh" in str(name).lower():
        out.append(f"{joined}-gmbh.de")
    seen, uniq = set(), []
    for h in out:
        if h not in seen and re.match(r"^[a-z0-9-]+\.(de|com)$", h):
            seen.add(h); uniq.append(h)
    return uniq


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def dns_lookup(hosts: list[str], workers: int = 32) -> dict[str, bool]:
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(hosts, ex.map(resolves, hosts)))


def _robots_allows(session: requests.Session, host: str) -> bool:
    try:
        r = session.get(f"https://{host}/robots.txt", timeout=6, allow_redirects=True)
        if r.status_code != 200:
            return True
        block = False
        for line in r.text.splitlines()[:400]:
            line = line.strip().lower()
            if line.startswith("user-agent:"):
                block = line.split(":", 1)[1].strip() == "*"
            elif block and line.startswith("disallow:") and line.split(":", 1)[1].strip() == "/":
                return False
        return True
    except requests.RequestException:
        return True


def fetch_text(session: requests.Session, host: str, limit: int = 150_000) -> tuple[str, str] | None:
    """(final host, page text) of https://host (fallback http); None on failure."""
    for scheme in ("https", "http"):
        try:
            r = session.get(f"{scheme}://{host}/", timeout=8, stream=True, allow_redirects=True)
            if r.status_code >= 400:
                continue
            raw = b""
            for chunk in r.iter_content(16_384):
                raw += chunk
                if len(raw) >= limit:
                    break
            enc = r.encoding or "utf-8"
            text = raw.decode(enc, errors="replace")
            text = html.unescape(_TAG_RE.sub(" ", text))
            return host_of(r.url) or host, _WS_RE.sub(" ", text).lower()
        except requests.RequestException:
            continue
    return None


def _registered(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def verify(session: requests.Session, host: str, name: object, postcode: object, city: object) -> dict:
    """Fetch and check one host. Returns {"host", "verified", "final_host"}."""
    if not _robots_allows(session, host):
        return {"host": host, "verified": "robots", "final_host": None}
    got = fetch_text(session, host)
    if got is None:
        return {"host": host, "verified": "dead", "final_host": None}
    final, text = got
    if _registered(final) != _registered(host):
        return {"host": host, "verified": "redirect_offdomain", "final_host": final}
    toks = name_tokens(name)
    hits = [t for t in toks if t in text]
    plz = str(postcode).strip() if isinstance(postcode, str) else ""
    cty = normalize_name(city, strip_noise=True) if isinstance(city, str) else ""
    loc = (plz and plz in text) or (cty and cty in text)
    if hits and (len(hits) >= min(2, len(toks))) and loc:
        return {"host": host, "verified": "name+plz", "final_host": final}
    if len(hits) >= 2:
        return {"host": host, "verified": "name", "final_host": final}
    return {"host": host, "verified": "mismatch", "final_host": final}


def discover(data_root: Path, scope: str, industrial_only: bool = True, limit: int | None = None,
             workers: int = 8, dry_run: bool = False) -> pd.DataFrame:
    import geopandas as gpd

    from .. import export
    src = paths.merged_parquet(data_root, scope, "4326")
    gdf = gpd.read_parquet(src)
    need = gdf["website"].isna() & gdf["name"].notna()
    if industrial_only:
        need &= gdf["is_industrial"].fillna(False)
    rows = gdf[need]
    if limit:
        rows = rows.head(limit)
    cand = {i: candidates(r["name"], r["legal_form"] if "legal_form" in rows.columns else None) for i, r in rows.iterrows()}
    hosts = sorted({h for hs in cand.values() for h in hs})
    print(f"[discover] {scope}: {len(rows)} rows without a site, {len(hosts)} candidate hosts")
    cache_p = cache_parquet(data_root)
    cache = pd.read_parquet(cache_p) if cache_p.exists() else pd.DataFrame(
        columns=["host", "resolves", "verified", "final_host", "checked_at"])
    known = set(cache["host"])
    todo = [h for h in hosts if h not in known]
    t0 = time.time()
    dns = dns_lookup(todo) if todo else {}
    print(f"[discover] DNS: {len(todo)} lookups, {sum(dns.values())} resolve ({time.time() - t0:.0f} s)")
    if dry_run:
        return rows
    session = requests.Session()
    session.headers.update({"User-Agent": config.WIKIDATA_USER_AGENT.replace("wikidata", "web")})
    # verification: one host at a time per company (first resolving candidate that verifies)
    new_rows = []
    verified: dict[str, dict] = {r["host"]: r for _, r in cache.iterrows()}
    for h, ok in dns.items():
        verified[h] = {"host": h, "resolves": bool(ok), "verified": None, "final_host": None}
    jobs, seen_hosts = [], set()
    for i, hs in cand.items():
        for h in hs:
            v = verified.get(h)
            if v and v["resolves"] and v.get("verified") in (None, pd.NA) and h not in seen_hosts:
                seen_hosts.add(h)          # one fetch per host; the first company that guessed it is checked
                jobs.append((i, h))
    print(f"[discover] verifying {len(jobs)} resolving hosts with one page fetch each")
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda ih: verify(session, ih[1], rows.at[ih[0], "name"],
                                                rows.at[ih[0], "address_postcode"], rows.at[ih[0], "address_city"]), jobs))
    for (i, h), res in zip(jobs, results):
        verified[h].update(res)
        verified[h]["checked_at"] = time.strftime("%Y-%m-%d")
    for h in set(dns) | {h for _, h in jobs}:
        v = verified[h]
        new_rows.append({"host": h, "resolves": bool(v.get("resolves")), "verified": v.get("verified"),
                         "final_host": v.get("final_host"), "checked_at": v.get("checked_at") or time.strftime("%Y-%m-%d")})
    cache = pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True).drop_duplicates("host", keep="last")
    cache.to_parquet(cache_p, index=False)
    # write back: best verified candidate per row (name+plz beats name)
    rank = {"name+plz": 0, "name": 1}
    n_fill = 0
    for i, hs in cand.items():
        best = None
        for h in hs:
            v = verified.get(h) or {}
            if v.get("verified") in rank and (best is None or rank[v["verified"]] < rank[best[1]]):
                best = (h, v["verified"])
        if best:
            n_fill += 1
            gdf.at[i, "website"] = f"https://{best[0]}"
            gdf.at[i, "website_host"] = best[0]
            gdf.at[i, "website_kind"] = "own"
            gdf.at[i, "website_source"] = "guess"
            gdf.at[i, "website_verified"] = best[1]
            gdf.at[i, "website_verified_at"] = time.strftime("%Y-%m-%d")
    print(f"[discover] verified sites written: {n_fill} of {len(rows)} rows "
          f"({cache['verified'].value_counts().to_dict()})")
    for p in [*export.write_merged(gdf, data_root, scope), export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf


__all__ = ["candidates", "discover", "dns_lookup", "name_tokens", "verify"]
_ = urlsplit
