from datetime import datetime
from pathlib import Path
import json
import re
import uuid


class CognitiveProcessManager:
    def __init__(self, memory_dir: str):
        self.path = Path(memory_dir) / "continuity" / "cognitive_process.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            "threads": [],
            "intentions": [],
            "pending_syntheses": [],
            "last_idle_cycle_at": None,
        }
        self.load()

    def load(self):
        if not self.path.exists():
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {
                    "threads": raw.get("threads", []) if isinstance(raw.get("threads"), list) else [],
                    "intentions": raw.get("intentions", []) if isinstance(raw.get("intentions"), list) else [],
                    "pending_syntheses": raw.get("pending_syntheses", []) if isinstance(raw.get("pending_syntheses"), list) else [],
                    "last_idle_cycle_at": raw.get("last_idle_cycle_at"),
                }
        except Exception:
            self.data = {
                "threads": [],
                "intentions": [],
                "pending_syntheses": [],
                "last_idle_cycle_at": None,
            }
            self.save()

    def save(self):
        try:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        words = re.findall(r"[a-zA-Zа-яА-Я0-9_]+", str(text or "").lower())
        return {w for w in words if len(w) > 2}

    @staticmethod
    def _iso_now() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _hours_since(ts: str | None) -> float:
        if not ts:
            return 9999.0
        try:
            return max(0.0, (datetime.now() - datetime.fromisoformat(str(ts))).total_seconds() / 3600.0)
        except Exception:
            return 9999.0

    def _find_similar_thread(self, topic: str) -> dict | None:
        topic_l = str(topic or "").strip().lower()
        if not topic_l:
            return None
        for t in self.data.get("threads", []):
            if t.get("status") not in {"active", "cooling"}:
                continue
            t_topic = str(t.get("topic", "")).lower()
            if topic_l == t_topic or topic_l in t_topic or t_topic in topic_l:
                return t

        topic_tokens = self._tokenize(topic_l)
        best = None
        best_score = 0
        for t in self.data.get("threads", []):
            if t.get("status") not in {"active", "cooling"}:
                continue
            t_tokens = self._tokenize(t.get("topic", ""))
            inter = len(topic_tokens & t_tokens)
            if inter > best_score:
                best = t
                best_score = inter
        return best if best_score >= 2 else None

    def _extract_topic(self, user_text: str, active_loops: list[dict]) -> str:
        if active_loops:
            top = active_loops[0]
            if isinstance(top, dict) and top.get("topic"):
                return str(top["topic"])
        raw = str(user_text or "").strip()
        if not raw:
            return ""
        topic_raw = raw[:80].strip()
        cut = topic_raw.rfind(" ", 0, 60)
        return topic_raw[:cut].strip() if cut > 10 else topic_raw[:60].strip()

    def _apply_lifecycle_decay(self):
        changed = False
        for t in self.data.get("threads", []):
            if not isinstance(t, dict):
                continue
            status = t.get("status", "active")
            if status not in {"active", "cooling"}:
                continue
            importance = float(t.get("importance", 0.6))
            recurrence = int(t.get("progress_count", 1))
            hours_touch = self._hours_since(t.get("last_touched"))
            decay = max(0.004, 0.012 - importance * 0.006 - min(recurrence, 5) * 0.0008)
            t["tension"] = round(max(0.05, float(t.get("tension", 0.5)) - decay), 3)
            if status == "active" and hours_touch > 72 and float(t.get("tension", 0.0)) < 0.28:
                t["status"] = "cooling"
            elif status == "cooling" and hours_touch > 240 and float(t.get("tension", 0.0)) < 0.18:
                t["status"] = "archived"
            changed = True
        if changed:
            self.save()

    def update_from_interaction(self, user_text: str, active_loops: list[dict], bridge: dict | None = None) -> None:
        try:
            topic = self._extract_topic(user_text, active_loops or [])
            if not topic:
                return
            now = self._iso_now()
            thread = self._find_similar_thread(topic)
            concrete = len(str(user_text or "")) > 140 or any(ch.isdigit() for ch in str(user_text or ""))
            loop_ids = [str(l.get("id", "")) for l in (active_loops or []) if isinstance(l, dict)]
            rel_topics = sorted(list(self._tokenize(topic)))[:6]

            if thread:
                thread["updated_at"] = now
                thread["last_touched"] = now
                thread["tension"] = round(min(1.0, float(thread.get("tension", 0.5)) + 0.03), 3)
                if concrete:
                    thread["clarity"] = round(min(1.0, float(thread.get("clarity", 0.4)) + 0.04), 3)
                thread["latest_note"] = str(user_text or "").strip()[:180]
                thread["progress_count"] = int(thread.get("progress_count", 0)) + 1
                thread["status"] = "active"
                existing_loops = set(thread.get("related_loops", []) or [])
                thread["related_loops"] = list(existing_loops | set(loop_ids))[:6]
                existing_topics = set(thread.get("related_topics", []) or [])
                thread["related_topics"] = list(existing_topics | set(rel_topics))[:8]
            else:
                tension = 0.55
                importance = 0.6
                related_loops = []
                if active_loops:
                    top = active_loops[0] if isinstance(active_loops[0], dict) else {}
                    tension = float(top.get("tension", 0.55) or 0.55)
                    importance = float(top.get("importance", 0.6) or 0.6)
                    related_loops = [str(top.get("id", ""))] if top.get("id") else []

                new_thread = {
                    "id": "thread_" + uuid.uuid4().hex[:8],
                    "topic": topic[:160],
                    "status": "active",
                    "source": "interaction",
                    "tension": round(min(1.0, max(0.1, tension)), 3),
                    "clarity": 0.4,
                    "importance": round(min(1.0, max(0.1, importance)), 3),
                    "created_at": now,
                    "updated_at": now,
                    "last_progress_at": now,
                    "last_touched": now,
                    "next_internal_step": "уточнить практический следующий шаг",
                    "latest_note": str(user_text or "").strip()[:180],
                    "related_loops": related_loops,
                    "related_topics": rel_topics,
                    "progress_count": 1,
                }
                self.data.setdefault("threads", []).append(new_thread)

            active = [t for t in self.data.get("threads", []) if t.get("status") == "active"]
            if len(active) > 12:
                active_sorted = sorted(active, key=lambda x: float(x.get("importance", 0.0)))
                for t in active_sorted[:-12]:
                    t["status"] = "cooling"

            self._apply_lifecycle_decay()
            self.save()
        except Exception:
            pass

    def run_idle_cycle(self, active_loops: list[dict], recent_emotions: list[dict] | None = None, research_items: list[dict] | None = None) -> dict | None:
        try:
            if not active_loops and not self.get_top_threads(limit=2):
                return None
            top = self.get_top_threads(limit=2)
            if not top:
                return None

            now = self._iso_now()
            updated_threads = []
            new_intentions = []
            new_syntheses = []

            for t in top:
                tension = float(t.get("tension", 0.5))
                prog = int(t.get("progress_count", 1))
                if tension > 0.6 and prog >= 2:
                    t["clarity"] = round(min(1.0, float(t.get("clarity", 0.4)) + 0.05), 3)
                else:
                    t["clarity"] = round(min(1.0, float(t.get("clarity", 0.4)) + 0.02), 3)
                t["updated_at"] = now
                t["last_progress_at"] = now
                t["last_touched"] = now
                t["progress_count"] = int(t.get("progress_count", 0)) + 1
                t["latest_note"] = f"Внутренний прогресс по теме: {t.get('topic', '')}"[:180]
                t["next_internal_step"] = "сформулировать следующий практический шаг" if t["clarity"] < 0.65 else "сверить гипотезу с текущими open loops"
                updated_threads.append(str(t.get("id", "")))

                topic_l = str(t.get("topic", "")).lower()
                if any(k in topic_l for k in ["design", "архит", "bug", "баг", "gating", "интеграц"]):
                    exists = any(i.get("target") == t.get("topic") and i.get("status") == "active" for i in self.data.get("intentions", []))
                    if not exists:
                        intent = {
                            "id": "intent_" + uuid.uuid4().hex[:8],
                            "kind": "design",
                            "target": t.get("topic", "")[:120],
                            "priority": round(min(1.0, 0.55 + float(t.get("importance", 0.6)) * 0.4), 2),
                            "why": "актуальная архитектурная нитка требует внимания",
                            "created_at": now,
                            "updated_at": now,
                            "status": "active",
                        }
                        self.data.setdefault("intentions", []).append(intent)
                        new_intentions.append(intent["id"])

                if int(t.get("progress_count", 0)) >= 2:
                    synth_topic = str(t.get("topic", ""))[:140]
                    existing_s = None
                    for s in self.data.get("pending_syntheses", []):
                        if str(s.get("topic", "")) == synth_topic:
                            existing_s = s
                            break
                    fragment = str(t.get("latest_note", ""))[:120]
                    if existing_s:
                        fr = existing_s.get("fragments", []) or []
                        if fragment and fragment not in fr:
                            fr.append(fragment)
                        existing_s["fragments"] = fr[-4:]
                        existing_s["readiness"] = round(min(1.0, float(existing_s.get("readiness", 0.5)) + 0.05), 2)
                        existing_s["updated_at"] = now
                    else:
                        s_new = {
                            "topic": synth_topic,
                            "fragments": [fragment] if fragment else [],
                            "readiness": 0.55,
                            "updated_at": now,
                        }
                        self.data.setdefault("pending_syntheses", []).append(s_new)
                        new_syntheses.append(synth_topic)

            self.data["last_idle_cycle_at"] = now
            self._apply_lifecycle_decay()
            self.save()
            return {
                "updated_threads": updated_threads,
                "new_intentions": new_intentions,
                "new_syntheses": new_syntheses,
            }
        except Exception:
            return None

    def get_top_threads(self, limit: int = 3) -> list[dict]:
        threads = [t for t in self.data.get("threads", []) if t.get("status") == "active"]
        if not threads:
            return []

        def score(t: dict) -> float:
            tension = float(t.get("tension", 0.0))
            importance = float(t.get("importance", 0.0))
            clarity = float(t.get("clarity", 0.0))
            recency = max(0.0, 0.15 - min(0.15, self._hours_since(t.get("last_touched")) * 0.01))
            return tension * 0.45 + importance * 0.35 + clarity * 0.15 + recency

        return sorted(threads, key=score, reverse=True)[: max(1, int(limit))]

    def summarize_for_prompt(self, limit: int = 2) -> str:
        top = self.get_top_threads(limit=limit)
        if not top:
            return ""
        topics = [str(t.get("topic", "")).strip() for t in top if str(t.get("topic", "")).strip()]
        if not topics:
            return ""
        summary = "ТЕКУЩИЙ ВНУТРЕННИЙ ФОКУС: " + "; ".join(topics[:limit]) + "."
        return summary[:160]
