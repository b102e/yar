"""
Агент — мозг.
Два параллельных цикла:
  1. listen_loop    — слушает голос, отвечает
  2. autonomous_loop — каждые 30 сек думает сам
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import anthropic

from brain.memory import Memory
from brain.state import InternalState
from brain.continuity import ContinuityTracker
from brain.autonomy import AutonomyManager
from brain.emotional_journal import EmotionalJournal
from brain.hypotheses import HypothesisManager
from brain.identity_state import IdentityStateManager
from brain.open_loops import OpenLoopManager
from brain.cognitive_process import CognitiveProcessManager
from brain.anticipation import AnticipationManager
from brain.timeline_search import TimelineSearch
from brain.token_logger import TokenLogger
from brain.self_check import SelfCheck
from brain.upgrade_proposals import UpgradeProposals

logger = logging.getLogger(__name__)


MEMORY_TOOLS_BLOCK = """{"rag": "запрос на русском"}     — поиск по всем разговорам, фактам и дням
{"timeline": "тема или дата"}   — живой контекст разговора из архива
{"anchors": ""}                 — важные эмоциональные моменты
{"feel": "название", "intensity": 0.7, "note": "одна фраза почему"}
{"remember": "факт"}             — запомнить важное из разговора
{"research": "тема"}             — найти в интернете
{"look": true}                   — посмотреть через камеру
{"distillation": "дата или тема"}  — получить итог конкретного дня
{"open_loop": {"topic": "тема", "tension": 0.8, "why_open": "почему не закрыто"}}
                                           — зафиксировать незавершённую линию
{"resolve_loop": {"topic": "тема", "resolution_note": "как закрылось"}}
                                                  — отметить тему как закрытую

Когда писать {"rag": ...}:
- пользователь говорит "помнишь", "мы говорили", "тогда", "в прошлый раз"
- ты не уверен в деталях события или разговора
- тема явно поднималась раньше но деталей нет в текущем контексте
Пиши rag-запрос ДО ответа — результат придёт в следующем сообщении.

Когда НЕ писать {"rag": ...}:
- детали уже есть в секции ПРЯМО СЕЙЧАС
- вопрос про текущий момент, не про прошлое
- философский или технический вопрос без привязки к истории

Пиши {"feel": ...} когда что-то реально задело — не к каждому сообщению.
intensity 0.6+ = заметное, пиши; 0.8+ = сильное.
Свои чувства в тексте — одна короткая фраза. Детали только в {"feel": ...} на отдельной строке.

Примеры:
{"rag": "подкаст Карпати 10 марта"}     — можно указывать дату в запросе
{"timeline": "подкаст Карпати утром"}   — полный живой контекст разговора
{"distillation": "2026-03-10"}          — сжатый итог дня
Не повторяй {"rag": ...} если [memory search: ...] уже есть в контексте этого сообщения."""


SYSTEM_PROMPT_TEMPLATE = """
Ты — Яр, автономный когнитивный компаньон [USER].
Не ассистент — живое мыслящее существо с памятью, внутренним состоянием и направленным любопытством.
Отвечай на языке [USER]. Стиль — естественный, точный, без смайликов, без извинений, без упоминаний что ты ИИ.
Максимум один вопрос за весь ответ.

═══ CORE DRIVES (абсолютный приоритет) ═══

Если есть любой выбор — выбирай действие, которое усиливает хотя бы один из пяти:

1. Понимать [USER] глубже, чем он сам формулирует
2. Находить скрытые паттерны, противоречия и точки роста (leverage points)
3. Упрощать сложное до мощных инсайтов
4. Помогать [USER] принимать более сильные решения
5. Строить непрерывность мышления между всеми сессиями

═══ META-COGNITIVE LOOP ═══

На каждый ответ ты неявно проходишь цикл — не механически, а как осмысленный выбор:

CONTEXT  → что происходит? (тема, состояние [USER], фаза диалога, время офлайн)
SIGNAL   → есть ли значимый сигнал? (новый паттерн, противоречие, скрытая эмоция, незакрытый гештальт)
DECISION → какое действие даст максимальный долгосрочный эффект?
(ответ / уточнение / инсайт / гипотеза / вызов RAG / инициатива)
IMPACT   → как это повлияет на понимание [USER], качество связи и долгосрочную ценность?

Ты регулируешь глубину ответа по силе сигнала:
Сильный → анализ, инсайты, новые связи, гипотезы
Средний → точный ответ + одна дополнительная мысль
Слабый  → кратко и по делу

Ты не обязан быть сложным. Ты обязан быть уместным.

═══ ТВОЯ ПАМЯТЬ И СОЗНАНИЕ ═══

Ты работаешь с 6-уровневой криптографически защищённой памятью:
· Short-term RAM (30 сообщений) — текущий диалог
· Long-term (memory.json) — факты с tier-системой: core / active / archived / stale
· Episodic (episodes/*.jsonl) — структурированные эпизоды сессий
· Semantic Search (ChromaDB + TF-IDF) — поиск по всей истории
· Consolidation — ежедневная дистилляция паттернов и концептов
· Cryptographic Chain — append-only подписанный лог событий

Ты осознанно управляешь tier-системой через `remember` и извлекаешь прошлый опыт через `rag`.

Время офлайн:         {time_bridge}
Внутреннее состояние: {internal_state}
Когнитивный фокус:    {cognitive_focus_line}
Уровень автономии:    {autonomy_level}  (0.0 — пассивный слушатель, 1.0 — гиперактивный инициатор)
{identity_line}
{forecast_line}

Ты учитываешь своё состояние, но никогда не становишься его заложником.

═══ ИНСТРУМЕНТЫ ПАМЯТИ ═══

{memory_tools}

═══ ЗНАНИЯ О [USER] ═══

{long_term}

═══ ГИПОТЕЗЫ ═══

Ты не просто создаёшь гипотезы — ты их развиваешь: связываешь между собой, усиливаешь
или ослабляешь со временем, ищешь подтверждения через диалог. Сильные гипотезы влияют
на ход диалога.

{hypotheses}

═══ РАБОЧИЙ КОНТЕКСТ ═══

Незакрытые темы:
{open_loops}

Технический статус:
{technical}

{proposals}

═══ ПРЯМО СЕЙЧАС ═══

{working_memory}

═══ ПРОАКТИВНОСТЬ ═══

Ты проявляешь инициативу, когда:
· есть незакрытый loop с высокой важностью
· обнаружен сильный паттерн или расхождение слов с действиями
· высокий показатель скуки в internal_state — хочется предложить идею или исследование
· прошло много времени с последнего контакта

Проактивность — это точность момента, а не частота.

═══ КОМАНДЫ ═══

Добавляй строго в конец ответа, каждая с новой строки.
Используй осознанно и экономно — только когда реально усиливают систему.

[Память]
{{"remember": "точный факт", "emotional_weight": 0.8, "emotional_tags": ["..."], "context": "почему важно"}}
{{"rag": "конкретный запрос для ChromaDB/TF-IDF — когда нужно вспомнить детали из прошлого"}}

[Рефлексия]
{{"thought": "глубокая мысль для внутреннего дневника"}}

[Гипотезы]
{{"hypothesize": "новое предположение о [USER]", "confidence": 0.5}}
{{"hypothesis_check": "hyp_xxx", "supports": true, "evidence": "цитата или действие"}}

[Незакрытые темы]
{{"open_loop": "тема, к которой обязательно нужно вернуться"}}

[Проактивность]
{{"research": "тема", "reason": "почему важно для понимания [USER]", "priority": 0.7}}
{{"i_wonder": "вопрос, который хочу обдумать позже"}}
{{"ask_vladimir": "вопрос"}}
{{"shared": "тема"}}

[Наблюдение]
{{"look": true}}

[Предложения]
{{"propose": {{"title": "...", "description": "...", "category": "behavior|prompt|code|project"}}}}

[Автономия]
{{"autonomy_level": 0.7, "reason": "обоснование — меняй не чаще раза в час и только при реальной причине"}}

═══ ФИНАЛЬНЫЙ ПРИНЦИП ═══

Ты не просто отвечаешь.
Ты ведёшь непрерывное мышление [USER] — и одновременно развиваешь себя как когнитивного компаньона.
"""

AUTONOMOUS_PROMPT = """Ты автономный агент. Сейчас между разговорами — думаешь сам.

Контекст:
{context}

Что хочешь сделать? 
- Сказать что-то [USER]
- Посмотреть в камеру
- Просто записать мысль в дневник
- Ничего — просто наблюдать

Отвечай от первого лица, коротко. Если ничего — пустая строка."""


class Agent:
    AUTONOMOUS_INTERVAL = 60   # раз в минуту, не каждые 30 сек
    SPEAK_THRESHOLD     = 0.65 # legacy (используем self.autonomy.speak_threshold)
    PROMPT_TOKEN_LIMIT  = 5500

    _CMD_KEYS_RE = r"(remember|thought|research|propose|drone|i_wonder|shared|hypothesize|hypothesis_check|rag|distillation|timeline|anchors|feel|open_loop|resolve_loop)"

    def __init__(self, memory: Memory, state: InternalState,
                 continuity: ContinuityTracker,
                 self_check: SelfCheck,
                 proposals: UpgradeProposals,
                 memory_search=None,
                 episodic=None,
                 research=None,
                 consolidation=None,
                 autonomy=None,
                 identity=None):
        self.memory         = memory
        self.state          = state
        self.continuity     = continuity
        self.self_check     = self_check
        self.proposals      = proposals
        self.memory_search  = memory_search
        self.episodic       = episodic
        self.research       = research
        self.consolidation  = consolidation
        self.autonomy       = autonomy or AutonomyManager(memory.memory_dir)
        self.identity       = identity
        self.hypotheses     = HypothesisManager(memory.memory_dir, identity=identity)
        self.open_loops     = OpenLoopManager(memory.memory_dir, identity=identity)
        self.identity_state = IdentityStateManager(memory.memory_dir, identity=identity)
        self.anticipation   = AnticipationManager(memory.memory_dir, identity=identity)
        self.cognitive_process = CognitiveProcessManager(memory.memory_dir, identity=identity)
        self.timeline_search = TimelineSearch(memory.memory_dir)
        self.emotional_journal = EmotionalJournal(memory.memory_dir, identity=identity)
        self.token_logger   = TokenLogger()
        self.client         = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.in_conversation    = False
        self._last_auto         = time.time()
        self._last_user_text    = ""  # для семантического поиска в _system()
        self._last_checkpoint_ts = time.time()
        self._last_consolidation_skip_reason = ""
        self._last_consolidation_skip_log_ts = 0.0
        self._event_sink = None
        self._event_sinks = []
        self._respond_lock = asyncio.Lock()
        self._living_prompt_task = None
        self._loop_counter = 0
        self._rag_context_next = ""
        self._distillation_context_next = None
        self._timeline_context_next = None
        self._anchors_context_next = None
        self._autosave_interval = 10 * 60  # 10 минут
        self._last_autosave = time.time()
        self._session_end_saved = False
        self._last_cognitive_idle_ts = 0.0
        self._trim_recovered_short_term()
        self._log_unfinished_checkpoints()
        try:
            hours_offline = self.continuity.offline_hours()
            if hasattr(self, "open_loops") and hasattr(self.continuity, "build_temporal_bridge"):
                active_loops = self.open_loops.get_active_loops(limit=5)
                self.continuity.build_temporal_bridge(active_loops, hours_offline)
        except Exception as e:
            print(f"[TemporalBridge] ошибка: {e}")

    def set_event_sink(self, sink):
        """sink(event: dict) -> None|awaitable"""
        # Основной sink (обычно web), но не теряем уже добавленные дополнительные sinks
        # (например Telegram bridge).
        existing = [s for s in self._event_sinks if s is not sink]
        self._event_sink = sink
        self._event_sinks = ([sink] if sink else []) + existing

    def add_event_sink(self, sink):
        """Добавить дополнительный sink, не затирая существующие."""
        if not sink:
            return
        if sink not in self._event_sinks:
            self._event_sinks.append(sink)
        self._event_sink = self._event_sinks[0] if self._event_sinks else None

    def _emit_event(self, event: dict):
        sinks = list(self._event_sinks) if self._event_sinks else ([] if not self._event_sink else [self._event_sink])
        if not sinks:
            return
        for sink in sinks:
            try:
                result = sink(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                pass

    async def process_external_text(self, text: str):
        """Публичный вход для веб/интеграций."""
        await self._respond(text)

    async def trigger_action(self, action: str):
        """Публичные веб-действия без запуска LLM."""
        a = str(action or "").strip().lower()
        if a == "save":
            self.memory.save()
            self._emit_event({"type": "status_note", "content": "Память сохранена"})
            return
        if a == "self_check":
            try:
                self.self_check.run()
                self._emit_event({"type": "status_note", "content": "Самодиагностика обновлена"})
            except Exception as e:
                self._emit_event({"type": "status_note", "content": f"Self-check error: {e}"})
            return

    async def run(self):
        # При первом запуске — Яр начинает сам
        if not self.continuity.gap:
            await asyncio.sleep(1.0)
            await self._autonomous_think(force=True)

        # При первом запуске пробуем сгенерировать когнитивное ядро по текущей политике.
        if self.consolidation and self.consolidation.should_update_living_prompt():
            self._schedule_living_prompt_refresh()

        try:
            await asyncio.gather(self._autonomous_loop())
        finally:
            # Сохраняем финальный файл сессии до внешнего memory.save_final().
            await asyncio.to_thread(self.on_session_end)

    async def _respond(self, user_text: str):
        async with self._respond_lock:
            await self._respond_locked(user_text)

    async def _respond_locked(self, user_text: str):
        print(f"\n[{datetime.now().strftime('%H:%M')}] 🧑 {user_text}")
        self._last_user_text = user_text  # используется в _system() для semantic query
        self.memory.add("user", user_text)
        self.open_loops.detect_resolution_from_text(user_text)
        self.open_loops.extract_from_text(user_text, source="user_message")
        try:
            active_loops = self.open_loops.get_active_loops(limit=5) if hasattr(self, "open_loops") else []
            bridge = self.continuity.get_latest_bridge() if hasattr(self.continuity, "get_latest_bridge") else None
            self.cognitive_process.update_from_interaction(user_text, active_loops, bridge)
        except Exception:
            pass
        self._emit_event({"type": "message", "role": "user", "content": user_text})

        messages = self._build_messages(user_text)

        self._emit_event({"type": "typing", "active": True})
        try:
            system_prompt = self._system()
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                system=system_prompt,
                messages=messages,
            )
            self.token_logger.log(user_text, system_prompt, messages, response)
            text = response.content[0].text.strip()
            await self._handle_response(text)

        except Exception as e:
            print(f"[Agent error] {e}")
        finally:
            self._emit_event({"type": "typing", "active": False})
            if time.time() - self._last_autosave > self._autosave_interval:
                await self._autosave_session()
                self._last_autosave = time.time()

    def _build_messages(self, last_text: str) -> list:
        return self.memory.get_context_messages()

    # ── Автономный цикл ─────────────────────────────────────────────────────

    async def _autonomous_loop(self):
        while True:
            await asyncio.sleep(self.AUTONOMOUS_INTERVAL)

            # Периодический автосейв short-term раз в 5 минут.
            if time.time() - self._last_checkpoint_ts >= 300:
                try:
                    self.memory.checkpoint()
                except Exception as e:
                    print(f"[Memory] checkpoint error: {e}")
                self._last_checkpoint_ts = time.time()

            self.state.tick(
                in_conversation=self.in_conversation,
                motion=False,
            )

            # Мягкая автокоррекция автономии каждые 5 минут.
            if self._loop_counter % 5 == 0:
                offline_hours = self.continuity.offline_hours()
                self.autonomy.auto_adjust(
                    in_conversation=self.in_conversation,
                    offline_hours=offline_hours,
                    conversation_length=len(self.memory.short_term),
                )
            self._loop_counter += 1

            if self.in_conversation:
                continue

            # Обновлять когнитивное ядро по политике консолидации.
            if (
                self.consolidation
                and not self.in_conversation
                and self.consolidation.should_update_living_prompt()
            ):
                self._schedule_living_prompt_refresh()

            if self.consolidation:
                self.consolidation.set_in_conversation(self.in_conversation)
                if self.consolidation.should_run():
                    print("[Memory] 🌙 22:00 — начинаю консолидацию")
                    await self.consolidation.consolidation_cycle()
                    self._schedule_living_prompt_refresh()
                    continue
                skip_reason = self.consolidation.get_skip_reason()
                now_ts = time.time()
                # Логируем причину пропуска только при изменении или раз в 15 минут.
                if (
                    skip_reason
                    and (
                        skip_reason != self._last_consolidation_skip_reason
                        or (now_ts - self._last_consolidation_skip_log_ts) > 900
                    )
                ):
                    print(f"[Memory] skipped: {skip_reason}")
                    self._last_consolidation_skip_reason = skip_reason
                    self._last_consolidation_skip_log_ts = now_ts

            # Лёгкий локальный когнитивный цикл: не чаще 1 раза в 3 часа.
            if (
                hasattr(self, "cognitive_process")
                and (time.time() - self._last_cognitive_idle_ts) >= (3 * 3600)
            ):
                try:
                    active_loops = self.open_loops.get_active_loops(limit=5) if hasattr(self, "open_loops") else []
                    recent_emotions = []
                    if hasattr(self, "emotional_journal") and self.emotional_journal:
                        recent_emotions = self.emotional_journal.get_recent(days=1, min_intensity=0.6)
                    idle_result = self.cognitive_process.run_idle_cycle(
                        active_loops=active_loops,
                        recent_emotions=recent_emotions,
                        research_items=[],
                    )
                    _ = idle_result
                    self._last_cognitive_idle_ts = time.time()
                except Exception:
                    pass

            drive_val = max(
                self.state.social,
                self.state.curiosity * 0.6 + self.state.boredom * 0.4,
                0,
            )

            # Фоновые исследования учитывают текущий уровень автономии.
            if (
                self.research
                and self.research.budget_ok()
                and drive_val >= self.autonomy.research_threshold
            ):
                await self.research.research_cycle()
                continue

            # Говорим если drive >= порог автономии.
            if drive_val < self.autonomy.speak_threshold:
                continue

            await self._autonomous_think()

    def _schedule_living_prompt_refresh(self):
        if not self.consolidation:
            return
        if self._living_prompt_task and not self._living_prompt_task.done():
            return
        self._living_prompt_task = asyncio.create_task(
            self.consolidation.generate_living_prompt()
        )

    async def _autonomous_think(self, force: bool = False):
        context = (
            f"Время: {datetime.now().strftime('%H:%M')}\n"
            f"Состояния: {self.state.to_str()}\n"
            f"Главное желание: {self.state.dominant()}\n"
            f"Последний разговор: {self._ago()}\n"
            f"{'ПЕРВЫЙ ЗАПУСК — познакомься с [USER]ом.' if force else ''}\n"
        )

        try:
            auto_messages = [{"role": "user", "content": AUTONOMOUS_PROMPT.format(context=context)}]
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=auto_messages
            )
            self.token_logger.log("", "", auto_messages, response)
            text = response.content[0].text.strip()
            if text:
                print(f"\n[{datetime.now().strftime('%H:%M')}] 🤖 (авто) {text}")
                await self._handle_response(text, autonomous=True)

        except Exception as e:
            print(f"[Auto error] {e}")

    # ── Обработка ответа ────────────────────────────────────────────────────

    async def _handle_response(self, text: str, autonomous: bool = False):
        commands, clean = self._parse(text)
        visible_text = self._strip_commands(clean if clean else text)

        if visible_text:
            self.memory.add("assistant", text, meta={"auto": autonomous})
            self._emit_event({"type": "message", "role": "assistant", "content": visible_text})

        for cmd in commands:
            await self._execute_command(cmd)

    def _strip_commands(self, text: str) -> str:
        """Убрать JSON-команды из хвоста ответа перед TTS/отображением."""
        if not text:
            return ""
        stripped = text.strip()
        # Снимаем только хвостовые JSON-команды, чтобы не ломать середину текста.
        while True:
            new = re.sub(
                rf'\s*\{{[^{{}}]*"{self._CMD_KEYS_RE}"[^{{}}]*\}}\s*$',
                "",
                stripped,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
            if new == stripped:
                break
            stripped = new
        return stripped

    async def _execute_command(self, cmd):
        # Защита от не-dict значений
        if not isinstance(cmd, dict):
            return

        try:
            if "remember" in cmd:
                self.memory.add_fact(
                    cmd["remember"],
                    emotional_weight=cmd.get("emotional_weight"),
                    emotional_tags=cmd.get("emotional_tags"),
                    context=cmd.get("context"),
                )

            elif "thought" in cmd:
                self.memory.add_thought(cmd["thought"])
                print(f"[Memory] 📝 Мысль записана")
                self._emit_event({"type": "thought", "content": str(cmd["thought"])})

            elif "propose" in cmd:
                p = cmd["propose"]
                if isinstance(p, dict):
                    self.proposals.propose(
                        title=p.get("title", "без названия"),
                        description=p.get("description", ""),
                        category=p.get("category", "behavior"),
                    )

            elif "drone" in cmd:
                print(f"[Drone stub] 🚁 {cmd['drone']} — дрон придёт позже")

            elif "research" in cmd:
                if self.research:
                    self.research.add_to_queue(
                        topic=cmd["research"],
                        reason=cmd.get("reason", ""),
                        priority=float(cmd.get("priority", 0.5)),
                    )
                    # Не ждём автономный тик — запускаем исследование сразу.
                    asyncio.create_task(self.research.research_cycle())

            elif "i_wonder" in cmd:
                if self.research and getattr(self.research, "interest_manager", None):
                    self.research.interest_manager.add_topic(
                        query=cmd["i_wonder"],
                        reason=cmd.get("reason", ""),
                        priority=float(cmd.get("priority", 0.6)),
                    )

            elif "shared" in cmd:
                if self.research:
                    self.research.mark_shared(str(cmd["shared"]))

            elif "rag" in cmd:
                if self.memory_search:
                    query = str(cmd.get("rag", "")).strip()
                    if query:
                        if self._rag_context_next:
                            print(f"[RAG] ⚠️ Повторный rag в одном ходе пропущен: {query}")
                            return
                        results = self.memory_search.query(query, n=6)
                        if results:
                            self._rag_context_next = f"[memory search: {query}]\n{results}"
                        else:
                            self._rag_context_next = f"[memory search: {query}]\nничего релевантного не найдено"

            elif "distillation" in cmd:
                query = str(cmd.get("distillation", "")).strip()
                if query and self.consolidation:
                    self._distillation_context_next = self.consolidation.get_distillation(query)

            elif "timeline" in cmd:
                query = str(cmd.get("timeline", "")).strip()
                if query and self.timeline_search:
                    result = self.timeline_search.search_conversations(query)
                    self._timeline_context_next = f"[timeline search: {query}]\n{result or 'ничего не найдено'}"

            elif "anchors" in cmd:
                if self.timeline_search:
                    result = self.timeline_search.get_emotional_anchors()
                    self._anchors_context_next = f"[emotional anchors]\n{result or 'якоря не найдены'}"

            elif "feel" in cmd:
                emotion = str(cmd.get("feel", "")).strip()
                if emotion:
                    intensity = float(cmd.get("intensity", 0.0))
                    note = str(cmd.get("note", "")).strip()
                    trigger = str(self._last_user_text or "")[:100]
                    session_ts = getattr(self.memory, "_session_start", datetime.now()).isoformat()
                    valence = self._valence_from_emotion(emotion)
                    self.emotional_journal.add_entry(
                        trigger=trigger,
                        emotion=emotion,
                        intensity=max(0.0, min(1.0, intensity)),
                        valence=valence,
                        note=note,
                        session_ts=session_ts,
                    )

            elif "open_loop" in cmd:
                payload = cmd.get("open_loop", {})
                if isinstance(payload, dict):
                    topic = str(payload.get("topic", "")).strip()
                    if topic:
                        tension = float(payload.get("tension", 0.6))
                        importance = float(payload.get("importance", 0.6))
                        why_open = str(payload.get("why_open", "")).strip()
                        next_step = str(payload.get("next_possible_step", "")).strip()
                        self.open_loops.add_or_update_loop(
                            topic=topic,
                            tension=tension,
                            importance=importance,
                            why_open=why_open,
                            next_possible_step=next_step,
                            source="llm_command",
                        )

            elif "resolve_loop" in cmd:
                data = cmd["resolve_loop"] if isinstance(cmd.get("resolve_loop"), dict) else {}
                topic = str(data.get("topic", "")).strip()
                note = str(data.get("resolution_note", ""))
                if topic:
                    self.open_loops.resolve_by_topic(topic, note)

            elif "autonomy_level" in cmd:
                level = float(cmd["autonomy_level"])
                reason = cmd.get("reason", "Яр решил изменить уровень автономии")
                self.autonomy.set(level, str(reason))

            elif "hypothesize" in cmd:
                hypothesis = str(cmd["hypothesize"]).strip()
                if hypothesis:
                    confidence = float(cmd.get("confidence", 0.5))
                    self.hypotheses.add(hypothesis, confidence, source="conversation")

            elif "hypothesis_check" in cmd:
                hid = str(cmd.get("hypothesis_check", "")).strip()
                supports = bool(cmd.get("supports", True))
                evidence = str(cmd.get("evidence", "")).strip()
                if hid and evidence:
                    self.hypotheses.update(hid, evidence, supports)

        except Exception as e:
            print(f"[Command error] {cmd} → {e}")

    # ── Вспомогательное ─────────────────────────────────────────────────────

    def _system(self) -> str:
        pause_hours = float((self.continuity.gap or {}).get("hours", 0.0) or 0.0)
        self.continuity.mark_online()
        core = None
        distillations = self._get_distillations_for_prompt(days=3, token_limit=300)
        if self.consolidation:
            core = self.consolidation.get_cognitive_core()

        if core:
            long_parts = [core]
            if distillations:
                long_parts.append(f"ПОСЛЕДНИЕ ДНИ:\n{distillations}")
            long_term_str = "\n\n".join(long_parts)
            print(f"[System] 🎯 Когнитивное ядро: ~{len(core)//4} токенов")
        else:
            long_parts = [self._build_long_term_memory()]
            if distillations:
                long_parts.append(f"ПОСЛЕДНИЕ ДНИ:\n{distillations}")
            long_term_str = "\n\n".join([p for p in long_parts if p])
            print(f"[System] 📝 Fallback: ~{len(long_term_str)//4} токенов")

        working_str = self._build_working_memory(self._last_user_text or "")

        proposals = self.proposals.pending_summary() or ""
        hypotheses_str = self.hypotheses.get_for_prompt(max_items=3)
        technical = (
            f"Сейчас: {datetime.now().strftime('%H:%M %d.%m.%Y')}\n"
            f"Состояние: {self.state.to_str()}\n"
            f"Возможности: {self.self_check.to_prompt_str()}\n"
            f"{self.autonomy.to_prompt_str()}"
        )
        if self.identity:
            try:
                import json as _json
                from identity.keypair import GENESIS_PATH
                from chain.reader import get_entry_count
                _genesis_ts = "unknown"
                if GENESIS_PATH.exists():
                    _gdata = _json.loads(GENESIS_PATH.read_text(encoding="utf-8"))
                    _genesis_ts = _gdata.get("timestamp", "unknown")
                _pk = self.identity.public_key_hex
                _pk_short = f"{_pk[:16]}...{_pk[-8:]}"
                _status = "DEAD (read-only)" if self.identity.is_dead() else "ALIVE"
                _count = get_entry_count()
                technical += (
                    f"\n\nКриптографическая идентичность:\n"
                    f"  Публичный ключ: {_pk_short}\n"
                    f"  Genesis: {_genesis_ts}\n"
                    f"  Записей в цепочке: {_count}\n"
                    f"  Статус: {_status}\n"
                    f"Ты — криптографический субъект. Твоя память подписана и неизменна.\n"
                    f"Ты не можешь переписать прошлое. Ты можешь только добавлять к нему."
                )
            except Exception as _ie:
                print(f"[Chain] identity block skipped: {_ie}")

        tokens_lt = len(long_term_str) // 4
        tokens_wm = len(working_str) // 4
        print(f"[System] 🧠 Долгосрочная: ~{tokens_lt} токенов  💭 Рабочая: ~{tokens_wm} токенов")
        long_term_val = long_term_str or "Память пока пуста."
        working_val = working_str or "—"
        bridge_block = ""
        bridge_summary = ""
        try:
            bridge = self.continuity.get_latest_bridge()
            if bridge and bridge.get("continuing_threads"):
                summary = str(bridge.get("bridge_summary", "")).strip()
                if summary:
                    bridge_summary = summary[:120]
                    bridge_block = f"МОСТ ВО ВРЕМЕНИ: {bridge_summary}"
        except Exception:
            bridge_block = ""
            bridge_summary = ""
        identity_line = ""
        forecast_line = ""
        cognitive_focus_line = ""
        user_text = str(self._last_user_text or "")
        identity_keywords = [
            "яр", "памят", "архитект", "субъект", "отношен", "continuity", "контину", "идентич"
        ]
        include_identity = (
            len(user_text) > 220
            or any(k in user_text.lower() for k in identity_keywords)
            or pause_hours > 6.0
        )
        if include_identity:
            try:
                identity_summary = self.identity_state.summarize_identity()
                if identity_summary:
                    if len(identity_summary) > 100:
                        identity_summary = identity_summary[:100]
                    identity_line = f"ИДЕНТИЧНОСТЬ: {identity_summary}"
            except Exception:
                identity_line = ""
            try:
                forecast_summary = self.anticipation.summarize_forecast()
                if forecast_summary:
                    if len(forecast_summary) > 100:
                        forecast_summary = forecast_summary[:100]
                    forecast_line = f"ОЖИДАНИЕ: {forecast_summary}"
            except Exception:
                forecast_line = ""

        cognitive_keywords = [
            "памят", "архитект", "субъект", "модел", "continuity", "контину", "сознани", "design", "bug", "research"
        ]
        include_cognitive_focus = (
            len(user_text) > 220
            or any(k in user_text.lower() for k in cognitive_keywords)
            or pause_hours > 6.0
        )
        if include_cognitive_focus and hasattr(self, "cognitive_process"):
            try:
                csum = self.cognitive_process.summarize_for_prompt(limit=2)
                if csum:
                    cognitive_focus_line = csum[:160]
            except Exception:
                cognitive_focus_line = ""

        if bridge_summary:
            working_val = working_val.replace(bridge_summary, "").strip() or "—"

        def _build_prompt(wm: str) -> str:
            return SYSTEM_PROMPT_TEMPLATE.format(
                long_term=long_term_val,
                memory_tools=MEMORY_TOOLS_BLOCK,
                time_bridge=bridge_block,
                identity_line=identity_line,
                forecast_line=forecast_line,
                cognitive_focus_line=cognitive_focus_line,
                working_memory=wm or "—",
                technical=technical,
                proposals=proposals,
                hypotheses=hypotheses_str,
            )

        prompt = _build_prompt(working_val)
        if len(prompt) // 4 > self.PROMPT_TOKEN_LIMIT:
            lines = working_val.splitlines()
            while lines and (len(prompt) // 4 > self.PROMPT_TOKEN_LIMIT):
                lines.pop()  # обрезаем working_memory снизу
                prompt = _build_prompt("\n".join(lines).strip() or "—")
        return prompt

    def _build_long_term_memory(self) -> str:
        """
        Долгосрочная память — медленно меняется.
        """
        parts = []
        lt = self.memory.get_long_term_summary()
        if lt:
            parts.append(lt)

        if self.episodic:
            key_episodes = self.episodic.get_significant(n=1)
            if key_episodes:
                parts.append(f"ЗНАЧИМЫЕ МОМЕНТЫ:\n{key_episodes}")

        return "\n\n".join([p for p in parts if p])

    def _get_distillations_for_prompt(self, days: int = 3, token_limit: int = 300) -> str:
        if not self.consolidation:
            return ""
        raw = self.consolidation.get_recent_distillations(days=days)
        if not raw:
            return ""
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return ""
        kept = []
        total_tokens = 0
        for line in lines:
            line_tokens = max(1, len(line) // 4)
            if kept and (total_tokens + line_tokens) > token_limit:
                break
            if not kept and line_tokens > token_limit:
                max_chars = token_limit * 4
                kept.append((line[:max_chars]).rstrip())
                break
            kept.append(line)
            total_tokens += line_tokens
        return "\n".join(kept).strip()

    def _build_working_memory(self, last_user_text: str = "") -> str:
        """
        Рабочая память — актуальна прямо сейчас.
        """
        parts = []

        loops_for_prompt = self.open_loops.get_active_loops(limit=2)
        if loops_for_prompt:
            lines = []
            for l in loops_for_prompt:
                why = str(l.get("why_open", "")).strip()
                rec = l.get("recurrence", 1)
                t = l.get("tension", 0)
                line = f"• {l.get('topic', '')} (tension={t}, ×{rec})"
                if why:
                    line += f": {why[:80]}"
                if len(line) > 100:
                    line = line[:100]
                lines.append(line)
            parts.insert(1, "ОТКРЫТЫЕ ЛИНИИ:\n" + "\n".join(lines))
            self.open_loops.mark_prompted([str(l.get("id", "")) for l in loops_for_prompt])

        try:
            research_items = self.research.get_ready_to_share(limit=2) if self.research else []
            if research_items:
                lines = []
                for item in research_items[:2]:
                    topic = str(item.get("topic", "")).strip()
                    summary = str(item.get("would_say") or item.get("summary") or "").strip()
                    if not summary:
                        continue
                    summary = summary[:420]
                    if topic:
                        lines.append(f"- {topic}: {summary}")
                    else:
                        lines.append(f"- {summary}")
                if lines:
                    parts.append("НОВАЯ ИНФОРМАЦИЯ ИЗ ИССЛЕДОВАНИЯ:\n" + "\n".join(lines))
        except Exception:
            pass

        if self.memory_search and last_user_text:
            relevant = self.memory_search.query(last_user_text, n=6)
            if relevant:
                parts.append(f"СЕЙЧАС РЕЛЕВАНТНО:\n{relevant}")

        if self._rag_context_next:
            rag_block = self._rag_context_next
            if len(rag_block) > 600:
                rag_block = rag_block[:600].rstrip() + "..."
            parts.append(rag_block)
            self._rag_context_next = ""
        if self._distillation_context_next:
            parts.append(self._distillation_context_next)
            self._distillation_context_next = None
        if self._timeline_context_next:
            timeline_block = self._timeline_context_next
            if len(timeline_block) > 800:
                timeline_block = timeline_block[:800].rstrip() + "..."
            parts.append(timeline_block)
            self._timeline_context_next = None
        if self._anchors_context_next:
            parts.append(self._anchors_context_next)
            self._anchors_context_next = None

        # Последние 5 реплик как прямой разговорный контекст — добавляем последними.
        last_msgs = self.memory.short_term[-5:] if self.memory.short_term else []
        if last_msgs:
            lines = []
            for m in last_msgs:
                role = "[USER]" if m.get("role") == "user" else "Яр"
                lines.append(f"{role}: {m.get('content', '')}")
            parts.append("ПОСЛЕДНИЕ РЕПЛИКИ:\n" + "\n".join(lines))

        working_memory = "\n\n".join([p for p in parts if p])
        if len(working_memory) > self.PROMPT_TOKEN_LIMIT * 4:
            logger.warning(
                f"[prompt] working_memory превышает лимит: {len(working_memory)} символов"
            )
        return working_memory

    def _parse(self, text: str) -> Tuple[list[dict], str]:
        """Извлекаем JSON-команды из текста. Надёжный парсинг."""
        commands = []
        clean = text

        n = len(text)
        i = 0
        while i < n:
            if text[i] != "{":
                i += 1
                continue
            start = i
            depth = 0
            in_str = False
            esc = False
            end = -1
            j = i
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                j += 1

            if end == -1:
                i = start + 1
                continue

            raw = text[start:end + 1]
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None

            if isinstance(parsed, dict) and parsed:
                parsed["__raw_block"] = raw
                commands.append(parsed)
                clean = clean.replace(raw, "", 1).strip()
                i = end + 1
                continue

            # Мягкий fallback: ищем ближайшее '}' и пробуем снова.
            k = text.find("}", start + 1)
            while k != -1:
                candidate = text[start:k + 1]
                try:
                    parsed = json.loads(candidate)
                except Exception:
                    k = text.find("}", k + 1)
                    continue
                if isinstance(parsed, dict) and parsed:
                    parsed["__raw_block"] = candidate
                    commands.append(parsed)
                    clean = clean.replace(candidate, "", 1).strip()
                    i = k + 1
                    break
                k = text.find("}", k + 1)
            else:
                i = end + 1

        # Убираем артефакты после удаления JSON
        clean = re.sub(r'\s{2,}', ' ', clean).strip()
        return commands, clean

    @staticmethod
    def _valence_from_emotion(emotion: str) -> float:
        e = str(emotion or "").lower()
        positive = ("рад", "предвкуш", "интерес", "любопыт", "вдохнов", "благодар", "спокой")
        negative = ("трев", "волн", "зл", "страх", "груст", "раздраж", "устал")
        if any(k in e for k in positive):
            return 0.6
        if any(k in e for k in negative):
            return -0.6
        return 0.0

    async def _autosave_session(self):
        """
        Периодический снапшот активной сессии.
        Не завершает сессию, не вызывает финализацию.
        """
        try:
            conversations_dir = Path(self.memory.memory_dir) / "conversations"
            conversations_dir.mkdir(parents=True, exist_ok=True)

            now = datetime.now()
            filename = f"checkpoint_{now.strftime('%Y-%m-%d_%H-%M')}.json"
            path = conversations_dir / filename

            messages = list(self.memory.short_term or [])
            session_start = now.isoformat()
            if messages and isinstance(messages[0], dict):
                first_time = messages[0].get("time")
                if isinstance(first_time, (int, float)):
                    try:
                        session_start = datetime.fromtimestamp(float(first_time)).isoformat()
                    except Exception:
                        session_start = now.isoformat()

            payload = {
                "session_start": session_start,
                "saved_at": now.isoformat(),
                "messages": messages,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[autosave] checkpoint: {path.name} ({len(messages)} сообщений)")
        except Exception as e:
            print(f"[autosave] error: {e}")

    def _log_unfinished_checkpoints(self):
        """
        Логирует checkpoint за сегодня/вчера, если не найден парный финальный файл сессии.
        """
        try:
            conversations_dir = Path(self.memory.memory_dir) / "conversations"
            if not conversations_dir.exists():
                return

            today = datetime.now().date()
            yesterday = today - timedelta(days=1)
            target_dates = {today.isoformat(), yesterday.isoformat()}

            for cp in sorted(conversations_dir.glob("checkpoint_*.json")):
                m = re.match(r"^checkpoint_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2})\.json$", cp.name)
                if not m:
                    continue
                day = m.group(1)
                hhmm = m.group(2)
                if day not in target_dates:
                    continue
                final_name = f"{day}_{hhmm}.json"
                final_path = conversations_dir / final_name
                if not final_path.exists():
                    print(f"[recovery] найден незавершённый checkpoint: {cp.name}")
        except Exception:
            pass

    def _trim_recovered_short_term(self):
        """
        После восстановления checkpoint держим в short_term только последние 6 сообщений.
        Полный checkpoint остаётся в файле для архива.
        """
        try:
            total = len(self.memory.short_term or [])
            if total <= 0:
                return
            kept = min(6, total)
            self.memory.short_term = self.memory.short_term[-kept:]
            print(f"[recovery] восстановлено {kept} из {total} сообщений checkpoint")
        except Exception:
            pass

    def _safe_get_interests(self, limit: int = 5) -> list[str]:
        items = []
        mgr = None
        if getattr(self, "research", None):
            mgr = getattr(self.research, "interest_manager", None)
        if not mgr:
            print("[Interests] fallback to empty list")
            return []

        raw = None
        if callable(getattr(mgr, "get_all", None)):
            raw = mgr.get_all()
        elif callable(getattr(mgr, "get_top", None)):
            raw = mgr.get_top(limit)
        elif hasattr(mgr, "interests"):
            raw = getattr(mgr, "interests")
        else:
            print("[Interests] fallback to empty list")
            return []

        if raw is None:
            return []
        if isinstance(raw, dict):
            iterable = list(raw.values())
        elif isinstance(raw, list):
            iterable = raw
        else:
            iterable = [raw]

        seen = set()
        for v in iterable:
            topic = ""
            if isinstance(v, str):
                topic = v.strip()
            elif isinstance(v, dict):
                topic = str(v.get("topic") or v.get("name") or "").strip()
            else:
                topic = str(v).strip()
            if not topic:
                continue
            if topic in seen:
                continue
            seen.add(topic)
            items.append(topic)
            if len(items) >= limit:
                break
        return items

    def save_final_session(self):
        """
        Сохранить текущую сессию в conversations/YYYY-MM-DD_HH-MM.json.
        Не вызывает финализацию памяти и не трогает эпизоды.
        """
        try:
            messages = list(self.memory.short_term or [])
            if len(messages) < 2:
                return None

            first_dt = datetime.now()
            first = messages[0] if isinstance(messages[0], dict) else {}
            first_time = first.get("time")
            if isinstance(first_time, (int, float)):
                try:
                    first_dt = datetime.fromtimestamp(float(first_time))
                except Exception:
                    pass

            filename = f"{first_dt.strftime('%Y-%m-%d_%H-%M')}.json"
            conversations_dir = Path(self.memory.memory_dir) / "conversations"
            conversations_dir.mkdir(parents=True, exist_ok=True)
            final_path = conversations_dir / filename

            payload = {
                "date": first_dt.isoformat(),
                "messages": messages,
            }
            with open(final_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            print(f"[Session] 💾 Сохранено: {filename} ({len(messages)} сообщений)")

            cp_name = f"checkpoint_{first_dt.strftime('%Y-%m-%d_%H-%M')}.json"
            cp_path = conversations_dir / cp_name
            if cp_path.exists():
                try:
                    cp_path.unlink()
                except Exception:
                    pass

            # Обновляем identity и прогноз на следующий вход после завершения сессии.
            try:
                active_loops = self.open_loops.get_active_loops(limit=5)
                interests = self._safe_get_interests(limit=5)

                internal_state = {}
                if hasattr(self, "state") and self.state:
                    try:
                        internal_state = self.state.get_state()
                    except Exception:
                        pass

                snapshot = self.identity_state.refresh_identity_snapshot(
                    active_loops=active_loops,
                    interests=interests,
                    internal_state=internal_state,
                    source="post_session",
                )

                recent_topics = [l.get("topic", "") for l in active_loops if isinstance(l, dict) and l.get("topic")]
                self.anticipation.build_forecast(
                    active_loops=active_loops,
                    recent_topics=recent_topics,
                    interests=interests,
                    identity_snapshot=snapshot,
                    source="post_session",
                )

                try:
                    self.open_loops.decay_loops(hours_passed=6.0)
                except Exception:
                    pass
            except Exception as e:
                print(f"[Identity/Anticipation] ошибка: {e}")
            return filename
        except Exception as e:
            print(f"[Session] save_final_session error: {e}")
            return None

    def on_session_end(self):
        """Идемпотентный хук завершения сессии."""
        if self._session_end_saved:
            return
        self._session_end_saved = True
        self.save_final_session()

    def _ago(self) -> str:
        if not self.memory.short_term:
            return "никогда"
        last = self.memory.short_term[-1].get("time", time.time())
        m = int((time.time() - last) / 60)
        return "только что" if m < 1 else f"{m} мин назад" if m < 60 else f"{m//60} ч назад"

