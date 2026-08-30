"""Phase B1 — IED adapter: mapping, id stability, coord-column variants, end-to-end parse."""
import pandas as pd
import pytest

from geoextract import download
from geoextract.sources import ied


def test_ied_id_uses_registry_tail():
    url = "https://registry.gdi-de.org/id/de.sh/50000083_515_0"
    assert ied._ied_id(url) == "ied_de.sh/50000083_515_0"
    assert ied._ied_id(url + "/") == "ied_de.sh/50000083_515_0"


@pytest.mark.parametrize("suffix", ["wgs84", "ETRS89"])
def test_coord_col_matches_both_workbook_variants(suffix):
    df = pd.DataFrame(columns=[f"Koordinaten.Anlage_geo_lat_{suffix}",
                               f"Koordinaten.Anlage_geo_long_{suffix}"])
    assert ied._coord_col(df, "lat").endswith(suffix)
    assert ied._coord_col(df, "long").endswith(suffix)


def test_coord_col_raises_with_column_listing():
    with pytest.raises(KeyError, match="Koordinaten"):
        ied._coord_col(pd.DataFrame(columns=["Koordinaten_maps.Anlage"]), "lat")


def _workbook(path):
    """Minimal THRU.de-shaped workbook: 2 valid rows + 1 old year + 1 disused + 1 NONIED."""
    rows = [
        # kept: two installations of the same Betrieb (A3 collapse case)
        dict(Berichtsjahr=2024, **{"Name.Betrieb": "Stahlwerke Bremen GmbH",
             "Name.Anlage": "Hochofen 1", "Anlage_Typ": "IED",
             "Status.Anlage": "In Betrieb (functional)"},
             Adresse_Str="Auf den Delben", Adresse_Str_Nr="35", Adresse_PLZ="28237",
             Adresse_Ort="Bremen", Bundesland="HB",
             **{"Koordinaten.Anlage_geo_lat_ETRS89": 53.12,
                "Koordinaten.Anlage_geo_long_ETRS89": 8.72,
                "IE_RL_Haupttaetigkeit_Nr_Anh_I": "2.2",
                "InspireID.Anlage": "https://registry.gdi-de.org/id/de.hb/1_1_0",
                "InspireID.Betrieb": "https://registry.gdi-de.org/id/de.hb/1",
                "Muttergesellschaft": "ArcelorMittal"}),
        dict(Berichtsjahr=2024, **{"Name.Betrieb": "Stahlwerke Bremen GmbH",
             "Name.Anlage": "Walzwerk", "Anlage_Typ": "IED",
             "Status.Anlage": "In Betrieb (functional)"},
             Adresse_Str="Auf den Delben", Adresse_Str_Nr="35", Adresse_PLZ="28237",
             Adresse_Ort="Bremen", Bundesland="HB",
             **{"Koordinaten.Anlage_geo_lat_ETRS89": 53.1202,
                "Koordinaten.Anlage_geo_long_ETRS89": 8.7203,
                "IE_RL_Haupttaetigkeit_Nr_Anh_I": "2.3(a)",
                "InspireID.Anlage": "https://registry.gdi-de.org/id/de.hb/1_2_0",
                "InspireID.Betrieb": "https://registry.gdi-de.org/id/de.hb/1",
                "Muttergesellschaft": "ArcelorMittal"}),
        # dropped: previous report year
        dict(Berichtsjahr=2023, **{"Name.Betrieb": "Alt GmbH", "Name.Anlage": "A",
             "Anlage_Typ": "IED", "Status.Anlage": "In Betrieb (functional)"},
             Adresse_Str="X", Adresse_Str_Nr="1", Adresse_PLZ="28195",
             Adresse_Ort="Bremen", Bundesland="HB",
             **{"Koordinaten.Anlage_geo_lat_ETRS89": 53.0,
                "Koordinaten.Anlage_geo_long_ETRS89": 8.8,
                "IE_RL_Haupttaetigkeit_Nr_Anh_I": "1.1",
                "InspireID.Anlage": "https://registry.gdi-de.org/id/de.hb/9_1_0",
                "InspireID.Betrieb": "https://registry.gdi-de.org/id/de.hb/9",
                "Muttergesellschaft": "-"}),
        # dropped: decommissioned
        dict(Berichtsjahr=2024, **{"Name.Betrieb": "Weg GmbH", "Name.Anlage": "B",
             "Anlage_Typ": "IED",
             "Status.Anlage": "Dauerhaft stillgelegt / abgebaut (decommissioned)"},
             Adresse_Str="Y", Adresse_Str_Nr="2", Adresse_PLZ="28199",
             Adresse_Ort="Bremen", Bundesland="HB",
             **{"Koordinaten.Anlage_geo_lat_ETRS89": 53.05,
                "Koordinaten.Anlage_geo_long_ETRS89": 8.75,
                "IE_RL_Haupttaetigkeit_Nr_Anh_I": "5.1",
                "InspireID.Anlage": "https://registry.gdi-de.org/id/de.hb/8_1_0",
                "InspireID.Betrieb": "https://registry.gdi-de.org/id/de.hb/8",
                "Muttergesellschaft": "-"}),
        # dropped: NONIED
        dict(Berichtsjahr=2024, **{"Name.Betrieb": "Non GmbH", "Name.Anlage": "C",
             "Anlage_Typ": "NONIED", "Status.Anlage": "In Betrieb (functional)"},
             Adresse_Str="Z", Adresse_Str_Nr="3", Adresse_PLZ="28217",
             Adresse_Ort="Bremen", Bundesland="HB",
             **{"Koordinaten.Anlage_geo_lat_ETRS89": 53.09,
                "Koordinaten.Anlage_geo_long_ETRS89": 8.79,
                "IE_RL_Haupttaetigkeit_Nr_Anh_I": "6.4",
                "InspireID.Anlage": "https://registry.gdi-de.org/id/de.hb/7_1_0",
                "InspireID.Betrieb": "https://registry.gdi-de.org/id/de.hb/7",
                "Muttergesellschaft": "-"}),
    ]
    from geoextract import config
    pd.DataFrame(rows).to_excel(path, sheet_name=config.IED_SHEET, index=False)
    return path


def test_extract_ied_end_to_end(tmp_path, monkeypatch):
    xlsx = _workbook(tmp_path / "ied.xlsx")
    monkeypatch.setattr(download, "download_file", lambda url, dest, force=False: xlsx)
    gdf = ied.extract_ied(tmp_path)

    assert len(gdf) == 2                       # old year, decommissioned, NONIED dropped
    assert set(gdf["id"]) == {"ied_de.hb/1_1_0", "ied_de.hb/1_2_0"}
    row = gdf.iloc[0]
    assert row["name"] == "Stahlwerke Bremen GmbH"
    assert row["business_type"] == "industrial"
    assert row["source"] == "ied"
    assert row["state"] == "Bremen"
    assert row["address_full"] == "Auf den Delben 35, 28237 Bremen"
    assert row["ied_activity"] == "2.2"
    assert row["ied_parent_company"] == "ArcelorMittal"
    assert gdf.crs.to_epsg() == 4326

    # cached parquet round-trips and is skipped on re-run
    again = ied.extract_ied(tmp_path)
    assert len(again) == 2
