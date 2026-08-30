"""Admin boundaries → state / district / district_ags via spatial join — spec §A2.

Berlin/Hamburg (and any row missing a level-6 hit) fall back to the level-4 polygon:
the city-state doubles as its own Kreis. Bremen is the exception with two Kreise
(Bremen 04011, Bremerhaven 04012) which exist as admin_level=6 relations.
"""
from __future__ import annotations

import geopandas as gpd

from . import config


def _rep_points(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        index=gdf.index, geometry=gdf.geometry.representative_point(), crs=gdf.crs
    )


def _join_level(pts: gpd.GeoDataFrame, boundaries: gpd.GeoDataFrame, level: str):
    polys = boundaries[boundaries["admin_level"] == level][["name", "ags", "geometry"]]
    if len(polys) == 0:
        return None
    joined = gpd.sjoin(pts, polys, how="left", predicate="within")
    # A point on a shared border can hit two polygons — keep the first.
    return joined[~joined.index.duplicated(keep="first")]


def assign_admin(gdf: gpd.GeoDataFrame, boundaries: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Fill state, district, district_ags from admin polygons (within-join on rep points)."""
    gdf = gdf.copy()
    pts = _rep_points(gdf)

    lvl4 = _join_level(pts, boundaries, config.ADMIN_LEVEL_STATE)
    if lvl4 is not None:
        gdf["state"] = lvl4["name"].astype("string")
        state_ags = lvl4["ags"].astype("string")
    else:
        state_ags = None

    lvl6 = _join_level(pts, boundaries, config.ADMIN_LEVEL_DISTRICT)
    if lvl6 is not None:
        gdf["district"] = lvl6["name"].astype("string")
        gdf["district_ags"] = lvl6["ags"].astype("string")
    else:
        gdf["district"] = None
        gdf["district_ags"] = None

    # City-state fallback: the level-4 polygon doubles as the Kreis.
    no_district = gdf["district"].isna()
    if no_district.any() and lvl4 is not None:
        gdf.loc[no_district, "district"] = gdf.loc[no_district, "state"]
        if state_ags is not None:
            gdf.loc[no_district, "district_ags"] = state_ags[no_district]

    # AGS fallback: some states' level-4 relation is broken/absent in clipped PBFs
    # (Brandenburg, Sachsen-Anhalt) — derive the state from the Kreis-AGS prefix.
    if "state" not in gdf.columns:
        gdf["state"] = None
    no_state = gdf["state"].isna() & gdf["district_ags"].notna()
    if no_state.any():
        prefix = gdf.loc[no_state, "district_ags"].astype("string").str[:2]
        gdf.loc[no_state, "state"] = prefix.map(config.AGS_STATE_NAMES).astype("string")

    return gdf
