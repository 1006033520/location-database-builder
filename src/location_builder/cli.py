"""CLI: location-builder <command> [options]."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .builder import build_countries_index, build_country, load_golden
from .downloader import download_all

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")
SUPPORTED = ["CN", "JP", "US"]


def _parse_country(value: str) -> list[str]:
    if value.upper() == "ALL":
        return SUPPORTED
    return [c.upper() for c in value.split(",")]


def cmd_download(args) -> int:
    download_all(CACHE_DIR)
    print("download complete")
    return 0


def cmd_build(args) -> int:
    countries = _parse_country(args.country)
    golden = load_golden()
    rc = 0
    for cc in countries:
        print(f"\n=== building {cc} ===")
        try:
            report = build_country(cc, CACHE_DIR, BUILD_DIR, golden.get(cc))
        except Exception as e:  # noqa: BLE001
            print(f"BUILD FAILED for {cc}: {e}")
            rc = 1
            continue
        print(f"  units={report['stats']['nodes']} by_level={report['stats']['by_level']} virtual={report['stats']['virtual']}")
        print(f"  problems={report['problems']}")
        print(f"  sqlite={report['sizes']['sqlite_bytes']}B gz={report['sizes']['gz_bytes']}B")
        if not report["ok"]:
            rc = 1
        for g in report["golden"]:
            mark = "OK " if g["ok"] else ("MISS" if not g["required"] else "FAIL")
            print(f"  golden[{mark}] {' -> '.join(g['path'])}")
    if args.index:
        idx = build_countries_index(CACHE_DIR, BUILD_DIR, SUPPORTED)
        print(f"\ncountries index: {idx}")
    return rc


def cmd_validate(args) -> int:
    from .validator import check_path, coverage_report, validate_structure

    countries = _parse_country(args.country)
    golden = load_golden()
    rc = 0
    for cc in countries:
        db = os.path.join(BUILD_DIR, f"{cc}.sqlite")
        if not os.path.exists(db):
            print(f"{cc}: no database at {db}, run build first")
            rc = 1
            continue
        problems = validate_structure(db, cc)
        cov = coverage_report(db)
        print(f"\n=== validate {cc} ===")
        print(f"  structure problems: {len(problems)}")
        for p in problems:
            print(f"    - {p}")
            rc = 1
        for lang, info in cov["languages"].items():
            print(f"  coverage {lang}: {info['coverage_pct']}% ({info['covered']}/{cov['total_units']})")
        for g in golden.get(cc, []):
            ok = check_path(db, g["path"], g.get("lang", "en"))
            print(f"  golden[{'OK' if ok else 'FAIL'}] {' -> '.join(g['path'])}")
            if not ok and g.get("required", False):
                rc = 1
    return rc


def cmd_diff(args) -> int:
    print("diff: implemented in phase 2 (version comparison)")
    return 0


def cmd_manifest(args) -> int:
    print("manifest: implemented in phase 2 (Ed25519 signed catalog)")
    return 0


def cmd_publish(args) -> int:
    print("publish: implemented in phase 2 (GitHub Releases)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="location-builder", description="GeoNames location database builder")
    parser.add_argument("--version", action="version", version=f"location-builder {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download", help="download GeoNames source files into cache/")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("build", help="build country database(s)")
    p.add_argument("--country", default="ALL", help="country code(s) comma separated or ALL")
    p.add_argument("--index", action="store_true", help="also build countries.sqlite index")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("validate", help="validate built database(s)")
    p.add_argument("--country", default="ALL", help="country code(s) comma separated or ALL")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("diff", help="diff two data versions (phase 2)")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("manifest", help="generate signed release manifest (phase 2)")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("publish", help="publish to GitHub Releases (phase 2)")
    p.set_defaults(func=cmd_publish)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
