"""
Signing utilities.

All signatures are Ed25519 over the SHA-256 digest of the input data.
Returned as base64url (no padding) strings — safe for JSON storage.
"""

import base64
import hashlib

from .keypair import Identity


def _digest(data: bytes) -> bytes:
    """SHA-256 of data. We sign the digest, not raw data."""
    return hashlib.sha256(data).digest()


def sign(identity: Identity, data: bytes) -> str:
    """
    Sign data with the agent's private key.

    Args:
        identity: loaded Identity instance
        data:     arbitrary bytes to sign

    Returns:
        base64url string (no padding) of the 64-byte Ed25519 signature
        over sha256(data).
    """
    digest = _digest(data)
    raw_sig = identity.sign_bytes(digest)
    return base64.urlsafe_b64encode(raw_sig).rstrip(b"=").decode()


def sign_str(identity: Identity, text: str) -> str:
    """Convenience wrapper for UTF-8 strings."""
    return sign(identity, text.encode("utf-8"))
