"""
Memory encryption using agent's Ed25519 identity key.

Symmetric key is derived from the private key via SHA-256 — same identity,
two uses: signing (Ed25519) and encryption (XSalsa20-Poly1305 via NaCl).

The derived key never appears in logs or chain entries.
"""

import hashlib
import json
from pathlib import Path

import nacl.secret
import nacl.utils


def derive_key(identity) -> bytes:
    """Derive 32-byte symmetric encryption key from Ed25519 private key."""
    raw = identity._private_key.private_bytes_raw()
    return hashlib.sha256(b"yar-memory-encryption-v1" + raw).digest()


def encrypt_json(identity, data: dict) -> bytes:
    """Encrypt a dict to bytes (nonce + ciphertext)."""
    key = derive_key(identity)
    box = nacl.secret.SecretBox(key)
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return bytes(box.encrypt(plaintext))


def decrypt_json(identity, ciphertext: bytes) -> dict:
    """Decrypt bytes to dict."""
    key = derive_key(identity)
    box = nacl.secret.SecretBox(key)
    plaintext = box.decrypt(ciphertext)
    return json.loads(plaintext.decode("utf-8"))


def encrypt_file(identity, path: Path, data: dict) -> None:
    """Write encrypted JSON to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt_json(identity, data))


def decrypt_file(identity, path: Path, default=None) -> dict:
    """
    Read and decrypt JSON from file.

    Migration: if file starts with '{' or '[' it is legacy plaintext —
    decrypt it in-memory and re-encrypt immediately so next read is fast.
    """
    if default is None:
        default = {}
    if not path.exists():
        return default

    raw = path.read_bytes()

    # Legacy plaintext migration
    if raw[:1] in (b"{", b"["):
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return default
        # Re-encrypt in place
        try:
            encrypt_file(identity, path, data)
        except Exception:
            pass
        return data

    # Encrypted
    try:
        return decrypt_json(identity, raw)
    except Exception:
        return default
