"""
Chain reader — iterate and inspect the signed memory chain.

All reads are non-destructive. The chain file is never modified here.
`get_last_entry()` uses a backward seek to avoid reading the full file —
important once the chain has thousands of entries.
"""

import json
import os
from pathlib import Path
from typing import Iterator

AGENT_DIR = Path(os.environ.get("AGENT_DIR", "~/.agent")).expanduser()
CHAIN_PATH = AGENT_DIR / "chain.jsonl"

# Chunk size for backward seek scan. 4 KB covers any realistic single entry.
_SEEK_CHUNK = 4096


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_entries() -> Iterator[dict]:
    """
    Yield parsed entry dicts from chain.jsonl in chronological order.
    Skips blank lines. Raises json.JSONDecodeError on malformed lines.
    """
    if not CHAIN_PATH.exists():
        return

    with open(CHAIN_PATH, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line:
                yield json.loads(line)


def get_last_entry() -> dict | None:
    """
    Return the last entry in the chain, or None if the chain is empty.

    Seeks backward from end of file — O(1) in practice regardless of
    chain length, as long as a single entry fits in _SEEK_CHUNK bytes.
    Falls back to full scan if the last entry is unusually large.
    """
    if not CHAIN_PATH.exists():
        return None

    file_size = CHAIN_PATH.stat().st_size
    if file_size == 0:
        return None

    with open(CHAIN_PATH, "rb") as fh:
        raw_line = _read_last_line(fh, file_size)

    if not raw_line:
        return None

    return json.loads(raw_line.decode("utf-8"))


def get_entry_count() -> int:
    """
    Return the total number of entries in the chain.
    Counts non-blank lines — O(n) but unavoidable without an index.
    """
    if not CHAIN_PATH.exists():
        return 0

    count = 0
    with open(CHAIN_PATH, "rb") as fh:
        for line in fh:
            if line.rstrip(b"\n"):
                count += 1
    return count


# ---------------------------------------------------------------------------


def decrypt_entry_content(identity, entry: dict) -> dict:
    from identity.encryption import decrypt_line
    content = entry.get("content")
    if isinstance(content, str):
        try:
            return {**entry, "content": decrypt_line(identity, content.encode("ascii"))}
        except Exception:
            return entry
    return entry


def read_entries_decrypted(identity):
    for entry in read_entries():
        yield decrypt_entry_content(identity, entry)

# Internal helpers
# ---------------------------------------------------------------------------

def _read_last_line(fh, file_size: int) -> bytes | None:
    """
    Seek backward through fh to find and return the last non-empty line,
    without reading the whole file. Returns raw bytes without the newline.

    Strategy:
      1. Start at end of file.
      2. Read backward in _SEEK_CHUNK chunks, accumulating bytes.
      3. Once we have at least two newline positions, we can isolate the
         last complete line.
      4. If the entire file fits in one chunk (small chain), handle directly.
    """
    accumulated = b""
    pos = file_size

    while pos > 0:
        read_size = min(_SEEK_CHUNK, pos)
        pos -= read_size
        fh.seek(pos)
        chunk = fh.read(read_size)
        accumulated = chunk + accumulated

        # Strip a trailing newline from the very end of the file before searching
        stripped = accumulated.rstrip(b"\n")

        newline_pos = stripped.rfind(b"\n")
        if newline_pos != -1:
            last_line = stripped[newline_pos + 1:]
            if last_line:
                return last_line
            # The last non-empty line is before this newline — keep scanning
            accumulated = stripped[:newline_pos + 1]
            pos = 0  # Force one more iteration to get the rest if needed

    # Whole file in accumulated — return the last non-empty line
    lines = [l for l in accumulated.split(b"\n") if l.strip()]
    if not lines:
        return None
    return lines[-1]
