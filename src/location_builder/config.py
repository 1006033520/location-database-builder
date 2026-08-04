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
    self_level_fallback: bool = True
    dedupe_level2_by_name: bool = False
    parent_resolution: list[str] = field(default_factory=lambda: ["hierarchy", "admin_code"])

    @property
    def candidate_codes(self) -> set[str]:
        return set(self.province) | set(self.city) | set(self.district)

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
class ThresholdsConfig:
    max_record_drop_pct: float = 10.0
    min_level1_nodes: int = 1
    min_level2_nodes: int = 1
    coverage_warn_drop_pct: float = 5.0

    @classmethod
    def load(cls) -> "ThresholdsConfig":
        path = os.path.join(PROJECT_ROOT, "config", "thresholds.yaml")
        return cls(**{k: v for k, v in _load(path).items() if k in cls.__dataclass_fields__})
