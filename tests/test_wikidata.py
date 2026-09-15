"""Item 6c — Wikidata identifier collection, SPARQL parsing (mocked), write-back."""
import pandas as pd

from geoextract.sources import wikidata


def test_collect_ids_and_apply():
    gdf = pd.DataFrame({
        "name": ["Karlsberg Brauerei", "Pumpwerk Horst", "Lidl", "Kiosk"],
        "osm_tags": ['{"wikidata": "Q520174", "brand:wikidata": "Q999"}', '{"operator:wikidata": "Q1"}', None, None],
        "ovt_brand_wikidata": [None, None, "Q151954", None],
        "website": [None, None, None, None], "website_host": [None] * 4, "website_kind": [None] * 4, "website_source": [None] * 4,
    })
    ids = wikidata.collect_ids(gdf)
    assert ids.loc[0, "wd_id"] == "Q520174" and ids.loc[0, "wd_brand_id"] == "Q999"
    assert ids.loc[1, "wd_operator_id"] == "Q1" and ids.loc[2, "wd_brand_id"] == "Q151954"
    assert pd.isna(ids.loc[3, "wd_id"])
    items = pd.DataFrame({
        "qid": ["Q520174", "Q1", "Q151954"], "label": ["Karlsberg Brauerei", "Stadtwerke Horst", "Lidl"],
        "website": ["https://www.karlsberg.de/", None, "https://www.lidl.de"],
        "industry": ["Brauwesen", "Wasserversorgung", "Einzelhandel"], "lei": ["5299001234", None, None],
        "legal_form": ["GmbH", "AöR", None], "parent": [None, None, "Schwarz Gruppe"], "parent_id": [None, None, "Q3"],
        "inception": ["1878-01-01", None, None], "dissolved": [None, None, None], "coord": [None] * 3, "hq": [None] * 3,
        "opencorporates": [None] * 3, "fetched_at": ["2026-09-09"] * 3,
    }).astype("string")
    out = wikidata.apply_wikidata(gdf, ids, items)
    assert out.loc[0, "website"] == "https://www.karlsberg.de" and out.loc[0, "website_source"] == "wikidata"
    assert out.loc[0, "wd_lei"] == "5299001234" and out.loc[0, "wd_industry"] == "Brauwesen"
    assert out.loc[1, "wd_industry"] == "Wasserversorgung" and pd.isna(out.loc[1, "website"])   # operator's data, no website
    assert pd.isna(out.loc[2, "wd_website"])       # brand id alone does not fill the branch's website
    assert pd.isna(out.loc[3, "wd_id"])


def test_fetch_items_parses_sparql(tmp_path, monkeypatch):
    class Resp:
        status_code = 200
        content = b"x" * 1200
        def raise_for_status(self): pass
        def json(self):
            return {"results": {"bindings": [{
                "item": {"value": "http://www.wikidata.org/entity/Q520174"},
                "label": {"value": "Karlsberg Brauerei"}, "website": {"value": "https://www.karlsberg.de/"},
                "industry": {"value": "Brauwesen"}, "lei": {"value": "5299001234"},
                "parent_id": {"value": "http://www.wikidata.org/entity/Q77"}, "parent": {"value": "Karlsberg Holding"},
                "inception": {"value": "1878-01-01T00:00:00Z"}}]}}
    class Sess:
        def __init__(self):
            self.headers = {}
        def get(self, url, params=None, timeout=None):
            assert "wd:Q520174" in params["query"] and "wd:Q1" in params["query"]
            return Resp()
    monkeypatch.setattr(wikidata.time, "sleep", lambda s: None)
    items = wikidata.fetch_items(["Q520174", "Q1"], tmp_path, session=Sess())
    got = items.set_index("qid")
    assert got.loc["Q520174", "website"] == "https://www.karlsberg.de/" and got.loc["Q520174", "parent_id"] == "Q77"
    assert got.loc["Q520174", "inception"] == "1878-01-01"
    assert "Q1" in got.index and pd.isna(got.loc["Q1", "website"])      # miss remembered
    # second call: nothing to fetch, cache returned
    again = wikidata.fetch_items(["Q520174", "Q1"], tmp_path, session=None)
    assert len(again) == 2


