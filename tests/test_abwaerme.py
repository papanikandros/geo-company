"""Phase B2 — Abwärme adapter: aggregation, ids, geocode ladder, anchor/confidence rules."""
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from geoextract import config, geocode, resolve, schema
from geoextract.sources import abwaerme

C = abwaerme._COLS


def _potentials_frame():
    """Two sites: one with two potentials (aggregation case), one single."""
    rows = [
        {C["uid"]: 2000001, C["name"]: "​Stahlwerke Bremen GmbH",
         C["site"]: "Werk Nord", C["street"]: "Auf den Delben 35", C["plz"]: "28237",
         C["city"]: "Bremen", C["pot_name"]: "Kühlturm",
         C["heat_kwh"]: 2_000_000, C["power_kw"]: 500, C["temp_c"]: 30,
         C["email"]: "x@stahlwerke.example", C["phone"]: None},
        {C["uid"]: 2000001, C["name"]: "Stahlwerke Bremen GmbH",
         C["site"]: "Werk Nord", C["street"]: "Auf den Delben 35", C["plz"]: "28237",
         C["city"]: "Bremen", C["pot_name"]: "Abgas",
         C["heat_kwh"]: 6_000_000, C["power_kw"]: 1500, C["temp_c"]: 150,
         C["email"]: None, C["phone"]: "0421-1"},
        {C["uid"]: 2000002, C["name"]: "Molkerei Weser GmbH",
         C["site"]: None, C["street"]: "Deichweg 1", C["plz"]: "28199",
         C["city"]: "Bremen", C["pot_name"]: "Kälteanlage",
         C["heat_kwh"]: 500_000, C["power_kw"]: 100, C["temp_c"]: 25,
         C["email"]: None, C["phone"]: None},
    ]
    return pd.DataFrame(rows)


def test_aggregate_sites_sums_and_weights():
    sites = abwaerme.aggregate_sites(_potentials_frame())
    assert len(sites) == 2
    s = sites[sites.uid == 2000001].iloc[0]
    assert s.heat_mwh_a == 8000.0                       # (2 + 6) GWh in MWh
    assert s.power_kw == 2000.0
    assert s.temp_c == 120.0                            # heat-weighted: (2*30+6*150)/8
    assert s.potentials == 2
    assert s.email == "x@stahlwerke.example" and s.phone == "0421-1"
    assert s.potential_names == "Kühlturm|Abgas"


def test_site_id_stable_and_case_insensitive():
    a = abwaerme._site_id(2000001, "Auf den Delben 35", "28237")
    assert a == abwaerme._site_id(2000001, "AUF DEN DELBEN 35", "28237")
    assert a.startswith("abw_2000001_")
    assert a != abwaerme._site_id(2000001, "Auf den Delben 36", "28237")


class _FakeCoder:
    """Precision ladder faked by street content."""
    def __init__(self, data_root): pass
    def geocode(self, street, plz, city):
        if "Delben" in str(street):
            return geocode.GeocodeResult(53.12, 8.72, "house", "Bremen")
        return geocode.GeocodeResult(53.08, 8.80, "postcode", "Bremen")
    def save(self): pass
    cache = {}


def test_extract_abwaerme_end_to_end(tmp_path, monkeypatch):
    xlsx = tmp_path / "pfa.xlsx"
    # write with the 2-row header shape the reader expects
    import openpyxl
    frame = _full_workbook_frame()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = config.ABWAERME_SHEET
    ws.append([None] * len(frame.columns))            # merged §17 header row
    ws.append(list(frame.columns))                     # real header
    for row in frame.itertuples(index=False):
        ws.append([None if pd.isna(v) else v for v in row])
    wb.save(xlsx)

    from geoextract import download
    monkeypatch.setattr(download, "download_file", lambda url, dest, force=False: xlsx)
    monkeypatch.setattr(geocode, "Geocoder", _FakeCoder)
    gdf = abwaerme.extract_abwaerme(tmp_path)

    assert len(gdf) == 2
    assert len(pd.read_parquet(abwaerme.potentials_parquet(tmp_path))) == 3   # written alongside
    s = gdf[gdf["name"] == "Stahlwerke Bremen GmbH"].iloc[0]   # zero-width char stripped
    assert s["business_type"] is pd.NA or pd.isna(s["business_type"])  # stays null
    assert s["source"] == "abwaerme"
    assert s["geocode_precision"] == "house" and bool(s["dedup_anchor"])
    assert s["abw_heat_mwh_a"] == 8000.0
    m = gdf[gdf["name"] == "Molkerei Weser GmbH"].iloc[0]
    assert m["geocode_precision"] == "postcode" and not bool(m["dedup_anchor"])


LAT, LON = 53.08, 8.80


def _frame(rows) -> gpd.GeoDataFrame:
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "geometry"} for r in rows])
    gdf = gpd.GeoDataFrame(df, geometry=[r["geometry"] for r in rows], crs="EPSG:4326")
    reps = gdf.geometry.representative_point()
    gdf["longitude"] = reps.x
    gdf["latitude"] = reps.y
    return schema.conform(gdf)


def test_coarse_geocode_rows_never_merge():
    # identical name at identical position, but dedup_anchor=False keeps them apart
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Molkerei Weser", "source": "osm",
         "geometry": Point(LON, LAT)},
        {"id": "abw_1_x", "name": "Molkerei Weser GmbH", "source": "abwaerme",
         "dedup_anchor": False, "geometry": Point(LON, LAT)},
    ])])
    assert len(merged) == 2


def test_coarse_geocode_confidence_capped():
    gdf = _frame([
        {"id": "abw_1_x", "name": "Molkerei Weser GmbH", "source": "abwaerme",
         "address_street": "Deichweg", "address_housenumber": "1",
         "address_postcode": "28199", "address_city": "Bremen",
         "phone": "0421-1", "geocode_precision": "postcode",
         "geometry": Point(LON, LAT)},
    ])
    out = resolve.compute_confidence(gdf)
    assert out.iloc[0]["confidence_score"] <= config.GEOCODE_COARSE_CONFIDENCE_CAP

def _full_workbook_frame():
    """The two-site frame plus every other sheet column (monthly profile etc.)."""
    df = _potentials_frame()
    P = abwaerme.POTENTIAL_COLS
    df[P["melde_id"]] = [11, 12, 21]
    for key, header in P.items():
        if header not in df.columns:
            df[header] = [f"{key} {i}" for i in range(len(df))]
    for m in abwaerme._MONTHS:
        df[P[f"leistungsprofil_{m}_kw"]] = [100.0, 200.0, 50.0]
    df[P["taegliche_verfuegbarkeit_h"]] = [24, 16, None]
    return df


def test_potentials_frame_keeps_every_workbook_field():
    raw = _full_workbook_frame()
    pots = abwaerme.potentials_frame(raw)
    sites = abwaerme.aggregate_sites(raw)
    assert len(pots) == 3 and set(abwaerme.POTENTIAL_COLS) <= set(pots.columns)
    # keyed by the same site id the site row gets
    site_ids = {abwaerme._site_id(r.uid, r.street, r.plz) for r in sites.itertuples()}
    assert set(pots.site_id) == site_ids
    assert (pots.site_id == pots.site_id.iloc[0]).sum() == 2          # two potentials, one site
    assert list(pots.leistungsprofil_januar_kw) == [100.0, 200.0, 50.0]
    assert pots.taegliche_verfuegbarkeit_h.isna().tolist() == [False, False, True]
    assert pots.verfuegbarkeit.iloc[0] == "verfuegbarkeit 0"        # texts stay as reported
    assert pots.melde_id.dtype == "Int64"


def test_write_potentials_from_cached_workbook(tmp_path, monkeypatch):
    from geoextract import paths
    raw = _full_workbook_frame()
    monkeypatch.setattr(abwaerme, "read_workbook", lambda xlsx: raw)
    assert abwaerme.write_potentials(tmp_path) is None               # no workbook, no network
    xlsx = paths.raw_dir(tmp_path) / "abwaerme" / "pfa_datentabelle.xlsx"
    xlsx.parent.mkdir(parents=True, exist_ok=True); xlsx.write_bytes(b"")
    dest = abwaerme.write_potentials(tmp_path)
    assert dest == abwaerme.potentials_parquet(tmp_path) and dest.exists()
    assert len(pd.read_parquet(dest)) == 3
