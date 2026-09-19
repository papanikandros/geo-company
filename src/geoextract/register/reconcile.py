"""Stage 4 — two-way diff between the map and the register.

4a map → register: ``register_match`` counts per state / district / industrial flag (from the
joined merged table, see ``summarize``).

4b register → map: every register company in the scope that no map row matched is placed on
the map by an OFFLINE geocode against the OpenStreetMap address points of the local PBF
extracts (``addr:postcode`` + ``addr:street`` + ``addr:housenumber``; fallback: the centre
of the street inside the postcode) and flagged industrial by a keyword rule on its
Unternehmensgegenstand (interim until Part C delivers real codes). Output
``register_only_{scope}_4326.parquet`` (GeoParquet points) + ``register_diff_{scope}.json``
— the "register-industrial firm with no map entity" completeness gap per district.
Only ACTIVE register companies count as a gap; dissolved ones are kept in the file for the
map but excluded from the gap numbers.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from .. import config, paths
from ..resolve import normalize_street
from . import build as register_build
from .match import normalize_city

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def addr_parquet(data_root: Path, slug: str) -> Path:
    return data_root / "geoextract" / f"addr_{slug}_4326.parquet"


def build_address_index(data_root: Path, slug: str, force: bool = False) -> gpd.GeoDataFrame:
    """OSM features with addr:housenumber of one state PBF → points keyed by postcode +
    street key (+ number). Cached per state; QuackOSM reads the local PBF, no download."""
    dest = addr_parquet(data_root, slug)
    if dest.exists() and not force:
        return gpd.read_parquet(dest)
    import quackosm

    pbf = paths.pbf_path(data_root, slug)
    if not pbf.exists():
        raise FileNotFoundError(f"{pbf} missing — the OSM extract of {slug} is needed for the address index")
    t0 = time.time()
    gdf = quackosm.convert_pbf_to_geodataframe(
        str(pbf), tags_filter={"addr:housenumber": True, "addr:street": True}, keep_all_tags=True,
        explode_tags=False, working_directory=str(paths.quackosm_workdir(data_root)))
    gdf = gdf.to_crs(config.CRS_STORAGE)
    tags = gdf["tags"]
    out = gpd.GeoDataFrame({
        "postcode": tags.map(lambda t: (t or {}).get("addr:postcode")).astype("string"),
        "street": tags.map(lambda t: (t or {}).get("addr:street")).astype("string"),
        "housenumber": tags.map(lambda t: (t or {}).get("addr:housenumber")).astype("string"),
        "city": tags.map(lambda t: (t or {}).get("addr:city")).astype("string"),
    }, geometry=gdf.geometry.representative_point(), crs=config.CRS_STORAGE)
    out = out[out["postcode"].notna() & out["street"].notna()].reset_index(drop=True)
    out["street_key"] = out["street"].map(normalize_street)
    out["addr_key"] = [normalize_street(f"{s} {n}") for s, n in zip(out["street"], out["housenumber"].fillna(""))]
    out["longitude"] = out.geometry.x
    out["latitude"] = out.geometry.y
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dest)
    print(f"[addr] {slug}: {len(out)} address points from {pbf.name} ({time.time() - t0:.0f} s)")
    return out


def industrial_from_objective(text: object) -> bool:
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return False
    t = str(text).lower().translate(_UMLAUTS)
    return any(k in t for k in config.REGISTER_INDUSTRIAL_KEYWORDS) and not any(
        k in t for k in config.REGISTER_INDUSTRIAL_EXCLUDE)


def geocode_register(companies: pd.DataFrame, addr: gpd.GeoDataFrame) -> pd.DataFrame:
    """Exact (postcode + street + number) → point; else street centre inside the postcode."""
    a_exact = addr.groupby(["postcode", "addr_key"])[["longitude", "latitude"]].mean()
    a_street = addr.groupby(["postcode", "street_key"])[["longitude", "latitude"]].mean()
    street_only = companies["street"].map(normalize_street).fillna("")
    keys_exact = list(zip(companies["postcode"].astype(str), companies["street_key"].fillna("").astype(str)))
    keys_street = list(zip(companies["postcode"].astype(str), street_only))
    ex = a_exact.reindex(keys_exact)
    st = a_street.reindex(keys_street)
    lon = ex["longitude"].values
    lat = ex["latitude"].values
    method = np.where(~np.isnan(lon), "exact", np.where(~np.isnan(st["longitude"].values), "street", "none"))
    lon = np.where(np.isnan(lon), st["longitude"].values, lon)
    lat = np.where(np.isnan(lat), st["latitude"].values, lat)
    out = companies.copy()
    out["longitude"], out["latitude"], out["hr_geocode_method"] = lon, lat, method
    return out


def reconcile(data_root: Path, scope: str, slugs: list[str]) -> tuple[Path, Path]:
    t0 = time.time()
    merged = pd.read_parquet(paths.merged_parquet(data_root, scope, "4326"),
                             columns=["id", "hr_id", "hr_source", "address_postcode", "district", "district_ags",
                                      "is_industrial", "state"])
    plz = set(merged["address_postcode"].dropna().astype(str))
    matched = set(zip(merged["hr_source"].dropna(), merged["hr_id"].dropna()))
    companies = register_build.load_companies(data_root)
    in_scope = companies[companies["postcode"].astype("string").isin(plz)].copy()
    # the same firm may sit in several register sources: keep one per (name key, postcode),
    # preferring hr2022 > gleif > hr2019 and active over dissolved
    in_scope["_pri"] = in_scope["hr_source"].map({"hr2022": 0, "gleif": 1, "hr2019": 2}).fillna(9)
    in_scope["_act"] = (in_scope["status"] == "active").astype(int)
    in_scope = in_scope.sort_values(["_pri", "_act"], ascending=[True, False]) \
        .drop_duplicates(["name_key_full", "postcode"]).reset_index(drop=True)
    in_scope["on_map"] = [(s, i) in matched for s, i in zip(in_scope["hr_source"], in_scope["hr_id"])]
    only = in_scope[~in_scope["on_map"]].copy()
    only["hr_industrial"] = only["objective"].map(industrial_from_objective)

    addr = pd.concat([build_address_index(data_root, s) for s in slugs], ignore_index=True)
    addr = gpd.GeoDataFrame(addr, geometry="geometry", crs=config.CRS_STORAGE)
    only = geocode_register(only, addr)
    # district via the postcode's most common district on the map
    plz_district = merged.dropna(subset=["address_postcode", "district"]).groupby("address_postcode")[
        ["district", "district_ags"]].agg(lambda s: s.mode().iloc[0])
    only = only.merge(plz_district, left_on="postcode", right_index=True, how="left")
    only["city_key"] = only["city"].map(normalize_city)

    geom = [Point(x, y) if not np.isnan(x) else None for x, y in zip(only["longitude"], only["latitude"])]
    gdf = gpd.GeoDataFrame(only.drop(columns=["_pri", "_act"]), geometry=geom, crs=config.CRS_STORAGE)
    # the address index comes from the state PBF, which is a rectangle around the state:
    # a register company geocoded onto a neighbouring town is not in scope
    import shapely
    from .. import paths as _paths
    polys = [gpd.read_parquet(_paths.boundaries_parquet(data_root, s)).union_all()
             for s in slugs if _paths.boundaries_parquet(data_root, s).exists()]
    if polys:
        area = polys[0] if len(polys) == 1 else shapely.union_all(polys)
        placed = gdf.geometry.notna().to_numpy()
        inside = np.zeros(len(gdf), dtype=bool)
        inside[placed] = shapely.contains_xy(area, gdf.loc[placed, "longitude"].to_numpy(),
                                             gdf.loc[placed, "latitude"].to_numpy())
        drop = placed & ~inside
        if drop.any():
            print(f"[reconcile] {int(drop.sum())} geocoded register companies outside the scope boundary → unplaced")
            gdf.loc[drop, "geometry"] = None
            gdf.loc[drop, ["longitude", "latitude"]] = np.nan
            gdf.loc[drop, "hr_geocode_method"] = "outside_scope"
    out_p = data_root / "geoextract" / f"register_only_{scope}_4326.parquet"
    gdf.to_parquet(out_p)

    active = gdf[gdf["status"] == "active"]
    gap = active[active["hr_industrial"]]
    diff = {
        "scope": scope, "generated_at": pd.Timestamp.utcnow().isoformat(timespec="seconds"),
        "register_in_scope": len(in_scope), "register_active_in_scope": int((in_scope["status"] == "active").sum()),
        "matched_by_map": int(in_scope["on_map"].sum()),
        "register_only": len(gdf), "register_only_active": len(active),
        "register_only_geocoded": {k: int(v) for k, v in gdf["hr_geocode_method"].value_counts().items()},
        "register_only_industrial_active": len(gap),
        "gap_by_district": {str(k): int(v) for k, v in gap["district"].value_counts().items()},
        "gap_by_legal_form": {str(k): int(v) for k, v in gap["legal_form"].value_counts().head(8).items()},
        "map_industrial": int(merged["is_industrial"].fillna(False).sum()),
        "map_industrial_matched": int((merged["is_industrial"].fillna(False) & merged["hr_id"].notna()).sum()),
        "seconds": round(time.time() - t0, 1),
    }
    out_j = data_root / "geoextract" / f"register_diff_{scope}.json"
    out_j.write_text(json.dumps(diff, ensure_ascii=False, indent=1), encoding="utf8")
    print(f"[reconcile] {scope}: {diff['register_in_scope']} register companies in scope, "
          f"{diff['matched_by_map']} matched by the map, {diff['register_only']} register-only "
          f"({diff['register_only_active']} active, {diff['register_only_industrial_active']} industrial-active gap), "
          f"geocoded {diff['register_only_geocoded']} ({diff['seconds']} s)")
    return out_p, out_j


_KW_SPLIT = re.compile(r"[,;]")
