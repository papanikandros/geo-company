"""Shared data contract for the extraction pipeline.

The full plan defines 30 fields; this MVP populates the OSM-derivable subset. The
sector-classification fields (`nace_codes`, `nace_section`, `is_industrial`, `wz_code`)
are present but empty — they are filled downstream by the classifier pipeline.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Company(BaseModel):
    id: str                                    # e.g. "osm_w123456"
    name: Optional[str] = None
    business_type: Optional[str] = None        # office | shop | craft | industrial
    business_subtype: Optional[str] = None     # the tag value, e.g. "supermarket"

    address_street: Optional[str] = None
    address_housenumber: Optional[str] = None
    address_postcode: Optional[str] = None
    address_city: Optional[str] = None
    address_full: Optional[str] = None

    state: Optional[str] = None                # spatial join (admin_level=4)
    district: Optional[str] = None             # spatial join (admin_level=district)

    website: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    latitude: Optional[float] = None
    longitude: Optional[float] = None

    grounds_area_m2: Optional[float] = None
    grounds_area_source: Optional[str] = None  # own_polygon | landuse_zone

    # Sector classification — populated downstream by the classifier.
    nace_codes: list[str] = Field(default_factory=list)
    nace_section: Optional[str] = None
    wz_code: Optional[str] = None
    is_industrial: Optional[bool] = None

    source: str = "osm"
    source_count: int = 1
    confidence_score: Optional[float] = None


# Column order for flat (CSV) export — geometry/lat/lng handled separately.
EXPORT_COLUMNS = [
    "id", "name", "business_type", "business_subtype",
    "address_street", "address_housenumber", "address_postcode", "address_city",
    "address_full", "state", "district",
    "website", "phone", "email",
    "latitude", "longitude",
    "grounds_area_m2", "grounds_area_source",
    "nace_codes", "nace_section", "wz_code", "is_industrial",
    "source", "source_count", "confidence_score",
]
