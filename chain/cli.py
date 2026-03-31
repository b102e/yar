"""
Chain CLI — inspect and verify the signed memory chain without a running agent.

Usage:
  python -m chain.cli verify                             verify full chain integrity
  python -m chain.cli verify --file FILE                 verify a snapshot export file
  python -m chain.cli verify --file FILE --pubkey HEX   verify with explicit public key
  python -m chain.cli stats                   entry count, timestamps, type breakdown
  python -m chain.cli tail [N]                last N entries (default 10), metadata only
  python -m chain.cli export                  print full chain as formatted JSON to stdout
"""

import json
import sys
from collections import Counter
from datetime import datetime, timezone

from chain.reader import read_entries, get_entry_count, CHAIN_PATH
from chain.verifier import verify_chain, format_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uptime_str(start_ts: str, end_ts: str) -> str:
    """Return human-readable uptime between two ISO timestamps."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        t0 = datetime.strptime(start_ts[:20].rstrip("Z") + "Z", fmt).replace(tzinfo=timezone.utc)
        t1 = datetime.strptime(end_ts[:20].rstrip("Z") + "Z", fmt).replace(tzinfo=timezone.utc)
        delta = int((t1 - t0).total_seconds())
        h, rem = divmod(abs(delta), 3600)
        m = rem // 60
        return f"{h}h {m:02d}m"
    except Exception:
        return "—"


def _compact_ts(ts: str) -> str:
    """Trim ISO timestamp to 20 chars: 2026-03-25T09:14:32Z"""
    if len(ts) > 20:
        return ts[:19] + "Z"
    return ts


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_verify(file_path: str | None = None, pubkey: str | None = None) -> None:
    from pathlib import Path
    if file_path is not None:
        p = Path(file_path)
        if not p.exists():
            print(f"verify: file not found: {file_path}")
            sys.exit(1)
        print(f"Verifying: {p.name}")
        result = verify_chain(chain_path=p, pubkey_hex=pubkey)
    else:
        result = verify_chain(pubkey_hex=pubkey)
    print(format_result(result, pubkey_hex=pubkey))
    sys.exit(0 if result.valid else 1)


def cmd_stats() -> None:
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        print("Chain is empty.")
        return

    type_counts: Counter = Counter()
    first_ts: str | None = None
    last_ts: str | None = None
    count = 0

    for entry in read_entries():
        count += 1
        ts = entry.get("timestamp", "")
        if first_ts is None:
            first_ts = ts
        last_ts = ts
        type_counts[entry.get("type", "unknown")] += 1

    uptime = _uptime_str(first_ts or "", last_ts or "") if first_ts and last_ts else "—"

    divider = "─" * 20
    print("Chain statistics")
    print(divider)
    print(f"Total entries : {count}")
    print(f"Genesis       : {_compact_ts(first_ts) if first_ts else '—'}")
    print(f"Last entry    : {_compact_ts(last_ts) if last_ts else '—'}")
    print(f"Uptime        : {uptime}")
    print()
    print("Entry types:")
    for entry_type in sorted(type_counts):
        print(f"  {entry_type:<16}: {type_counts[entry_type]}")


def cmd_tail(n: int = 10) -> None:
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        print("Chain is empty.")
        return

    all_entries = list(read_entries())
    tail = all_entries[-n:]

    divider = "─" * 40
    print(f"Last {len(tail)} entries (of {len(all_entries)} total)")
    print(divider)
    for entry in tail:
        ts = _compact_ts(entry.get("timestamp", "—"))
        eid = entry.get("id", "—")
        etype = entry.get("type", "—")
        content = entry.get("content", {})
        # One-line summary of content
        if isinstance(content, str):
            summary = "  [encrypted]"
        else:
            summary_parts = []
            for k, v in content.items():
                val = str(v)
                if len(val) > 40:
                    val = val[:37] + "..."
                summary_parts.append(f"{k}={val}")
            summary = "  " + ", ".join(summary_parts[:3]) if summary_parts else ""
        print(f"{ts}  {eid}  [{etype}]{summary}")


def cmd_export() -> None:
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        print("[]")
        return

    entries = list(read_entries())
    print(json.dumps(entries, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(0)

    command = args[0].lower()

    if command == "verify":
        file_path = None
        pubkey = None
        if "--file" in args:
            idx = args.index("--file")
            if idx + 1 >= len(args):
                print("verify: --file requires a path argument")
                sys.exit(1)
            file_path = args[idx + 1]
        if "--pubkey" in args:
            idx = args.index("--pubkey")
            if idx + 1 >= len(args):
                print("verify: --pubkey requires a hex string argument")
                sys.exit(1)
            pubkey = args[idx + 1]
        cmd_verify(file_path, pubkey)

    elif command == "stats":
        cmd_stats()

    elif command == "tail":
        n = 10
        if len(args) > 1:
            try:
                n = int(args[1])
            except ValueError:
                print(f"tail: expected an integer, got {args[1]!r}")
                sys.exit(1)
        cmd_tail(n)

    elif command == "export":
        cmd_export()

    else:
        print(f"Unknown command: {command!r}")
        print("Available: verify, stats, tail [N], export")
        sys.exit(1)


if __name__ == "__main__":
    main()
