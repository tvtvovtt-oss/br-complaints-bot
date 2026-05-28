"""Фоновый обработчик очереди жалоб.

Берёт по одной pending-жалобе из БД, ищет свободный аккаунт через
claim_available_account и публикует на форуме. После успеха шлёт
автору ссылку на тему. При ошибке — помечает failed и тоже уведомляет.

Каждый цикл:
  1. fetch первого pending
  2. claim аккаунта (если все в кулдауне — ждём 30 сек)
  3. apply cookies → post_complaint
  4. update DB + уведомление
"""
import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from src.config import ADMIN_IDS
from src.database import (
    list_queue_pending,
    mark_queue_done,
    mark_queue_failed,
    increment_queue_attempt,
    claim_available_account,
    add_complaint,
    update_account_cookies,
    mark_account_needs_reauth,
)
from src.forum.xenforo import post_complaint, is_auth_error, is_noperm_error

logger = logging.getLogger(__name__)


# Кулдаун аккаунтов (должен совпадать с COMPLAINT_COOLDOWN_SECONDS из complaint.py)
ACCOUNT_COOLDOWN_SECONDS = 180

# Пауза между обработкой соседних жалоб
PROCESS_INTERVAL = 5

# Максимум попыток на одну жалобу
MAX_ATTEMPTS = 3

# Сколько жалоб обрабатываем одновременно. У каждой публикации свой аккаунт
# из пула (claim_available_account), так что параллельные публикации не мешают
# друг другу. 2 — компромисс: ускорение в 2 раза, но не перегружаем форум.
PARALLEL_WORKERS = 2


async def _notify_user(bot: Bot, telegram_id: int, text: str,
                        disable_preview: bool = False) -> None:
    """Безопасное уведомление пользователя — глушит ошибки доставки."""
    try:
        await bot.send_message(telegram_id, text,
                                disable_web_page_preview=disable_preview)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.debug("notify %s: %s", telegram_id, e)
    except Exception:
        logger.exception("Неизвестная ошибка уведомления %s", telegram_id)


async def _process_one(bot: Bot, item: dict) -> None:
    """Публикует одну жалобу из очереди."""
    qid = item["id"]
    target = item["target_nickname"]
    section_id = item["section_id"]
    telegram_id = item["telegram_id"]

    if item["attempts"] >= MAX_ATTEMPTS:
        logger.warning("Жалоба #%s превысила лимит попыток (%d) — failed.",
                       qid, MAX_ATTEMPTS)
        await mark_queue_failed(qid, "превышен лимит попыток")
        await _notify_user(bot, telegram_id,
            f"❌ Жалоба из очереди на <b>{escape(target)}</b> "
            f"не была опубликована после {MAX_ATTEMPTS} попыток.")
        return

    # Берём свободный аккаунт админа (общий пул)
    owner_id = ADMIN_IDS[0] if ADMIN_IDS else telegram_id
    account = await claim_available_account(owner_id, ACCOUNT_COOLDOWN_SECONDS)

    if not account:
        # Все аккаунты в кулдауне или нет аккаунтов вообще.
        # Не помечаем failed — просто пропускаем итерацию, попробуем позже.
        logger.debug("Жалоба #%s ждёт свободный аккаунт.", qid)
        return

    logger.info("Жалоба из очереди #%s публикуется от имени «%s» (попытка %d).",
                qid, account["username"], item["attempts"] + 1)

    # Передаём куки явно — это исключает race на cookies.json при
    # параллельных воркерах. post_complaint не трогает общий файл,
    # работает в изолированном httpx-клиенте.
    success, result = await post_complaint(
        section_id=section_id,
        title=item["title"],
        message=item["bb_code"],
        cookies=account["cookies"],
    )

    if success:
        # Сохраняем в БД ссылку на тему и шлём пользователю уведомление.
        # Свежие куки (xf_session/xf_csrf могли обновиться) не зеркалим —
        # post_complaint в режиме cookies= не имеет доступа к свежему jar.
        # Это компромисс: альтернатива — race на cookies.json. Куки в БД
        # остаются «как при логине» — XenForo сам обновит при следующем
        # запросе через apply_account_cookies в админских сценариях.
        await mark_queue_done(qid, result)
        await add_complaint(
            telegram_id=telegram_id,
            nickname=target,
            description=item["description"],
            proof_link=item["proof_link"],
            forum_thread_url=result,
            account_id=account["id"],
        )
        await _notify_user(bot, telegram_id,
            f"🎉 <b>Жалоба из очереди опубликована!</b>\n\n"
            f"🎯 Цель: <b>{escape(target)}</b>\n"
            f"🔗 <a href=\"{escape(result)}\">Открыть тему на форуме</a>")
    else:
        # AUTH-ошибка — куки протухли, помечаем аккаунт как нужный перелогин.
        # Жалобу возвращаем в pending (не failed) — другой аккаунт её
        # подхватит на следующей итерации.
        if is_auth_error(str(result)):
            await mark_account_needs_reauth(account["id"])
            await increment_queue_attempt(qid, error=str(result))
            logger.warning(
                "Жалоба #%s: аккаунт «%s» нуждается в перелогине, "
                "пробуем другим на следующей итерации.",
                qid, account["username"],
            )
            return

        # NOPERM — нет прав в разделе или DDoS-Guard на path.
        # Аккаунт валиден, просто не подходит для этой жалобы. Не считаем
        # это «провалом попытки» (иначе после 3 разных аккаунтов жалоба
        # уйдёт в failed) — оставляем в pending, на следующей итерации
        # claim_available_account даст следующий по rotation.
        if is_noperm_error(str(result)):
            logger.info(
                "Жалоба #%s: аккаунт «%s» не имеет прав в разделе %s — "
                "оставляю в pending для другого аккаунта.",
                qid, account["username"], section_id,
            )
            return

        # increment_queue_attempt поднимает attempts на 1.
        await increment_queue_attempt(qid, error=str(result))
        new_attempts = item["attempts"] + 1
        logger.warning("Жалоба #%s провалила попытку %d/%d: %s",
                       qid, new_attempts, MAX_ATTEMPTS, result)
        if new_attempts >= MAX_ATTEMPTS:
            await mark_queue_failed(qid, str(result))
            await _notify_user(bot, telegram_id,
                f"❌ <b>Жалоба из очереди не опубликована</b>\n\n"
                f"Цель: <b>{escape(target)}</b>\n"
                f"Причина: <code>{escape(str(result))}</code>")


async def queue_processor_loop(bot: Bot) -> None:
    """Бесконечный цикл обработки очереди.

    Обрабатывает до PARALLEL_WORKERS жалоб одновременно — у каждой свой
    аккаунт из пула (через claim_available_account), параллелизм не
    мешает кулдауну на стороне форума.
    """
    logger.info("Запущен процессор очереди жалоб (интервал %d сек, "
                "параллельность %d).", PROCESS_INTERVAL, PARALLEL_WORKERS)
    await asyncio.sleep(60)  # стартовая задержка, как у других циклов

    sem = asyncio.Semaphore(PARALLEL_WORKERS)

    async def _run_one(item):
        async with sem:
            try:
                await asyncio.wait_for(_process_one(bot, item), timeout=120)
            except asyncio.TimeoutError:
                logger.warning("Жалоба #%s — таймаут публикации.", item["id"])
                await increment_queue_attempt(item["id"], "таймаут")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка обработки жалобы #%s", item["id"])

    while True:
        try:
            pending = await list_queue_pending()
            if not pending:
                await asyncio.sleep(PROCESS_INTERVAL)
                continue

            # Запускаем все pending параллельно (semaphore ограничит).
            await asyncio.gather(*(_run_one(item) for item in pending))
            await asyncio.sleep(PROCESS_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Процессор очереди остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка цикла процессора очереди.")
            await asyncio.sleep(PROCESS_INTERVAL)
