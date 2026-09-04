"""Phase B5 — MaStR adapter: filters, natural-person drop, site aggregation, per-unit
capacity capture (B5.1: one-to-one, no sums), WZ 2025 group code (mastr_wz_code)."""
import json
import sqlite3

import pandas as pd
import pytest

from geoextract import config, paths
from geoextract.sources import mastr


def _mk_db(tmp_path):
    root = tmp_path / "data"
    db = root / config.MASTR_DB
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    wind = pd.DataFrame({
        "EinheitMastrNummer": ["SEE1", "SEE2", "SEE3", "SEE4"],
        "AnlagenbetreiberMastrNummer": ["ABR1", "ABR1", "ABR2", "ABR2"],
        "LokationMastrNummer": ["SEL1", "SEL1", "SEL2", "SEL2"],
        "EinheitBetriebsstatus": ["In Betrieb"] * 4,
        "Bruttoleistung": [3000.0, 2000.0, 4000.0, 40.0],   # SEE4 < 50 kW → dropped
        "Nettonennleistung": [2900.0, None, 3950.0, 39.0],
        "Laengengrad": [8.8, 8.801, 9.9, 9.9],
        "Breitengrad": [53.1, 53.101, 53.5, 53.5],
        "Strasse": ["Deich", "Deich", None, None],
        "Hausnummer": ["1", "1", None, None],
        "Postleitzahl": ["28195", "28195", "20095", "20095"],
        "Ort": ["Bremen", "Bremen", "Hamburg", "Hamburg"],
        "Bundesland": ["Bremen", "Bremen", "Hamburg", "Hamburg"],
        "Landkreis": [None, None, None, None],
        "Gemeindeschluessel": ["04011000", "04011000", "02000000", "02000000"],
        "Inbetriebnahmedatum": ["2019-05-01", "2020-06-01", "2018-01-01", "2021-01-01"],
        "NameWindpark": ["WP Deich", "WP Deich", "WP Elbe", "WP Elbe"],
    })
    wind.to_sql("wind_extended", con, index=False)
    solar = wind.iloc[:3].copy()
    solar["EinheitMastrNummer"] = ["SOL1", "SOL2", "SOL3"]
    solar["Bruttoleistung"] = [30.0, 500.0, 99.0]     # only SOL2 passes ≥100 kW
    solar = solar.rename(columns={"NameWindpark": "NameStromerzeugungseinheit"})
    solar.to_sql("solar_extended", con, index=False)
    gas = pd.DataFrame({
        "EinheitMastrNummer": ["GVE1"],
        "AnlagenbetreiberMastrNummer": ["ABR2"],
        "LokationMastrNummer": ["GVL1"],
        "EinheitBetriebsstatus": ["In Betrieb"],
        "MaximaleGasbezugsleistung": [5000.0],
        "Laengengrad": [10.0], "Breitengrad": [53.6],
        "Strasse": [None], "Hausnummer": [None], "Postleitzahl": ["20097"],
        "Ort": ["Hamburg"], "Bundesland": ["Hamburg"], "Landkreis": [None],
        "Inbetriebnahmedatum": ["2015-01-01"],
        "NameGasverbrauchsseinheit": ["Werk Nord Gasbezug"],
    })
    gas.to_sql("gas_consumer", con, index=False)
    actors = pd.DataFrame({
        "MastrNummer": ["ABR1", "ABR2", "ABR3"],
        "Firmenname": ["Windkraft Weser GmbH", "Hanse Energie AG", None],
        "Personenart": ["juristische Person", "juristische Person", "natürliche Person"],
        "Rechtsform": ["GmbH", "AG", None],
        "Registernummer": ["HRB 123", "HRB 456", None],
        "Registergericht": ["Bremen", "Hamburg", None],
        "Webseite": ["https://ww.example", None, None],
        "Email": [None, "info@hanse.example", None],
        "Telefon": [None, None, None],
    })
    actors.to_sql("market_actors", con, index=False)
    con.close()
    return root


def _mk_wz_xlsx(root):
    """Minimal Destatis-shaped WZ 2025 structure file (Level/Code/Titel)."""
    xlsx = root / config.MASTR_WZ2025_XLSX
    xlsx.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "Level": [1, 2, 3, 3, 3],
        "Code": ["D", "35", "35.1", "82.2", "16.1"],
        "Titel": ["ENERGIEVERSORGUNG", "Energieversorgung", "Elektrizitätsversorgung",
                  "Call Centers", "Säge- und Hobelwerke; Bearbeitung und Veredlung von Holz"],
    }).to_excel(xlsx, sheet_name=config.MASTR_WZ2025_SHEET, index=False)
    return xlsx


def test_extract_mastr_aggregates_and_filters(tmp_path):
    root = _mk_db(tmp_path)
    gdf = mastr.extract_mastr(root)
    # SEL1: 2 wind + 1 solar (SOL2, same lokation) for ABR1 → one site
    assert len(gdf) == 3
    s1 = gdf[gdf.id == "mastr_ABR1_SEL1"].iloc[0]
    assert s1["name"] == "Windkraft Weser GmbH"
    assert s1.business_type == "power"          # no WZ → tech rule, no suffix
    assert pd.isna(s1.business_subtype)          # technology is never a subtype
    assert s1.mastr_units == 3 and s1.mastr_techs == "solar+wind"
    assert "mastr_kw" not in gdf.columns         # no summed capacity anywhere
    # per-unit capacities one-to-one, MaStR field names verbatim, NaN fields omitted
    detail = json.loads(s1.mastr_tech_detail)
    assert sorted(detail) == ["solar", "wind"]
    assert detail["wind"] == [
        {"unit": "SEE1", "Bruttoleistung": 3000.0, "Nettonennleistung": 2900.0},
        {"unit": "SEE2", "Bruttoleistung": 2000.0},
    ]
    assert detail["solar"] == [{"unit": "SOL2", "Bruttoleistung": 500.0}]
    assert s1.legal_form == "GmbH" and s1.hr_registration == "HRB 123"
    assert s1.mastr_coord_method == "mastr_published" and s1.mastr_coord_dropped_units == 0
    # SEL2: the 40 kW unit SEE4 is below the 50 kW floor → one unit
    s2 = gdf[gdf.id == "mastr_ABR2_SEL2"].iloc[0]
    assert s2.mastr_units == 1
    assert json.loads(s2.mastr_tech_detail) == {
        "wind": [{"unit": "SEE3", "Bruttoleistung": 4000.0, "Nettonennleistung": 3950.0}]}
    # gas consumer site → industrial; its own MaStR field, not "kw"
    g1 = gdf[gdf.id == "mastr_ABR2_GVL1"].iloc[0]
    assert g1.business_type == "industrial"
    assert json.loads(g1.mastr_tech_detail) == {
        "gas_consumption": [{"unit": "GVE1", "MaximaleGasbezugsleistung": 5000.0}]}
    # ABR3 (natural person) contributed no rows; ids unique; cache written
    assert gdf.id.is_unique
    assert paths.source_parquet(root, "mastr", "DE").exists()


def test_business_type_from_wz_section():
    f = mastr._business_type_from_wz
    assert f("Abschnitt D – Energieversorgung") == "power"
    assert f("Abschnitt C – Verarbeitendes Gewerbe") == "industrial"
    assert f("Abschnitt E – Wasserversorgung; Abwasser- und Abfallentsorgung") == "industrial"
    assert f("Abschnitt G - Handel") == "shop"
    assert f("Abschnitt F - Baugewerbe") == "craft"
    assert f("Abschnitt R – Gesundheits- und Sozialwesen") == "amenity"
    assert f("Abschnitt K -Telekommunikation, Softwareentwicklung") == "office"
    assert f(None) is None and f(pd.NA) is None and f("something else") is None


def test_wz_overrides_tech_rule_and_streetless_sites_get_coarse_geocode(tmp_path, monkeypatch):
    root = _mk_db(tmp_path)
    con = sqlite3.connect(root / config.MASTR_DB)
    # ABR1 becomes a manufacturer → its wind/solar site must be "industrial", not "power"
    con.execute("ALTER TABLE market_actors ADD COLUMN HauptwirtdschaftszweigAbschnitt TEXT")
    con.execute("UPDATE market_actors SET HauptwirtdschaftszweigAbschnitt = "
                "'Abschnitt C – Verarbeitendes Gewerbe' WHERE MastrNummer = 'ABR1'")
    # a ≥ 50 kW unit with neither coordinates nor street, only PLZ + town
    con.execute("INSERT INTO wind_extended VALUES ('SEE5','ABR2','SEL3','In Betrieb',900.0,880.0,"
                "NULL,NULL,NULL,NULL,'27568','Bremerhaven','Bremen',NULL,'04012000',"
                "'2022-01-01','WP Hafen')")
    con.commit(); con.close()

    class StubGeocoder:
        def __init__(self, data_root): self.calls = []
        def geocode(self, street, postcode, city):
            self.calls.append((street, postcode, city))
            assert street == ""            # no street must reach Nominatim as such
            return geocode_module.GeocodeResult(53.55, 8.58, "postcode", "Bremen")
        def save(self): pass
    from geoextract import geocode as geocode_module
    monkeypatch.setattr(mastr.geocode, "Geocoder", StubGeocoder)

    gdf = mastr.extract_mastr(root, force=True)
    s1 = gdf[gdf.id == "mastr_ABR1_SEL1"].iloc[0]
    assert s1.business_type == "industrial" and s1.mastr_business_type_method == "wz_section"
    s2 = gdf[gdf.id == "mastr_ABR2_SEL2"].iloc[0]
    assert s2.business_type == "power" and s2.mastr_business_type_method == "tech"
    s3 = gdf[gdf.id == "mastr_ABR2_SEL3"].iloc[0]
    assert s3.mastr_coord_method == "address_geocode_plz_town_only"
    assert s3.geocode_precision == "postcode" and not s3.dedup_anchor
    assert s3.latitude == pytest.approx(53.55)


def test_drop_implausible_coords_bbox_and_district():
    import geopandas as gpd
    from shapely.geometry import box
    # one synthetic Landkreis "04011" = square around Bremen
    districts = gpd.GeoDataFrame({"AGS": ["04011"]},
                                 geometry=[box(8.7, 53.0, 8.9, 53.2)], crs="EPSG:4326")
    units = pd.DataFrame({
        "EinheitMastrNummer": ["inside", "near", "far", "bbox", "nocoord", "unknown_ags"],
        "Gemeindeschluessel": ["04011000", "04011000", "04011000", "04011000", "04011000", "99999000"],
        "Laengengrad": [8.8, 8.95, 9.9, 8.8, None, 9.9],
        "Breitengrad": [53.1, 53.1, 53.5, 60.0, None, 53.5],
    })
    out = mastr._drop_implausible_coords(units, districts).set_index("EinheitMastrNummer")
    assert out.loc["inside", "_coord_dropped"] is pd.NA
    assert out.loc["near", "_coord_dropped"] is pd.NA          # ~3 km outside < 10 km tolerance
    assert out.loc["far", "_coord_dropped"] == "district"      # ~80 km from declared Landkreis
    assert out.loc["bbox", "_coord_dropped"] == "bbox"
    assert out.loc["unknown_ags", "_coord_dropped"] is pd.NA   # no polygon → untestable, kept
    assert pd.isna(out.loc["far", "Laengengrad"]) and pd.isna(out.loc["bbox", "Breitengrad"])
    assert out.loc["near", "Laengengrad"] == 8.95
    # without district polygons only the bbox rule applies
    out2 = mastr._drop_implausible_coords(units, None).set_index("EinheitMastrNummer")
    assert out2["_coord_dropped"].notna().sum() == 1 and out2.loc["bbox", "_coord_dropped"] == "bbox"


def test_status_filter_raises_loudly_on_unknown_values(tmp_path):
    root = _mk_db(tmp_path)
    db = root / config.MASTR_DB
    con = sqlite3.connect(db)
    con.execute("UPDATE wind_extended SET EinheitBetriebsstatus = '99'")
    con.commit(); con.close()
    with pytest.raises(ValueError, match="status filter matched 0"):
        mastr.extract_mastr(root, force=True)


def test_wz_label_norm():
    n = mastr._norm_wz_label
    assert n("Säge- und Hobelwerke; Bearbeitung") == n("Säge – und  Hobelwerke, Bearbeitung")
    assert n("Call Center") != n("Call Centers")   # aliases exist for exactly this


def test_wz_group_code_resolved_and_contract_fields_untouched(tmp_path):
    root = _mk_db(tmp_path)
    _mk_wz_xlsx(root)
    con = sqlite3.connect(root / config.MASTR_DB)
    con.execute("ALTER TABLE market_actors ADD COLUMN HauptwirtdschaftszweigAbschnitt TEXT")
    con.execute("ALTER TABLE market_actors ADD COLUMN HauptwirtdschaftszweigGruppe TEXT")
    con.execute("UPDATE market_actors SET HauptwirtdschaftszweigAbschnitt = "
                "'Abschnitt D – Energieversorgung', HauptwirtdschaftszweigGruppe = "
                "'Elektrizitätsversorgung' WHERE MastrNummer = 'ABR1'")
    # ABR2: a MaStR label that differs from the Destatis title → alias table
    con.execute("UPDATE market_actors SET HauptwirtdschaftszweigAbschnitt = "
                "'Abschnitt O – Erbringung von sonstigen wirtschaftlichen Dienstleistungen', "
                "HauptwirtdschaftszweigGruppe = 'Call Center' WHERE MastrNummer = 'ABR2'")
    con.commit(); con.close()

    gdf = mastr.extract_mastr(root, force=True)
    s1 = gdf[gdf.id == "mastr_ABR1_SEL1"].iloc[0]
    assert s1.business_type == "power" and pd.isna(s1.business_subtype)
    assert s1.mastr_wz_code == "35.1" and s1.mastr_wz_gruppe == "Elektrizitätsversorgung"
    s2 = gdf[gdf.id == "mastr_ABR2_SEL2"].iloc[0]
    assert s2.business_type == "office" and pd.isna(s2.business_subtype)
    assert s2.mastr_wz_code == "82.2"          # alias table
    assert set(gdf.business_type) <= {"power", "office", "industrial"}


def test_wz_code_null_without_wz_file(tmp_path, capsys):
    root = _mk_db(tmp_path)
    con = sqlite3.connect(root / config.MASTR_DB)
    con.execute("ALTER TABLE market_actors ADD COLUMN HauptwirtdschaftszweigGruppe TEXT")
    con.execute("UPDATE market_actors SET HauptwirtdschaftszweigGruppe = "
                "'Elektrizitätsversorgung' WHERE MastrNummer = 'ABR1'")
    con.commit(); con.close()
    gdf = mastr.extract_mastr(root, force=True)
    assert "WARNING" in capsys.readouterr().out
    s1 = gdf[gdf.id == "mastr_ABR1_SEL1"].iloc[0]
    assert pd.isna(s1.business_subtype) and pd.isna(s1.mastr_wz_code)
    assert s1.mastr_wz_gruppe == "Elektrizitätsversorgung" and s1.business_type == "power"
