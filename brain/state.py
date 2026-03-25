"""
Внутренние состояния — живые мотивации
"""

import time
from dataclasses import dataclass


@dataclass
class InternalState:
    curiosity:  float = 0.5
    social:     float = 0.6
    alertness:  float = 0.3
    energy:     float = 1.0
    boredom:    float = 0.1
    last_update: float = 0.0

    def __post_init__(self):
        self.last_update = time.time()

    def tick(self, in_conversation: bool = False, motion: bool = False):
        now = time.time()
        dt = (now - self.last_update) / 60.0  # минуты
        self.last_update = now

        self.curiosity  = min(1.0, self.curiosity  + 0.015 * dt)
        self.boredom    = min(1.0, self.boredom    + 0.04  * dt) if not in_conversation else max(0.0, self.boredom - 0.15)
        self.social     = min(1.0, self.social     + 0.025 * dt) if not in_conversation else max(0.1, self.social  - 0.1)
        self.alertness  = min(1.0, self.alertness  + 0.4)        if motion              else max(0.1, self.alertness - 0.01 * dt)

    def dominant(self) -> str:
        drives = {
            "explore":    self.curiosity * 0.7 + self.boredom * 0.3,
            "talk":       self.social    * 0.9,
            "investigate":self.alertness,
            "rest":       max(0, 0.3 - self.energy),
        }
        return max(drives, key=drives.get)

    def to_str(self) -> str:
        return (
            f"curiosity={self.curiosity:.2f} "
            f"social={self.social:.2f} "
            f"boredom={self.boredom:.2f} "
            f"alertness={self.alertness:.2f}"
        )
