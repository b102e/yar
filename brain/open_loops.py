from pathlib import Path
from datetime import datetime
import json, uuid


class OpenLoopManager:
    VALID_STATUSES = {"open", "cooling", "resolved", "expired"}
    COMPLETION_SIGNALS = [
        "готово",
        "сделано",
        "починил",
        "исправил",
        "закрыли",
        "внедрил",
        "работает",
        "разобрался",
        "решено",
        "фикс готов",
    ]

    def __init__(self, memory_dir: str, identity=None):
        self.path = Path(memory_dir) / "continuity" / "open_loops.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self._load()

    def _load(self):
        if self.path.exists():
            if self.identity:
                from identity.encryption import decrypt_file
                self._data = decrypt_file(self.identity, self.path, default={"loops": []})
            else:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self._data = {"loops": []}
        self._data.setdefault("loops", [])
        for l in self._data["loops"]:
            if not isinstance(l, dict):
                continue
            status = str(l.get("status", "open"))
            if status not in self.VALID_STATUSES:
                l["status"] = "open"
            l.setdefault("last_prompted_at", None)
            l.setdefault("prompt_count", 0)

    def _save(self):
        if self.identity:
            from identity.encryption import encrypt_file
            encrypt_file(self.identity, self.path, self._data)
        else:
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _find_similar(self, topic: str) -> dict | None:
        topic_low = str(topic or "").lower()
        for l in self._data["loops"]:
            if l.get("status") not in {"open", "cooling"}:
                continue
            t = str(l.get("topic", "")).lower()
            if topic_low in t or t in topic_low:
                return l
        return None

    @staticmethod
    def _hours_since(ts: str | None) -> float:
        if not ts:
            return 9999.0
        try:
            dt = datetime.fromisoformat(str(ts))
            return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
        except Exception:
            return 9999.0

    def _score(self, loop: dict) -> float:
        tension = float(loop.get("tension", 0.0))
        importance = float(loop.get("importance", 0.0))
        recurrence = float(min(int(loop.get("recurrence", 1)), 5))
        hours_since_touch = self._hours_since(loop.get("last_touched"))
        recency_bonus = max(0.0, 0.12 - min(0.12, hours_since_touch * 0.005))
        prompt_penalty = min(0.12, float(loop.get("prompt_count", 0)) * 0.01)
        stale_penalty = min(0.15, max(0.0, (hours_since_touch - 72.0) / 240.0))
        return tension * 0.6 + importance * 0.25 + recurrence * 0.03 + recency_bonus - prompt_penalty - stale_penalty

    def add_or_update_loop(
        self,
        topic: str,
        source_text: str = "",
        tension: float = 0.5,
        importance: float = 0.5,
        emotional_weight: float = 0.0,
        why_open: str = "",
        next_possible_step: str = "",
        source: str = "unknown",
    ) -> dict:
        existing = self._find_similar(topic)
        now = datetime.now().isoformat()
        if existing:
            existing["recurrence"] = int(existing.get("recurrence", 1)) + 1
            existing["tension"] = round(min(1.0, float(existing.get("tension", tension)) + 0.05), 3)
            existing["importance"] = round(min(1.0, float(existing.get("importance", importance)) + 0.03), 3)
            existing["status"] = "open"
            existing["last_touched"] = now
            existing["updated_at"] = now
            if why_open:
                existing["why_open"] = why_open
            if next_possible_step:
                existing["next_possible_step"] = next_possible_step
            self._save()
            return existing

        loop = {
            "id": "loop_" + str(uuid.uuid4())[:6],
            "topic": str(topic)[:120],
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "last_touched": now,
            "last_prompted_at": None,
            "prompt_count": 0,
            "source": source,
            "tension": round(min(1.0, float(tension)), 3),
            "importance": round(min(1.0, float(importance)), 3),
            "recurrence": 1,
            "emotional_weight": round(float(emotional_weight), 3),
            "why_open": why_open,
            "next_possible_step": next_possible_step,
            "resolved_at": None,
            "resolution_note": None,
        }
        self._data["loops"].append(loop)
        active = [l for l in self._data["loops"] if l.get("status") in {"open", "cooling"}]
        if len(active) > 50:
            weakest = min(active, key=lambda l: (float(l.get("importance", 0.0)), str(l.get("created_at", ""))))
            weakest["status"] = "expired"
        self._save()
        return loop

    def resolve_loop(self, loop_id: str, resolution_note: str = "") -> bool:
        for l in self._data["loops"]:
            if l.get("id") == loop_id:
                l["status"] = "resolved"
                l["resolved_at"] = datetime.now().isoformat()
                l["resolution_note"] = resolution_note
                self._save()
                return True
        return False

    def resolve_by_topic(self, topic: str, resolution_note: str = "") -> bool:
        loop = self._find_similar(topic)
        if loop:
            return self.resolve_loop(loop["id"], resolution_note)
        return False

    def soft_resolve_by_topic(self, topic: str, resolution_note: str = "") -> bool:
        loop = self._find_similar(topic)
        if not loop:
            return False
        now = datetime.now().isoformat()
        status = loop.get("status", "open")
        if status == "open":
            loop["status"] = "cooling"
            loop["tension"] = round(max(0.15, float(loop.get("tension", 0.5)) - 0.2), 3)
            loop["updated_at"] = now
            if resolution_note:
                loop["resolution_note"] = resolution_note
            self._save()
            return True
        if status == "cooling":
            return self.resolve_loop(str(loop.get("id", "")), resolution_note)
        return False

    def get_active_loops(self, limit: int = 10) -> list[dict]:
        eligible = []
        for l in self._data["loops"]:
            status = l.get("status")
            if status == "open":
                eligible.append(l)
            elif status == "cooling" and float(l.get("tension", 0.0)) >= 0.75:
                eligible.append(l)
        ranked = sorted(eligible, key=self._score, reverse=True)
        return ranked[:limit]

    def get_top_tensions(self, limit: int = 5) -> list[dict]:
        return self.get_active_loops(limit=limit)

    def mark_prompted(self, loop_ids: list[str]) -> None:
        ids = {str(i) for i in (loop_ids or []) if i}
        if not ids:
            return
        now = datetime.now().isoformat()
        changed = False
        for l in self._data["loops"]:
            if str(l.get("id", "")) in ids:
                l["last_prompted_at"] = now
                l["prompt_count"] = int(l.get("prompt_count", 0)) + 1
                changed = True
        if changed:
            self._save()

    def decay_loops(self, hours_passed: float = 24.0) -> None:
        days_silent = max(0.0, float(hours_passed)) / 24.0
        now = datetime.now()
        changed = False
        for l in self._data["loops"]:
            status = l.get("status")
            if status not in {"open", "cooling"}:
                continue

            importance = float(l.get("importance", 0.5))
            recurrence = int(l.get("recurrence", 1))
            tension = float(l.get("tension", 0.5))
            last_touched = l.get("last_touched")
            age_days = 9999.0
            try:
                age_days = max(0.0, (now - datetime.fromisoformat(str(last_touched))).total_seconds() / 86400.0)
            except Exception:
                pass

            base_decay = 0.03 * days_silent
            protection = importance * 0.02 + min(recurrence, 5) * 0.005
            decay = max(0.005, base_decay - protection)
            if importance >= 0.9 and recurrence >= 3:
                decay = min(decay, 0.01 * days_silent)
            tension = max(0.0, tension - decay)
            l["tension"] = round(tension, 3)

            if l.get("status") == "open" and age_days > 7 and tension < 0.3:
                l["status"] = "cooling"
            if l.get("status") == "cooling" and age_days > 14 and tension < 0.2:
                l["status"] = "expired"
            changed = True

        if changed:
            self._save()

    def detect_resolution_from_text(self, text: str) -> dict | None:
        text_low = str(text or "").lower()
        if not text_low:
            return None
        if not any(sig in text_low for sig in self.COMPLETION_SIGNALS):
            return None

        candidate = None
        for l in self._data.get("loops", []):
            if l.get("status") not in {"open", "cooling"}:
                continue
            topic_low = str(l.get("topic", "")).lower()
            if not topic_low:
                continue
            if topic_low in text_low:
                candidate = l
                break
            words = [w for w in topic_low.split() if len(w) > 4][:4]
            if any(w in text_low for w in words):
                candidate = l
                break

        if not candidate:
            candidate = self._find_similar(text_low)
        if not candidate:
            return None

        now = datetime.now().isoformat()
        note = f"completion-signal: {text[:160].strip()}"
        status = candidate.get("status", "open")
        tension = float(candidate.get("tension", 0.5))

        if status == "open":
            candidate["resolution_note"] = note
            candidate["updated_at"] = now
            if tension > 0.35:
                candidate["status"] = "cooling"
                candidate["tension"] = round(max(0.15, tension - 0.2), 3)
            else:
                candidate["status"] = "resolved"
                candidate["resolved_at"] = now
            self._save()
            return candidate

        if status == "cooling":
            candidate["resolution_note"] = note
            candidate["resolved_at"] = now
            candidate["status"] = "resolved"
            candidate["updated_at"] = now
            self._save()
            return candidate

        return None

    def extract_from_text(self, text: str, source: str = "user_message") -> dict | None:
        triggers = [
            "нужно", "не хватает", "проблема", "узкое место", "как сделать",
            "что добавить", "следующий шаг", "не завершено", "хочу чтобы",
            "надо продумать", "нужен", "не работает", "баг",
            "нужна", "непонятно", "стоит разобраться",
        ]
        text_low = str(text).lower()
        has_trigger = any(t in text_low for t in triggers)
        if not has_trigger and len(text) < 300:
            return None
        topic_raw = text[:80].strip()
        cut = topic_raw.rfind(" ", 0, 60)
        topic = topic_raw[:cut].strip() if cut > 10 else topic_raw[:60].strip()
        if not topic:
            return None
        tension = 0.6 if has_trigger else 0.45
        return self.add_or_update_loop(
            topic=topic,
            source_text=text[:200],
            tension=tension,
            source=source,
        )

    def summarize_active_loops(self, limit: int = 3) -> str:
        loops = self.get_active_loops(limit=limit)
        if not loops:
            return ""
        lines = []
        for l in loops:
            why = l.get("why_open", "")
            rec = l.get("recurrence", 1)
            t = l.get("tension", 0)
            line = f"• {l['topic']} (tension={t}, ×{rec})"
            if why:
                line += f": {why[:80]}"
            lines.append(line)
        return "\n".join(lines)
