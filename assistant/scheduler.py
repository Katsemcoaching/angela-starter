"""Плановые чекины и (опционально) напоминания.

Ядро: утренний и вечерний чекин по будням/ежедневно.
Опции (включаются в .env): недельный обзор (вс) и опрос напоминаний.

Защита от дублей: факт отправки чекина пишется в Supabase, поэтому
рестарт на Railway не присылает чекин повторно.
"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from assistant import config, db, prompts
from assistant.agent import ask

logger = logging.getLogger(__name__)

# Функцию отправки в Telegram задаёт __main__ после старта бота.
_send = None


def set_sender(fn) -> None:
    global _send
    _send = fn


# Плановый чекин зовёт на шеринг. Если Катя уже прислала его сама — звать
# второй раз незачем. Она часто пишет раньше расписания: в 6 утра или в 9
# вечера, а в 7:00 и 22:00 получала тот же вопрос заново.
_REFLECTION_OF = {"утренний": "утро", "вечерний": "вечер"}


async def _do_checkin(checkin_prompt: str, label: str) -> None:
    """Провести один плановый чекин (если ещё не присылали сегодня)."""
    if not _send:
        return
    today = datetime.now(config.TIMEZONE).strftime("%Y-%m-%d")
    if db.was_checkin_sent(label, today):
        logger.info("чекин '%s' уже был сегодня — пропускаю", label)
        return

    # Шеринг за сегодня уже есть — молчим. Помечаем чекин отправленным,
    # иначе досылка после рестарта принесёт его через полчаса.
    # Недельный обзор сюда не попадает: он не про шеринг.
    reflection = _REFLECTION_OF.get(label)
    if reflection:
        try:
            if db.has_reflection_today(reflection, today):
                db.mark_checkin_sent(label, today)
                logger.info("шеринг '%s' за %s уже прислан — чекин не шлю", reflection, today)
                return
        except Exception:
            # База не ответила — это не повод молчать. Лишний вопрос
            # безобиднее пропущенного чекина.
            logger.exception("не смогла проверить шеринг за сегодня — шлю чекин как обычно")
    # Историю подтягиваем так же, как в обычном разговоре. Без неё чекин
    # шёл с чистого листа: Катя утром называла фокус дня, а бот в следующем
    # плановом сообщении спрашивал о нём заново, будто не слышал.
    history = db.get_recent_memory(limit=config.HISTORY_LIMIT)

    for attempt in range(3):
        try:
            text = await ask(
                f"Сегодня {today}. Проведи {label} чекин.",
                history=history,
                is_checkin=True,
                extra_system=checkin_prompt,
            )
            await _send(text)
            db.mark_checkin_sent(label, today)
            logger.info("чекин '%s' отправлен", label)
            return
        except Exception:
            logger.exception("не удалось отправить чекин '%s' (попытка %d/3)", label, attempt + 1)
            if attempt < 2:
                await asyncio.sleep(20)


async def _poll_reminders() -> None:
    """Проверить наступившие напоминания и отправить их (модуль reminders)."""
    if not _send:
        return
    try:
        from assistant.tools.reminders import get_due_reminders, mark_reminder_done
        for r in get_due_reminders():
            await _send(f"⏰ напоминание: {r['text']}")
            mark_reminder_done(r["id"])
    except Exception:
        logger.exception("ошибка опроса напоминаний")


async def run_catchup() -> None:
    """На старте досылает чекин, если время уже прошло, а его сегодня не было."""
    if not _send:
        return
    now = datetime.now(config.TIMEZONE)

    # Утро и вечер стоят в расписании на пн-пт. Досылка про это не знала:
    # любой рестарт в выходной присылал «доброе утро» просто потому, что
    # сегодня чекина ещё не было. Держим то же правило, что и в расписании.
    if now.weekday() >= 5:  # 5 = суббота, 6 = воскресенье
        logger.info("выходной — плановые чекины не досылаем")
        return

    schedule = [
        (config.MORNING_HOUR, prompts.MORNING_CHECKIN, "утренний"),
        (config.EVENING_HOUR, prompts.EVENING_CHECKIN, "вечерний"),
    ]
    for hour, prompt, label in schedule:
        # досылаем, только если уже прошло время, но не больше чем на 3 часа
        if hour <= now.hour < hour + 3:
            await _do_checkin(prompt, label)


def create_scheduler() -> AsyncIOScheduler:
    """Собрать планировщик: ядро всегда, опции — по флагам в .env."""
    sched = AsyncIOScheduler(timezone=config.TIMEZONE)

    # ── Ядро: утро и вечер по будням ──
    sched.add_job(
        _do_checkin,
        CronTrigger(hour=config.MORNING_HOUR, minute=0, day_of_week="mon-fri",
                    timezone=config.TIMEZONE),
        args=[prompts.MORNING_CHECKIN, "утренний"],
        id="morning", replace_existing=True,
    )
    sched.add_job(
        _do_checkin,
        CronTrigger(hour=config.EVENING_HOUR, minute=0, day_of_week="mon-fri",
                    timezone=config.TIMEZONE),
        args=[prompts.EVENING_CHECKIN, "вечерний"],
        id="evening", replace_existing=True,
    )

    # ── Опция: недельный обзор (воскресенье) ──
    if config.ENABLE_WEEKLY_REVIEW:
        sched.add_job(
            _do_checkin,
            CronTrigger(hour=config.WEEKLY_REVIEW_HOUR, minute=0, day_of_week="sun",
                        timezone=config.TIMEZONE),
            args=[prompts.WEEKLY_REVIEW, "недельный"],
            id="weekly", replace_existing=True,
        )

    # ── Опция: опрос напоминаний раз в минуту ──
    if config.ENABLE_REMINDERS:
        sched.add_job(_poll_reminders, IntervalTrigger(minutes=1),
                      id="reminders", replace_existing=True)

    return sched
