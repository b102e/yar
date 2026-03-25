"""
Chain writer — append-only signed memory chain.

Every significant agent event becomes a chain entry:
  - signed with the agent's Ed25519 private key
  - chained via sha256 of the previous raw entry line

Chain file: ~/.agent/chain.jsonl  (one JSON object per line)

Invariants:
  - Entries are append-only. Never rewrite or delete.
  - Each entry's prev_hash = sha256(raw bytes of previous line as stored).
  - Signing surface = sha256(deterministic JSON of entry without 'signature').
  - Stored lines are deterministically serialized (sort_keys, no spaces).
  - Breaking any of these makes chain/verifier.py report corruption.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from identity.keypair import Identity
from identity.sign import sign

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

AGENT_DIR = Path(os.environ.get("AGENT_DIR", "~/.agent")).expanduser()
CHAIN_PATH = AGENT_DIR / "chain.jsonl"

# ---------------------------------------------------------------------------
# Module-level lock — protects writes within a single process.
# Cross-process safety is handled via fcntl.flock below.
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _canonical(obj: dict) -> bytes:
    """
    Deterministic JSON serialization used for both signing and hashing.
    sort_keys ensures field order is independent of insertion order.
    separators removes all optional whitespace.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _next_id(last_entry: dict | None) -> str:
    """Derive the next sequential entry ID."""
    if last_entry is None:
        return "entry_0001"
    last_id: str = last_entry["id"]  # e.g. "entry_0042"
    n = int(last_id.split("_")[1]) + 1
    # Preserve at least 4-digit padding; grows naturally beyond 9999
    width = max(4, len(str(n)))
    return f"entry_{n:0{width}d}"


def _acquire_flock(fh) -> None:
    """Best-effort cross-process exclusive lock (Unix only)."""
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass  # Not available on Windows; threading.Lock is the fallback


def _release_flock(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


# ---------------------------------------------------------------------------
# Core write logic (called under _write_lock)
# ---------------------------------------------------------------------------

def _write_locked(identity: Identity, content: dict, entry_type: str) -> dict:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    CHAIN_PATH.touch(exist_ok=True)

    # Import here to avoid circular import at module load time
    from chain.reader import get_last_entry

    with open(CHAIN_PATH, "a+b") as fh:
        _acquire_flock(fh)
        try:
            # Re-read last entry under the lock so concurrent writers are safe
            last_entry = get_last_entry()

            prev_hash: str
            if last_entry is None:
                prev_hash = "genesis"
            else:
                # Hash the raw line as it was stored (without trailing newline)
                raw_prev_line = _canonical(last_entry)
                prev_hash = _sha256_hex(raw_prev_line)

            entry_id = _next_id(last_entry)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Build entry without signature first — this is the signing surface
            entry_without_sig: dict[str, Any] = {
                "id": entry_id,
                "timestamp": timestamp,
                "type": entry_type,
                "content": content,
                "prev_hash": prev_hash,
            }

            # Sign the deterministic serialization of the unsigned entry
            signing_bytes = _canonical(entry_without_sig)
            signature = sign(identity, signing_bytes)

            # Complete entry — canonical serialization is the stored line
            full_entry = {**entry_without_sig, "signature": signature}
            line = _canonical(full_entry) + b"\n"

            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

        finally:
            _release_flock(fh)

    return full_entry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_entry(identity: Identity, content: dict, entry_type: str) -> dict:
    """
    Append a signed, chained entry to ~/.agent/chain.jsonl.

    Args:
        identity:   loaded Identity instance (holds the private key)
        content:    arbitrary dict — the semantic payload of this entry
        entry_type: one of: fact, episode, consolidation, session, death, genesis

    Returns:
        The written entry dict (including id, signature, prev_hash).

    Raises:
        ValueError: if identity.is_dead() — a dead agent cannot write
    """
    if identity.is_dead():
        raise ValueError("Agent is dead. Chain is sealed. No new entries can be written.")

    with _write_lock:
        return _write_locked(identity, content, entry_type)
