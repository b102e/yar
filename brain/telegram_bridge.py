"""
Telegram bridge для приватного чата с Яром.

Работает через long polling (без входящих webhook портов).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

import httpx


class TelegramBridge:
    API_BASE = "https://api.telegram.org"

    def __init__(self, token: str, agent, allowed_chat_ids: Iterable[int] | None = None,
                 identity=None):
        self.token = str(token or "").strip()
        self.agent = agent
        # Accept explicit identity or fall back to agent.identity (set in Day 4)
        self.identity = identity or getattr(agent, "identity", None)
        self.allowed_chat_ids = {int(x) for x in (allowed_chat_ids or []) if str(x).strip()}
        self.offset = 0
        self._running = False
        self._stop = asyncio.Event()
        self._active_chat_id: int | None = None
        self._lock = asyncio.Lock()
        self._last_sent: tuple[str, str] = ("", "")

    def get_event_sink(self):
        async def _sink(event: dict):
            await self._on_agent_event(event)
        return _sink

    async def run(self):
        if not self.token:
            print("[Telegram] ⚠️ TOKEN пустой, bridge не запущен")
            return
        self._running = True
        print("[Telegram] ✅ Bridge запущен (long polling)")
        async with httpx.AsyncClient(timeout=40.0) as client:
            while not self._stop.is_set():
                try:
                    updates = await self._get_updates(client)
                    for upd in updates:
                        await self._handle_update(client, upd)
                except Exception as e:
                    print(f"[Telegram] poll error: {e}")
                    await asyncio.sleep(2.0)

    def stop(self):
        self._running = False
        self._stop.set()

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict]:
        params = {
            "timeout": 30,
            "offset": self.offset,
            "allowed_updates": '["message"]',
        }
        r = await client.get(f"{self.API_BASE}/bot{self.token}/getUpdates", params=params)
        data = r.json() if r.status_code == 200 else {}
        if not data.get("ok"):
            return []
        result = data.get("result", []) or []
        if result:
            self.offset = int(result[-1].get("update_id", 0)) + 1
        return result

    async def _handle_update(self, client: httpx.AsyncClient, upd: dict):
        msg = upd.get("message") or {}
        if not msg:
            return
        chat = msg.get("chat") or {}
        chat_id = int(chat.get("id", 0) or 0)
        if not chat_id:
            return
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            return
        text = str(msg.get("text") or "").strip()
        if not text:
            return

        self._active_chat_id = chat_id

        # Strip @botname suffix that Telegram appends in group chats
        cmd = text.lower().split("@")[0]
        # First word of cmd (for commands that carry arguments like /die)
        cmd_word = cmd.split()[0] if cmd.split() else ""

        if cmd in {"/start", "/help"}:
            await self._send_message(client, chat_id, "Яр на связи. Пиши как обычно.")
            return

        if cmd == "/verify":
            await self._cmd_verify(client, chat_id)
            return

        if cmd == "/whoami":
            await self._cmd_whoami(client, chat_id)
            return

        if cmd == "/chain":
            await self._cmd_chain(client, chat_id)
            return

        if cmd_word == "/die":
            # Preserve original-case reason; strip just the "/die" prefix from original text
            die_args = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            await self._cmd_die(client, chat_id, die_args)
            return

        # Read-only mode: dead agent ignores non-command messages
        if self.identity and self.identity.is_dead():
            await self._send_message(client, chat_id,
                "Агент мёртв. История доступна для верификации.\n"
                "/verify — проверить цепочку\n"
                "/whoami — идентичность\n"
                "/chain — последние записи"
            )
            return

        # Сериализуем ответы, чтобы не перемешивать диалоги.
        async with self._lock:
            await self.agent.process_external_text(text)

    # ── Crypto commands ──────────────────────────────────────────────────────

    async def _cmd_verify(self, client: httpx.AsyncClient, chat_id: int) -> None:
        """Run full chain verification and report result."""
        try:
            from chain.verifier import verify_chain, format_result
            result = verify_chain()
            text = format_result(result)
        except Exception as e:
            text = f"Ошибка верификации: {e}"
        await self._send_message(client, chat_id, text)

    async def _cmd_whoami(self, client: httpx.AsyncClient, chat_id: int) -> None:
        """Report agent's cryptographic identity."""
        try:
            from identity.keypair import GENESIS_PATH
            from chain.reader import get_entry_count

            identity = self.identity
            if identity is None:
                await self._send_message(client, chat_id, "Идентичность не инициализирована.")
                return

            genesis_ts = "unknown"
            if GENESIS_PATH.exists():
                gdata = json.loads(GENESIS_PATH.read_text(encoding="utf-8"))
                genesis_ts = gdata.get("timestamp", "unknown")

            count = get_entry_count()
            status = "✗ DEAD (read-only)" if identity.is_dead() else "✓ ALIVE"

            lines = [
                "Я — криптографический субъект.",
                "",
                f"Публичный ключ: {identity.public_key_hex}",
                f"Genesis: {genesis_ts}",
                f"Записей в цепочке: {count}",
                f"Статус: {status}",
                "",
                "Верифицировать историю: /verify",
            ]
            await self._send_message(client, chat_id, "\n".join(lines))
        except Exception as e:
            await self._send_message(client, chat_id, f"Ошибка /whoami: {e}")

    async def _cmd_chain(self, client: httpx.AsyncClient, chat_id: int) -> None:
        """Show last 7 chain entries (metadata only, no content)."""
        try:
            from chain.reader import read_entries

            all_entries = list(read_entries())
            if not all_entries:
                await self._send_message(client, chat_id, "Цепочка пуста.")
                return

            recent = all_entries[-7:][::-1]  # last 7, newest first
            lines = ["Последние записи цепочки:", ""]
            for entry in recent:
                raw_id = entry.get("id", "entry_0000")
                num = raw_id.split("_")[-1]          # "0041"
                etype = entry.get("type", "unknown")
                ts = entry.get("timestamp", "")
                lines.append(f"#{num}  {etype:<13}  {ts}")

            await self._send_message(client, chat_id, "\n".join(lines))
        except Exception as e:
            await self._send_message(client, chat_id, f"Ошибка /chain: {e}")

    async def _cmd_die(self, client: httpx.AsyncClient, chat_id: int, args: str) -> None:
        """Death protocol — irreversible. Requires explicit /die confirm."""
        if self.identity and self.identity.is_dead():
            await self._send_message(client, chat_id, "Агент уже мёртв. /verify чтобы проверить цепочку.")
            return

        args_lower = args.strip().lower()
        if not args_lower.startswith("confirm"):
            await self._send_message(client, chat_id,
                "⚠️ Это необратимое действие.\n\n"
                "Агент запишет финальную подписанную запись, "
                "уничтожит приватный ключ и перейдёт в режим "
                "только чтения навсегда.\n\n"
                "Для подтверждения:\n"
                "/die confirm [причина]"
            )
            return

        # Extract reason: everything after "confirm" (preserving original case from args)
        parts = args.split(None, 1)
        reason = parts[1].strip() if len(parts) > 1 else ""

        await self._send_message(client, chat_id, "Выполняю финальный акт...")

        try:
            from lifecycle.die import die
            cert_path = die(self.identity, reason=reason)
            txt = cert_path.with_suffix(".txt").read_text(encoding="utf-8")
            await self._send_message(client, chat_id, txt)
        except Exception as e:
            await self._send_message(client, chat_id, f"Ошибка death protocol: {e}")

    # ─────────────────────────────────────────────────────────────────────────

    async def _on_agent_event(self, event: dict):
        if not self._running:
            return
        chat_id = self._active_chat_id
        if not chat_id:
            return
        etype = str(event.get("type") or "")
        if etype != "message":
            return
        if str(event.get("role") or "") != "assistant":
            return
        text = str(event.get("content") or "").strip()
        if not text:
            return

        # Защита от дублирующих отправок одного и того же ответа.
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if self._last_sent == (text, stamp):
            return
        self._last_sent = (text, stamp)

        async with httpx.AsyncClient(timeout=15.0) as client:
            await self._send_message(client, chat_id, text)

    async def _send_message(self, client: httpx.AsyncClient, chat_id: int, text: str):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        r = await client.post(f"{self.API_BASE}/bot{self.token}/sendMessage", json=payload)
        if r.status_code != 200:
            print(f"[Telegram] send error: {r.status_code} {r.text[:120]}")
