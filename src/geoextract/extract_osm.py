"""Extract businesses from OSM (pyrosm) and normalize to the Company schema."""
from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd

# Priority order when a feature carries several business keys — first present wins.
BUSINESS_KEYS = ["industrial", "office", "craft", "shop"]
WEBSITE_COLS = ["website", "url", "contact:website"]
PHONE_COLS = ["phone", "contact:phone"]
EMAIL_COLS = ["email", "contact:email"]


def _first(row: pd.Series, cols: list[str]) -> Any:
    """First non-empty value across ``cols`` (missing columns ignored)."""
    for c in cols:
        if c in row.index:
            v = row[c]
            if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
                return v
    return None


def extract_businesses(osm, config: dict) -> gpd.GeoDataFrame:
    """Pull business POIs and map OSM tag soup onto Company fields (source='osm')."""
    custom_filter = {k: True for k, on in config["osm_business_tags"].items() if on}
    pois = osm.get_pois(custom_filter=custom_filter)
    if pois is None or len(pois) == 0:
        raise RuntimeError("no POIs returned — check the tag filter / PBF")

    present_keys = [k for k in BUSINESS_KEYS if k in pois.columns]

    def _btype(row):
        for k in present_keys:
            v = row.get(k)
            if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
                return k, v
        return None, None

    records = []
    for _, row in pois.iterrows():
        btype, bsub = _btype(row)
        osm_type = row.get("osm_type") or "n"
        prefix = {"node": "n", "way": "w", "relation": "r"}.get(osm_type, str(osm_type)[:1])
        street = _first(row, ["addr:street"])
        hnr = _first(row, ["addr:housenumber"])
        city = _first(row, ["addr:city"])
        plz = _first(row, ["addr:postcode"])
        full = ", ".join(
            p for p in [
                " ".join(x for x in [street, hnr] if x),
                " ".join(x for x in [plz, city] if x),
            ] if p
        ) or None
        records.append({
            "id": f"osm_{prefix}{row.get('id')}",
            "name": _first(row, ["name"]),
            "business_type": btype,
            "business_subtype": bsub,
            "address_street": street,
            "address_housenumber": hnr,
            "address_postcode": plz,
            "address_city": city,
            "address_full": full,
            "website": _first(row, WEBSITE_COLS),
            "phone": _first(row, PHONE_COLS),
            "email": _first(row, EMAIL_COLS),
            "source": "osm",
            "source_count": 1,
        })

    gdf = gpd.GeoDataFrame(records, geometry=pois.geometry.values, crs=pois.crs)
    # Representative point (inside polygons; identity for points) for lat/lng + joins.
    reps = gdf.geometry.representative_point()
    gdf["longitude"] = reps.x
    gdf["latitude"] = reps.y
    gdf["_rep"] = reps
    return gdf


def assign_admin(gdf: gpd.GeoDataFrame, osm, config: dict) -> gpd.GeoDataFrame:
    """Spatial-join each business to its containing state + district boundary."""
    boundaries = osm.get_boundaries(boundary_type="administrative")
    levels = config["admin_levels"]
    pts = gpd.GeoDataFrame(gdf[["id"]].copy(), geometry=gdf["_rep"].values, crs=gdf.crs)

    for field, level in (("state", levels["state"]), ("district", levels["district"])):
        polys = boundaries[boundaries["admin_level"] == str(level)][["name", "geometry"]]
        if len(polys) == 0:
            gdf[field] = None
            continue
        joined = gpd.sjoin(pts, polys, how="left", predicate="within")
        joined = joined[~joined.index.duplicated(keep="first")]
        gdf[field] = joined["name"].values

    return gdf
