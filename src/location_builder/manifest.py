"""FIX-007: catalog manifest + Ed25519 signature.

manifest.json describes every release attachment (name, compressed/uncompressed
size, SHA-256) plus catalog/schema/mapping versions, the trusted source date and
per-country node/name/level statistics. The manifest is signed with Ed25519;
the private key must only ever come from an environment variable or a GitHub
Actions secret (never from a repo file).
"""
from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def load_private_key(source: str | None = None) -> Ed25519PrivateKey:
    """Private key from SIGNING_KEY env var (PEM body) or --key-file path."""
    if source:
        with open(source, "rb") as f:
            data = f.read()
    else:
        data = os.environ.get("SIGNING_KEY", "").encode()
        if not data:
            raise RuntimeError("no signing key: set SIGNING_KEY env or pass --key-file")
    try:
        return serialization.load_pem_private_key(data, password=None)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"failed to load signing key: {e}") from e


def load_public_key(source: str | None = None) -> Ed25519PublicKey:
    if source:
        with open(source, "rb") as f:
            data = f.read()
    else:
        data = os.environ.get("SIGNING_PUBKEY", "").encode()
        if not data:
            raise RuntimeError("no public key: set SIGNING_PUBKEY env or pass --key-file")
    try:
        return serialization.load_pem_public_key(data)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"failed to load public key: {e}") from e


def generate_keypair(pub_path: str, priv_path: str) -> None:
    """Generate an Ed25519 keypair for release signing (test/ops helper)."""
    key = Ed25519PrivateKey.generate()
    with open(priv_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    with open(pub_path, "wb") as f:
        f.write(key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ))


def build_manifest(build_dir: str, versions, reports: list[dict], index_report: dict, source_date: str) -> dict:
    """Assemble the release manifest from build artifacts + per-country reports."""
    attachments = []
    for report in reports:
        cc = report["country"]
        gz = os.path.join(build_dir, f"{cc}.sqlite.gz")
        db = os.path.join(build_dir, f"{cc}.sqlite")
        attachments.append({
            "name": f"{cc}.sqlite.gz",
            "compressedSize": os.path.getsize(gz),
            "size": os.path.getsize(db),
            "sha256": report["sha256"]["gz"],
        })
    gz = os.path.join(build_dir, "countries.sqlite.gz")
    db = os.path.join(build_dir, "countries.sqlite")
    attachments.append({
        "name": "countries.sqlite.gz",
        "compressedSize": os.path.getsize(gz),
        "size": os.path.getsize(db),
        "sha256": index_report["sha256"]["gz"],
    })
    countries = {}
    for report in reports:
        countries[report["country"]] = {
            "units": report["stats"]["nodes"],
            "names": report["metadata"]["name_count"],
            "levels": sorted(int(k) for k in report["stats"]["by_level"] if int(k) > 0 or int(k) == 0),
        }
    return {
        "catalogVersion": versions.catalog_version,
        "schemaVersion": versions.schema_version,
        "mappingVersion": versions.mapping_version,
        "sourceDate": source_date,
        "attachments": attachments,
        "countries": countries,
    }


def sign(manifest: dict, key: Ed25519PrivateKey) -> bytes:
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return key.sign(data)


def verify(manifest: dict, signature: bytes, key: Ed25519PublicKey) -> bool:
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        key.verify(signature, data)
        return True
    except Exception:  # noqa: BLE001
        return False
