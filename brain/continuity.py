"""
Непрерывность — Яр понимает когда был офлайн и что могло измениться.

При каждом запуске:
1. Читаем когда последний раз был онлайн
2. Считаем сколько прошло
3. Генерируем "пробуждение" с осознанием пропущенного времени
4. Проверяем изменилась ли локация / время суток / и пр.
"""

import json
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


class TemporalPatterns:
    def __init__(self, memory_dir: Path, identity=None):
        self.memory_dir = Path(memory_dir)
        self.consolidated_dir = self.memory_dir / "consolidated"
        self.consolidated_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.consolidated_dir / "time_patterns.json"
        self.identity = identity
        self.patterns = self._load()

    def _load(self) -> dict:
        default = {
            "day_of_week": {},
            "month_patterns": {},
            "streak": {"current_days": 0, "avg_gap_hours": 0, "longest_streak": 0},
            "seasonal": {},
            "last_updated": None,
        }
        if not self.file.exists():
            return default
        try:
            if self.identity:
                from identity.encryption import decrypt_file
                data = decrypt_file(self.identity, self.file, default=default)
            else:
                with open(self.file, encoding="utf-8") as f:
                    data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return default

    def _save(self) -> None:
        self.patterns["last_updated"] = datetime.now().isoformat()
        if self.identity:
            from identity.encryption import encrypt_file
            encrypt_file(self.identity, self.file, self.patterns)
        else:
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(self.patterns, f, ensure_ascii=False, indent=2)

    def get_context(self) -> str:
        # Возможны несколько инстансов (agent + consolidation) в одном процессе,
        # поэтому перед выдачей контекста перечитываем актуальный файл.
        self.patterns = self._load()
        if not self.patterns or not self.patterns.get("day_of_week"):
            return ""
        now = datetime.now()
        parts = []

        dow = now.strftime("%A").lower()
        dow_data = self.patterns.get("day_of_week", {}).get(dow, {})
        if int(dow_data.get("sessions", 0)) >= 3:
            avg_mood = str(dow_data.get("avg_mood", "")).lower()
            if dow == "sunday":
                parts.append("воскресенье — завтра рабочая неделя")
            elif dow == "monday" and avg_mood in {"tired", "усталый"}:
                parts.append("понедельник — исторически усталый день")
            elif dow == "friday":
                parts.append("пятница — обычно расслабленнее")

        if self.is_unusual_time():
            typical = str(dow_data.get("typical_time", "")).strip()
            if typical:
                parts.append(f"необычное время — обычно приходишь {typical}")

        if now.day <= 5:
            parts.append("начало месяца")

        streak = self.get_streak()
        if streak >= 3:
            parts.append(f"{streak} дней подряд общаемся")
        elif streak == 0:
            gap = self._days_since_last()
            if gap >= 3:
                parts.append(f"не общались {gap} дней — необычно")

        month_patterns = self.patterns.get("month_patterns", {})
        if now.day <= 5 and month_patterns.get("month_start"):
            trend = month_patterns.get("month_start", {}).get("mood_trend")
            if trend and trend != "unknown":
                parts.append(f"начало месяца — исторически {trend}")
        if now.day >= 28 and month_patterns.get("month_end"):
            trend = month_patterns.get("month_end", {}).get("mood_trend")
            if trend and trend != "unknown":
                parts.append(f"конец месяца — исторически {trend}")

        return " / ".join(parts) if parts else ""

    def is_unusual_time(self) -> bool:
        day_of_week = self.patterns.get("day_of_week", {})
        dow = datetime.now().strftime("%A").lower()
        data = day_of_week.get(dow, {})
        if int(data.get("sessions", 0)) < 3:
            return False

        typical_hour = data.get("typical_hour")
        if typical_hour is None:
            typical_time = str(data.get("typical_time", "")).lower()
            typical_hour = self._typical_hour_from_label(typical_time)
        if typical_hour is None:
            return False

        now_hour = datetime.now().hour
        diff = abs(int(now_hour) - int(typical_hour))
        wrapped_diff = min(diff, 24 - diff)
        return wrapped_diff > 4

    def get_streak(self) -> int:
        return int(self.patterns.get("streak", {}).get("current_days", 0))

    def update(self, sessions_history: list):
        sessions = self._normalize_sessions(sessions_history)
        if not sessions:
            return

        day_of_week = self._build_day_of_week(sessions)
        month_patterns = self._build_month_patterns(sessions)
        streak = self._build_streak(sessions)
        seasonal = self._seasonal_defaults()

        self.patterns = {
            "day_of_week": day_of_week,
            "month_patterns": month_patterns,
            "streak": streak,
            "seasonal": seasonal,
            "last_updated": datetime.now().isoformat(),
        }
        self._save()

    def update_from_llama(self, response):
        if not isinstance(response, dict):
            return
        merged = dict(self.patterns)
        if isinstance(response.get("day_of_week"), dict):
            for day, row in response.get("day_of_week", {}).items():
                if not isinstance(row, dict):
                    continue
                existing = merged.setdefault("day_of_week", {}).get(day, {})
                out = dict(existing)
                out.update(row)
                if "typical_hour" not in out:
                    out["typical_hour"] = self._typical_hour_from_label(str(out.get("typical_time", "")).lower())
                merged["day_of_week"][day] = out
        if isinstance(response.get("month_patterns"), dict):
            merged["month_patterns"] = response["month_patterns"]
        if isinstance(response.get("streak"), dict):
            merged["streak"] = response["streak"]
        if isinstance(response.get("seasonal"), dict):
            seasonal = self._seasonal_defaults()
            seasonal.update(response["seasonal"])
            merged["seasonal"] = seasonal
        else:
            merged["seasonal"] = self._seasonal_defaults()

        merged["last_updated"] = datetime.now().isoformat()
        self.patterns = merged
        self._save()

    def _days_since_last(self) -> int:
        last = self.patterns.get("streak", {}).get("last_session_date")
        if not last:
            return 0
        try:
            last_day = datetime.fromisoformat(str(last)).date()
        except Exception:
            return 0
        return max(0, (datetime.now().date() - last_day).days)

    @staticmethod
    def _seasonal_defaults() -> dict:
        return {
            "spring": "оливки апрель-май, активность на участке растёт",
            "summer": "жара июль-август и туристы в Лигурии, возможна ниже активность",
        }

    @staticmethod
    def _typical_hour_from_label(label: str):
        mapping = {"morning": 9, "утро": 9, "day": 14, "день": 14, "evening": 20, "вечер": 20, "night": 1, "ночь": 1}
        return mapping.get(label)

    @staticmethod
    def _time_label(hour: int) -> str:
        if 6 <= hour < 12:
            return "morning"
        if 12 <= hour < 18:
            return "day"
        if 18 <= hour < 23:
            return "evening"
        return "night"

    @staticmethod
    def _normalize_sessions(sessions_history: list) -> list[dict]:
        out = []
        for s in sessions_history or []:
            if not isinstance(s, dict):
                continue
            raw_date = s.get("date") or s.get("started_at") or s.get("start")
            if not raw_date:
                continue
            try:
                dt = datetime.fromisoformat(str(raw_date))
            except Exception:
                continue
            duration = int(s.get("duration_min", 0) or 0)
            mood = s.get("mood")
            if mood is not None:
                mood = str(mood).lower()
            out.append({"dt": dt, "duration_min": duration, "mood": mood})
        out.sort(key=lambda x: x["dt"])
        return out

    def _build_day_of_week(self, sessions: list[dict]) -> dict:
        grouped = {}
        for s in sessions:
            dow = s["dt"].strftime("%A").lower()
            grouped.setdefault(dow, []).append(s)
        result = {}
        for dow, items in grouped.items():
            if len(items) < 3:
                continue
            durations = [max(1, int(i.get("duration_min", 1))) for i in items]
            avg_len = int(round(sum(durations) / len(durations)))
            hours = [i["dt"].hour for i in items]
            typical_hour = int(round(sum(hours) / len(hours)))
            typical_time = self._time_label(typical_hour)
            mood = self._avg_mood(items)
            result[dow] = {
                "avg_mood": mood,
                "avg_length_min": avg_len,
                "typical_time": typical_time,
                "typical_hour": typical_hour,
                "sessions": len(items),
            }
        return result

    def _build_month_patterns(self, sessions: list[dict]) -> dict:
        start = [s for s in sessions if s["dt"].day <= 5]
        end = [s for s in sessions if s["dt"].day >= 28]
        out = {}
        if len(start) >= 3:
            out["month_start"] = {
                "mood_trend": self._avg_mood(start),
                "days": [1, 2, 3, 4, 5],
            }
        if len(end) >= 3:
            out["month_end"] = {
                "mood_trend": self._avg_mood(end),
                "days": [28, 29, 30, 31],
            }
        return out

    @staticmethod
    def _avg_mood(items: list[dict]) -> str:
        moods = [str(i.get("mood", "")).lower() for i in items if i.get("mood")]
        if moods:
            counts = {}
            for m in moods:
                counts[m] = counts.get(m, 0) + 1
            return max(counts, key=counts.get)
        avg_len = sum(max(1, int(i.get("duration_min", 1))) for i in items) / max(1, len(items))
        if avg_len < 8:
            return "tired"
        if avg_len > 20:
            return "relaxed"
        return "neutral"

    @staticmethod
    def _build_streak(sessions: list[dict]) -> dict:
        if not sessions:
            return {"current_days": 0, "avg_gap_hours": 0, "longest_streak": 0}
        unique_days = sorted({s["dt"].date() for s in sessions})
        current_streak = 0
        cursor = datetime.now().date()
        days_set = set(unique_days)
        while cursor in days_set:
            current_streak += 1
            cursor = cursor - timedelta(days=1)

        longest = 0
        run = 0
        prev = None
        for d in unique_days:
            if prev and (d - prev).days == 1:
                run += 1
            else:
                run = 1
            longest = max(longest, run)
            prev = d

        gaps = []
        for i in range(len(sessions) - 1):
            delta_h = (sessions[i + 1]["dt"] - sessions[i]["dt"]).total_seconds() / 3600
            if delta_h >= 0:
                gaps.append(delta_h)
        avg_gap = round(sum(gaps) / len(gaps), 1) if gaps else 0
        return {
            "current_days": current_streak,
            "avg_gap_hours": avg_gap,
            "longest_streak": longest,
            "last_session_date": sessions[-1]["dt"].isoformat(),
        }


class ContinuityTracker:

    def __init__(self, memory_dir: Path, identity=None):
        self.memory_dir = memory_dir
        self.identity = identity
        self.state_file = memory_dir / "continuity.json"
        self.bridges_file = Path(memory_dir) / "continuity" / "temporal_bridges.jsonl"
        self.bridges_file.parent.mkdir(parents=True, exist_ok=True)
        self.temporal_patterns = TemporalPatterns(memory_dir, identity=identity)
        self._state = self._load()
        self.gap = self._calculate_gap()
        self.online = False
        self._heartbeat_task = None

    def _load(self) -> dict:
        default = {
            "last_seen": None,
            "last_location": None,
            "last_ssid": None,
            "session_count": 0,
            "total_offline_minutes": 0,
        }
        if not self.state_file.exists():
            return default
        if self.identity:
            from identity.encryption import decrypt_file
            return decrypt_file(self.identity, self.state_file, default=default)
        with open(self.state_file, encoding="utf-8") as f:
            return json.load(f)

    def _save(self):
        if self.identity:
            from identity.encryption import encrypt_file
            encrypt_file(self.identity, self.state_file, self._state)
        else:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)

    def _calculate_gap(self) -> Optional[dict]:
        """Сколько прошло с последнего сеанса"""
        last = self._state.get("last_seen")
        if not last:
            return None  # первый запуск

        last_dt = datetime.fromisoformat(last)
        now = datetime.now()
        delta = now - last_dt
        minutes = int(delta.total_seconds() / 60)

        return {
            "minutes": minutes,
            "hours": round(delta.total_seconds() / 3600, 1),
            "last_seen": last_dt.strftime("%d.%m %H:%M"),
            "now": now.strftime("%d.%m %H:%M"),
            "crossed_night": self._crossed_night(last_dt, now),
            "crossed_day": delta.days > 0,
        }

    @staticmethod
    def _crossed_night(a: datetime, b: datetime) -> bool:
        """Прошла ли ночь между двумя моментами"""
        if (b - a).days > 0:
            return True
        # Проверяем пересечение ночного окна 23:00-07:00
        night_start = a.replace(hour=23, minute=0)
        night_end = b.replace(hour=7, minute=0)
        return a < night_start < b or a.hour >= 23 or b.hour < 7

    def mark_online(self):
        """Вызываем при каждом успешном запросе к API"""
        self.online = True
        self._state["last_seen"] = datetime.now().isoformat()
        self._state["session_count"] = self._state.get("session_count", 0) + 1
        ssid = self._get_wifi_ssid()
        if ssid:
            self._state["last_ssid"] = ssid
        self._save()

    def mark_offline(self):
        self.online = False

    def check_connectivity(self) -> bool:
        """Проверяем есть ли интернет"""
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3",
                 "https://api.anthropic.com/health"],
                capture_output=True, timeout=4
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_wifi_ssid(self) -> Optional[str]:
        """Текущий WiFi — косвенная метрика локации"""
        try:
            # macOS
            result = subprocess.run(
                ["/System/Library/PrivateFrameworks/Apple80211.framework"
                 "/Versions/Current/Resources/airport", "-I"],
                capture_output=True, text=True, timeout=3
            )
            for line in result.stdout.split("\n"):
                if " SSID:" in line:
                    return line.split("SSID:")[-1].strip()
        except Exception:
            pass

        # Альтернатива через networksetup
        try:
            result = subprocess.run(
                ["networksetup", "-getairportnetwork", "en0"],
                capture_output=True, text=True, timeout=3
            )
            if "Current Wi-Fi Network:" in result.stdout:
                return result.stdout.split(":")[-1].strip()
        except Exception:
            pass

        return None

    def location_changed(self) -> bool:
        """Сменился ли WiFi = вероятно сменилась локация"""
        current = self._get_wifi_ssid()
        last = self._state.get("last_ssid")
        if current and last and current != last:
            return True
        return False

    def build_wakeup_context(self) -> str:
        """
        Текст для системного промпта — что Яр должен осознать при запуске.
        Передаётся один раз в начале сессии.
        """
        if not self.gap:
            return "Пауза 0ч (short_break)."

        hours = float(self.gap.get("hours", 0.0) or 0.0)
        if hours < 2:
            mode = "short_break"
        elif hours < 8:
            mode = "medium_pause"
        else:
            mode = "long_pause"
        return f"Пауза {int(hours)}ч ({mode})."

    def short_status(self) -> str:
        """Для консоли при запуске"""
        if not self.gap:
            return "первый запуск"
        m = self.gap["minutes"]
        if m < 60:
            return f"офлайн {m} мин"
        if m < 1440:
            return f"офлайн {self.gap['hours']} ч"
        return f"офлайн {m // 1440} дней"

    def offline_hours(self) -> float:
        """Текущее время офлайна по last_seen в continuity state."""
        last = self._state.get("last_seen")
        if not last:
            return 0.0
        try:
            last_dt = datetime.fromisoformat(str(last))
            return max(0.0, round((datetime.now() - last_dt).total_seconds() / 3600, 2))
        except Exception:
            return 0.0

    def build_temporal_bridge(self, active_loops: list[dict], hours_offline: float) -> dict:
        continuing_threads = [
            {"topic": l["topic"],
             "tension": l.get("tension", 0),
             "recurrence": l.get("recurrence", 1)}
            for l in (active_loops or [])
            if isinstance(l, dict) and l.get("tension", 0) > 0.4
        ][:5]

        if hours_offline < 2:
            offline_mode = "short_break"
        elif hours_offline < 8:
            offline_mode = "medium_pause"
        elif hours_offline < 24:
            offline_mode = "long_absence"
        else:
            offline_mode = f"extended_absence_{int(hours_offline // 24)}d"

        if continuing_threads:
            top = continuing_threads[0]["topic"]
            bridge_summary = (f"Центральная незавершённая линия: «{top}». "
                              f"Пауза {int(hours_offline)}ч ({offline_mode}).")
        else:
            bridge_summary = f"Активных незавершённых линий нет. Пауза {int(hours_offline)}ч."

        bridge = {
            "timestamp": datetime.now().isoformat(),
            "hours_passed": round(hours_offline, 1),
            "offline_mode": offline_mode,
            "continuing_threads": continuing_threads,
            "bridge_summary": bridge_summary,
        }

        with open(self.bridges_file, "ab") as f:
            if self.identity:
                from identity.encryption import encrypt_line
                f.write(encrypt_line(self.identity, bridge) + b"\n")
            else:
                f.write(json.dumps(bridge, ensure_ascii=False).encode() + b"\n")
        return bridge

    def get_latest_bridge(self) -> dict | None:
        if not self.bridges_file.exists():
            return None
        with open(self.bridges_file, "rb") as f:
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
