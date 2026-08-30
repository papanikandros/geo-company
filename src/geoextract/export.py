"""Write outputs: CSV, GeoJSON, GeoPackage, EPSG:3857 GeoParquet (map cache) + summary."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .models import EXPORT_COLUMNS

ATTRIBUTION = "© OpenStreetMap contributors, ODbL (https://www.openstreetmap.org/copyright)"


def _flat(gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """Attribute frame for vector/CSV formats (list columns serialized to strings)."""
    df = pd.DataFrame({c: gdf[c] if c in gdf.columns else None for c in EXPORT_COLUMNS})
    df["nace_codes"] = df["nace_codes"].apply(
        lambda v: ";".join(v) if isinstance(v, list) else ("" if v is None else str(v))
    )
    return df


def export(gdf: gpd.GeoDataFrame, out_dir, scope: str, formats: list[str], config: dict) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = _dt.date.today().strftime("%Y%m%d")
    stem = f"{scope}_companies_{date}"
    written: list[Path] = []

    flat = _flat(gdf)

    if "csv" in formats:
        p = out_dir / f"{stem}.csv"
        flat.to_csv(p, index=False, encoding="utf-8")
        written.append(p)

    if "geojson" in formats or "gpkg" in formats:
        vec = gpd.GeoDataFrame(flat.copy(), geometry=gdf.geometry.values, crs=gdf.crs).to_crs(
            config["crs_output"]
        )
        if "geojson" in formats:
            p = out_dir / f"{stem}.geojson"
            vec.to_file(p, driver="GeoJSON")
            written.append(p)
        if "gpkg" in formats:
            p = out_dir / f"{stem}.gpkg"
            vec.to_file(p, driver="GPKG", layer="companies")
            written.append(p)

    if "parquet" in formats:
        # Map cache: point geometry in Web Mercator + x/y columns (app convention).
        pts = gdf["_rep"].to_crs(config["crs_map"])
        cache = gpd.GeoDataFrame(
            pd.DataFrame({c: gdf[c] if c in gdf.columns else None for c in EXPORT_COLUMNS}),
            geometry=pts.values, crs=config["crs_map"],
        )
        cache["x"] = pts.x.values
        cache["y"] = pts.y.values
        p = out_dir / f"{stem}_3857.parquet"
        cache.to_parquet(p)
        written.append(p)

    _write_summary(gdf, out_dir / f"{scope}_summary_{date}.json", scope, config)
    written.append(out_dir / f"{scope}_summary_{date}.json")
    (out_dir / "ATTRIBUTION.txt").write_text(ATTRIBUTION + "\n", encoding="utf-8")
    return written


def _write_summary(gdf, path: Path, scope: str, config: dict) -> None:
    n = len(gdf)
    area = pd.to_numeric(gdf["grounds_area_m2"], errors="coerce").dropna()
    summary = {
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "scope": scope,
        "region": config.get("region"),
        "sources_used": ["osm"],
        "total_companies": n,
        "field_coverage": {
            "has_website": int(gdf["website"].notna().sum()),
            "has_phone": int(gdf["phone"].notna().sum()),
            "has_full_address": int(gdf["address_full"].notna().sum()),
            "has_grounds_area": int(area.shape[0]),
        },
        "by_business_type": gdf["business_type"].value_counts(dropna=True).to_dict(),
        "by_grounds_source": gdf["grounds_area_source"].value_counts(dropna=True).to_dict(),
        "grounds_area_m2": {
            "min": round(float(area.min()), 1) if len(area) else None,
            "median": round(float(area.median()), 1) if len(area) else None,
            "max": round(float(area.max()), 1) if len(area) else None,
        },
        "attribution": ATTRIBUTION,
    }
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
