"""Streaming parsers for GeoNames TSV files. Never loads whole files into memory."""
from __future__ import annotations

import zipfile
from collections.abc import Iterator

from .model import AltName, CountryInfo, Unit


def iter_zip_lines(zip_path: str) -> Iterator[list[str]]:
    """Yield tab-split rows from the data entry of a zip file, streaming.
    Skips readme.txt-style entries."""
    with zipfile.ZipFile(zip_path) as zf:
        name = _data_entry(zf)
        with zf.open(name) as f:
            for raw in f:
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                yield line.decode("utf-8", "replace").split("\t")


def _data_entry(zf: zipfile.ZipFile) -> str:
    for n in zf.namelist():
        if n.lower() not in ("readme.txt", "readme.md", "license.txt"):
            return n
    return zf.namelist()[0]


def parse_country_file(zip_path: str, country_code: str, keep_codes: set[str]) -> Iterator[Unit]:
    """Stream the per-country gazetteer zip, yielding only rows for the target country
    whose feature code is in keep_codes."""
    for p in iter_zip_lines(zip_path):
        if len(p) < 19:
            continue
        if p[8] != country_code:
            continue
        fcode = p[7]
        if fcode not in keep_codes:
            continue
        yield Unit(
            geoname_id=int(p[0]),
            name=p[1],
            latitude=_f(p[4]),
            longitude=_f(p[5]),
            feature_class=p[6],
            feature_code=fcode,
            country_code=p[8],
            admin1=p[10],
            admin2=p[11],
            admin3=p[12],
            admin4=p[13],
            population=_i(p[14]),
        )


def parse_alternate_names(zip_path: str) -> Iterator[AltName]:
    """Stream alternate names (V2 10-col or legacy 8-col format)."""
    for p in iter_zip_lines(zip_path):
        if len(p) < 4:
            continue
        yield AltName(
            geoname_id=int(p[1]),
            iso_language=p[2],
            name=p[3],
            is_preferred=(len(p) > 4 and p[4] == "1"),
            is_short=(len(p) > 5 and p[5] == "1"),
            is_colloquial=(len(p) > 6 and p[6] == "1"),
            is_historic=(len(p) > 7 and p[7] == "1"),
        )


def parse_hierarchy(zip_path: str) -> Iterator[tuple[int, int]]:
    """Stream hierarchy.zip: yield (parent_id, child_id)."""
    for p in iter_zip_lines(zip_path):
        if len(p) >= 2:
            try:
                yield int(p[0]), int(p[1])
            except ValueError:
                continue


def parse_country_info(txt_path: str) -> dict[str, CountryInfo]:
    """Parse countryInfo.txt (skip # comments). Keyed by ISO code."""
    out: dict[str, CountryInfo] = {}
    with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line.startswith("#"):
                continue
            p = line.rstrip("\r\n").split("\t")
            if len(p) < 17:
                continue
            iso = p[0].strip()
            langs = [x.strip() for x in p[15].split(",") if x.strip()]
            out[iso] = CountryInfo(
                iso=iso,
                iso3=p[1].strip(),
                name=p[4].strip(),
                capital=p[5].strip(),
                population=_i(p[7]),
                languages=langs,
                geoname_id=_i(p[16]),
            )
    return out


def _f(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _i(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0
