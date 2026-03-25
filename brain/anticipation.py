from pathlib import Path
from datetime import datetime
import json


class AnticipationManager:
    def __init__(self, memory_dir: str, identity=None):
        self.path = Path(memory_dir) / "continuity" / "anticipation.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity

    def build_forecast(
        self,
        active_loops: list[dict],
        recent_topics: list[str],
        interests: list[str],
        identity_snapshot: dict | None = None,
        source: str = "post_session",
    ) -> dict:
        # expected_next_topics: loops с tension > 0.75
        high_tension = [l["topic"] for l in active_loops if l.get("tension", 0) > 0.75][:3]

        # recurring: темы встречавшиеся 2+ раза в recent_topics
        seen = {}
        for t in recent_topics:
            seen[t] = seen.get(t, 0) + 1
        recurring = [t for t, c in seen.items() if c >= 2][:2]

        expected = list(dict.fromkeys(high_tension + recurring))[:4]

        # reentry_mode
        all_topics_str = " ".join(expected + interests).lower()
        if any(w in all_topics_str for w in ["архитект", "код", "баг", "модул", "интеграц"]):
            reentry_mode = "resume_deep_architecture_work"
        elif any(w in all_topics_str for w in ["дзогч", "практик", "медит", "буддизм"]):
            reentry_mode = "philosophical_exploration"
        elif any(w in all_topics_str for w in ["песн", "suno", "музык", "текст"]):
            reentry_mode = "creative_work"
        else:
            reentry_mode = "general_conversation"

        intent = []
        if high_tension:
            intent.append(f"подсветить незавершённую линию: {high_tension[0]}")
        if reentry_mode == "resume_deep_architecture_work":
            intent.append("быть готовым к техническим деталям")
        elif reentry_mode == "philosophical_exploration":
            intent.append("войти в медленный созерцательный режим")

        confidence = round(min(0.92, 0.5 + len(high_tension) * 0.1 + len(recurring) * 0.08), 2)

        forecast = {
            "timestamp": datetime.now().isoformat(),
            "expected_next_topics": expected,
            "reentry_mode_prediction": reentry_mode,
            "preparatory_intent": intent,
            "confidence": confidence,
            "source": source
        }

        with open(self.path, "ab") as f:
            if self.identity:
                from identity.encryption import encrypt_line
                f.write(encrypt_line(self.identity, forecast) + b"\n")
            else:
                f.write(json.dumps(forecast, ensure_ascii=False).encode() + b"\n")

        return forecast

    def get_latest_forecast(self) -> dict | None:
        if not self.path.exists():
            return None
        with open(self.path, "rb") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return None
        try:
            raw = lines[-1]
            if self.identity and raw[:1] not in (b"{", b"["):
                from identity.encryption import decrypt_line
                return decrypt_line(self.identity, raw)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def summarize_forecast(self) -> str:
        f = self.get_latest_forecast()
        if not f:
            return ""
        topics = f.get("expected_next_topics", [])
        mode = f.get("reentry_mode_prediction", "")
        first_topic = topics[0] if topics else ""
        result = f"Ожидаю: {first_topic}. Режим: {mode}." if first_topic else f"Режим: {mode}."
        return result[:130]
