"""
Chain verifier — walk the full signed memory chain and validate every entry.

Verification checks two independent invariants for each entry:

  1. HASH CHAIN: entry["prev_hash"] == sha256(canonical(previous_entry))
     For the first entry: prev_hash must equal the literal string "genesis".
     Tampering any field of a stored entry changes its hash and breaks
     the link forward — the next entry's prev_hash will no longer match.

  2. SIGNATURE: verify(public_key, canonical(entry_without_sig), entry["signature"])
     The signing surface excludes the "signature" field itself.
     Tampering any other field (including prev_hash) invalidates the signature
     because the signing surface includes all other fields.

Both checks are evaluated for every entry even after failures so that a
single corrupt run reports ALL broken positions, not just the first.

Public key is loaded from ~/.agent/identity/genesis.json — the record
written at agent birth. Verification requires no private key and no
running agent: anyone with the genesis.json can verify the chain.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from identity.keypair import GENESIS_PATH
from identity.verify import verify as verify_sig
from chain.writer import _canonical, _sha256_hex, CHAIN_PATH
from chain.reader import read_entries


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class VerifyResult:
    valid: bool
    entry_count: int
    first_entry: dict | None
    last_entry: dict | None
    broken_at: int | None   # 1-based index of first broken entry, None if valid
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------

def _load_public_key_hex() -> str | None:
    """
    Load the agent's public key from genesis.json.
    Returns None if the file does not exist or is malformed.
    """
    if not GENESIS_PATH.exists():
        return None
    try:
        data = json.loads(GENESIS_PATH.read_text(encoding="utf-8"))
        return data["public_key_hex"]
    except (json.JSONDecodeError, KeyError):
        return None


def verify_chain() -> VerifyResult:
    """
    Walk the full chain and validate every entry.

    Checks performed for each entry (index is 1-based in all output):
      - prev_hash integrity
      - Ed25519 signature validity

    Continues past failures to collect all errors.
    broken_at is set to the lowest 1-based index that has any error.
    """
    # ── 0. Prerequisite: public key ────────────────────────────────────────
    if not GENESIS_PATH.exists():
        return VerifyResult(
            valid=False,
            entry_count=0,
            first_entry=None,
            last_entry=None,
            broken_at=None,
            errors=["identity/genesis.json not found — cannot verify signatures"],
        )

    pub_key_hex = _load_public_key_hex()
    if pub_key_hex is None:
        return VerifyResult(
            valid=False,
            entry_count=0,
            first_entry=None,
            last_entry=None,
            broken_at=None,
            errors=["identity/genesis.json is malformed — missing public_key_hex"],
        )

    # ── 1. Empty / missing chain ────────────────────────────────────────────
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        return VerifyResult(
            valid=True,
            entry_count=0,
            first_entry=None,
            last_entry=None,
            broken_at=None,
        )

    # ── 2. Walk entries ─────────────────────────────────────────────────────
    errors: list[str] = []
    broken_at: int | None = None
    first_entry: dict | None = None
    last_entry: dict | None = None
    prev_entry: dict | None = None
    count = 0

    def _record_error(idx: int, msg: str) -> None:
        nonlocal broken_at
        errors.append(msg)
        if broken_at is None:
            broken_at = idx

    try:
        for idx, entry in enumerate(read_entries(), start=1):
            count = idx
            if first_entry is None:
                first_entry = entry

            entry_id = entry.get("id", f"entry_{idx:04d}")

            # ── Check 1: prev_hash ──────────────────────────────────────────
            stored_prev_hash: str = entry.get("prev_hash", "")

            if prev_entry is None:
                # First entry must declare genesis
                if stored_prev_hash != "genesis":
                    _record_error(idx, f"{entry_id}: prev_hash should be 'genesis', got '{stored_prev_hash}'")
            else:
                expected_prev_hash = _sha256_hex(_canonical(prev_entry))
                if stored_prev_hash != expected_prev_hash:
                    _record_error(idx, f"{entry_id}: hash mismatch (chain broken before this entry)")

            # ── Check 2: signature ──────────────────────────────────────────
            signature: str = entry.get("signature", "")
            entry_without_sig = {k: v for k, v in entry.items() if k != "signature"}
            signing_bytes = _canonical(entry_without_sig)

            result = verify_sig(pub_key_hex, signing_bytes, signature)
            if not result.valid:
                _record_error(idx, f"{entry_id}: invalid signature ({result.error})")

            prev_entry = entry
            last_entry = entry

    except json.JSONDecodeError as exc:
        _record_error(count + 1, f"entry_{(count+1):04d}: malformed JSON at line {count+1} — {exc}")

    valid = len(errors) == 0
    return VerifyResult(
        valid=valid,
        entry_count=count,
        first_entry=first_entry,
        last_entry=last_entry,
        broken_at=broken_at,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Telegram-ready formatter
# ---------------------------------------------------------------------------

_DIVIDER = "─" * 25

def format_result(result: VerifyResult) -> str:
    """
    Format a VerifyResult for display in Telegram or a terminal.
    Returns a plain-text string with Unicode dividers.
    """
    if result.valid:
        genesis_ts = result.first_entry["timestamp"] if result.first_entry else "—"
        last_ts    = result.last_entry["timestamp"]  if result.last_entry  else "—"

        pub_key_hex = _load_public_key_hex() or "unknown"
        short_key = f"{pub_key_hex[:4]}...{pub_key_hex[-4:]}" if len(pub_key_hex) >= 8 else pub_key_hex

        lines = [
            "Chain integrity: ✓ VALID",
            _DIVIDER,
            f"Entries:    {result.entry_count}",
            f"Genesis:    {genesis_ts}",
            f"Last:       {last_ts}",
            f"Public key: {short_key}",
        ]
    else:
        lines = [
            "Chain integrity: ✗ BROKEN",
            _DIVIDER,
            f"Entries checked: {result.entry_count}",
        ]

        if result.broken_at is not None:
            lines.append(f"Broken at entry: {result.broken_at}")

        if result.errors:
            lines.append("Errors:")
            for err in result.errors:
                lines.append(f"  • {err}")

    return "\n".join(lines)
