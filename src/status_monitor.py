"""Фоновый мониторинг статусов отправленных жалоб.

Каждые N минут бот проходит по всем жалобам в БД с непустым forum_thread_url
и непринятым статусом, обращается к форуму, обновляет статус и шлёт
уведомление автору жалобы в Telegram при изменении.
"""
import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from src.database import (
    list_complaints_for_status_check,
    update_complaint_status,
    mark_complaint_notified,
)
from src.forum.xenforo import fetch_complaint_status
from src.effects import EFFECT_CONFETTI, EFFECT_FIRE

logger = logging.getLogger(__name__)

# Интервал между проверками (секунды). 5 минут — достаточно для админ-форума.
CHECK_INTERVAL_SECONDS = 5 * 60

# Сколько жалоб проверяем за раз (rate-limit форума)
BATCH_SIZE = 10
# Пауза между запросами в одной партии (секунды), чтобы не упереться в DDoS-Guard
DELAY_BETWEEN_REQUESTS = 1.5


# Человекочитаемые подписи для статусов
STATUS_LABELS = {
    "pending":  "⏳ Ожидание",
    "accepted": "✅ Принята",
    "rejected": "❌ Отклонена",
    "closed":   "🔒 Закрыта",
    "unknown":  "❔ Неизвестно",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, f"❔ {status}")


async def _notify_user(bot: Bot, complaint: dict, new_status: str) -> bool:
    """Шлёт уведомление автору жалобы. Возвращает True если доставлено."""
    label = status_label(new_status)
    nickname = escape(complaint.get("nickname", "?"))
    url = complaint.get("forum_thread_url") or ""
    link_part = (
        f"\n🔗 <a href=\"{escape(url)}\">Открыть тему</a>" if url else ""
    )

    if new_status == "accepted":
        text = (
            f"🎉 <b>Ваша жалоба на «{nickname}» принята!</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}"
        )
        effect = EFFECT_CONFETTI
    elif new_status == "rejected":
        text = (
            f"😔 <b>Жалоба на «{nickname}» отклонена.</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}\n\n"
            "<i>Возможные причины: недостаточно доказательств, "
            "истёк срок подачи, нарушены правила оформления.</i>"
        )
        effect = None
    elif new_status == "closed":
        text = (
            f"🔒 <b>Тема жалобы на «{nickname}» закрыта.</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}"
        )
        effect = None
    else:
        text = (
            f"ℹ️ <b>Изменился статус жалобы на «{nickname}».</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}"
        )
        effect = None

    try:
        kwargs = {"disable_web_page_preview": False}
        if effect:
            kwargs["message_effect_id"] = effect
        await bot.send_message(complaint["telegram_id"], text, **kwargs)
        return True
    except TelegramForbiddenError:
        logger.info("Не могу уведомить telegram_id=%s: пользователь заблокировал бота.",
                    complaint["telegram_id"])
        return False
    except TelegramBadRequest as e:
        logger.warning("Telegram отклонил уведомление для %s: %s",
                       complaint["telegram_id"], e)
        return False
    except Exception:
        logger.exception("Неожиданная ошибка при уведомлении %s",
                         complaint["telegram_id"])
        return False


async def _check_once(bot: Bot) -> None:
    """Один проход по всем жалобам, требующим проверки."""
    complaints = await list_complaints_for_status_check()
    if not complaints:
        logger.debug("Мониторинг: нет жалоб для проверки.")
        return

    logger.info("Мониторинг: проверяю %d жалоб.", len(complaints))
    changed = 0
    notified = 0

    for i, comp in enumerate(complaints):
        try:
            new_status, prefix_text = await fetch_complaint_status(
                comp["forum_thread_url"]
            )
        except Exception:
            logger.exception("Ошибка при проверке жалобы id=%s", comp["id"])
            continue

        if new_status is None:
            # Не смогли определить — не меняем
            logger.debug("Жалоба id=%s: статус не удалось определить.", comp["id"])
            continue

        old_status = comp["status"]
        if new_status != old_status:
            await update_complaint_status(comp["id"], new_status)
            changed += 1
            logger.info("Жалоба id=%s: статус %s → %s (префикс «%s»).",
                        comp["id"], old_status, new_status, prefix_text or "?")

        # Шлём уведомление если статус финальный и пользователь о нём ещё не знал
        notified_status = comp.get("notified_status") or "pending"
        if new_status != notified_status and new_status in ("accepted", "rejected", "closed"):
            ok = await _notify_user(bot, comp, new_status)
            if ok:
                await mark_complaint_notified(comp["id"], new_status)
                notified += 1

        # Защита от rate-limit / DDoS-Guard
        if (i + 1) % BATCH_SIZE == 0:
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS * 4)
        else:
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    logger.info("Мониторинг: завершён. Изменений статусов: %d, уведомлений: %d.",
                changed, notified)


async def status_monitor_loop(bot: Bot) -> None:
    """Запускает бесконечный цикл проверки статусов с интервалом."""
    logger.info("Запущен фоновый мониторинг статусов жалоб "
                "(интервал %d сек).", CHECK_INTERVAL_SECONDS)
    # Стартовая задержка чтобы не дублировать с авто-импортом и sync на старте
    await asyncio.sleep(60)
    while True:
        try:
            await _check_once(bot)
        except asyncio.CancelledError:
            logger.info("Мониторинг статусов остановлен.")
            raise
        except Exception:
            logger.exception("Непредвиденная ошибка цикла мониторинга, "
                             "продолжаю через интервал.")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
