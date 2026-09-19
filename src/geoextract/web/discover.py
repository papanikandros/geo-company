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
    # verification through the imprint stage (shared host cache, process pool) and the
    # acceptance rule of the discovery routes — one fetch per resolving host, the row
    # keeps its unverified old site when nothing better is found
    from . import ccindex, impressum
    impressum.ensure_columns(gdf)
    prev = gdf["website_source"] == "guess"                # idempotent: an earlier run is redone
    if prev.any():
        for c in ["website", "website_host", "website_kind", "website_source", "website_verified", "website_verified_at",
                  "legal_name", "website_replaced", "website_replaced_source"] + impressum.EXTRA_COLUMNS:
            gdf.loc[prev, c] = pd.NA
    resolving = {h for h, ok in dns.items() if ok} | set(cache[cache["resolves"].fillna(False).astype(bool)]["host"])
    new_rows = [{"host": h, "resolves": bool(ok), "verified": None, "final_host": None, "checked_at": time.strftime("%Y-%m-%d")}
                for h, ok in dns.items()]
    if new_rows:
        cache = pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True).drop_duplicates("host", keep="last")
        cache.to_parquet(cache_p, index=False)
    hosts = sorted({h for hs in cand.values() for h in hs if h in resolving})
    print(f"[discover] verifying {len(hosts)} resolving hosts through the imprint stage")
    recs = impressum.fetch_hosts(data_root, hosts, workers=workers, tag="discover")
    today = time.strftime("%Y-%m-%d")
    rank = {"name+plz": 0, "name": 1}
    n_fill, verdicts = 0, {}
    for i, hs in cand.items():
        best = None
        for h in hs:
            rec = recs.get(h)
            if rec is None:
                continue
            v = impressum.verdict(rec, rows.at[i, "name"], rows.at[i, "address_postcode"])
            verdicts[v] = verdicts.get(v, 0) + 1
            if v in rank and ccindex.accept(rec, v, rows.at[i, "name"], h.split(".")[0]) \
                    and (best is None or rank[v] < rank[best[1]]):
                best = (h, v, rec)
        if best is None:
            continue
        h, v, rec = best
        n_fill += 1
        impressum.keep_replaced(gdf, i)
        gdf.at[i, "website"] = f"https://{h}"
        gdf.at[i, "website_host"] = h
        gdf.at[i, "website_kind"] = "own"
        gdf.at[i, "website_source"] = "guess"
        impressum.write_row(gdf, i, rec, v, today)
    print(f"[discover] candidate verdicts {verdicts}; verified sites written: {n_fill} of {len(rows)} rows")
    for p in [*export.write_merged(gdf, data_root, scope), export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf


__all__ = ["candidates", "discover", "dns_lookup", "name_tokens", "verify"]
_ = urlsplit
