"""Verbatim source records (rawrecords.py): every column of every source row, keyed by the
adapter id, nested by the serve build as `raw`."""
import json
import sqlite3

import pandas as pd
import pyarrow.parquet as pq

from geoextract import config, paths, rawrecords
from geoextract.sources import ied


def test_text_verbatim_rules():
    t = rawrecords._text
    assert t(None) is None and t(float("nan")) is None and t("  ") is None and t("nan") is None
    assert t(3000.0) == "3000" and t(2.5) == "2.5" and t(" Bremen ") == "Bremen" and t(7) == "7"


def test_rows_to_records_keeps_every_non_empty_column():
    df = pd.DataFrame({"_id": ["a", "a"], "K": ["k1", "k2"], "X": [1.0, None], "Y": ["y", "nan"]})
    recs = rawrecords.rows_to_records(df, "_id", "thing", "K")
    assert recs[0] == ("a", "thing", "k1", [("K", "k1"), ("X", "1"), ("Y", "y")])
    assert recs[1] == ("a", "thing", "k2", [("K", "k2")])


def test_write_ied_all_columns_all_years(tmp_path):
    xlsx = paths.raw_dir(tmp_path) / "ied" / config.IED_URL.rsplit("/", 1)[-1]
    xlsx.parent.mkdir(parents=True)
    pd.DataFrame({
        "Berichtsjahr": [2023, 2024],
        "InspireID.Anlage": ["https://registry.gdi-de.org/id/de.hb/1_1_0"] * 2,
        "Name.Betrieb": ["Weser Stahl", "Weser Stahl"],
        "Genehmigung_URL": ["https://x/2023", None],
        "Inspektionen_Anzahl": [2, 3],
    }).to_excel(xlsx, sheet_name=config.IED_SHEET, index=False)
    dest = rawrecords.write_ied(tmp_path)
    df = pd.read_parquet(dest)
    assert list(df.columns) == rawrecords.RAW_COLUMNS and len(df) == 2
    assert set(df["id"]) == {ied._ied_id("https://registry.gdi-de.org/id/de.hb/1_1_0")}
    f0 = dict(df["fields"].iloc[0])
    assert f0["Genehmigung_URL"] == "https://x/2023" and f0["Inspektionen_Anzahl"] == "2"
    assert "Genehmigung_URL" not in dict(df["fields"].iloc[1])   # empty cells are dropped


def _mastr_db(root):
    db = root / config.MASTR_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    pd.DataFrame({"EinheitMastrNummer": ["SEE1", "SEE2", "SEE9"],
                  "AnlagenbetreiberMastrNummer": ["ABR1", "ABR1", "ABR9"],
                  "LokationMastrNummer": ["SEL1", "SEL1", "SEL9"],
                  "EegMastrNummer": ["EEG1", None, "EEG9"], "GenMastrNummer": ["GEN1", "GEN1", None],
                  "Hersteller": ["Enercon", "Vestas", "x"], "Nabenhoehe": [98.0, 120.0, 1.0],
                  "Bruttoleistung": [3000.0, 2000.0, 5.0]}).to_sql("wind_extended", con, index=False)
    pd.DataFrame({"EegMastrNummer": ["EEG1", "EEG9"], "Zuschlagsnummer": ["Z1", "Z9"]}).to_sql("wind_eeg", con, index=False)
    pd.DataFrame({"GenMastrNummer": ["GEN1"], "Behoerde": ["Gewerbeaufsicht Bremen"]}).to_sql("permit", con, index=False)
    pd.DataFrame({"MastrNummer": ["ABR1", "ABR9"], "Firmenname": ["Windkraft Weser GmbH", "x"],
                  "Kmu": ["Ja", None]}).to_sql("market_actors", con, index=False)
    pd.DataFrame({"MastrNummer": ["SEL1"], "NameDerTechnischenLokation": ["WP Deich"]}).to_sql("locations_extended", con, index=False)
    pd.DataFrame({"LokationMastrNummer": ["SEL1"], "NetzanschlusspunktMastrNummer": ["NAP1"],
                  "MaximaleEinspeiseleistung": [5000.0]}).to_sql("grid_connections", con, index=False)
    con.close()


def test_write_mastr_units_and_linked_records(tmp_path):
    _mastr_db(tmp_path)
    sites = pd.DataFrame({"id": ["mastr_ABR1_SEL1"],
                          "mastr_tech_detail": [json.dumps({"wind": [{"unit": "SEE1", "Bruttoleistung": 3000.0},
                                                                     {"unit": "SEE2", "Bruttoleistung": 2000.0}]})]})
    p = paths.source_parquet(tmp_path, "mastr", "DE")
    sites.to_parquet(p)
    dest = rawrecords.write_mastr(tmp_path)
    df = pd.read_parquet(dest)
    assert set(df["id"]) == {"mastr_ABR1_SEL1"}           # SEE9 / ABR9 belong to no site
    kinds = df.groupby("record")["key"].apply(list).to_dict()
    assert kinds["unit:wind_extended"] == ["SEE1", "SEE2"]
    assert kinds["eeg:wind_eeg"] == ["EEG1"] and kinds["permit"] == ["GEN1"]
    assert kinds["operator"] == ["ABR1"] and kinds["location"] == ["SEL1"] and kinds["grid_connection"] == ["NAP1"]
    unit = dict(df[df["key"] == "SEE1"]["fields"].iloc[0])
    assert unit["Hersteller"] == "Enercon" and unit["Nabenhoehe"] == "98" and unit["Bruttoleistung"] == "3000"
    assert dict(df[df["record"] == "operator"]["fields"].iloc[0])["Kmu"] == "Ja"


def test_write_register_histories_no_officers(tmp_path, monkeypatch):
    db = tmp_path / config.HR2022_DB
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE Companies (companyId, firstSeenDate, lastSeenDate, foundedDate, dissolutionDate);
    CREATE TABLE Names (companyId, globalId, validFrom, name, validTill, isCurrent);
    CREATE TABLE Addresses (companyId, globalId, validFrom, fullAddress, address, zipCode, zipAndPlace, validTill, isCurrent);
    CREATE TABLE ReferenceNumbers (companyId, stdRefNo, nativeReferenceNumber, courtName, courtCode, referenceNumberFirstSeen, validTill);
    CREATE TABLE Objectives (companyId, globalId, validFrom, objective, validTill, isCurrent);
    CREATE TABLE Capital (companyId, globalId, validFrom, capitalAmount, capitalCurrency, validTill, isCurrent);
    CREATE TABLE Positions (companyId, firstName, lastName, birthDate);
    INSERT INTO Companies VALUES ('H1101_HRB1', '2010-01-05', '2022-03-01', '2010-01-05', '');
    INSERT INTO Names VALUES ('H1101_HRB1', 'hb_1', '2010-01-05', 'Weser Stahl GmbH', '2015-01-01', 'False');
    INSERT INTO Names VALUES ('H1101_HRB1', 'hb_2', '2015-01-01', 'Weser Stahlbau GmbH', '', 'True');
    INSERT INTO Addresses VALUES ('H1101_HRB1', 'hb_2', '2015-01-01', 'Auf den Delben 35, 28237 Bremen', 'Auf den Delben 35', '28237', '28237 Bremen', '', 'True');
    INSERT INTO ReferenceNumbers VALUES ('H1101_HRB1', 'H1101_HRB1', 'Bremen HRB 1', 'Bremen', 'H1101', '2010-01-05', '');
    INSERT INTO Positions VALUES ('H1101_HRB1', 'Anna', 'Muster', '1970-01-01');
    INSERT INTO Companies VALUES ('H1101_HRB2', '2005-02-02', '2019-11-04', '', '2019-11-04');
    """)
    con.commit(); con.close()
    monkeypatch.setattr(rawrecords, "gleif_zip", lambda root: None, raising=False)
    ids = {"hr2022": {"H1101_HRB1"}}
    dest = rawrecords.write_register(tmp_path, ids)
    df = pd.read_parquet(dest)
    assert set(df["id"]) == {"hr2022:H1101_HRB1"}                 # HRB2 not referenced → absent
    assert sorted(df["record"]) == ["address", "company", "name", "name", "reference"]
    names = df[df["record"] == "name"]
    assert [dict(f)["name"] for f in names["fields"]] == ["Weser Stahl GmbH", "Weser Stahlbau GmbH"]
    assert dict(names["fields"].iloc[0])["validTill"] == "2015-01-01"   # the history is kept
    assert "officer" not in set(df["record"]) and "position" not in set(df["record"])


def test_register_ids_from_merged_tables(tmp_path):
    p = paths.merged_parquet(tmp_path, "bremen", "4326")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"id": ["a", "b", "c"], "hr_id": ["X1", None, "L1"],
                  "hr_source": ["hr2022", None, "gleif"]}).to_parquet(p)
    assert rawrecords.register_ids(tmp_path) == {"hr2022": {"X1"}, "gleif": {"L1"}}
