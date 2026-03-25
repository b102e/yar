"""
Signature verification.

Verifies Ed25519 signatures produced by sign.py.
Accepts the public key as a hex string (from Identity.public_key_hex)
so verification can be performed by anyone who knows the public key —
no private key, no running agent required.
"""

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.exceptions import InvalidSignature


@dataclass
class VerifyResult:
    valid: bool
    error: str | None = None

    def __bool__(self) -> bool:
        return self.valid


def _load_public_key(public_key_hex: str) -> Ed25519PublicKey:
    raw = bytes.fromhex(public_key_hex)
    return Ed25519PublicKey.from_public_bytes(raw)


def _decode_signature(signature_b64url: str) -> bytes:
    # Re-add stripped padding
    padding = 4 - len(signature_b64url) % 4
    if padding != 4:
        signature_b64url += "=" * padding
    return base64.urlsafe_b64decode(signature_b64url)


def verify(public_key_hex: str, data: bytes, signature_b64url: str) -> VerifyResult:
    """
    Verify a signature produced by sign.sign().

    Args:
        public_key_hex:   agent's public key as hex string
        data:             the original bytes that were signed
        signature_b64url: base64url signature string from sign()

    Returns:
        VerifyResult with .valid bool and optional .error string
    """
    try:
        pub_key = _load_public_key(public_key_hex)
        sig_bytes = _decode_signature(signature_b64url)
        digest = hashlib.sha256(data).digest()
        pub_key.verify(sig_bytes, digest)
        return VerifyResult(valid=True)
    except InvalidSignature:
        return VerifyResult(valid=False, error="signature mismatch")
    except ValueError as e:
        return VerifyResult(valid=False, error=f"invalid key or signature format: {e}")
    except Exception as e:
        return VerifyResult(valid=False, error=f"unexpected error: {e}")


def verify_str(public_key_hex: str, text: str, signature_b64url: str) -> VerifyResult:
    """Convenience wrapper for UTF-8 strings."""
    return verify(public_key_hex, text.encode("utf-8"), signature_b64url)
