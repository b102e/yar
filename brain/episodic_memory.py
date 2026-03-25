"""
Эпизодическая память — каждая сессия становится структурированным эпизодом.

Хранилище: ~/claude-memory/episodes/
  episodes/
    2026-03-08.jsonl   — эпизоды по дням (JSONL, один эпизод = одна строка)
    index.json         — быстрый индекс: id, date, topics, key_moments, summary_short

Структура эпизода:
{
  "id": "ep_20260308_174800",
  "timestamp": "2026-03-08T17:48:00",
  "duration_min": 7,
  "summary": "[USER] показал себя через камеру, рассказал про выходной...",
  "topics": ["работа", "дрон", "голос"],
  "mood": "спокойный, сосредоточенный",
  "people": ["владимир"],
  "key_moments": ["владимир показал лицо впервые", "сказал возьмёт меня на работу"],
  "raw_highlights": ["первые 3 реплики пользователя"]
}

Summary генерируется через claude-haiku-4-5-20251001 — один вызов ~$0.001 при завершении.
При первом запуске — bootstrap из существующих conversations/*.json без API (бесплатно).
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from pathlib import Path


SUMMARY_PROMPT = """Проанализируй разговор между Яром (AI-компаньон) и [USER]ом.
Верни ТОЛЬКО JSON, без markdown-обёртки и без пояснений:

{{
  "summary": "Краткое описание сессии, 1-2 предложения. Что обсуждали, что произошло важного.",
  "topics": ["тема1", "тема2"],
  "mood": "настроение и эмоции [USER]",
  "people": ["владимир"],
  "key_moments": ["конкретный факт или событие", "ещё один момент"]
}}

Разговор:
{dialog}"""


class EpisodicMemory:

    def __init__(self, memory_dir: Path, api_key: str, identity=None):
        self.memory_dir   = Path(memory_dir)
        self.api_key      = api_key
        self.identity     = identity
        self.episodes_dir = self.memory_dir / "episodes"
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        self.index_file   = self.episodes_dir / "index.json"
        self._index: list = self._load_index()

        # Первый запуск — создаём базовые эпизоды из архива разговоров
        if not self._index:
            self._bootstrap_from_conversations()
        self._repair_previous_day_if_needed()

    # ── Публичный API ────────────────────────────────────────────────────────

    def record_episode(self, messages: list,
                       state_snapshot: str = None,
                       duration_min: int = 0) -> dict:
        """Записать эпизод завершённой сессии.
        Вызывается ОДИН РАЗ при завершении — через save_final() в memory.py.
        Генерирует summary через Claude Haiku (~$0.001)."""
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return {}

        summary_data = self._generate_summary(messages, state_snapshot)
        # Синхронный fallback: если summary неполный — повтор с таймаутом 10 сек.
        if (not summary_data.get("summary") or not summary_data.get("topics")):
            retry = self._generate_summary_with_timeout(
                messages,
                state_snapshot=state_snapshot,
                timeout_sec=10,
            )
            if retry:
                if retry.get("summary"):
                    summary_data["summary"] = retry.get("summary")
                if retry.get("topics"):
                    summary_data["topics"] = retry.get("topics")
                if retry.get("mood"):
                    summary_data["mood"] = retry.get("mood")
                if retry.get("people"):
                    summary_data["people"] = retry.get("people")
                if retry.get("key_moments"):
                    summary_data["key_moments"] = retry.get("key_moments")

        ep_id    = self._make_id()
        date_str = datetime.now().strftime("%Y-%m-%d")
        episode  = {
            "id":           ep_id,
            "timestamp":    datetime.now().isoformat(),
            "duration_min": duration_min,
            "summary":      summary_data.get("summary", "Сессия завершена."),
            "topics":       summary_data.get("topics", []),
            "mood":         summary_data.get("mood", ""),
            "people":       summary_data.get("people", ["владимир"]),
            "key_moments":  summary_data.get("key_moments", []),
            "raw_highlights": [m["content"][:120] for m in user_msgs[:3]],
        }

        # Дописываем в дневной .jsonl
        jsonl_path = self.episodes_dir / f"{date_str}.jsonl"
        if self.identity:
            from identity.encryption import encrypt_json
            with open(jsonl_path, "ab") as f:
                f.write(encrypt_json(self.identity, episode) + b"\n")
        else:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(episode, ensure_ascii=False) + "\n")

        # Обновляем быстрый индекс
        self._index.append({
            "id":            ep_id,
            "timestamp":     episode["timestamp"],
            "date":          date_str,
            "topics":        episode["topics"],
            "key_moments":   episode["key_moments"],
            "summary_short": episode["summary"][:120],
        })
        self._save_index()

        print(f"[EpisodicMemory] 📖 Эпизод записан: {ep_id}")
        return episode

    def repair_episodes(self, date_str: str):
        """
        Переобработать эпизоды за дату с пустыми topics/mood.
        """
        target = str(date_str or "").strip()
        if not target:
            return
        jsonl_path = self.episodes_dir / f"{target}.jsonl"
        if not jsonl_path.exists():
            return

        episodes = []
        try:
            if self.identity:
                from identity.encryption import decrypt_json
                with open(jsonl_path, "rb") as f:
                    for line in f:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            if raw[:1] == b"{":
                                episodes.append(json.loads(raw.decode("utf-8")))
                            else:
                                episodes.append(decrypt_json(self.identity, raw))
                        except Exception:
                            continue
            else:
                with open(jsonl_path, encoding="utf-8") as f:
                    for line in f:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            episodes.append(json.loads(raw))
                        except Exception:
                            continue
        except Exception:
            return

        if not episodes:
            return

        repaired = 0
        for ep in episodes:
            topics = ep.get("topics", []) or []
            mood = str(ep.get("mood", "") or "").strip()
            if topics and mood:
                continue

            raw_highlights = ep.get("raw_highlights", []) or []
            if not isinstance(raw_highlights, list):
                raw_highlights = []
            user_lines = [str(x).strip() for x in raw_highlights if str(x).strip()]
            if not user_lines:
                continue

            pseudo_messages = [{"role": "user", "content": t} for t in user_lines[:8]]
            summary_data = self._generate_summary_with_timeout(
                pseudo_messages,
                state_snapshot=None,
                timeout_sec=10,
            )
            if not summary_data:
                continue

            if summary_data.get("summary"):
                ep["summary"] = summary_data.get("summary")
            if summary_data.get("topics"):
                ep["topics"] = summary_data.get("topics")
            if summary_data.get("mood"):
                ep["mood"] = summary_data.get("mood")
            if summary_data.get("people"):
                ep["people"] = summary_data.get("people")
            if summary_data.get("key_moments"):
                ep["key_moments"] = summary_data.get("key_moments")
            repaired += 1

        if repaired == 0:
            return

        try:
            if self.identity:
                from identity.encryption import encrypt_json
                with open(jsonl_path, "wb") as f:
                    for ep in episodes:
                        f.write(encrypt_json(self.identity, ep) + b"\n")
            else:
                with open(jsonl_path, "w", encoding="utf-8") as f:
                    for ep in episodes:
                        f.write(json.dumps(ep, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[EpisodicMemory] repair write error: {e}")
            return

        for ep in episodes:
            if str(ep.get("timestamp", ""))[:10] != target:
                continue
            self._upsert_index_entry(ep)
        self._save_index()
        print(f"[EpisodicMemory] 🛠 repaired {repaired} эпизодов за {target}")

    def get_recent(self, n: int = 3) -> str:
        """Последние N эпизодов — строка для системного промпта."""
        if not self._index:
            return ""
        lines = []
        for ep in reversed(self._index[-n:]):
            date    = ep.get("timestamp", "")[:10]
            summary = ep.get("summary_short", "")
            topics  = ", ".join(ep.get("topics", []))
            line    = f"[{date}] {summary}"
            if topics:
                line += f"  ({topics})"
            lines.append(line)
        return "\n".join(lines)

    def get_significant(self, n: int = 2) -> str:
        """
        Наиболее значимые эпизоды (не просто последние):
        сортировка по количеству key_moments и насыщенности summary.
        """
        if not self._index:
            return ""
        scored = []
        for ep in self._index:
            km = ep.get("key_moments", []) or []
            summary = str(ep.get("summary_short", ""))
            score = len(km) * 3 + min(3, len(summary) // 40)
            scored.append((score, ep))
        scored.sort(key=lambda x: (x[0], x[1].get("timestamp", "")), reverse=True)
        lines = []
        for _, ep in scored[:max(1, n)]:
            date = ep.get("timestamp", "")[:10]
            summary = ep.get("summary_short", "")
            moments = ep.get("key_moments", [])[:2]
            line = f"[{date}] {summary}"
            if moments:
                line += " — " + "; ".join(str(m) for m in moments)
            lines.append(line)
        return "\n".join(lines)

    def search(self, query: str, n: int = 3) -> str:
        """Поиск по topics и key_moments — строка для системного промпта."""
        if not query or not self._index:
            return ""
        words = [w for w in query.lower().split() if len(w) > 2]
        if not words:
            return ""

        scored = []
        for ep in self._index:
            score = 0
            for topic in ep.get("topics", []):
                for w in words:
                    if w in topic.lower():
                        score += 2
            for moment in ep.get("key_moments", []):
                for w in words:
                    if w in moment.lower():
                        score += 1
            for w in words:
                if w in ep.get("summary_short", "").lower():
                    score += 1
            if score > 0:
                scored.append((score, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return ""

        lines = []
        for _, ep in scored[:n]:
            date    = ep.get("timestamp", "")[:10]
            summary = ep.get("summary_short", "")
            moments = ep.get("key_moments", [])[:2]
            line    = f"[{date}] {summary}"
            if moments:
                line += " — " + "; ".join(moments)
            lines.append(line)
        return "\n".join(lines)

    def get_by_date(self, date_str: str) -> str:
        """Эпизоды за конкретную дату — строка для промпта."""
        eps = [ep for ep in self._index if ep.get("date") == date_str]
        if not eps:
            return ""
        lines = []
        for ep in eps:
            time_str = ep.get("timestamp", "")[:16].replace("T", " ")
            lines.append(f"[{time_str}] {ep.get('summary_short', '')}")
        return "\n".join(lines)

    # ── Генерация summary через Claude Haiku ─────────────────────────────────

    def _generate_summary(self, messages: list,
                          state_snapshot: str = None) -> dict:
        """Один вызов Claude Haiku → структурированный JSON с описанием сессии."""
        # Пропускаем observation-записи (описания камеры), берём последние 20
        recent = [
            m for m in messages[-20:]
            if m.get("meta", {}).get("type") != "observation"
        ]
        if not recent:
            return {}

        dialog_lines = []
        for m in recent:
            speaker = "[USER]" if m.get("role") == "user" else "Яр"
            content = m.get("content", "")[:300]
            dialog_lines.append(f"{speaker}: {content}")
        dialog = "\n".join(dialog_lines)

        if state_snapshot:
            dialog += f"\n\n[Состояния Яра в конце: {state_snapshot}]"

        try:
            import anthropic
            client   = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user",
                           "content": SUMMARY_PROMPT.format(dialog=dialog)}],
            )
            text  = response.content[0].text.strip()
            match = re.search(r'\{[\s\S]+\}', text)
            if match:
                return json.loads(match.group())
        except Exception as e:
            print(f"[EpisodicMemory] _generate_summary error: {e}")

        # Fallback без API — базовый summary из highlights
        highlights = [m["content"][:80]
                      for m in messages if m.get("role") == "user"][:2]
        return {
            "summary":     "Разговор: " + " | ".join(highlights),
            "topics":      [],
            "mood":        "",
            "people":      ["владимир"],
            "key_moments": highlights[:2],
        }

    def _generate_summary_with_timeout(self, messages: list,
                                       state_snapshot: str = None,
                                       timeout_sec: int = 10) -> dict:
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self._generate_summary, messages, state_snapshot)
        try:
            result = future.result(timeout=max(1, int(timeout_sec)))
            return result if isinstance(result, dict) else {}
        except FuturesTimeoutError:
            print(f"[EpisodicMemory] ⏱ summary timeout ({timeout_sec}s), сохраняю сырой эпизод")
            return {}
        except Exception as e:
            print(f"[EpisodicMemory] summary retry error: {e}")
            return {}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # ── Индекс ───────────────────────────────────────────────────────────────

    def _load_index(self) -> list:
        if not self.index_file.exists():
            return []
        if self.identity:
            from identity.encryption import decrypt_file
            result = decrypt_file(self.identity, self.index_file, default=[])
            return result if isinstance(result, list) else []
        try:
            with open(self.index_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_index(self):
        if self.identity:
            from identity.encryption import encrypt_file
            encrypt_file(self.identity, self.index_file, self._index)
        else:
            with open(self.index_file, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2)

    def _upsert_index_entry(self, episode: dict):
        ep_id = str(episode.get("id", "")).strip()
        if not ep_id:
            return
        entry = {
            "id": ep_id,
            "timestamp": episode.get("timestamp", ""),
            "date": str(episode.get("timestamp", ""))[:10] or str(episode.get("date", ""))[:10],
            "topics": episode.get("topics", []) or [],
            "key_moments": (episode.get("key_moments", []) or [])[:2],
            "summary_short": str(episode.get("summary", ""))[:120],
        }
        for i, ex in enumerate(self._index):
            if str(ex.get("id", "")) == ep_id:
                self._index[i] = entry
                return
        self._index.append(entry)

    def _repair_previous_day_if_needed(self):
        prev = (datetime.now().date() - timedelta(days=1)).isoformat()
        path = self.episodes_dir / f"{prev}.jsonl"
        if not path.exists():
            return
        need_repair = False
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        ep = json.loads(raw)
                    except Exception:
                        continue
                    topics = ep.get("topics", []) or []
                    mood = str(ep.get("mood", "") or "").strip()
                    if (not topics) or (not mood):
                        need_repair = True
                        break
        except Exception:
            return
        if need_repair:
            self.repair_episodes(prev)

    @staticmethod
    def _make_id() -> str:
        return f"ep_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # ── Bootstrap из архива ──────────────────────────────────────────────────

    def _bootstrap_from_conversations(self):
        """Создать базовые эпизоды из conversations/*.json при первом запуске.
        Без вызова Claude API — быстро и бесплатно."""
        conv_dir = self.memory_dir / "conversations"
        if not conv_dir.exists():
            return

        conv_files = sorted(conv_dir.glob("*.json"))
        if not conv_files:
            return

        print(f"📚 EpisodicMemory bootstrap: {len(conv_files)} разговоров из архива...")
        for cf in conv_files:
            try:
                with open(cf, encoding="utf-8") as f:
                    data = json.load(f)

                messages   = data.get("messages", [])
                date_str   = (data.get("date") or "")[:10] or cf.stem[:10]
                timestamp  = data.get("date") or datetime.now().isoformat()
                highlights = [
                    m["content"][:120]
                    for m in messages
                    if m.get("role") == "user"
                ][:3]

                if not highlights:
                    continue

                ep_id   = f"ep_{cf.stem.replace('-', '').replace('_', '')}"
                episode = {
                    "id":           ep_id,
                    "timestamp":    timestamp,
                    "duration_min": 0,
                    "summary":      "Архив: " + " | ".join(highlights[:2]),
                    "topics":       [],
                    "mood":         "",
                    "people":       ["владимир"],
                    "key_moments":  highlights,
                    "raw_highlights": highlights,
                }

                jsonl_path = self.episodes_dir / f"{date_str}.jsonl"
                if self.identity:
                    from identity.encryption import encrypt_json
                    with open(jsonl_path, "ab") as f:
                        f.write(encrypt_json(self.identity, episode) + b"\n")
                else:
                    with open(jsonl_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(episode, ensure_ascii=False) + "\n")

                self._index.append({
                    "id":            ep_id,
                    "timestamp":     timestamp,
                    "date":          date_str,
                    "topics":        [],
                    "key_moments":   highlights[:2],
                    "summary_short": episode["summary"][:120],
                })
            except Exception as e:
                print(f"[EpisodicMemory] bootstrap skip {cf.name}: {e}")

        if self._index:
            self._save_index()
            print(f"✅ EpisodicMemory: создано {len(self._index)} эпизодов из архива")
