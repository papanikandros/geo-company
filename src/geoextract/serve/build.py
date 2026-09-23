"""S1 — serve build: the merged Company table → everything the map + API serve statically.

Output ``data/serve/{scope}/{version}/`` (serve-plan.md §1):

``companies_flat.parquet``  tier 1: contract columns minus ``email`` + website hygiene +
                            ``member_ids``, sorted by state → district → id (row groups of
                            SERVE_ROW_GROUP rows, so DuckDB skips groups on state filters)
``companies_full.parquet``  tier 1 + tier 2: one NESTED column per merged source (``osm``,
                            ``ied``, ``abwaerme``, ``mastr``, ``overture``), each a
                            LIST<STRUCT> of that source's raw adapter records for the company
                            (own name / address / website / coordinates + the source-specific
                            fields). The flattened internal debug columns (abw_*, ovt_*, …)
                            are NOT here — every value exists once, inside its source record.
``sites.parquet``           id + polygon geometry for companies with an own grounds polygon
``extracts/{state|scope}/{sector|all|industrial}_{flat,full}.parquet``
                            precomputed downloads (parquet only; CSV/GeoJSON on demand, S3)
``companies.pmtiles``       three layers (points z4–14 / sites z12+ / landuse z10+), built
                            with tippecanoe when installed (skipped with a warning otherwise)
``search.parquet``          id, name, name_key, coordinates, state, district
``manifest.json``           version, scope, counts, per-file sha256 + size, licence block

Idempotent per version directory; ``force`` rebuilds. Personal data: ``email`` is dropped
from every tier (SERVE_PUBLIC_DROP); register officer data never exists in our tables.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd

from .. import config, paths
from ..sources import abwaerme
from ..resolve import normalize_name
from ..schema import COMPANY_COLUMNS
from . import groups

SOURCE_KEYS = list(config.SERVE_SOURCE_PREFIXES)


# --- helpers -----------------------------------------------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]


def public_columns(available: list[str]) -> list[str]:
    """Tier 1 column order: contract order minus SERVE_PUBLIC_DROP, then the public extras."""
    cols = [c for c in COMPANY_COLUMNS if c not in config.SERVE_PUBLIC_DROP and c in available]
    cols += [c for c in config.SERVE_EXTRA_PUBLIC if c in available and c not in cols]
    return cols


def source_files(data_root: Path, source: str, states: list[str]) -> list[Path]:
    """Per-source parquet(s) behind the merged table: OSM per state, the rest DE-wide."""
    if source == "osm":
        name_to_slug = {v: k for k, v in config.SLUG_STATE_NAMES.items()}
        out = []
        for st in states:
            slug = name_to_slug.get(st)
            if slug:
                p = paths.source_parquet(data_root, "osm", slug)
                if p.exists():
                    out.append(p)
        return out
    p = paths.source_parquet(data_root, source, "DE")
    return [p] if p.exists() else []


def _safe(name: str) -> str:
    tr = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", " ": "-", "/": "-"})
    return name.lower().translate(tr)


# --- tiles -------------------------------------------------------------------------------

def tippecanoe_available() -> bool:
    return shutil.which("tippecanoe") is not None


TILE_SOURCE_COLS = ["id", "name", "business_type", "source", "source_count", "is_industrial",
                    "state", "district_ags", "website", "nace_section", "ied_activity",
                    "abw_heat_mwh_a", "mastr_techs", "grounds_area_source",
                    "register_match", "hr_status", "wd_id", "wd_operator_id", "wd_brand_id", "website_source",
                    "latitude", "longitude"]


def _point_props(r: dict) -> dict:
    """Tile properties of one company (short keys keep tiles small). Derived group values
    are pure 1:1 reads of native source concepts (serve/groups.py)."""
    p = {"id": r["id"]}
    if r.get("name"):
        p["name"] = r["name"]
    if r.get("business_type"):
        p["bt"] = r["business_type"]
    p["src"] = r.get("source") or "?"
    p["sc"] = int(r.get("source_count") or 1)
    if r.get("is_industrial"):
        p["ind"] = 1
    if r.get("state"):
        p["st"] = r["state"]
    if r.get("district_ags"):
        p["ags"] = r["district_ags"]
    if r.get("website"):
        p["web"] = 1
    if r.get("nace_section"):
        p["nace"] = r["nace_section"]
    g = groups.activity_group(r.get("ied_activity"))
    if g:
        p["ied"] = g
    b = groups.heat_band(r.get("abw_heat_mwh_a"))
    if b:
        p["abw"] = b
    t = groups.techs_key(r.get("mastr_techs"))
    if t:
        p["mt"] = t
    if r.get("grounds_area_source") == "own_polygon":
        p["poly"] = 1
    hr = register_class(r.get("register_match"), r.get("hr_status"))
    if hr:
        p["hr"] = hr
    wd = wikidata_key(r)
    if wd:
        p["wd"] = wd
    return p


def wikidata_key(r: dict) -> str | None:
    """Which Wikidata links a row has, as a delimited key for the tile filter:
    e = the entity itself, o = its operator, b = its brand, w = website filled from Wikidata."""
    parts = [k for k, col in (("e", "wd_id"), ("o", "wd_operator_id"), ("b", "wd_brand_id")) if r.get(col)]
    if r.get("website_source") == "wikidata":
        parts.append("w")
    return "+" + "+".join(parts) + "+" if parts else None


def register_class(match: object, status: object) -> str | None:
    """Register verdict class for the map's register row: matched_active | matched_dissolved |
    ambiguous | none; None for unnamed rows (n/a) or tables without the join."""
    if match is None or match is pd.NA or match in ("", "n/a"):
        return None
    if match == "ambiguous":
        return "ambiguous"
    if match == "none":
        return "none"
    return "matched_dissolved" if status == "dissolved" else "matched_active"


def _iter_points(con: duckdb.DuckDBPyConnection, merged: Path):
    """(lon, lat, props) for every company with coordinates — shared by the tile writer and
    props.parquet so both carry exactly the same display properties."""
    have = set(_columns(con, merged))
    cols = [c for c in TILE_SOURCE_COLS if c in have]
    rows = con.execute(
        f"SELECT {', '.join(_q(c) for c in cols)} FROM read_parquet('{merged}') "
        f"WHERE longitude IS NOT NULL AND latitude IS NOT NULL")
    while True:
        batch = rows.fetchmany(50_000)
        if not batch:
            break
        for vals in batch:
            r = {c: (None if v is None or v is pd.NA else v) for c, v in zip(cols, vals)}
            yield float(r["longitude"]), float(r["latitude"]), _point_props(r)


PROPS_COLS = ["id", "name", "bt", "src", "sc", "ind", "st", "ags", "web", "nace", "ied", "abw", "mt", "poly", "hr", "wd"]


def write_props(con: duckdb.DuckDBPyConnection, merged: Path, out: Path) -> int:
    """props.parquet — the tile display properties per company (id, bt, src, sc, ind, st, ags,
    web, nace, ied, abw, mt, poly). The API evaluates the map's toggle model against it, so a
    box / lasso selection is computed over the FULL data with the same semantics as the map
    at any zoom (tiles drop points below zoom 11)."""
    recs = [p for _lon, _lat, p in _iter_points(con, merged)]
    df = pd.DataFrame.from_records(recs, columns=PROPS_COLS)
    for c in ("sc", "ind", "web", "poly"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("Int64")
    for c in PROPS_COLS:
        if c not in ("sc", "ind", "web", "poly"):
            df[c] = df[c].astype("string")
    df.to_parquet(out, index=False)
    return len(df)


def register_only_parquet(data_root: Path, scope: str) -> Path:
    return data_root / "geoextract" / f"register_only_{scope}_4326.parquet"


def wikidata_items_parquet(data_root: Path) -> Path:
    return data_root / "geoextract" / "src_wikidata" / "wikidata_items.parquet"


_COORD_RE = re.compile(r"Point\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", re.IGNORECASE)


def wikidata_only(con: duckdb.DuckDBPyConnection, merged: Path, items: Path) -> pd.DataFrame:
    """Wikidata items referenced by the scope (operator or brand of a map row) that carry
    coordinates but are NOT the entity item of any map row: companies Wikidata knows at a
    place where the map has no entity of their own. Columns: qid, label, role, lon, lat,
    website, industry."""
    have = set(_columns(con, merged))
    if not items.exists() or "wd_id" not in have:
        return pd.DataFrame(columns=["qid", "label", "role", "lon", "lat", "website", "industry"])
    ids = con.execute(f"""
        SELECT list(DISTINCT wd_id) FILTER (wd_id IS NOT NULL),
               list(DISTINCT wd_operator_id) FILTER (wd_operator_id IS NOT NULL),
               list(DISTINCT wd_brand_id) FILTER (wd_brand_id IS NOT NULL)
        FROM read_parquet('{merged}')""").fetchone()
    ent, ops, brands = (set(x or []) for x in ids)
    it = pd.read_parquet(items, columns=["qid", "label", "coord", "website", "industry"])
    it = it[it["coord"].notna() & ~it["qid"].isin(ent) & it["qid"].isin(ops | brands)]
    # only inside the scope: the map's own extent (percentile bbox, outliers ignored) + 2 km
    x0, y0, x1, y1 = con.execute(f"""
        SELECT quantile_cont(longitude, 0.005) - 0.03, quantile_cont(latitude, 0.005) - 0.02,
               quantile_cont(longitude, 0.995) + 0.03, quantile_cont(latitude, 0.995) + 0.02
        FROM read_parquet('{merged}')""").fetchone()
    rows = []
    for r in it.itertuples(index=False):
        m = _COORD_RE.match(str(r.coord))
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        rows.append({"qid": r.qid, "label": r.label, "role": "operator" if r.qid in ops else "brand",
                     "lon": lon, "lat": lat,
                     "website": r.website if isinstance(r.website, str) else None,
                     "industry": r.industry if isinstance(r.industry, str) else None})
    return pd.DataFrame(rows, columns=["qid", "label", "role", "lon", "lat", "website", "industry"]).astype(
        {"website": "object", "industry": "object"})


def _write_tile_features(con: duckdb.DuckDBPyConnection, merged: Path, sites: Path | None,
                         landuse: list[Path], out: Path, register_only: Path | None = None,
                         wd_only: pd.DataFrame | None = None, out_register: Path | None = None) -> dict[str, int]:
    """GeoJSON text sequence (one feature per line) with per-feature tippecanoe layer/zoom.
    Points come from the MERGED table (internal debug columns feed the display groups);
    the tiles are a rendering product, not a download."""
    counts = {"points": 0, "sites": 0, "landuse": 0, "register": 0, "wikidata": 0}
    bt_of: dict[str, str] = {}
    # the register-only companies are written separately: they are the bulk of a city tile
    # (Hamburg zoom 12 before the split: 7.7 MB of 11.2 MB) and they are registered SEATS,
    # so they keep a byte budget while the company dots are built complete
    reg_fh = open(out_register, "w", encoding="utf8") if out_register is not None else None
    with open(out, "w", encoding="utf8") as fh:
        if wd_only is not None and len(wd_only):   # item 6c wikidata-only companies
            for r in wd_only.to_dict("records"):
                props = {k: v for k, v in (("id", r["qid"]), ("name", r["label"]), ("role", r["role"]),
                                           ("web", r["website"]), ("ind", r["industry"])) if isinstance(v, str) and v}
                feat = {"type": "Feature", "tippecanoe": {"layer": "wikidata", "minzoom": config.SERVE_TILE_MINZOOM},
                        "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]}, "properties": props}
                fh.write(json.dumps(feat, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                counts["wikidata"] += 1
        if register_only is not None and register_only.exists():   # stage 4 register-only companies
            # every ACTIVE register company that could be geocoded goes in (1.36 M DE-wide);
            # dissolved ones and those without a usable address stay in the download only
            cols = ["hr_id", "name", "hr_source", "legal_form", "hr_registration", "hr_geocode_method",
                    "hr_industrial", "objective", "status", "geometry"]
            ro = gpd.read_parquet(register_only, columns=cols)
            ro = ro[ro.geometry.notna() & (ro["status"] == "active")]
            for r in ro.itertuples(index=False):
                props = {"id": r.hr_id, "name": r.name, "src": r.hr_source, "lf": r.legal_form,
                         "reg": r.hr_registration, "geo": r.hr_geocode_method,
                         "ind": 1 if r.hr_industrial else 0}
                if isinstance(r.objective, str) and r.objective:
                    props["obj"] = r.objective[:240]
                props = {k: v for k, v in props.items() if v is not None and v is not pd.NA}
                zoom = (config.SERVE_TILE_REGISTER_INDUSTRIAL_MINZOOM if r.hr_industrial
                        else config.SERVE_TILE_REGISTER_MINZOOM)
                feat = {"type": "Feature", "tippecanoe": {"layer": "register", "minzoom": zoom},
                        "geometry": {"type": "Point", "coordinates": [float(r.geometry.x), float(r.geometry.y)]},
                        "properties": props}
                (reg_fh or fh).write(json.dumps(feat, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                counts["register"] += 1
        for lon, lat, p in _iter_points(con, merged):
            if p.get("poly") and p.get("bt"):
                bt_of[p["id"]] = p["bt"]
            feat = {"type": "Feature",
                    "tippecanoe": {"layer": "points", "minzoom": config.SERVE_TILE_MINZOOM},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": p}
            fh.write(json.dumps(feat, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            counts["points"] += 1
        if sites is not None and sites.exists():
            g = gpd.read_parquet(sites)
            for cid, geom in zip(g["id"], g.geometry):
                if geom is None or geom.is_empty:
                    continue
                props = {"id": cid}
                if cid in bt_of:
                    props["bt"] = bt_of[cid]
                feat = {"type": "Feature",
                        "tippecanoe": {"layer": "sites", "minzoom": config.SERVE_TILE_SITES_MINZOOM},
                        "geometry": geom.__geo_interface__, "properties": props}
                fh.write(json.dumps(feat, separators=(",", ":")) + "\n")
                counts["sites"] += 1
        for lp in landuse:
            lu = gpd.read_parquet(lp)
            for typ, geom in zip(lu["landuse"], lu.geometry):
                if geom is None or geom.is_empty:
                    continue
                feat = {"type": "Feature",
                        "tippecanoe": {"layer": "landuse", "minzoom": config.SERVE_TILE_LANDUSE_MINZOOM},
                        "geometry": geom.__geo_interface__, "properties": {"landuse": str(typ)}}
                fh.write(json.dumps(feat, separators=(",", ":")) + "\n")
                counts["landuse"] += 1
    if reg_fh is not None:
        reg_fh.close()
    return counts


def build_ui_model(con: duckdb.DuckDBPyConnection, merged: Path, version: str,
                   data_root: Path | None = None, scope: str | None = None) -> dict:
    """ui.json — everything the map page needs to draw the toggle rows with counts and the
    state / district / sector selectors, without touching the parquet: dataset rows with
    match + group counts (same semantics as the canonical preview), states + districts with
    bounding boxes, the business-type palette."""
    have = set(_columns(con, merged))
    cols = [c for c in TILE_SOURCE_COLS if c in have and c != "name"] + (
        ["district"] if "district" in have else []) + (["hr_status"] if "hr_status" in have and "hr_status" not in TILE_SOURCE_COLS else [])
    df = con.execute(f"SELECT {', '.join(_q(c) for c in cols)} FROM read_parquet('{merged}')").fetchdf()
    for c in ("source", "business_type", "state", "district", "district_ags", "nace_section"):
        if c in df.columns:
            df[c] = df[c].astype("string")
    src = df["source"].fillna("?")
    ds: dict[str, dict] = {}
    osm_mask = src.str.contains(r"(?:^|\+)osm(?:\+|$)", regex=True)
    cat = df["business_type"].fillna("∅") if "business_type" in df.columns else pd.Series("∅", index=df.index)
    ds["osm"] = {"n": int(osm_mask.sum()),
                 "cats": {str(k): int(v) for k, v in cat[osm_mask].value_counts().items()},
                 "nSurf": int(((df.get("grounds_area_source") == "own_polygon") & osm_mask).sum())
                 if "grounds_area_source" in df.columns else 0}
    multi = df["source_count"].fillna(1) > 1
    for key in ("ied", "abwaerme", "overture", "mastr"):
        m = src.str.contains(rf"(?:^|\+){key}(?:\+|$)", regex=True)
        if not m.any():
            continue
        entry = {"n": int(m.sum()),
                 "match": {"matched": int((m & multi).sum()), "only": int((m & ~multi).sum())},
                 "groups": {}}
        if key == "ied" and "ied_activity" in df.columns:
            grp = df.loc[m, "ied_activity"].map(groups.activity_group)
        elif key == "abwaerme" and "abw_heat_mwh_a" in df.columns:
            grp = df.loc[m, "abw_heat_mwh_a"].map(groups.heat_band)
        elif key == "overture":
            grp = cat[m].replace("∅", "unmapped")
        elif key == "mastr" and "mastr_techs" in df.columns:
            grp = df.loc[m, "mastr_techs"].fillna("unknown").astype(str).str.split("+").explode()
        else:
            grp = pd.Series(dtype="string")
        entry["groups"] = {str(k): int(v) for k, v in grp.dropna().value_counts().items()}
        ds[key] = entry
    states = []
    if "state" in df.columns:
        for st, g in df.dropna(subset=["state"]).groupby("state"):
            states.append({"name": str(st), "n": len(g),
                           "bbox": [float(g["longitude"].quantile(0.005)), float(g["latitude"].quantile(0.005)),
                                    float(g["longitude"].quantile(0.995)), float(g["latitude"].quantile(0.995))]})
    districts = []
    if "district_ags" in df.columns:
        for (ags, name, st), g in df.dropna(subset=["district_ags"]).groupby(
                ["district_ags", df.get("district", pd.Series("", index=df.index)).fillna(""),
                 df.get("state", pd.Series("", index=df.index)).fillna("")]):
            districts.append({"ags": str(ags), "name": str(name), "state": str(st), "n": len(g),
                              "bbox": [float(g["longitude"].quantile(0.005)), float(g["latitude"].quantile(0.005)),
                                       float(g["longitude"].quantile(0.995)), float(g["latitude"].quantile(0.995))]})
    register = None
    register_only = None
    ro_path = register_only_parquet(data_root, scope) if data_root is not None else None
    if ro_path is not None and ro_path.exists():
        ro = pd.read_parquet(ro_path, columns=["status", "hr_industrial", "hr_geocode_method"])
        ro = ro[(ro["status"] == "active") & (ro["hr_geocode_method"] != "none")]
        register_only = {"industrial": int(ro["hr_industrial"].sum()), "other": int((~ro["hr_industrial"]).sum())}
    if "wd_id" in df.columns and data_root is not None:
        wd_only_n = len(wikidata_only(con, merged, wikidata_items_parquet(data_root)))
    else:
        wd_only_n = 0
    if "register_match" in df.columns:
        cls = pd.Series([register_class(m, st) for m, st in zip(df["register_match"], df["hr_status"])])
        register = {str(k): int(v) for k, v in cls.dropna().value_counts().items()}
        register["matched"] = register.get("matched_active", 0) + register.get("matched_dissolved", 0)
        register["only"] = sum(register_only.values()) if register_only else 0
    wikidata = None
    if "wd_id" in df.columns:
        recs = df[["wd_id", "wd_operator_id", "wd_brand_id", "website_source"]].to_dict("records")
        keys = pd.Series([wikidata_key({k: (None if pd.isna(v) else v) for k, v in r.items()}) for r in recs]).dropna()
        wikidata = {name: int(keys.str.contains(f"+{k}+", regex=False).sum())
                    for k, name in (("e", "entity"), ("o", "operator"), ("b", "brand"), ("w", "website"))}
        wikidata = {k: v for k, v in wikidata.items() if v}
        # "matched" = rows validated by an item of the entity itself or of its operator (a row
        # may carry both; the brand item alone says nothing about the site, so it does not count)
        wikidata["matched"] = int((keys.str.contains("+e+", regex=False) | keys.str.contains("+o+", regex=False)).sum())
        wikidata["only"] = wd_only_n
    sectors = sorted(cat[cat != "∅"].unique().tolist())
    palette = {c: groups.PALETTE[i % len(groups.PALETTE)] for i, c in enumerate(sorted(cat.unique()))}
    # scope bbox from the STATE bboxes (a few rows carry far-away coordinates — geocoded
    # register sites, Overture bbox spill — that would blow the initial view up to half of DE)
    if states:
        bbox = [min(s["bbox"][0] for s in states), min(s["bbox"][1] for s in states),
                max(s["bbox"][2] for s in states), max(s["bbox"][3] for s in states)]
    else:
        bbox = [float(df["longitude"].min()), float(df["latitude"].min()),
                float(df["longitude"].max()), float(df["latitude"].max())]
    return {"version": version, "companies": len(df), "bbox": bbox,
            "tile_minzoom": config.SERVE_TILE_MINZOOM, "tile_maxzoom": config.SERVE_TILE_MAXZOOM,
            "datasets": ds, "register": register, "register_only": register_only, "wikidata": wikidata,
            "labels": groups.DS_LABELS, "grp_order": groups.GRP_ORDER,
            "palette": palette, "match_colours": groups.MATCH_COLOURS,
            "ied_labels": groups.IED_ACTIVITY_LABELS,
            "lu_colours": groups.LU_COLOURS, "unit_of": groups.UNIT_OF,
            "states": states, "districts": sorted(districts, key=lambda d: d["name"]),
            "sectors": sectors, "sector_column": config.SERVE_SECTOR_COLUMN}


def build_tiles(features: Path, out: Path, register_features: Path | None = None) -> None:
    """Up to three tippecanoe passes, joined into one archive.

    Overview (SERVE_TILE_MINZOOM … SERVE_TILE_COMPLETE_FROM - 1): a tile that would burst
    SERVE_TILE_MAX_BYTES loses its densest points. Thousands of dots share a pixel there, so
    the loss is invisible — and without it the zoom-4 tiles held all 4.1 M points at 61 MB
    each and the map showed nothing for a minute.

    Detail (SERVE_TILE_COMPLETE_FROM … SERVE_TILE_MAXZOOM): no byte limit, no feature limit,
    no dropping — zoomed in, EVERY company dot is there. Measured before the split: a Berlin
    tile carried 20 % of its companies at zoom 12 and 57 % at 13.

    The register-only companies are built separately and keep a budget at every zoom: they
    are registered seats, several hundred of them share one address, and they made up two
    thirds of a city tile (Hamburg zoom 12: 7.7 MB of 11.2 MB).
    """
    base = ["--full-detail", "13", "--low-detail", "10", "-r1", "--cluster-distance=0",
            "--preserve-input-order", "--quiet"]
    complete_from = max(config.SERVE_TILE_COMPLETE_FROM, config.SERVE_TILE_MINZOOM)
    parts: list[Path] = []
    low, high, reg = (out.with_suffix(f".{n}.mbtiles") for n in ("low", "high", "reg"))
    try:
        subprocess.run(["tippecanoe", "-o", str(low), "--force",
                        "-Z", str(config.SERVE_TILE_MINZOOM), "-z", str(complete_from - 1),
                        "--drop-densest-as-needed",
                        "-M", str(config.SERVE_TILE_MAX_BYTES), *base, str(features)], check=True)
        parts.append(low)
        subprocess.run(["tippecanoe", "-o", str(high), "--force",
                        "-Z", str(complete_from), "-z", str(config.SERVE_TILE_MAXZOOM),
                        "--no-tile-size-limit", "--no-feature-limit", *base, str(features)], check=True)
        parts.append(high)
        if register_features is not None and register_features.exists() and register_features.stat().st_size:
            subprocess.run(["tippecanoe", "-o", str(reg), "--force",
                            "-Z", str(config.SERVE_TILE_MINZOOM), "-z", str(config.SERVE_TILE_MAXZOOM),
                            "--drop-densest-as-needed",
                            "-M", str(config.SERVE_TILE_REGISTER_MAX_BYTES), *base,
                            str(register_features)], check=True)
            parts.append(reg)
        subprocess.run(["tile-join", "-f", "-o", str(out), "-pk", "-pC", *[str(p) for p in parts]], check=True)
    finally:
        for p in (low, high, reg):
            p.unlink(missing_ok=True)


# --- main build --------------------------------------------------------------------------

def build(data_root: Path, scope: str, version: str | None = None, tiles: bool = True,
          force: bool = False) -> Path:
    merged = paths.merged_parquet(data_root, scope, "4326")
    if not merged.exists():
        raise FileNotFoundError(f"{merged} — run `geoextract extract` first")
    t_all = time.time()
    con = duckdb.connect()
    cols = _columns(con, merged)
    merged_at = con.execute(
        f"SELECT max(merged_at) FROM read_parquet('{merged}')").fetchone()[0] if "merged_at" in cols else None
    version = version or (str(merged_at) if merged_at else _dt.datetime.now(_dt.UTC).date().isoformat())
    out = paths.serve_dir(data_root, scope, version)
    manifest_path = out / "manifest.json"
    if manifest_path.exists() and not force:
        print(f"[serve] {out} exists (use --force to rebuild)")
        return out

    # tier 1 ------------------------------------------------------------------------------
    t0 = time.time()
    pub = public_columns(cols)
    flat = out / "companies_flat.parquet"
    sel = ", ".join(_q(c) for c in pub)
    con.execute(f"""
        COPY (SELECT {sel} FROM read_parquet('{merged}')
              ORDER BY state NULLS LAST, district NULLS LAST, id)
        TO '{flat}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {config.SERVE_ROW_GROUP})""")
    n_rows = con.execute(f"SELECT count(*) FROM read_parquet('{flat}')").fetchone()[0]
    states = [r[0] for r in con.execute(
        f"SELECT DISTINCT state FROM read_parquet('{flat}') WHERE state IS NOT NULL ORDER BY 1").fetchall()]
    print(f"[serve] tier 1: {n_rows} rows, {len(pub)} columns, {len(states)} states "
          f"({time.time() - t0:.0f} s)")

    # tier 2: nested raw source records -----------------------------------------------------
    t0 = time.time()
    con.execute(f"""
        CREATE TABLE members AS
        SELECT id AS company_id,
               unnest(CASE WHEN member_ids IS NULL OR member_ids = '' THEN [id]
                           ELSE string_split(member_ids, '|') END) AS member_id
        FROM read_parquet('{flat}')""")
    joins, nested_cols, counts = [], [], {}
    for key in SOURCE_KEYS:
        files = source_files(data_root, key, states)
        if not files:
            continue
        raw_cols = [c for c in _columns(con, files[0]) if c not in config.SERVE_RAW_DROP]
        raw_sel = ", ".join(_q(c) for c in raw_cols)
        file_list = ", ".join(f"'{f}'" for f in files)
        prefix = config.SERVE_SOURCE_PREFIXES[key]
        raw_src = f"(SELECT {raw_sel} FROM read_parquet([{file_list}], union_by_name = true))"
        if key == "abwaerme":
            # every workbook field of every waste-heat potential, nested under the site record
            # (the site row itself holds only the sums)
            pot = abwaerme.potentials_parquet(data_root)
            if pot.exists():
                pot_cols = [c for c in _columns(con, pot)
                            if c not in config.SERVE_RAW_DROP and c != "site_id"]
                pot_sel = ", ".join(f"{_q(c)} := {_q(c)}" for c in pot_cols)
                con.execute(f"""
                    CREATE TABLE pot AS
                    SELECT site_id, list(struct_pack({pot_sel}) ORDER BY melde_id) AS potentials
                    FROM read_parquet('{pot}') GROUP BY site_id""")
                raw_src = f"""(SELECT s0.*, pot.potentials FROM {raw_src} s0
                               LEFT JOIN pot ON pot.site_id = s0.id)"""
        con.execute(f"""
            CREATE TABLE nest_{key} AS
            SELECT m.company_id, list(s ORDER BY s.id) AS {_q(key)}
            FROM members m
            JOIN {raw_src} s
              ON s.id = m.member_id
            WHERE m.member_id LIKE '{prefix}%'
            GROUP BY m.company_id""")
        counts[key] = con.execute(f"SELECT count(*) FROM nest_{key}").fetchone()[0]
        if key == "abwaerme" and "pot.potentials" in raw_src:
            counts["abwaerme_potentials"] = int(con.execute(f"""
                SELECT coalesce(sum(list_sum(list_transform({_q(key)}, a -> len(a.potentials)))), 0)
                FROM nest_{key}""").fetchone()[0])
        joins.append(f"LEFT JOIN nest_{key} ON nest_{key}.company_id = f.id")
        nested_cols.append(f"nest_{key}.{_q(key)}")
    # the register row behind hr_id (stage 2) and the Wikidata items behind wd_* (item 6c):
    # raw records of those two sources, nested like the adapter records above
    reg_files = [p for p in (data_root / "geoextract" / "register").glob("hr_companies_*.parquet")] \
        if data_root is not None else []
    if reg_files and {"hr_id", "hr_source"} <= set(cols):
        reg_cols = [c for c in _columns(con, reg_files[0])
                    if c not in {"name_key_full", "name_key_light", "street_key"}]
        file_list = ", ".join(f"'{f}'" for f in reg_files)
        con.execute(f"""
            CREATE TABLE nest_register AS
            SELECT f.id AS company_id, list(r) AS register
            FROM read_parquet('{flat}') f
            JOIN (SELECT {', '.join(_q(c) for c in reg_cols)} FROM read_parquet([{file_list}], union_by_name = true)) r
              ON r.hr_id = f.hr_id AND r.hr_source = f.hr_source
            GROUP BY f.id""")
        counts["register"] = con.execute("SELECT count(*) FROM nest_register").fetchone()[0]
        joins.append("LEFT JOIN nest_register ON nest_register.company_id = f.id")
        nested_cols.append("nest_register.register")
    wd_items = wikidata_items_parquet(data_root) if data_root is not None else None
    if wd_items is not None and wd_items.exists() and "wd_id" in cols:
        con.execute(f"""
            CREATE TABLE nest_wikidata AS
            WITH links AS (
              SELECT id AS company_id, wd_id AS qid, 'entity' AS role FROM read_parquet('{merged}') WHERE wd_id IS NOT NULL
              UNION ALL
              SELECT id, wd_operator_id, 'operator' FROM read_parquet('{merged}') WHERE wd_operator_id IS NOT NULL
              UNION ALL
              SELECT id, wd_brand_id, 'brand' FROM read_parquet('{merged}') WHERE wd_brand_id IS NOT NULL)
            SELECT l.company_id,
                   list(struct_pack(role := l.role, qid := i.qid, label := i.label, website := i.website,
                                    industry := i.industry, lei := i.lei, legal_form := i.legal_form,
                                    parent := i.parent, parent_id := i.parent_id, inception := i.inception,
                                    dissolved := i.dissolved, coord := i.coord, hq := i.hq,
                                    opencorporates := i.opencorporates, fetched_at := i.fetched_at)
                        ORDER BY CASE l.role WHEN 'entity' THEN 0 WHEN 'operator' THEN 1 ELSE 2 END) AS wikidata
            FROM links l JOIN read_parquet('{wd_items}') i ON i.qid = l.qid
            GROUP BY l.company_id""")
        counts["wikidata"] = con.execute("SELECT count(*) FROM nest_wikidata").fetchone()[0]
        joins.append("LEFT JOIN nest_wikidata ON nest_wikidata.company_id = f.id")
        nested_cols.append("nest_wikidata.wikidata")
    full = out / "companies_full.parquet"
    con.execute(f"""
        COPY (SELECT f.*{', ' + ', '.join(nested_cols) if nested_cols else ''}
              FROM read_parquet('{flat}') f {' '.join(joins)}
              ORDER BY f.state NULLS LAST, f.district NULLS LAST, f.id)
        TO '{full}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {config.SERVE_ROW_GROUP})""")
    print(f"[serve] tier 2: nested sources {counts} ({time.time() - t0:.0f} s)")

    # sites --------------------------------------------------------------------------------
    t0 = time.time()
    sites = out / "sites.parquet"
    g = gpd.read_parquet(merged, columns=["id", "grounds_area_source", "geometry"])
    g = g[g["grounds_area_source"] == "own_polygon"][["id", "geometry"]]
    g = g[g.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].reset_index(drop=True)
    g.to_parquet(sites)
    n_sites = len(g)
    del g
    print(f"[serve] sites: {n_sites} polygons ({time.time() - t0:.0f} s)")

    # extracts -----------------------------------------------------------------------------
    t0 = time.time()
    sector_col = config.SERVE_SECTOR_COLUMN if config.SERVE_SECTOR_COLUMN in pub else "business_type"
    sectors = [r[0] for r in con.execute(
        f"SELECT DISTINCT {_q(sector_col)} FROM read_parquet('{flat}') "
        f"WHERE {_q(sector_col)} IS NOT NULL ORDER BY 1").fetchall()]
    ex_dir = out / "extracts"
    n_extracts = 0
    for state_name in [*states, None]:
        sdir = ex_dir / (_safe(state_name) if state_name else scope)
        sdir.mkdir(parents=True, exist_ok=True)
        where_state = f"state = '{state_name}'" if state_name else "TRUE"
        # "is_industrial" = the intrinsic flag (spec §6.5), distinct from business_type=industrial
        for sector in [*sectors, "is_industrial", "all"]:
            if sector == "all":
                where_sector = "TRUE"
            elif sector == "is_industrial":
                where_sector = "is_industrial"
            else:
                where_sector = f"{_q(sector_col)} = '{sector}'"
            for tier, src in (("flat", flat), ("full", full)):
                target = sdir / f"{_safe(sector)}_{tier}.parquet"
                con.execute(f"""
                    COPY (SELECT * FROM read_parquet('{src}') WHERE {where_state} AND {where_sector})
                    TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
                n_extracts += 1
    print(f"[serve] extracts: {n_extracts} files for {len(states)} states × "
          f"{len(sectors) + 2} sectors × 2 tiers ({time.time() - t0:.0f} s)")

    # search index -------------------------------------------------------------------------
    search = out / "search.parquet"
    s = con.execute(f"SELECT id, name, latitude, longitude, state, district FROM read_parquet('{flat}') "
                    "WHERE name IS NOT NULL").fetchdf()
    s["name_key"] = s["name"].map(lambda n: normalize_name(n, strip_noise=True))
    s.to_parquet(search, index=False)

    # display props (for the API's toggle-aware selections) ----------------------------------
    n_props = write_props(con, merged, out / "props.parquet")
    print(f"[serve] props.parquet: {n_props} rows")

    # tiles --------------------------------------------------------------------------------
    tile_counts: dict[str, int] = {}
    pmtiles = out / "companies.pmtiles"
    if tiles:
        if tippecanoe_available():
            t0 = time.time()
            name_to_slug = {v: k for k, v in config.SLUG_STATE_NAMES.items()}
            landuse = [paths.landuse_parquet(data_root, name_to_slug[st]) for st in states
                       if st in name_to_slug and paths.landuse_parquet(data_root, name_to_slug[st]).exists()]
            features = out / "features.geojsonl"
            features_reg = out / "features_register.geojsonl"
            tile_counts = _write_tile_features(con, merged, sites, landuse, features,
                                               register_only_parquet(data_root, scope),
                                               wikidata_only(con, merged, wikidata_items_parquet(data_root)),
                                               out_register=features_reg)
            build_tiles(features, pmtiles, features_reg)
            features.unlink()
            features_reg.unlink(missing_ok=True)
            print(f"[serve] tiles: {tile_counts} → {pmtiles.name} "
                  f"({pmtiles.stat().st_size / 1e6:.0f} MB, {time.time() - t0:.0f} s)")
        else:
            print("[serve] WARNING: tippecanoe not installed — companies.pmtiles skipped "
                  "(install it and rerun with --force, or --no-tiles to silence)")

    # ui model -----------------------------------------------------------------------------
    ui = build_ui_model(con, merged, version, data_root, scope)
    (out / "ui.json").write_text(json.dumps(ui, ensure_ascii=False), encoding="utf8")
    print(f"[serve] ui.json: datasets {list(ui['datasets'])}, {len(ui['states'])} states, "
          f"{len(ui['districts'])} districts")

    # manifest -----------------------------------------------------------------------------
    files = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files[str(p.relative_to(out))] = {"bytes": p.stat().st_size, "sha256": _sha256(p)}
    manifest = {
        "version": version, "scope": scope, "merged_at": str(merged_at),
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "companies": int(n_rows), "states": states, "sectors": sectors,
        "sector_column": sector_col, "sites": int(n_sites),
        "nested_sources": {k: int(v) for k, v in counts.items()},
        "tiles": tile_counts or None,
        "columns_flat": pub,
        "columns_full": pub + [k for k in SOURCE_KEYS + ["register", "wikidata"] if k in counts],
        "excluded": sorted(config.SERVE_PUBLIC_DROP),
        "licence": {"note": config.LICENCE_NOTE, "attribution": config.ATTRIBUTION,
                    "register": config.REGISTER_ATTRIBUTION},
        "files": files,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf8")
    con.close()
    # atomic version switch: data/serve/{scope}/current → this version (Caddy + API read it)
    current = out.parent / "current"
    tmp_link = out.parent / ".current.tmp"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(out.name)
    tmp_link.replace(current)
    print(f"[serve] {out} ({sum(f['bytes'] for f in files.values()) / 1e6:.0f} MB, "
          f"{time.time() - t_all:.0f} s total)")
    return out
