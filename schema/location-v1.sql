-- SQLite schema for per-country location databases (phase 1).
CREATE TABLE metadata (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE administrative_units (
 id INTEGER PRIMARY KEY,
 geoname_id INTEGER,
 parent_id INTEGER,
 normalized_level INTEGER NOT NULL CHECK (normalized_level BETWEEN 0 AND 3),
 country_code TEXT NOT NULL,
 admin_code TEXT,
 source_feature_code TEXT NOT NULL,
 source_admin_level INTEGER,
 default_name TEXT NOT NULL,
 latitude REAL,
 longitude REAL,
 population INTEGER,
 is_virtual INTEGER NOT NULL DEFAULT 0 CHECK (is_virtual IN (0, 1)),
 sort_priority INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY (parent_id) REFERENCES administrative_units(id)
);

CREATE TABLE administrative_unit_names (
 unit_id INTEGER NOT NULL,
 language_tag TEXT NOT NULL,
 name TEXT NOT NULL,
 normalized_name TEXT NOT NULL,
 is_preferred INTEGER NOT NULL DEFAULT 0 CHECK (is_preferred IN (0, 1)),
 is_short INTEGER NOT NULL DEFAULT 0 CHECK (is_short IN (0, 1)),
 PRIMARY KEY (unit_id, language_tag, name),
 FOREIGN KEY (unit_id) REFERENCES administrative_units(id)
);

CREATE INDEX idx_units_parent_level
 ON administrative_units(parent_id, normalized_level);

CREATE INDEX idx_units_country_level
 ON administrative_units(country_code, normalized_level);

CREATE INDEX idx_units_geoname
 ON administrative_units(geoname_id);

CREATE INDEX idx_names_language_normalized
 ON administrative_unit_names(language_tag, normalized_name);
