"""Item 6 stage 0d — URL normalisation, host classification, propagation, idempotence."""
import pandas as pd

from geoextract import config
from geoextract.web import hygiene


def test_normalize_url_variants():
    n = hygiene.normalize_url
    assert n("www.stadtwerke-barsinghausen.de") == "https://www.stadtwerke-barsinghausen.de"
    assert n("HTTP://Example.DE/Pfad/?utm_source=x&id=3#top") == "http://example.de/Pfad?id=3"
    assert n("https://a.de/; https://b.de/") == "https://a.de"
    assert n("  ") is None and n(None) is None and n("not a url") is None
    assert n("https://example.de:8443/x") == "https://example.de:8443/x"
    assert hygiene.host_of("https://www.Edeka.de/markt/1") == "edeka.de"
    assert hygiene.host_of(None) is None


def _frame():
    rows = [
        ("Metallbau Braun GmbH", "28195", "https://www.metallbau-braun.de/"),
        ("Metallbau Braun GmbH", "28195", None),                  # gets the URL propagated
        ("Metallbau Braun GmbH", "28217", None),                  # other postcode: no
        ("Kiosk Ali", "28195", "https://www.gelbeseiten.de/gsbiz/1"),
        ("Kiosk Ali", "28203", "paketshop.myhermes.de/shop/22"),
        ("Café Sonne", "28195", "https://www.instagram.com/cafesonne"),
        ("Bremer Kaffee", "28195", "https://maps.google.com/?cid=1"),
        ("Bremer Kaffee", "28199", "   "),
        ("Nähstube Lotte", "28199", "https://sites.google.com/view/naehstube-lotte"),
    ]
    # a chain host shared by 25 rows whose names carry the brand, and a learned directory
    # host with 120 rows of distinct names without the brand label
    for i in range(25):
        rows.append((f"EDEKA Markt {i}", f"281{i:02d}", "https://www.edeka.de/markt"))
    for i in range(120):
        rows.append((f"Firma {i} GmbH", f"280{i % 100:02d}", f"https://www.branchenliste-xy.de/e/{i}"))
    # 120 distinct small firms on Google Sites: exception host, must stay "own"
    for i in range(120):
        rows.append((f"Laden {i}", f"282{i % 100:02d}", f"https://sites.google.com/view/laden-{i}"))
    # seeded chain whose names lack the brand: agent network
    for i in range(30):
        rows.append((f"Deutsche Vermögensberatung Berater {i}", f"283{i % 100:02d}", f"https://www.dvag.de/berater-{i}"))
    return pd.DataFrame(rows, columns=["name", "address_postcode", "website"])


def test_classification_seeded_and_learned():
    out = hygiene.apply_hygiene(_frame())
    k = out.set_index(out["website_listing"].fillna(out["website"]).fillna(out["name"]))
    assert out.loc[0, "website_kind"] == "own" and out.loc[0, "website"] == "https://www.metallbau-braun.de"
    assert out.loc[3, "website_kind"] == "directory" and pd.isna(out.loc[3, "website"])
    assert out.loc[3, "website_listing"] == "https://www.gelbeseiten.de/gsbiz/1"
    assert out.loc[4, "website_kind"] == "parcel"
    assert out.loc[5, "website_kind"] == "social"
    assert out.loc[6, "website_kind"] == "directory"          # google.com parent match
    assert pd.isna(out.loc[7, "website_kind"]) and pd.isna(out.loc[7, "website"])
    assert out.loc[8, "website_kind"] == "own"                 # sites.google.com exception
    edeka = out[out["website_host"] == "edeka.de"]
    assert (edeka["website_kind"] == "chain").all() and edeka["website"].notna().all()
    learned = out[out["website_host"] == "branchenliste-xy.de"]
    assert (learned["website_kind"] == "directory").all() and learned["website"].isna().all()
    assert "branchenliste-xy.de" not in config.WEBSITE_DIRECTORY_HOSTS
    gsites = out[out["website_host"] == "sites.google.com"]
    assert (gsites["website_kind"] == "own").all()
    dvag = out[out["website_host"] == "dvag.de"]
    assert (dvag["website_kind"] == "chain").all()
    del k


def test_propagation_and_sources():
    out = hygiene.apply_hygiene(_frame())
    assert out.loc[1, "website"] == "https://www.metallbau-braun.de"
    assert out.loc[1, "website_source"] == "propagated"
    assert out.loc[0, "website_source"] == "extract"
    assert pd.isna(out.loc[2, "website"])                     # different postcode
    off = hygiene.apply_hygiene(_frame(), propagate=False)
    assert pd.isna(off.loc[1, "website"])


def test_idempotent_rerun():
    once = hygiene.apply_hygiene(_frame())
    twice = hygiene.apply_hygiene(once)
    cols = ["website", "website_kind", "website_host", "website_listing", "website_source"]
    pd.testing.assert_frame_equal(once[cols], twice[cols])
    rep = hygiene.hygiene_report(twice)
    assert rep["website_kind"]["directory"] == 122 and rep["website_propagated"] == 1
