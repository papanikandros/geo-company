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
    p_extract.add_argument("--sources", default="osm,ied,abwaerme,overture,mastr",
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

    p_reg = sub.add_parser("register", help="register + website pipeline (item 6): register side")
    reg_sub = p_reg.add_subparsers(dest="register_cmd", required=True)
    p_reg_build = reg_sub.add_parser("build", help="bulk register files → hr_companies/hr_names parquet")
    p_reg_build.add_argument("--sources", default="hr2022,hr2019,gleif",
                             help="comma-separated: hr2022 (handelsregister.db), hr2019 (OKFN dump), gleif")
    p_reg_build.add_argument("--force", action="store_true", help="rebuild existing parquet files")
    _add_common(p_reg_build)
    p_reg_match = reg_sub.add_parser("match", help="join the merged table to the register tables (name + postcode)")
    p_reg_match.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_reg_match.add_argument("--sources", default="hr2022,gleif,hr2019", help="register sources to use")
    _add_common(p_reg_match)
    p_reg_rec = reg_sub.add_parser("reconcile", help="register → map diff: register-only companies, geocoded offline")
    p_reg_rec.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    _add_common(p_reg_rec)

    p_wd = sub.add_parser("wikidata", help="item 6c: enrich the merged table from Wikidata items")
    wd_sub = p_wd.add_subparsers(dest="wd_cmd", required=True)
    p_wd_enrich = wd_sub.add_parser("enrich", help="fetch missing items (SPARQL, cached) and write wd_* columns")
    p_wd_enrich.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_wd_enrich.add_argument("--no-fetch", action="store_true", help="only apply the cached items")
    _add_common(p_wd_enrich)

    p_web = sub.add_parser("web", help="register + website pipeline (item 6): website side")
    web_sub = p_web.add_subparsers(dest="web_cmd", required=True)
    p_web_hyg = web_sub.add_parser("hygiene", help="normalise URLs, split own sites from listings")
    p_web_hyg.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_web_hyg.add_argument("--no-propagate", action="store_true",
                           help="do not copy URLs between rows with equal name key + postcode")
    _add_common(p_web_hyg)
    p_web_disc = web_sub.add_parser("discover", help="stage 3b: guess domains from names, DNS + one verifying fetch")
    p_web_disc.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_web_disc.add_argument("--all", action="store_true", help="all named rows without a site (default: industrial only)")
    p_web_disc.add_argument("--limit", type=int, default=None)
    p_web_disc.add_argument("--dry-run", action="store_true", help="candidates + DNS only, no page fetches, no write")
    _add_common(p_web_disc)
    p_web_imp = web_sub.add_parser("impressum", help="stage 1: fetch the imprint of every own website, extract the legal fields")
    p_web_imp.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_web_imp.add_argument("--all", action="store_true", help="all own-website rows (default: industrial only)")
    p_web_imp.add_argument("--limit", type=int, default=None)
    p_web_imp.add_argument("--workers", type=int, default=8)
    p_web_imp.add_argument("--refetch", action="store_true", help="ignore the host cache")
    p_web_imp.add_argument("--reextract", action="store_true", help="re-run the extractor on the cached imprint texts (no network)")
    _add_common(p_web_imp)

    p_serve = sub.add_parser("serve", help="serve the merged table (item 7): build / api / push")
    serve_sub = p_serve.add_subparsers(dest="serve_cmd", required=True)
    p_serve_build = serve_sub.add_parser("build", help="merged table → flat/full parquet, extracts, tiles, manifest")
    p_serve_build.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_serve_build.add_argument("--version", default=None, help="version label (default: merged_at)")
    p_serve_build.add_argument("--no-tiles", action="store_true", help="skip the PMTiles build")
    p_serve_build.add_argument("--force", action="store_true", help="rebuild an existing version")
    _add_common(p_serve_build)
    p_serve_dev = serve_sub.add_parser("dev", help="local map + JSON endpoints for a serve build (stdlib)")
    p_serve_dev.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_serve_dev.add_argument("--version", default=None, help="serve version dir (default: latest)")
    p_serve_dev.add_argument("--port", type=int, default=8765)
    _add_common(p_serve_dev)
    p_serve_api = serve_sub.add_parser("api", help="FastAPI + DuckDB API + map page (extra: serve)")
    p_serve_api.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_serve_api.add_argument("--version", default=None, help="serve version dir (default: latest)")
    p_serve_api.add_argument("--port", type=int, default=8791)
    p_serve_api.add_argument("--host", default="127.0.0.1")
    _add_common(p_serve_api)

    p_run = sub.add_parser("run", help="chain extract → classify → export")
    p_run.add_argument("--scope", default="bremen", help='state (name/code) or "DE"')
    p_run.add_argument("--sources", default="osm,ied,abwaerme,overture,mastr")
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
    if args.command == "register" and args.register_cmd == "build":
        from . import paths as _paths
        from .register import build as register_build
        written = register_build.build(_paths.data_root(args.data_dir),
                                       [x.strip() for x in args.sources.split(",") if x.strip()],
                                       force=args.force)
        for p in written:
            print(f"[out] {p}")
        return 0 if written else 1
    if args.command == "register" and args.register_cmd == "match":
        from . import config as _config
        from . import paths as _paths
        from .register import match as register_match
        register_match.run(_paths.data_root(args.data_dir), _config.scope_name(args.scope),
                           [x.strip() for x in args.sources.split(",") if x.strip()])
        return 0
    if args.command == "register" and args.register_cmd == "reconcile":
        from . import config as _config
        from . import paths as _paths
        from .register import reconcile as register_reconcile
        slugs = _config.resolve_states(args.scope if args.scope != "DE" else "all")
        register_reconcile.reconcile(_paths.data_root(args.data_dir), _config.scope_name(args.scope), slugs)
        return 0
    if args.command == "wikidata" and args.wd_cmd == "enrich":
        from . import config as _config
        from . import paths as _paths
        from .sources import wikidata as wikidata_source
        wikidata_source.enrich(_paths.data_root(args.data_dir), _config.scope_name(args.scope), fetch=not args.no_fetch)
        return 0
    if args.command == "web" and args.web_cmd == "discover":
        from . import config as _config
        from . import paths as _paths
        from .web import discover as web_discover
        web_discover.discover(_paths.data_root(args.data_dir), _config.scope_name(args.scope),
                              industrial_only=not args.all, limit=args.limit, dry_run=args.dry_run)
        return 0
    if args.command == "web" and args.web_cmd == "impressum":
        from . import config as _config
        from . import paths as _paths
        from .web import impressum as web_impressum
        web_impressum.run(_paths.data_root(args.data_dir), _config.scope_name(args.scope),
                          industrial_only=not args.all, limit=args.limit, workers=args.workers, refetch=args.refetch,
                          reextract=args.reextract)
        return 0
    if args.command == "web" and args.web_cmd == "hygiene":
        from .pipeline import run_web_hygiene
        return run_web_hygiene(scope=args.scope, data_dir=args.data_dir,
                               propagate=not args.no_propagate)
    if args.command == "serve" and args.serve_cmd == "build":
        from . import config as _config
        from . import paths as _paths
        from .serve import build as serve_build
        scope = _config.scope_name(args.scope)
        out = serve_build.build(_paths.data_root(args.data_dir), scope, version=args.version,
                                tiles=not args.no_tiles, force=args.force)
        print(f"[out] {out}")
        return 0
    if args.command == "serve" and args.serve_cmd == "dev":
        from . import config as _config
        from . import paths as _paths
        from .serve import dev as serve_dev
        scope = _config.scope_name(args.scope)
        return serve_dev.main(_paths.data_root(args.data_dir), scope, args.version, args.port)
    if args.command == "serve" and args.serve_cmd == "api":
        from . import config as _config
        from . import paths as _paths
        from .serve import api as serve_api
        scope = _config.scope_name(args.scope)
        return serve_api.main(_paths.data_root(args.data_dir), scope, args.version, args.port, args.host)
    if args.command == "classify":
        print("geoextract classify is Part C — not implemented yet (see GEOEXTRACT_SPEC.md).",
              file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
