"""Знания о бизнесе Кати — папка knowledge/ рядом с кодом.

Бот живёт на Railway и папку AI_SPACE на Макбуке Кати не видит: между
ними нет никакой связи. Поэтому нужные файлы лежат копией в репозитории,
а агент переносит их туда и отправляет вместе с кодом.

Читаются один раз при импорте — и попадают в СТАТИЧНЫЙ блок системного
промпта (`agent._system_blocks`), тот, что кэшируется. Значит на каждом
сообщении за них платится десятая часть цены, а не полная.

Что сюда класть, а что нет — решено 11 августа 2026, разбор в
`angelina/состояние.md`, раздел 3.1. Коротко: факты о бизнесе, клиентках,
целях и принципах — да. Правила оформления постов и палитры — нет, это
для будущего контент-ассистента.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

_HEADER = """\
ниже — что тебе нужно знать про Катю и её бизнес. это факты, на которые
ты опираешься, когда говоришь про её работу, цели, клиенток и контент.

это не инструкция по стилю: пиши по-прежнему коротко и живо, не
пересказывай эти разделы вслух и не сыпь ими без повода. они нужны,
чтобы ты понимала, ради чего вообще её день.
"""


def load() -> str:
    """Собрать все knowledge/*.md в один кусок текста для промпта."""
    if not KNOWLEDGE_DIR.is_dir():
        logger.warning("папки knowledge/ нет — Анджелина без знаний о бизнесе")
        return ""

    parts = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.exception("не прочитался %s", path.name)
            continue
        if text:
            parts.append(text)

    if not parts:
        logger.warning("в knowledge/ нет ни одного непустого файла")
        return ""

    body = "\n\n---\n\n".join(parts)
    logger.info("знания загружены: %d файлов, %d символов", len(parts), len(body))
    return _HEADER + "\n\n" + body


KNOWLEDGE = load()
