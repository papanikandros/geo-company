"""Phase B3 — Overture adapter: taxonomy mapping and query construction."""
from geoextract import config
from geoextract.sources import overture

VALID_TYPES = {"office", "shop", "craft", "industrial", "amenity", "man_made", "power"}


def test_root_mapping_targets_contract_business_types():
    assert set(config.OVERTURE_ROOT_TYPES.values()) <= VALID_TYPES
    assert set(config.OVERTURE_SUBTYPE_TYPES.values()) <= VALID_TYPES


def test_apply_taxonomy_slug_override_and_drop_roots():
    import pandas as pd
    from geoextract.sources import overture
    df = pd.DataFrame({
        "business_subtype": ["chemical_plant", "lawyer", "energy_company", "lake", None],
        "ovt_root": ["services_and_business", "services_and_business",
                     "services_and_business", "geographic_entities", None],
    })
    out = overture.apply_taxonomy(df)
    assert len(out) == 4                                   # the lake is gone
    assert out.business_type.tolist()[:3] == ["industrial", "office", "power"]
    assert pd.isna(out.business_type.iloc[3])               # no root → NA, logged
    assert out.ovt_category.tolist()[:3] == ["chemical_plant", "lawyer", "energy_company"]
    assert (out.business_subtype.iloc[:3] == out.ovt_category.iloc[:3]).all()


def test_observed_roots_are_covered():
    # roots seen in the 2026-08-19.0 release (Bremen probe); geographic_entities is
    # deliberately unmapped (parks etc. are no businesses — business_type stays NA)
    observed = {"services_and_business", "shopping", "food_and_drink",
                "lifestyle_services", "community_and_government", "health_care",
                "sports_and_recreation", "travel_and_transportation", "education",
                "arts_and_entertainment", "cultural_and_historic", "lodging"}
    assert observed <= set(config.OVERTURE_ROOT_TYPES)


def test_query_pins_release_and_filters():
    q = overture._query(config.GERMANY_BBOX)
    assert config.OVERTURE_RELEASE in q
    assert f"confidence >= {config.OVERTURE_MIN_CONFIDENCE}" in q
    assert 'names."primary" IS NOT NULL' in q
    assert "'ovt_' || id" in q
