"""
Agent identity — Ed25519 keypair.

Generated exactly once at first run. Stored in ~/.agent/identity/.
The private key IS the agent. It never leaves the server, never logs,
never appears in output. The public key hex is the agent's name.
"""

import os
import stat
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
)

IDENTITY_DIR = Path(os.environ.get("AGENT_IDENTITY_DIR", "~/.agent/identity")).expanduser()
PRIVATE_KEY_PATH = IDENTITY_DIR / "private.key"
PUBLIC_KEY_PATH = IDENTITY_DIR / "public.key"
GENESIS_PATH = IDENTITY_DIR / "genesis.json"


class Identity:
    """
    Loaded once at agent startup. Holds the Ed25519 keypair.
    Pass this object wherever signing is needed — never extract the private key.
    """

    def __init__(self, private_key: Ed25519PrivateKey):
        self._private_key = private_key
        self._public_key: Ed25519PublicKey = private_key.public_key()

    @property
    def public_key_hex(self) -> str:
        raw = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return raw.hex()

    @property
    def public_key_bytes(self) -> bytes:
        return self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign_bytes(self, data: bytes) -> bytes:
        """Raw Ed25519 signature over data. Returns 64 bytes."""
        return self._private_key.sign(data)

    def is_dead(self) -> bool:
        """Returns True if the death protocol has been executed."""
        return (IDENTITY_DIR / "DEAD").exists()

    def __repr__(self) -> str:
        return f"<Identity pubkey={self.public_key_hex[:16]}...>"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    # Directory itself: owner rwx only
    os.chmod(IDENTITY_DIR, stat.S_IRWXU)


def _save_keypair(private_key: Ed25519PrivateKey) -> None:
    _ensure_dir()

    private_bytes = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    PRIVATE_KEY_PATH.write_bytes(private_bytes)
    os.chmod(PRIVATE_KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    PUBLIC_KEY_PATH.write_bytes(public_bytes)
    os.chmod(PUBLIC_KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _load_private_key() -> Ed25519PrivateKey:
    raw = PRIVATE_KEY_PATH.read_bytes()
    return Ed25519PrivateKey.from_private_bytes(raw)


def _write_genesis(identity: Identity) -> None:
    """Write genesis record on first birth. Idempotent."""
    if GENESIS_PATH.exists():
        return

    genesis = {
        "type": "genesis",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "public_key_hex": identity.public_key_hex,
    }
    GENESIS_PATH.write_text(json.dumps(genesis, indent=2))
    os.chmod(GENESIS_PATH, stat.S_IRUSR | stat.S_IWUSR)

    print(f"[identity] Genesis")
    print(f"           timestamp : {genesis['timestamp']}")
    print(f"           public_key : {genesis['public_key_hex']}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_or_create() -> Identity:
    """
    Load existing keypair or generate a new one.
    This is the only entry point — call it once at agent startup.
    """
    if PRIVATE_KEY_PATH.exists():
        private_key = _load_private_key()
        identity = Identity(private_key)
        return identity

    # First run — generate
    _ensure_dir()
    private_key = Ed25519PrivateKey.generate()
    _save_keypair(private_key)
    identity = Identity(private_key)
    _write_genesis(identity)
    return identity


# ---------------------------------------------------------------------------
# Permissions audit
# ---------------------------------------------------------------------------

def check_permissions() -> list[str]:
    """
    Audit filesystem permissions for identity files.
    Returns a list of warning strings; empty list means all correct.
    """
    warnings: list[str] = []

    checks = [
        (IDENTITY_DIR,      0o700, "directory"),
        (PRIVATE_KEY_PATH,  0o600, "private.key"),
        (PUBLIC_KEY_PATH,   0o600, "public.key"),
        (GENESIS_PATH,      0o600, "genesis.json"),
    ]

    for path, expected_mode, label in checks:
        if not path.exists():
            continue
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        if actual_mode != expected_mode:
            warnings.append(
                f"[Identity] ⚠️  {label}: permissions {oct(actual_mode)} (expected {oct(expected_mode)})"
            )

    return warnings


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    identity = load_or_create()
    print(identity.public_key_hex)
