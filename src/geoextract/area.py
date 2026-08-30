"""Company grounds surface area: own site polygon, else containing landuse zone.

Areas are computed in a metric CRS (EPSG:32632). Building footprints/floor area are
out of scope — we never call get_buildings().
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np


def compute_grounds_area(gdf: gpd.GeoDataFrame, osm, config: dict) -> gpd.GeoDataFrame:
    """Populate grounds_area_m2 + grounds_area_source (own_polygon | landuse_zone)."""
    metric = config["crs_metric"]

    # --- Own-polygon area (preferred): the feature is itself a polygon ---
    geom_m = gdf.geometry.to_crs(metric)
    is_poly = geom_m.geom_type.isin(["Polygon", "MultiPolygon"])
    own_area = np.where(is_poly, geom_m.area, np.nan)

    # --- Landuse-zone fallback (for points / no own area) ---
    landuse = osm.get_landuse()
    wanted = set(config["landuse_values"])
    zones = landuse[landuse["landuse"].isin(wanted)][["landuse", "geometry"]].copy()
    zones = zones.to_crs(metric)
    zones["_zone_area"] = zones.geometry.area

    pts_m = gpd.GeoDataFrame(
        gdf[["id"]].copy(), geometry=gdf["_rep"].to_crs(metric).values, crs=metric
    )
    joined = gpd.sjoin(pts_m, zones[["_zone_area", "geometry"]], how="left", predicate="within")
    # A point can fall in overlapping zones — keep the smallest (most specific) plot.
    joined = joined.sort_values("_zone_area").groupby(level=0).first().reindex(gdf.index)
    zone_area = joined["_zone_area"].to_numpy()

    grounds = np.where(~np.isnan(own_area), own_area, zone_area)
    source = np.where(
        ~np.isnan(own_area), "own_polygon",
        np.where(~np.isnan(zone_area), "landuse_zone", None),
    )
    gdf["grounds_area_m2"] = np.round(grounds.astype(float), 1)
    gdf["grounds_area_source"] = source
    return gdf
