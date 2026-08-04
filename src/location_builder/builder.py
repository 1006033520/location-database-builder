"""Per-country build orchestration: parse -> normalize -> names -> sqlite -> validate -> package."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import packager
from .config import CountryConfig, LanguagesConfig, PROJECT_ROOT, VersionsConfig
from .database import write_database
from .model import CountryInfo
from .names import NameSelector, build_source_to_app_map, norm_key
from .normalizer import Normalizer
from .parser import parse_alternate_names, parse_country_file, parse_country_info, parse_hierarchy
from .validator import (
    check_path,
    check_path_absent,
    check_forbidden_names_by_level,
    check_max_virtual_by_level,
    check_expected_feature_codes,
    coverage_report,
    validate_structure,
)

APP_LANGUAGE_ORDER = ["zh-Hans", "en", "ja", "local"]

GOLDEN_KEYS = (
    "requiredPaths",
    "forbiddenPaths",
    "maxVirtualByLevel",
    "forbiddenNamesByLevel",
    "expectedFeatureCodesByLevel",
)


def _now_iso() -> str:
    """FIX-012: honour SOURCE_DATE_EPOCH for reproducible builds."""
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    if sde:
        try:
            return datetime.fromtimestamp(int(sde), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError):
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_date(cache_dir: str, main_zip: str) -> str:
    """FIX-008: source date comes from the download sidecar (HTTP Last-Modified),
    never from local file mtime."""
    meta_path = os.path.join(cache_dir, os.path.basename(main_zip) + ".meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            lm = meta.get("last_modified")
            if lm:
                # HTTP date -> ISO date (best effort; keep prefix otherwise)
                try:
                    from email.utils import parsedate_to_datetime

                    return parsedate_to_datetime(lm).astimezone(timezone.utc).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    return lm[:10]
        except (json.JSONDecodeError, OSError):
            pass
    raise RuntimeError(f"no trusted source metadata for {main_zip} (run 'download' to populate cache sidecars)")


def build_country(
    country_code: str,
    cache_dir: str,
    build_dir: str,
    golden: dict | None = None,
    cfg: CountryConfig | None = None,
    versions: VersionsConfig | None = None,
) -> dict:
    """Build one country package. `golden` is a semantic spec dict (FIX-010),
    `cfg`/`versions` allow test injection (fixture e2e / version parameterization)."""
    cfg = cfg or CountryConfig.load(country_code)
    lang_cfg = LanguagesConfig.load()
    versions = versions or VersionsConfig.load()
    countries = parse_country_info(os.path.join(cache_dir, "countryInfo.txt"))
    if country_code not in countries:
        raise RuntimeError(f"countryInfo.txt has no entry for {country_code}")
    ci: CountryInfo = countries[country_code]

    main_zip = os.path.join(cache_dir, f"{country_code}.zip")
    alt_zip = os.path.join(cache_dir, f"alt_{country_code}.zip")
    hier_zip = os.path.join(cache_dir, "hierarchy.zip")

    # 1. stream units (keep candidates + PCLI for country root coords)
    keep_codes = cfg.candidate_codes | {"PCLI"}
    units = list(parse_country_file(main_zip, country_code, keep_codes))

    # 2. stream alternate names for our geoname ids
    wanted_ids = {u.geoname_id for u in units}
    altnames: dict[int, list] = {}
    for alt in parse_alternate_names(alt_zip):
        if alt.geoname_id in wanted_ids:
            altnames.setdefault(alt.geoname_id, []).append(alt)

    # 3. hierarchy edges
    hierarchy: dict[int, set[int]] = {}
    for parent, child in parse_hierarchy(hier_zip):
        if child in wanted_ids and parent in wanted_ids:
            hierarchy.setdefault(child, set()).add(parent)

    # 4. normalize
    norm = Normalizer(cfg, ci)
    result = norm.run(units, hierarchy)
    nodes = result.nodes

    # 5. names
    source_to_app = build_source_to_app_map(lang_cfg)
    selector = NameSelector(source_to_app, set(lang_cfg.excluded_tags), ci.first_language)
    name_rows: dict[int, list[tuple]] = {}
    for node in nodes:
        selected = selector.select(altnames.get(node.geoname_id, []), node.default_name)
        rows: list[tuple] = []
        for app_tag in APP_LANGUAGE_ORDER:
            if app_tag == "local":
                # FIX-004: local_entry is always preferred (see names.py)
                entry = selector.local_entry(selected, node.default_name)
                if entry:
                    rows.append(("local", entry["name"], norm_key(entry["name"]), 1, 0))
                continue
            for e in selected.get(app_tag, []):
                # FIX-009: normalized_name uses the unified norm_key() so
                # search keys are case/space/fullwidth-insensitive; the display
                # name keeps its original case.
                rows.append((app_tag, e["name"], norm_key(e["name"]), 1 if e["is_preferred"] else 0, 1 if e["is_short"] else 0))
        name_rows[node.id] = rows

    # 6. write db
    os.makedirs(build_dir, exist_ok=True)
    db_path = os.path.join(build_dir, f"{country_code}.sqlite")
    source_date = _source_date(cache_dir, main_zip)
    metadata = {
        "schema_version": versions.schema_version,
        "catalog_version": versions.catalog_version,
        "country_code": country_code,
        "built_at": _now_iso(),
        "source": "GeoNames",
        "source_date": source_date,
        "mapping_version": versions.mapping_version,
        "unit_count": str(len(nodes)),
        "name_count": str(sum(len(v) for v in name_rows.values())),
        "languages": ",".join(APP_LANGUAGE_ORDER),
    }
    write_database(db_path, nodes, name_rows, metadata)

    # 7. vacuum + analyze
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
    finally:
        conn.close()

    # 8. validate
    problems = validate_structure(db_path, country_code)
    coverage = coverage_report(db_path)
    golden_checks: list[dict] = []
    semantic_problems: list[str] = []
    if golden:
        # FIX-010: semantic spec — required/forbidden paths, virtual caps,
        # forbidden names per level, expected feature codes per level.
        for g in golden.get("requiredPaths", []):
            ok = check_path(db_path, g["path"], g.get("lang", "en"))
            golden_checks.append({"path": g["path"], "ok": ok, "required": True})
            if not ok:
                problems.append(f"required path missing: {' -> '.join(g['path'])}")
        for g in golden.get("forbiddenPaths", []):
            absent = check_path_absent(db_path, g["path"], g.get("lang", "en"))
            golden_checks.append({"path": g["path"], "ok": absent, "required": True, "kind": "forbidden"})
            if not absent:
                problems.append(f"forbidden path present: {' -> '.join(g['path'])}")
        mv = check_max_virtual_by_level(db_path, golden.get("maxVirtualByLevel", {}))
        if mv:
            problems.extend(mv)
            semantic_problems.extend(mv)
        fn = check_forbidden_names_by_level(db_path, golden.get("forbiddenNamesByLevel", {}))
        if fn:
            problems.extend(fn)
            semantic_problems.extend(fn)
        fc = check_expected_feature_codes(db_path, golden.get("expectedFeatureCodesByLevel", {}))
        if fc:
            problems.extend(fc)
            semantic_problems.extend(fc)

    # 9. package
    gz_path = os.path.join(build_dir, f"{country_code}.sqlite.gz")
    packager.gzip_file(db_path, gz_path)
    sha = packager.sha256_hex(gz_path)
    sha_db = packager.sha256_hex(db_path)

    report = {
        "country": country_code,
        "ok": not problems,
        "stats": result.stats,
        "problems": problems,
        "semantic_problems": semantic_problems,
        "coverage": coverage,
        "golden": golden_checks,
        "sizes": {
            "sqlite_bytes": os.path.getsize(db_path),
            "gz_bytes": os.path.getsize(gz_path),
        },
        "sha256": {"gz": sha, "sqlite": sha_db},
        "metadata": metadata,
    }
    with open(os.path.join(build_dir, f"{country_code}_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def build_countries_index(
    cache_dir: str,
    build_dir: str,
    country_codes: list[str],
    versions: VersionsConfig | None = None,
) -> dict:
    """Small global index db: countries + names (phase 1: the 3 supported countries).

    FIX-001: ALL alternate names for a country's geoname_id are collected and a
    single NameSelector.select() call produces the full zh-Hans/ja/en/local set.
    Missing language data raises instead of silently writing an incomplete index.
    """
    lang_cfg = LanguagesConfig.load()
    versions = versions or VersionsConfig.load()
    countries = parse_country_info(os.path.join(cache_dir, "countryInfo.txt"))
    source_to_app = build_source_to_app_map(lang_cfg)

    os.makedirs(build_dir, exist_ok=True)
    db_path = os.path.join(build_dir, "countries.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE countries (
          code TEXT PRIMARY KEY, geoname_id INTEGER, default_name TEXT NOT NULL,
          sort_priority INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE country_names (
          country_code TEXT NOT NULL, language_tag TEXT NOT NULL, name TEXT NOT NULL,
          is_preferred INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (country_code, language_tag, name)
        );
        CREATE INDEX idx_country_names_lang ON country_names(language_tag, name);
        """)
        name_rows: list[tuple] = []
        for cc in country_codes:
            ci = countries.get(cc)
            if ci is None:
                raise RuntimeError(f"countryInfo.txt has no entry for {cc}")
            conn.execute("INSERT INTO countries VALUES (?,?,?,?)", (cc, ci.geoname_id, ci.name, 0))
            selector = NameSelector(source_to_app, set(lang_cfg.excluded_tags), ci.first_language)
            alt_zip = os.path.join(cache_dir, f"alt_{cc}.zip")
            if not os.path.exists(alt_zip):
                raise FileNotFoundError(
                    f"alternate names for {cc} missing ({alt_zip}); refusing to build an incomplete index"
                )
            # FIX-001: collect ALL names for this country, then select once.
            all_alts = [
                alt for alt in parse_alternate_names(alt_zip) if alt.geoname_id == ci.geoname_id
            ]
            selected = selector.select(all_alts, ci.name)
            for app_tag in APP_LANGUAGE_ORDER:
                if app_tag == "local":
                    entry = selector.local_entry(selected, ci.name)
                    if entry:
                        name_rows.append((cc, "local", entry["name"], 1))
                    continue
                for e in selected.get(app_tag, []):
                    name_rows.append((cc, app_tag, e["name"], 1 if e["is_preferred"] else 0))
        conn.executemany("INSERT INTO country_names VALUES (?,?,?,?)", name_rows)
        conn.execute(
            "INSERT INTO metadata VALUES ('schema_version',?),('catalog_version',?),('mapping_version',?),('generated_at',?)",
            (versions.schema_version, versions.catalog_version, versions.mapping_version, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    gz_path = os.path.join(build_dir, "countries.sqlite.gz")
    packager.gzip_file(db_path, gz_path)
    result = {
        "countries": country_codes,
        "sqlite_bytes": os.path.getsize(db_path),
        "gz_bytes": os.path.getsize(gz_path),
        "sha256": {"gz": packager.sha256_hex(gz_path), "sqlite": packager.sha256_hex(db_path)},
    }
    with open(os.path.join(build_dir, "countries_report.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def load_golden() -> dict[str, dict]:
    path = os.path.join(PROJECT_ROOT, "tests", "fixtures", "golden.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
