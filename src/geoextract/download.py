"""Geofabrik PBF downloads — resumable (Range), skip-if-exists — spec §A1.

Geofabrik `*-latest` files change daily, so the file's Last-Modified date is recorded in
a sidecar `<pbf>.meta.json` and later surfaced in the summary JSON for reproducibility.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests
from tqdm import tqdm

from . import config, paths

_CHUNK = 1 << 20  # 1 MiB


def _meta_path(pbf: Path) -> Path:
    return pbf.with_suffix(pbf.suffix + ".meta.json")


def read_meta(pbf: Path) -> dict:
    try:
        return json.loads(_meta_path(pbf).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def download_pbf(slug: str, data_root: Path, force: bool = False) -> Path:
    """Download the per-state PBF into data/raw/, resuming partial downloads.

    Skips when the local file exists and its size matches the server's Content-Length.
    Returns the local path.
    """
    url = config.geofabrik_url(slug)
    dest = paths.pbf_path(data_root, slug)
    part = dest.with_suffix(dest.suffix + ".part")

    head = requests.head(url, timeout=60, allow_redirects=True)
    head.raise_for_status()
    total = int(head.headers.get("Content-Length", 0))
    last_modified = head.headers.get("Last-Modified")

    if dest.exists() and not force and total and dest.stat().st_size == total:
        return dest

    offset = part.stat().st_size if part.exists() and not force else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    mode = "ab" if offset else "wb"

    with requests.get(url, headers=headers, stream=True, timeout=180) as resp:
        if offset and resp.status_code != 206:  # server ignored Range — restart
            offset, mode = 0, "wb"
        resp.raise_for_status()
        with open(part, mode) as fh, tqdm(
            total=total or None, initial=offset, unit="B", unit_scale=True,
            desc=f"{slug}-latest.osm.pbf",
        ) as bar:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                fh.write(chunk)
                bar.update(len(chunk))

    if total and part.stat().st_size != total:
        raise RuntimeError(
            f"download incomplete: {part} is {part.stat().st_size} bytes, expected {total}"
        )
    part.replace(dest)
    _meta_path(dest).write_text(
        json.dumps({"url": url, "last_modified": last_modified, "size": total}, indent=2),
        encoding="utf-8",
    )
    return dest


def download_file(url: str, dest: Path, force: bool = False) -> Path:
    """Plain cached download for small source files (xlsx registers etc.).

    Skips when ``dest`` exists (unless force); writes the same ``.meta.json`` sidecar
    as the PBF path so the summary can report source freshness.
    """
    if dest.exists() and not force:
        return dest
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    _meta_path(dest).write_text(
        json.dumps({"url": url, "last_modified": resp.headers.get("Last-Modified"),
                    "size": len(resp.content)}, indent=2),
        encoding="utf-8",
    )
    return dest
