"""Phase A0 — contract tests for schema.conform / schema.validate_frame."""
import pandas as pd
import pytest

from geoextract import schema


def _minimal_frame(**overrides) -> pd.DataFrame:
    base = {
        "id": ["osm_way/1", "osm_node/2"],
        "name": ["Alpha GmbH", "Beta AG"],
        "source": ["osm", "osm"],
        "latitude": [53.08, 53.09],
        "longitude": [8.80, 8.81],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_conform_adds_defaults_and_orders_columns():
    df = schema.conform(_minimal_frame())
    assert list(df.columns) == schema.CONTRACT_ORDER
    assert (df["source_count"] == 1).all()
    assert (df["confidence_score"] == 0.0).all()
    assert (df["is_industrial"] == False).all()
    assert df["name"].dtype == "string"


def test_conform_keeps_extra_debug_columns_after_contract():
    df = schema.conform(_minimal_frame(ied_activity=["2.2", None]))
    assert list(df.columns[: len(schema.CONTRACT_ORDER)]) == schema.CONTRACT_ORDER
    assert df.columns[-1] == "ied_activity"


def test_validate_accepts_conformed_frame():
    schema.validate_frame(schema.conform(_minimal_frame()), source="osm")


def test_validate_rejects_duplicate_ids():
    df = schema.conform(_minimal_frame(id=["osm_way/1", "osm_way/1"]))
    with pytest.raises(schema.SchemaError, match="duplicate ids"):
        schema.validate_frame(df, source="osm")


def test_validate_rejects_coordinates_outside_germany():
    df = schema.conform(_minimal_frame(latitude=[53.08, 40.0]))
    with pytest.raises(schema.SchemaError, match="outside Germany"):
        schema.validate_frame(df, source="osm")


def test_validate_rejects_insane_area():
    df = schema.conform(_minimal_frame())
    df.loc[0, "grounds_area_m2"] = 9e9  # a mis-set CRS symptom — must fail loudly
    with pytest.raises(schema.SchemaError, match="insane grounds_area_m2"):
        schema.validate_frame(df, source="osm")


def test_validate_reports_missing_columns():
    with pytest.raises(schema.SchemaError, match="missing contract columns"):
        schema.validate_frame(_minimal_frame(), source="osm")
