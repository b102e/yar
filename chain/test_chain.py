"""
Chain writer/reader tests.

Run from the project root:
    python -m chain.test_chain

Uses a temporary chain file so it never touches ~/.agent/chain.jsonl.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Override chain path before importing chain modules
# ---------------------------------------------------------------------------

_tmp_dir = tempfile.mkdtemp(prefix="yar_chain_test_")
os.environ["AGENT_DIR"] = _tmp_dir
_tmp_chain = Path(_tmp_dir) / "chain.jsonl"

# chain/__init__.py imports writer/reader at package load time, baking CHAIN_PATH
# to the default path before this module's env override takes effect.
# Explicitly patch the module-level constants after import.
import chain.writer as _writer
import chain.reader as _reader
_writer.CHAIN_PATH = _tmp_chain
_writer.AGENT_DIR  = Path(_tmp_dir)
_reader.CHAIN_PATH = _tmp_chain
_reader.AGENT_DIR  = Path(_tmp_dir)

from chain.writer import write_entry, _canonical, _sha256_hex
from chain.reader import read_entries, get_last_entry, get_entry_count
from identity.keypair import load_or_create


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_chain():
    """Delete the test chain file so each test starts clean."""
    # Use the module's resolved CHAIN_PATH directly — avoids macOS /var symlink
    # discrepancies between tempfile paths and Path-resolved paths.
    _reader.CHAIN_PATH.unlink(missing_ok=True)


def _load_identity():
    return load_or_create()


def _check(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(f"Test failed: {label}")


# ---------------------------------------------------------------------------
# Test 1: Write 3 entries of different types, read back, verify structure
# ---------------------------------------------------------------------------

def test_write_and_read_structure():
    print("\nTest 1: Write 3 entries, read back, verify structure")
    _fresh_chain()
    identity = _load_identity()

    e1 = write_entry(identity, {"text": "user prefers dark mode"}, "fact")
    e2 = write_entry(identity, {"summary": "discussed project goals"}, "episode")
    e3 = write_entry(identity, {"core": "focused, technical"}, "consolidation")

    entries = list(read_entries())
    _check(len(entries) == 3, "3 entries in chain")

    for i, entry in enumerate(entries, 1):
        _check("id" in entry, f"entry {i} has id")
        _check("timestamp" in entry, f"entry {i} has timestamp")
        _check("type" in entry, f"entry {i} has type")
        _check("content" in entry, f"entry {i} has content")
        _check("prev_hash" in entry, f"entry {i} has prev_hash")
        _check("signature" in entry, f"entry {i} has signature")

    _check(entries[0]["id"] == "entry_0001", "first id is entry_0001")
    _check(entries[1]["id"] == "entry_0002", "second id is entry_0002")
    _check(entries[2]["id"] == "entry_0003", "third id is entry_0003")

    _check(entries[0]["prev_hash"] == "genesis", "first entry prev_hash is 'genesis'")

    _check(entries[0]["type"] == "fact", "first type is fact")
    _check(entries[1]["type"] == "episode", "second type is episode")
    _check(entries[2]["type"] == "consolidation", "third type is consolidation")

    # prev_hash of entry 2 = sha256 of canonical bytes of entry 1
    expected_prev = _sha256_hex(_canonical(entries[0]))
    _check(entries[1]["prev_hash"] == expected_prev, "entry 2 prev_hash chains to entry 1")

    expected_prev2 = _sha256_hex(_canonical(entries[1]))
    _check(entries[2]["prev_hash"] == expected_prev2, "entry 3 prev_hash chains to entry 2")


# ---------------------------------------------------------------------------
# Test 2: Write entry, read last entry — IDs match
# ---------------------------------------------------------------------------

def test_last_entry_id():
    print("\nTest 2: Write entry, get_last_entry() — IDs match")
    _fresh_chain()
    identity = _load_identity()

    written = write_entry(identity, {"note": "test"}, "session")
    last = get_last_entry()

    _check(last is not None, "get_last_entry() returns something")
    _check(last["id"] == written["id"], f"last entry id matches written ({written['id']})")

    # Write a second one and verify again
    written2 = write_entry(identity, {"note": "second"}, "fact")
    last2 = get_last_entry()
    _check(last2["id"] == written2["id"], f"last entry id updates after second write ({written2['id']})")


# ---------------------------------------------------------------------------
# Test 3: Write 100 entries — get_entry_count() returns 100
# ---------------------------------------------------------------------------

def test_entry_count():
    print("\nTest 3: Write 100 entries — get_entry_count() returns 100")
    _fresh_chain()
    identity = _load_identity()

    for i in range(100):
        write_entry(identity, {"n": i}, "fact")

    count = get_entry_count()
    _check(count == 100, f"entry count is 100 (got {count})")

    last = get_last_entry()
    _check(last["id"] == "entry_0100", f"last id is entry_0100 (got {last['id']})")


# ---------------------------------------------------------------------------
# Test 4: Sequential ID is derived from disk, not an in-memory counter
# ---------------------------------------------------------------------------

def test_sequential_id_after_restart():
    print("\nTest 4: Sequential ID derived from disk state (restart-safe)")
    _fresh_chain()
    identity = _load_identity()

    # Write 5 entries
    for i in range(5):
        write_entry(identity, {"i": i}, "fact")

    # The writer reads the last entry from disk on every call — no in-memory counter.
    # Proof: a fresh function call (simulating a process restart) still gets entry_0006.
    # We verify this by calling _writer.write_entry directly (re-resolves from disk).
    e6 = _writer.write_entry(identity, {"i": 5}, "fact")
    _check(e6["id"] == "entry_0006", f"6th write gets entry_0006 (got {e6['id']})")

    # Verify the chain now has exactly 6 entries
    count = get_entry_count()
    _check(count == 6, f"chain has 6 entries after 6 writes (got {count})")

    # Verify the IDs are contiguous
    ids = [e["id"] for e in read_entries()]
    expected = [f"entry_{n:04d}" for n in range(1, 7)]
    _check(ids == expected, f"IDs are contiguous: {ids}")


# ---------------------------------------------------------------------------
# Test 5: Signature is verifiable
# ---------------------------------------------------------------------------

def test_signature_verifiable():
    print("\nTest 5: Signature is independently verifiable")
    _fresh_chain()
    identity = _load_identity()

    from identity.verify import verify as verify_sig
    from chain.writer import _canonical

    entry = write_entry(identity, {"msg": "hello"}, "fact")

    # Reconstruct signing surface (entry without signature field)
    entry_without_sig = {k: v for k, v in entry.items() if k != "signature"}
    signing_bytes = _canonical(entry_without_sig)

    result = verify_sig(identity.public_key_hex, signing_bytes, entry["signature"])
    _check(result.valid, "signature verifies against signing surface")

    # Tamper with content — signature must fail
    tampered = dict(entry_without_sig)
    tampered["content"] = {"msg": "tampered"}
    tampered_bytes = _canonical(tampered)
    bad = verify_sig(identity.public_key_hex, tampered_bytes, entry["signature"])
    _check(not bad.valid, "signature fails on tampered content")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Chain test directory: {_tmp_dir}")
    try:
        test_write_and_read_structure()
        test_last_entry_id()
        test_entry_count()
        test_sequential_id_after_restart()
        test_signature_verifiable()
        print("\nAll tests passed.")
    finally:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)
