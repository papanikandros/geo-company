"""Item 6 stage 2 (probabilistic) — register join on synthetic map rows and register tables."""
import json

import pandas as pd

from geoextract.register import match


def _names(rows):
    df = pd.DataFrame(rows, columns=["hr_id", "hr_source", "name", "postcode", "city", "street_key", "is_current"])
    from geoextract.resolve import normalize_name
    df["name_key_full"] = df["name"].map(lambda n: normalize_name(n, strip_noise=True))
    df["name_key_light"] = df["name"].map(lambda n: normalize_name(n, strip_noise=False))
    return df


def _companies(rows):
    cols = ["hr_source", "hr_id", "name", "status", "dissolved", "snapshot_date", "objective",
            "capital_amount", "legal_form", "hr_registration", "court"]
    return pd.DataFrame(rows, columns=cols)


def test_match_types_and_fills():
    map_df = pd.DataFrame({
        "id": ["m1", "m2", "m3", "m4", "m5", "m6"],
        "name": ["Weser Stahlbau GmbH", "Bäckerei Krause", "Hansa Druck", "Kiosk Ali Baba", None, "Nordmilch eG"],
        "address_postcode": ["28237", "28195", None, "28195", "28195", "28215"],
        "address_city": ["Bremen", "Bremen", "Bremen", "Bremen", "Bremen", None],
        "district": ["Bremen"] * 6,
        "address_street": ["Auf den Delben", "Marktstraße", None, "Am Markt", None, None],
        "address_housenumber": ["35", "2", None, "1", None, None],
        "legal_form": [None, None, None, None, None, "eG"],
        "hr_registration": ["1234", None, None, None, None, None],
        "hr_court": [None] * 6,
        "confidence_score": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        "is_industrial": [True, False, False, False, False, True],
    })
    names = _names([
        ("H1", "hr2022", "Weser Stahlbau GmbH", "28237", "Bremen", "auf den delben 35", True),
        ("H1", "hr2022", "Weser Stahl GmbH", "28237", "Bremen", "auf den delben 35", False),
        ("H2", "hr2022", "Bäckerei Krause e.K.", "28195", "Bremen", "marktstrasse 2", True),
        ("H3", "hr2019", "Hansa Druck GmbH", None, "Bremen", "", True),          # no postcode → city block
        ("H4", "hr2022", "Kiosk Alibaba GmbH", "28195", "Bremen", "bahnhofstrasse 9", True),
        ("H5", "hr2022", "Kiosk Ali Baba UG", "28195", "Bremen", "am markt 1", True),
        ("G1", "gleif", "Nordmilch eG", None, "Bremen", "", True),
    ])
    companies = _companies([
        ("hr2022", "H1", "Weser Stahlbau GmbH", "active", None, "2022-08-01", "Stahlbau", 250000.0, "GmbH", "HRB 1234 HB", "Bremen"),
        ("hr2022", "H2", "Bäckerei Krause e.K.", "dissolved", "2019-11-04", "2022-08-01", None, None, "e.K.", "HRA 9", "Bremen"),
        ("hr2019", "H3", "Hansa Druck GmbH", "active", None, "2019-01-31", None, None, "GmbH", "HRB 77", "Bremen"),
        ("hr2022", "H4", "Kiosk Alibaba GmbH", "active", None, "2022-08-01", None, None, "GmbH", "HRB 5", "Bremen"),
        ("hr2022", "H5", "Kiosk Ali Baba UG", "active", None, "2022-08-01", None, None, "UG (haftungsbeschränkt)", "HRB 6", "Bremen"),
        ("gleif", "G1", "Nordmilch eG", "active", None, "2026-09-07", None, None, "eG", "GnR 12", None),
    ])
    res = match.match_frame(map_df, names, companies)
    out = match.apply_match(map_df, res).set_index("id")
    assert out.loc["m1", "register_match"] == "name_plz_street" and out.loc["m1", "hr_id"] == "H1"
    assert out.loc["m1", "hr_registration"] == "HRB 1234 HB"       # bare MaStR number → register reference
    assert out.loc["m1", "hr_court"] == "Bremen" and out.loc["m1", "legal_form"] == "GmbH"
    assert out.loc["m1", "hr_objective"] == "Stahlbau" and out.loc["m1", "hr_capital"] == 250000.0
    assert out.loc["m1", "confidence_score"] == 0.6                 # active match +0.10
    assert out.loc["m1", "hr_matched_name"] == "Weser Stahlbau GmbH"
    assert out.loc["m2", "register_match"] == "name_plz_street" and out.loc["m2", "hr_status"] == "dissolved"
    assert out.loc["m2", "hr_dissolved_date"] == "2019-11-04" and out.loc["m2", "confidence_score"] == 0.4
    assert out.loc["m3", "register_match"] == "name_city" and out.loc["m3", "hr_source"] == "hr2019"
    assert out.loc["m4", "register_match"] == "name_plz_street" and out.loc["m4", "hr_id"] == "H5"   # street decides
    assert out.loc["m5", "register_match"] == "n/a"
    assert out.loc["m6", "register_match"] == "name_city" and out.loc["m6", "hr_source"] == "gleif"
    assert out.loc["m6", "hr_snapshot_date"] == "2026-09-07"


def test_ambiguous_and_substance_rule():
    map_df = pd.DataFrame({
        "id": ["a", "b"], "name": ["Müller Bau", "NK Beauty"],
        "address_postcode": ["28195", "28195"], "address_city": ["Bremen", "Bremen"], "district": ["Bremen"] * 2,
        "address_street": [None, None], "address_housenumber": [None, None],
        "legal_form": [None, None], "hr_registration": [None, None], "hr_court": [None, None],
        "confidence_score": [0.3, 0.3], "is_industrial": [False, False],
    })
    names = _names([
        ("X1", "hr2022", "Müller Bau GmbH", "28195", "Bremen", "", True),
        ("X2", "hr2022", "Müller Bau UG", "28195", "Bremen", "", True),
        ("X3", "hr2022", "Beauty Line GmbH", "28195", "Bremen", "", True),
    ])
    companies = _companies([
        ("hr2022", "X1", "Müller Bau GmbH", "active", None, "2022-08-01", None, None, "GmbH", "HRB 1", "Bremen"),
        ("hr2022", "X2", "Müller Bau UG", "active", None, "2022-08-01", None, None, "UG", "HRB 2", "Bremen"),
        ("hr2022", "X3", "Beauty Line GmbH", "active", None, "2022-08-01", None, None, "GmbH", "HRB 3", "Bremen"),
    ])
    res = match.match_frame(map_df, names, companies)
    out = match.apply_match(map_df, res).set_index("id")
    assert out.loc["a", "register_match"] == "ambiguous"
    cands = json.loads(out.loc["a", "hr_candidates"])
    assert {c["hr_id"] for c in cands} == {"X1", "X2"}
    assert out.loc["b", "register_match"] == "none"                   # ratio 80–89, shared token "beauty" is common


def test_same_company_in_two_sources_is_not_ambiguous():
    map_df = pd.DataFrame({
        "id": ["a"], "name": ["Rehda-Carosse GmbH"], "address_postcode": ["28307"], "address_city": ["Bremen"],
        "district": ["Bremen"], "address_street": [None], "address_housenumber": [None],
        "legal_form": [None], "hr_registration": [None], "hr_court": [None], "confidence_score": [0.3], "is_industrial": [False],
    })
    names = _names([
        ("H9", "hr2022", "Rehda-Carosse GmbH", "28307", "Bremen", "", True),
        ("K9", "hr2019", "Rehda-Carosse GmbH", "28307", "Bremen", "", True),
    ])
    companies = _companies([
        ("hr2022", "H9", "Rehda-Carosse GmbH", "active", None, "2022-08-01", None, None, "GmbH", "HRB 9", "Bremen"),
        ("hr2019", "K9", "Rehda-Carosse GmbH", "active", None, "2019-01-31", None, None, "GmbH", "HRB 9", "Bremen"),
    ])
    out = match.apply_match(map_df, match.match_frame(map_df, names, companies)).set_index("id")
    assert out.loc["a", "register_match"] == "name_plz" and out.loc["a", "hr_source"] == "hr2022"


def test_normalizers():
    assert match.normalize_city("Freie Hansestadt Bremen") == "bremen"
    assert match.normalize_city("Bremerhaven") == "bremerhaven"
    assert match.normalize_registration("Bremen HRB 541657 HB") == "HRB 541657 HB"
    assert match.normalize_registration("541657") == "541657"
    assert match.normalize_registration(None) is None


def test_exact_join_by_imprint_number():
    from geoextract.register.match import exact_frame, merge_exact
    map_df = pd.DataFrame({
        "id": ["m1", "m2", "m3", "m4"],
        "name": ["Weser Stahlbau", "Hansa Druck", "Nordkraft", "Fremdseite"],
        "website_verified": ["name+plz", "name", "name", "mismatch"],
        "imp_register_type": ["HRB", "HRB", "HRB", "HRB"],
        "imp_register_no": ["1234", "777", "555", "1234"],
        "imp_court": ["Bremen", None, None, "Bremen"],
        "imp_legal_name": ["Weser Stahlbau GmbH", None, None, None],
    })
    comp = _companies([
        ("hr2022", "H1", "Weser Stahlbau GmbH", "active", None, "2022-08-01", "Stahlbau", None, "GmbH", "HRB 1234 HB", "Bremen"),
        ("hr2022", "H2", "Weser Stahlbau Süd GmbH", "active", None, "2022-08-01", None, None, "GmbH", "HRB 1234", "München"),
        ("gleif", "G1", "Hansa Druck GmbH", "active", None, "2026-09-07", None, None, "GmbH", "HRB 777", None),
        ("hr2022", "H3", "Nordkraft GmbH", "active", None, "2022-08-01", None, None, "GmbH", "HRB 555", "Bremen"),
        ("hr2019", "H4", "Nordkraft Beteiligungs GmbH", "active", None, "2019-01-01", None, None, "GmbH", "HRB 555", "Hamburg"),
    ])
    comp["register_type"] = "HRB"
    comp["register_number"] = comp["hr_registration"].str.replace("HRB ", "")
    comp["postcode"] = ["28237", "80331", "28195", "28195", "20095"]
    from geoextract.resolve import normalize_name
    comp["name_key_full"] = comp["name"].map(lambda n: normalize_name(n, strip_noise=True))
    ex = exact_frame(map_df, comp, scope_plz={"28237", "28195"})
    assert ex.loc[0, "register_match"] == "exact_hrb" and ex.loc[0, "hr_id"] == "H1" and ex.loc[0, "register_score"] == 100
    assert ex.loc[1, "hr_id"] == "G1" and ex.loc[1, "register_score"] == 99          # courtless GLEIF row, unique in scope
    assert ex.loc[2, "hr_id"] == "H3"                                              # Hamburg twin is outside the scope
    assert pd.isna(ex.loc[3, "register_match"])                                    # a mismatch site never joins
    prob = pd.DataFrame(index=map_df.index)
    for c in ex.columns:
        prob[c] = pd.NA
    # a weak name join (88) to another firm: the exact join wins, nothing to audit
    prob.loc[0, ["register_match", "hr_id", "hr_source", "hr_name", "register_score"]] = ["name_plz", "H2", "hr2022", "Weser Stahlbau Süd GmbH", 88.0]
    merged = merge_exact(prob, ex)
    assert merged.loc[0, "hr_id"] == "H1" and pd.isna(merged.loc[0, "hr_candidates"])
    # a strong name join (95) to another firm: kept, the imprint's owner goes to the audit column
    prob.loc[0, "register_score"] = 95.0
    merged = merge_exact(prob, ex)
    assert merged.loc[0, "hr_id"] == "H2" and "site's owner" in merged.loc[0, "hr_candidates"]
    # a strong name join to the SAME firm under another number (moved court): the exact join wins
    prob.loc[0, ["hr_name"]] = ["Weser Stahlbau GmbH"]
    merged = merge_exact(prob, ex)
    assert merged.loc[0, "hr_id"] == "H1" and "same firm" in merged.loc[0, "hr_candidates"]
