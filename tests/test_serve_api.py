"""Item 7 S3 — FastAPI + DuckDB API on the synthetic serve build."""
import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_serve_build import data_root  # noqa: F401  (fixture reuse)

from geoextract.serve import api, build


@pytest.fixture
def client(data_root):  # noqa: F811
    out = build.build(data_root, "bremen", tiles=False)
    return TestClient(api.create_app(out))


def test_manifest_summary_extracts(client):
    assert client.get("/v1/manifest").json()["companies"] == 3
    s = client.get("/v1/summary").json()
    assert s["companies"] == 3 and s["is_industrial"] == 2 and s["by_state"] == {"Bremen": 2, "Niedersachsen": 1}
    ex = client.get("/v1/extracts").json()
    assert any(e["area"] == "bremen" and e["sector"] == "is_industrial" and e["tier"] == "flat" for e in ex)
    assert ex[0]["url"].startswith("http://testserver/data/extracts/")
    assert "X-Query-Ms" in client.get("/v1/ui").headers


def test_filters_and_formats(client):
    r = client.get("/v1/companies", params={"state": "Bremen"})
    ids = {x["id"] for x in r.json()}
    assert r.status_code == 200 and ids == {"osm_way/1", "osm_node/2"}
    assert "email" not in r.json()[0]
    assert client.get("/v1/companies", params={"sector": "power"}).json()[0]["id"] == "mastr_B7_L1"
    assert client.get("/v1/companies", params={"industrial": "true", "count_only": "true"}).json() == {"count": 2}
    assert client.get("/v1/companies", params={"district": "04011", "count_only": "true"}).json() == {"count": 2}
    assert client.get("/v1/companies", params={"district": "Cuxhaven", "count_only": "true"}).json() == {"count": 1}
    r = client.get("/v1/companies", params={"bbox": "8.79,53.07,8.805,53.09"})
    assert {x["id"] for x in r.json()} == {"osm_way/1"}
    # full tier json carries the nested sources
    r = client.get("/v1/companies", params={"state": "Bremen", "tier": "full"}).json()
    rec = [x for x in r if x["id"] == "osm_way/1"][0]
    assert len(rec["mastr"]) == 2 and rec["abwaerme"][0]["abw_heat_mwh_a"] == 1234.0
    # geojson
    g = client.get("/v1/companies", params={"format": "geojson", "state": "Bremen"}).json()
    assert g["type"] == "FeatureCollection" and len(g["features"]) == 2
    assert g["features"][0]["geometry"]["type"] == "Point" and "name" in g["features"][0]["properties"]
    # csv (flat) and csv (full → nested as JSON strings)
    r = client.get("/v1/companies", params={"format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    df = pd.read_csv(io.BytesIO(r.content))
    assert len(df) == 3 and "email" not in df.columns
    r = client.get("/v1/companies", params={"format": "csv", "tier": "full", "state": "Bremen"})
    df = pd.read_csv(io.BytesIO(r.content))
    assert json.loads(df.set_index("id").loc["osm_way/1", "mastr"])[0]["id"] == "mastr_A1_L1"
    # parquet
    r = client.get("/v1/companies", params={"format": "parquet", "tier": "full"})
    assert r.headers["content-type"] == "application/vnd.apache.parquet"
    pq = pd.read_parquet(io.BytesIO(r.content))
    assert len(pq) == 3 and "mastr" in pq.columns
    assert "attachment" in r.headers["content-disposition"]
    # limit / offset / bad input
    assert len(client.get("/v1/companies", params={"limit": 1}).json()) == 1
    assert len(client.get("/v1/companies", params={"limit": 1, "offset": 5}).json()) == 0
    assert client.get("/v1/companies", params={"format": "xml"}).status_code == 400
    assert client.get("/v1/companies", params={"bbox": "1,2,3"}).status_code == 400


def test_polygon_query(client):
    poly = {"type": "Polygon", "coordinates": [[[8.79, 53.07], [8.805, 53.07], [8.805, 53.09], [8.79, 53.09], [8.79, 53.07]]]}
    r = client.post("/v1/companies/query", json=poly)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == ["osm_way/1"]
    feat = {"type": "Feature", "properties": {}, "geometry": poly}
    assert client.post("/v1/companies/query?count_only=true", json=feat).json() == {"count": 1}
    assert client.post("/v1/companies/query?count_only=true&sector=shop", json=poly).json() == {"count": 0}
    r = client.post("/v1/companies/query", params={"format": "parquet", "tier": "full"}, json=poly)
    assert len(pd.read_parquet(io.BytesIO(r.content))) == 1
    assert client.post("/v1/companies/query", json={"type": "Point", "coordinates": [8.8, 53.08]}).status_code == 400


def test_company_search_static_range(client):
    rec = client.get("/v1/companies/osm_way/1").json()
    assert rec["name"] == "Weser Stahl GmbH" and len(rec["mastr"]) == 2
    assert client.get("/v1/companies/nope").status_code == 404
    hits = client.get("/v1/search", params={"q": "weser"}).json()
    assert [h["id"] for h in hits] == ["osm_way/1"]
    assert client.get("/v1/search", params={"q": "w"}).json() == []
    assert b"maplibre-gl" in client.get("/").content
    assert client.get("/static/app.js").status_code == 200
    full = client.get("/data/companies_flat.parquet")
    part = client.get("/data/companies_flat.parquet", headers={"Range": "bytes=0-3"})
    assert full.status_code == 200 and part.status_code == 206 and part.content == full.content[:4]
    assert part.headers["content-range"].startswith("bytes 0-3/")
    assert client.get("/docs").status_code == 200


def test_display_filter(client):
    # toggle model: only the MaStR dataset, only its "solar" technology → the 2-unit cluster
    disp = json.dumps({"osm": {"on": False, "cats": []},
                       "reg": {"mastr": {"on": True, "matched": True, "only": True, "groups": ["solar"]}}})
    r = client.get("/v1/companies", params={"display": disp})
    assert [x["id"] for x in r.json()] == ["osm_way/1"]
    # OSM shop only
    disp = json.dumps({"osm": {"on": True, "cats": ["shop"]}, "reg": {}})
    assert [x["id"] for x in client.get("/v1/companies", params={"display": disp}).json()] == ["osm_node/2"]
    # mastr dataset-only rows (source_count 1) without group restriction
    disp = json.dumps({"osm": {"on": False, "cats": []},
                       "reg": {"mastr": {"on": True, "matched": False, "only": True, "groups": None}}})
    assert [x["id"] for x in client.get("/v1/companies", params={"display": disp}).json()] == ["mastr_B7_L1"]
    # everything off → nothing
    disp = json.dumps({"osm": {"on": False, "cats": []}, "reg": {}})
    assert client.get("/v1/companies", params={"display": disp, "count_only": "true"}).json() == {"count": 0}
    # display inside a polygon body + search with display
    poly = {"type": "Polygon", "coordinates": [[[8.79, 53.07], [8.83, 53.07], [8.83, 53.09], [8.79, 53.09], [8.79, 53.07]]]}
    body = {"type": "Feature", "geometry": poly, "display": {"osm": {"on": True, "cats": ["shop"]}, "reg": {}}}
    assert client.post("/v1/companies/query?count_only=true", json=body).json() == {"count": 1}
    disp = json.dumps({"osm": {"on": True, "cats": ["industrial"]}, "reg": {}})
    assert [h["id"] for h in client.get("/v1/search", params={"q": "we", "display": disp}).json()] == ["osm_way/1"]
    assert client.get("/v1/companies", params={"display": "nope"}).status_code == 400


def test_fields_q_and_search_columns(client):
    r = client.get("/v1/companies", params={"fields": "id,name"}).json()
    assert set(r[0]) == {"id", "name"} and len(r) == 3
    assert client.get("/v1/companies", params={"fields": "id,nope"}).status_code == 400
    assert [x["id"] for x in client.get("/v1/companies", params={"q": "stahl", "fields": "id"}).json()] == ["osm_way/1"]
    csv = client.get("/v1/companies", params={"q": "kiosk", "format": "csv", "fields": "id,name"}).text
    assert csv.splitlines()[0] == "id,name" and "Kiosk Ali" in csv
    hit = client.get("/v1/search", params={"q": "kiosk"}).json()[0]
    assert hit["business_type"] == "shop" and "source" in hit and "grounds_area_m2" in hit
