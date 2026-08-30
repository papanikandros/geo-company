"""Company grounds surface: own site polygon, else smallest containing landuse zone — §A2.

All areas are computed in EPSG:25832 (metric, official for Germany). Building footprints
and floor area are out of scope. A mis-set CRS produces areas of fractions of a m² or
billions — validate_frame fails loudly on those.
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np

from . import config


def compute_grounds_area(
    gdf: gpd.GeoDataFrame, landuse_zones: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Populate grounds_area_m2 + grounds_area_source (own_polygon | landuse_zone)."""
    gdf = gdf.copy()

    geom_m = gdf.geometry.to_crs(config.CRS_METRIC)
    is_poly = geom_m.geom_type.isin(["Polygon", "MultiPolygon"]).to_numpy()
    own_area = np.where(is_poly, geom_m.area, np.nan)

    zones = landuse_zones.to_crs(config.CRS_METRIC).copy()
    zones["_zone_area"] = zones.geometry.area
    zones = zones[zones["_zone_area"] > 0]

    pts = gpd.GeoDataFrame(
        index=gdf.index, geometry=geom_m.representative_point(), crs=config.CRS_METRIC
    )
    joined = gpd.sjoin(pts, zones[["_zone_area", "geometry"]], how="left", predicate="within")
    # A point can fall in overlapping zones — keep the smallest (most specific) plot.
    smallest = joined["_zone_area"].groupby(level=0).min().reindex(gdf.index)
    zone_area = smallest.to_numpy(dtype=float)

    grounds = np.where(~np.isnan(own_area), own_area, zone_area)
    source = np.where(
        ~np.isnan(own_area), "own_polygon",
        np.where(~np.isnan(zone_area), "landuse_zone", None),
    )
    gdf["grounds_area_m2"] = np.round(grounds, 1)
    gdf["grounds_area_source"] = source
    gdf["grounds_area_m2"] = gdf["grounds_area_m2"].astype("Float64")
    gdf["grounds_area_source"] = gdf["grounds_area_source"].astype("string")
    return gdf
