"""Мозг ассистента — Claude API с циклом вызова инструментов (tool use).

Что здесь происходит за один ответ:
  1. собираем системный промпт (личность + включённые модули + текущее время)
  2. собираем список инструментов из tools/registry.py (только включённые)
  3. зовём Claude; если он просит инструмент — выполняем и зовём снова
  4. возвращаем финальный текст

Кэш промптов и повторные попытки при перегрузе API оставлены «как у взрослых» —
это экономит деньги и переживает пики нагрузки. Менять не нужно.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta

import anthropic

from assistant import prompts
from assistant.knowledge import KNOWLEDGE
from assistant.config import (
    ANTHROPIC_API_KEY,
    MAX_TOKENS,
    MAX_TOOL_ROUNDS,
    MODEL_CHAT,
    MODEL_CHECKIN,
    TIMEZONE,
)
from assistant.tools.registry import build_runtime

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0, max_retries=4)

# HTTP-статусы, которые имеет смысл повторить (временные сбои сервера/лимиты).
# 529 = Anthropic API overloaded (перегружен).
_RETRYABLE = {408, 409, 429, 500, 502, 503, 529}
_OVERLOADED_MSG = (
    "claude сейчас перегружен — так бывает в пики. попробуй ещё раз через минуту 🙏"
)
# Показывается, только если и вторая попытка вернула пустой ответ.
_EMPTY_MSG = "(что-то подвисло, попробуй ещё раз)"
_MAX_TOOL_RESULT_CHARS = 6000  # обрезаем огромные ответы инструментов, чтобы не переплачивать


def _messages_create(**kwargs):
    """client.messages.create со вторым слоем ретраев на затяжной перегруз."""
    last_exc = None
    for attempt in range(3):
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            if exc.status_code not in _RETRYABLE:
                raise
            last_exc = exc
            logger.warning("Claude API %s (попытка %d/3)", exc.status_code, attempt + 1)
        except anthropic.APIConnectionError as exc:
            last_exc = exc
            logger.warning("Claude API недоступен (попытка %d/3)", attempt + 1)
        if attempt < 2:
            time.sleep(4 * (attempt + 1))  # 4с, потом 8с
    raise last_exc


_DAYS_RU = ("понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье")


def _week_ahead(now: datetime) -> str:
    """Готовые даты на неделю вперёд — чтобы модель не считала дни в уме.

    Она в этом ошибается: 8 августа на вопрос «что в понедельник» ответила
    «понедельник 11 августа», хотя понедельник был 10-го. Дату сегодняшнего
    дня она получала верно — промахивалась именно на пересчёте вперёд.
    """
    days = []
    for i in range(1, 8):
        d = now + timedelta(days=i)
        days.append(f"  {_DAYS_RU[d.weekday()]} = {d.strftime('%Y-%m-%d')}")
    return "\n".join(days)


def _system_blocks(module_addons: str, extra_system: str) -> list[dict]:
    """Системный промпт двумя блоками: статичный (кэшируется) + изменчивый (время)."""
    static = prompts.PERSONA
    if KNOWLEDGE:  # факты о бизнесе из knowledge/ — тоже статичны, кэшируются
        static += "\n\n" + KNOWLEDGE
    if module_addons:
        static += "\n\n" + module_addons

    now = datetime.now(TIMEZONE)
    volatile = (
        f"сейчас: {now.strftime('%Y-%m-%d %H:%M')}, {_DAYS_RU[now.weekday()]}\n"
        f"СЕГОДНЯ={now.strftime('%Y-%m-%d')} ({_DAYS_RU[now.weekday()]})\n"
        f"ЗАВТРА={(now + timedelta(days=1)).strftime('%Y-%m-%d')}\n"
        f"следующие 7 дней (сегодняшний день сюда НЕ входит) —\n"
        f"когда называешь будущий день недели, бери дату отсюда, не вычисляй:\n"
        f"{_week_ahead(now)}"
    )
    if extra_system:
        volatile += "\n\n" + extra_system

    return [
        # Статичный блок кэшируется — на каждом раунде инструментов он берётся
        # из кэша, а не пересчитывается. Время идёт ПОСЛЕ точки кэширования.
        {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile},
    ]


def _cached_tools(tools: list[dict]) -> list[dict]:
    """Поставить точку кэширования на последний инструмент (кэшируется весь список)."""
    if not tools:
        return tools
    cached = list(tools)
    last = dict(cached[-1])
    last["cache_control"] = {"type": "ephemeral"}
    cached[-1] = last
    return cached


def _run_tool(handlers: dict, name: str, data: dict) -> str:
    """Выполнить инструмент и вернуть результат строкой JSON."""
    handler = handlers.get(name)
    if handler is None:
        return json.dumps({"error": f"неизвестный инструмент: {name}"}, ensure_ascii=False)
    try:
        result = handler(data)
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:  # инструмент не должен ронять весь ответ
        logger.exception("инструмент %s упал", name)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        text = text[:_MAX_TOOL_RESULT_CHARS] + "…[обрезано]"
    return text


async def ask(
    user_message: str,
    history: list[dict] | None = None,
    is_checkin: bool = False,
    extra_system: str = "",
) -> str:
    """Ответ ассистента. Пустой ответ — переспрашиваем один раз, молча.

    15 августа Катя дважды получила «(что-то подвисло)» на голосовые: Claude
    вернул ответ вообще без текста. Оба раза — вокруг большого вечернего
    шеринга, где идёт много вызовов инструментов. В логи при этом не писалось
    ничего, поэтому причина осталась догадкой. Теперь пишется, а Катя вместо
    заглушки получает вторую попытку и обычно ничего не замечает.
    """
    for attempt in (1, 2):
        text = await _ask_once(user_message, history, is_checkin, extra_system)
        if text:
            return text
        logger.warning(
            "Claude вернул пустой ответ (попытка %d из 2)%s",
            attempt, " — переспрашиваю" if attempt == 1 else " — сдаюсь",
        )
    return _EMPTY_MSG


async def _ask_once(
    user_message: str,
    history: list[dict] | None = None,
    is_checkin: bool = False,
    extra_system: str = "",
) -> str:
    """Один заход к Claude с выполнением инструментов. Пусто — вернёт ""."""
    tools, handlers, addons = build_runtime()
    system = _system_blocks(addons, extra_system)
    cached_tools = _cached_tools(tools)
    model = MODEL_CHECKIN if is_checkin else MODEL_CHAT

    messages = list(history or [])
    messages.append({"role": "user", "content": user_message})

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = _messages_create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=cached_tools,
                messages=messages,
            )
            # Claude закончил (или ответ обрезан по лимиту токенов) — отдаём текст.
            if response.stop_reason in ("end_turn", "max_tokens"):
                return _extract_text(response)

            # Иначе он просит инструменты — выполняем и продолжаем цикл.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _run_tool(handlers, block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                return _extract_text(response)

        # Раунды кончились, а Claude всё ещё зовёт инструменты — просим финальный текст.
        final = _messages_create(
            model=model, max_tokens=MAX_TOKENS, system=system, messages=messages
        )
        return _extract_text(final)

    except anthropic.APIConnectionError:
        return _OVERLOADED_MSG
    except anthropic.APIStatusError as exc:
        if exc.status_code in _RETRYABLE:
            return _OVERLOADED_MSG
        raise


# Пометка времени, которую мы сами ставим перед сообщениями из памяти
# (см. db._stamp). Модель начала копировать её в свои ответы: 11 августа
# написала «[11 авг, 11:26] Сегодня по бизнесу…» — причём время взяла
# старое, из чужой строки истории. Просьбы в промпте не хватило, и это
# закреплялось само: ответ с пометкой сохраняется в память и подкрепляет
# привычку. Поэтому срезаем железно — до отправки Кате и до сохранения.
# Ловим не точный вид пометки, а её признак: скобка в начале строки, внутри
# которой стоит сокращённый русский месяц. Первая версия требовала ровно
# «[12 авг, 18:49]» — и 12 августа мимо неё проехали «[12 авг, ~18:49]» и
# «[12 авг, 20:15+]». Модель подмешивает в пометку своё, поэтому перечислять
# варианты бесполезно.
# (?:...)+ — чтобы снимались и две пометки подряд, за один проход.
_MONTHS_RE = "янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек"
_STAMP_RE = re.compile(
    rf"^[ \t]*(?:\[[^\]\n]{{0,30}}(?:{_MONTHS_RE})[^\]\n]{{0,30}}\][ \t]*)+",
    re.MULTILINE,
)


def _strip_stamps(text: str) -> str:
    """Убрать системные пометки времени, если модель их скопировала."""
    return _STAMP_RE.sub("", text).strip()


def _extract_text(response) -> str:
    """Текст ответа. Пусто — возвращаем "", решение принимает ask()."""
    parts = [block.text for block in response.content if hasattr(block, "text")]
    if not parts:
        # Ровно этот случай Катя видела 15 августа: в ответе одни вызовы
        # инструментов и ни одного текстового блока.
        logger.warning(
            "в ответе нет текста: stop_reason=%s, блоки=%s",
            getattr(response, "stop_reason", "?"),
            [getattr(b, "type", "?") for b in response.content],
        )
        return ""
    text = _strip_stamps("\n".join(parts))
    if not text:
        logger.warning("текст ответа исчез после срезания пометок: %r", parts)
    return text
