"""Packaging: gzip + SHA-256.

FIX-012: gzip output is byte-reproducible — the header timestamp is fixed
(SOURCE_DATE_EPOCH if set, else 0) and the stored filename is empty, so the
same input always yields the same compressed bytes.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil


def _gzip_mtime() -> int:
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    if sde:
        try:
            return int(sde)
        except ValueError:
            pass
    return 0


def gzip_file(src: str, dst: str, compresslevel: int = 9) -> None:
    mtime = _gzip_mtime()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        with gzip.GzipFile(
            filename="", fileobj=fout, mode="wb", compresslevel=compresslevel, mtime=mtime
        ) as gz:
            shutil.copyfileobj(fin, gz, length=1024 * 1024)


def sha256_hex(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
