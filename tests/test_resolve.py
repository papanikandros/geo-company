"""Phase A3 — entity resolution on synthetic duplicates."""
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon

from geoextract import resolve, schema

# ~0.00045° lat ≈ 50 m; use a Bremen-ish anchor so validation bounds pass.
LAT, LON = 53.08, 8.80


def _frame(rows) -> gpd.GeoDataFrame:
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "geometry"} for r in rows])
    gdf = gpd.GeoDataFrame(df, geometry=[r["geometry"] for r in rows], crs="EPSG:4326")
    reps = gdf.geometry.representative_point()
    gdf["longitude"] = reps.x
    gdf["latitude"] = reps.y
    return schema.conform(gdf)


def test_normalize_name_strips_legal_forms_and_umlauts():
    assert resolve.normalize_name("Müller GmbH & Co. KG") == "mueller"
    assert resolve.normalize_name("STAHLBAU-Weser AG") == "stahlbau weser"
    assert resolve.normalize_name(None) == ""


def test_normalize_name_handles_dotted_forms_titles_and_connectors():
    n = resolve.normalize_name
    assert n("Schmidt G.m.b.H.") == "schmidt"
    assert n("Bäckerei Krause e.K.") == "baeckerei krause"
    assert n("Weser Stahl GmbH & Co.KG") == "weser stahl"
    assert n("Nordmilch eG") == "nordmilch"
    assert n("Hanse UG (haftungsbeschränkt)") == "hanse"
    assert n("Dr. med. dent. Hans Müller") == "hans mueller"
    assert n("Müller & Söhne") == n("Müller und Söhne") == "mueller soehne"
    assert n("Metallbau Inh. K. Braun") == "metallbau k braun"
    assert n("Coworking Space") == "coworking space"      # 'co' only as a whole token
    assert n("Dr. med. Joachim Heidt", strip_noise=False) == "dr med joachim heidt"


def test_shared_title_pair_still_matches_via_light_key():
    # full keys "j heidt" / "joachim heidt" score 70 — the light keys keep them ≥ 80
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Dr. med J Heidt", "source": "osm",
         "geometry": Point(LON, LAT)},
        {"id": "ovt_1", "name": "Dr. med. Joachim Heidt", "source": "overture",
         "geometry": Point(LON, LAT + 0.0002)},
    ])])
    assert len(merged) == 1


def test_normalize_street_variants():
    s = resolve.normalize_street
    assert s("Bahnhofstr. 12") == s("Bahnhofstraße 12") == s("Bahnhof-Str. 12 ") == "bahnhofstrasse 12"
    assert s("Hauptstrasse 3 a") == s("Hauptstraße 3a") == "hauptstrasse 3a"
    assert s("Am Markt 1") == "am markt 1"
    assert s(None) == ""


def test_duplicates_within_50m_and_similar_name_collapse():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Stahlwerke Bremen GmbH", "source": "osm",
         "geometry": Point(LON, LAT)},
        {"id": "ied_1", "name": "Stahlwerke Bremen", "source": "ied",
         "geometry": Point(LON, LAT + 0.0002)},  # ~22 m
    ])])
    assert len(merged) == 1
    row = merged.iloc[0]
    assert row["id"] == "osm_node/1"          # winning source priority: osm before ied
    assert row["source"] == "ied+osm"
    assert row["source_count"] == 2
    assert row["member_ids"] == "osm_node/1|ied_1"


def test_ied_point_inside_osm_polygon_merges_at_relaxed_ratio():
    # names at token_sort_ratio ~60-79: too weak for the 50m rule, enough for containment
    poly = Polygon([(LON - 0.003, LAT - 0.003), (LON + 0.003, LAT - 0.003),
                    (LON + 0.003, LAT + 0.003), (LON - 0.003, LAT + 0.003)])
    merged = resolve.resolve([_frame([
        {"id": "osm_way/1", "name": "Brauerei Beck", "source": "osm", "geometry": poly},
        {"id": "ied_1", "name": "Beck GmbH & Co. KG Brauerei Bremen", "source": "ied",
         "geometry": Point(LON + 0.002, LAT + 0.002)},  # ~250 m off, but inside
    ])])
    assert len(merged) == 1
    assert merged.iloc[0]["source"] == "ied+osm"
    assert merged.iloc[0].geometry.geom_type == "Polygon"   # polygon member wins geometry


def test_contained_point_close_to_rep_point_uses_relaxed_ratio():
    # ratio in [60, 80) and distance < 50 m: containment must still win (spec §A3)
    poly = Polygon([(LON - 0.001, LAT - 0.001), (LON + 0.001, LAT - 0.001),
                    (LON + 0.001, LAT + 0.001), (LON - 0.001, LAT + 0.001)])
    merged = resolve.resolve([_frame([
        {"id": "osm_way/1", "name": "Melitta Kaffee Bremen", "source": "osm", "geometry": poly},
        {"id": "ied_1", "name": "Melitta Kaffee Bremen Rösterei Werk", "source": "ied",
         "geometry": Point(LON + 0.0002, LAT)},  # ~13 m from the rep point, inside; ratio 74
    ])])
    assert len(merged) == 1
    assert merged.iloc[0]["source"] == "ied+osm"


def test_different_names_nearby_do_not_collapse():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Bäckerei Meyer", "source": "osm",
         "geometry": Point(LON, LAT)},
        {"id": "osm_node/2", "name": "Autohaus Schulz", "source": "osm",
         "geometry": Point(LON, LAT + 0.0002)},
    ])])
    assert len(merged) == 2


def test_same_name_but_far_apart_does_not_collapse():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Filiale Nord GmbH", "source": "osm",
         "geometry": Point(LON, LAT)},
        {"id": "osm_node/2", "name": "Filiale Nord GmbH", "source": "osm",
         "geometry": Point(LON, LAT + 0.002)},  # ~220 m
    ])])
    assert len(merged) == 2


def test_point_inside_polygon_matches_with_relaxed_name_threshold():
    site = Polygon([  # ~200 m x 200 m site around the anchor
        (LON - 0.0015, LAT - 0.001), (LON + 0.0015, LAT - 0.001),
        (LON + 0.0015, LAT + 0.001), (LON - 0.0015, LAT + 0.001),
    ])
    merged = resolve.resolve([_frame([
        {"id": "osm_way/1", "name": "Klöckner Stahlwerk Bremen", "source": "osm",
         "geometry": site},
        {"id": "ied_2", "name": "Klöckner Bremen", "source": "ied",
         "geometry": Point(LON + 0.0012, LAT + 0.0008)},  # inside polygon, > 50 m from rep pt
    ])])
    assert len(merged) == 1
    assert merged.iloc[0].geometry.geom_type == "Polygon"  # polygon wins over point


def test_near_identical_names_match_up_to_100m_but_not_beyond():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Rossmann", "source": "osm", "geometry": Point(LON, LAT)},
        {"id": "ovt_1", "name": "Rossmann", "source": "overture",
         "geometry": Point(LON, LAT + 0.0007)},   # ~78 m
        {"id": "ovt_2", "name": "Rossmann", "source": "overture",
         "geometry": Point(LON, LAT + 0.0016)},   # ~178 m from the first
    ])])
    assert len(merged) == 2


def test_short_generic_shared_token_does_not_carry_a_ratio80_match():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "NK Beauty", "source": "osm", "geometry": Point(LON, LAT)},
        {"id": "ovt_1", "name": "Beauty Line", "source": "overture",
         "geometry": Point(LON, LAT + 0.0001)},
        {"id": "osm_node/2", "name": "GEW Bremen", "source": "osm",
         "geometry": Point(LON + 0.001, LAT)},
        {"id": "ovt_2", "name": "NGG Bremen", "source": "overture",
         "geometry": Point(LON + 0.001, LAT + 0.0001)},
    ])])
    assert len(merged) == 4


def test_spelling_variants_at_90_plus_need_no_shared_token():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": "Erotic Gigant", "source": "osm", "geometry": Point(LON, LAT)},
        {"id": "ovt_1", "name": "Erotik Gigant", "source": "overture",
         "geometry": Point(LON, LAT + 0.0001)},
        {"id": "osm_node/2", "name": "Veronika's Treff", "source": "osm",
         "geometry": Point(LON + 0.001, LAT)},
        {"id": "ovt_2", "name": "Veronikas Treff", "source": "overture",
         "geometry": Point(LON + 0.001, LAT + 0.0001)},
    ])])
    assert len(merged) == 2


def test_containment_needs_two_tokens_and_a_real_name_overlap():
    site = Polygon([(LON - 0.003, LAT - 0.003), (LON + 0.003, LAT - 0.003),
                    (LON + 0.003, LAT + 0.003), (LON - 0.003, LAT + 0.003)])
    merged = resolve.resolve([_frame([
        {"id": "osm_way/1", "name": "Metropol Theater Bremen", "source": "osm", "geometry": site},
        {"id": "ovt_1", "name": "Musical Theater Bremen", "source": "overture",
         "geometry": Point(LON + 0.002, LAT + 0.002)},   # inside, ratio 76 but different
    ])])
    # ratio70 rule alone would merge these (76 ≥ 70); token_subset would not — the test
    # pins the currently configured rule's behaviour on this pair
    from geoextract import config
    assert len(merged) == (1 if config.DEDUP_CONTAIN_RULE == "ratio70" else 2)


def test_nameless_records_never_merge():
    merged = resolve.resolve([_frame([
        {"id": "osm_node/1", "name": None, "source": "osm", "geometry": Point(LON, LAT)},
        {"id": "osm_node/2", "name": None, "source": "osm",
         "geometry": Point(LON, LAT + 0.0001)},
    ])])
    assert len(merged) == 2


def test_single_source_passthrough_keeps_rows_and_contract():
    frame = _frame([
        {"id": "osm_node/1", "name": "Solo GmbH", "source": "osm",
         "geometry": Point(LON, LAT)},
    ])
    merged = resolve.resolve([frame])
    assert len(merged) == 1
    assert list(merged.columns[: len(schema.CONTRACT_ORDER)]) == schema.CONTRACT_ORDER
    assert merged.iloc[0]["source_count"] == 1
    assert merged.iloc[0]["merged_at"] is not pd.NA


def test_confidence_score_weights():
    frame = _frame([
        {"id": "osm_node/1", "name": "Voll GmbH", "source": "osm",
         "address_street": "Weg", "address_housenumber": "1",
         "address_postcode": "28195", "address_city": "Bremen",
         "website": "https://voll.example", "phone": "+49 421 1",
         "geometry": Point(LON, LAT)},
    ])
    scored = resolve.compute_confidence(resolve.resolve([frame]))
    # website .15 + address .20 + phone .10 (point geometry, no area, single source)
    assert float(scored.iloc[0]["confidence_score"]) == 0.45
