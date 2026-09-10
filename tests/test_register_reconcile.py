"""Item 6 stage 4 — register→map reconciliation helpers (offline geocode, industrial rule)."""
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from geoextract.register import reconcile


def test_industrial_from_objective():
    assert reconcile.industrial_from_objective("Die Herstellung von Stahlbauteilen")
    assert reconcile.industrial_from_objective("Betrieb eines Windparks in Bremerhaven")
    assert not reconcile.industrial_from_objective("Handel mit Stahlbauteilen aller Art")
    assert not reconcile.industrial_from_objective("Die Verwaltung von Beteiligungen an Produktionsbetrieben")
    assert not reconcile.industrial_from_objective(None)


def test_geocode_register_exact_then_street():
    addr = gpd.GeoDataFrame({
        "postcode": ["28195", "28195", "28195"], "street": ["Marktstraße", "Marktstraße", "Am Wall"],
        "housenumber": ["1", "3", "10"], "city": ["Bremen"] * 3,
    }, geometry=[Point(8.80, 53.075), Point(8.81, 53.076), Point(8.82, 53.08)], crs="EPSG:4326")
    addr["street_key"] = addr["street"].map(reconcile.normalize_street)
    addr["addr_key"] = [reconcile.normalize_street(f"{s} {n}") for s, n in zip(addr["street"], addr["housenumber"])]
    addr["longitude"], addr["latitude"] = addr.geometry.x, addr.geometry.y
    companies = pd.DataFrame({
        "postcode": ["28195", "28195", "28195", "28199"],
        "street": ["Marktstr.", "Marktstraße", "Am Wall", "Nirgendwo"],
        "street_key": ["marktstrasse 3", "marktstrasse 99", "am wall 10", "nirgendwo 1"],
    })
    out = reconcile.geocode_register(companies, addr)
    assert list(out["hr_geocode_method"]) == ["exact", "street", "exact", "none"]
    assert abs(out.loc[0, "longitude"] - 8.81) < 1e-9          # Marktstraße 3 exactly
    assert abs(out.loc[1, "longitude"] - 8.805) < 1e-9         # street centre of Marktstraße
    assert pd.isna(out.loc[3, "longitude"])


