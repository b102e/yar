import json
from datetime import datetime, timedelta
from pathlib import Path


class EmotionalJournal:
    def __init__(self, memory_dir: Path):
        self.memory_dir = Path(memory_dir).expanduser()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.memory_dir / "emotional_journal.jsonl"

    def add_entry(self, trigger, emotion, intensity, valence, note, session_ts):
        now_iso = datetime.now().isoformat()
        entry = {
            "at": now_iso,
            "ts": now_iso,
            "trigger": str(trigger or "").strip(),
            "emotion": str(emotion or "").strip().lower(),
            "intensity": self._clamp_01(float(intensity) if intensity is not None else 0.0),
            "valence": self._clamp_11(float(valence) if valence is not None else 0.0),
            "note": str(note or "").strip(),
            "session_ts": str(session_ts or "").strip(),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_recent(self, days: int = 7, min_intensity: float = 0.5) -> list:
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
        out = []
        for e in self._load_all():
            at = self._parse_dt(str(e.get("at", "")))
            if not at or at < cutoff:
                continue
            if float(e.get("intensity", 0.0)) < float(min_intensity):
                continue
            out.append(e)
        return out

    def get_by_emotion(self, emotion: str, days: int = 30) -> list:
        needle = str(emotion or "").strip().lower()
        if not needle:
            return []
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
        out = []
        for e in self._load_all():
            at = self._parse_dt(str(e.get("at", "")))
            if not at or at < cutoff:
                continue
            if str(e.get("emotion", "")).strip().lower() == needle:
                out.append(e)
        return out

    def get_peaks(self, limit: int = 5) -> list:
        items = self._load_all()
        items.sort(key=lambda x: float(x.get("intensity", 0.0)), reverse=True)
        return items[: max(1, int(limit))]

    def _load_all(self) -> list:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if isinstance(item, dict):
                    out.append(item)
        return out

    @staticmethod
    def _parse_dt(value: str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    @staticmethod
    def _clamp_01(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @staticmethod
    def _clamp_11(v: float) -> float:
        return max(-1.0, min(1.0, float(v)))
