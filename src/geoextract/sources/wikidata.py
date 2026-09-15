"""Item 6c — Wikidata enrichment by identifier (register-website-pipeline-plan.md).

The merged table already carries Wikidata identifiers without any Wikidata access:
OpenStreetMap ``wikidata`` (the entity), ``operator:wikidata`` (the operating company),
``brand:wikidata`` (the chain) and Overture's brand identifier. This module fetches the
items behind those identifiers with ONE SPARQL query per batch (≈ 1 KB per item instead of
the 50–500 KB of a full entity download): official website (P856), industry (P452), Legal
Entity Identifier (P1278 → joins the GLEIF table), legal form (P1454), parent (P749),
inception (P571), dissolution (P576), coordinates (P625), headquarters (P159),
OpenCorporates id (P1320). Results are cached in ``src_wikidata/wikidata_items.parquet``
(resumable: only missing identifiers are fetched; 1 request per second; descriptive
User-Agent per the Wikimedia policy). CC0 — no attribution obligation.

Write-back (additive): ``wd_id``, ``wd_operator_id``, ``wd_brand_id`` and, from the entity's
own item first, else the operator's: ``wd_website``, ``wd_industry``, ``wd_lei``,
``wd_legal_form``, ``wd_parent``, ``wd_inception``, ``wd_dissolved``. A company without a
website whose item (or operator item) has one gets it as ``website`` with
``website_source = wikidata``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

from .. import config, paths

SPARQL = "https://query.wikidata.org/sparql"
_QID_RE = re.compile(r"^Q\d+$")
_TAG_KEYS = {"wikidata": "wd_id", "operator:wikidata": "wd_operator_id", "brand:wikidata": "wd_brand_id"}

QUERY = """
SELECT ?item (SAMPLE(?itemLabel) AS ?label) (SAMPLE(?website) AS ?website)
       (SAMPLE(?industryLabel) AS ?industry) (SAMPLE(?lei) AS ?lei)
       (SAMPLE(?formLabel) AS ?legal_form) (SAMPLE(?parentLabel) AS ?parent)
       (SAMPLE(?parent) AS ?parent_id) (SAMPLE(?inception) AS ?inception)
       (SAMPLE(?dissolved) AS ?dissolved) (SAMPLE(?coord) AS ?coord)
       (SAMPLE(?hqLabel) AS ?hq) (SAMPLE(?oc) AS ?opencorporates)
WHERE {
  VALUES ?item { %s }
  OPTIONAL { ?item wdt:P856 ?website. }
  OPTIONAL { ?item wdt:P452 ?industry. }
  OPTIONAL { ?item wdt:P1278 ?lei. }
  OPTIONAL { ?item wdt:P1454 ?form. }
  OPTIONAL { ?item wdt:P749 ?parent. }
  OPTIONAL { ?item wdt:P571 ?inception. }
  OPTIONAL { ?item wdt:P576 ?dissolved. }
  OPTIONAL { ?item wdt:P625 ?coord. }
  OPTIONAL { ?item wdt:P159 ?hq. }
  OPTIONAL { ?item wdt:P1320 ?oc. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "de,en".
    ?item rdfs:label ?itemLabel. ?industry rdfs:label ?industryLabel.
    ?form rdfs:label ?formLabel. ?parent rdfs:label ?parentLabel. ?hq rdfs:label ?hqLabel. }
}
GROUP BY ?item
"""


def items_parquet(data_root: Path) -> Path:
    p = data_root / "geoextract" / "src_wikidata" / "wikidata_items.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def collect_ids(gdf: pd.DataFrame) -> pd.DataFrame:
    """Per map row: wd_id / wd_operator_id / wd_brand_id from OSM tags and the Overture brand."""
    out = pd.DataFrame(index=gdf.index)
    for col in _TAG_KEYS.values():
        out[col] = pd.NA
    if "osm_tags" in gdf.columns:
        for i, raw in gdf["osm_tags"].items():
            if not isinstance(raw, str) or "wikidata" not in raw:
                continue
            try:
                tags = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for key, col in _TAG_KEYS.items():
                v = tags.get(key)
                if isinstance(v, str):
                    v = v.strip().split(";")[0].strip()
                    if _QID_RE.match(v):
                        out.at[i, col] = v
    if "ovt_brand_wikidata" in gdf.columns:
        ovt = gdf["ovt_brand_wikidata"].astype("string")
        ok = ovt.notna() & ovt.str.match(r"^Q\d+$")
        out.loc[ok & out["wd_brand_id"].isna(), "wd_brand_id"] = ovt[ok & out["wd_brand_id"].isna()]
    return out.astype("string")


def _value(binding: dict, key: str):
    v = binding.get(key)
    return v["value"] if v else None


def fetch_items(qids: list[str], data_root: Path, batch: int = 150, sleep_s: float = 1.0,
                session: requests.Session | None = None) -> pd.DataFrame:
    """Fetch the missing identifiers into the cache; returns the full cached table."""
    cache = items_parquet(data_root)
    have = pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=[
        "qid", "label", "website", "industry", "lei", "legal_form", "parent", "parent_id", "inception",
        "dissolved", "coord", "hq", "opencorporates", "fetched_at"])
    todo = sorted({q for q in qids if _QID_RE.match(str(q))} - set(have["qid"]))
    if not todo:
        return have
    s = session or requests.Session()
    s.headers.update({"User-Agent": config.WIKIDATA_USER_AGENT, "Accept": "application/sparql-results+json"})
    rows, n_bytes, t0 = [], 0, time.time()
    for k in range(0, len(todo), batch):
        chunk = todo[k:k + batch]
        q = QUERY % " ".join(f"wd:{x}" for x in chunk)
        for attempt in range(4):
            try:
                r = s.get(SPARQL, params={"query": q, "format": "json"}, timeout=90)
                if r.status_code == 429:
                    time.sleep(10 * (attempt + 1)); continue
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
        n_bytes += len(r.content)
        seen = set()
        for b in r.json()["results"]["bindings"]:
            qid = _value(b, "item").rsplit("/", 1)[-1]
            seen.add(qid)
            pid = _value(b, "parent_id")
            rows.append({"qid": qid, "label": _value(b, "label"), "website": _value(b, "website"),
                         "industry": _value(b, "industry"), "lei": _value(b, "lei"),
                         "legal_form": _value(b, "legal_form"), "parent": _value(b, "parent"),
                         "parent_id": pid.rsplit("/", 1)[-1] if pid else None,
                         "inception": (_value(b, "inception") or "")[:10] or None,
                         "dissolved": (_value(b, "dissolved") or "")[:10] or None,
                         "coord": _value(b, "coord"), "hq": _value(b, "hq"),
                         "opencorporates": _value(b, "opencorporates"),
                         "fetched_at": time.strftime("%Y-%m-%d")})
        for qid in chunk:            # remember misses too, so they are not re-fetched
            if qid not in seen:
                rows.append({"qid": qid, "fetched_at": time.strftime("%Y-%m-%d")})
        print(f"[wikidata] {min(k + batch, len(todo))}/{len(todo)} items, {n_bytes / 1e6:.1f} MB "
              f"({time.time() - t0:.0f} s)")
        time.sleep(sleep_s)
    new = pd.DataFrame(rows)
    allrows = pd.concat([have, new], ignore_index=True).drop_duplicates("qid", keep="last")
    for c in allrows.columns:
        allrows[c] = allrows[c].astype("string")
    allrows.to_parquet(cache, index=False)
    return allrows


def apply_wikidata(gdf: pd.DataFrame, ids: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    """Write wd_* columns; fill an empty website from the entity's or the operator's item."""
    from ..web.hygiene import host_of, normalize_url

    gdf = gdf.copy()
    it = items.set_index("qid")
    for col in ("wd_id", "wd_operator_id", "wd_brand_id"):
        gdf[col] = ids[col].astype("string")

    def pick(col: str) -> pd.Series:
        ent = gdf["wd_id"].map(it[col]) if col in it.columns else pd.Series(pd.NA, index=gdf.index)
        op = gdf["wd_operator_id"].map(it[col]) if col in it.columns else pd.Series(pd.NA, index=gdf.index)
        return ent.where(ent.notna(), op).astype("string")

    gdf["wd_website"] = pick("website")
    gdf["wd_industry"] = pick("industry")
    gdf["wd_lei"] = pick("lei")
    gdf["wd_legal_form"] = pick("legal_form")
    gdf["wd_parent"] = pick("parent")
    gdf["wd_inception"] = pick("inception")
    gdf["wd_dissolved"] = pick("dissolved")
    fill = gdf["website"].isna() & gdf["wd_website"].notna()
    if fill.any():
        urls = gdf.loc[fill, "wd_website"].map(normalize_url).astype("string")
        ok = urls.notna()
        idx = urls[ok].index
        gdf.loc[idx, "website"] = urls[ok]
        gdf.loc[idx, "website_host"] = urls[ok].map(host_of).astype("string")
        gdf.loc[idx, "website_kind"] = "own"
        gdf.loc[idx, "website_source"] = "wikidata"
    return gdf


def enrich(data_root: Path, scope: str, fetch: bool = True) -> pd.DataFrame:
    import geopandas as gpd

    from .. import export
    src = paths.merged_parquet(data_root, scope, "4326")
    gdf = gpd.read_parquet(src)
    ids = collect_ids(gdf)
    all_ids = sorted(set(ids["wd_id"].dropna()) | set(ids["wd_operator_id"].dropna()) | set(ids["wd_brand_id"].dropna()))
    print(f"[wikidata] {scope}: {int(ids['wd_id'].notna().sum())} entity ids, "
          f"{int(ids['wd_operator_id'].notna().sum())} operator ids, {int(ids['wd_brand_id'].notna().sum())} brand ids "
          f"→ {len(all_ids)} distinct")
    items = fetch_items(all_ids, data_root) if fetch else pd.read_parquet(items_parquet(data_root))
    before = int(gdf["website"].notna().sum())
    gdf = apply_wikidata(gdf, ids, items)
    hit = items[items["qid"].isin(all_ids)]
    print(f"[wikidata] items with website {int(hit['website'].notna().sum())}, industry "
          f"{int(hit['industry'].notna().sum())}, LEI {int(hit['lei'].notna().sum())}; websites filled on the map: "
          f"{int(gdf['website'].notna().sum()) - before}")
    for p in [*export.write_merged(gdf, data_root, scope), export.write_summary(gdf, data_root, scope, None, {})]:
        print(f"[out] {p}")
    return gdf
