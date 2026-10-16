"""Один «телесный» ход: цикл tool use, пока Claude действует телом.

YAR по-прежнему получает в конце обычный текст с JSON-командами памяти —
он разбирается штатным _handle_response. Телесные действия идут через
нативный tool use, потому что телу нужна обратная связь: проехал → посмотрел →
поправился.
"""
import asyncio

from embodiment import tools as body_tools
from embodiment.link import BodyLink, BodyUnavailable

MAX_ROUNDS = 8


async def run_embodied_turn(client, model: str, max_tokens: int, system: str,
                            messages: list[dict], link: BodyLink,
                            attach_frame: bool = False, on_text=None, token_logger=None):
    """
    messages — контекст из памяти YAR (строковый content).
    attach_frame — приложить свежий кадр к последней реплике (если человек говорит с телом).
    on_text(str) — вызывается с промежуточным текстом каждого раунда (для озвучки).
    Возвращает (весь текст ответа, текст последнего раунда — ещё не озвучен, действия тела).
    """
    msgs = [dict(m) for m in messages]
    if attach_frame and msgs and msgs[-1]["role"] == "user":
        try:
            res = await link.call("take_photo", {}, timeout=8)
            if res.get("image_b64"):
                msgs[-1] = {"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": res["image_b64"]}},
                    {"type": "text", "text": str(msgs[-1]["content"])}]}
        except BodyUnavailable:
            pass

    tools = body_tools.available_tools(link)   # фиксируем на весь ход, даже если связь пропадёт
    texts, actions, last_text = [], [], ""

    for _ in range(MAX_ROUNDS):
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=msgs)
        if tools:
            kwargs["tools"] = tools
        # синхронный SDK — в отдельном потоке, чтобы не держать event loop (и heartbeat тела)
        resp = await asyncio.to_thread(client.messages.create, **kwargs)
        if token_logger:
            try:
                token_logger.log("", system, msgs, resp)
            except Exception:
                pass

        round_text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        if round_text:
            texts.append(round_text)
        last_text = round_text
        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            break
        if round_text and on_text:
            await _maybe_await(on_text(round_text))   # «Смотрю…» звучит сразу, не в конце
        last_text = ""

        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for c in calls:
            content, note = await body_tools.execute(link, c.name, c.input or {})
            actions.append(note)
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": content})
        msgs.append({"role": "user", "content": results})
    else:
        await link.fire("stop")
        actions.append("лимит шагов за ход исчерпан — тело остановлено")

    return "\n".join(texts).strip(), last_text, actions


async def _maybe_await(x):
    if asyncio.iscoroutine(x):
        await x
