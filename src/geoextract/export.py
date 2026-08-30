"""Write the merged Company table: 4326 + 3857 parquets and the summary JSON — spec §A4.

The `*_3857` map file carries point geometry only plus scalar x/y columns (the map reads
x/y, not shapely geometries). The `*_4326` file keeps full geometry for downstream work.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

from . import config, paths


def write_merged(gdf: gpd.GeoDataFrame, data_root: Path, scope: str) -> list[Path]:
    p4326 = paths.merged_parquet(data_root, scope, "4326")
    gdf.to_crs(config.CRS_STORAGE).to_parquet(p4326)

    pts = gdf.geometry.representative_point().to_crs(config.CRS_MAP)
    map_gdf = gpd.GeoDataFrame(
        pd.DataFrame(gdf.drop(columns="geometry")), geometry=pts.values, crs=config.CRS_MAP
    )
    map_gdf["x"] = pts.x.to_numpy()
    map_gdf["y"] = pts.y.to_numpy()
    p3857 = paths.merged_parquet(data_root, scope, "3857")
    map_gdf.to_parquet(p3857)
    return [p4326, p3857]


def _dist(series: pd.Series) -> dict:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if not len(s):
        return {"count": 0}
    return {
        "count": len(s),
        "min": round(float(s.min()), 1),
        "p25": round(float(s.quantile(0.25)), 1),
        "median": round(float(s.median()), 1),
        "p75": round(float(s.quantile(0.75)), 1),
        "max": round(float(s.max()), 1),
    }


def write_summary(
    gdf: gpd.GeoDataFrame, data_root: Path, scope: str,
    pbf_meta: dict[str, dict] | None = None, runtimes_s: dict[str, float] | None = None,
) -> Path:
    n = len(gdf)
    coverage_cols = ["name", "website", "phone", "email", "address_full",
                     "grounds_area_m2", "district_ags", "nace_primary"]
    summary = {
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "scope": scope,
        "total_companies": n,
        "by_state": gdf["state"].value_counts(dropna=True).to_dict(),
        "by_business_type": gdf["business_type"].value_counts(dropna=True).to_dict(),
        "by_source": gdf["source"].value_counts(dropna=True).to_dict(),
        "multi_source_clusters": int((gdf["source_count"] >= 2).sum()),
        "is_industrial": int(gdf["is_industrial"].fillna(False).sum()),
        "field_coverage": {
            c: {"count": int(gdf[c].notna().sum()),
                "share": round(float(gdf[c].notna().mean()), 3)}
            for c in coverage_cols if c in gdf.columns
        },
        "grounds_area_m2": _dist(gdf["grounds_area_m2"]),
        "confidence_score_mean": round(
            float(pd.to_numeric(gdf["confidence_score"], errors="coerce").mean()), 3),
        "pbf_files": pbf_meta or {},
        "runtimes_s": {k: round(v, 1) for k, v in (runtimes_s or {}).items()},
        "attribution": config.ATTRIBUTION,
        "licence_note": config.LICENCE_NOTE,
    }
    dest = paths.summary_json(data_root, scope)
    dest.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest
