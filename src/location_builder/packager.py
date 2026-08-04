"""Packaging: gzip + SHA-256."""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil


def gzip_file(src: str, dst: str, compresslevel: int = 9) -> None:
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=compresslevel) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)


def sha256_hex(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
