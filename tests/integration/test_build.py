"""Integration test: end-to-end build of the 3 phase-1 countries against the
cached source data, then structural validation + golden samples."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from location_builder.builder import build_country, load_golden
from location_builder.validator import validate_structure

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(PROJECT_ROOT, "cache")
BUILD = os.path.join(PROJECT_ROOT, "build")


@unittest.skipUnless(os.path.exists(os.path.join(CACHE, "CN.zip")), "source cache missing")
class TestEndToEnd(unittest.TestCase):
    def test_build_validate_golden_cn(self):
        golden = load_golden().get("CN", [])
        report = build_country("CN", CACHE, BUILD, golden)
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ok"])
        for g in report["golden"]:
            if g.get("required"):
                self.assertTrue(g["ok"], f"required golden failed: {g['path']}")
        # stable ids: rebuild yields identical unit ids
        ids1 = self._unit_ids()
        report2 = build_country("CN", CACHE, BUILD, golden)
        ids2 = self._unit_ids()
        self.assertEqual(ids1, ids2)

    def _unit_ids(self):
        import sqlite3
        conn = sqlite3.connect(os.path.join(BUILD, "CN.sqlite"))
        ids = [r[0] for r in conn.execute("SELECT id FROM administrative_units ORDER BY id")]
        conn.close()
        return ids


if __name__ == "__main__":
    unittest.main(verbosity=2)
