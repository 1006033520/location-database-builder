"""SQLite database writer (schema from schema/location-v1.sql)."""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable

from .normalizer import Node

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "schema", "location-v1.sql")


def _load_schema() -> str:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_database(db_path: str, nodes: Iterable[Node], names: dict[int, list[tuple]], metadata: dict[str, str]) -> None:
    """names: geoname_id -> [(language_tag, name, normalized_name, is_preferred, is_short)]"""
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_load_schema())

        unit_rows = []
        for n in nodes:
            unit_rows.append((
                n.id, n.geoname_id, n.parent_id, n.level, n.country_code,
                n.admin_code, n.source_feature_code, n.source_admin_level,
                n.default_name, n.latitude, n.longitude, n.population,
                n.is_virtual, n.sort_priority,
            ))
        # parents always have a strictly lower level than children: insert level-ascending
        # so the FK (parent_id -> id) never references a not-yet-inserted row.
        unit_rows.sort(key=lambda r: r[3])

        conn.executemany(
            "INSERT INTO administrative_units (id, geoname_id, parent_id, normalized_level, country_code,"
            " admin_code, source_feature_code, source_admin_level, default_name, latitude, longitude,"
            " population, is_virtual, sort_priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            unit_rows,
        )
        conn.commit()

        name_rows = []
        for gid, rows in names.items():
            for (lang, name, norm, pref, short) in rows:
                name_rows.append((gid, lang, name, norm, pref, short))
        if name_rows:
            conn.executemany(
                "INSERT INTO administrative_unit_names (unit_id, language_tag, name, normalized_name,"
                " is_preferred, is_short) VALUES (?,?,?,?,?,?)",
                name_rows,
            )
        conn.commit()

        conn.executemany(
            "INSERT INTO metadata (key, value) VALUES (?,?)",
            list(metadata.items()),
        )
        conn.commit()
    finally:
        conn.close()
