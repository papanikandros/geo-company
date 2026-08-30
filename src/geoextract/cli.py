"""geoextract CLI: extract | classify | export | run — spec §A4/§C6.

`classify` lands with Part C; until then it exits with a clear message.
"""
from __future__ import annotations

import argparse
import sys


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=None,
                   help="data root (default: $GEOEXTRACT_DATA_DIR or ./data)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geoextract",
        description="Bottom-up extraction of German companies from open geodata.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="companies from geodata → canonical table")
    p_extract.add_argument("--states", default="bremen",
                           help='comma-separated Bundesländer (names or codes), or "all"')
    p_extract.add_argument("--sources", default="osm,ied,abwaerme,overture",
                           help="comma-separated sources (osm,ied,abwaerme,overture,…)")
    p_extract.add_argument("--skip-download", action="store_true",
                           help="fail instead of downloading missing PBFs")
    _add_common(p_extract)

    p_classify = sub.add_parser("classify", help="websites/tags/registers → NACE codes")
    p_classify.add_argument("--scope", default="bremen")
    p_classify.add_argument("--mode", choices=["ai", "traditional"], default="ai")
    p_classify.add_argument("--limit", type=int, default=None)
    p_classify.add_argument("--industrial-only", action="store_true")
    _add_common(p_classify)

    p_export = sub.add_parser("export", help="map parquet (EPSG:3857) + summary JSON")
    p_export.add_argument("--scope", default="bremen")
    _add_common(p_export)

    p_run = sub.add_parser("run", help="chain extract → classify → export")
    p_run.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_run.add_argument("--sources", default="osm,ied,abwaerme,overture")
    _add_common(p_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "extract":
        from .pipeline import run_extract
        return run_extract(states=args.states, sources=args.sources,
                           data_dir=args.data_dir, skip_download=args.skip_download)
    if args.command == "export":
        from .pipeline import run_export
        return run_export(scope=args.scope, data_dir=args.data_dir)
    if args.command == "run":
        from .pipeline import run_all
        return run_all(scope=args.scope, sources=args.sources, data_dir=args.data_dir)
    if args.command == "classify":
        print("geoextract classify is Part C — not implemented yet (see GEOEXTRACT_SPEC.md).",
              file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
