"""Unit tests: parsers, name selection, stable IDs, virtual node rules."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from location_builder import names as names_mod
from location_builder.model import AltName, CountryInfo, Unit
from location_builder.names import NameSelector, build_source_to_app_map, nfc, norm_key, to_simplified
from location_builder.normalizer import Normalizer, virtual_id, VIRTUAL_BIT
from location_builder.config import CountryConfig, LanguagesConfig


class TestParsers(unittest.TestCase):
    def test_zip_readme_skipped(self):
        from location_builder.parser import iter_zip_lines
        # real CN.zip first entry is readme.txt; parser must skip to data entry
        path = os.path.join(os.path.dirname(__file__), "..", "..", "cache", "CN.zip")
        if not os.path.exists(path):
            self.skipTest("cache not available")
        rows = list(iter_zip_lines(path))
        self.assertTrue(len(rows) > 100)
        self.assertEqual(len(rows[0]), 19)

    def test_country_info_parse(self):
        from location_builder.parser import parse_country_info
        path = os.path.join(os.path.dirname(__file__), "..", "..", "cache", "countryInfo.txt")
        if not os.path.exists(path):
            self.skipTest("cache not available")
        countries = parse_country_info(path)
        self.assertIn("CN", countries)
        self.assertIn("JP", countries)
        self.assertIn("US", countries)
        self.assertTrue(countries["CN"].geoname_id > 0)
        self.assertIn("zh-CN", countries["CN"].languages)


class TestNameSelection(unittest.TestCase):
    def setUp(self):
        lang_cfg = LanguagesConfig.load()
        self.sel = NameSelector(
            build_source_to_app_map(lang_cfg), set(lang_cfg.excluded_tags), "zh-CN"
        )

    def test_historic_and_link_excluded(self):
        alts = [
            AltName(1, "zh", "旧名", is_historic=True),
            AltName(1, "link", "https://en.wikipedia.org/wiki/X"),
            AltName(1, "wk", "Q12345"),
            AltName(1, "zh", "现代名"),
        ]
        result = self.sel.select(alts, "X")
        names = [e["name"] for e in result.get("zh-Hans", [])]
        self.assertIn("现代名", names)
        self.assertNotIn("旧名", names)  # historic excluded
        self.assertNotIn("https://en.wikipedia.org/wiki/X", names)
        self.assertNotIn("Q12345", names)

    def test_builder_designates_preferred(self):
        alts = [AltName(1, "zh", "北京"), AltName(1, "wuu", "北京")]
        result = self.sel.select(alts, "Beijing")
        zh = result["zh-Hans"]
        self.assertTrue(any(e["is_preferred"] for e in zh))
        # dedupe: 北京 appears once despite two source tags
        self.assertEqual(len([e for e in zh if e["name"] == "北京"]), 1)

    def test_non_han_noise_filtered(self):
        alts = [AltName(1, "nan", "Lo̍k-chhám-kî"), AltName(1, "yue", "洛杉磯")]
        result = self.sel.select(alts, "Los Angeles")
        zh = [e["name"] for e in result.get("zh-Hans", [])]
        self.assertIn("洛杉矶", zh)  # FIX-005: converted to simplified
        self.assertNotIn("Lo̍k-chhám-kî", zh)
        self.assertNotIn("洛杉磯", zh)

    def test_zh_source_priority_over_file_order(self):
        """FIX-005: yue appearing first with is_preferred must NOT beat zh-CN."""
        alts = [
            AltName(1, "yue", "臺灣", is_preferred=True),
            AltName(1, "zh-CN", "台湾"),
        ]
        result = self.sel.select(alts, "Taiwan")
        pref = next(e for e in result["zh-Hans"] if e["is_preferred"])
        self.assertEqual(pref["name"], "台湾")
        self.assertEqual(pref["source_tag"], "zh-CN")

    def test_zh_preferred_is_simplified(self):
        """FIX-005: traditional admin suffixes must never survive as preferred."""
        alts = [
            AltName(1, "yue", "淮安區"),
            AltName(1, "zh", "淮安区"),
            AltName(1, "zh-Hans", "澜沧拉祜族自治縣", is_preferred=True),
        ]
        result = self.sel.select(alts, "Huai'an")
        for e in result["zh-Hans"]:
            for bad in ("縣", "區", "臺"):
                self.assertNotIn(bad, e["name"])
        pref = next(e for e in result["zh-Hans"] if e["is_preferred"])
        self.assertEqual(pref["name"], "澜沧拉祜族自治县")

    def test_local_entry_always_preferred(self):
        """FIX-004: fallback local name must be preferred."""
        sel = NameSelector(build_source_to_app_map(LanguagesConfig.load()), set(), None)
        entry = sel.local_entry({}, "Some Place")
        self.assertTrue(entry["is_preferred"])
        # official-language path also preferred
        sel2 = NameSelector(build_source_to_app_map(LanguagesConfig.load()), set(), "zh-CN")
        sel2_result = sel2.select([AltName(1, "zh-CN", "合肥")], "Hefei")
        entry2 = sel2.local_entry(sel2_result, "Hefei")
        self.assertTrue(entry2["is_preferred"])
        self.assertEqual(entry2["name"], "合肥")

    def test_unicode_nfc(self):
        composed = "é"  # U+00E9
        decomposed = "e\u0301"
        self.assertEqual(nfc(decomposed), composed)
        self.assertEqual(norm_key(decomposed), norm_key(composed))

    def test_norm_key_case_space_fullwidth(self):
        """FIX-009: search keys insensitive to case/trim/fullwidth space."""
        self.assertEqual(norm_key(" New York "), norm_key("new york"))
        self.assertEqual(norm_key("NEW YORK"), norm_key("new york"))
        self.assertEqual(norm_key("東京都　新宿区"), norm_key("東京都 新宿区"))

    def test_to_simplified(self):
        self.assertEqual(to_simplified("臺灣臺北縣"), "台湾台北县")
        self.assertEqual(to_simplified("中国"), "中国")  # idempotent


class TestVirtualId(unittest.TestCase):
    def test_stable_and_unique(self):
        a = virtual_id("CN", 2, 100, 200)
        b = virtual_id("CN", 2, 100, 200)
        c = virtual_id("CN", 2, 101, 200)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a & VIRTUAL_BIT)  # high bit set -> never collides with real ids

    def test_never_collides_with_real(self):
        for gid in (1, 1000, 2**31 - 1):
            self.assertLess(gid, VIRTUAL_BIT)


class TestNormalizerRules(unittest.TestCase):
    def _cfg(self, cc):
        return CountryConfig.load(cc)

    def test_cn_municipality_assigns_district_level(self):
        cfg = self._cfg("CN")
        norm = Normalizer(cfg, CountryInfo("CN", "CHN", "China", "Beijing", 0, ["zh-CN"], 1814991))
        # ADM2 under municipality (admin1=22 Beijing) duplicates the province
        # level (北京市整体) and is dropped entirely.
        u = Unit(1, "Chaoyang", 0, 0, "A", "ADM2", "CN", admin1="22", admin2="1101")
        self.assertIsNone(norm._assign_level(u))
        # ADM2 under normal province -> level 2
        u2 = Unit(2, "Hefei", 0, 0, "A", "ADM2", "CN", admin1="01", admin2="3401")
        self.assertEqual(norm._assign_level(u2), 2)

    def test_jp_config_district_adm3_only(self):
        """FIX-002: PPLX must not be a JP district source anymore."""
        cfg = self._cfg("JP")
        self.assertTrue(cfg.dedupe_level2_by_name)
        self.assertNotIn("PPLX", cfg.district)
        self.assertIn("ADM3", cfg.district)
        self.assertFalse(cfg.fallback_for("district"))
        self.assertTrue(cfg.fallback_for("city"))

    def test_us_config_no_virtual_districts(self):
        """FIX-003: US per-city missing district allowed; no self fallback."""
        cfg = self._cfg("US")
        self.assertTrue(cfg.allow_missing_district)
        self.assertNotIn("ADM2", cfg.candidate_codes)  # county not materialized
        self.assertFalse(cfg.fallback_for("district"))
        self.assertFalse(cfg.fallback_for("city"))

    def test_fallback_for_dict_and_bool(self):
        cfg = CountryConfig(country="X", self_level_fallback={"city": True, "district": False})
        self.assertTrue(cfg.fallback_for("city"))
        self.assertFalse(cfg.fallback_for("district"))
        cfg2 = CountryConfig(country="X", self_level_fallback=True)
        self.assertTrue(cfg2.fallback_for("city"))
        self.assertTrue(cfg2.fallback_for("district"))


class TestVersions(unittest.TestCase):
    def test_versions_load(self):
        from location_builder.config import VersionsConfig
        v = VersionsConfig.load()
        self.assertTrue(v.catalog_version)
        self.assertTrue(v.mapping_version)
        self.assertEqual(v.schema_version, "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
