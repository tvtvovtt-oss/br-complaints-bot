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
    get_account,
)
from src.forum.xenforo import fetch_complaint_status
from src.effects import EFFECT_CONFETTI

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
    "review":   "🔎 На рассмотрении",
    "accepted": "✅ Принята",
    "rejected": "❌ Отклонена",
    "closed":   "🔒 Закрыта",
    "unknown":  "❔ Неизвестно",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, f"❔ {status}")


async def _notify_user(bot: Bot, complaint: dict, new_status: str,
                        admin_comment: str | None = None) -> bool:
    """Шлёт уведомление автору жалобы. Возвращает True если доставлено."""
    label = status_label(new_status)
    nickname = escape(complaint.get("nickname", "?"))
    url = complaint.get("forum_thread_url") or ""
    link_part = (
        f"\n🔗 <a href=\"{escape(url)}\">Открыть тему</a>" if url else ""
    )

    comment_part = ""
    if admin_comment:
        comment_part = (
            f"\n\n📝 <b>Комментарий администратора:</b>\n"
            f"<blockquote>{escape(admin_comment)}</blockquote>"
        )

    if new_status == "accepted":
        text = (
            f"🎉 <b>Ваша жалоба на «{nickname}» принята!</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}{comment_part}"
        )
        effect = EFFECT_CONFETTI
    elif new_status == "rejected":
        text = (
            f"😔 <b>Жалоба на «{nickname}» отклонена.</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}{comment_part}"
        )
        if not admin_comment:
            text += (
                "\n\n<i>Возможные причины: недостаточно доказательств, "
                "истёк срок подачи, нарушены правила оформления.</i>"
            )
        effect = None
    elif new_status == "closed":
        text = (
            f"🔒 <b>Тема жалобы на «{nickname}» закрыта.</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}{comment_part}"
        )
        effect = None
    elif new_status == "review":
        text = (
            f"🔎 <b>Вашу жалобу на «{nickname}» взяли на рассмотрение.</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}{comment_part}\n\n"
            "<i>Скоро придёт окончательное решение.</i>"
        )
        effect = None
    else:
        text = (
            f"ℹ️ <b>Изменился статус жалобы на «{nickname}».</b>\n\n"
            f"Статус: <b>{label}</b>{link_part}{comment_part}"
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
    # Circuit breaker: считаем подряд идущие «не смогли определить» (None).
    # Если их слишком много — форум либо упал, либо забанил IP. Прерываем
    # весь цикл, чтобы не тратить минуты на гарантированно-провальные запросы.
    consecutive_unknown = 0
    UNKNOWN_BREAKER_THRESHOLD = 5

    for i, comp in enumerate(complaints):
        new_status = None
        prefix_text = None
        admin_comment = None
        try:
            # Собираем список наборов кук, которые имеет смысл попробовать.
            # Порядок важен: сначала «родной» аккаунт жалобы, потом весь
            # пул админа (на BR тема видна только автору и модераторам),
            # в самом конце — активные куки cookies.json как «последний шанс».
            cookies_to_try: list[dict | None] = []
            seen_ids: set[int] = set()

            if comp.get("account_id"):
                acc = await get_account(comp["account_id"])
                if acc and acc.get("cookies"):
                    cookies_to_try.append(acc["cookies"])
                    seen_ids.add(acc["id"])

            # Все остальные аккаунты владельца пула — как фолбэк
            from src.database import list_accounts
            from src.config import ADMIN_IDS
            owner_id = ADMIN_IDS[0] if ADMIN_IDS else comp["telegram_id"]
            pool = await list_accounts(owner_id)
            for acc_short in pool:
                if acc_short["id"] in seen_ids:
                    continue
                full = await get_account(acc_short["id"])
                if full and full.get("cookies"):
                    cookies_to_try.append(full["cookies"])
                    seen_ids.add(acc_short["id"])

            # Активные куки cookies.json (если ничего другого нет вообще)
            if not cookies_to_try:
                cookies_to_try.append(None)

            # Перебираем по очереди, пока кто-то не вернёт настоящий
            # префикс или финальный статус.
            for cookies in cookies_to_try:
                status_attempt, prefix_attempt, comment_attempt = (
                    await fetch_complaint_status(
                        comp["forum_thread_url"], cookies=cookies,
                    )
                )
                # Запоминаем последний осмысленный ответ — на случай если
                # ни один аккаунт не даст префикс, у нас будет хоть что-то.
                if status_attempt is not None:
                    new_status = status_attempt
                    prefix_text = prefix_attempt
                    admin_comment = comment_attempt
                # Если есть префикс — это самый надёжный сигнал, выходим
                if prefix_attempt:
                    break
                # Если форум сам сказал «закрыто/принято/отклонено» без префикса,
                # тоже считаем достоверным
                if status_attempt and status_attempt != "pending":
                    break

        except Exception:
            logger.exception("Ошибка при проверке жалобы id=%s", comp["id"])
            continue

        if new_status is None:
            # Не смогли определить — не меняем. Считаем для circuit breaker.
            consecutive_unknown += 1
            logger.debug("Жалоба id=%s: статус не удалось определить (%d подряд).",
                          comp["id"], consecutive_unknown)
            if consecutive_unknown >= UNKNOWN_BREAKER_THRESHOLD:
                logger.warning(
                    "Мониторинг: %d жалоб подряд недоступны (форум блокирует?), "
                    "прерываю цикл досрочно.", consecutive_unknown,
                )
                break
            continue
        # Успешный ответ — сбрасываем счётчик
        consecutive_unknown = 0

        old_status = comp["status"]
        if new_status != old_status:
            await update_complaint_status(comp["id"], new_status)
            changed += 1
            logger.info("Жалоба id=%s: статус %s → %s (префикс «%s»).",
                        comp["id"], old_status, new_status, prefix_text or "?")

        # Сохраняем свежий комментарий админа в БД (для карточки жалобы),
        # даже если уведомлять пока не нужно.
        if admin_comment and admin_comment != comp.get("admin_comment"):
            from src.database import update_complaint_admin_comment
            await update_complaint_admin_comment(comp["id"], admin_comment)

        # Шлём уведомление если статус финальный/review и пользователь о нём
        # ещё не знал. review — промежуточный, но юзеру важно знать.
        notified_status = comp.get("notified_status") or "pending"
        if new_status != notified_status and new_status in (
                "accepted", "rejected", "closed", "review"):
            ok = await _notify_user(bot, comp, new_status, admin_comment)
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
    # Жёсткий потолок: один цикл проверки не должен идти дольше 5 минут.
    # Если сеть зависнет, не блокируем мониторинг навсегда.
    cycle_timeout = 5 * 60
    while True:
        try:
            await asyncio.wait_for(_check_once(bot), timeout=cycle_timeout)
        except asyncio.TimeoutError:
            logger.warning("Цикл мониторинга статусов превысил %d сек — прерван.",
                           cycle_timeout)
        except asyncio.CancelledError:
            logger.info("Мониторинг статусов остановлен.")
            raise
        except Exception:
            logger.exception("Непредвиденная ошибка цикла мониторинга, "
                             "продолжаю через интервал.")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
