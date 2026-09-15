"""Item 6 stage 3d — query set and host features (no network)."""
import pandas as pd

from geoextract.web import search


def test_queries():
    q = search.queries("Schwarzwaldmilch", "Schwarzwaldmilch GmbH", "Freiburg", "Breisgau", "Haslacher Str. 4")
    assert q[0] == "Schwarzwaldmilch GmbH Freiburg" and q[1] == "Schwarzwaldmilch GmbH Freiburg impressum"
    assert any("Haslacher" in x for x in q) and len(q) <= 4
    assert search.queries(None, None, "Bremen", None, None) == []
    assert search.queries("Kiosk Ali", None, None, "Bremen", None)[0] == "Kiosk Ali Bremen"


def test_host_features_rank_and_flags():
    results = {
        "Schwarzwaldmilch GmbH Freiburg": [
            {"url": "https://www.schwarzwaldmilch.de/", "title": "Schwarzwaldmilch – Molkerei Freiburg", "content": "Haslacher Str. 4, 79115 Freiburg", "engines": ["brave", "duckduckgo"], "rank": 1},
            {"url": "https://de.wikipedia.org/wiki/Schwarzwaldmilch", "title": "Schwarzwaldmilch – Wikipedia", "content": "", "engines": ["brave"], "rank": 2},
            {"url": "https://www.gelbeseiten.de/x", "title": "Schwarzwaldmilch GmbH Freiburg", "content": "", "engines": ["brave"], "rank": 3},
        ],
        "Schwarzwaldmilch GmbH Freiburg impressum": [
            {"url": "https://www.schwarzwaldmilch.de/impressum", "title": "Impressum | Schwarzwaldmilch", "content": "Schwarzwaldmilch GmbH", "engines": ["duckduckgo"], "rank": 1},
        ],
    }
    f = search.host_features("Schwarzwaldmilch GmbH", "Freiburg", "79115", "Haslacher Str. 4", "GmbH", results, {"gelbeseiten.de": 40})
    f = f.set_index("host")
    own = f.loc["schwarzwaldmilch.de"]
    assert own["n_queries"] == 2 and own["n_hits"] == 2 and own["best_rank"] == 1 and own["label_is_key"] == 1
    assert own["city_in"] == 1 and own["plz_in"] == 1 and own["street_in"] == 1 and own["tld_de"] == 1
    assert own["sim_host"] == 1.0 and own["n_engines"] == 2
    assert f.loc["gelbeseiten.de", "listed"] == 1 and f.loc["gelbeseiten.de", "host_freq"] == 40
    assert f.loc["wikipedia.org", "listed"] == 1
    assert list(f.columns) == search.FEATURES
    assert search.host_features("X", None, None, None, None, {}, {}).empty


def test_skip_reason():
    places = {"farge", "bremen nord", "groepelingen", "bremen"}
    assert search.skip_reason("Farge", places) == "place name"
    assert search.skip_reason("Bremen/Nord", places) in ("place name", "generic name")
    assert search.skip_reason("TX Logistik", places) == "generic name"
    assert search.skip_reason("Umspannwerk Bremen-Nord", places) == "generic name"
    assert search.skip_reason(None, places) == "no name"
    assert search.skip_reason("Nabertherm", places) is None
    assert search.skip_reason("Schwarzwaldmilch GmbH", places) is None
