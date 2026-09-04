"""Реестр инструментов — собирает то, что ВКЛЮЧЕНО, в один набор.

Каждый модуль инструментов (core, cycle, gmail, ...) экспортирует:
  TOOLS        — список схем инструментов для Claude
  HANDLERS     — словарь {имя_инструмента: функция(data) -> результат}
  PROMPT_ADDON — (опц.) кусок текста, который добавится в системный промпт

build_runtime() склеивает включённые модули. Хочешь добавить свой
инструмент — напиши такой же модуль и добавь сюда одну ветку if.
См. docs/07-build-your-own-tool.md
"""

from assistant import config
from assistant.tools import core


def build_runtime() -> tuple[list[dict], dict, str]:
    """Вернуть (tools, handlers, prompt_addon) с учётом включённых модулей."""
    tools: list[dict] = list(core.TOOLS)
    handlers: dict = dict(core.HANDLERS)
    addons: list[str] = []

    if config.ENABLE_CYCLE:
        from assistant.tools import cycle
        tools += cycle.TOOLS
        handlers.update(cycle.HANDLERS)
        addons.append(cycle.PROMPT_ADDON)

    if config.ENABLE_REMINDERS:
        from assistant.tools import reminders
        tools += reminders.TOOLS
        handlers.update(reminders.HANDLERS)
        addons.append(reminders.PROMPT_ADDON)

    if config.ENABLE_GMAIL:
        from assistant.tools import gmail
        tools += gmail.TOOLS
        handlers.update(gmail.HANDLERS)
        addons.append(gmail.PROMPT_ADDON)

    if config.ENABLE_GCAL:
        from assistant.tools import gcal
        tools += gcal.TOOLS
        handlers.update(gcal.HANDLERS)
        addons.append(gcal.PROMPT_ADDON)

    if config.ENABLE_OURA:
        from assistant.tools import oura
        tools += oura.TOOLS
        handlers.update(oura.HANDLERS)
        addons.append(oura.PROMPT_ADDON)

    return tools, handlers, "\n\n".join(a for a in addons if a)
