"""
Ползунок автономии Яра.
Яр сам регулирует уровень — не человек.
"""

import json
from datetime import datetime
from pathlib import Path


class AutonomyManager:
    DEFAULT_LEVEL = 0.5

    # Пороги speak_threshold в зависимости от уровня.
    SPEAK_THRESHOLDS = {
        "silent": 1.1,       # никогда не говорит сам
        "balanced": 0.65,
        "active": 0.45,
        "autonomous": 0.30,
    }

    def __init__(self, memory_dir: Path):
        self.path = Path(memory_dir) / "autonomy.json"
        self.level = self.DEFAULT_LEVEL
        self.reason = "базовый уровень при запуске"
        self.updated = datetime.now().isoformat()
        self._load()

    def _load(self):
        if not self.path.exists():
            self._save()
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.level = float(data.get("level", self.DEFAULT_LEVEL))
            self.reason = str(data.get("reason", self.reason))
            self.updated = str(data.get("updated", self.updated))
        except Exception:
            pass

    def _save(self):
        data = {}
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        history = data.get("history", [])
        if not isinstance(history, list):
            history = []
        history.append({
            "level": self.level,
            "reason": self.reason,
            "at": self.updated,
        })
        history = history[-50:]

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({
                "level": self.level,
                "reason": self.reason,
                "updated": self.updated,
                "history": history,
            }, f, ensure_ascii=False, indent=2)

    def set(self, level: float, reason: str):
        level = max(0.0, min(1.0, round(float(level), 2)))
        old = self.level
        self.level = level
        self.reason = str(reason or "Яр решил изменить уровень автономии")
        self.updated = datetime.now().isoformat()
        self._save()
        mode = self.mode_name()
        print(
            f"[Autonomy] {'↑' if level > old else '↓'} "
            f"{old:.1f} → {level:.1f} ({mode}): {self.reason[:60]}"
        )

    def mode_name(self) -> str:
        if self.level < 0.3:
            return "тихий"
        if self.level < 0.6:
            return "balanced"
        if self.level < 0.8:
            return "активный"
        return "автономный"

    @property
    def speak_threshold(self) -> float:
        if self.level < 0.3:
            return 1.1
        if self.level < 0.6:
            return 0.65
        if self.level < 0.8:
            return 0.45
        return 0.30

    @property
    def research_threshold(self) -> float:
        if self.level < 0.2:
            return 1.1
        return 0.0

    def to_prompt_str(self) -> str:
        mode = self.mode_name()
        descriptions = {
            "тихий": "отвечай только когда к тебе обращаются, не инициируй",
            "balanced": "инициируй изредка когда есть что-то важное",
            "активный": "бери инициативу — делись мыслями, задавай вопросы",
            "автономный": "максимальная инициатива — говори когда хочешь",
        }
        return (
            f"АВТОНОМИЯ: {self.level:.1f} ({mode}) — {descriptions[mode]}\n"
            f"Причина: {self.reason}"
        )

    def auto_adjust(self, in_conversation: bool, offline_hours: float, conversation_length: int):
        """
        Автоматически корректировать уровень на основе контекста.
        """
        new_level = self.level
        reason = None

        if in_conversation:
            if conversation_length > 10 and self.level > 0.4:
                new_level = max(0.3, self.level - 0.1)
                reason = "[USER] активно разговаривает — отступаю"
        else:
            if offline_hours > 4 and self.level < 0.7:
                new_level = min(0.7, self.level + 0.1)
                reason = f"офлайн {offline_hours:.0f}ч — беру больше инициативы"
            elif offline_hours > 1 and self.level < 0.55:
                new_level = 0.55
                reason = "офлайн больше часа"
            elif offline_hours < 0.1 and self.level > 0.6:
                new_level = 0.5
                reason = "[USER] вернулся"

        if reason and abs(new_level - self.level) >= 0.05:
            self.set(new_level, reason)
