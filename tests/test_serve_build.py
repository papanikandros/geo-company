"""Item 7 S1 — serve build on a synthetic merged table + source parquets (no tiles)."""
import json

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point, Polygon

from geoextract.sources import abwaerme
from geoextract import paths, schema
from geoextract.serve import build

LAT, LON = 53.08, 8.80


def _gdf(rows, extra=None):
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "geometry"} for r in rows])
    g = gpd.GeoDataFrame(df, geometry=[r["geometry"] for r in rows], crs="EPSG:4326")
    reps = g.geometry.representative_point()
    g["longitude"], g["latitude"] = reps.x, reps.y
    g = schema.conform(g)
    for c, v in (extra or {}).items():
        g[c] = v
    return g


@pytest.fixture
def data_root(tmp_path):
    poly = Polygon([(LON, LAT), (LON + 0.001, LAT), (LON + 0.001, LAT + 0.001), (LON, LAT + 0.001)])
    # source tables (adapter shape + their debug columns)
    osm = _gdf([
        {"id": "osm_way/1", "name": "Weser Stahl GmbH", "business_type": "industrial", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "website": "https://weser-stahl.de",
             "email": "info@weser-stahl.de", "phone": "0421", "geometry": poly, "source": "osm"},
        {"id": "osm_node/2", "name": "Kiosk Ali", "business_type": "shop", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "geometry": Point(LON + 0.01, LAT), "source": "osm"},
    ], {"osm_tags": ['{"industrial":"factory"}', '{"shop":"kiosk"}']})
    osm.to_parquet(paths.source_parquet(tmp_path, "osm", "bremen"))
    abw = _gdf([
        {"id": "abw_9", "name": "Weser Stahl", "business_type": "industrial", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "geometry": Point(LON + 0.0005, LAT + 0.0005),
             "source": "abwaerme"},
    ], {"abw_heat_mwh_a": [1234.0], "abw_site_name": ["Weser Stahl Werk"]})
    abw.to_parquet(paths.source_parquet(tmp_path, "abwaerme", "DE"))
    pd.DataFrame({"site_id": ["abw_9", "abw_9"], "melde_id": pd.array([2, 1], dtype="Int64"),
                  "abwaermepotential": ["Abgas", "Kühlturm"], "email": ["x@y.de", None],
                  "leistungsprofil_januar_kw": [200.0, 100.0], "verfuegbarkeit": [None, "5 Tage 24 Std"]}
                 ).to_parquet(abwaerme.potentials_parquet(tmp_path), index=False)
    mastr = _gdf([
        {"id": "mastr_A1_L1", "name": "Weser Stahl GmbH", "business_type": "power", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "geometry": Point(LON + 0.0004, LAT + 0.0004),
             "source": "mastr"},
        {"id": "mastr_A1_L2", "name": "Weser Stahl GmbH", "business_type": "power", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "geometry": Point(LON + 0.0006, LAT + 0.0006),
             "source": "mastr"},
        {"id": "mastr_B7_L1", "name": "Windpark Nord GmbH & Co. KG", "business_type": "power",
             "state": "Niedersachsen", "district": "Cuxhaven", "district_ags": "03352",
             "geometry": Point(LON + 0.2, LAT + 0.2), "source": "mastr"},
    ], {"mastr_techs": ["combustion", "solar", "wind"], "mastr_units": [1, 2, 3]})
    mastr.to_parquet(paths.source_parquet(tmp_path, "mastr", "DE"))
    # merged table: cluster of osm_way/1 + abw_9 + two mastr Lokationen, plus singles
    merged = _gdf([
        {"id": "osm_way/1", "name": "Weser Stahl GmbH", "business_type": "industrial", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "website": "https://weser-stahl.de",
             "email": "info@weser-stahl.de", "phone": "0421", "grounds_area_source": "own_polygon",
             "grounds_area_m2": 8000.0, "geometry": poly, "source": "abwaerme+mastr+osm", "source_count": 3,
             "is_industrial": True, "merged_at": "2026-09-07"},
        {"id": "osm_node/2", "name": "Kiosk Ali", "business_type": "shop", "state": "Bremen",
             "district": "Bremen", "district_ags": "04011", "geometry": Point(LON + 0.01, LAT),
             "source": "osm", "merged_at": "2026-09-07"},
        {"id": "mastr_B7_L1", "name": "Windpark Nord GmbH & Co. KG", "business_type": "power",
             "state": "Niedersachsen", "district": "Cuxhaven", "district_ags": "03352",
             "geometry": Point(LON + 0.2, LAT + 0.2), "source": "mastr", "is_industrial": True,
             "merged_at": "2026-09-07"},
    ], {"member_ids": ["osm_way/1|abw_9|mastr_A1_L1|mastr_A1_L2", None, None],
        "abw_heat_mwh_a": [1234.0, None, None], "mastr_techs": ["solar+combustion", None, "wind"],
        "website_kind": ["own", None, None],
        "website_host": ["weser-stahl.de", None, None], "website_listing": [None] * 3,
        "website_source": ["extract", None, None]})
    merged.to_parquet(paths.merged_parquet(tmp_path, "bremen", "4326"))
    lu = gpd.GeoDataFrame({"landuse": ["industrial"]}, geometry=[poly], crs="EPSG:4326")
    lu.to_parquet(paths.landuse_parquet(tmp_path, "bremen"))
    return tmp_path


def test_build_layout_and_tiers(data_root, capsys):
    out = build.build(data_root, "bremen", tiles=False)
    assert out == data_root / "serve" / "bremen" / "2026-09-07"
    flat = pd.read_parquet(out / "companies_flat.parquet")
    assert "email" not in flat.columns and "geometry" not in flat.columns
    assert "abw_heat_mwh_a" not in flat.columns            # internal debug column, never public
    assert list(flat.columns[:2]) == ["id", "name"] and "website_kind" in flat.columns
    assert list(flat["state"]) == ["Bremen", "Bremen", "Niedersachsen"]   # sorted by state
    full = pd.read_parquet(out / "companies_full.parquet")
    assert {"osm", "abwaerme", "mastr"} <= set(full.columns) and "email" not in full.columns
    row = full.set_index("id").loc["osm_way/1"]
    assert len(row["mastr"]) == 2 and {r["id"] for r in row["mastr"]} == {"mastr_A1_L1", "mastr_A1_L2"}
    assert row["abwaerme"][0]["abw_heat_mwh_a"] == 1234.0
    pots = row["abwaerme"][0]["potentials"]                 # every workbook field, ordered by report id
    assert [p["abwaermepotential"] for p in pots] == ["Kühlturm", "Abgas"]
    assert pots[1]["leistungsprofil_januar_kw"] == 200.0 and "email" not in pots[0]
    assert pots[0]["verfuegbarkeit"] == "5 Tage 24 Std"
    assert row["osm"][0]["osm_tags"] == '{"industrial":"factory"}'
    assert "email" not in row["osm"][0] and "confidence_score" not in row["osm"][0]
    single = full.set_index("id").loc["osm_node/2"]
    assert single["osm"][0]["name"] == "Kiosk Ali" and single["mastr"] is None
    # nested columns are real parquet LIST<STRUCT>, not JSON strings
    sch = pq.read_schema(out / "companies_full.parquet")
    assert str(sch.field("mastr").type).startswith("list<")
    sites = gpd.read_parquet(out / "sites.parquet")
    assert list(sites["id"]) == ["osm_way/1"] and sites.geometry.iloc[0].geom_type == "Polygon"
    ex = sorted(p.relative_to(out).as_posix() for p in (out / "extracts").rglob("*.parquet"))
    assert "extracts/bremen/industrial_flat.parquet" in ex   # business_type = industrial
    assert "extracts/bremen/is_industrial_flat.parquet" in ex   # the intrinsic flag (spec §6.5)
    assert "extracts/niedersachsen/power_full.parquet" in ex
    assert "extracts/bremen/all_full.parquet" in ex          # scope-wide dir uses the scope name
    ind = pd.read_parquet(out / "extracts" / "bremen" / "is_industrial_flat.parquet")
    assert set(ind["id"]) == {"osm_way/1", "mastr_B7_L1"}   # scope 'bremen' dir = whole scope
    bt = pd.read_parquet(out / "extracts" / "bremen" / "industrial_flat.parquet")
    assert set(bt["id"]) == {"osm_way/1"}
    search = pd.read_parquet(out / "search.parquet")
    assert search.set_index("id").loc["osm_way/1", "name_key"] == "weser stahl"
    m = json.loads((out / "manifest.json").read_text())
    assert m["companies"] == 3 and m["nested_sources"] == {"osm": 2, "abwaerme": 1, "abwaerme_potentials": 2, "mastr": 2}
    assert "email" in m["excluded"] and "companies_flat.parquet" in m["files"]
    assert m["files"]["companies_flat.parquet"]["sha256"]
    assert "ODbL" in m["licence"]["note"] or "derivative" in m["licence"]["note"]
    # idempotent: second call returns without rebuilding
    assert build.build(data_root, "bremen", tiles=False) == out
    assert "exists" in capsys.readouterr().out


def test_public_columns_order():
    cols = build.public_columns(["name", "id", "email", "website", "website_kind", "member_ids", "foo"])
    assert cols == ["id", "name", "website", "website_kind", "member_ids"]


def test_ui_model_and_tile_props(data_root):
    out = build.build(data_root, "bremen", tiles=False)
    ui = json.loads((out / "ui.json").read_text())
    assert ui["companies"] == 3 and ui["sector_column"] == "business_type"
    assert ui["datasets"]["osm"]["cats"] == {"industrial": 1, "shop": 1} and ui["datasets"]["osm"]["nSurf"] == 1
    assert ui["datasets"]["mastr"]["match"] == {"matched": 1, "only": 1}
    assert ui["datasets"]["abwaerme"]["groups"] == {"1–10 GWh/a": 1}
    assert [s["name"] for s in ui["states"]] == ["Bremen", "Niedersachsen"]
    assert ui["districts"][0]["ags"] == "04011" and len(ui["districts"][0]["bbox"]) == 4
    assert set(ui["palette"]) >= {"industrial", "shop", "power"}
    from geoextract.serve.build import _point_props
    p = _point_props({"id": "x", "name": "N", "business_type": "power", "source": "mastr+osm",
                      "source_count": 2, "is_industrial": True, "state": "Bremen", "district_ags": "04011",
                      "website": "https://x.de", "ied_activity": "6.6(a)", "abw_heat_mwh_a": 50000.0,
                      "mastr_techs": "solar+storage", "grounds_area_source": "own_polygon"})
    assert p == {"id": "x", "name": "N", "bt": "power", "src": "mastr+osm", "sc": 2, "ind": 1, "st": "Bremen",
                 "ags": "04011", "web": 1, "ied": "livestock", "abw": "10–100 GWh/a", "mt": "+solar+storage+", "poly": 1}
