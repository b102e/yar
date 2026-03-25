"""
Chain verifier tests.

Run from the project root:
    python -m chain.test_verifier

Uses a temporary chain dir so it never touches ~/.agent/chain.jsonl.
Uses the real ~/.agent/identity/ keypair (already generated).

Corruption helper: parse the Nth line of the JSONL file, mutate one
field, re-write the file. The verifier detects:
  - changed prev_hash → hash check fails AND signature check fails
    (signature was over the original prev_hash)
  - changed signature → signature check fails; hash chain may cascade
    because _canonical(mutated_entry) ≠ _canonical(original_entry)
"""

import json
import os
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Override AGENT_DIR before importing chain modules
# ---------------------------------------------------------------------------

from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="yar_verifier_test_")
os.environ["AGENT_DIR"] = _tmp_dir
_tmp_chain = Path(_tmp_dir) / "chain.jsonl"

# chain/__init__.py imports writer/reader at package load time (before this module's
# code ran), so CHAIN_PATH is already baked in to ~/.agent/chain.jsonl.
# Explicitly override the module attributes so all functions use the temp path.
import chain.reader as _reader
import chain.writer as _writer
_writer.CHAIN_PATH = _tmp_chain
_writer.AGENT_DIR  = Path(_tmp_dir)
_reader.CHAIN_PATH = _tmp_chain
_reader.AGENT_DIR  = Path(_tmp_dir)

# Patch verifier's CHAIN_PATH binding as well
import chain.verifier as _verifier
_verifier.CHAIN_PATH = _tmp_chain

from chain.writer import write_entry, _canonical
from chain.reader import read_entries
from chain.verifier import verify_chain, format_result
from identity.keypair import load_or_create

# Guard: verify test isolation before any test writes data
assert _writer.CHAIN_PATH == _tmp_chain, (
    f"CHAIN_PATH not isolated!\n  expected: {_tmp_chain}\n  got: {_writer.CHAIN_PATH}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_chain() -> None:
    _reader.CHAIN_PATH.unlink(missing_ok=True)


def _identity():
    return load_or_create()


def _check(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(f"Test failed: {label}")


def _read_lines() -> list[str]:
    """Read all non-empty lines from the chain file as raw strings."""
    path = _writer.CHAIN_PATH  # use writer's binding — same module, avoids any path divergence
    if not path.exists():
        return []
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _write_lines(lines: list[str]) -> None:
    """Overwrite the chain file with the given lines (one per line)."""
    _writer.CHAIN_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _corrupt_field(line_index: int, field: str, new_value: str) -> None:
    """
    Replace field in the Nth line (0-indexed) of the chain file.
    Writes back as regular JSON (not canonical) — the verifier
    still parses it correctly, but the hash/sig checks will fail.
    """
    lines = _read_lines()
    entry = json.loads(lines[line_index])
    entry[field] = new_value
    lines[line_index] = json.dumps(entry)  # not canonical — that's the point
    _write_lines(lines)


# ---------------------------------------------------------------------------
# Test 1: Valid chain — verify_chain() returns valid=True
# ---------------------------------------------------------------------------

def test_valid_chain():
    print("\nTest 1: 5 valid entries → valid=True")
    _fresh_chain()
    identity = _identity()

    write_entry(identity, {"text": "fact one"},   "fact")
    write_entry(identity, {"summary": "session"}, "session")
    write_entry(identity, {"core": "focused"},    "consolidation")
    write_entry(identity, {"text": "fact two"},   "fact")
    write_entry(identity, {"text": "fact three"}, "fact")

    result = verify_chain()

    _check(result.valid,           "valid=True")
    _check(result.entry_count == 5, "entry_count=5")
    _check(result.broken_at is None, "broken_at is None")
    _check(result.errors == [],     "no errors")
    _check(result.first_entry is not None, "first_entry present")
    _check(result.last_entry  is not None, "last_entry present")
    _check(result.first_entry["id"] == "entry_0001", "first entry is entry_0001")
    _check(result.last_entry["id"]  == "entry_0005", "last entry is entry_0005")
    _check(result.first_entry["prev_hash"] == "genesis", "genesis prev_hash")


# ---------------------------------------------------------------------------
# Test 2: Corrupt prev_hash of entry 3 → broken_at=3
# ---------------------------------------------------------------------------

def test_corrupt_prev_hash():
    print("\nTest 2: Corrupt prev_hash of entry 3 → broken_at=3")
    _fresh_chain()
    identity = _identity()

    for i in range(5):
        write_entry(identity, {"n": i}, "fact")

    # Line index 2 = entry 3 (0-indexed)
    _corrupt_field(2, "prev_hash", "sha256:deadbeefdeadbeef")

    result = verify_chain()

    _check(not result.valid,        "valid=False")
    _check(result.broken_at == 3,   f"broken_at=3 (got {result.broken_at})")
    _check(len(result.errors) > 0,  "errors list non-empty")

    # Entry 3 must appear in errors (both hash and sig fail when prev_hash is tampered)
    errors_str = " ".join(result.errors)
    _check("entry_0003" in errors_str, f"entry_0003 mentioned in errors: {result.errors}")


# ---------------------------------------------------------------------------
# Test 3: Corrupt signature of entry 4 → broken_at=4
# ---------------------------------------------------------------------------

def test_corrupt_signature():
    print("\nTest 3: Corrupt signature of entry 4 → broken_at=4")
    _fresh_chain()
    identity = _identity()

    for i in range(5):
        write_entry(identity, {"n": i}, "fact")

    # Line index 3 = entry 4 (0-indexed)
    _corrupt_field(3, "signature", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    result = verify_chain()

    _check(not result.valid,       "valid=False")
    _check(result.broken_at == 4,  f"broken_at=4 (got {result.broken_at})")

    errors_str = " ".join(result.errors)
    _check("entry_0004" in errors_str, f"entry_0004 mentioned in errors: {result.errors}")


# ---------------------------------------------------------------------------
# Test 4: Both corrupted → both appear in errors list
# ---------------------------------------------------------------------------

def test_both_corrupted():
    print("\nTest 4: Corrupt entry 3 prev_hash + entry 4 signature → both in errors")
    _fresh_chain()
    identity = _identity()

    for i in range(5):
        write_entry(identity, {"n": i}, "fact")

    _corrupt_field(2, "prev_hash",  "sha256:cafebabecafebabe")   # entry 3
    _corrupt_field(3, "signature",  "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")  # entry 4

    result = verify_chain()

    _check(not result.valid,      "valid=False")
    _check(result.broken_at == 3, f"broken_at=3 (first failure, got {result.broken_at})")

    errors_str = " ".join(result.errors)
    _check("entry_0003" in errors_str, f"entry_0003 in errors: {result.errors}")
    _check("entry_0004" in errors_str, f"entry_0004 in errors: {result.errors}")


# ---------------------------------------------------------------------------
# Test 5: Empty chain → valid=True, entry_count=0
# ---------------------------------------------------------------------------

def test_empty_chain():
    print("\nTest 5: Empty chain → valid=True, entry_count=0")
    _fresh_chain()

    result = verify_chain()

    _check(result.valid,            "valid=True")
    _check(result.entry_count == 0, "entry_count=0")
    _check(result.broken_at is None, "broken_at is None")
    _check(result.first_entry is None, "first_entry is None")
    _check(result.last_entry  is None, "last_entry is None")


# ---------------------------------------------------------------------------
# Test 6: format_result() output sanity
# ---------------------------------------------------------------------------

def test_format_output():
    print("\nTest 6: format_result() output for valid and invalid chains")
    _fresh_chain()
    identity = _identity()

    for i in range(3):
        write_entry(identity, {"n": i}, "fact")

    valid_result = verify_chain()
    valid_text = format_result(valid_result)
    _check("VALID" in valid_text,    "VALID in output for clean chain")
    _check("Entries:" in valid_text,  "Entries line present")
    _check("Genesis:"  in valid_text, "Genesis line present")
    _check("Public key:" in valid_text, "Public key line present")

    _corrupt_field(0, "signature", "invalidsignature")
    broken_result = verify_chain()
    broken_text = format_result(broken_result)
    _check("BROKEN"  in broken_text,         "BROKEN in output for corrupt chain")
    _check("entry_0001" in broken_text,       "broken entry mentioned in output")
    _check("Broken at entry:" in broken_text, "broken_at line present")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Verifier test directory: {_tmp_dir}")
    try:
        test_valid_chain()
        test_corrupt_prev_hash()
        test_corrupt_signature()
        test_both_corrupted()
        test_empty_chain()
        test_format_output()
        print("\nAll tests passed.")
    finally:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)
