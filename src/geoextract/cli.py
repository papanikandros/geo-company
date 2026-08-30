"""geoextract CLI — OSM-only MVP."""
from __future__ import annotations

import time
from pathlib import Path

import click
from pyrosm import OSM

from .area import compute_grounds_area
from .config import load_config
from .download import download_pbf
from .export import export
from .extract_osm import assign_admin, extract_businesses


@click.group()
def main() -> None:
    """Extract German companies from open geodata."""


@main.command()
@click.option("--config", "config_path", default=None, help="Path to config YAML.")
@click.option("--region", default=None, help="pyrosm region (overrides config), e.g. germany/hamburg.")
@click.option("--data-dir", default="./data", help="Where PBF downloads land.")
@click.option("--output-dir", default="./output", help="Where outputs are written.")
@click.option("--formats", default=None, help="Comma-separated: csv,geojson,gpkg,parquet.")
@click.option("--skip-download", is_flag=True, help="Use an existing PBF in --data-dir.")
def run(config_path, region, data_dir, output_dir, formats, skip_download) -> None:
    """Run the full OSM-only pipeline: download → extract → grounds area → export."""
    cfg = load_config(config_path)
    if region:
        cfg["region"] = region
        cfg["scope_name"] = region.split("/")[-1]
    if formats:
        cfg["formats"] = [f.strip() for f in formats.split(",")]

    t0 = time.time()
    region = cfg["region"]
    click.echo(f"[1/5] PBF for {region!r} …")
    if skip_download:
        # pyrosm caches as <name>-latest.osm.pbf in data_dir
        pbf = Path(data_dir) / f"{region.split('/')[-1]}-latest.osm.pbf"
        if not pbf.exists():
            raise click.ClickException(f"--skip-download but {pbf} not found")
    else:
        pbf = download_pbf(region, data_dir)
    click.echo(f"      {pbf}")

    osm = OSM(str(pbf))

    click.echo("[2/5] extract businesses …")
    gdf = extract_businesses(osm, cfg)
    click.echo(f"      {len(gdf)} businesses")

    click.echo("[3/5] spatial join admin boundaries …")
    gdf = assign_admin(gdf, osm, cfg)

    click.echo("[4/5] compute grounds area …")
    gdf = compute_grounds_area(gdf, osm, cfg)

    click.echo("[5/5] export …")
    written = export(gdf, output_dir, cfg["scope_name"], cfg["formats"], cfg)
    for p in written:
        click.echo(f"      → {p}")
    click.echo(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
