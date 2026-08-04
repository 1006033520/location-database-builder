"""CLI: location-builder <command> [options]."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .builder import build_countries_index, build_country, load_golden
from .config import PROJECT_ROOT, VersionsConfig
from .downloader import download_all
from . import manifest as manifest_mod

CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")
SUPPORTED = ["CN", "JP", "US"]


def _parse_country(value: str) -> list[str]:
    if value.upper() == "ALL":
        return SUPPORTED
    return [c.upper() for c in value.split(",")]


def _versions(args) -> VersionsConfig:
    v = VersionsConfig.load()
    if getattr(args, "catalog_version", None):
        v.catalog_version = args.catalog_version
    if getattr(args, "mapping_version", None):
        v.mapping_version = args.mapping_version
    return v


def cmd_download(args) -> int:
    # FIX-006: any failed download -> non-zero exit (CI must not paper over it).
    failed = download_all(CACHE_DIR, refresh=getattr(args, "refresh", False))
    if failed:
        print(f"download FAILED for: {', '.join(failed)}")
        return 1
    print("download complete")
    return 0


def cmd_build(args) -> int:
    countries = _parse_country(args.country)
    golden = load_golden()
    versions = _versions(args)
    rc = 0
    for cc in countries:
        print(f"\n=== building {cc} ===")
        try:
            report = build_country(cc, CACHE_DIR, BUILD_DIR, golden.get(cc), versions=versions)
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
        idx = build_countries_index(CACHE_DIR, BUILD_DIR, SUPPORTED, versions=versions)
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
        for g in golden.get(cc, {}).get("requiredPaths", []):
            ok = check_path(db, g["path"], g.get("lang", "en"))
            print(f"  golden[{'OK' if ok else 'FAIL'}] {' -> '.join(g['path'])}")
            if not ok:
                rc = 1
    return rc


def cmd_diff(args) -> int:
    print("diff: implemented in phase 2 (version comparison)")
    return 0


def _collect_reports(countries: list[str]) -> list[dict]:
    reports = []
    for cc in countries:
        p = os.path.join(BUILD_DIR, f"{cc}_report.json")
        if not os.path.exists(p):
            print(f"missing report for {cc}: {p}")
            sys.exit(1)
        with open(p, "r", encoding="utf-8") as f:
            reports.append(json.load(f))
    return reports


def cmd_manifest(args) -> int:
    """FIX-007: emit manifest.json (+ manifest.sig when signing)."""
    countries = _parse_country(args.country)
    reports = _collect_reports(countries)
    with open(os.path.join(BUILD_DIR, "countries_report.json"), "r", encoding="utf-8") as f:
        index_report = json.load(f)
    versions = _versions(args)
    source_date = max(r["metadata"]["source_date"] for r in reports)
    m = manifest_mod.build_manifest(BUILD_DIR, versions, reports, index_report, source_date)
    out = os.path.join(BUILD_DIR, "manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    print(f"manifest written: {out}")
    print(f"  catalog={m['catalogVersion']} schema={m['schemaVersion']} mapping={m['mappingVersion']} sourceDate={m['sourceDate']}")
    for a in m["attachments"]:
        print(f"  {a['name']}: {a['compressedSize']}B -> {a['size']}B sha256={a['sha256'][:16]}...")
    if args.sign or args.verify:
        key = manifest_mod.load_private_key(args.key_file)
        sig = manifest_mod.sign(m, key)
        sig_path = os.path.join(BUILD_DIR, "manifest.sig")
        with open(sig_path, "wb") as f:
            f.write(sig)
        print(f"signature written: {sig_path} ({len(sig)} bytes)")
        if args.verify:
            ok = manifest_mod.verify(m, sig, key.public_key())
            print(f"self-verify: {'OK' if ok else 'FAILED'}")
            return 0 if ok else 1
    return 0


def cmd_sign(args) -> int:
    """Sign an existing manifest.json -> manifest.sig."""
    m_path = os.path.join(BUILD_DIR, "manifest.json")
    if not os.path.exists(m_path):
        print(f"no manifest at {m_path}; run 'manifest' first")
        return 1
    with open(m_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    key = manifest_mod.load_private_key(args.key_file)
    sig = manifest_mod.sign(m, key)
    sig_path = os.path.join(BUILD_DIR, "manifest.sig")
    with open(sig_path, "wb") as f:
        f.write(sig)
    print(f"signature written: {sig_path} ({len(sig)} bytes)")
    return 0


def cmd_verify(args) -> int:
    """Verify manifest.sig against manifest.json with the public key."""
    m_path = os.path.join(BUILD_DIR, "manifest.json")
    s_path = os.path.join(BUILD_DIR, "manifest.sig")
    if not os.path.exists(m_path) or not os.path.exists(s_path):
        print(f"missing manifest.json or manifest.sig in {BUILD_DIR}")
        return 1
    with open(m_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    with open(s_path, "rb") as f:
        sig = f.read()
    key = manifest_mod.load_public_key(args.key_file)
    ok = manifest_mod.verify(m, sig, key)
    print(f"signature verify: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def cmd_genkey(args) -> int:
    manifest_mod.generate_keypair(args.pub, args.priv)
    print(f"keypair written: {args.priv} / {args.pub}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="location-builder", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    p_down = sub.add_parser("download", help="download GeoNames source data into cache/")
    p_down.add_argument("--refresh", action="store_true", help="FIX-008: re-check and re-download changed files")
    p_down.set_defaults(func=cmd_download)

    p_build = sub.add_parser("build", help="build country packages")
    p_build.add_argument("country", help="comma-separated country codes or ALL")
    p_build.add_argument("--index", action="store_true", help="also build countries.sqlite index")
    p_build.add_argument("--catalog-version", help="FIX-011: override catalog version")
    p_build.add_argument("--mapping-version", help="FIX-011: override mapping version")
    p_build.set_defaults(func=cmd_build)

    p_val = sub.add_parser("validate", help="validate existing builds")
    p_val.add_argument("country", help="comma-separated country codes or ALL")
    p_val.set_defaults(func=cmd_validate)

    sub.add_parser("diff", help="(phase 2) compare versions").set_defaults(func=cmd_diff)

    p_man = sub.add_parser("manifest", help="FIX-007: write manifest.json [+ sign/verify]")
    p_man.add_argument("country", nargs="?", default="ALL", help="comma-separated country codes or ALL")
    p_man.add_argument("--sign", action="store_true", help="also write manifest.sig")
    p_man.add_argument("--verify", action="store_true", help="self-verify after signing")
    p_man.add_argument("--key-file", help="PEM private key path (else SIGNING_KEY env)")
    p_man.add_argument("--catalog-version")
    p_man.add_argument("--mapping-version")
    p_man.set_defaults(func=cmd_manifest)

    p_sign = sub.add_parser("sign", help="FIX-007: sign existing manifest.json")
    p_sign.add_argument("--key-file", help="PEM private key path (else SIGNING_KEY env)")
    p_sign.set_defaults(func=cmd_sign)

    p_ver = sub.add_parser("verify", help="FIX-007: verify manifest.sig")
    p_ver.add_argument("--key-file", help="PEM public key path (else SIGNING_PUBKEY env)")
    p_ver.set_defaults(func=cmd_verify)

    p_key = sub.add_parser("genkey", help="FIX-007: generate an Ed25519 keypair")
    p_key.add_argument("--pub", default="signing_pub.pem")
    p_key.add_argument("--priv", default="signing_priv.pem")
    p_key.set_defaults(func=cmd_genkey)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
