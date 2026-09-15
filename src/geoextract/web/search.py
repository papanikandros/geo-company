"""Stage 3d — website discovery through a search engine (register-website-pipeline-plan.md).

Source: a LOCAL SearXNG instance (``docker compose --profile search up -d searxng``,
JSON API at ``GEOEXTRACT_SEARX_URL``, default http://127.0.0.1:8888), free engines only,
one query per second, every query cached in ``web/search_cache.parquet``.

Per company the CBS query set (van Delden et al., ``SNStatComp/urlfinding``): the legal name
from the register or the imprint when known, else the map name — each with the
municipality; one variant with "impressum"; one with the street. Every result host of every
query is a candidate. A classifier ranks the candidate hosts of a company so that at most
``topk`` hosts are fetched for imprint verification; it decides nothing final — the imprint
verdict (``web.impressum``) and the discovery acceptance rule (``web.ccindex.accept``) do.

Features per (company, host), following urlfinding: name similarity to the page titles, to
the host label and to the snippets; municipality / postcode / street in title or snippet;
best rank; number of query variants and of results returning the host; host on the
directory / social / parcel / chain lists; how many DIFFERENT companies' searches return
the host (portals score high there); top-level domain; label length; label = name key.

Training needs no hand labels: the companies with an imprint-verified own website are
searched the same way and a host is positive when its registered domain is the known
site's. ``train`` reports a group-wise cross-validation (no company in both folds) and
writes ``web/search_model.joblib``; ``run`` applies it to the residual and writes
``website`` with ``website_source = search`` and ``website_score``.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from rapidfuzz import fuzz

from .. import config, paths
from ..resolve import normalize_name
from . import ccindex, impressum
from .discover import _registered, name_tokens
from .hygiene import host_of

SEARX_URL = os.environ.get("GEOEXTRACT_SEARX_URL", "http://127.0.0.1:8888")
# pace: 1/s survived > 4 000 queries in an hour; 4/s got Yahoo to refuse after ≈ 1 500 queries in
# ten minutes (2026-09-15) — the short ramp blocks (200 queries) had not shown that
QPS = float(os.environ.get("GEOEXTRACT_SEARX_QPS", "1"))
QUERY_SLEEP = 1.0 / QPS
FEATURES = ["sim_title", "sim_host", "sim_snippet", "city_in", "plz_in", "street_in", "best_rank", "n_hits",
            "n_queries", "listed", "host_freq", "tld_de", "label_len", "label_is_key", "legal_form_in_title", "n_engines"]
_BLOCK_TLDS = {"wikipedia.org", "wikidata.org", "openstreetmap.org", "google.com", "google.de", "bing.com"}


def cache_parquet(data_root: Path) -> Path:
    p = data_root / "geoextract" / "web" / "search_cache.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def model_path(data_root: Path) -> Path:
    return data_root / "geoextract" / "web" / "search_model.joblib"


# --- queries ---------------------------------------------------------------------------------

def _clean(s: object) -> str:
    return re.sub(r"\s+", " ", str(s)).strip() if isinstance(s, str) else ""


def queries(name: object, legal_name: object, city: object, district: object, street: object) -> list[str]:
    """The query set of one company, best evidence first; empty without a name."""
    base = _clean(legal_name) or _clean(name)
    if not base:
        return []
    muni = _clean(city) or _clean(district)
    out = []
    # no exact-phrase quotes: 38 % of quoted queries came back empty from Yahoo, 4 % of unquoted
    if muni:
        out.append(f"{base} {muni}")
        out.append(f"{base} {muni} impressum")
    else:
        out.append(base)
        out.append(f"{base} impressum")
    if _clean(name) and _clean(legal_name) and normalize_name(name, strip_noise=True) != normalize_name(legal_name, strip_noise=True):
        out.append(f"{_clean(name)} {muni}".strip())
    if _clean(street):
        out.append(f"{base} {_clean(street)}")
    return out[:4]


# --- search with cache -----------------------------------------------------------------------

class SearchCache:
    def __init__(self, data_root: Path):
        self.path = cache_parquet(data_root)
        self.df = pd.read_parquet(self.path) if self.path.exists() else pd.DataFrame(columns=["query", "results", "fetched_at"])
        self.known: dict[str, list[dict]] = {q: json.loads(r) for q, r in zip(self.df["query"], self.df["results"])}
        self.new: list[dict] = []

    def get(self, q: str):
        return self.known.get(q)

    def put(self, q: str, results: list[dict]) -> None:
        self.known[q] = results
        self.new.append({"query": q, "results": json.dumps(results, ensure_ascii=False), "fetched_at": time.strftime("%Y-%m-%d")})

    def save(self) -> None:
        if not self.new:
            return
        self.df = pd.concat([self.df, pd.DataFrame(self.new)], ignore_index=True).drop_duplicates("query", keep="last")
        for c in self.df.columns:
            self.df[c] = self.df[c].astype("string")
        self.df.to_parquet(self.path, index=False)
        self.new = []


_BLOCK_WORDS = ("suspended", "too many", "captcha", "access denied", "blocked")


def search_one(session: requests.Session, q: str, engines: str | None = None) -> list[dict] | None:
    """SearXNG JSON results of one query: url, title, content, engines, rank (1-based).
    None when the request failed or no engine delivered because of blocking / timeouts —
    such an answer is NOT cached (it says nothing about the query)."""
    params = {"q": q, "format": "json", "language": "de-DE"}
    if engines:
        params["engines"] = engines
    try:
        r = session.get(f"{SEARX_URL}/search", params=params, timeout=40)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    unresp = data.get("unresponsive_engines") or []
    if not data.get("results") and unresp:
        return None                          # every engine that answered was blocked / timed out
    if not data.get("results") and any(any(w in str(u).lower() for w in _BLOCK_WORDS) for u in unresp):
        return None
    out = []
    for i, res in enumerate(data.get("results", []), 1):
        url = res.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        out.append({"url": url, "title": res.get("title") or "", "content": res.get("content") or "",
                    "engines": res.get("engines") or [], "rank": min(res.get("positions") or [i])})
    return out


class Pacer:
    """Query pacing with back-off: after every failed answer (blocked / timed out) the wait
    doubles up to 5 minutes; MAX_FAILS consecutive failures abort the run (the engines are
    gone for now — rerun later, the cache keeps everything answered so far)."""
    MAX_FAILS = 25

    def __init__(self, sleep_s: float = QUERY_SLEEP):   # sequential fallback (run_queries)
        self.sleep_s, self.fails, self.n_fail_total = sleep_s, 0, 0

    def ok(self) -> None:
        self.fails = 0
        time.sleep(self.sleep_s)

    def failed(self) -> None:
        self.fails += 1
        self.n_fail_total += 1
        if self.fails >= self.MAX_FAILS:
            raise RuntimeError(f"search engines unresponsive for {self.fails} queries in a row — stopping; "
                               f"rerun later (cache kept)")
        time.sleep(min(300.0, self.sleep_s * 2 ** min(self.fails, 8)))


def run_queries(session: requests.Session, cache: SearchCache, qs: list[str], pacer: Pacer | None = None,
                engines: str | None = None) -> dict[str, list[dict]]:
    pacer = pacer or Pacer()
    out = {}
    for q in qs:
        hit = cache.get(q)
        if hit is None:
            hit = search_one(session, q, engines)
            if hit is None:
                pacer.failed()
                continue                     # not cached, not part of this company's results
            cache.put(q, hit)
            pacer.ok()
        out[q] = hit
    return out


# --- features ---------------------------------------------------------------------------------

def _listed(host: str) -> int:
    reg = _registered(host)
    for lst in (config.WEBSITE_DIRECTORY_HOSTS, config.WEBSITE_SOCIAL_HOSTS, config.WEBSITE_PARCEL_HOSTS, config.WEBSITE_CHAIN_HOSTS):
        if host in lst or reg in lst:
            return 1
    return 1 if reg in _BLOCK_TLDS else 0


def host_features(name: object, city: object, postcode: object, street: object, legal_form: object,
                  results: dict[str, list[dict]], host_freq: dict[str, int]) -> pd.DataFrame:
    """One row per registered domain across all queries of the company."""
    key = normalize_name(name, strip_noise=True)
    key_j, key_h = "".join(name_tokens(name)), "-".join(name_tokens(name))
    muni = normalize_name(city, strip_noise=True) if isinstance(city, str) else ""
    plz = str(postcode).strip() if isinstance(postcode, str) else ""
    st = normalize_name(street, strip_noise=True) if isinstance(street, str) else ""
    lf = str(legal_form).lower() if isinstance(legal_form, str) else ""
    per: dict[str, dict] = {}
    for qi, (q, hits) in enumerate(results.items()):
        for h in hits:
            host = host_of(h["url"])
            if not host:
                continue
            reg = _registered(host)
            d = per.setdefault(reg, {"host": reg, "sim_title": 0.0, "sim_snippet": 0.0, "best_rank": 99, "n_hits": 0,
                                     "qs": set(), "city_in": 0, "plz_in": 0, "street_in": 0, "legal_form_in_title": 0,
                                     "engines": set()})
            title = normalize_name(h["title"], strip_noise=True)
            snip = normalize_name(h["content"], strip_noise=True)
            d["sim_title"] = max(d["sim_title"], fuzz.token_set_ratio(key, title) / 100 if key and title else 0.0)
            d["sim_snippet"] = max(d["sim_snippet"], fuzz.partial_ratio(key, snip) / 100 if key and snip else 0.0)
            d["best_rank"] = min(d["best_rank"], int(h["rank"]))
            d["n_hits"] += 1
            d["qs"].add(qi)
            d["engines"].update(h.get("engines") or [])
            text = f"{title} {snip} {h['url'].lower()}"
            if muni and muni in text:
                d["city_in"] = 1
            if plz and plz in f"{h['title']} {h['content']}":
                d["plz_in"] = 1
            if st and st in text:
                d["street_in"] = 1
            if lf and lf in (h["title"] or "").lower():
                d["legal_form_in_title"] = 1
    rows = []
    for reg, d in per.items():
        label = reg.split(".")[0]
        rows.append({"host": reg, "sim_title": d["sim_title"], "sim_snippet": d["sim_snippet"],
                     "sim_host": fuzz.ratio(key_j, label.replace("-", "")) / 100 if key_j else 0.0,
                     "city_in": d["city_in"], "plz_in": d["plz_in"], "street_in": d["street_in"],
                     "best_rank": d["best_rank"], "n_hits": d["n_hits"], "n_queries": len(d["qs"]),
                     "listed": _listed(reg), "host_freq": host_freq.get(reg, 0), "tld_de": int(reg.endswith(".de")),
                     "label_len": len(label), "label_is_key": int(label in (key_j, key_h)),
                     "legal_form_in_title": d["legal_form_in_title"], "n_engines": len(d["engines"])})
    return pd.DataFrame(rows, columns=["host"] + FEATURES)


def skip_reason(name: object, places: set[str]) -> str | None:
    """Why a company is not worth a search: no name, no distinctive token (only generic or
    infrastructure words: "TX Logistik", "Umspannwerk Nord"), or a name that IS a place
    (district, locality, city — "Farge", "Bremen/Nord", "Gröpelingen")."""
    if not isinstance(name, str) or not name.strip():
        return "no name"
    toks = name_tokens(name)
    distinct = [t for t in toks if t not in impressum._HOST_GENERIC]
    if not distinct:
        return "generic name"
    key = normalize_name(name, strip_noise=True)
    if key in places or " ".join(distinct) in places or (len(distinct) == 1 and distinct[0] in places):
        return "place name"
    return None


def place_keys(gdf: pd.DataFrame, data_root: Path | None = None) -> set[str]:
    """Normalised names of the places the table knows: districts, address cities, and the
    cities of the register tables (all German municipalities)."""
    out: set[str] = set()
    for c in ("district", "address_city", "state"):
        if c in gdf.columns:
            out |= {normalize_name(v, strip_noise=True) for v in gdf[c].dropna().unique() if isinstance(v, str)}
    if data_root is not None:
        out |= {normalize_name(v, strip_noise=True) for v in impressum.known_cities(data_root)}
    out.discard("")
    return out


def _company_rows(gdf: pd.DataFrame, verified: bool, industrial_only: bool) -> pd.DataFrame:
    ok = gdf["website_verified"].isin(["name+plz", "name"]) if "website_verified" in gdf.columns else pd.Series(False, index=gdf.index)
    if verified:
        need = ok & gdf["name"].notna() & gdf["website_host"].notna()
    else:
        need = gdf["name"].notna() & ~ok & (gdf["website"].isna() | gdf["website_verified"].isin(ccindex._RESIDUAL_VERDICTS))
    if industrial_only:
        need &= gdf["is_industrial"].fillna(False)
    return gdf[need]


def _legal(r) -> object:
    for c in ("legal_name", "hr_name"):
        v = r.get(c)
        if isinstance(v, str) and v.strip():
            return v
    return None


def run_batch(session: requests.Session, cache: SearchCache, todo: list[str], qps: float = QPS,
              engines: str | None = None) -> int:
    """Uncached queries of one batch, started ``qps`` per second on ``ceil(qps)`` parallel
    workers; answered queries go into the cache. Returns the number of failed answers."""
    import concurrent.futures as cf
    import math
    if not todo:
        return 0
    t0 = time.time()
    fails = 0

    def one(k_q):
        k, q = k_q
        d = t0 + k / qps - time.time()
        if d > 0:
            time.sleep(d)
        return q, search_one(session, q, engines)

    with cf.ThreadPoolExecutor(max_workers=max(1, math.ceil(qps))) as ex:
        for q, res in ex.map(one, enumerate(todo)):
            if res is None:
                fails += 1
            else:
                cache.put(q, res)
    return fails


def _collect(data_root: Path, rows: pd.DataFrame, tag: str, batch: int = 100) -> tuple[dict, dict[str, int]]:
    """Run (or read from cache) the query set of every row in batches of companies, the
    uncached queries of a batch in parallel at QPS; a batch with more than half of its
    answers failed is retried once after a minute, three such batches in a row abort the
    run (the cache keeps everything answered). Returns per-row results + host frequency."""
    cache = SearchCache(data_root)
    session = requests.Session()
    engines = os.environ.get("GEOEXTRACT_SEARX_ENGINES")
    per_row, t0, n_q, n_fail, bad_batches = {}, time.time(), 0, 0, 0
    idx = list(rows.index)
    try:
        for b in range(0, len(idx), batch):
            chunk = idx[b:b + batch]
            qmap = {i: queries(rows.at[i, "name"], _legal(rows.loc[i]), rows.at[i, "address_city"],
                               rows.at[i, "district"], rows.at[i, "address_street"]) for i in chunk}
            todo = sorted({q for qs in qmap.values() for q in qs if cache.get(q) is None})
            fails = run_batch(session, cache, todo, QPS, engines)
            if todo and fails > len(todo) / 2:
                bad_batches += 1
                print(f"[search:{tag}] {fails}/{len(todo)} answers failed — waiting 60 s and retrying the batch once")
                time.sleep(60)
                retry = [q for q in todo if cache.get(q) is None]
                fails = run_batch(session, cache, retry, QPS, engines)
                if bad_batches >= 3:
                    raise RuntimeError("search engine unresponsive for three batches in a row — stopping (cache kept)")
            else:
                bad_batches = 0
            n_q += len(todo) - fails
            n_fail += fails
            for i, qs in qmap.items():
                per_row[i] = {q: cache.get(q) for q in qs if cache.get(q) is not None}
            cache.save()
            print(f"[search:{tag}] {min(b + batch, len(idx))}/{len(idx)} companies, {n_q} new queries, {n_fail} failed "
                  f"({time.time() - t0:.0f} s)")
    finally:
        cache.save()
    per_row = {i: res for i, res in per_row.items() if res}
    freq: dict[str, int] = {}
    for res in per_row.values():
        hosts = {_registered(host_of(h["url"]) or "") for hits in res.values() for h in hits}
        for h in hosts:
            if h:
                freq[h] = freq.get(h, 0) + 1
    return per_row, freq


# --- train / run ------------------------------------------------------------------------------

def train(data_root: Path, scope: str, industrial_only: bool = False, limit: int | None = None) -> Path:
    import geopandas as gpd
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import GroupKFold

    gdf = gpd.read_parquet(paths.merged_parquet(data_root, scope, "4326"))
    rows = _company_rows(gdf, verified=True, industrial_only=industrial_only)
    if limit:
        rows = rows.head(limit)
    print(f"[search:train] {scope}: {len(rows)} companies with a verified own site")
    per_row, freq = _collect(data_root, rows, "train")
    frames = []
    for i, res in per_row.items():
        r = rows.loc[i]
        f = host_features(r["name"], r.get("address_city"), r.get("address_postcode"), r.get("address_street"),
                          r.get("legal_form"), res, freq)
        if f.empty:
            continue
        f["company"] = r["id"]
        f["label"] = (f["host"] == _registered(str(r["website_host"]))).astype(int)
        frames.append(f)
    X = pd.concat(frames, ignore_index=True)
    found = X.groupby("company")["label"].max()
    print(f"[search:train] {len(X)} candidate hosts, {int(X['label'].sum())} positives; known site among the "
          f"results for {int(found.sum())} of {len(found)} companies ({found.mean():.2f} = recall ceiling)")
    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_depth=4, random_state=0)
    groups = X["company"].values
    top1 = precision_hits = precision_n = 0
    probs = np.zeros(len(X))
    for tr, te in GroupKFold(n_splits=5).split(X, X["label"], groups):
        model.fit(X.iloc[tr][FEATURES], X.iloc[tr]["label"])
        probs[te] = model.predict_proba(X.iloc[te][FEATURES])[:, 1]
    X["p"] = probs
    for cid, g in X.groupby("company"):
        best = g.sort_values("p", ascending=False).iloc[0]
        if best["label"] == 1:
            top1 += 1
        if best["p"] >= 0.5:
            precision_n += 1
            precision_hits += int(best["label"] == 1)
    print(f"[search:train] cross-validation: top-1 right for {top1}/{len(found)} companies ({top1 / len(found):.2f}); "
          f"at p ≥ 0.5 the top host is right {precision_hits}/{precision_n} times ({precision_hits / max(precision_n, 1):.2f})")
    model.fit(X[FEATURES], X["label"])
    imp = pd.Series(model.feature_importances_ if hasattr(model, "feature_importances_") else np.zeros(len(FEATURES)), index=FEATURES)
    _ = imp
    out = model_path(data_root)
    joblib.dump({"model": model, "features": FEATURES, "trained_at": time.strftime("%Y-%m-%d"), "n_companies": len(found),
                 "cv_top1": top1 / len(found), "cv_precision_05": precision_hits / max(precision_n, 1)}, out)
    X.to_parquet(data_root / "geoextract" / "web" / "search_train_features.parquet", index=False)
    print(f"[search:train] model → {out}")
    return out


def run(data_root: Path, scope: str, industrial_only: bool = True, limit: int | None = None, topk: int = 3,
        threshold: float = 0.3, workers: int = 8, dry_run: bool = False) -> pd.DataFrame:
    import geopandas as gpd
    import joblib

    from .. import export
    gdf = gpd.read_parquet(paths.merged_parquet(data_root, scope, "4326"))
    impressum.ensure_columns(gdf)
    if "website_score" not in gdf.columns:
        gdf["website_score"] = pd.array([None] * len(gdf), dtype="Float64")
    prev = gdf["website_source"] == "search"
    if prev.any():
        for c in ["website", "website_host", "website_kind", "website_source", "website_verified", "website_verified_at",
                  "website_score", "legal_name"] + impressum.EXTRA_COLUMNS:
            gdf.loc[prev, c] = pd.NA
        print(f"[search] reset {int(prev.sum())} rows of an earlier run")
    rows = _company_rows(gdf, verified=False, industrial_only=industrial_only)
    places = place_keys(gdf, data_root)
    reasons = rows["name"].map(lambda n: skip_reason(n, places))
    print(f"[search] skipped before any query: {reasons.dropna().value_counts().to_dict()} of {len(rows)}")
    rows = rows[reasons.isna()]
    if limit:
        rows = rows.head(limit)
    bundle = joblib.load(model_path(data_root))
    model = bundle["model"]
    print(f"[search] {scope}: {len(rows)} companies without a verified site; model of {bundle['trained_at']} "
          f"({bundle['n_companies']} training companies, cv top-1 {bundle['cv_top1']:.2f})")
    per_row, freq = _collect(data_root, rows, "run")
    cand: dict = {}
    for i, res in per_row.items():
        r = rows.loc[i]
        f = host_features(r["name"], r.get("address_city"), r.get("address_postcode"), r.get("address_street"),
                          r.get("legal_form"), res, freq)
        if f.empty:
            continue
        f["p"] = model.predict_proba(f[FEATURES])[:, 1]
        f = f[(f["p"] >= threshold) & (f["listed"] == 0)].sort_values("p", ascending=False).head(topk)
        if len(f):
            cand[i] = list(zip(f["host"], f["p"]))
    hosts = sorted({h for hs in cand.values() for h, _ in hs})
    print(f"[search] {len(cand)} companies with candidates above p ≥ {threshold}, {len(hosts)} hosts to verify")
    if dry_run:
        for i, hs in list(cand.items())[:40]:
            print(f"  {str(rows.at[i, 'name'])[:40]:40} → {[(h, round(p, 2)) for h, p in hs]}")
        return rows
    recs = impressum.fetch_hosts(data_root, hosts, workers=workers, tag="search")
    today = time.strftime("%Y-%m-%d")
    rank = {"name+plz": 0, "name": 1}
    n_fill, verdicts = 0, {}
    for i, hs in cand.items():
        best = None
        for host, p in hs:
            rec = recs.get(host)
            if rec is None:
                continue
            v = impressum.verdict(rec, rows.at[i, "name"], rows.at[i, "address_postcode"])
            verdicts[v] = verdicts.get(v, 0) + 1
            if v in rank and ccindex.accept(rec, v, rows.at[i, "name"], host.split(".")[0]) \
                    and (best is None or rank[v] < rank[best[1]]):
                best = (host, v, rec, p)
        if best is None:
            continue
        host, v, rec, p = best
        n_fill += 1
        gdf.at[i, "website"] = f"https://{host}"
        gdf.at[i, "website_host"] = host
        gdf.at[i, "website_kind"] = "own"
        gdf.at[i, "website_source"] = "search"
        gdf.at[i, "website_score"] = round(float(p), 3)
        impressum.write_row(gdf, i, rec, v, today)
    print(f"[search] candidate verdicts {verdicts}; verified sites written: {n_fill} of {len(rows)} rows")
    for p in [*export.write_merged(gdf, data_root, scope), export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf


__all__ = ["FEATURES", "host_features", "queries", "run", "search_one", "train"]
