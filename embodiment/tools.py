"""Телесные инструменты для Claude и их исполнение через BodyLink.

Набор инструментов зависит от состояния тела: нет связи — нет инструментов,
критический заряд или аварийный стоп — нет движения. Это не замена лимитам
на самом роботе (они там жёсткие), а способ не предлагать модели невозможное.
"""
import asyncio

from embodiment.link import BodyLink, BodyUnavailable

_T = lambda name, desc, props=None, req=None: {  # noqa: E731
    "name": name, "description": desc,
    "input_schema": {"type": "object", "properties": props or {}, "required": req or []}}

MOTION_TOOLS = [
    _T("body_drive", "Проехать прямо. + вперёд, − назад. Тело само урежет до своих лимитов.",
       {"distance_cm": {"type": "number", "description": "от -50 до 50"},
        "speed": {"type": "number", "description": "0.1–0.6, по умолчанию 0.4"}}, ["distance_cm"]),
    _T("body_turn", "Развернуться на месте. + влево, − вправо.",
       {"degrees": {"type": "number", "description": "от -180 до 180"}}, ["degrees"]),
]

SENSE_TOOLS = [
    _T("body_look", "Повернуть голову с камерой. pan: + влево, − вправо; tilt: + вверх, − вниз.",
       {"pan": {"type": "number", "description": "от -80 до 80"},
        "tilt": {"type": "number", "description": "от -30 до 40"}}, ["pan", "tilt"]),
    _T("body_photo", "Посмотреть камерой прямо сейчас."),
    _T("body_emotion", "Выражение глаз на дисплеях.",
       {"emotion": {"type": "string", "enum": ["neutral", "happy", "curious", "surprised", "sad",
                                                "angry", "sleepy", "thinking"]}}, ["emotion"]),
    _T("body_nod", "Кивнуть (да)."),
    _T("body_shake_head", "Покачать головой (нет)."),
    _T("body_stop", "Немедленно остановить моторы."),
]

_CMD = {"body_drive": "drive", "body_turn": "turn", "body_look": "look", "body_photo": "take_photo",
        "body_emotion": "set_emotion", "body_nod": "nod", "body_shake_head": "shake_head",
        "body_stop": "stop"}

BODY_RULES = """
Как пользоваться телом (инструменты body_*):
- Двигайся маленькими шагами (10–30 см) и после движения смотри снимком, что вышло.
- Сзади и под собой ты не видишь — назад только понемногу.
- Если человек говорит остановиться — сразу body_stop и не двигайся, пока не попросят.
- Лимиты скорости, расстояний и заряда стоят на самом теле; если команду урезали или отклонили — прими это.
- Свои JSON-команды памяти (remember, feel и т.д.) пиши как обычно, в конце ответа.
- Ответ человеку, который говорит с телом голосом, озвучивается — держи его коротким."""


def available_tools(link: BodyLink | None) -> list[dict]:
    if link is None or not link.connected:
        return []
    return (MOTION_TOOLS if link.can_move() else []) + SENSE_TOOLS


async def execute(link: BodyLink, name: str, args: dict) -> tuple[list | str, str]:
    """Возвращает (content для tool_result, короткая запись для памяти)."""
    cmd = _CMD.get(name)
    if not cmd:
        return f"Неизвестный инструмент {name}.", f"{name}: неизвестно"
    if cmd in ("drive", "turn") and not link.can_move():
        why = "аварийный стоп" if link.state.estop else f"заряд {link.battery_level()}"
        return f"Отказ: движение заблокировано ({why}).", f"{cmd}: отказ ({why})"
    try:
        res = await link.call(cmd, args, timeout=10 if cmd == "take_photo" else None)
    except BodyUnavailable as e:
        return f"Тело недоступно: {e}", f"{cmd}: тело недоступно"
    text = str(res.get("text") or ("Ок." if res.get("ok") else "Ошибка."))
    if res.get("image_b64"):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": res["image_b64"]}},
                   {"type": "text", "text": text}]
        return content, f"{cmd}: снимок"
    return text, f"{cmd}({_short(args)}) → {text}"


def _short(args: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in (args or {}).items())


# соответствие эмоций из журнала YAR (команда feel) выражению глаз
_FEEL_MAP = [
    (("рад", "счаст", "весел", "joy", "happy", "благодар", "тепл"), "happy"),
    (("любопыт", "интерес", "curio", "удивл", "wonder"), "curious"),
    (("груст", "печал", "тоск", "sad", "одинок"), "sad"),
    (("зл", "раздраж", "anger", "angry", "обид"), "angry"),
    (("устал", "сонн", "tired", "sleep"), "sleepy"),
    (("шок", "surpris", "изумл"), "surprised"),
    (("задум", "сомнен", "think", "неувер"), "thinking"),
]


def emotion_for_feel(feel: str) -> str:
    f = (feel or "").lower()
    for keys, emo in _FEEL_MAP:
        if any(k in f for k in keys):
            return emo
    return "neutral"
