"""Structural validation, language coverage, semantic golden checks."""
from __future__ import annotations

import sqlite3

REQUIRED_LEVELS = (0, 1, 2, 3)


def validate_structure(db_path: str, expected_country: str) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("PRAGMA integrity_check")
        if cur.fetchone()[0] != "ok":
            problems.append("integrity_check failed")

        cur = conn.execute("PRAGMA foreign_key_check")
        orphans_fk = cur.fetchall()
        if orphans_fk:
            problems.append(f"foreign_key_check: {len(orphans_fk)} violations")

        # country code consistent
        row = conn.execute("SELECT COUNT(*) FROM administrative_units WHERE country_code != ?", (expected_country,)).fetchone()
        if row[0] > 0:
            problems.append(f"{row[0]} units with mismatched country_code")

        # exactly one level 0 root
        n_root = conn.execute("SELECT COUNT(*) FROM administrative_units WHERE normalized_level = 0").fetchone()[0]
        if n_root != 1:
            problems.append(f"expected exactly 1 level-0 root, found {n_root}")

        # levels within range
        bad_level = conn.execute("SELECT COUNT(*) FROM administrative_units WHERE normalized_level NOT IN (0,1,2,3)").fetchone()[0]
        if bad_level:
            problems.append(f"{bad_level} units with out-of-range level")

        # all non-root have valid parent, parent level < child level
        bad_parent = conn.execute(
            "SELECT COUNT(*) FROM administrative_units c JOIN administrative_units p ON c.parent_id = p.id"
            " WHERE c.normalized_level > 0 AND p.normalized_level >= c.normalized_level"
        ).fetchone()[0]
        if bad_parent:
            problems.append(f"{bad_parent} units whose parent level >= own level")

        dangling = conn.execute(
            "SELECT COUNT(*) FROM administrative_units c WHERE c.normalized_level > 0"
            " AND c.parent_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM administrative_units p WHERE p.id = c.parent_id)"
        ).fetchone()[0]
        if dangling:
            problems.append(f"{dangling} units with missing parent row")

        # no empty default names
        empty = conn.execute("SELECT COUNT(*) FROM administrative_units WHERE default_name = '' OR default_name IS NULL").fetchone()[0]
        if empty:
            problems.append(f"{empty} units with empty default_name")

        # no duplicate preferred names per (unit, language)
        dup = conn.execute(
            "SELECT unit_id, language_tag, COUNT(*) c FROM administrative_unit_names"
            " WHERE is_preferred = 1 GROUP BY unit_id, language_tag HAVING c > 1"
        ).fetchall()
        if dup:
            problems.append(f"{len(dup)} (unit, language) pairs with duplicate preferred names")

        # FIX-004: every administrative unit must have exactly one local
        # preferred name (client fallback query depends on it).
        missing_local = conn.execute(
            "SELECT COUNT(*) FROM administrative_units u"
            " WHERE NOT EXISTS (SELECT 1 FROM administrative_unit_names n"
            "  WHERE n.unit_id = u.id AND n.language_tag = 'local' AND n.is_preferred = 1)"
        ).fetchone()[0]
        if missing_local:
            problems.append(f"{missing_local} units without a preferred local name")

        # reachability: every node reachable from root
        total = conn.execute("SELECT COUNT(*) FROM administrative_units").fetchone()[0]
        reach = conn.execute(
            "WITH RECURSIVE reach(id) AS ("
            "  SELECT id FROM administrative_units WHERE normalized_level = 0"
            "  UNION"
            "  SELECT u.id FROM administrative_units u JOIN reach r ON u.parent_id = r.id"
            ") SELECT COUNT(*) FROM reach"
        ).fetchone()[0]
        if total != reach:
            problems.append(f"orphan/cycle: {total} units but only {reach} reachable from root")
    finally:
        conn.close()
    return problems


# ---------------------------------------------------------------------------
# FIX-010 semantic checks
# ---------------------------------------------------------------------------

def check_path(db_path: str, path: list[str], lang: str) -> bool:
    """Golden check: walk the tree by name in the given language
    (falls back to default_name). Level 0 also matches the names table."""
    conn = sqlite3.connect(db_path)
    try:
        parent_id = None
        for i, want in enumerate(path):
            if parent_id is None:
                row = conn.execute(
                    "SELECT u.id FROM administrative_units u"
                    " WHERE u.normalized_level = 0 AND ("
                    "   EXISTS (SELECT 1 FROM administrative_unit_names n"
                    "           WHERE n.unit_id = u.id AND n.language_tag = ? AND n.name = ?)"
                    "   OR u.default_name = ?) LIMIT 1",
                    (lang, want, want),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT u.id FROM administrative_units u"
                    " WHERE u.parent_id = ? AND u.normalized_level = ? AND ("
                    "   EXISTS (SELECT 1 FROM administrative_unit_names n"
                    "           WHERE n.unit_id = u.id AND n.language_tag = ? AND n.name = ?)"
                    "   OR u.default_name = ?) LIMIT 1",
                    (parent_id, i, lang, want, want),
                ).fetchone()
            if row is None:
                return False
            parent_id = row[0]
        return True
    finally:
        conn.close()


def check_path_absent(db_path: str, path: list[str], lang: str) -> bool:
    """FIX-010: assert that a path does NOT exist (forbidden paths)."""
    return not check_path(db_path, path, lang)


def check_max_virtual_by_level(db_path: str, caps: dict) -> list[str]:
    """FIX-010: virtual node count per level must not exceed the cap."""
    problems: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        for level_str, cap in (caps or {}).items():
            level = int(level_str)
            n = conn.execute(
                "SELECT COUNT(*) FROM administrative_units WHERE normalized_level = ? AND is_virtual = 1",
                (level,),
            ).fetchone()[0]
            if n > int(cap):
                problems.append(f"virtual level {level} count {n} exceeds cap {cap}")
    finally:
        conn.close()
    return problems


def check_forbidden_names_by_level(db_path: str, forbidden: dict) -> list[str]:
    """FIX-010: no unit at a level may have a default_name in the forbidden list."""
    problems: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        for level_str, names in (forbidden or {}).items():
            level = int(level_str)
            if not names:
                continue
            qmarks = ",".join("?" * len(names))
            rows = conn.execute(
                f"SELECT default_name FROM administrative_units"
                f" WHERE normalized_level = ? AND default_name IN ({qmarks})",
                (level, *names),
            ).fetchall()
            for (name,) in rows:
                problems.append(f"forbidden name at level {level}: {name!r}")
    finally:
        conn.close()
    return problems


def check_expected_feature_codes(db_path: str, expected: dict) -> list[str]:
    """FIX-010: real (non-virtual) units at a level may only carry the expected
    GeoNames feature codes."""
    problems: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        for level_str, codes in (expected or {}).items():
            level = int(level_str)
            if not codes:
                continue
            qmarks = ",".join("?" * len(codes))
            rows = conn.execute(
                f"SELECT DISTINCT source_feature_code FROM administrative_units"
                f" WHERE normalized_level = ? AND is_virtual = 0 AND source_feature_code NOT IN ({qmarks})",
                (level, *codes),
            ).fetchall()
            for (code,) in rows:
                problems.append(f"unexpected feature code at level {level}: {code!r}")
    finally:
        conn.close()
    return problems


def coverage_report(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM administrative_units").fetchone()[0]
        langs = [r[0] for r in conn.execute(
            "SELECT DISTINCT language_tag FROM administrative_unit_names ORDER BY language_tag"
        )]
        out = {"total_units": total, "languages": {}}
        for lang in langs:
            covered = conn.execute(
                "SELECT COUNT(DISTINCT unit_id) FROM administrative_unit_names WHERE language_tag = ? AND is_preferred = 1",
                (lang,),
            ).fetchone()[0]
            out["languages"][lang] = {
                "covered": covered,
                "coverage_pct": round(covered * 100.0 / total, 2) if total else 0.0,
            }
        return out
    finally:
        conn.close()
