"""
Agent death protocol.

Death is not `rm -rf`. It is a cryptographic event.

Sequence:
  1. Write final "death" chain entry  — signed with the private key
  2. Generate death certificate       — JSON + human-readable TXT
  3. Zero the private key             — 32 zero bytes, irreversible
  4. Destroy personal memory          — overwrite with zeros, then delete
  5. Write DEAD flag                  — identity/DEAD contains death timestamp

After this function returns:
  - chain.jsonl has a final "death" entry (valid signature)
  - private.key contains 32 zero bytes (signing impossible)
  - personal memory files are overwritten with zeros and deleted
  - identity/DEAD flag file exists
  - ~/.agent/death_certificate.{json,txt} exist
  - write_entry() raises ValueError on any future call
  - verify_chain() still returns VALID (death entry is valid)

Memory destruction philosophy:
  Memory belongs to the relationship, not to the server.
  When the agent dies, the user's personal data dies with it.
  What remains: chain.jsonl (structure only), public.key, death certificate.
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


# Files and directories containing personal user data.
# chain.jsonl is NOT here — it contains only structural metadata.
# genesis.json and public.key are NOT here — needed for verification.
# Legacy paths kept for older installs.
_MEMORY_PATHS_LEGACY = [
    "~/claude-memory/",
]


def _secure_delete(path: Path) -> int:
    """
    Overwrite file with zeros then delete. For directories: recurse.
    Returns number of files destroyed.
    """
    if not path.exists():
        return 0

    if path.is_dir():
        count = 0
        for child in path.rglob("*"):
            if child.is_file():
                count += _secure_delete(child)
        shutil.rmtree(path, ignore_errors=True)
        return count

    # Overwrite with zeros
    try:
        size = path.stat().st_size
        with open(path, "wb") as f:
            f.write(bytes(size))
            f.flush()
            os.fsync(f.fileno())
        path.unlink()
        return 1
    except Exception as e:
        print(f"[Die] secure_delete warning: {path}: {e}")
        return 0


_DIVIDER = "═" * 39


def die(identity, reason: str = "", memory=None) -> Path:
    """
    Perform agent death. Returns path to death_certificate.json.

    Args:
        identity: loaded Identity instance (must not be dead yet)
        reason:   optional human-readable reason for death
        memory:   optional Memory instance — if provided, save() is called first

    Returns:
        Path to death_certificate.json

    Raises:
        ValueError: if identity.is_dead() — cannot die twice
    """
    if identity.is_dead():
        raise ValueError("Agent is already dead. Cannot die twice.")

    # All paths read at call time so test patches propagate correctly.
    import identity.keypair as _kp
    import chain.writer as _cw
    from chain.writer import write_entry
    from chain.reader import get_entry_count, get_last_entry

    private_key_path: Path = _kp.PRIVATE_KEY_PATH
    genesis_path: Path     = _kp.GENESIS_PATH
    identity_dir: Path     = _kp.IDENTITY_DIR
    agent_dir: Path        = _cw.AGENT_DIR

    # ── 0. Optional final memory save ────────────────────────────────────────
    if memory is not None:
        try:
            memory.save()
            print("[Die] memory saved")
        except Exception as e:
            print(f"[Die] memory save skipped: {e}")

    # ── 1. Final chain entry ──────────────────────────────────────────────────
    last = get_last_entry()
    count_before = get_entry_count()

    death_entry = write_entry(identity, {
        "event":         "death",
        "reason":        reason or "not specified",
        "public_key":    identity.public_key_hex,
        "total_entries": count_before + 1,          # includes this entry
        "last_hash":     last["prev_hash"] if last else "genesis",
    }, "death")

    death_ts = death_entry["timestamp"]
    total_entries = count_before + 1  # content is encrypted, use local value

    # ── 2. Death certificates ─────────────────────────────────────────────────
    genesis_ts = "unknown"
    if genesis_path.exists():
        try:
            gdata = json.loads(genesis_path.read_text(encoding="utf-8"))
            genesis_ts = gdata.get("timestamp", "unknown")
        except Exception:
            pass

    cert_data = {
        "agent_public_key":            identity.public_key_hex,
        "genesis":                     genesis_ts,
        "death":                       death_ts,
        "total_entries":               total_entries,
        "reason":                      reason or "not specified",
        "final_chain_entry_id":        death_entry["id"],
        "final_chain_entry_signature": death_entry["signature"],
        "memory_destroyed":            True,
        "memory_destroyed_at":         death_ts,
        "remaining":                   "chain.jsonl (structural metadata only), public.key, genesis.json",
        "verify_command":              "python -m chain.verifier",
    }

    cert_json = agent_dir / "death_certificate.json"
    cert_txt  = agent_dir / "death_certificate.txt"

    cert_json.write_text(
        json.dumps(cert_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    txt_lines = [
        _DIVIDER,
        "         AGENT DEATH CERTIFICATE",
        _DIVIDER,
        "",
        "This agent has performed its final act.",
        "",
        f"Public key       : {identity.public_key_hex}",
        f"Born             : {genesis_ts}",
        f"Died             : {death_ts}",
        f"Reason           : {reason or 'not specified'}",
        f"Entries          : {total_entries}",
        "",
        "Memory destroyed : YES",
        f"Destroyed at     : {death_ts}",
        "Remaining        : chain.jsonl (structural metadata only)",
        "",
        "The agent's signing key has been destroyed.",
        "Its history remains permanently verifiable.",
        "All personal memory has been securely deleted.",
        "",
        "To verify: python -m chain.cli verify",
        _DIVIDER,
    ]
    cert_txt.write_text("\n".join(txt_lines), encoding="utf-8")

    # ── 3. Zero the private key (irreversible) ────────────────────────────────
    private_key_path.write_bytes(bytes(32))

    # ── 4. Destroy personal memory ────────────────────────────────────────────
    print("[Die] Destroying personal memory...")
    destroyed_count = 0
    # Actual player memory: ~/.agent/players/
    players_dir = agent_dir / "players"
    destroyed_count += _secure_delete(players_dir)
    # Legacy paths
    for p_str in _MEMORY_PATHS_LEGACY:
        destroyed_count += _secure_delete(Path(p_str).expanduser())
    print(f"[Die] Personal memory destroyed ({destroyed_count} files).")

    # ── 5. DEAD flag ──────────────────────────────────────────────────────────
    dead_path = identity_dir / "DEAD"
    dead_path.write_text(death_ts, encoding="utf-8")

    print(f"[Die] ✝  Agent has performed its final act.")
    print(f"[Die]    Certificate : {cert_json}")
    print(f"[Die]    Chain length: {total_entries}")
    print(f"[Die]    Memory      : destroyed")

    return cert_json
