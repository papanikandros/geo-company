"""Download the OSM PBF extract for a region (via pyrosm/Geofabrik, cached)."""
from __future__ import annotations

from pathlib import Path

from pyrosm import get_data


def download_pbf(region: str, data_dir: str | Path, update: bool = False) -> Path:
    """Fetch the Geofabrik PBF for ``region`` into ``data_dir``. Cached by pyrosm.

    ``region`` is a pyrosm dataset name, region-qualified for subregions
    (e.g. ``"germany/hamburg"``). Returns the local PBF path.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    fp = get_data(region, update=update, directory=str(data_dir))
    return Path(fp)
