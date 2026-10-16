"""
BodyLink — связь субъекта (YAR на VPS) с телом (робот дома).

Почему тело подключается само, а не наоборот: робот сидит за домашним NAT,
VPS до него не достучится. Поэтому робот держит исходящее WebSocket-соединение
к YAR и переподключается при обрыве. YAR не знает адрес робота и не должен.

Протокол (JSON-сообщения):

  тело → YAR
    {"type": "hello",  "token": "...", "body_id": "rasptank-01", "fw": "0.2"}
    {"type": "status", "battery": {...}, "estop": bool, "busy": bool, "pose": {...}}
    {"type": "heard",  "text": "..."}                 — человек сказал фразу телу
    {"type": "event",  "name": "...", "data": {...}}  — estop, battery_level, shutdown...
    {"type": "result", "id": "...", "ok": bool, "text": "...", "image_b64": "..."}

  YAR → тело
    {"type": "welcome", "subject": "<public key>", "dead": bool}
    {"type": "cmd", "id": "...", "cmd": "drive", "args": {...}}

Тело живёт и без YAR: при обрыве оно само останавливается и уходит в офлайн-режим.
YAR при обрыве перестаёт предлагать Claude телесные инструменты и знает, с какого
момента тела нет.
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field


class BodyUnavailable(Exception):
    pass


@dataclass
class BodyState:
    connected: bool = False
    body_id: str = ""
    connected_at: float = 0.0
    disconnected_at: float = 0.0
    last_seen: float = 0.0
    battery: dict = field(default_factory=dict)   # voltage, percent, level, charging
    estop: bool = False
    busy: bool = False
    pose: dict = field(default_factory=dict)


class BodyLink:
    HEARTBEAT_TIMEOUT = 45.0     # нет статуса столько секунд — считаем тело потерянным
                                 # (с запасом: другие части YAR иногда держат event loop)
    DEFAULT_CMD_TIMEOUT = 15.0   # одна команда (движение ограничено таймаутом на самом теле)

    def __init__(self, token: str, host: str = "0.0.0.0", port: int = 8765,
                 subject_pubkey: str = "", is_dead=lambda: False):
        if not token or len(token) < 16:
            raise ValueError("BODY_TOKEN должен быть не короче 16 символов")
        self.token = token
        self.host, self.port = host, port
        self.subject_pubkey = subject_pubkey
        self.is_dead = is_dead
        self.state = BodyState()
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._on_heard = None        # async fn(text)
        self._on_change = None       # fn(kind: str, data: dict)
        self.recent_events: list[tuple[float, str]] = []   # для промпта: что недавно случилось с телом

    # ── подписки ────────────────────────────────────────────────────────────
    def on_heard(self, fn):
        self._on_heard = fn

    def on_change(self, fn):
        self._on_change = fn

    # ── свойства для агента ─────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self.state.connected

    def battery_level(self) -> str:
        return str(self.state.battery.get("level", "unknown"))

    def can_move(self) -> bool:
        return (self.connected and not self.state.estop
                and self.battery_level() not in ("critical", "shutdown"))

    # ── сервер ──────────────────────────────────────────────────────────────
    async def serve(self):
        import websockets
        print(f"[Body] 🦾 жду тело на ws://{self.host}:{self.port}")
        async with websockets.serve(self._handler, self.host, self.port,
                                    ping_interval=20, ping_timeout=40,
                                    max_size=8 * 1024 * 1024):
            await asyncio.gather(asyncio.Future(), self._watchdog())

    async def _handler(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except Exception:
            await ws.close(code=4000, reason="no hello")
            return
        if hello.get("type") != "hello" or not _consteq(str(hello.get("token", "")), self.token):
            print("[Body] ✗ отклонено подключение: неверный токен")
            await ws.close(code=4001, reason="unauthorized")
            return
        if self._ws is not None:
            # одно тело на одного субъекта; новое соединение вытесняет старое (переподключение)
            try:
                await self._ws.close(code=4002, reason="replaced")
            except Exception:
                pass

        self._ws = ws
        s = self.state
        s.connected, s.body_id = True, str(hello.get("body_id", "body"))
        s.connected_at = s.last_seen = time.time()
        offline_for = (s.connected_at - s.disconnected_at) if s.disconnected_at else 0.0
        await ws.send(json.dumps({"type": "welcome", "subject": self.subject_pubkey,
                                  "dead": bool(self.is_dead())}))
        self._note("connected", {"body_id": s.body_id, "offline_sec": round(offline_for)})

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                s.last_seen = time.time()
                await self._dispatch(msg)
        except Exception:
            pass
        finally:
            if self._ws is ws:
                self._drop("connection closed")

    async def _dispatch(self, msg: dict):
        t = msg.get("type")
        s = self.state
        if t == "status":
            old_level = self.battery_level()
            old_charging = bool(s.battery.get("charging"))
            s.battery = msg.get("battery") or {}
            s.estop = bool(msg.get("estop"))
            s.busy = bool(msg.get("busy"))
            s.pose = msg.get("pose") or {}
            if self.battery_level() != old_level and old_level != "unknown":
                self._note("battery_level", {"from": old_level, "to": self.battery_level(),
                                             "percent": s.battery.get("percent")})
            if bool(s.battery.get("charging")) != old_charging:
                self._note("charging" if s.battery.get("charging") else "charging_stopped",
                           {"percent": s.battery.get("percent")})
        elif t == "result":
            fut = self._pending.pop(str(msg.get("id")), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif t == "heard":
            text = str(msg.get("text", "")).strip()
            if text and self._on_heard:
                asyncio.create_task(self._on_heard(text))
        elif t == "event":
            name = str(msg.get("name", ""))
            data = msg.get("data") or {}
            if name == "estop":
                s.estop = bool(data.get("active", True))
            self._note(name, data)

    async def _watchdog(self):
        while True:
            await asyncio.sleep(2)
            s = self.state
            if s.connected and time.time() - s.last_seen > self.HEARTBEAT_TIMEOUT:
                ws = self._ws
                self._drop("heartbeat timeout")
                if ws is not None:
                    try:
                        await ws.close(code=4003, reason="heartbeat timeout")
                    except Exception:
                        pass

    def _drop(self, reason: str):
        s = self.state
        if not s.connected:
            return
        s.connected = False
        s.disconnected_at = time.time()
        self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BodyUnavailable(f"связь с телом потеряна ({reason})"))
        self._pending.clear()
        self._note("disconnected", {"reason": reason})

    def _note(self, kind: str, data: dict):
        self.recent_events.append((time.time(), _human_event(kind, data)))
        self.recent_events = self.recent_events[-8:]
        print(f"[Body] {kind} {data}")
        if self._on_change:
            try:
                self._on_change(kind, data)
            except Exception as e:
                print(f"[Body] on_change error: {e}")

    # ── команды телу ────────────────────────────────────────────────────────
    async def call(self, cmd: str, args: dict | None = None, timeout: float | None = None) -> dict:
        """Отправить команду и дождаться результата. BodyUnavailable — если тела нет."""
        if not self.connected or self._ws is None:
            raise BodyUnavailable("тело не подключено")
        cid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await self._ws.send(json.dumps({"type": "cmd", "id": cid, "cmd": cmd, "args": args or {}}))
            return await asyncio.wait_for(fut, timeout or self.DEFAULT_CMD_TIMEOUT)
        except asyncio.TimeoutError:
            # тело живо, но не ответило — просим остановиться на всякий случай
            asyncio.create_task(self.fire("stop"))
            raise BodyUnavailable(f"тело не ответило на {cmd} за {timeout or self.DEFAULT_CMD_TIMEOUT:.0f} с")
        except BodyUnavailable:
            raise
        except Exception as e:
            raise BodyUnavailable(f"ошибка связи: {e}")
        finally:
            self._pending.pop(cid, None)

    async def fire(self, cmd: str, args: dict | None = None):
        """Команда без ожидания ответа (глаза, стоп). Молча ничего не делает без тела."""
        if not self.connected or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"type": "cmd", "id": uuid.uuid4().hex[:12],
                                            "cmd": cmd, "args": args or {}, "noreply": True}))
        except Exception:
            pass

    # ── текст для системного промпта ────────────────────────────────────────
    def prompt_block(self) -> str:
        s = self.state
        lines = ["", "ТЕЛО:"]
        if not s.connected:
            if s.disconnected_at:
                ago = _ago(time.time() - s.disconnected_at)
                lines.append(f"Связи с телом нет уже {ago}. Ты сейчас без тела: не видишь, не слышишь "
                             f"комнату и не можешь двигаться. Не обещай физических действий.")
            else:
                lines.append("Тело ещё ни разу не подключалось с момента запуска. Сейчас ты без тела.")
        else:
            b = s.battery
            pct = b.get("percent")
            lvl = self.battery_level()
            bat = f"{pct:.0f}%" if isinstance(pct, (int, float)) else "неизвестно"
            charging = ", заряжается" if b.get("charging") else ""
            lines.append(f"Тело ({s.body_id}) подключено. Батарея: {bat} ({_level_ru(lvl)}{charging}).")
            if lvl == "low":
                lines.append("Заряд низкий: двигайся только по необходимости и короткими отрезками; "
                             "можешь сказать человеку, что пора на зарядку.")
            elif lvl in ("critical", "shutdown"):
                lines.append("Заряд критический: моторы заблокированы телом. Ты можешь только смотреть, "
                             "менять выражение глаз и говорить. Попроси поставить тебя на зарядку.")
            if s.estop:
                lines.append("Аварийный стоп активен: движение заблокировано, снять его может только человек.")
        if self.recent_events:
            lines.append("Недавно с телом: " + "; ".join(
                f"{time.strftime('%H:%M', time.localtime(t))} {txt}" for t, txt in self.recent_events[-4:]))
        return "\n".join(lines)


# ── утилиты ─────────────────────────────────────────────────────────────────

def _consteq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


def _ago(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return f"{sec} с"
    if sec < 5400:
        return f"{sec // 60} мин"
    if sec < 172800:
        return f"{sec // 3600} ч"
    return f"{sec // 86400} дн"


def _level_ru(level: str) -> str:
    return {"ok": "норма", "low": "низкий", "critical": "критический",
            "shutdown": "отключение"}.get(level, level)


def _human_event(kind: str, d: dict) -> str:
    if kind == "connected":
        off = d.get("offline_sec") or 0
        return "тело подключилось" + (f" после {_ago(off)} без связи" if off > 60 else "")
    if kind == "disconnected":
        return "связь с телом пропала"
    if kind == "battery_level":
        return f"заряд {d.get('from')} → {d.get('to')} ({d.get('percent', '?')}%)"
    if kind == "charging":
        return "поставили на зарядку"
    if kind == "charging_stopped":
        return "сняли с зарядки"
    if kind == "estop":
        return "аварийный стоп " + ("включён" if d.get("active", True) else "снят")
    if kind == "shutdown":
        return "тело выключается: батарея на нуле"
    return kind
