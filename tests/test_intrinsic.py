"""§6.5 intrinsic industrial signal — item 4 rules (2026-09-04)."""
import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from geoextract import pipeline


def _gdf(rows):
    df = pd.DataFrame(rows)
    return gpd.GeoDataFrame(df, geometry=[Point(8.8, 53.1)] * len(df), crs="EPSG:4326")


def test_power_generator_and_substation_need_a_name_plant_does_not():
    g = _gdf([
        {"source": "osm", "name": None, "osm_tags": json.dumps({"power": "generator"})},
        {"source": "osm", "name": "Windpark Nord", "osm_tags": json.dumps({"power": "generator"})},
        {"source": "osm", "name": None, "osm_tags": json.dumps({"power": "substation"})},
        {"source": "osm", "name": "UW Bremen-Nord", "osm_tags": json.dumps({"power": "substation"})},
        {"source": "osm", "name": None, "osm_tags": json.dumps({"power": "plant"})},
        {"source": "osm", "name": None, "osm_tags": json.dumps({"man_made": "works"})},
        {"source": "osm", "name": "Bäckerei", "osm_tags": json.dumps({"shop": "bakery"})},
    ])
    out = pipeline.apply_intrinsic_industrial(g)
    assert out.is_industrial.tolist() == [False, True, False, True, True, True, False]


def test_register_merge_and_overture_slug_carry_the_signal():
    g = _gdf([
        {"source": "mastr+osm", "name": None, "osm_tags": json.dumps({"power": "generator"})},
        {"source": "overture", "name": "Stahlwerk", "ovt_category": "metal_fabricator"},
        {"source": "overture", "name": "Stadtwerke", "ovt_category": "energy_company"},
        {"source": "overture", "name": "Kanzlei", "ovt_category": "lawyer"},
        {"source": "osm+overture", "name": "Kanzlei", "osm_tags": json.dumps({"office": "lawyer"}),
         "ovt_category": "chemical_plant"},
    ])
    out = pipeline.apply_intrinsic_industrial(g)
    assert out.is_industrial.tolist() == [True, True, True, False, True]
