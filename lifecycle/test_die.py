"""
Death protocol tests.

Run from the project root:
    python -m lifecycle.test_die

SAFETY: uses fully isolated temp dirs.
The real ~/.agent/identity/private.key is NEVER touched.

Test matrix:
  1. die() writes final death entry to chain
  2. Chain ends with type="death"
  3. private.key is 32 zero bytes after death
  4. DEAD flag file exists with timestamp
  5. death_certificate.json and .txt exist with correct fields
  6. verify_chain() returns VALID after death
  7. write_entry() raises ValueError after death
  8. identity.is_dead() returns True after death (read-only mode)
  9. die() raises ValueError on second call
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Isolated temp environment — patch ALL relevant module attributes
# ---------------------------------------------------------------------------

_tmp_id    = tempfile.mkdtemp(prefix="yar_die_test_id_")
_tmp_chain = tempfile.mkdtemp(prefix="yar_die_test_chain_")

import identity.keypair as _kp
import chain.writer as _cw
import chain.reader as _cr
import chain.verifier as _cv

# Override identity paths
_kp.IDENTITY_DIR      = Path(_tmp_id)
_kp.PRIVATE_KEY_PATH  = Path(_tmp_id) / "private.key"
_kp.PUBLIC_KEY_PATH   = Path(_tmp_id) / "public.key"
_kp.GENESIS_PATH      = Path(_tmp_id) / "genesis.json"

# Override chain paths
_tmp_chain_file = Path(_tmp_chain) / "chain.jsonl"
_cw.CHAIN_PATH  = _tmp_chain_file
_cw.AGENT_DIR   = Path(_tmp_chain)
_cr.CHAIN_PATH  = _tmp_chain_file
_cr.AGENT_DIR   = Path(_tmp_chain)
_cv.CHAIN_PATH  = _tmp_chain_file
# Verifier uses its own GENESIS_PATH binding — patch it to the temp identity dir
_cv.GENESIS_PATH = Path(_tmp_id) / "genesis.json"

# Sanity guard
assert _kp.PRIVATE_KEY_PATH != Path("~/.agent/identity/private.key").expanduser(), \
    "SAFETY: real private key path leaked into test!"

# Now import the function under test and create a fresh identity
from identity.keypair import load_or_create
from chain.writer import write_entry
from chain.reader import get_entry_count, get_last_entry, read_entries
from chain.verifier import verify_chain, format_result
from lifecycle.die import die


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(f"Test failed: {label}")


def _fresh_env() -> object:
    """Create fresh chain + identity for one test run. Returns identity."""
    # Clear chain
    _tmp_chain_file.unlink(missing_ok=True)
    # Clear identity dir and recreate
    shutil.rmtree(_tmp_id, ignore_errors=True)
    Path(_tmp_id).mkdir(parents=True, exist_ok=True)
    return load_or_create()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_death_protocol():
    print("\nDeath protocol: full sequence")

    identity = _fresh_env()

    # Write some entries to simulate a lived session
    write_entry(identity, {"fact": "preferred dark mode"}, "fact")
    write_entry(identity, {"event": "session_save", "message_count": 5}, "session")
    write_entry(identity, {"fact": "working on crypto agent"}, "fact")

    count_before = get_entry_count()
    _check(count_before == 3, f"3 entries before death (got {count_before})")

    # ── Execute death ────────────────────────────────────────────────────────
    cert_path = die(identity, reason="test death")

    # ── Test 1: chain ends with death entry ──────────────────────────────────
    entries = list(read_entries())
    _check(len(entries) == 4, f"Test 1: 4 entries after death (got {len(entries)})")

    # ── Test 2: last entry type is "death" ───────────────────────────────────
    last = entries[-1]
    _check(last["type"] == "death", f"Test 2: last entry type='death' (got '{last['type']}')")
    _check(last["content"]["reason"] == "test death", "Test 2: reason recorded in death entry")
    _check(last["content"]["total_entries"] == 4, "Test 2: total_entries=4 in death content")
    _check(last["content"]["public_key"] == identity.public_key_hex, "Test 2: pubkey in death entry")

    # ── Test 3: private key is 32 zero bytes ─────────────────────────────────
    key_bytes = _kp.PRIVATE_KEY_PATH.read_bytes()
    _check(len(key_bytes) == 32,         "Test 3: private.key is 32 bytes")
    _check(key_bytes == bytes(32),       "Test 3: private.key is all zeros")

    # ── Test 4: DEAD flag file exists with timestamp ──────────────────────────
    dead_path = _kp.IDENTITY_DIR / "DEAD"
    _check(dead_path.exists(),           "Test 4: DEAD flag exists")
    dead_ts = dead_path.read_text(encoding="utf-8").strip()
    _check(len(dead_ts) > 0,             f"Test 4: DEAD flag contains timestamp: '{dead_ts}'")

    # ── Test 5: death certificates exist with correct content ─────────────────
    cert_json_path = cert_path
    cert_txt_path  = cert_path.with_suffix(".txt")

    _check(cert_json_path.exists(), "Test 5: death_certificate.json exists")
    _check(cert_txt_path.exists(),  "Test 5: death_certificate.txt exists")

    cert = json.loads(cert_json_path.read_text(encoding="utf-8"))
    _check(cert["agent_public_key"]  == identity.public_key_hex, "Test 5: pubkey in cert")
    _check(cert["total_entries"]     == 4,                        "Test 5: total_entries=4 in cert")
    _check(cert["reason"]            == "test death",             "Test 5: reason in cert")
    _check(cert["final_chain_entry_id"] == last["id"],            "Test 5: final_chain_entry_id matches")
    _check(cert["final_chain_entry_signature"] == last["signature"], "Test 5: cert sig matches chain entry")
    _check(cert["verify_command"]    == "python -m chain.verifier", "Test 5: verify_command present")

    txt = cert_txt_path.read_text(encoding="utf-8")
    _check("AGENT DEATH CERTIFICATE" in txt, "Test 5: .txt contains header")
    _check(identity.public_key_hex  in txt,  "Test 5: .txt contains full pubkey")
    _check("test death"             in txt,  "Test 5: .txt contains reason")
    _check("python -m chain.verifier" in txt, "Test 5: .txt contains verify command")

    # ── Test 6: verify_chain() returns VALID after death ──────────────────────
    result = verify_chain()
    _check(result.valid,          f"Test 6: chain VALID after death (errors: {result.errors})")
    _check(result.entry_count == 4, f"Test 6: 4 entries verified (got {result.entry_count})")

    # ── Test 7: write_entry() raises ValueError after death ───────────────────
    raised = False
    try:
        write_entry(identity, {"should": "fail"}, "fact")
    except ValueError as e:
        raised = True
        _check("dead" in str(e).lower() or "sealed" in str(e).lower(),
               f"Test 7: correct error message: {e}")
    _check(raised, "Test 7: write_entry raises ValueError after death")

    # ── Test 8: identity.is_dead() True — read-only mode active ──────────────
    _check(identity.is_dead(), "Test 8: is_dead() returns True")

    # Simulate agent startup in dead state: load identity again
    identity2 = load_or_create()
    _check(identity2.is_dead(), "Test 8: freshly loaded identity also reports dead")

    # ── Test 9: die() raises ValueError on second call ────────────────────────
    raised2 = False
    try:
        die(identity, reason="second death")
    except ValueError as e:
        raised2 = True
    _check(raised2, "Test 9: second die() raises ValueError")

    print("\n  Death certificate (txt):")
    for line in txt.splitlines():
        print(f"  {line}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Identity temp dir : {_tmp_id}")
    print(f"Chain temp dir    : {_tmp_chain}")
    try:
        test_death_protocol()
        print("\nAll tests passed.")
    finally:
        shutil.rmtree(_tmp_id,    ignore_errors=True)
        shutil.rmtree(_tmp_chain, ignore_errors=True)
