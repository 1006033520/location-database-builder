"""CLI: location-builder <command> [options]."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import __version__, packager
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
    """FIX-007: emit manifest.json (+ manifest.sig when signing).

    Version consistency: catalog/schema/mapping versions are taken from the
    per-country build reports (i.e. exactly what was written into the SQLite
    metadata), never re-read from config — the manifest and the databases
    cannot diverge. An explicit --catalog-version that disagrees with the
    built databases is an error, not a silent override.
    """
    countries = _parse_country(args.country)
    reports = _collect_reports(countries)
    with open(os.path.join(BUILD_DIR, "countries_report.json"), "r", encoding="utf-8") as f:
        index_report = json.load(f)
    versions = VersionsConfig(
        catalog_version=reports[0]["metadata"]["catalog_version"],
        mapping_version=reports[0]["metadata"]["mapping_version"],
        schema_version=reports[0]["metadata"]["schema_version"],
    )
    for r in reports[1:]:
        md = r["metadata"]
        if (md["catalog_version"], md["mapping_version"], md["schema_version"]) != (
            versions.catalog_version,
            versions.mapping_version,
            versions.schema_version,
        ):
            print("ERROR: inconsistent versions across country reports")
            for rr in reports:
                print(f"  {rr['country']}: catalog={rr['metadata']['catalog_version']} "
                      f"mapping={rr['metadata']['mapping_version']} schema={rr['metadata']['schema_version']}")
            return 1
    if getattr(args, "catalog_version", None) and args.catalog_version != versions.catalog_version:
        print(f"ERROR: --catalog-version {args.catalog_version} != built catalog {versions.catalog_version}")
        return 1
    source_date = max(r["metadata"]["source_date"] for r in reports)
    m = manifest_mod.build_manifest(BUILD_DIR, versions, reports, index_report, source_date)
    out = os.path.join(BUILD_DIR, "manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    print(f"manifest written: {out}")
    print(f"  catalog={m['catalogVersion']} schema={m['schemaVersion']} mapping={m['mappingVersion']} sourceDate={m['sourceDate']}")
    for a in m["attachments"]:
        print(f"  {a['name']}: {a['compressedSize']}B -> {a['size']}B sha256={a['sha256'][:16]}...")
    for cc, info in m["countries"].items():
        print(f"  {cc}: units={info['units']} names={info['names']} levels={info['levels']}")
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


def cmd_verify_release(args) -> int:
    """Release-level consistency gate (run in CI before publishing):

    1. every manifest attachment hash matches the built file;
    2. manifest.sig verifies with the committed public key;
    3. every country SQLite (decompressed) carries the SAME catalog/schema/
       mapping versions as the manifest;
    4. manifest per-country levels match the actual non-empty levels in each
       SQLite.
    """
    import gzip
    import tempfile

    m_path = os.path.join(BUILD_DIR, "manifest.json")
    if not os.path.exists(m_path):
        print(f"no manifest at {m_path}; run 'manifest' first")
        return 1
    with open(m_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    rc = 0

    def fail(msg: str) -> None:
        nonlocal rc
        print(f"  FAIL: {msg}")
        rc = 1

    # 1. attachment hashes
    print("verify-release: attachment hashes")
    for a in m["attachments"]:
        p = os.path.join(BUILD_DIR, a["name"])
        if not os.path.exists(p):
            fail(f"missing attachment {a['name']}")
            continue
        h = packager.sha256_hex(p)
        if h != a["sha256"]:
            fail(f"hash mismatch {a['name']}: manifest {a['sha256'][:16]}... file {h[:16]}...")
        else:
            print(f"  OK {a['name']}")

    # 2. signature
    s_path = os.path.join(BUILD_DIR, "manifest.sig")
    if os.path.exists(s_path):
        with open(s_path, "rb") as f:
            sig = f.read()
        pub = os.path.join(PROJECT_ROOT, "signing_pub.pem")
        if os.path.exists(pub) and manifest_mod.verify(m, sig, manifest_mod.load_public_key(pub)):
            print("  OK manifest.sig (Ed25519, committed public key)")
        else:
            fail("manifest.sig does not verify")

    # 3+4. per-country SQLite metadata + levels
    print("verify-release: SQLite metadata vs manifest")
    with tempfile.TemporaryDirectory() as tmp:
        for cc, info in m["countries"].items():
            gz = os.path.join(BUILD_DIR, f"{cc}.sqlite.gz")
            db = os.path.join(tmp, f"{cc}.sqlite")
            with gzip.open(gz, "rb") as fin, open(db, "wb") as fout:
                import shutil

                shutil.copyfileobj(fin, fout)
            conn = sqlite3.connect(db)
            meta = dict(conn.execute("SELECT key, value FROM metadata"))
            levels = [r[0] for r in conn.execute(
                "SELECT normalized_level FROM administrative_units GROUP BY normalized_level"
            )]
            conn.close()
            if meta.get("catalog_version") != m["catalogVersion"]:
                fail(f"{cc} catalog_version {meta.get('catalog_version')!r} != manifest {m['catalogVersion']!r}")
            if meta.get("schema_version") != m["schemaVersion"]:
                fail(f"{cc} schema_version mismatch")
            if meta.get("mapping_version") != m["mappingVersion"]:
                fail(f"{cc} mapping_version mismatch")
            if sorted(levels) != info["levels"]:
                fail(f"{cc} levels {sorted(levels)} != manifest {info['levels']}")
            print(f"  OK {cc}: catalog={meta.get('catalog_version')} levels={sorted(levels)}")
    return rc


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

    p_rel = sub.add_parser("verify-release", help="release gate: manifest vs built SQLite consistency")
    p_rel.set_defaults(func=cmd_verify_release)

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
