"""Unit tests: parsers, name selection, stable IDs, virtual node rules."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from location_builder import names as names_mod
from location_builder.model import AltName, CountryInfo, Unit
from location_builder.names import NameSelector, build_source_to_app_map, nfc, norm_key
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
        self.assertIn("洛杉磯", zh)
        self.assertNotIn("Lo̍k-chhám-kî", zh)

    def test_unicode_nfc(self):
        composed = "é"  # U+00E9
        decomposed = "e\u0301"
        self.assertEqual(nfc(decomposed), composed)
        self.assertEqual(norm_key(decomposed), norm_key(composed))


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
        # ADM2 under municipality (admin1=22 Beijing) -> level 3
        u = Unit(1, "Chaoyang", 0, 0, "A", "ADM2", "CN", admin1="22", admin2="1101")
        self.assertEqual(norm._assign_level(u), 3)
        # ADM2 under normal province -> level 2
        u2 = Unit(2, "Hefei", 0, 0, "A", "ADM2", "CN", admin1="01", admin2="3401")
        self.assertEqual(norm._assign_level(u2), 2)

    def test_jp_dedupe_level2(self):
        cfg = self._cfg("JP")
        self.assertTrue(cfg.dedupe_level2_by_name)

    def test_us_config(self):
        cfg = self._cfg("US")
        self.assertTrue(cfg.allow_missing_district)
        self.assertNotIn("ADM2", cfg.candidate_codes)  # county not materialized


if __name__ == "__main__":
    unittest.main(verbosity=2)
