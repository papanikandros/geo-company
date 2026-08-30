"""Data-directory layout and cache file naming — spec §1.

data/
├── raw/                                              # downloads as received, never edited
├── geoextract/
│   ├── src_{source}/src_{source}_{scope}_4326.parquet
│   ├── boundaries_DE_4326.parquet
│   ├── companies_merged_{scope}_4326.parquet
│   ├── companies_merged_{scope}_3857.parquet
│   ├── companies_merged_{scope}_summary.json
│   └── classify/
└── previews/*.html
"""
from __future__ import annotations

import os
from pathlib import Path


def data_root(override: str | Path | None = None) -> Path:
    """Data root: explicit override > $GEOEXTRACT_DATA_DIR > ./data."""
    return Path(override or os.environ.get("GEOEXTRACT_DATA_DIR", "./data")).resolve()


def _ensured(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def raw_dir(root: Path) -> Path:
    d = root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pbf_path(root: Path, slug: str) -> Path:
    return _ensured(raw_dir(root) / f"{slug}-latest.osm.pbf")


def source_parquet(root: Path, source: str, scope: str) -> Path:
    return _ensured(root / "geoextract" / f"src_{source}" / f"src_{source}_{scope}_4326.parquet")


def boundaries_parquet(root: Path, scope: str = "DE") -> Path:
    return _ensured(root / "geoextract" / f"boundaries_{scope}_4326.parquet")


def landuse_parquet(root: Path, scope: str) -> Path:
    return _ensured(root / "geoextract" / f"landuse_{scope}_4326.parquet")


def merged_parquet(root: Path, scope: str, crs: str = "4326") -> Path:
    return _ensured(root / "geoextract" / f"companies_merged_{scope}_{crs}.parquet")


def summary_json(root: Path, scope: str) -> Path:
    return _ensured(root / "geoextract" / f"companies_merged_{scope}_summary.json")


def classify_dir(root: Path) -> Path:
    d = root / "geoextract" / "classify"
    d.mkdir(parents=True, exist_ok=True)
    return d


def previews_dir(root: Path) -> Path:
    d = root / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def quackosm_workdir(root: Path) -> Path:
    d = root / "raw" / "quackosm_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d
