"""Downloader: fetch GeoNames source files into the cache directory with retries.

FIX-008:
- every download records HTTP Last-Modified / ETag / URL / size / local SHA-256
  into a sidecar `<name>.meta.json`;
- cache hits verify size + SHA-256 (not just non-empty);
- ZIP sources get an integrity check;
- `source_date` is derived from HTTP metadata, never from local file mtime;
- `--refresh` forces a re-check against the server;
- `download_all` returns the list of failed files; CLI exits non-zero on any.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
import zipfile

BASE_URL = "https://download.geonames.org/export/dump"
FILES = [
    "countryInfo.txt",
    "iso-languagecodes.txt",
    "hierarchy.zip",
]
COUNTRY_FILES = ["CN", "JP", "US"]
ALT_FILES = ["CN", "JP", "US"]


def sha256_hex(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def meta_path(dst: str) -> str:
    return dst + ".meta.json"


def _verify_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except zipfile.BadZipFile:
        return False


def _write_meta(dst: str, url: str, headers, size: int) -> None:
    meta = {
        "url": url,
        "last_modified": headers.get("Last-Modified"),
        "etag": headers.get("ETag"),
        "size": size,
        "sha256": sha256_hex(dst),
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(meta_path(dst), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _cache_valid(dst: str) -> bool:
    """FIX-008: cached file is usable only when sidecar exists AND size+hash match.
    Legacy caches without a sidecar are treated as invalid (re-download once so
    provenance metadata exists)."""
    if not os.path.exists(dst) or os.path.getsize(dst) <= 0:
        return False
    mp = meta_path(dst)
    if not os.path.exists(mp):
        return False
    try:
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if meta.get("size") != os.path.getsize(dst):
        return False
    if meta.get("sha256") and sha256_hex(dst) != meta["sha256"]:
        return False
    if dst.endswith(".zip") and not _verify_zip(dst):
        return False
    return True


def download(url: str, dst: str, retries: int = 5, refresh: bool = False) -> bool:
    if not refresh and _cache_valid(dst):
        return True

    tmp = dst + ".part"
    for attempt in range(1, retries + 1):
        try:
            print(f"downloading {os.path.basename(dst)} (attempt {attempt}) ...")
            req = urllib.request.Request(url, headers={"User-Agent": "location-builder/0.1"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
            if dst.endswith(".zip") and not _verify_zip(tmp):
                raise ValueError("downloaded file is not a valid zip")
            os.replace(tmp, dst)
            _write_meta(dst, url, resp.headers, os.path.getsize(dst))
            print(f"  ok ({os.path.getsize(dst)} bytes)")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  failed: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt < retries:
                time.sleep(3 * attempt)
    return False


def download_all(cache_dir: str, refresh: bool = False) -> list[str]:
    """Download everything. Returns list of failed file names (empty = success)."""
    os.makedirs(cache_dir, exist_ok=True)
    failed: list[str] = []
    for f in FILES:
        if not download(f"{BASE_URL}/{f}", os.path.join(cache_dir, f), refresh=refresh):
            failed.append(f)
    for cc in COUNTRY_FILES:
        if not download(f"{BASE_URL}/{cc}.zip", os.path.join(cache_dir, f"{cc}.zip"), refresh=refresh):
            failed.append(f"{cc}.zip")
    for cc in ALT_FILES:
        if not download(
            f"{BASE_URL}/alternatenames/{cc}.zip",
            os.path.join(cache_dir, f"alt_{cc}.zip"),
            refresh=refresh,
        ):
            failed.append(f"alt_{cc}.zip")
    return failed
