"""Item 6 stage 1 — imprint link discovery, field extraction, verdict (no network)."""
from geoextract.web import impressum

HOME = """<html><body><nav><a href="/">Start</a><a href="/produkte">Produkte</a>
<a href="/kontakt/">Kontakt</a><a href="/de/impressum/">Impressum</a><a href="https://facebook.com/x">FB</a>
<a href="/datenschutz">Datenschutz</a></nav></body></html>"""

IMPRINT = """<html><body><h1>Impressum</h1><p>Angaben gemäß § 5 TMG</p>
<p>Schwarzwaldmilch GmbH Freiburg<br>Haslacher Str. 4<br>79115 Freiburg</p>
<p>Vertreten durch: Andreas Schneider</p>
<p>Registergericht: Amtsgericht Freiburg<br>Registernummer: HRB 5678</p>
<p>Umsatzsteuer-ID: DE 123 456 789</p><p>Telefon: 0761 1234</p></body></html>"""


def test_imprint_links_rank_and_domain():
    links = impressum.imprint_links(HOME, "https://www.example.de/")
    assert links[0] == "https://www.example.de/de/impressum/"
    assert "https://www.example.de/kontakt/" in links
    assert not any("facebook" in u for u in links)


def test_extract_fields():
    text = impressum.page_text(IMPRINT)
    f = impressum.extract(text, courts={"Freiburg", "Bremen"})
    assert f["imp_legal_name"] == "Schwarzwaldmilch GmbH Freiburg"
    assert f["imp_legal_form"] == "GmbH"
    assert f["imp_register_type"] == "HRB" and f["imp_register_no"] == "5678" and f["imp_registration"] == "HRB 5678"
    assert f["imp_court"] == "Freiburg"
    assert f["imp_vat_id"] == "DE123456789"
    assert f["imp_plz"] == "79115" and f["imp_city"] == "Freiburg"
    assert f["imp_street"] == "Haslacher Str. 4"
    assert f["imp_score"] == 1.0


def test_extract_suffix_and_label_forms():
    text = "Impressum\nMüller Maschinenbau GmbH & Co. KG\nAm Hafen 12\n28195 Bremen\nAmtsgericht Bremen HRA 21901 HB\nUSt-IdNr.: DE811223344"
    f = impressum.extract(text)
    assert f["imp_registration"] == "HRA 21901 HB" and f["imp_court"] == "Bremen"
    assert f["imp_legal_form"] == "GmbH & Co. KG" and f["imp_street"] == "Am Hafen 12"
    assert impressum.extract("nothing here")["imp_score"] == 0.0


def test_verdict():
    rec = {"status": "ok", "tokens": ["schwarzwaldmilch", "freiburg", "haslacher"], "postcodes": ["79115"], "imp_plz": "79115"}
    assert impressum.verdict(rec, "Schwarzwaldmilch GmbH", "79115") == "name+plz"
    assert impressum.verdict(rec, "Schwarzwaldmilch GmbH", "28195") == "name"
    assert impressum.verdict(rec, "Bäckerei Meier", "79115") == "plz"
    assert impressum.verdict(rec, "Bäckerei Meier", "28195") == "mismatch"
    assert impressum.verdict({"status": "dead"}, "X", None) == "dead"


def test_redirected_host_verdict_and_writeback():
    import pandas as pd
    rec = {"status": "ok_redirected", "final_host": "huth-zaun.de", "tokens": ["huth", "zaun", "torsysteme", "bremerhaven"],
           "postcodes": ["27572"], "imp_plz": "27572", "imp_legal_name": "Huth Zaun + Torsysteme GmbH"}
    assert impressum.verdict(rec, "Huth Zaun + Torsysteme GmbH", "27572") == "name+plz"
    assert impressum.verdict(rec, "Bäckerei Meier", "27572") == "redirect_offdomain"    # landing imprint names someone else
    gdf = pd.DataFrame({"name": ["Huth Zaun + Torsysteme GmbH"], "website": ["https://hzt.de"], "website_host": ["hzt.de"],
                        "legal_form": [None]})
    impressum.ensure_columns(gdf)
    impressum.write_row(gdf, 0, rec, "name+plz", "2026-09-16")
    assert gdf.at[0, "website_host"] == "huth-zaun.de" and gdf.at[0, "legal_name"] == "Huth Zaun + Torsysteme GmbH"


def test_keep_replaced_records_the_old_unverified_site():
    import pandas as pd
    gdf = pd.DataFrame({"name": ["Muster GmbH"], "website": ["https://alt-muster.de"], "website_host": ["alt-muster.de"],
                        "website_source": ["extract"], "legal_form": [None]})
    impressum.ensure_columns(gdf)
    impressum.keep_replaced(gdf, 0)
    assert gdf.at[0, "website_replaced"] == "https://alt-muster.de"
    assert gdf.at[0, "website_replaced_source"] == "extract"
    empty = pd.DataFrame({"name": ["X"], "website": [None], "website_source": [None], "legal_form": [None]})
    impressum.ensure_columns(empty)
    impressum.keep_replaced(empty, 0)
    assert pd.isna(empty.at[0, "website_replaced"])        # nothing to keep
