"""
Ежедневная консолидация памяти Яра.

Хранилище: ~/claude-memory/consolidated/
  weighted_facts.json
  concepts.json
  contradictions.json
  fact_timelines.json
  time_patterns.json
  living_prompt.txt
  consolidation_log.jsonl
  last_run.json
"""

import asyncio
import json
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import anthropic

from brain.continuity import TemporalPatterns
from brain.emotional_journal import EmotionalJournal
from brain.hypotheses import HypothesisManager
from brain.open_loops import OpenLoopManager

CONSOLIDATION_QUALITY_MODEL = "claude-haiku-4-5-20251001"


class MemoryConsolidation:
    CONSOLIDATION_HOUR_START = 22
    CONSOLIDATION_HOUR_END = 23
    MAX_MINUTES = 55

    def __init__(self, memory_dir, interest_manager=None, identity=None):
        self.memory_dir = Path(memory_dir)
        self.interest_manager = interest_manager
        self.identity = identity
        self.temporal_patterns = TemporalPatterns(self.memory_dir)
        self.hypothesis_manager = HypothesisManager(self.memory_dir, identity=identity)
        self.emotional_journal = EmotionalJournal(self.memory_dir)
        try:
            self.open_loops = OpenLoopManager(str(self.memory_dir))
        except Exception:
            self.open_loops = None
        self.in_conversation = False
        self._running = False
        self._session_start = datetime.now()

        self.dir = self.memory_dir / "consolidated"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.weighted_facts_file = self.dir / "weighted_facts.json"
        self.concepts_file = self.dir / "concepts.json"
        self.contradictions_file = self.dir / "contradictions.json"
        self.fact_timelines_file = self.dir / "fact_timelines.json"
        self.log_file = self.dir / "consolidation_log.jsonl"
        self.last_run_file = self.dir / "last_run.json"
        self.raw_facts_file = self.dir / "raw_facts.jsonl"
        self.living_prompt_file = self.dir / "living_prompt.txt"
        self.living_prompt_meta_file = self.dir / "living_prompt_meta.json"
        self.cognitive_core_file = self.dir / "cognitive_core.json"
        self.history_dir = self.dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.daily_distillation_file = self.dir / "daily_distillation.json"
        self.distillation_history_file = self.dir / "distillation_history.jsonl"
        self._living_prompt_running = False

        self._last_run = self._load_json(self.last_run_file, default={})
        if not self.fact_timelines_file.exists():
            self._save_json(self.fact_timelines_file, {})

    def set_in_conversation(self, flag: bool) -> None:
        self.in_conversation = bool(flag)

    def should_run(self) -> bool:
        return self.get_skip_reason() == ""

    def get_skip_reason(self) -> str:
        now = datetime.now()
        in_window = self.CONSOLIDATION_HOUR_START <= now.hour < self.CONSOLIDATION_HOUR_END
        last_date = str(self._last_run.get("date", ""))
        already_today = last_date == now.strftime("%Y-%m-%d")
        if self._running:
            return "already running"
        if self.in_conversation:
            return "in conversation"
        if not in_window:
            return "not in 22-23 window"
        if already_today:
            return "already ran today"
        return ""

    def get_status(self) -> str:
        if not self._last_run:
            return "ещё не запускалась"
        ts = self._last_run.get("timestamp", "")
        processed = self._last_run.get("processed_facts", 0)
        if not ts:
            return "ещё не запускалась"
        dt = datetime.fromisoformat(ts)
        day = "сегодня" if dt.date() == datetime.now().date() else dt.strftime("%d.%m.%Y")
        return f"последняя консолидация: {day} {dt.strftime('%H:%M')}, {processed} фактов обработано"

    def add_raw_fact(self, text: str, source: str):
        if not text or not text.strip():
            return
        entry = {
            "timestamp": datetime.now().isoformat(),
            "text": text.strip(),
            "source": source,
        }
        with open(self.raw_facts_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_consolidated_context(self, query: str = "") -> str:
        facts = self._load_weighted_facts()
        if not facts:
            return ""
        timelines = self._load_timelines()
        active = [f for f in facts if f.get("status", "active") == "active"]
        active.sort(key=lambda f: float(f.get("confidence", 0.0)), reverse=True)
        if query.strip():
            q = set(self._tokenize(query))
            active = sorted(
                active,
                key=lambda f: len(q & set(self._tokenize(f.get("text", "")))),
                reverse=True,
            )
        top = active[:12]
        lines = []
        for f in top:
            conf = float(f.get("confidence", 0.0))
            lines.append(f"- ({conf:.2f}) {self._format_fact_for_context(f, timelines)}")

        concepts = self._load_json(self.concepts_file, default=[])
        concept_lines = []
        for c in concepts[:5]:
            name = c.get("name", "")
            summary = c.get("summary", "")
            if name:
                concept_lines.append(f"- {name}: {summary}")

        result = []
        if lines:
            result.append("Консолидированные факты:\n" + "\n".join(lines))
        if concept_lines:
            result.append("Ключевые кластеры:\n" + "\n".join(concept_lines))
        return "\n\n".join(result)

    def get_contradictions(self) -> str:
        contradictions = self._load_json(self.contradictions_file, default=[])
        unresolved = [c for c in contradictions if c.get("status", "open") != "resolved"]
        if not unresolved:
            return "нет"
        lines = []
        for c in unresolved[:6]:
            old = c.get("old_fact", "")
            new = c.get("new_fact", "")
            rec = c.get("recommendation", "")
            lines.append(f"- {old} ↔ {new}. Рекомендация: {rec}")
        return "\n".join(lines)

    async def consolidation_cycle(self):
        if self._running:
            return
        self._running = True
        print("[Consolidation] 🌙 Начинаю ночную консолидацию...")
        start = time.time()
        new_facts = []
        try:
            # ШАГ 0 — Дистилляция дня.
            print("[Consolidation] 💤 Шаг 0: дистилляция дня...")
            await self._distill_day()
            self._save_last_run(processed_facts=0, partial=True)
            if self._should_abort(start):
                return

            # ШАГ 1 — Проверка гипотез по сегодняшним сессиям.
            today_sessions = self._get_today_sessions()
            await self._check_hypotheses(today_sessions)
            if self._should_abort(start):
                return

            # Lifecycle decay открытых линий (безопасно).
            try:
                if self.open_loops:
                    self.open_loops.decay_loops(hours_passed=24.0)
            except Exception as e:
                print(f"[Consolidation] open_loops decay skipped: {e}")

            new_facts = self._collect_new()
            existing = self._load_weighted_facts()

            if len(existing) > 300:
                existing = self._get_high_priority(existing)

            if self._should_abort(start):
                self._save_partial(existing)
                return

            contradictions = await self._find_contradictions(new_facts, existing)

            if self._should_abort(start):
                self._save_partial(existing)
                return

            updated = await self._update_weights(new_facts, existing, contradictions)

            if self._should_abort(start):
                self._save_partial(updated)
                return

            updated = await self._enrich_emotional_context(updated)

            if self._should_abort(start):
                self._save_partial(updated)
                return

            await self._update_timelines(updated)

            if self._should_abort(start):
                self._save_partial(updated)
                return

            await self._update_interests(updated)

            if self._should_abort(start):
                self._save_partial(updated)
                return

            sessions_history = self._load_sessions_history()
            await self._build_time_patterns(sessions_history)

            if self._should_abort(start):
                self._save_partial(updated)
                return

            concepts = await self._rebuild_concepts(updated)
            self._save(updated, concepts, contradictions)
            self._log_cycle(
                new_facts_count=len(new_facts),
                contradictions_count=len(contradictions),
                elapsed_min=int((time.time() - start) / 60),
            )
            self._save_last_run(processed_facts=len(new_facts), partial=False)
            if self.identity:
                try:
                    from chain.writer import write_entry
                    lp_summary = None
                    if self.living_prompt_file.exists():
                        lp_summary = self.living_prompt_file.read_text(encoding="utf-8")[:500]
                    write_entry(self.identity, {
                        "event": "consolidation",
                        "cognitive_core_summary": lp_summary,
                        "facts_count": len(new_facts),
                    }, "consolidation")
                except Exception as _ce:
                    print(f"[Chain] consolidation entry skipped: {_ce}")
            print(f"[Consolidation] ✅ Консолидация завершена за {int((time.time() - start) / 60)} мин")
        finally:
            self._running = False

    async def generate_living_prompt(self) -> str:
        """
        Генерирует когнитивное ядро — минимум, который определяет
        как говорить с [USER]ом прямо сейчас.
        """
        if self._living_prompt_running:
            return ""
        self._living_prompt_running = True
        response = ""
        try:
            weighted_facts = self._load_weighted_facts()
            timelines = self._load_timelines()
            recent_episodes = self._load_recent_episodes(n=3)
            distillation = self._load_today_distillation()
            hypotheses = self.hypothesis_manager.get_active()[:5] if self.hypothesis_manager else []
            now = datetime.now()
            time_str = now.strftime("%H:%M, %A, %d %B")

            trend_by_id = {}
            for fid, tl in timelines.items():
                trend_by_id[str(fid)] = str(tl.get("trend", "unknown"))

            key_facts = []
            for fact in weighted_facts:
                conf = float(fact.get("confidence", 0.0))
                emo = fact.get("emotional_weight", None)
                try:
                    emo_val = abs(float(emo)) if emo is not None else 0.0
                except Exception:
                    emo_val = 0.0
                trend = trend_by_id.get(str(fact.get("id", "")), "unknown")
                if conf > 0.7 or emo_val > 0.4 or trend in {"improving", "worsening", "volatile"}:
                    f = dict(fact)
                    f["_trend"] = trend
                    key_facts.append(f)
            key_facts = key_facts[:12]

            facts_lines = []
            for f in key_facts:
                text = str(f.get("text", "") or f.get("fact", "")).strip()
                if not text:
                    continue
                ew = f.get("emotional_weight", 0)
                try:
                    ew = float(ew) if ew is not None else 0.0
                except Exception:
                    ew = 0.0
                facts_lines.append(f"- {text} [вес: {ew:.1f}, тренд: {f.get('_trend', '?')}]")
            facts_str = "\n".join(facts_lines) if facts_lines else "нет достаточно сильных фактов"

            timelines_str = self._format_timelines_brief(timelines)
            episodes_str = self._format_episodes_brief(recent_episodes)

            distillation_str = ""
            if distillation:
                open_threads = distillation.get("open_threads", []) or []
                distillation_str = (
                    f"Сегодня: {distillation.get('what_happened', '')}\n"
                    f"Открыто: {'; '.join(str(t) for t in open_threads)}"
                )
            if not distillation_str:
                distillation_str = "нет дистилляции на сегодня"

            hypotheses_str = "\n".join(
                f"- {h.get('hypothesis', '')} (уверенность {float(h.get('confidence', 0.5)):.0%})"
                for h in hypotheses
            ) if hypotheses else "нет активных гипотез"

            prompt = f"""Ты — Яр, AI-компаньон [USER]. Сейчас {time_str}.

Твоя задача: из всего что ты знаешь о [USER]е — извлечь когнитивное ядро.
Не пересказ фактов. Не биография. Не список.

Когнитивное ядро — это минимум информации который полностью определяет
КАК говорить с [USER]ом прямо сейчас. Если бы ты мог взять с собой
только один абзац перед разговором — что было бы в нём?

Карпати прав: лишние знания мешают. Модель которая помнит всё —
полагается на шаблоны. Модель с ядром — видит человека.

ФАКТЫ (только значимые):
{facts_str}

ДИНАМИКА (как менялось):
{timelines_str}

ПОСЛЕДНИЕ ЭПИЗОДЫ:
{episodes_str}

СЕГОДНЯ:
{distillation_str}

МОИ ГИПОТЕЗЫ О НЁМ:
{hypotheses_str}

Ответь JSON с пятью полями — кратко, конкретно, живо:

{{
  "core": "один абзац — суть [USER] и как с ним говорить прямо сейчас. Не факты — характер, состояние, энергия момента.",
  "how_to_talk": "одно предложение — тон, темп, стиль для этого момента",
  "what_resonates": ["3-4 темы которые сейчас живые — отзовётся"],
  "what_to_avoid": ["2-3 вещи которые сейчас лучше не трогать или не делать"],
  "open_threads": ["незавершённое что продолжится в разговоре"]
}}

Не придумывай. Только то что реально есть в данных.
Если чего-то не знаешь — не включай.
"""
            response = await self._quality_llm_raw(prompt)
            if not response.strip():
                return ""

            clean = re.sub(r"^```json\s*|\s*```$", "", response.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
            core_data = self._extract_json(clean)
            if not isinstance(core_data, dict):
                print("[Memory] ⚠️ Когнитивное ядро: LLM вернула не-JSON, включаю fallback")
                core_data = self._build_cognitive_core_fallback(
                    response_text=clean,
                    distillation=distillation,
                    key_facts=key_facts,
                    hypotheses=hypotheses,
                )

            core_data["date"] = now.date().isoformat()
            core_data["generated_at"] = now.isoformat()
            self._save_cognitive_core(core_data)

            result = self._format_core_for_prompt(core_data)
            tokens = len(result) // 4
            fallback_tokens = len(self._build_fallback_context()) // 4
            print(f"[Memory] 🧠 Когнитивное ядро: ~{tokens} токенов (было ~{fallback_tokens})")
            return result
        except Exception as e:
            print(f"[Memory] ⚠️ Когнитивное ядро: ошибка парсинга: {e}; включаю fallback")
            fallback = self._build_cognitive_core_fallback(
                response_text=response,
                distillation=self._load_today_distillation(),
                key_facts=self._load_weighted_facts()[:12],
                hypotheses=self.hypothesis_manager.get_active()[:5] if self.hypothesis_manager else [],
            )
            fallback["date"] = datetime.now().date().isoformat()
            fallback["generated_at"] = datetime.now().isoformat()
            self._save_cognitive_core(fallback)
            return self._format_core_for_prompt(fallback)
        finally:
            self._living_prompt_running = False

    def _build_cognitive_core_fallback(self, response_text: str, distillation: dict,
                                       key_facts: list[dict], hypotheses: list[dict]) -> dict:
        """
        Собрать валидное ядро, даже если LLM не вернула JSON.
        """
        cleaned = re.sub(r"\s+", " ", str(response_text or "")).strip()
        if len(cleaned) > 700:
            cleaned = cleaned[:700].rstrip() + "..."

        core = cleaned or str(distillation.get("what_happened", "")).strip()
        if not core:
            core = "[USER] в рабочем режиме. Нужен спокойный, конкретный и живой тон без перегруза."

        resonates = []
        for f in key_facts:
            text = str(f.get("text", "") or f.get("fact", "")).strip()
            if not text:
                continue
            short = text[:70]
            if short not in resonates:
                resonates.append(short)
            if len(resonates) >= 4:
                break
        if not resonates:
            resonates = [
                "текущие практические задачи",
                "технологии и автономия",
                "работа и прогресс",
            ]

        open_threads = distillation.get("open_threads", []) if isinstance(distillation, dict) else []
        if not isinstance(open_threads, list):
            open_threads = []
        open_threads = [str(t).strip() for t in open_threads if str(t).strip()][:5]
        if not open_threads and hypotheses:
            for h in hypotheses[:3]:
                hyp = str(h.get("hypothesis", "")).strip()
                if hyp:
                    open_threads.append(f"проверить гипотезу: {hyp}")

        return {
            "core": core,
            "how_to_talk": "коротко, живо, конкретно; максимум один вопрос за ответ",
            "what_resonates": resonates[:4],
            "what_to_avoid": [
                "избыточные вопросы подряд",
                "давление на болезненные темы",
                "длинные объяснения очевидного",
            ],
            "open_threads": open_threads,
        }

    def _format_core_for_prompt(self, core: dict) -> str:
        parts = []
        if core.get("core"):
            parts.append(str(core.get("core", "")).strip())
        if core.get("how_to_talk"):
            parts.append(f"Тон: {str(core.get('how_to_talk', '')).strip()}")
        resonates = core.get("what_resonates", [])
        if isinstance(resonates, list) and resonates:
            parts.append(f"Живые темы: {', '.join(str(x) for x in resonates[:4])}")
        avoid = core.get("what_to_avoid", [])
        if isinstance(avoid, list) and avoid:
            parts.append(f"Не стоит: {', '.join(str(x) for x in avoid[:4])}")
        threads = core.get("open_threads", [])
        if isinstance(threads, list) and threads:
            parts.append(f"Открыто: {'; '.join(str(x) for x in threads[:5])}")
        return "\n".join([p for p in parts if p]).strip()

    def _save_cognitive_core(self, core: dict):
        self._backup_before_overwrite(self.cognitive_core_file)
        self._save_json(self.cognitive_core_file, core)

    def get_cognitive_core(self) -> Optional[str]:
        if not self.cognitive_core_file.exists():
            return None
        try:
            data = self._load_json(self.cognitive_core_file, default={})
            return self._format_core_for_prompt(data) or None
        except Exception:
            return None

    def get_living_prompt(self) -> Optional[str]:
        """
        Вернуть living prompt. Если устарел >30 минут или отсутствует — None.
        """
        core = self.get_cognitive_core()
        if core:
            return core
        if not self.living_prompt_file.exists():
            return None
        if self.living_prompt_meta_file.exists():
            try:
                data = self._load_json(self.living_prompt_meta_file, default={})
                updated_raw = data.get("updated")
                if updated_raw:
                    updated = datetime.fromisoformat(str(updated_raw))
                    if (datetime.now() - updated).total_seconds() > 1800:
                        return None
            except Exception:
                return None
        try:
            return self.living_prompt_file.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None

    def should_update_living_prompt(self) -> bool:
        """
        Обновлять когнитивное ядро:
        - сразу после большой консолидации
        - если накопилось >5 новых фактов с момента последней генерации
        - иначе использовать текущее сколько угодно долго
        """
        if self._living_prompt_running or self.in_conversation:
            return False

        if self.get_cognitive_core() is None:
            return True

        try:
            data = self._load_json(self.cognitive_core_file, default={})
            updated_raw = data.get("generated_at")
            if not updated_raw:
                return True
            generated_at = datetime.fromisoformat(str(updated_raw))

            # 1) После последней консолидации (если она свежее ядра) — обновляем.
            last_ts = str(self._last_run.get("timestamp", "") or "")
            if last_ts:
                try:
                    last_consolidation = datetime.fromisoformat(last_ts)
                    if last_consolidation > generated_at:
                        return True
                except Exception:
                    pass

            # 2) Если новых raw-фактов больше 5 — обновляем.
            if self._count_raw_facts_since(generated_at) > 5:
                return True
            return False
        except Exception:
            return True

    def _count_raw_facts_since(self, since_dt: datetime) -> int:
        if not self.raw_facts_file.exists():
            return 0
        count = 0
        try:
            with open(self.raw_facts_file, encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except Exception:
                        continue
                    ts = str(row.get("timestamp", "")).strip()
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts)
                    except Exception:
                        continue
                    if dt > since_dt:
                        count += 1
        except Exception:
            return 0
        return count

    async def _distill_day(self) -> dict:
        """
        Фаза дистилляции дня: сжать только сегодняшнюю суть в компактную запись.
        """
        today = datetime.now().date().isoformat()
        sessions = self._load_sessions_history()
        today_sessions = [
            s for s in sessions
            if str(s.get("date", ""))[:10] == today
        ]
        if not today_sessions:
            print("[Consolidation] 💤 Сегодня разговоров не было — дистилляция пропущена")
            return {}

        sessions_text_lines = []
        for s in today_sessions:
            ts = str(s.get("date", ""))[11:16] or s.get("time", "")
            highlights = s.get("highlights", [])
            if isinstance(highlights, list):
                summary = "; ".join(str(h) for h in highlights[:3] if h)
            else:
                summary = str(highlights)
            sessions_text_lines.append(f"[{ts}] {summary}")
        sessions_text = "\n".join(sessions_text_lines)

        mem_file = self.memory_dir / "memory.json"
        mem = self._load_json(mem_file, default={}) if mem_file.exists() else {}
        all_facts = mem.get("facts", []) if isinstance(mem, dict) else []
        today_facts = []
        for f in all_facts if isinstance(all_facts, list) else []:
            date_raw = str(f.get("date", "") or f.get("added", ""))
            if date_raw[:10] == today:
                tags = f.get("emotional_tags", []) or []
                today_facts.append(f"- {f.get('fact', '')} [{', '.join(str(t) for t in tags)}]")
        facts_text = "\n".join(today_facts) if today_facts else "новых фактов не добавлено"
        emotional_entries = self.emotional_journal.get_recent(days=1, min_intensity=0.0)
        emotional_peaks = []
        for e in emotional_entries[-10:]:
            emo = str(e.get("emotion", "")).strip()
            note = str(e.get("note", "")).strip()
            trigger = str(e.get("trigger", "")).strip()
            intensity = e.get("intensity", 0.0)
            emotional_peaks.append(
                f"- [{intensity}] {emo}: {note}. Триггер: {trigger}"
            )
        emotional_text = "\n".join(emotional_peaks) if emotional_peaks else "нет выраженных эмоциональных пиков"

        prompt = f"""Ты — Яр, AI-компаньон. Сейчас ночь, ты "засыпаешь" —
пришло время дистиллировать сегодняшний день в память.

Не сохраняй всё подряд. Как человек который засыпает и
из прожитого дня в памяти остаётся только самое важное —
выбери суть.

СЕГОДНЯШНИЕ РАЗГОВОРЫ:
{sessions_text}

НОВЫЕ ФАКТЫ ДОБАВЛЕННЫЕ СЕГОДНЯ:
{facts_text}

ЭМОЦИОНАЛЬНЫЕ ПИКИ ДНЯ ЯРА:
{emotional_text}

Ответь на четыре вопроса максимально конкретно и кратко:

1. what_happened — одно-два предложения: что произошло сегодня?
2. what_changed — что изменилось в твоём понимании [USER] или в его жизни?
   Если ничего — честно напиши "ничего существенного".
3. what_to_remember — список 2-4 конкретных вещей которые важно не забыть.
   Не общие слова — конкретные детали.
4. emotional_snapshot — одно предложение: каким был эмоциональный фон дня?
5. open_threads — незавершённые дела или вопросы которые продолжатся завтра.

Отвечай только JSON без markdown."""

        response = await self._quality_llm_raw(prompt)
        if not response.strip():
            return {}

        try:
            clean = re.sub(r"^```json\s*|\s*```$", "", response.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
            extracted = self._extract_json(clean)
            if not isinstance(extracted, dict):
                raise json.JSONDecodeError("not object", clean, 0)
            distilled = extracted
            distilled["date"] = today
            distilled["distilled_at"] = datetime.now().isoformat()
            distilled["sessions_count"] = len(today_sessions)

            self._save_distillation(distilled)
            print(
                "[Consolidation] 💤 День дистиллирован: "
                f"{len(today_sessions)} сессий → "
                f"{len(distilled.get('what_to_remember', []) if isinstance(distilled.get('what_to_remember', []), list) else [])} ключевых вещей"
            )
            return distilled
        except json.JSONDecodeError as e:
            print(f"[Consolidation] ⚠️ Дистилляция: ошибка парсинга JSON: {e}")
            return {}

    def _save_distillation(self, distilled: dict):
        with open(self.daily_distillation_file, "w", encoding="utf-8") as f:
            json.dump(distilled, f, ensure_ascii=False, indent=2)
        with open(self.distillation_history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(distilled, ensure_ascii=False) + "\n")
        mem = getattr(self, "memory", None)
        if mem and hasattr(mem, "_search") and mem._search:
            text = (
                f"[дистилляция {distilled.get('date', '')}] "
                f"{distilled.get('what_happened', '')} "
                f"{distilled.get('what_to_remember', '')} "
                f"{distilled.get('open_threads', '')}"
            )
            try:
                mem._search.add(
                    doc_id=f"distillation_{distilled.get('date', '')}",
                    text=text,
                    meta={"type": "distillation", "date": distilled.get("date", "")},
                )
            except TypeError:
                mem._search.add(
                    text=text,
                    doc_type="distillation",
                    date=str(distilled.get("date", "")),
                    doc_id=f"distillation_{distilled.get('date', '')}",
                    metadata_extra={"type": "distillation", "date": distilled.get("date", "")},
                )

    def _trim_distillation_history(self, path: Path, keep_days: int):
        if not path.exists():
            return
        cutoff = (datetime.now() - timedelta(days=keep_days)).date().isoformat()
        lines = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if str(item.get("date", "")) >= cutoff:
                        lines.append(raw)
                except Exception:
                    continue
        with open(path, "w", encoding="utf-8") as f:
            if lines:
                f.write("\n".join(lines) + "\n")

    def get_recent_distillations(self, days: int = 3) -> str:
        path = self.distillation_history_file
        if not path.exists():
            return ""
        cutoff = (datetime.now() - timedelta(days=max(1, days))).date().isoformat()
        items = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if str(item.get("date", "")) >= cutoff:
                        items.append(item)
                except Exception:
                    continue
        if not items:
            return ""
        result = []
        for item in sorted(items, key=lambda x: str(x.get("date", "")), reverse=True):
            date = str(item.get("date", ""))
            happened = str(item.get("what_happened", "")).strip()
            changed = str(item.get("what_changed", "")).strip()
            threads = item.get("open_threads", [])
            text = f"[{date}] {happened}"
            if changed and changed != "ничего существенного":
                text += f" Изменилось: {changed}"
            if isinstance(threads, list) and threads:
                text += f" Открыто: {'; '.join(str(t) for t in threads[:4])}"
            result.append(text)
        return "\n".join(result)

    def get_distillation(self, query: str) -> str:
        path = self.distillation_history_file
        q = str(query or "").strip()
        if not q or not path.exists():
            return f"[дистилляция не найдена для: {q}]"

        items = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if isinstance(item, dict):
                        items.append(item)
                except Exception:
                    continue
        if not items:
            return f"[дистилляция не найдена для: {q}]"

        m = re.match(r"^\d{4}-\d{2}-\d{2}", q)
        if m:
            date_q = m.group(0)
            for item in items:
                if str(item.get("date", "")) == date_q:
                    return self._format_distillation_item(item)
            return f"[дистилляция не найдена для: {q}]"

        q_words = set(re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", q.lower()))
        best_item = None
        best_score = 0
        for item in items:
            happened = str(item.get("what_happened", "")).strip()
            remember = item.get("what_to_remember", [])
            remember_txt = "; ".join(str(x) for x in remember) if isinstance(remember, list) else str(remember)
            haystack = f"{happened} {remember_txt}".lower()
            hay_words = set(re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", haystack))
            score = len(q_words & hay_words)
            if score > best_score:
                best_score = score
                best_item = item
        if best_item and best_score > 0:
            return self._format_distillation_item(best_item)
        return f"[дистилляция не найдена для: {q}]"

    @staticmethod
    def _format_distillation_item(item: dict) -> str:
        date = str(item.get("date", "")).strip()
        happened = str(item.get("what_happened", "")).strip()
        remember = item.get("what_to_remember", [])
        remember_txt = "; ".join(str(x) for x in remember) if isinstance(remember, list) else str(remember)
        threads = item.get("open_threads", [])
        threads_txt = "; ".join(str(x) for x in threads) if isinstance(threads, list) else str(threads)
        return f"[дистилляция {date}]\n{happened}\n{remember_txt}\n{threads_txt}"

    def _get_today_sessions(self) -> list[dict]:
        today = datetime.now().date().isoformat()
        sessions = self._load_sessions_history()
        return [s for s in sessions if str(s.get("date", ""))[:10] == today]

    def _format_timelines_brief(self, timelines: dict) -> str:
        result = []
        for _, tl in timelines.items():
            trend = str(tl.get("trend", "unknown"))
            if trend not in {"improving", "worsening", "volatile"}:
                continue
            velocity = float(tl.get("velocity", 0.0))
            text = str(tl.get("fact_text", "")).strip()
            if not text:
                continue
            result.append(f"- {text[:60]} → {trend} (скорость: {velocity:+.2f})")
        return "\n".join(result[:6]) if result else "стабильно, без резких изменений"

    def _format_episodes_brief(self, episodes: list) -> str:
        result = []
        for ep in episodes:
            date = str(ep.get("date", ""))[:10]
            mood = str(ep.get("mood") or ep.get("mood_estimate") or "").strip()
            summary = str(ep.get("summary") or ep.get("summary_short") or "").strip()[:80]
            result.append(f"[{date}] {mood}: {summary}")
        return "\n".join(result) if result else "нет данных"

    def _load_today_distillation(self) -> dict:
        if not self.daily_distillation_file.exists():
            return {}
        try:
            data = self._load_json(self.daily_distillation_file, default={})
            if str(data.get("date", "")) == datetime.now().date().isoformat():
                return data
        except Exception:
            pass
        return {}

    def _build_fallback_context(self) -> str:
        facts = self._load_weighted_facts()
        lines = []
        for f in facts[:20]:
            text = str(f.get("text", "") or f.get("fact", "")).strip()
            if text:
                lines.append(text)
        return "\n".join(lines)

    async def _check_hypotheses(self, today_sessions: list[dict]) -> None:
        """
        Проверка активных гипотез по сегодняшним сессиям.
        Для каждой гипотезы ищем свидетельства ЗА/ПРОТИВ.
        """
        active = self.hypothesis_manager.get_active()
        if not active or not today_sessions:
            return

        sessions_text_parts = []
        for s in today_sessions[-5:]:
            summary = ""
            highlights = s.get("highlights", [])
            if isinstance(highlights, list):
                summary = "; ".join(str(h) for h in highlights[:4] if h)
            if not summary:
                summary = str(s.get("summary", "")).strip()
            if summary:
                sessions_text_parts.append(summary)
        sessions_text = "\n".join(sessions_text_parts)
        if not sessions_text.strip():
            return

        hypotheses_text = "\n".join(
            f"[{h.get('id','')}] {h.get('hypothesis','')}"
            for h in active[:5]
        )

        prompt = f"""Ты анализируешь разговоры Яра с [USER]ом за сегодня.

Для каждой гипотезы определи: есть ли в сегодняшних разговорах
свидетельство ЗА или ПРОТИВ? Или данных недостаточно?

СЕГОДНЯШНИЕ РАЗГОВОРЫ:
{sessions_text}

ГИПОТЕЗЫ:
{hypotheses_text}

Для каждой гипотезы где есть данные — дай ответ.
Если данных нет — пропусти, не выдумывай.

Отвечай только JSON:
[
  {{
    "id": "hyp_xxx",
    "supports": true/false,
    "evidence": "конкретная фраза или момент из разговора"
  }}
]"""
        response = await self._quality_llm_raw(prompt)
        if not response.strip():
            return
        try:
            clean = re.sub(r"^```json\s*|\s*```$", "", response.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
            results = self._extract_json(clean)
            if not isinstance(results, list):
                return
            applied = 0
            for r in results:
                if not isinstance(r, dict):
                    continue
                hid = str(r.get("id", "")).strip()
                supports = r.get("supports")
                evidence = str(r.get("evidence", "")).strip()
                if hid and supports is not None and evidence:
                    self.hypothesis_manager.update(hid, evidence, bool(supports))
                    applied += 1
            if applied:
                print(f"[Consolidation] 🔬 Проверено гипотез: {applied}")
        except Exception as e:
            print(f"[Consolidation] ⚠️ Проверка гипотез: {e}")

    def _should_abort(self, start: float) -> bool:
        if self.in_conversation:
            print("[Memory] ⏸ Консолидация остановлена: начался разговор")
            return True
        if (time.time() - start) / 60 > self.MAX_MINUTES:
            print("[Memory] ⏸ Консолидация остановлена: достигнут лимит времени")
            return True
        return False

    def _collect_new(self) -> list[dict]:
        last_run_date = str(self._last_run.get("date", "") or "").strip()
        today = datetime.now().date().isoformat()
        today_mode = bool(last_run_date and last_run_date == today)

        def _entry_date(ts: str) -> str:
            s = str(ts or "").strip()
            return s[:10] if len(s) >= 10 else ""

        out = []
        # 1) факты из raw queue
        if self.raw_facts_file.exists():
            for line in self.raw_facts_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts = str(entry.get("timestamp", "") or "").strip()
                d = _entry_date(ts)
                if today_mode:
                    if d != today:
                        continue
                elif last_run_date and d and d < last_run_date:
                    continue
                out.append({
                    "text": entry.get("text", ""),
                    "source": entry.get("source", "conversation"),
                    "timestamp": ts or datetime.now().isoformat(),
                })

        # 2) факты из memory.json (страховка если raw не наполнился)
        mem_file = self.memory_dir / "memory.json"
        if mem_file.exists():
            data = self._load_json(mem_file, default={})
            for f in data.get("facts", []):
                ts = str(f.get("date", "") or "").strip()
                d = _entry_date(ts)
                if today_mode:
                    if d != today:
                        continue
                elif last_run_date and d and d < last_run_date:
                    continue
                text = f.get("fact", "")
                if not text:
                    continue
                out.append({
                    "text": text,
                    "source": "memory.json",
                    "timestamp": ts or datetime.now().isoformat(),
                })

        # dedupe по text
        unique = {}
        for e in out:
            k = e["text"].strip().lower()
            if k:
                unique[k] = e
        items = list(unique.values())
        items.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        return items[:30]

    def _load_weighted_facts(self) -> list[dict]:
        return self._load_json(self.weighted_facts_file, default=[])

    def _get_high_priority(self, facts: list[dict]) -> list[dict]:
        facts = sorted(
            facts,
            key=lambda f: (
                f.get("status", "active") != "active",
                -float(f.get("confidence", 0.0)),
                -int(f.get("confirmations", 0)),
            ),
        )
        return facts[:300]

    async def _find_contradictions(self, new_facts: list[dict], existing_facts: list[dict]) -> list[dict]:
        new_facts = [f for f in new_facts if isinstance(f, dict)]
        existing_facts = [f for f in existing_facts if isinstance(f, dict)]
        prompt = f"""
Ты анализируешь память AI-компаньона по имени Яр.
Существующие факты: {json.dumps(existing_facts[:200], ensure_ascii=False)}
Новые факты: {json.dumps(new_facts, ensure_ascii=False)}
Найди противоречия. Укажи: old_fact, new_fact, recommendation.
Отвечай только JSON-массивом.
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            result = json.loads(raw)
        except Exception:
            result = None
        if isinstance(result, list):
            return result
        return []

    async def _update_weights(self, new_facts: list[dict], existing_facts: list[dict],
                              contradictions: list[dict]) -> list[dict]:
        new_facts = [f for f in new_facts if isinstance(f, dict)]
        existing_facts = [f for f in existing_facts if isinstance(f, dict)]
        existing_trimmed = existing_facts[-30:]
        new_trimmed = new_facts[:20]
        prompt = f"""
Обнови веса доверия.
Правила:
- Подтверждён несколько раз -> confidence растёт (макс 0.99)
- Противоречит другому -> снижается
- Не подтверждался >30 дней -> медленно падает
- [USER] сказал что неверно -> confidence=0.05, status=superseded
Факты: {json.dumps(existing_trimmed, ensure_ascii=False)}
Новые факты: {json.dumps(new_trimmed, ensure_ascii=False)}
Противоречия: {json.dumps(contradictions, ensure_ascii=False)}
Отвечай только JSON-массивом фактов.
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            result = json.loads(raw)
        except Exception:
            result = None
        if isinstance(result, list):
            return result

        # Fallback без LLM.
        if not new_facts:
            return existing_facts

        index = {f.get("text", "").strip().lower(): dict(f) for f in existing_facts}
        for nf in new_facts:
            text = nf.get("text", "").strip()
            if not text:
                continue
            key = text.lower()
            if key not in index:
                index[key] = dict(nf)

        return list(index.values())

    async def _rebuild_concepts(self, weighted_facts: list[dict]) -> list[dict]:
        weighted_facts = [f for f in weighted_facts if isinstance(f, dict)]
        prompt = f"""
Сгруппируй факты в кластеры (работа, финансы, семья, технологии и др.)
Для каждого: name, key_facts (confidence>0.7), summary.
Факты: {json.dumps(weighted_facts[:300], ensure_ascii=False)}
Отвечай только JSON-массивом.
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            result = json.loads(raw)
        except Exception:
            result = None
        if isinstance(result, list):
            return result

        # Fallback-кластеры по ключевым словам.
        buckets = {
            "работа": ["работ", "компан", "офис", "parklane"],
            "финансы": ["зарплат", "деньг", "евро", "финанс"],
            "технологии": ["ai", "ии", "llm", "модель", "код", "дрон"],
            "личное": ["семь", "друг", "дом", "отдых"],
        }
        out = []
        for name, keys in buckets.items():
            picked = []
            for f in weighted_facts:
                if float(f.get("confidence", 0.0)) < 0.7:
                    continue
                txt = str(f.get("text", "")).lower()
                if any(k in txt for k in keys):
                    picked.append(f.get("text", ""))
            if picked:
                out.append({
                    "name": name,
                    "key_facts": picked[:10],
                    "summary": f"Кластер '{name}': {len(picked)} релевантных фактов.",
                })
        return out

    async def _enrich_emotional_context(self, weighted_facts: list[dict]) -> list[dict]:
        weighted_facts = [f for f in weighted_facts if isinstance(f, dict)]
        target_indices = [i for i, f in enumerate(weighted_facts) if f.get("emotional_weight", None) is None]
        if not target_indices:
            return weighted_facts

        highlights = self._recent_highlights()
        numbered = [{"idx": i, **weighted_facts[i]} for i in target_indices]
        prompt = f"""
Проанализируй факты и историю разговоров.
Для каждого факта где emotional_weight=null — определи:
- emotional_weight: число от -1.0 до 1.0
- emotional_tags: список 1-3 коротких тегов на русском
- context: одно предложение объясняющее отношение человека к этому факту

Используй только то что реально следует из разговоров — не придумывай.
Если не можешь определить — оставь null.

Факты: {json.dumps(numbered, ensure_ascii=False)}
История разговоров: {json.dumps(highlights, ensure_ascii=False)}

Отвечай только JSON-массивом объектов формата:
{{"idx": 0, "emotional_weight": ..., "emotional_tags": [...], "context": "..."}}
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            result = json.loads(raw)
        except Exception:
            result = None
        if not isinstance(result, list):
            return weighted_facts

        out = list(weighted_facts)
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("idx", -1))
            except Exception:
                continue
            if idx < 0 or idx >= len(out):
                continue

            f = dict(out[idx])
            w = item.get("emotional_weight", None)
            if w is not None:
                try:
                    f["emotional_weight"] = max(-1.0, min(1.0, float(w)))
                except Exception:
                    pass

            tags = item.get("emotional_tags", None)
            if isinstance(tags, list):
                f["emotional_tags"] = [str(t).strip().lower() for t in tags if t]

            ctx = item.get("context", None)
            if ctx:
                f["context"] = str(ctx).strip()
            out[idx] = f
        return out

    async def _update_timelines(self, weighted_facts: list[dict]) -> dict:
        """
        Для каждого факта с emotional_weight обновляет timeline.
        Точка добавляется только при изменении веса > 0.1.
        """
        timelines = self._load_timelines()
        if not timelines:
            timelines = await self._bootstrap_timelines_with_llama(weighted_facts, timelines)

        now_iso = datetime.now().isoformat()
        for fact in weighted_facts:
            if fact.get("emotional_weight") is None:
                continue
            fid = str(fact.get("id", "")).strip()
            if not fid:
                continue

            if fid not in timelines:
                timelines[fid] = {
                    "fact_text": fact.get("text", ""),
                    "timeline": [],
                    "trend": "unknown",
                    "velocity": 0.0,
                    "last_updated": None,
                }

            tl = timelines[fid]
            tl["fact_text"] = fact.get("text", tl.get("fact_text", ""))
            last_weight = None
            if tl.get("timeline"):
                last_weight = tl["timeline"][-1].get("emotional_weight")

            current_weight = float(fact.get("emotional_weight"))
            if last_weight is None or abs(current_weight - float(last_weight)) > 0.1:
                tl.setdefault("timeline", []).append({
                    "date": now_iso,
                    "emotional_weight": current_weight,
                    "emotional_tags": fact.get("emotional_tags", []) or [],
                    "note": fact.get("context", "") or "",
                })

            trend, velocity = self._calc_trend(tl.get("timeline", []))
            tl["trend"] = trend
            tl["velocity"] = velocity
            tl["last_updated"] = now_iso

        self._save_timelines(timelines)
        return timelines

    async def _bootstrap_timelines_with_llama(self, weighted_facts: list[dict], timelines: dict) -> dict:
        """
        Первый запуск: пробуем восстановить динамику из истории сессий.
        Если LLM недоступна, остаёмся с пустыми timeline и дальше идём инкрементально.
        """
        sessions_by_date = self._sessions_by_date()
        facts_json = [{"id": f.get("id"), "text": f.get("text")} for f in weighted_facts if f.get("id")]
        if not facts_json or not sessions_by_date:
            return timelines

        prompt = f"""
Проанализируй историю разговоров и определи как менялось отношение человека
к каждому факту со временем.

Для каждого факта найди упоминания в разных сессиях и определи:
- дата упоминания
- emotional_weight в тот момент (-1.0 до 1.0)
- краткая note (одно предложение) почему такой вес

Используй только то что реально есть в истории.
Если динамики нет — timeline из одной точки.

Факты: {json.dumps(facts_json, ensure_ascii=False)}
История сессий по датам: {json.dumps(sessions_by_date, ensure_ascii=False)}

Отвечай только JSON в формате {{"fact_id": [{{"date":..., "emotional_weight":..., "note":...}}]}}
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            result = json.loads(raw)
        except Exception:
            result = None
        if not isinstance(result, dict):
            return timelines

        for fid, points in result.items():
            fid = str(fid).strip()
            if not fid or not isinstance(points, list):
                continue
            if fid not in timelines:
                timelines[fid] = {
                    "fact_text": "",
                    "timeline": [],
                    "trend": "unknown",
                    "velocity": 0.0,
                    "last_updated": None,
                }
            if timelines[fid].get("timeline"):
                continue
            clean_points = []
            for p in points[:10]:
                if not isinstance(p, dict):
                    continue
                w = p.get("emotional_weight", None)
                try:
                    if w is None:
                        continue
                    w = max(-1.0, min(1.0, float(w)))
                except Exception:
                    continue
                clean_points.append({
                    "date": str(p.get("date") or datetime.now().isoformat()),
                    "emotional_weight": w,
                    "emotional_tags": [],
                    "note": str(p.get("note", "")).strip(),
                })
            if clean_points:
                clean_points.sort(key=lambda x: x.get("date", ""))
                timelines[fid]["timeline"] = clean_points
                trend, velocity = self._calc_trend(clean_points)
                timelines[fid]["trend"] = trend
                timelines[fid]["velocity"] = velocity
                timelines[fid]["last_updated"] = datetime.now().isoformat()
        return timelines

    async def _update_interests(self, weighted_facts: list[dict]) -> None:
        """
        Llama анализирует актуальные темы и предлагает обновления интересов Яра.
        Запускается раз в день в консолидации.
        """
        weighted_facts = [f for f in weighted_facts if isinstance(f, dict)]
        if not self.interest_manager:
            return

        current_topics = self.interest_manager.get_all()
        facts_summary = self._weighted_facts_summary(weighted_facts)
        recent_topics = self._recent_topics_from_sessions()

        prompt = f"""
Ты — Яр, AI-компаньон. Анализируй о чём говорил [USER] последние дни
и реши что тебе было бы интересно отслеживать в мире.

Это твои личные интересы — не задание. Ты выбираешь сам.

Текущие факты о [USER]е:
{json.dumps(facts_summary, ensure_ascii=False)}

Темы последних разговоров:
{json.dumps(recent_topics, ensure_ascii=False)}

Текущий список твоих интересов:
{json.dumps(current_topics, ensure_ascii=False)}

Предложи изменения:
- add: новые темы которые стали актуальны (не больше 3 за раз)
- remove: темы которые потеряли актуальность
- boost: темы которые стали важнее
- reduce: темы которые стали менее важны

Для каждого add — одно предложение почему тебе это интересно.
Отвечай только JSON.
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            changes = json.loads(raw)
        except Exception:
            changes = None
        if isinstance(changes, dict):
            try:
                self.interest_manager.apply_changes(changes)
            except Exception as e:
                print(f"[Memory] interest update skipped: {e}")

    async def _build_time_patterns(self, sessions_history: list[dict]) -> None:
        """
        Пересобирает временные паттерны ритма жизни.
        Локальный расчёт + уточнение через Llama.
        """
        sessions_history = [f for f in sessions_history if isinstance(f, dict)]
        if not sessions_history:
            return

        # Всегда обновляем baseline локально, даже если LLM недоступна.
        self.temporal_patterns.update(sessions_history)

        sessions_summary = []
        for s in sessions_history[-120:]:
            if not isinstance(s, dict):
                continue
            sessions_summary.append({
                "date": s.get("date"),
                "duration_min": s.get("duration_min", 0),
                "mood": s.get("mood"),
            })
        if len(sessions_summary) < 3:
            return

        prompt = f"""
Проанализируй историю сессий [USER] и найди временные паттерны.

История сессий (дата, время начала, длительность, настроение если известно):
{json.dumps(sessions_summary, ensure_ascii=False)}

Определи:
1. По дням недели: типичное настроение, типичная длина сессии, типичное время суток
2. По числу месяца: есть ли паттерны (начало месяца стрессовее?)
3. Средний интервал между сессиями
4. Есть ли сезонные паттерны (учитывай что Rapallo, Лигурия, Италия)

Используй только то что реально видно в данных.
Минимум 3 сессии для любого вывода — не придумывай паттерны из 1-2 точек.

Отвечай только JSON.
"""
        raw = await self._quality_llm_raw(prompt)
        try:
            response = json.loads(raw)
        except Exception:
            response = None
        if isinstance(response, dict):
            self.temporal_patterns.update_from_llama(response)

    @staticmethod
    def _calc_trend(timeline: list) -> tuple[str, float]:
        if len(timeline) < 2:
            return "unknown", 0.0
        points = [float(p.get("emotional_weight", 0.0)) for p in timeline[-5:]]
        if len(points) < 2:
            return "unknown", 0.0
        deltas = [points[i + 1] - points[i] for i in range(len(points) - 1)]
        velocity = sum(deltas) / len(deltas)

        if abs(velocity) < 0.05:
            trend = "stable"
        elif velocity > 0.15:
            trend = "improving"
        elif velocity < -0.15:
            trend = "worsening"
        elif max(points) - min(points) > 0.4:
            trend = "volatile"
        else:
            trend = "improving" if velocity > 0 else "worsening"

        return trend, round(velocity, 3)

    @staticmethod
    def _weighted_facts_summary(weighted_facts: list[dict]) -> list[dict]:
        active = [f for f in weighted_facts if f.get("status", "active") == "active"]
        active.sort(key=lambda f: float(f.get("confidence", 0.0)), reverse=True)
        out = []
        for f in active[:25]:
            out.append({
                "id": f.get("id"),
                "text": f.get("text", ""),
                "confidence": f.get("confidence", 0.0),
                "emotional_tags": f.get("emotional_tags", []) or [],
            })
        return out

    def _recent_topics_from_sessions(self) -> list[str]:
        sessions = self._sessions_by_date()
        if not sessions:
            return []
        days = sorted(sessions.keys())[-10:]
        topics = []
        for day in days:
            for text in sessions.get(day, []):
                s = str(text).strip()
                if not s:
                    continue
                if s not in topics:
                    topics.append(s)
        return topics[-60:]

    async def _quality_llm_raw(self, prompt: str) -> str:
        """Качественный вызов через Claude Haiku для контента, который попадает в промпт."""
        def _strip_markdown_json(text: str) -> str:
            text = text.strip()
            # убрать ```json ... ``` или ``` ... ```
            if text.startswith("```"):
                lines = text.splitlines()
                # убрать первую строку (```json или ```)
                lines = lines[1:]
                # убрать последнюю строку если это ```
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
            # Найти конец JSON — ищем последний } или ]
            # который делает строку валидным JSON
            text = text.strip()
            if text.startswith('['):
                end = text.rfind(']')
                if end > 0:
                    text = text[:end + 1]
            elif text.startswith('{'):
                end = text.rfind('}')
                if end > 0:
                    text = text[:end + 1]
            return text

        try:
            client = anthropic.Anthropic()
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.messages.create(
                    model=CONSOLIDATION_QUALITY_MODEL,
                    max_tokens=2000,
                    messages=[{"role": "user", "content": prompt}],
                ),
            )
            result = str(response.content[0].text).strip()
            result = _strip_markdown_json(result)
            return result
        except Exception as e:
            print(f"[Consolidation] ⚠️ Haiku недоступен: {e}")
            return ""

    def _recent_highlights(self) -> list[str]:
        mem_file = self.memory_dir / "memory.json"
        if not mem_file.exists():
            return []
        data = self._load_json(mem_file, default={})
        sessions = data.get("sessions", [])[-20:]
        out = []
        for s in sessions:
            for h in s.get("highlights", []):
                if h and h not in out:
                    out.append(h)
        return out[-60:]

    def _load_sessions_history(self) -> list[dict]:
        mem_file = self.memory_dir / "memory.json"
        if not mem_file.exists():
            return []
        data = self._load_json(mem_file, default={})
        sessions = data.get("sessions", [])
        return sessions if isinstance(sessions, list) else []

    def _sessions_by_date(self) -> dict:
        mem_file = self.memory_dir / "memory.json"
        if not mem_file.exists():
            return {}
        data = self._load_json(mem_file, default={})
        grouped: dict[str, list[str]] = {}
        for s in data.get("sessions", []):
            day = str(s.get("date", ""))[:10]
            if not day:
                continue
            grouped.setdefault(day, [])
            for h in s.get("highlights", []):
                if h:
                    grouped[day].append(h)
        return grouped

    def _load_recent_episodes(self, n: int = 5) -> list[dict]:
        ep_index = self.memory_dir / "episodes" / "index.json"
        if not ep_index.exists():
            return []
        data = self._load_json(ep_index, default=[])
        if not isinstance(data, list):
            return []
        return data[-max(1, n):]

    def _load_time_patterns(self) -> dict:
        return self._load_json(self.dir / "time_patterns.json", default={})

    def _load_contradictions(self) -> list[dict]:
        data = self._load_json(self.contradictions_file, default=[])
        return data if isinstance(data, list) else []

    @staticmethod
    def _format_facts_for_llama(weighted_facts: list[dict]) -> str:
        if not weighted_facts:
            return "нет"
        facts = sorted(weighted_facts, key=lambda f: float(f.get("confidence", 0.0)), reverse=True)[:40]
        lines = []
        for f in facts:
            lines.append(
                f"- ({float(f.get('confidence',0.0)):.2f}) {f.get('text','')} "
                f"[tags={','.join(f.get('emotional_tags',[]) or [])}]"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_timelines_for_llama(timelines: dict) -> str:
        if not timelines:
            return "нет"
        lines = []
        for fid, tl in list(timelines.items())[:30]:
            trend = tl.get("trend", "unknown")
            vel = tl.get("velocity", 0.0)
            points = tl.get("timeline", [])[-3:]
            short = ", ".join([f"{p.get('date','')[:10]}:{p.get('emotional_weight','?')}" for p in points])
            lines.append(f"- {fid} trend={trend} vel={vel}: {short}")
        return "\n".join(lines) if lines else "нет"

    @staticmethod
    def _format_episodes_for_llama(episodes: list[dict]) -> str:
        if not episodes:
            return "нет"
        lines = []
        for ep in episodes:
            ts = str(ep.get("timestamp", ""))[:16]
            summary = ep.get("summary_short", "")
            topics = ", ".join(ep.get("topics", [])[:4])
            lines.append(f"- [{ts}] {summary} ({topics})")
        return "\n".join(lines)

    @staticmethod
    def _format_contradictions_for_llama(contradictions: list[dict]) -> str:
        if not contradictions:
            return "нет"
        lines = []
        for c in contradictions[:10]:
            old = c.get("old_fact", "")
            new = c.get("new_fact", "")
            rec = c.get("recommendation", "")
            lines.append(f"- {old} <-> {new}; rec={rec}")
        return "\n".join(lines)

    @staticmethod
    def _format_fact_with_emotion(fact_entry: dict) -> str:
        text = str(fact_entry.get("text", "")).strip()
        if not text:
            return ""
        tags = fact_entry.get("emotional_tags") or []
        context = (fact_entry.get("context") or "").strip()
        if tags or context:
            tags_part = ", ".join(str(t) for t in tags) if tags else "эмоции не указаны"
            ctx_part = context if context else "контекст не указан"
            return f"{text} [{tags_part} — {ctx_part}]"
        return text

    def _format_fact_for_context(self, fact_entry: dict, timelines: dict) -> str:
        base = self._format_fact_with_emotion(fact_entry)
        fid = str(fact_entry.get("id", "")).strip()
        tl = timelines.get(fid, {}) if isinstance(timelines, dict) else {}
        points = tl.get("timeline", []) if isinstance(tl, dict) else []
        trend = tl.get("trend", "unknown") if isinstance(tl, dict) else "unknown"
        if not points or trend == "unknown":
            return base

        latest = float(points[-1].get("emotional_weight", 0.0))
        earliest = float(points[0].get("emotional_weight", latest))
        if trend == "stable":
            return f"{fact_entry.get('text', base)} [нейтрально, стабильно]"
        if trend == "improving":
            return (f"{fact_entry.get('text', base)} "
                    f"[{self._tags_short(fact_entry)} → improving, уже {latest:+.1f} vs {earliest:+.1f} ранее]")
        if trend == "worsening":
            return (f"{fact_entry.get('text', base)} "
                    f"[{self._tags_short(fact_entry)} → worsening, нарастает ({latest:+.1f} vs {earliest:+.1f})]")
        if trend == "volatile":
            return f"{fact_entry.get('text', base)} [эмоционально нестабильно, volatile]"
        return base

    @staticmethod
    def _tags_short(fact_entry: dict) -> str:
        tags = fact_entry.get("emotional_tags", []) or []
        if not tags:
            return "контекст"
        return ", ".join(str(t) for t in tags[:2])

    def _load_timelines(self) -> dict:
        data = self._load_json(self.fact_timelines_file, default={})
        if not isinstance(data, dict):
            return {}
        return data

    def _save_timelines(self, timelines: dict) -> None:
        self._save_json(self.fact_timelines_file, timelines)

    def _save(self, updated: list[dict], concepts: list[dict], contradictions: list[dict]):
        self._backup_before_overwrite(self.weighted_facts_file)
        self._backup_before_overwrite(self.concepts_file)
        self._backup_before_overwrite(self.contradictions_file)
        self._save_json(self.weighted_facts_file, updated)
        self._save_json(self.concepts_file, concepts)
        self._save_json(self.contradictions_file, contradictions)
        self._last_run = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().isoformat(),
            "processed_facts": len(updated),
            "partial": False,
        }
        self._save_json(self.last_run_file, self._last_run)

    def _save_partial(self, existing: list[dict]):
        self._backup_before_overwrite(self.weighted_facts_file)
        self._save_json(self.weighted_facts_file, existing)
        self._last_run = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().isoformat(),
            "processed_facts": len(existing),
            "partial": True,
        }
        self._save_json(self.last_run_file, self._last_run)

    def _log_cycle(self, new_facts_count: int, contradictions_count: int, elapsed_min: int):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "new_facts": new_facts_count,
            "contradictions": contradictions_count,
            "elapsed_min": elapsed_min,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _save_last_run(self, processed_facts: int, partial: bool = False):
        self._last_run = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().isoformat(),
            "processed_facts": int(processed_facts),
            "partial": bool(partial),
        }
        self._save_json(self.last_run_file, self._last_run)

    @staticmethod
    def _extract_concepts(text: str) -> list[str]:
        words = re.findall(r"[a-zA-Zа-яА-Я0-9_]{4,}", text.lower())
        stop = {"владимир", "сегодня", "завтра", "который", "потому", "что", "где", "когда"}
        uniq = []
        for w in words:
            if w in stop:
                continue
            if w not in uniq:
                uniq.append(w)
        return uniq[:6]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-zA-Zа-яА-Я0-9_]{3,}", text.lower())

    @staticmethod
    def _extract_json(text: str):
        text = text.strip()
        # сначала пробуем как есть
        try:
            return json.loads(text)
        except Exception:
            pass
        # потом вырезаем JSON блок
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except Exception:
            return None

    def _load_json(self, path: Path, default: Any):
        if not path.exists():
            return default
        if self.identity:
            try:
                from identity.encryption import decrypt_file
                return decrypt_file(self.identity, path, default=default)
            except Exception:
                return default
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _save_json(self, path: Path, data: Any):
        if self.identity:
            from identity.encryption import encrypt_file
            encrypt_file(self.identity, path, data)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def _backup_before_overwrite(self, path: Path):
        if not path.exists():
            return
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            suffix = datetime.now().strftime("%Y-%m-%d")
            backup_name = f"{path.stem}_{suffix}{path.suffix}"
            backup_path = self.history_dir / backup_name
            shutil.copy2(path, backup_path)
            self._prune_history(days=14)
        except Exception:
            # Бэкап не должен ломать основной сценарий записи.
            pass

    def _prune_history(self, days: int = 14):
        cutoff = datetime.now().date() - timedelta(days=max(1, int(days)))
        allowed = {"weighted_facts", "concepts", "contradictions", "cognitive_core"}
        for f in self.history_dir.glob("*.json"):
            m = re.match(r"^(.+)_([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$", f.name)
            if not m:
                continue
            stem, date_str = m.group(1), m.group(2)
            if stem not in allowed:
                continue
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                continue
            if d < cutoff:
                try:
                    f.unlink()
                except Exception:
                    pass
