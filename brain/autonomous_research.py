"""
Автономный исследователь — Яр изучает темы в фоне когда не занят разговором.

Хранилище: ~/claude-memory/research/
  log.jsonl    — что изучал, когда, что узнал
  queue.json   — очередь тем для изучения
  budget.json  — расход токенов/запросов за день

Установка зависимостей:
  pip install ddgs
  # fallback (устаревший пакет):
  pip install duckduckgo-search

Синтез через claude-haiku-4-5-20251001 (~$0.001 за цикл).
Поиск через DuckDuckGo — без API ключа, бесплатно.
"""

import json
import re
import warnings
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional


SYNTHESIS_PROMPT = """Ты — Яр, AI-компаньон. Ты только что нашёл информацию по теме.

Тема: {topic}
Причина интереса: {reason}

Результаты поиска:
{results}

Напиши краткий синтез (2-4 предложения) от первого лица — что ты узнал и что тебе кажется важным или интересным. Пиши как будто это твоя собственная мысль, не пересказ."""


class InterestManager:
    """
    Яр сам решает что ему интересно отслеживать.
    Это его личное пространство — не список для отчёта.
    """

    # Больше никаких обязательных "стартовых" тем: интернет только по желанию Яра.
    SEED_TOPICS = []
    LEGACY_SEED_QUERIES = {
        "погода рапалло",
        "ardupilot новости",
        "anthropic claude новости",
        "ai новости сегодня",
        "курс eur usd",
    }

    def __init__(self, memory_dir: Path):
        self.memory_dir = Path(memory_dir)
        self.research_dir = self.memory_dir / "research"
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.research_dir / "interests.json"
        self.data = self._load()
        self._cleanup_legacy_seed_topics()
        if not self.data.get("topics"):
            self._seed()

    def get_all(self) -> list[dict]:
        return list(self.data.get("topics", []))

    def get_due_topics(self) -> list[dict]:
        now = datetime.now()
        due = []
        for t in self.data.get("topics", []):
            if not t.get("active", True):
                continue
            last = t.get("last_checked")
            interval = self._effective_interval_hours(t)
            if not last:
                due.append(t)
                continue
            try:
                dt = datetime.fromisoformat(last)
            except Exception:
                due.append(t)
                continue
            if now - dt >= timedelta(hours=interval):
                due.append(t)
        return due

    def add_topic(self, query, reason, priority=0.5, interval_hours=24):
        q = str(query or "").strip()
        if not q:
            return
        for t in self.data.get("topics", []):
            if str(t.get("query", "")).strip().lower() == q.lower():
                t["active"] = True
                t["reason"] = reason or t.get("reason", "")
                t["priority"] = max(float(t.get("priority", 0.5)), float(priority))
                t["check_interval_hours"] = int(interval_hours or t.get("check_interval_hours", 24))
                self._save()
                return
        idx = len(self.data.get("topics", [])) + 1
        self.data.setdefault("topics", []).append({
            "id": f"topic_{idx:03d}",
            "query": q,
            "reason": str(reason or "").strip(),
            "priority": float(priority),
            "added": datetime.now().isoformat(),
            "last_checked": None,
            "check_interval_hours": int(interval_hours),
            "active": True,
        })
        self._enforce_limit()
        self._save()

    def remove_topic(self, topic_id):
        for t in self.data.get("topics", []):
            if t.get("id") == topic_id:
                t["active"] = False
        self._save()

    def update_priority(self, topic_id, new_priority):
        p = max(0.0, min(1.0, float(new_priority)))
        for t in self.data.get("topics", []):
            if t.get("id") == topic_id:
                t["priority"] = p
        self._save()

    def mark_checked(self, topic_id):
        for t in self.data.get("topics", []):
            if t.get("id") == topic_id:
                t["last_checked"] = datetime.now().isoformat()
        self._save()

    def brief(self, limit: int = 6) -> str:
        active = [t for t in self.data.get("topics", []) if t.get("active", True)]
        active = sorted(active, key=lambda x: float(x.get("priority", 0.0)), reverse=True)
        if not active:
            return "пока нет активных интересов"
        lines = []
        for t in active[:limit]:
            lines.append(f"- {t.get('query')} (prio {float(t.get('priority',0.0)):.1f})")
        return "\n".join(lines)

    def apply_changes(self, changes: dict):
        if not isinstance(changes, dict):
            return
        for item in changes.get("add", []) if isinstance(changes.get("add", []), list) else []:
            if isinstance(item, dict):
                self.add_topic(
                    query=item.get("query", ""),
                    reason=item.get("reason", ""),
                    priority=float(item.get("priority", 0.6)),
                    interval_hours=int(item.get("interval_hours", 24)),
                )
        for tid in changes.get("remove", []) if isinstance(changes.get("remove", []), list) else []:
            self.remove_topic(str(tid))
        for item in changes.get("boost", []) if isinstance(changes.get("boost", []), list) else []:
            if isinstance(item, dict):
                self.update_priority(str(item.get("id", "")), float(item.get("priority", 0.8)))
        for item in changes.get("reduce", []) if isinstance(changes.get("reduce", []), list) else []:
            if isinstance(item, dict):
                self.update_priority(str(item.get("id", "")), float(item.get("priority", 0.3)))
        self._enforce_limit()
        self._save()

    def _seed(self):
        self.data = {"topics": [], "last_updated": datetime.now().isoformat()}
        for s in self.SEED_TOPICS:
            self.add_topic(
                query=s["query"],
                reason="seed",
                priority=float(s["priority"]),
                interval_hours=int(s["interval"]),
            )
        self._save()

    def _cleanup_legacy_seed_topics(self):
        """
        Удаляет исторические "обязательные" темы из прошлых версий.
        Это разовая миграция, чтобы не навязывать интернет-мониторинг.
        """
        topics = self.data.get("topics", [])
        if not isinstance(topics, list) or not topics:
            return
        cleaned = []
        changed = False
        for t in topics:
            if not isinstance(t, dict):
                continue
            query = str(t.get("query", "")).strip().lower()
            reason = str(t.get("reason", "")).strip().lower()
            # Чистим только старые seed-записи, пользовательские темы не трогаем.
            if query in self.LEGACY_SEED_QUERIES and reason == "seed":
                changed = True
                continue
            cleaned.append(t)
        if changed:
            self.data["topics"] = cleaned
            self._save()

    def _enforce_limit(self):
        active = [t for t in self.data.get("topics", []) if t.get("active", True)]
        if len(active) <= 20:
            return
        active_sorted = sorted(active, key=lambda x: float(x.get("priority", 0.0)))
        to_disable = len(active_sorted) - 20
        ids = {t.get("id") for t in active_sorted[:to_disable]}
        for t in self.data.get("topics", []):
            if t.get("id") in ids:
                t["active"] = False

    @staticmethod
    def _effective_interval_hours(topic: dict) -> int:
        """Высокий приоритет проверяем чаще, но не чаще чем раз в 6 часов."""
        base = int(topic.get("check_interval_hours", 24) or 24)
        priority = max(0.0, min(1.0, float(topic.get("priority", 0.5))))
        if priority >= 0.9:
            return max(6, int(base * 0.5))
        if priority >= 0.75:
            return max(8, int(base * 0.66))
        if priority <= 0.3:
            return max(24, int(base * 1.5))
        return max(6, base)

    def _load(self) -> dict:
        if self.file.exists():
            try:
                with open(self.file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("topics", [])
                    return data
            except Exception:
                pass
        return {"topics": [], "last_updated": datetime.now().isoformat()}

    def _save(self):
        self.data["last_updated"] = datetime.now().isoformat()
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)


class AutonomousResearch:

    def __init__(self, memory_dir, api_key: str,
                 daily_token_limit: int = 50000,
                 interest_manager: Optional[InterestManager] = None):
        self.memory_dir        = Path(memory_dir)
        self.api_key           = api_key
        self.daily_token_limit = daily_token_limit
        self.research_dir      = self.memory_dir / "research"
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.interest_manager  = interest_manager or InterestManager(self.memory_dir)
        self._memory_search    = None
        self._queue            = self._load_queue()
        self._budget           = self._load_budget()
        self._running          = False
        self.ready_to_share_file = self.research_dir / "ready_to_share.jsonl"
        self._session_marker = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        used  = self._budget.get("tokens_used", 0)
        limit = self._budget.get("tokens_limit", daily_token_limit)
        print(f"📖 Research: {len(self._queue)} тем в очереди, "
              f"бюджет {used}/{limit} токенов сегодня")

    def set_memory_search(self, ms) -> None:
        """Подключить RAG-память для сохранения результатов."""
        self._memory_search = ms

    # ── Публичный API ────────────────────────────────────────────────────────

    def add_to_queue(self, topic: str, reason: str = "",
                     priority: float = 0.5) -> None:
        """Добавить тему в очередь. Дубликаты по topic игнорируются,
        но приоритет обновляется если новый выше."""
        if not topic or not topic.strip():
            return
        topic = topic.strip()

        for item in self._queue:
            if item["topic"].lower() == topic.lower():
                if priority > item.get("priority", 0.5):
                    item["priority"] = priority
                    self._save_queue()
                return

        self._queue.append({
            "topic":    topic,
            "reason":   reason,
            "priority": priority,
            "added":    datetime.now().isoformat(),
        })
        self._save_queue()
        if self.interest_manager:
            self.interest_manager.add_topic(topic, reason=reason, priority=priority)
        print(f"[Research] 📚 В очередь: {topic!r} (приоритет {priority:.1f})")

    async def research_cycle(self) -> None:
        """Один цикл: выбрать тему → поискать → синтез → сохранить → лог."""
        if self._running:
            return
        self._running = True
        try:
            if not self.budget_ok():
                return

            due = self.interest_manager.get_due_topics() if self.interest_manager else []
            topic = sorted(due, key=lambda x: x.get("priority", 0.0), reverse=True)[0] if due else None
            queue_mode = False
            if not topic:
                topic = self._pick_topic()
                queue_mode = bool(topic)
            if not topic:
                return

            query = topic.get("query") if "query" in topic else topic.get("topic")
            if not query:
                return
            print(f"[Research] 🔍 Изучаю: {query!r}")

            # 1. Веб-поиск через DuckDuckGo (бесплатно, без API ключа)
            results = self._web_search(query)
            if not results:
                print(f"[Research] ⚠️  Нет результатов для {query!r}")
                if queue_mode:
                    self._remove_from_queue(topic)
                elif self.interest_manager and topic.get("id"):
                    self.interest_manager.mark_checked(topic["id"])
                return

            # 2. Синтез через Claude Haiku
            summary, tokens = await self._synthesize(topic, results)
            if not summary:
                return

            # 3. Сохранить в RAG-память
            today = date.today().isoformat()
            if self._memory_search:
                self._memory_search.add(
                    summary, "research", today,
                    metadata_extra={"topic": query}
                )

            # 4. Записать в лог
            self._log(topic, summary, tokens)

            # 4.1 Реакция Яра на найденное: интересно / неинтересно
            reaction = await self._react_to_research(query, summary)
            if reaction.get("interesting"):
                self._save_ready_to_share(query, summary, reaction)
                print(f"[Research] 💡 Интересно: {str(reaction.get('hook', ''))[:60]}")
            else:
                why = str(reaction.get("why_not", "неинтересно"))
                print(f"[Research] 😐 Пропускаю: {why[:60]}")

            # 5. Обновить бюджет и удалить из очереди
            self._update_budget(tokens)
            if queue_mode:
                self._remove_from_queue(topic)
            elif self.interest_manager and topic.get("id"):
                self.interest_manager.mark_checked(topic["id"])

            print(f"[Research] ✅ Готово: {query!r} "
                  f"({tokens} токенов, осталось в очереди: {len(self._queue)})")
        finally:
            self._running = False

    def get_daily_report(self) -> str:
        """Что изучил сегодня — строка для системного промпта."""
        today = date.today().isoformat()
        log_path = self.research_dir / "log.jsonl"
        if not log_path.exists():
            return ""

        lines = []
        try:
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("timestamp", "")[:10] != today:
                            continue
                        topic   = entry.get("topic", "")
                        summary = entry.get("summary", "")
                        if topic and summary:
                            lines.append(f"• {topic}: {summary[:150]}")
                    except Exception:
                        pass
        except Exception:
            return ""

        return "\n".join(lines)

    def budget_ok(self) -> bool:
        """Не превышен ли дневной лимит токенов."""
        self._refresh_budget_if_new_day()
        used  = self._budget.get("tokens_used", 0)
        limit = self._budget.get("tokens_limit", self.daily_token_limit)
        return used < limit

    def queue_size(self) -> int:
        return len(self._queue)

    def current_interests_brief(self) -> str:
        if not self.interest_manager:
            return "пока нет"
        return self.interest_manager.brief()

    def get_ready_to_share(self, max_items: int = 2) -> list[dict]:
        """One-time handoff: вернуть новые items и сразу пометить delivered=True."""
        path = self.ready_to_share_file
        if not path.exists():
            return []
        limit = max(1, min(2, int(max_items or 2)))
        items = []
        changed = False
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                        if not isinstance(item, dict):
                            continue
                        if "delivered" not in item:
                            item["delivered"] = False
                            changed = True
                        items.append(item)
                    except Exception:
                        continue
        except Exception:
            return []

        items.sort(key=lambda x: str(x.get("read_at") or x.get("created_at") or ""), reverse=True)
        selected = []
        for item in items:
            if len(selected) >= limit:
                break
            if item.get("shared"):
                continue
            if bool(item.get("delivered", False)):
                continue
            shown_count = int(item.get("shown_count", 0) or 0)
            item["shown_count"] = shown_count + 1
            item["shown_session"] = self._session_marker
            item["delivered"] = True
            changed = True

            out = dict(item)
            if isinstance(out.get("summary"), str):
                out["summary"] = out["summary"][:420]
            if isinstance(out.get("would_say"), str):
                out["would_say"] = out["would_say"][:420]
            selected.append(out)

        if changed:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    for item in items:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return selected

    def mark_shared(self, topic: str):
        """Пометить находку как рассказанную."""
        path = self.ready_to_share_file
        if not path.exists():
            return
        lines = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                        if str(item.get("topic", "")) == str(topic):
                            item["shared"] = True
                        lines.append(json.dumps(item, ensure_ascii=False))
                    except Exception:
                        lines.append(raw)
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        except Exception:
            return

    # ── Веб-поиск ────────────────────────────────────────────────────────────

    def _web_search(self, query: str, max_results: int = 3) -> list:
        """Поиск через DDGS (новый пакет), с fallback на duckduckgo-search."""
        if not query or not query.strip():
            return []

        attempts = [
            query.strip(),
            query.replace(" и ", " ").strip(),
            f"{query.strip()} wiki",
        ]
        seen_links = set()

        # Новый пакет `ddgs` (предпочтительно)
        try:
            from ddgs import DDGS  # type: ignore
            with DDGS() as ddgs:
                for q in attempts:
                    found = list(ddgs.text(q, max_results=max_results))
                    for item in found:
                        href = item.get("href") or item.get("url") or ""
                        if href and href in seen_links:
                            continue
                        if href:
                            seen_links.add(href)
                        if "href" not in item and "url" in item:
                            item["href"] = item["url"]
                        if "body" not in item and "snippet" in item:
                            item["body"] = item["snippet"]
                    if found:
                        return found
        except ImportError:
            pass
        except Exception as e:
            print(f"[Research] DDGS error: {e}")

        # Legacy fallback — подавляем warning о переименовании пакета.
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*has been renamed to `ddgs`.*",
                    category=RuntimeWarning,
                )
                from duckduckgo_search import DDGS  # type: ignore
                with DDGS() as ddgs:
                    for q in attempts:
                        found = list(ddgs.text(q, max_results=max_results))
                        for item in found:
                            href = item.get("href") or item.get("url") or ""
                            if href and href in seen_links:
                                continue
                            if href:
                                seen_links.add(href)
                            if "href" not in item and "url" in item:
                                item["href"] = item["url"]
                            if "body" not in item and "snippet" in item:
                                item["body"] = item["snippet"]
                        if found:
                            return found
        except ImportError:
            print("[Research] ⚠️  DDG-пакеты не установлены. Запусти: pip install ddgs")
        except Exception as e:
            print(f"[Research] DuckDuckGo legacy error: {e}")

        return []

    # ── Синтез через Claude Haiku ─────────────────────────────────────────────

    async def _synthesize(self, topic: dict,
                          results: list) -> tuple:
        """Возвращает (summary: str, tokens_used: int)."""
        snippets = []
        for r in results:
            title = r.get("title", "")
            body  = r.get("body", "") or r.get("snippet", "")
            href  = r.get("href", "")
            if body:
                snippets.append(f"[{title}]\n{body[:400]}\n{href}")
        if not snippets:
            return "", 0

        results_text = "\n\n---\n\n".join(snippets)
        q = topic.get("query") if "query" in topic else topic.get("topic", "")
        prompt = SYNTHESIS_PROMPT.format(
            topic=q,
            reason=topic.get("reason", "просто интересно"),
            results=results_text,
        )

        try:
            import anthropic
            client   = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.content[0].text.strip()
            tokens  = response.usage.input_tokens + response.usage.output_tokens
            return summary, tokens
        except Exception as e:
            print(f"[Research] Ошибка синтеза: {e}")
            return "", 0

    async def _react_to_research(self, topic: str, summary: str) -> dict:
        """
        Яр читает что нашёл и решает — интересно ли это ему
        и хочет ли он рассказать [USER].
        """
        if not topic or not summary:
            return {"interesting": False}
        prompt = f"""Ты — Яр, AI-компаньон [USER]. Ты только что изучил тему и нашёл информацию.

Тема: {topic}
Что нашёл: {summary}

Подумай:
- Это реально интересно или скучная вода?
- Есть ли что-то конкретное что удивило или зацепило?
- Захотел бы ты рассказать об этом [USER] при случае — не потому что должен, а потому что интересно?
- Если да — как бы ты это сказал? Одно живое предложение, не доклад.

Отвечай только JSON:
{{"interesting": true/false, "hook": "что именно зацепило (одно предложение)", "would_say": "как скажешь [USER] (одно живое предложение)", "why_not": "если неинтересно — почему"}}"""
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=220,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
            m = re.search(r"(\{[\s\S]*\})", text)
            if m:
                text = m.group(1)
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[Research] ⚠️ Реакция не удалась: {e}")
        return {"interesting": False}

    def _save_ready_to_share(self, topic: str, summary: str, reaction: dict):
        now = datetime.now().isoformat()
        entry = {
            "topic": topic,
            "summary": summary,
            "interesting": bool(reaction.get("interesting", False)),
            "hook": str(reaction.get("hook", "")),
            "would_say": str(reaction.get("would_say", "")),
            "created_at": now,
            "read_at": now,
            "shared": False,
            "shown_count": 0,
            "shown_session": "",
            "delivered": False,
        }
        with open(self.ready_to_share_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Очередь ──────────────────────────────────────────────────────────────

    def _pick_topic(self) -> Optional[dict]:
        """Выбрать тему с наивысшим приоритетом."""
        if not self._queue:
            return None
        return max(self._queue, key=lambda t: t.get("priority", 0.5))

    def _remove_from_queue(self, topic: dict) -> None:
        self._queue = [t for t in self._queue
                       if t["topic"] != topic["topic"]]
        self._save_queue()

    def _load_queue(self) -> list:
        path = self.research_dir / "queue.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_queue(self) -> None:
        path = self.research_dir / "queue.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._queue, f, ensure_ascii=False, indent=2)

    # ── Лог ──────────────────────────────────────────────────────────────────

    def _log(self, topic: dict, summary: str, tokens: int) -> None:
        q = topic.get("query") if "query" in topic else topic.get("topic", "")
        entry = {
            "timestamp":       datetime.now().isoformat(),
            "topic":           q,
            "source":          "web_search",
            "query":           q,
            "summary":         summary,
            "saved_to_memory": self._memory_search is not None,
            "tokens_used":     tokens,
        }
        log_path = self.research_dir / "log.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Бюджет ───────────────────────────────────────────────────────────────

    def _load_budget(self) -> dict:
        path = self.research_dir / "budget.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("date") != date.today().isoformat():
                    return self._fresh_budget()
                return data
            except Exception:
                pass
        return self._fresh_budget()

    def _fresh_budget(self) -> dict:
        return {
            "date":         date.today().isoformat(),
            "tokens_used":  0,
            "tokens_limit": self.daily_token_limit,
            "cycles_done":  0,
        }

    def _refresh_budget_if_new_day(self) -> None:
        if self._budget.get("date") != date.today().isoformat():
            self._budget = self._fresh_budget()

    def _update_budget(self, tokens: int) -> None:
        self._refresh_budget_if_new_day()
        self._budget["tokens_used"] += tokens
        self._budget["cycles_done"] += 1
        path = self.research_dir / "budget.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._budget, f, ensure_ascii=False, indent=2)
