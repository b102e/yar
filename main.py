#!/usr/bin/env python3
"""
YAR — Cryptographic Agent
Запуск: python main.py --telegram
"""

import asyncio
import argparse
import os
import sys
from identity.keypair import load_or_create, check_permissions, GENESIS_PATH
from chain.writer import write_entry
from brain.agent import Agent
from brain.memory import Memory
from brain.memory_search import MemorySearch
from brain.episodic_memory import EpisodicMemory
from brain.autonomous_research import AutonomousResearch, InterestManager
from brain.state import InternalState
from brain.autonomy import AutonomyManager
from brain.continuity import ContinuityTracker
from brain.self_check import SelfCheck
from brain.upgrade_proposals import UpgradeProposals
from brain.memory_consolidation import MemoryConsolidation
from brain.telegram_bridge import TelegramBridge

def banner():
    print("""
╔══════════════════════════════════════╗
║        YAR — Cryptographic Agent     ║
║         память + разум + цепочка     ║
╚══════════════════════════════════════╝
""")

def _parse_allowed_chat_ids(raw: str) -> list[int]:
    out = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            continue
    return out


async def main(telegram_mode: bool = False):
    banner()

    # ── Cryptographic identity — must be first ───────────────────────────────
    from chain.reader import get_entry_count, CHAIN_PATH as _chain_path

    # Edge case: chain exists but genesis missing → corruption, abort
    if _chain_path.exists() and _chain_path.stat().st_size > 0 and not GENESIS_PATH.exists():
        print("[Identity] ✗ Chain exists but identity/genesis.json is missing — possible corruption")
        print("[Identity] ✗ Cannot verify authorship. Refusing to start.")
        sys.exit(1)

    identity = load_or_create()

    # Permissions audit
    for warn in check_permissions():
        print(warn)

    # Edge case: identity exists but chain is empty → write bootstrap entry
    if not _chain_path.exists() or _chain_path.stat().st_size == 0:
        if GENESIS_PATH.exists():
            print("[Identity] ⚠️  Identity exists but chain is empty — writing genesis entry")

    if identity.is_dead():
        print("[Identity] ⚠️  Agent is dead. Running in read-only mode.")

    write_entry(identity, {
        "event": "startup",
        "public_key": identity.public_key_hex,
        "mode": "read-only" if identity.is_dead() else "normal",
    }, "session")

    # Startup summary
    import json as _json
    _genesis_ts = "—"
    try:
        _g = _json.loads(GENESIS_PATH.read_text(encoding="utf-8"))
        _genesis_ts = _g.get("timestamp", "—")
        # Trim to compact form: 2026-03-25T09:14:32Z
        if len(_genesis_ts) > 20:
            _genesis_ts = _genesis_ts[:19] + "Z"
    except Exception:
        pass
    _chain_count = get_entry_count()
    _pk = identity.public_key_hex
    _pk_display = f"{_pk[:8]}...{_pk[-8:]}"
    _status = "✗ DEAD (read-only)" if identity.is_dead() else "✓ ALIVE"
    _bar = "═" * 41
    print(f"\n{_bar}")
    print(f"  YAR — Cryptographic Subject")
    print(_bar)
    print(f"  Public key : {_pk_display}")
    print(f"  Genesis    : {_genesis_ts}")
    print(f"  Chain      : {_chain_count} entries")
    print(f"  Status     : {_status}")
    print(f"{_bar}\n")
    # ─────────────────────────────────────────────────────────────────────────

    memory      = Memory(identity=identity)
    autonomy    = AutonomyManager(memory.memory_dir)
    interest_manager = InterestManager(memory.memory_dir)
    consolidation = MemoryConsolidation(
        memory_dir=memory.memory_dir,
        interest_manager=interest_manager,
        identity=identity,
    )
    memory.set_consolidation(consolidation)
    continuity  = ContinuityTracker(memory.memory_dir)
    checker     = SelfCheck(memory.memory_dir, consolidation=consolidation)
    proposals   = UpgradeProposals(memory.memory_dir)

    # Семантический поиск — инициализируется после Memory (читает те же файлы).
    # Если chromadb/sentence-transformers не установлены — работает без них.
    memory_search = MemorySearch(memory.memory_dir, identity=identity)
    memory.set_search(memory_search)

    # Эпизодическая память — при первом запуске делает bootstrap из conversations/.
    episodic = EpisodicMemory(memory.memory_dir,
                              api_key=os.environ["ANTHROPIC_API_KEY"],
                              identity=identity)
    memory.set_episodic(episodic)

    # Автономный исследователь — изучает темы в фоне между разговорами.
    # Graceful fallback если duckduckgo-search не установлен.
    research = AutonomousResearch(
        memory_dir=memory.memory_dir,
        api_key=os.environ["ANTHROPIC_API_KEY"],
        daily_token_limit=50000,
        interest_manager=interest_manager,
    )
    research.set_memory_search(memory_search)

    state       = InternalState()
    agent       = Agent(
        memory=memory, state=state,
        continuity=continuity,
        self_check=checker, proposals=proposals,
        memory_search=memory_search,
        episodic=episodic,
        research=research,
        consolidation=consolidation,
        autonomy=autonomy,
        identity=identity,
    )
    checker.set_agent(agent)
    telegram_bridge = None

    # Диагностика при старте
    checker.run()

    _shutdown_done = False

    async def shutdown():
        nonlocal _shutdown_done
        if _shutdown_done:
            return
        _shutdown_done = True
        print("\n\n👋 Выключаюсь. Сохраняю память...")
        continuity.mark_online()
        await asyncio.to_thread(memory.save_final)  # записывает эпизод через Haiku, затем сохраняет JSON
        try:
            write_entry(identity, {"event": "shutdown", "chain_length": get_entry_count()}, "session")
        except Exception as e:
            print(f"[Chain] shutdown entry skipped: {e}")
        pending = proposals.get_pending()
        if pending:
            print(f"💡 Накоплено предложений: {len(pending)}")
            for p in pending:
                print(f"   #{p['id']} {p['title']}")
        print("✅ Готово. Пока.")

    print(f"💾 Память: {memory.memory_dir}")
    print(f"⏱  Статус: {continuity.short_status()}")
    print(f"💡 Предложений pending: {len(proposals.get_pending())}")
    if telegram_mode:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed = _parse_allowed_chat_ids(
            os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "")
            or os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        )
        if not token:
            print("⚠️ Telegram режим включён, но TELEGRAM_BOT_TOKEN не задан")
        else:
            telegram_bridge = TelegramBridge(token=token, agent=agent, allowed_chat_ids=allowed,
                                             identity=identity)
            agent.add_event_sink(telegram_bridge.get_event_sink())
            print(f"📨 Telegram bridge: ON ({'private whitelist' if allowed else 'no chat whitelist'})")

    try:
        if telegram_bridge:
            agent_task = asyncio.create_task(agent.run(), name="agent")
            tg_task = asyncio.create_task(telegram_bridge.run(), name="telegram_bridge")
            await asyncio.gather(agent_task, tg_task)
        else:
            await agent.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if telegram_bridge:
            telegram_bridge.stop()
        await shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Запустить Telegram bridge (long polling)")
    args = parser.parse_args()
    try:
        asyncio.run(main(telegram_mode=args.telegram))
    except KeyboardInterrupt:
        pass
