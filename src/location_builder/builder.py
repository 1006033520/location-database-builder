"""Per-country build orchestration: parse -> normalize -> names -> sqlite -> validate -> package."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import packager
from .config import CountryConfig, LanguagesConfig, PROJECT_ROOT
from .database import write_database
from .model import CountryInfo
from .names import NameSelector, build_source_to_app_map, nfc
from .normalizer import Normalizer
from .parser import parse_alternate_names, parse_country_file, parse_country_info, parse_hierarchy
from .validator import check_path, coverage_report, validate_structure

APP_LANGUAGE_ORDER = ["zh-Hans", "en", "ja", "local"]


def build_country(country_code: str, cache_dir: str, build_dir: str, golden: list[dict] | None = None) -> dict:
    cfg = CountryConfig.load(country_code)
    lang_cfg = LanguagesConfig.load()
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
                entry = selector.local_entry(selected, node.default_name)
                if entry:
                    rows.append(("local", entry["name"], nfc(entry["name"]), 1 if entry["is_preferred"] else 0, 1 if entry["is_short"] else 0))
                continue
            for e in selected.get(app_tag, []):
                rows.append((app_tag, e["name"], nfc(e["name"]), 1 if e["is_preferred"] else 0, 1 if e["is_short"] else 0))
        name_rows[node.id] = rows

    # 6. write db
    os.makedirs(build_dir, exist_ok=True)
    db_path = os.path.join(build_dir, f"{country_code}.sqlite")
    source_date = _file_date(main_zip)
    metadata = {
        "schema_version": "1",
        "catalog_version": "2026.08.0",
        "country_code": country_code,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "GeoNames",
        "source_date": source_date,
        "mapping_version": "1.0.0",
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
    golden_checks = []
    if golden:
        for g in golden:
            ok = check_path(db_path, g["path"], g.get("lang", "en"))
            golden_checks.append({"path": g["path"], "ok": ok, "required": g.get("required", False)})

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


def build_countries_index(cache_dir: str, build_dir: str, country_codes: list[str]) -> dict:
    """Small global index db: countries + names (phase 1: the 3 supported countries)."""
    lang_cfg = LanguagesConfig.load()
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
                continue
            conn.execute("INSERT INTO countries VALUES (?,?,?,?)", (cc, ci.geoname_id, ci.name, 0))
            selector = NameSelector(source_to_app, set(lang_cfg.excluded_tags), ci.first_language)
            alt_zip = os.path.join(cache_dir, f"alt_{cc}.zip")
            selected = {}
            if os.path.exists(alt_zip):
                for alt in parse_alternate_names(alt_zip):
                    if alt.geoname_id == ci.geoname_id:
                        selected = selector.select([alt], ci.name)
                        break
            for app_tag in APP_LANGUAGE_ORDER:
                if app_tag == "local":
                    entry = selector.local_entry(selected, ci.name)
                    if entry:
                        name_rows.append((cc, "local", entry["name"], 1 if entry["is_preferred"] else 0))
                    continue
                for e in selected.get(app_tag, []):
                    name_rows.append((cc, app_tag, e["name"], 1 if e["is_preferred"] else 0))
        conn.executemany("INSERT INTO country_names VALUES (?,?,?,?)", name_rows)
        conn.execute(
            "INSERT INTO metadata VALUES ('schema_version','1'),('catalog_version','2026.08.0'),('generated_at',?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.commit()
    finally:
        conn.close()
    gz_path = os.path.join(build_dir, "countries.sqlite.gz")
    packager.gzip_file(db_path, gz_path)
    return {"countries": country_codes, "sqlite_bytes": os.path.getsize(db_path), "gz_bytes": os.path.getsize(gz_path)}


def load_golden() -> dict[str, list[dict]]:
    path = os.path.join(PROJECT_ROOT, "tests", "fixtures", "golden.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _file_date(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        return "unknown"
