"""Deterministic serialization and Ed25519 signing for audit manifests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_ALGORITHM = "Ed25519"


def deterministic_json(value: Any) -> bytes:
    """Serialize a manifest deterministically for hashing and signing."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def public_key_id(public_key: Ed25519PublicKey) -> str:
    """Return a stable key ID from the public key's DER subject public key info."""
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def ensure_signing_keypair(meta_dir: Path) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Load or generate a workspace signing keypair."""
    signing_dir = meta_dir / "signing"
    signing_dir.mkdir(parents=True, exist_ok=True)
    private_path = signing_dir / "private_key.pem"
    public_path = signing_dir / "public_key.pem"

    if private_path.is_file():
        private_key = serialization.load_pem_private_key(
            private_path.read_bytes(),
            password=None,
        )
        if not isinstance(private_key, Ed25519PrivateKey):
            raise RuntimeError(f"Signing key is not Ed25519: {private_path}")
    else:
        private_key = Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(private_bytes)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(private_path, stat.S_IRUSR | stat.S_IWUSR)

    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not public_path.exists():
        descriptor = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as file:
            file.write(public_bytes)
            file.flush()
            os.fsync(file.fileno())
    else:
        public_path.write_bytes(public_bytes)
    return private_key, public_key


def generate_signing_keypair(meta_dir: Path) -> tuple[Path, Path, str]:
    """Generate or load signing keys and return paths plus key ID."""
    _private_key, public_key = ensure_signing_keypair(meta_dir)
    signing_dir = meta_dir / "signing"
    return (
        signing_dir / "private_key.pem",
        signing_dir / "public_key.pem",
        public_key_id(public_key),
    )


def sign_manifest(
    manifest: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    key_id: str | None = None,
) -> dict[str, str]:
    """Sign canonical manifest bytes and return signature metadata."""
    signature = private_key.sign(deterministic_json(manifest))
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key_id or public_key_id(private_key.public_key()),
        "value": base64.b64encode(signature).decode("ascii"),
    }


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    """Load an Ed25519 PEM public key."""
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError(f"Public key is not Ed25519: {path}")
    return key


def extract_signed_manifest(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Extract the manifest and signature from a history record or signed envelope."""
    if "manifest" in record and "signature" in record:
        manifest = record["manifest"]
        signature = record["signature"]
    else:
        manifest = record.get("audit_manifest")
        signature = record.get("signature")
    if not isinstance(manifest, dict):
        raise TypeError("Manifest JSON must contain an audit_manifest or manifest object.")
    if not isinstance(signature, dict):
        raise TypeError("Manifest JSON must contain a signature object.")
    return manifest, signature


def verify_audit(
    record: dict[str, Any],
    public_key: Ed25519PublicKey,
    workbook_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify signature validity and separately compare the supplied workbook."""
    manifest, signature = extract_signed_manifest(record)
    algorithm = signature.get("algorithm")
    recorded_key_id = signature.get("key_id")
    trusted_key_id = public_key_id(public_key)
    valid_signature = False

    if algorithm == SIGNATURE_ALGORITHM:
        try:
            raw_signature = base64.b64decode(signature["value"], validate=True)
            public_key.verify(raw_signature, deterministic_json(manifest))
            valid_signature = recorded_key_id == trusted_key_id
        except (InvalidSignature, KeyError, ValueError):
            valid_signature = False

    workbook_match: bool | None = None
    workbook_sha256: str | None = None
    if workbook_path is not None:
        workbook_bytes = Path(workbook_path).read_bytes()
        workbook_sha256 = hashlib.sha256(workbook_bytes).hexdigest()
        workbook_match = workbook_sha256 == manifest.get("candidate_sha256")

    return {
        "signature_valid": valid_signature,
        "key_status": (
            "trusted"
            if valid_signature
            else "unknown"
            if recorded_key_id != trusted_key_id
            else "invalid"
        ),
        "recorded_key_id": recorded_key_id,
        "trusted_key_id": trusted_key_id,
        "workbook_match": workbook_match,
        "workbook_sha256": workbook_sha256,
        "recorded_workbook_sha256": manifest.get("candidate_sha256"),
        "audit_outcome": manifest.get("outcome"),
        "audit_id": manifest.get("audit_id"),
    }
