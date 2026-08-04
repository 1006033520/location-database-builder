"""Downloader: fetch GeoNames source files into the cache directory with retries."""
from __future__ import annotations

import os
import time
import urllib.request

BASE_URL = "https://download.geonames.org/export/dump"
FILES = [
    "countryInfo.txt",
    "iso-languagecodes.txt",
    "hierarchy.zip",
]
COUNTRY_FILES = ["CN", "JP", "US"]
ALT_FILES = ["CN", "JP", "US"]


def download(url: str, dst: str, retries: int = 5) -> bool:
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return True
    tmp = dst + ".part"
    for attempt in range(1, retries + 1):
        try:
            print(f"downloading {os.path.basename(dst)} (attempt {attempt}) ...")
            req = urllib.request.Request(url, headers={"User-Agent": "location-builder/0.1"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dst)
            print(f"  ok ({os.path.getsize(dst)} bytes)")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  failed: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
    return False


def download_all(cache_dir: str) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    for f in FILES:
        download(f"{BASE_URL}/{f}", os.path.join(cache_dir, f))
    for cc in COUNTRY_FILES:
        download(f"{BASE_URL}/{cc}.zip", os.path.join(cache_dir, f"{cc}.zip"))
    for cc in ALT_FILES:
        download(f"{BASE_URL}/alternatenames/{cc}.zip", os.path.join(cache_dir, f"alt_{cc}.zip"))
