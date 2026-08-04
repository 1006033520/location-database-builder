"""Core data models."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Unit:
    """A single GeoNames gazetteer record (main file row)."""

    geoname_id: int
    name: str
    latitude: float | None
    longitude: float | None
    feature_class: str
    feature_code: str
    country_code: str
    admin1: str = ""
    admin2: str = ""
    admin3: str = ""
    admin4: str = ""
    population: int = 0


@dataclass
class AltName:
    """A single alternate name row (alternateNames format)."""

    geoname_id: int
    iso_language: str
    name: str
    is_preferred: bool = False
    is_short: bool = False
    is_colloquial: bool = False
    is_historic: bool = False


@dataclass
class CountryInfo:
    """Parsed countryInfo.txt row."""

    iso: str
    iso3: str
    name: str
    capital: str
    population: int
    languages: list[str] = field(default_factory=list)
    geoname_id: int = 0

    @property
    def first_language(self) -> str | None:
        return self.languages[0] if self.languages else None
