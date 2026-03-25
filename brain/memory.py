"""
Долгосрочная память — хранится в ~/claude-memory/
Структура:
  ~/claude-memory/
    memory.json       — факты, сессии, предпочтения
    conversations/    — каждый разговор отдельным файлом
    observations/     — что видел через камеру
    thoughts/         — автономные мысли между разговорами
"""

import json
import re
import time
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import Optional

from brain.memory_lifecycle import MemoryLifecycleManager


class Memory:
    MAX_SHORT_TERM = 30

    def __init__(self, base_dir: str = "~/claude-memory", identity=None):
        self.memory_dir = Path(base_dir).expanduser()
        self.identity = identity
        self._setup_dirs()

        self.short_term: list[dict] = []
        self.long_term: dict = self._load()
        self._session_start = datetime.now()
        self._search   = None  # подключается через set_search() после инициализации
        self._episodic = None  # подключается через set_episodic() после инициализации
        self.consolidation = None  # подключается через set_consolidation()
        self.lifecycle = MemoryLifecycleManager(str(self.memory_dir), memory=self)
        self._migrate_fact_lifecycle_schema()
        self._restore_checkpoint()

    def set_search(self, search) -> None:
        """Подключить семантический поиск (MemorySearch)."""
        self._search = search

    def set_episodic(self, episodic) -> None:
        """Подключить эпизодическую память (EpisodicMemory)."""
        self._episodic = episodic

    def set_consolidation(self, consolidation) -> None:
        """Подключить консолидацию памяти (MemoryConsolidation)."""
        self.consolidation = consolidation

    def _setup_dirs(self):
        for subdir in ["", "conversations", "observations", "thoughts"]:
            (self.memory_dir / subdir).mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        path = self.memory_dir / "memory.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                print(f"✅ Память загружена: {path}")
                return data
        print(f"🆕 Новая память создана: {path}")
        return {
            "owner_name": "[USER]",
            "created": datetime.now().isoformat(),
            "sessions": [],
            "facts": [],
            "preferences": {},
            "mood_history": [],
        }

    def save(self):
        """Сохраняем всё"""
        # Сохраняем текущую сессию
        if self.short_term:
            highlights = self._extract_highlights()
            session = {
                "date":         self._session_start.isoformat(),
                "duration_min": int((datetime.now() - self._session_start).seconds / 60),
                "exchanges":    len([m for m in self.short_term if m["role"] == "user"]),
                "highlights":   highlights,
            }
            # Индексируем highlights сессии в семантический поиск
            if self._search:
                date = self._session_start.date().isoformat()
                for h in highlights:
                    if h:
                        self._search.add(h, "highlight", date)

            # Антидублирование: save() может вызываться многократно в одной сессии.
            # Не добавляем повтор, если последняя запись уже за ту же минуту.
            sessions = self.long_term.get("sessions", [])
            cur_minute = str(session.get("date", ""))[:16]
            last_minute = str(sessions[-1].get("date", ""))[:16] if sessions else ""
            if cur_minute != last_minute:
                sessions.append(session)
            self.long_term["sessions"] = sessions
            self.long_term["sessions"] = self.long_term["sessions"][-200:]

            # Сохраняем полный разговор отдельным файлом
            conv_file = self.memory_dir / "conversations" / f"{self._session_start.strftime('%Y-%m-%d_%H-%M')}.json"
            with open(conv_file, "w", encoding="utf-8") as f:
                json.dump({
                    "date":     self._session_start.isoformat(),
                    "messages": self.short_term,
                }, f, ensure_ascii=False, indent=2)

        self._absorb_promoted_hypotheses()
        self._write_memory_json()
        if self.identity:
            try:
                from chain.writer import write_entry
                write_entry(self.identity, {
                    "event": "session_save",
                    "message_count": len(self.short_term),
                }, "session")
            except Exception as _ce:
                print(f"[Chain] session_save entry skipped: {_ce}")

    def checkpoint(self):
        """Периодический автосейв short_term для восстановления после падения."""
        if not self.short_term:
            return
        payload = {
            "session_start": self._session_start.isoformat(),
            "saved_at": datetime.now().isoformat(),
            "messages": self.short_term[-self.MAX_SHORT_TERM:],
        }
        cp = self.memory_dir / "conversations" / "checkpoint.json"
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _restore_checkpoint(self):
        """Восстановить short_term из conversations/checkpoint.json (если есть)."""
        cp = self.memory_dir / "conversations" / "checkpoint.json"
        if not cp.exists():
            return
        try:
            with open(cp, encoding="utf-8") as f:
                data = json.load(f)
            restored = data.get("messages", [])
            if isinstance(restored, list) and restored:
                self.short_term = restored[-self.MAX_SHORT_TERM:]
                ts = data.get("session_start")
                if ts:
                    try:
                        self._session_start = datetime.fromisoformat(ts)
                    except Exception:
                        self._session_start = datetime.now()
                print(f"[Memory] ♻️  Восстановил checkpoint: {len(self.short_term)} сообщений")
            cp.unlink(missing_ok=True)
        except Exception as e:
            print(f"[Memory] checkpoint restore error: {e}")

    def save_final(self) -> None:
        """Вызывать ОДИН РАЗ при завершении сессии (вместо save()).
        Записывает эпизод через Claude Haiku, затем сохраняет JSON.
        save() вызывается несколько раз за сессию (из add_fact и т.д.) —
        туда API-вызов ставить нельзя."""
        if self._episodic and self.short_term:
            duration = int((datetime.now() - self._session_start).seconds / 60)
            self._episodic.record_episode(
                self.short_term,
                state_snapshot=None,
                duration_min=duration,
            )
        self.save()
        if self.lifecycle:
            try:
                report = self.lifecycle.run_maintenance_cycle()
                self._write_memory_json()
                print(
                    "[MemoryLifecycle] "
                    f"tiers core={report.get('core', 0)} "
                    f"active={report.get('active', 0)} "
                    f"archived={report.get('archived', 0)} "
                    f"stale={report.get('stale', 0)}"
                )
            except Exception as e:
                print(f"[MemoryLifecycle] maintenance skipped: {e}")
        if self._search:
            try:
                self._search.reindex_all()
            except Exception as e:
                print(f"[Memory] search reindex error: {e}")

    def _extract_highlights(self) -> list[str]:
        """Первые слова каждого сообщения пользователя"""
        return [
            m["content"][:80]
            for m in self.short_term
            if m["role"] == "user"
        ][:5]

    def add(self, role: str, content: str, meta: dict = None):
        entry = {
            "role": role,
            "content": content,
            "time": time.time(),
            "ts": datetime.now().strftime("%H:%M"),
        }
        if meta:
            entry["meta"] = meta
        self.short_term.append(entry)
        if len(self.short_term) > self.MAX_SHORT_TERM:
            self.short_term = self.short_term[-self.MAX_SHORT_TERM:]

    def add_fact(self, fact: str,
                 emotional_weight: float = None,
                 emotional_tags: list = None,
                 context: str = None,
                 confidence: float = None,
                 save_now: bool = True):
        """Claude узнал что-то о владельце"""
        fact_clean = (fact or "").strip()
        if not fact_clean:
            return

        date = datetime.now().isoformat()
        norm_new = self._normalize_fact(fact_clean)
        norm_tags_new = self._normalize_tags(emotional_tags)
        weight_new = self._normalize_weight(emotional_weight)
        context_new = (context or "").strip() or None
        conf_new = self._normalize_confidence(confidence)

        best_idx = None
        best_score = 0.0
        for i, existing in enumerate(self.long_term.get("facts", [])):
            old_text = str(existing.get("fact", ""))
            norm_old = self._normalize_fact(old_text)
            if not norm_old:
                continue
            score = SequenceMatcher(None, norm_old, norm_new).ratio()
            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx is not None and best_score >= 0.85:
            existing = self.long_term["facts"][best_idx]
            existing["fact"] = fact_clean
            existing["date"] = existing.get("date") or date
            existing["last_confirmed"] = date
            existing["confirmations"] = int(existing.get("confirmations", 1)) + 1
            # Сырые точки динамики веса для последующего timeline-анализа.
            if existing.get("emotional_weight") is not None:
                existing.setdefault("weight_history", []).append({
                    "date": existing.get("last_confirmed") or date,
                    "weight": existing.get("emotional_weight"),
                })
            self._merge_emotional(existing, weight_new, norm_tags_new, context_new)
            if conf_new is not None:
                prev_conf = self._normalize_confidence(existing.get("confidence"))
                existing["confidence"] = conf_new if prev_conf is None else round((prev_conf + conf_new) / 2.0, 3)
            existing["updated_at"] = date
            if self.lifecycle:
                self.lifecycle.update_fact_confirmation(existing, confidence_hint=conf_new)
                existing["tier"] = self.lifecycle.classify_fact(existing)
            if self.identity:
                try:
                    from chain.writer import write_entry
                    write_entry(self.identity, {"fact": fact_clean, "action": "update", "similarity": round(best_score, 3)}, "fact")
                except Exception as _ce:
                    print(f"[Chain] fact update entry skipped: {_ce}")
            if save_now:
                self.save()
            print(f"[Memory] 🔁 Обновил факт (similarity={best_score:.2f}): {fact_clean}")
        else:
            item = {
                "fact": fact_clean,
                "date": date,
                "last_confirmed": date,
                "confirmations": 1,
                "emotional_weight": weight_new,
                "emotional_tags": norm_tags_new,
                "context": context_new,
                "tier": "active",
                "fact_status": "confirmed",
                "created_at": date,
                "updated_at": date,
                "last_confirmed_at": date,
                "last_used_at": None,
                "use_count": 0,
                "identity_relevance": 0.0,
                "project_relevance": 0.0,
                "archival_reason": None,
            }
            if conf_new is not None:
                item["confidence"] = conf_new
            if self.lifecycle:
                item["tier"] = self.lifecycle.classify_fact(item)
            self.long_term["facts"].append(item)
            if self.identity:
                try:
                    from chain.writer import write_entry
                    write_entry(self.identity, {"fact": fact_clean, "action": "new"}, "fact")
                except Exception as _ce:
                    print(f"[Chain] fact new entry skipped: {_ce}")
            if save_now:
                self.save()
            print(f"[Memory] 💡 Запомнил: {fact_clean}")

        if self._search:
            self._search.add(fact_clean, "fact", date[:10])
        if self.consolidation:
            self.consolidation.add_raw_fact(fact_clean, source="memory_add")

    def _absorb_promoted_hypotheses(self) -> None:
        promoted_path = self.memory_dir / "hypotheses_promoted.jsonl"
        if not promoted_path.exists():
            return
        processed = 0
        try:
            with open(promoted_path, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except Exception:
                        continue
                    fact_text = str(entry.get("fact", "")).strip()
                    if not fact_text:
                        continue
                    self.add_fact(
                        fact_text,
                        emotional_weight=entry.get("emotional_weight"),
                        emotional_tags=entry.get("emotional_tags", []),
                        context=entry.get("context"),
                        confidence=entry.get("confidence"),
                        save_now=False,
                    )
                    processed += 1
            promoted_path.unlink(missing_ok=True)
            if processed:
                print(f"[Memory] 🔬 Поглотил подтверждённых гипотез: {processed}")
        except Exception as e:
            print(f"[Memory] promoted hypotheses absorb error: {e}")

    def add_thought(self, thought: str):
        """Автономная мысль"""
        date = datetime.now()
        thought_file = self.memory_dir / "thoughts" / f"{date.strftime('%Y-%m-%d')}.txt"
        with open(thought_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{date.strftime('%H:%M')}] {thought}\n")
        if self._search:
            self._search.add(thought, "thought", date.strftime("%Y-%m-%d"))

    def add_observation(self, description: str, image_path: str = None):
        """Что увидел в камеру"""
        date = datetime.now()
        obs_file = self.memory_dir / "observations" / f"{date.strftime('%Y-%m-%d')}.txt"
        with open(obs_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{date.strftime('%H:%M')}] {description}\n")
        if self._search:
            self._search.add(description, "observation", date.strftime("%Y-%m-%d"))

    def add_mood(self, mood_point: dict):
        """Сохранить точку голосового состояния в memory.json и mood_history.jsonl."""
        if not mood_point:
            return
        entry = dict(mood_point)
        entry.setdefault("timestamp", datetime.now().isoformat())

        # memory.json
        self.long_term.setdefault("mood_history", [])
        self.long_term["mood_history"].append(entry)
        self.long_term["mood_history"] = self.long_term["mood_history"][-2000:]
        with open(self.memory_dir / "memory.json", "w", encoding="utf-8") as f:
            json.dump(self.long_term, f, ensure_ascii=False, indent=2)

        # mood_history.jsonl
        path = self.memory_dir / "mood_history.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_context_messages(self) -> list[dict]:
        # Observations идут как assistant — Яр помнит что видел.
        # Мержим подряд идущие сообщения одной роли: Anthropic API требует чередования.
        merged: list[dict] = []
        for m in self.short_term:
            role = m["role"]
            content = m["content"]
            if role == "assistant":
                content = self._strip_command_blocks(str(content))
                if not content:
                    continue
            entry = {"role": role, "content": content}
            if merged and merged[-1]["role"] == entry["role"]:
                merged[-1]["content"] += "\n" + entry["content"]
            else:
                merged.append(entry)
        return merged

    def get_long_term_summary(self) -> str:
        selected_facts = []
        if self.lifecycle:
            try:
                selected_facts = self.lifecycle.select_relevant_facts_for_summary(limit=15)
            except Exception as e:
                print(f"[MemoryLifecycle] summary select fallback: {e}")
        if not selected_facts:
            selected_facts = self.long_term.get("facts", [])[-15:]

        facts = [self._format_fact_with_emotion(f) for f in selected_facts]
        sessions = self.long_term.get("sessions", [])
        total = len(sessions)
        recent = sessions[-3:] if sessions else []

        parts = []
        if total:
            parts.append(f"Мы общаемся уже {total} сессий.")
        if facts:
            parts.append("Что я знаю о тебе: " + "; ".join(facts))
        if recent:
            highlights = []
            for s in recent:
                h = s.get("highlights", [])
                if h:
                    highlights.append(h[0])
            if highlights:
                parts.append("Недавно говорили о: " + "; ".join(highlights))

        return "\n".join(parts) if parts else "Это наша первая встреча."

    def get_todays_thoughts(self) -> str:
        thought_file = self.memory_dir / "thoughts" / f"{datetime.now().strftime('%Y-%m-%d')}.txt"
        if thought_file.exists():
            return thought_file.read_text(encoding="utf-8")[-500:]
        return ""

    @property
    def owner_name(self) -> str:
        return self.long_term.get("owner_name", "[USER]")

    @staticmethod
    def _normalize_fact(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _normalize_tags(tags) -> list:
        if not tags:
            return []
        out = []
        for t in tags:
            s = str(t).strip().lower()
            if s and s not in out:
                out.append(s)
        return out

    @staticmethod
    def _normalize_weight(weight):
        if weight is None:
            return None
        try:
            w = float(weight)
        except Exception:
            return None
        return max(-1.0, min(1.0, w))

    @staticmethod
    def _normalize_confidence(confidence):
        if confidence is None:
            return None
        try:
            c = float(confidence)
        except Exception:
            return None
        return max(0.0, min(1.0, c))

    @staticmethod
    def _merge_emotional(target: dict, new_weight, new_tags: list, new_context: Optional[str]) -> None:
        # Обязательная обратная совместимость со старыми записями.
        old_weight = target.get("emotional_weight")
        old_tags = target.get("emotional_tags", []) or []
        old_context = target.get("context")

        if new_weight is not None:
            if old_weight is None:
                target["emotional_weight"] = new_weight
            else:
                target["emotional_weight"] = round((float(old_weight) + float(new_weight)) / 2.0, 3)
        else:
            target.setdefault("emotional_weight", old_weight if old_weight is not None else None)

        merged_tags = []
        for tag in list(old_tags) + list(new_tags or []):
            t = str(tag).strip().lower()
            if t and t not in merged_tags:
                merged_tags.append(t)
        target["emotional_tags"] = merged_tags

        if new_context:
            if not old_context:
                target["context"] = new_context
            elif new_context not in old_context:
                target["context"] = f"{old_context} | {new_context}"
        else:
            target.setdefault("context", old_context if old_context else None)

    @staticmethod
    def _format_fact_with_emotion(fact_entry: dict) -> str:
        text = str(fact_entry.get("fact", "")).strip()
        if not text:
            return ""
        tags = fact_entry.get("emotional_tags") or []
        context = (fact_entry.get("context") or "").strip()
        if tags or context:
            tags_part = ", ".join(str(t) for t in tags) if tags else "эмоции не указаны"
            ctx_part = context if context else "контекст не указан"
            return f"{text} [{tags_part} — {ctx_part}]"
        return text

    @staticmethod
    def _strip_command_blocks(text: str) -> str:
        # Убираем JSON-блоки: однострочные и с одним уровнем вложенности.
        text = re.sub(r'\{[^{}]*\}', '', text or '')
        text = re.sub(r'\{[^{}]*\{[^{}]*\}[^{}]*\}', '', text)
        lines = [l for l in text.split('\n') if l.strip()]
        return '\n'.join(lines).strip()

    def _write_memory_json(self) -> None:
        with open(self.memory_dir / "memory.json", "w", encoding="utf-8") as f:
            json.dump(self.long_term, f, ensure_ascii=False, indent=2)

    def _migrate_fact_lifecycle_schema(self) -> None:
        if not self.lifecycle:
            return
        try:
            report = self.lifecycle.migrate_facts_schema()
            self._write_memory_json()
            print(
                "[MemoryLifecycle] миграция facts: "
                f"core={report.get('core', 0)} "
                f"active={report.get('active', 0)} "
                f"archived={report.get('archived', 0)} "
                f"stale={report.get('stale', 0)}"
            )
        except Exception as e:
            print(f"[MemoryLifecycle] migration skipped: {e}")
