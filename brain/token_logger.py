"""
Пассивный логгер токенов Anthropic API.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class TokenLogger:
    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            flag = str(os.getenv("TOKEN_LOG", "false")).strip().lower()
            enabled = flag in {"1", "true", "yes", "on"}
        self.enabled = bool(enabled)
        self.base_dir = Path("~/claude-memory/token_logs").expanduser()

    def log(self, user_text: str, system_prompt: str, messages: list, response: Any):
        if not self.enabled:
            return
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)

            now = datetime.now()
            day_file = self.base_dir / f"{now.date().isoformat()}.jsonl"

            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

            msg_estimate = self._estimate_messages_tokens(messages)
            system_tokens = max(0, input_tokens - msg_estimate)

            text = ""
            content = getattr(response, "content", None)
            if isinstance(content, list) and content:
                part = content[0]
                text = str(getattr(part, "text", "") or "")

            row = {
                "ts": now.isoformat(),
                "user_text": str(user_text or ""),
                "system_prompt": str(system_prompt or ""),
                "messages_count": len(messages or []),
                "system_tokens": system_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "response_text": text[:500],
            }
            with open(day_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # Не ломаем основной поток.
            pass

    def _estimate_messages_tokens(self, messages: list) -> int:
        if not messages:
            return 0
        total_chars = 0
        for msg in messages:
            total_chars += len(str(msg.get("role", "")))
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total_chars += len(str(part.get("text", "")))
                        else:
                            total_chars += len(json.dumps(part, ensure_ascii=False))
                    else:
                        total_chars += len(str(part))
            else:
                total_chars += len(str(content))
        return max(0, total_chars // 4)


def test_token_logger():
    """Запусти: TOKEN_LOG=true python -c 'from brain.token_logger import test_token_logger; test_token_logger()'"""
    import anthropic

    logger = TokenLogger(enabled=True)
    client = anthropic.Anthropic()

    system = "Ты тестовый ассистент."
    messages = [{"role": "user", "content": "Скажи одно слово."}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=system,
        messages=messages
    )

    logger.log("Скажи одно слово.", system, messages, response)
    print("✅ Тест прошёл. Смотри ~/claude-memory/token_logs/")
