import json
import re
from datetime import datetime, timedelta
from pathlib import Path


class TimelineSearch:
    def __init__(self, memory_dir: Path):
        self.memory_dir = Path(memory_dir)
        self.conversations_dir = self.memory_dir / "conversations"
        self.memory_file = self.memory_dir / "memory.json"
        self.emotional_journal_file = self.memory_dir / "emotional_journal.jsonl"

    def search_conversations(self, query: str, days: int = 14) -> str:
        q = str(query or "").strip()
        if not q:
            return ""
        files = self._conversation_files(days=days)
        if not files:
            return ""
        q_words = self._words(q)
        chunks = []
        for path in files:
            data = self._load_json(path)
            if not isinstance(data, dict):
                continue
            base_dt = self._file_date(path, data)
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                continue
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                content = str(msg.get("content", ""))
                if not content.strip():
                    continue
                if not self._matches(content, q_words):
                    continue
                snippet = self._dialogue_snippet(messages, i)
                if not snippet:
                    continue
                ts = self._message_ts(msg, base_dt)
                chunks.append(f"[{ts}] {snippet}")
        if not chunks:
            return ""
        return self._cap_text("\n\n".join(chunks), 800)

    def search_by_period(self, date_from: str, date_to: str, topic: str = "") -> str:
        try:
            d_from = datetime.strptime(str(date_from), "%Y-%m-%d").date()
            d_to = datetime.strptime(str(date_to), "%Y-%m-%d").date()
        except Exception:
            return ""
        if d_from > d_to:
            d_from, d_to = d_to, d_from

        topic_words = self._words(topic) if topic else set()
        chunks = []
        for path in sorted(self.conversations_dir.glob("*.json")):
            if path.name == "checkpoint.json":
                continue
            data = self._load_json(path)
            if not isinstance(data, dict):
                continue
            base_dt = self._file_date(path, data)
            if not base_dt:
                continue
            if not (d_from <= base_dt.date() <= d_to):
                continue
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                continue
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                content = str(msg.get("content", ""))
                if not content.strip():
                    continue
                if topic_words and not self._matches(content, topic_words):
                    continue
                snippet = self._dialogue_snippet(messages, i)
                if not snippet:
                    continue
                ts = self._message_ts(msg, base_dt)
                chunks.append(f"[{ts}] {snippet}")
        if not chunks:
            return ""
        return self._cap_text("\n\n".join(chunks), 800)

    def get_emotional_anchors(self, min_weight: float = 0.8, limit: int = 5) -> str:
        data = self._load_json(self.memory_file)
        if not isinstance(data, dict):
            data = {}
        facts = data.get("facts", [])
        if not isinstance(facts, list):
            facts = []
        rows = []
        for f in facts:
            if not isinstance(f, dict):
                continue
            ew = f.get("emotional_weight", None)
            if ew is None:
                continue
            try:
                weight = float(ew)
            except Exception:
                continue
            if weight < float(min_weight):
                continue
            fact = str(f.get("fact", "")).strip()
            if not fact:
                continue
            date = str(f.get("date", "") or f.get("added", "")).strip()
            rows.append((weight, f"{fact}", date[:10]))

        if self.emotional_journal_file.exists():
            with open(self.emotional_journal_file, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    try:
                        intensity = float(item.get("intensity", 0.0))
                    except Exception:
                        continue
                    if intensity < float(min_weight):
                        continue
                    emotion = str(item.get("emotion", "")).strip()
                    note = str(item.get("note", "")).strip()
                    at = str(item.get("at", "") or item.get("session_ts", "")).strip()
                    date_label = self._ru_date_label(at)
                    text = f"{emotion}: {note}".strip(": ").strip()
                    if text:
                        rows.append((intensity, text, date_label))

        if not rows:
            return ""
        rows.sort(key=lambda x: x[0], reverse=True)
        out = [f"[{w:.1f}] {text} ({date})" for w, text, date in rows[: max(1, int(limit))]]
        return "\n".join(out)

    def _conversation_files(self, days: int) -> list[Path]:
        if not self.conversations_dir.exists():
            return []
        cutoff = datetime.now().date() - timedelta(days=max(1, int(days)))
        files = []
        for path in sorted(self.conversations_dir.glob("*.json")):
            if path.name == "checkpoint.json":
                continue
            data = self._load_json(path)
            if not isinstance(data, dict):
                continue
            base_dt = self._file_date(path, data)
            if base_dt and base_dt.date() >= cutoff:
                files.append(path)
        return files

    @staticmethod
    def _load_json(path: Path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def _words(text: str) -> set[str]:
        return set(re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", str(text).lower()))

    @staticmethod
    def _matches(text: str, q_words: set[str]) -> bool:
        if not q_words:
            return False
        tw = set(re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", str(text).lower()))
        return len(q_words & tw) > 0

    @staticmethod
    def _dialogue_snippet(messages: list, i: int) -> str:
        pairs = []
        for j in [i, i + 1]:
            if j < 0 or j >= len(messages):
                continue
            msg = messages[j]
            if not isinstance(msg, dict):
                continue
            role = "[USER]" if msg.get("role") == "user" else "Яр"
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            pairs.append(f"{role}: {content}")
        return "\n".join(pairs).strip()

    def _message_ts(self, msg: dict, base_dt: datetime | None) -> str:
        hhmm = str(msg.get("ts", "")).strip()
        dt = base_dt or datetime.now()
        day = self._ru_day_month(dt)
        if hhmm:
            return f"{day} {hhmm}"
        return day

    @staticmethod
    def _file_date(path: Path, data: dict) -> datetime | None:
        raw = str(data.get("date", "")).strip()
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except Exception:
                pass
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", path.name)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d")
            except Exception:
                return None
        return None

    @staticmethod
    def _ru_day_month(dt: datetime) -> str:
        months = {
            1: "января", 2: "февраля", 3: "марта", 4: "апреля",
            5: "мая", 6: "июня", 7: "июля", 8: "августа",
            9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
        }
        return f"{dt.day} {months.get(dt.month, '')}".strip()

    @classmethod
    def _ru_date_label(cls, value: str) -> str:
        try:
            dt = datetime.fromisoformat(value)
            return cls._ru_day_month(dt)
        except Exception:
            return str(value)[:10]

    @staticmethod
    def _cap_text(text: str, max_chars: int) -> str:
        s = str(text).strip()
        if len(s) <= max_chars:
            return s
        return s[:max_chars].rstrip() + "..."
