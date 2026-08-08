"""Configuration loading (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class CountryConfig:
    country: str
    province: list[str] = field(default_factory=list)
    city: list[str] = field(default_factory=list)
    district: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    municipalities: list[str] = field(default_factory=list)
    allow_missing_district: bool = False
    # bool True  -> fallback at every level (legacy behavior)
    # bool False -> no self fallback at any level
    # dict       -> per-level switch: {"city": bool, "district": bool}
    self_level_fallback: bool | dict = True
    dedupe_level2_by_name: bool = False
    # "admin2"     -> dedupe by (admin1, admin2) only (JP: same city)
    # "admin2_name" -> dedupe by (admin1, admin2, stripped name) (US)
    dedupe_level2_key: str = "admin2_name"
    # CN: drop level-3 ADM3 twins that duplicate a level-2 entry of the same
    # province (GeoNames double records with broken admin2).
    dedupe_cross_level: bool = False
    parent_resolution: list[str] = field(default_factory=lambda: ["hierarchy", "admin_code"])

    @property
    def candidate_codes(self) -> set[str]:
        return set(self.province) | set(self.city) | set(self.district)

    def fallback_for(self, level: str) -> bool:
        """FIX-003: per-target-level self fallback control.
        level is 'city' (level 2) or 'district' (level 3)."""
        if isinstance(self.self_level_fallback, dict):
            return bool(self.self_level_fallback.get(level, False))
        return bool(self.self_level_fallback)

    @classmethod
    def load(cls, country_code: str) -> "CountryConfig":
        path = os.path.join(PROJECT_ROOT, "config", "countries", f"{country_code}.yaml")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Missing country config: {path}")
        return cls(**{k: v for k, v in _load(path).items() if k in cls.__dataclass_fields__})


@dataclass
class LanguagesConfig:
    languages: dict[str, list[str]] = field(default_factory=dict)
    excluded_tags: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "LanguagesConfig":
        return cls(**_load(os.path.join(PROJECT_ROOT, "config", "languages.yaml")))


@dataclass
class VersionsConfig:
    """FIX-011: version information is injected from config / CLI / release tag,
    never hardcoded in builder code."""

    catalog_version: str = "0.0.0-dev"
    mapping_version: str = "0.0.0-dev"
    schema_version: str = "1"

    @classmethod
    def load(cls) -> "VersionsConfig":
        path = os.path.join(PROJECT_ROOT, "config", "versions.yaml")
        if not os.path.exists(path):
            return cls()
        return cls(**{k: v for k, v in _load(path).items() if k in cls.__dataclass_fields__})


@dataclass
class ThresholdsConfig:
    max_record_drop_pct: float = 10.0
    min_level1_nodes: int = 1
    min_level2_nodes: int = 1
    coverage_warn_drop_pct: float = 5.0

    @classmethod
    def load(cls) -> "ThresholdsConfig":
        path = os.path.join(PROJECT_ROOT, "config", "thresholds.yaml")
        return cls(**{k: v for k, v in _load(path).items() if k in cls.__dataclass_fields__})
