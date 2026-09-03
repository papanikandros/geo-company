"""Address geocoding via Nominatim with a persistent disk cache — spec §4.1.

Used by address-only sources (Abwärme, later Handelsregister). Public Nominatim is
throttled to ≥ 1 req/s; every result — including misses — is cached in
``data/raw/geocode_cache.parquet`` keyed by the normalized address, so each address
is fetched once, ever. Set ``NOMINATIM_URL`` for a self-hosted instance.

Precision ladder per address (spec §4.1: house / street / postcode / city):
  1. structured street + housenumber + PLZ + city → ``house`` or ``street``
  2. PLZ + city                                   → ``postcode``
  3. city                                          → ``city``
Rows at ``postcode``/``city`` precision must not anchor dedup and get their
confidence capped (handled in resolve.py via the ``dedup_anchor`` debug flag).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from . import config

# Nominatim addresstype values that mean the hit is the addressed object itself.
_HOUSE_TYPES = {"building", "house", "residential", "industrial", "commercial",
                "amenity", "shop", "office", "craft", "man_made", "place_house",
                "isolated_dwelling", "yes"}
_STREET_TYPES = {"road", "street", "pedestrian", "highway"}


@dataclass
class GeocodeResult:
    latitude: float | None
    longitude: float | None
    precision: str | None      # house | street | postcode | city | None (miss)
    state: str | None          # Bundesland per Nominatim addressdetails


def _norm_key(street: str, postcode: str, city: str) -> str:
    return "|".join(" ".join(str(p or "").lower().split()) for p in (street, postcode, city))


class Geocoder:
    """Disk-cached Nominatim client. Instantiate once per run."""

    def __init__(self, data_root: Path):
        self.cache_path = data_root / "raw" / "geocode_cache.parquet"
        self._last_request = 0.0
        self._dirty = 0
        if self.cache_path.exists():
            df = pd.read_parquet(self.cache_path)
            self.cache: dict[str, dict] = {
                r["key"]: r for r in df.to_dict("records")}
        else:
            self.cache = {}

    # -- persistence -----------------------------------------------------------------
    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(list(self.cache.values())).to_parquet(self.cache_path)
        self._dirty = 0

    # -- HTTP ------------------------------------------------------------------------
    def _request(self, params: dict) -> list[dict]:
        # transient timeouts/5xx/429 are retried with backoff; a persistent failure
        # raises so the run aborts WITHOUT caching a false miss (rerun resumes)
        for attempt, backoff in enumerate((5, 15, 45), start=1):
            wait = config.NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = requests.get(
                    f"{config.NOMINATIM_URL}/search",
                    params={"format": "jsonv2", "limit": 1, "countrycodes": "de",
                            "addressdetails": 1, **params},
                    headers={"User-Agent": config.NOMINATIM_USER_AGENT},
                    timeout=60,
                )
                if resp.status_code in (429, 502, 503, 504):
                    raise requests.exceptions.RetryError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.RetryError) as err:
                self.save()   # keep everything gathered so far
                print(f"[geocode] transient error ({err}); retry {attempt}/3 in {backoff}s")
                time.sleep(backoff)
        raise RuntimeError("Nominatim unreachable after 3 retries — rerun to resume "
                           "(cache is persisted)")

    # -- public ----------------------------------------------------------------------
    def geocode(self, street: str, postcode: str, city: str) -> GeocodeResult:
        key = _norm_key(street, postcode, city)
        hit = self.cache.get(key)
        if hit is not None:
            return GeocodeResult(hit.get("latitude"), hit.get("longitude"),
                                 hit.get("precision"), hit.get("state"))

        result = self._lookup(street, postcode, city)
        self.cache[key] = {
            "key": key, "latitude": result.latitude, "longitude": result.longitude,
            "precision": result.precision, "state": result.state,
        }
        self._dirty += 1
        if self._dirty >= 200:   # crash-safe: persist every 200 fresh lookups
            self.save()
        return result

    def _lookup(self, street: str, postcode: str, city: str) -> GeocodeResult:
        # 1. full structured address → house / street (skipped without a street: a
        #    postcode/town-only query must never be labelled house precision)
        if street:
            rows = self._request({"street": street, "postalcode": postcode, "city": city})
            if rows:
                addresstype = str(rows[0].get("addresstype", ""))
                precision = "house" if addresstype not in _STREET_TYPES else "street"
                return self._result(rows[0], precision)
        # 2. postcode + city → postcode centroid
        rows = self._request({"postalcode": postcode, "city": city})
        if rows:
            return self._result(rows[0], "postcode")
        # 3. city only → city centroid
        rows = self._request({"city": city})
        if rows:
            return self._result(rows[0], "city")
        return GeocodeResult(None, None, None, None)

    @staticmethod
    def _result(row: dict, precision: str) -> GeocodeResult:
        return GeocodeResult(
            float(row["lat"]), float(row["lon"]), precision,
            (row.get("address") or {}).get("state")
            or (row.get("address") or {}).get("city"),
        )
