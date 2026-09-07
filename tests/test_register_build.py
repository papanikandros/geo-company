"""Item 6 stage 0a–0c — register builders on synthetic raw files + legal-form parsing."""
import bz2
import json
import sqlite3
import zipfile

import pandas as pd
import pytest

from geoextract.register import build
from geoextract.register.legalform import legal_form_from_name, register_division

# --- legal form ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,label,division", [
    ("Schwarzwaldmilch GmbH", "GmbH", "HRB"),
    ("Dold Holzwerke GmbH & Co. KG", "GmbH & Co. KG", "HRA"),
    ("Windpark Hohenlochen GmbH ＆ Co.KG", "GmbH & Co. KG", "HRA"),
    ("Sanum Kehlbeck GmbH u. Co. KG", "GmbH & Co. KG", "HRA"),
    ("Müller ＆ Zettler Stromerzeugungs KG", "KG", "HRA"),
    ("olly UG (haftungsbeschränkt)", "UG (haftungsbeschränkt)", "HRB"),
    ("Bäckerei Krause e.K.", "e.K.", "HRA"),
    ("Nordmilch eG", "eG", "GnR"),
    ("Bürgerenergie Nord Genossenschaft", "eG", "GnR"),
    ("TSV Bremen e.V.", "e.V.", "VR"),
    ("Kanzlei Meier PartG mbB", "PartG", "PR"),
    ("Gartenbau Kreissl GbR", "GbR", None),
    ("Abwasserzweckverband Breisgauer Bucht", "Körperschaft/Anstalt öR", None),
    ("Airbus SE", "SE", "HRB"),
    ("Metallbau Braun", None, None),
    (None, None, None),
])
def test_legal_form_and_division(name, label, division):
    assert legal_form_from_name(name) == label
    assert register_division(label) == division


# --- parsers -----------------------------------------------------------------------------

def test_parse_reference_and_addresses():
    assert build.parse_reference("Jena HRB 519801") == ("HRB", "519801")
    assert build.parse_reference("HRB 12345 B") == ("HRB", "12345 B")
    assert build.parse_reference("Bremen VR 4711") == ("VR", "4711")
    assert build.parse_reference("no register") == (None, None)
    assert build.split_street("Am Holunderstrauch 39") == ("Am Holunderstrauch", "39")
    assert build.split_street("Steinacher Straße 225 a") == ("Steinacher Straße", "225 a")
    assert build.split_street("Postfach") == ("Postfach", None)
    assert build.parse_address_line("Waidmannstraße 1, 22769 Hamburg.") == (
        "Waidmannstraße", "1", "22769", "Hamburg")
    assert build.parse_address_line("Bahnhofstr. 12a, 28195 Bremen") == (
        "Bahnhofstr.", "12a", "28195", "Bremen")
    assert build.parse_address_line(None) == (None, None, None, None)


# --- 0a: handelsregister.db --------------------------------------------------------------

def _sqlite(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE Companies (companyId, firstSeenDate, lastSeenDate, foundedDate, dissolutionDate);
    CREATE TABLE Names (companyId, globalId, validFrom, name, validTill, isCurrent);
    CREATE TABLE Addresses (companyId, globalId, validFrom, fullAddress, address, zipCode,
                            zipAndPlace, validTill, isCurrent);
    CREATE TABLE ReferenceNumbers (companyId, stdRefNo, nativeReferenceNumber, courtName,
                                   courtCode, referenceNumberFirstSeen, validTill);
    CREATE TABLE Objectives (companyId, globalId, validFrom, objective, validTill, isCurrent);
    CREATE TABLE Capital (companyId, globalId, validFrom, capitalAmount, capitalCurrency,
                          validTill, isCurrent);
    CREATE TABLE Positions (companyId, firstName, lastName, birthDate);
    """)
    con.executemany("INSERT INTO Companies VALUES (?,?,?,?,?)", [
        ("H1101_HRB1", "2010-01-05", "2022-03-01", "2010-01-05", ""),
        ("H1101_HRB2", "2005-02-02", "2019-11-04", "", "2019-11-04"),
    ])
    con.executemany("INSERT INTO Names VALUES (?,?,?,?,?,?)", [
        ("H1101_HRB1", "hb_1", "2010-01-05", "Weser Stahl GmbH", "2015-01-01", "False"),
        ("H1101_HRB1", "hb_2", "2015-01-01", "Weser Stahlbau GmbH", "", "True"),
        ("H1101_HRB2", "hb_3", "2005-02-02", "Hansa Druck GmbH", "", "True"),
    ])
    con.executemany("INSERT INTO Addresses VALUES (?,?,?,?,?,?,?,?,?)", [
        ("H1101_HRB1", "hb_1", "2010-01-05", "Alte Str. 1, 28195 Bremen", "Alte Straße 1",
         "28195", "28195 Bremen", "2015-01-01", "False"),
        ("H1101_HRB1", "hb_2", "2015-01-01", "Auf den Delben 35, 28237 Bremen",
         "Auf den Delben 35", "28237", "28237 Bremen", "", "True"),
        ("H1101_HRB2", "hb_3", "2005-02-02", "Marktplatz 2, 28195 Bremen", "Marktplatz 2",
         "28195", "28195 Bremen", "", "True"),
    ])
    con.executemany("INSERT INTO ReferenceNumbers VALUES (?,?,?,?,?,?,?)", [
        ("H1101_HRB1", "H1101_HRB1", "Bremen HRB 1", "Bremen", "H1101", "2010-01-05", ""),
        ("H1101_HRB2", "H1101_HRB2", "Bremen HRB 2", "Bremen", "H1101", "2005-02-02", ""),
    ])
    con.executemany("INSERT INTO Objectives VALUES (?,?,?,?,?,?)", [
        ("H1101_HRB1", "hb_2", "2015-01-01", "Herstellung von Stahlbauteilen", "", "True"),
    ])
    con.executemany("INSERT INTO Capital VALUES (?,?,?,?,?,?,?)", [
        ("H1101_HRB1", "hb_2", "2015-01-01", 250000, "EUR", "", "True"),
    ])
    con.executemany("INSERT INTO Positions VALUES (?,?,?,?)",
                    [("H1101_HRB1", "Anna", "Muster", "1970-01-01")])
    con.commit()
    con.close()


def test_build_hr2022(tmp_path):
    raw = tmp_path / "raw" / "handelsregister"
    raw.mkdir(parents=True)
    _sqlite(raw / "handelsregister.db")
    out = build.build_hr2022(tmp_path)
    df = pd.read_parquet(out)
    assert list(df.columns) == build.COMPANY_COLUMNS
    a = df.set_index("hr_id").loc["H1101_HRB1"]
    assert a["name"] == "Weser Stahlbau GmbH"          # current name, not the 2010 one
    assert a["previous_names"] == "Weser Stahl GmbH"
    assert a["name_key_full"] == "weser stahlbau"
    assert (a["street"], a["housenumber"], a["postcode"], a["city"]) == (
        "Auf den Delben", "35", "28237", "Bremen")
    assert a["street_key"] == "auf den delben 35"
    assert (a["register_type"], a["register_number"], a["hr_registration"], a["court"]) == (
        "HRB", "1", "HRB 1", "Bremen")
    assert a["legal_form"] == "GmbH" and a["register_division"] == "HRB"
    assert a["status"] == "active" and a["objective"].startswith("Herstellung")
    assert a["capital_amount"] == 250000 and a["snapshot_date"] == "2022-08-01"
    b = df.set_index("hr_id").loc["H1101_HRB2"]
    assert b["status"] == "dissolved" and b["dissolved"] == "2019-11-04"
    names = pd.read_parquet(tmp_path / "geoextract" / "register" / "hr_names_hr2022.parquet")
    assert set(names["name"]) == {"Weser Stahlbau GmbH", "Weser Stahl GmbH", "Hansa Druck GmbH"}
    assert names.loc[names["name"] == "Weser Stahl GmbH", "is_current"].item() is False
    assert "birthDate" not in df.columns and "Positions" not in str(df.columns)
    # cache: second call returns without rebuilding
    assert build.build_hr2022(tmp_path) == out


# --- 0b: 2019 dump -----------------------------------------------------------------------

def test_build_hr2019(tmp_path):
    raw = tmp_path / "raw" / "handelsregister"
    raw.mkdir(parents=True)
    recs = [
        {"company_number": "K1101R_HRB150148", "name": "olly UG (haftungsbeschränkt)",
         "current_status": "currently registered", "jurisdiction_code": "de",
         "registered_address": "Waidmannstraße 1, 22769 Hamburg.",
         "retrieved_at": "2018-11-09T18:03:03Z",
         "all_attributes": {"_registerArt": "HRB", "_registerNummer": "150148",
                            "federal_state": "Hamburg", "registrar": "Hamburg",
                            "native_company_number": "Hamburg HRB 150148",
                            "registered_office": "Hamburg"},
         "officers": [{"name": "Max Muster", "position": "geschäftsführer"}],
         "previous_names": [{"company_name": "olly Verwaltungs UG"}]},
        {"company_number": "H1101_HRA9", "name": "Fischhandel Meyer e.K.",
         "current_status": "removed", "jurisdiction_code": "de",
         "retrieved_at": "2018-06-01T00:00:00Z",
         "all_attributes": {"_registerArt": "HRA", "_registerNummer": "9",
                            "_registerNummerSuffix": "HB", "federal_state": "Bremen",
                            "registrar": "Bremen", "registered_office": "Bremerhaven"}},
    ]
    with bz2.open(raw / "de_companies_ocdata.jsonl.bz2", "wt", encoding="utf8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    df = pd.read_parquet(build.build_hr2019(tmp_path)).set_index("hr_id")
    a = df.loc["K1101R_HRB150148"]
    assert (a["street"], a["housenumber"], a["postcode"], a["city"]) == (
        "Waidmannstraße", "1", "22769", "Hamburg")
    assert a["register_type"] == "HRB" and a["hr_registration"] == "HRB 150148"
    assert a["legal_form"] == "UG (haftungsbeschränkt)" and a["state"] == "Hamburg"
    assert a["status"] == "active" and a["previous_names"] == "olly Verwaltungs UG"
    b = df.loc["H1101_HRA9"]
    assert b["status"] == "dissolved" and b["register_division"] == "HRA"
    assert b["hr_registration"] == "HRA 9 HB" and b["city"] == "Bremerhaven"
    names = pd.read_parquet(tmp_path / "geoextract" / "register" / "hr_names_hr2019.parquet")
    assert "olly Verwaltungs UG" in set(names["name"])
    assert "officers" not in df.columns


# --- 0c: GLEIF ---------------------------------------------------------------------------

def test_build_gleif(tmp_path):
    raw = tmp_path / "raw" / "gleif"
    raw.mkdir(parents=True)
    cols = list(build.GLEIF_COLS) + ["Entity.LegalAddress.Region"]
    rows = [
        {"LEI": "5299000ABC", "Entity.LegalName": "Schwarzwaldmilch GmbH",
         "Entity.LegalAddress.FirstAddressLine": "Waldkircher Straße", "Entity.LegalAddress.AddressNumber": "12",
         "Entity.LegalAddress.City": "Freiburg", "Entity.LegalAddress.PostalCode": "79106",
         "Entity.LegalAddress.Country": "DE", "Entity.LegalAddress.Region": "DE-BW",
         "Entity.RegistrationAuthority.RegistrationAuthorityID": "RA000221",
         "Entity.RegistrationAuthority.RegistrationAuthorityEntityID": "HRB 2345",
         "Entity.LegalForm.EntityLegalFormCode": "2HBR", "Entity.EntityStatus": "ACTIVE",
         "Entity.EntityCreationDate": "1998-01-01T00:00:00Z",
         "Registration.InitialRegistrationDate": "2014-05-05T00:00:00Z",
         "Registration.LastUpdateDate": "2026-09-01T00:00:00Z",
         "Registration.RegistrationStatus": "ISSUED"},
        {"LEI": "5299000XYZ", "Entity.LegalName": "Foreign NV", "Entity.LegalAddress.FirstAddressLine": "Dam 1",
         "Entity.LegalAddress.City": "Amsterdam", "Entity.LegalAddress.PostalCode": "1012",
         "Entity.LegalAddress.Country": "NL", "Entity.EntityStatus": "ACTIVE"},
    ]
    csv = pd.DataFrame(rows, columns=cols).to_csv(index=False)
    z = raw / "20260907-0800-gleif-goldencopy-lei2-golden-copy.csv.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("20260907-0800-gleif-goldencopy-lei2-golden-copy.csv", csv)
    df = pd.read_parquet(build.build_gleif(tmp_path))
    assert len(df) == 1                                  # NL row filtered out
    a = df.iloc[0]
    assert a["lei"] == "5299000ABC" and a["hr_id"] == "5299000ABC"
    assert (a["street"], a["housenumber"], a["postcode"], a["city"]) == (
        "Waldkircher Straße", "12", "79106", "Freiburg")
    assert a["hr_registration"] == "HRB 2345" and a["register_division"] == "HRB"
    assert a["status"] == "active" and a["snapshot_date"] == "2026-09-07"
    assert a["founded"] == "1998-01-01"


def test_build_skips_missing_sources(tmp_path, capsys):
    assert build.build(tmp_path) == []
    assert "skip hr2022" in capsys.readouterr().out
    assert build.load_companies(tmp_path).empty
