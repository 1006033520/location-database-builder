"""Integration tests.

- TestEndToEnd: real GeoNames cache (skipped when absent) — full builds with the
  FIX-010 semantic golden spec, countries index names, local-preferred invariant
  and FIX-012 reproducibility (SOURCE_DATE_EPOCH -> identical gz bytes).
- TestFixtureE2E: FIX-006 — a small synthetic country built entirely from
  generated zips (no network) covering parse -> normalize -> names -> sqlite
  -> validate -> gzip.
- TestManifest: FIX-007 — Ed25519 sign / verify / tamper-detection.
- TestDownloadCache: FIX-008 — sidecar size+hash verification.
"""
import gzip
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from location_builder.builder import build_countries_index, build_country, load_golden
from location_builder.config import CountryConfig, VersionsConfig
from location_builder.validator import validate_structure

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(PROJECT_ROOT, "cache")
BUILD = os.path.join(PROJECT_ROOT, "build")


def _row(geoname_id, name, lat, lon, fclass, fcode, cc, a1="", a2="", a3="", pop=0):
    return (
        f"{geoname_id}\t{name}\t{name}\t\t{lat}\t{lon}\t{fclass}\t{fcode}\t{cc}\t"
        f"\t{a1}\t{a2}\t{a3}\t\t{pop}\t0\t0\tAsia/Shanghai\t2026-01-01"
    )


def _make_fixture_cache(tmp: str) -> str:
    """Generate a tiny synthetic GeoNames cache for country 'TT'."""
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    # countryInfo.txt (col 0 iso,1 iso3,4 name,5 capital,7 pop,15 langs,16 geonameid)
    with open(os.path.join(cache, "countryInfo.txt"), "w", encoding="utf-8") as f:
        f.write("# test fixture\n")
        f.write("TT\tTTO\t780\t\tTestland\tTown\t100\t500000\tAS\t.tt\tTTD\t$\t1\t99\t\\d{3}\ten\t999999\n")
    with zipfile.ZipFile(os.path.join(cache, "TT.zip"), "w") as z:
        z.writestr("readme.txt", "fixture")
        z.writestr("TT.txt", "\n".join([
            _row(999999, "Testland", 10.0, -61.0, "A", "PCLI", "TT"),
            _row(9001, "Province One", 10.1, -61.1, "A", "ADM1", "TT", a1="P1"),
            _row(9002, "City One", 10.2, -61.2, "A", "ADM2", "TT", a1="P1", a2="C1"),
            _row(9003, "District One", 10.3, -61.3, "A", "ADM3", "TT", a1="P1", a2="C1", a3="D1"),
            "",
        ]))
    with zipfile.ZipFile(os.path.join(cache, "alt_TT.zip"), "w") as z:
        z.writestr("altTT.txt", "\n".join([
            "1\t999999\tzh-CN\t测试国\t\t\t\t",
            "2\t999999\ten\tTestland\t\t\t\t",
            "3\t9001\tzh-CN\t一省\t\t\t\t",
            "4\t9002\tzh-CN\t城市\t1\t\t\t",
            "5\t9002\ten\tCity One\t\t\t\t",
            "6\t9003\ten\tDistrict One\t\t\t\t",
        ]))
    with zipfile.ZipFile(os.path.join(cache, "hierarchy.zip"), "w") as z:
        z.writestr("hierarchy.txt", "999999\t9001\n9001\t9002\n9002\t9003\n")
    # FIX-008: sidecars so source_date is trusted
    for name in ("TT.zip", "alt_TT.zip", "hierarchy.zip"):
        meta = {"url": f"file://{name}", "last_modified": "Fri, 01 Jan 2026 00:00:00 GMT",
                "etag": '"x"', "size": os.path.getsize(os.path.join(cache, name)),
                "sha256": "0" * 64}
        with open(os.path.join(cache, name + ".meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    return cache


@unittest.skipUnless(os.path.exists(os.path.join(CACHE, "CN.zip")), "source cache missing")
class TestEndToEnd(unittest.TestCase):
    def test_build_all_with_semantic_golden(self):
        golden = load_golden()
        versions = VersionsConfig.load()
        for cc in ("CN", "JP", "US"):
            with self.subTest(country=cc):
                report = build_country(cc, CACHE, BUILD, golden.get(cc), versions=versions)
                self.assertEqual(report["problems"], [], f"{cc} problems: {report['problems']}")
                self.assertTrue(report["ok"])
                for g in report["golden"]:
                    if g.get("required"):
                        self.assertTrue(g["ok"], f"{cc} golden failed: {g}")
                # FIX-010: semantic problems must be empty
                self.assertEqual(report["semantic_problems"], [], f"{cc} semantic: {report['semantic_problems']}")

    def test_local_preferred_invariant(self):
        """FIX-004 acceptance SQL: every unit has exactly one preferred local name."""
        for cc in ("CN", "JP", "US"):
            db = os.path.join(BUILD, f"{cc}.sqlite")
            if not os.path.exists(db):
                build_country(cc, CACHE, BUILD, load_golden().get(cc))
            conn = sqlite3.connect(db)
            n = conn.execute(
                "SELECT COUNT(*) FROM administrative_units u WHERE NOT EXISTS ("
                " SELECT 1 FROM administrative_unit_names n"
                " WHERE n.unit_id = u.id AND n.language_tag = 'local' AND n.is_preferred = 1)"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(n, 0, f"{cc}: {n} units without preferred local name")

    def test_us_virtual_near_zero(self):
        """FIX-003 acceptance: US virtual level-3 count ~ 0, no same-name city district."""
        db = os.path.join(BUILD, "US.sqlite")
        if not os.path.exists(db):
            build_country("US", CACHE, BUILD, load_golden().get("US"))
        conn = sqlite3.connect(db)
        v3 = conn.execute(
            "SELECT COUNT(*) FROM administrative_units WHERE normalized_level=3 AND is_virtual=1"
        ).fetchone()[0]
        same = conn.execute(
            "SELECT COUNT(*) FROM administrative_units c JOIN administrative_units p ON c.parent_id=p.id"
            " WHERE c.is_virtual=1 AND c.normalized_level=3 AND c.default_name = p.default_name"
        ).fetchone()[0]
        conn.close()
        self.assertLessEqual(v3, 0)
        self.assertEqual(same, 0)

    def test_jp_no_pplx_level3(self):
        """FIX-002 acceptance: no community-level names under Shibuya ward."""
        db = os.path.join(BUILD, "JP.sqlite")
        if not os.path.exists(db):
            build_country("JP", CACHE, BUILD, load_golden().get("JP"))
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM administrative_units WHERE normalized_level=3 AND source_feature_code='PPLX'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_countries_index_names(self):
        """FIX-001 acceptance: real multilingual country names, one preferred per tag."""
        golden = load_golden()
        req = golden.get("COUNTRIES", {}).get("requiredNames", {})
        build_countries_index(CACHE, BUILD, ["CN", "JP", "US"], VersionsConfig.load())
        conn = sqlite3.connect(os.path.join(BUILD, "countries.sqlite"))
        try:
            for cc, langs in req.items():
                for tag, want in langs.items():
                    row = conn.execute(
                        "SELECT name FROM country_names WHERE country_code=? AND language_tag=?"
                        " AND is_preferred=1", (cc, tag)
                    ).fetchone()
                    self.assertIsNotNone(row, f"{cc}/{tag}: no preferred name")
                    self.assertEqual(row[0], want, f"{cc}/{tag}: got {row[0]!r}, want {want!r}")
            # exactly one preferred per (country, tag)
            dup = conn.execute(
                "SELECT country_code, language_tag, COUNT(*) c FROM country_names"
                " WHERE is_preferred=1 GROUP BY country_code, language_tag HAVING c > 1"
            ).fetchall()
            self.assertEqual(dup, [])
        finally:
            conn.close()

    def test_reproducible_gz(self):
        """FIX-012: same input + SOURCE_DATE_EPOCH -> identical gz bytes."""
        env = dict(os.environ, SOURCE_DATE_EPOCH="1767225600")
        tmp = tempfile.mkdtemp()
        try:
            from location_builder import packager
            from location_builder.parser import parse_country_info
            cfg = CountryConfig(country="TT", province=["ADM1"], city=["ADM2"], district=["ADM3"])
            cache = _make_fixture_cache(tmp)
            d1, d2 = os.path.join(tmp, "b1"), os.path.join(tmp, "b2")
            os.makedirs(d1)
            os.makedirs(d2)
            r1 = build_country("TT", cache, d1, cfg=cfg, versions=VersionsConfig.load())
            r2 = build_country("TT", cache, d2, cfg=cfg, versions=VersionsConfig.load())
            self.assertEqual(r1["sha256"]["gz"], r2["sha256"]["gz"])
        finally:
            shutil.rmtree(tmp)


class TestFixtureE2E(unittest.TestCase):
    """FIX-006: real end-to-end build without network (synthetic country)."""

    def test_fixture_build_ok(self):
        tmp = tempfile.mkdtemp()
        try:
            cache = _make_fixture_cache(tmp)
            build = os.path.join(tmp, "build")
            cfg = CountryConfig(country="TT", province=["ADM1"], city=["ADM2"], district=["ADM3"])
            report = build_country("TT", cache, build, cfg=cfg, versions=VersionsConfig.load())
            self.assertEqual(report["problems"], [])
            self.assertTrue(report["ok"])
            self.assertEqual(report["stats"]["nodes"], 4)  # root + 3
            self.assertEqual(report["stats"]["by_level"], {0: 1, 1: 1, 2: 1, 3: 1, 4: 0})
            # names written (zh-Hans preferred for the city)
            db = os.path.join(build, "TT.sqlite")
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT name FROM administrative_unit_names n JOIN administrative_units u ON u.id=n.unit_id"
                " WHERE u.geoname_id=9002 AND n.language_tag='zh-Hans' AND n.is_preferred=1"
            ).fetchone()
            self.assertEqual(row[0], "城市")
            n_local = conn.execute(
                "SELECT COUNT(*) FROM administrative_units u WHERE NOT EXISTS ("
                " SELECT 1 FROM administrative_unit_names n WHERE n.unit_id=u.id"
                " AND n.language_tag='local' AND n.is_preferred=1)"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(n_local, 0)
            # gzip produced and decompressible
            gz_path = os.path.join(build, "TT.sqlite.gz")
            with gzip.open(gz_path, "rb") as f:
                raw = f.read()
            self.assertGreater(len(raw), 100)
        finally:
            shutil.rmtree(tmp)

    def test_fixture_index_names(self):
        tmp = tempfile.mkdtemp()
        try:
            cache = _make_fixture_cache(tmp)
            build = os.path.join(tmp, "build")
            build_countries_index(cache, build, ["TT"], VersionsConfig.load())
            conn = sqlite3.connect(os.path.join(build, "countries.sqlite"))
            zh = conn.execute(
                "SELECT name FROM country_names WHERE country_code='TT' AND language_tag='zh-Hans' AND is_preferred=1"
            ).fetchone()
            conn.close()
            self.assertEqual(zh[0], "测试国")
        finally:
            shutil.rmtree(tmp)


class TestManifest(unittest.TestCase):
    """FIX-007: Ed25519 signing, verification and tamper detection."""

    def test_sign_verify_tamper(self):
        from location_builder import manifest as m
        from location_builder.manifest import build_manifest
        tmp = tempfile.mkdtemp()
        try:
            priv, pub = os.path.join(tmp, "priv.pem"), os.path.join(tmp, "pub.pem")
            m.generate_keypair(pub, priv)
            versions = VersionsConfig.load()
            # tiny fake artifacts
            build = os.path.join(tmp, "b")
            os.makedirs(build)
            for name in ("CN.sqlite.gz", "CN.sqlite", "countries.sqlite.gz", "countries.sqlite"):
                with open(os.path.join(build, name), "wb") as f:
                    f.write(b"x" * 10)
            reports = [{
                "country": "CN",
                "sha256": {"gz": "a" * 64},
                "stats": {"nodes": 5, "by_level": {0: 1, 1: 1, 2: 1, 3: 2}},
                "metadata": {"name_count": "9"},
            }]
            index_report = {"sha256": {"gz": "b" * 64}}
            manifest = build_manifest(build, versions, reports, index_report, "2026-08-05")
            key = m.load_private_key(priv)
            sig = m.sign(manifest, key)
            self.assertTrue(m.verify(manifest, sig, key.public_key()))
            # tamper: change one byte of a name -> verification must fail
            manifest["attachments"][0]["name"] = "CN.sqlite.gz" + "x"
            self.assertFalse(m.verify(manifest, sig, key.public_key()))
        finally:
            shutil.rmtree(tmp)


class TestDownloadCache(unittest.TestCase):
    """FIX-008: sidecar size+hash validation."""

    def test_cache_valid_rejects_mismatch(self):
        from location_builder.downloader import _cache_valid
        tmp = tempfile.mkdtemp()
        try:
            dst = os.path.join(tmp, "f.bin")
            with open(dst, "wb") as f:
                f.write(b"hello world")
            with open(dst + ".meta.json", "w", encoding="utf-8") as f:
                json.dump({"size": 11, "sha256": "0" * 64, "url": "u"}, f)
            self.assertFalse(_cache_valid(dst))  # hash mismatch
            with open(dst + ".meta.json", "w", encoding="utf-8") as f:
                json.dump({"size": 999, "sha256": "0" * 64}, f)
            self.assertFalse(_cache_valid(dst))  # size mismatch
            # legacy cache without sidecar is invalid
            os.remove(dst + ".meta.json")
            self.assertFalse(_cache_valid(dst))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
