"""
Самодиагностика — Яр проверяет свои возможности при каждом запуске
и периодически в фоне.
"""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class SelfCheck:

    def __init__(self, memory_dir: Path, consolidation=None, agent=None):
        self.memory_dir = memory_dir
        self.results: dict = {}
        self.check_file = memory_dir / "self_check_last.json"
        self.consolidation = consolidation
        self.agent = agent

    def set_agent(self, agent) -> None:
        self.agent = agent

    def run(self) -> dict:
        """Полная диагностика — запускается при старте"""
        print("🔍 Самодиагностика...")

        self.results = {
            "timestamp": datetime.now().isoformat(),
            "modules":   self._check_modules(),
            "camera":    self._check_camera(),
            "audio":     self._check_audio(),
            "api":       self._check_api(),
            "memory":    self._check_memory(),
            "code":      self._check_code(),
            "system":    self._check_system(),
        }

        self._save()
        self._print_summary()
        return self.results

    # ── Модули ──────────────────────────────────────────────────────────────

    def _check_modules(self) -> dict:
        modules = {
            "anthropic":      "anthropic",
            "faster_whisper": "faster_whisper",
            "sounddevice":    "sounddevice",
            "numpy":          "numpy",
            "opencv":         "cv2",
            "pymavlink":      "pymavlink",
        }
        status = {}
        for name, imp in modules.items():
            try:
                mod = importlib.import_module(imp)
                ver = getattr(mod, "__version__", "?")
                status[name] = {"ok": True, "version": ver}
            except ImportError:
                status[name] = {"ok": False, "version": None}
        return status

    # ── Камера ──────────────────────────────────────────────────────────────

    def _check_camera(self) -> dict:
        try:
            import cv2
            cap = cv2.VideoCapture(0)
            if cap.isOpened():
                ret, _ = cap.read()
                cap.release()
                return {"ok": ret, "index": 0}
            cap.release()
            return {"ok": False, "reason": "не открывается"}
        except ImportError:
            return {"ok": False, "reason": "opencv не установлен"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    # ── Аудио ───────────────────────────────────────────────────────────────

    def _check_audio(self) -> dict:
        result = {"microphone": False, "tts": False, "tts_voice": None}

        # Микрофон
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            inputs = [d for d in devices if d["max_input_channels"] > 0]
            result["microphone"] = len(inputs) > 0
            result["input_devices"] = len(inputs)
        except Exception:
            pass

        # TTS — macOS say
        try:
            r = subprocess.run(
                ["say", "-v", "?"],
                capture_output=True, text=True, timeout=3
            )
            result["tts"] = r.returncode == 0
            # Ищем русский голос
            for line in r.stdout.split("\n"):
                if "ru_RU" in line or "Milena" in line or "Yuri" in line:
                    result["tts_voice"] = line.split()[0]
                    break
        except Exception:
            pass

        return result

    # ── API ─────────────────────────────────────────────────────────────────

    def _check_api(self) -> dict:
        result = {"key_set": False, "reachable": False, "latency_ms": None}

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        result["key_set"] = key.startswith("sk-ant-")

        if not result["key_set"]:
            result["reason"] = "ANTHROPIC_API_KEY не установлен"
            return result

        try:
            t0 = time.time()
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            # Минимальный тест
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
                messages=[{"role": "user", "content": "1"}]
            )
            result["reachable"] = True
            result["latency_ms"] = int((time.time() - t0) * 1000)
        except Exception as e:
            result["reason"] = str(e)[:100]

        return result

    # ── Память ──────────────────────────────────────────────────────────────

    def _check_memory(self) -> dict:
        result = {}
        try:
            # Размер папки
            total = sum(
                f.stat().st_size
                for f in self.memory_dir.rglob("*")
                if f.is_file()
            )
            result["total_mb"] = round(total / 1024 / 1024, 2)

            # Основной файл
            mem_file = self.memory_dir / "memory.json"
            if mem_file.exists():
                with open(mem_file, encoding="utf-8") as f:
                    data = json.load(f)
                result["sessions"]      = len(data.get("sessions", []))
                result["facts"]         = len(data.get("facts", []))
                result["ok"]            = True
            else:
                result["ok"] = False
                result["reason"] = "memory.json не существует"

            # Файлы разговоров
            convs = list((self.memory_dir / "conversations").glob("*.json"))
            result["conversation_files"] = len(convs)

        except Exception as e:
            result["ok"] = False
            result["reason"] = str(e)

        return result

    # ── Код ─────────────────────────────────────────────────────────────────

    def _check_code(self) -> dict:
        """Хэши ключевых файлов — чтобы знать изменился ли код"""
        files = [
            "main.py",
            "brain/agent.py",
            "brain/memory.py",
            "brain/continuity.py",
            "brain/state.py",
            "brain/self_check.py",
            "audio/voice.py",
            "vision/camera.py",
        ]
        hashes = {}
        for f in files:
            path = Path(f)
            if path.exists():
                content = path.read_bytes()
                hashes[f] = hashlib.md5(content).hexdigest()[:8]
            else:
                hashes[f] = "missing"
        return {
            "file_hashes": hashes,
            "python_version": sys.version.split()[0],
        }

    # ── Система ─────────────────────────────────────────────────────────────

    def _check_system(self) -> dict:
        result = {}
        try:
            # Диск
            r = subprocess.run(
                ["df", "-h", str(Path.home())],
                capture_output=True, text=True, timeout=3
            )
            lines = r.stdout.strip().split("\n")
            if len(lines) > 1:
                parts = lines[1].split()
                result["disk_available"] = parts[3] if len(parts) > 3 else "?"

            # Процессор / память через python
            try:
                import psutil
                result["cpu_percent"]  = psutil.cpu_percent(interval=0.5)
                result["ram_available_gb"] = round(
                    psutil.virtual_memory().available / 1024**3, 1
                )
            except ImportError:
                pass

            result["ok"] = True
        except Exception as e:
            result["ok"] = False
            result["reason"] = str(e)
        return result

    # ── Сохранение и вывод ──────────────────────────────────────────────────

    def _save(self):
        with open(self.check_file, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)

    def _print_summary(self):
        r = self.results
        mods = r.get("modules", {})

        ok  = lambda x: "✅" if x else "❌"

        print(f"  API:        {ok(r['api'].get('reachable'))}  "
              f"{'latency ' + str(r['api'].get('latency_ms')) + 'ms' if r['api'].get('latency_ms') else r['api'].get('reason','')}")
        print(f"  Камера:     {ok(r['camera'].get('ok'))}")
        print(f"  Микрофон:   {ok(r['audio'].get('microphone'))}")
        print(f"  TTS голос:  {ok(r['audio'].get('tts'))}  {r['audio'].get('tts_voice','')}")
        print(f"  Whisper:    {ok(mods.get('faster_whisper',{}).get('ok'))}")
        print(f"  Память:     {ok(r['memory'].get('ok'))}  "
              f"{r['memory'].get('total_mb','?')}MB  "
              f"{r['memory'].get('sessions','?')} сессий  "
              f"{r['memory'].get('facts','?')} фактов")
        if self.consolidation:
            print(f"  Консолидация: {self.consolidation.get_status()}")
        if self.agent:
            try:
                prompt_sample = self.agent._system()
                tokens_approx = len(prompt_sample) // 4
                print(f"  Промпт:     📝 ~{tokens_approx} токенов")
            except Exception as e:
                print(f"  Промпт:     ⚠️  не удалось оценить ({e})")
        print()

    def to_prompt_str(self) -> str:
        """Короткий статус для системного промпта"""
        if not self.results:
            return "диагностика не запускалась"

        parts = []
        if not self.results["api"].get("reachable"):
            parts.append("API недоступен")
        if not self.results["camera"].get("ok"):
            parts.append("камера недоступна")
        if not self.results["audio"].get("microphone"):
            parts.append("микрофон недоступен")
        if not self.results["modules"].get("faster_whisper", {}).get("ok"):
            parts.append("Whisper не установлен (текстовый режим)")

        ok_parts = []
        if self.results["api"].get("reachable"):
            ok_parts.append(f"API ок ({self.results['api'].get('latency_ms')}мс)")
        if self.results["camera"].get("ok"):
            ok_parts.append("камера ок")
        if self.results["audio"].get("microphone"):
            ok_parts.append("микрофон ок")

        mem = self.results.get("memory", {})
        ok_parts.append(
            f"память: {mem.get('sessions',0)} сессий, "
            f"{mem.get('facts',0)} фактов, "
            f"{mem.get('total_mb',0)}MB"
        )

        result = []
        if ok_parts:
            result.append("Работает: " + ", ".join(ok_parts))
        if parts:
            result.append("Не работает: " + ", ".join(parts))
        return ". ".join(result)
