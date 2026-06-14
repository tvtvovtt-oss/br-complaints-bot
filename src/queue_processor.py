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
import time
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
    release_account_cooldown,
    get_account_pool_status,
)
from src.forum.xenforo import post_complaint, is_auth_error, is_noperm_error
from src.settings import get_queue_settings, format_seconds
from src.premium_emoji import te, PE_WARNING, PE_TARGET

logger = logging.getLogger(__name__)


# Кулдаун аккаунтов — fallback-дефолт, если настройка в БД недоступна.
# Реальное значение читается из get_queue_settings() каждый проход.
ACCOUNT_COOLDOWN_SECONDS = 180

# Пауза между обработкой соседних жалоб (fallback-дефолт)
PROCESS_INTERVAL = 5

# Максимум попыток на одну жалобу (fallback-дефолт)
MAX_ATTEMPTS = 3

# Сколько жалоб обрабатываем одновременно (fallback-дефолт). У каждой
# публикации свой аккаунт из пула (claim_available_account), так что
# параллельные публикации не мешают друг другу.
PARALLEL_WORKERS = 2

_last_admin_alerts: dict[str, float] = {}


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


async def _notify_admins(
    bot: Bot,
    key: str,
    text: str,
    cooldown_seconds: int,
) -> None:
    now = time.monotonic()
    last = _last_admin_alerts.get(key, 0.0)
    if now - last < cooldown_seconds:
        return
    _last_admin_alerts[key] = now

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                text,
                disable_web_page_preview=True,
                disable_notification=False,
            )
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.debug("admin alert %s: %s", admin_id, e)
        except Exception:
            logger.exception("Не удалось отправить alert админу %s", admin_id)


async def _alert_pool_problem(
    bot: Bot,
    owner_id: int,
    pool: dict,
    queue_id: int,
    target: str,
    alert_cooldown: int,
) -> None:
    total = int(pool.get("total", 0) or 0)
    usable = int(pool.get("usable", 0) or 0)
    available = int(pool.get("available", 0) or 0)
    needs_reauth = int(pool.get("needs_reauth", 0) or 0)
    cooldown = int(pool.get("cooldown", 0) or 0)
    next_seconds = pool.get("next_available_seconds")

    if total <= 0:
        await _notify_admins(
            bot,
            f"queue_pool:{owner_id}:no_accounts",
            f"{te(PE_WARNING, '⚠️')} <b>Очередь жалоб ждёт аккаунты</b>\n\n"
            f"Жалоба <code>#{queue_id}</code> на <b>{escape(target)}</b> не может быть опубликована: "
            "в пуле нет ни одного форумного аккаунта.\n\n"
            "Добавьте аккаунт через <code>/login</code> или кнопку <b>🔐 Войти по паролю</b>.",
            alert_cooldown,
        )
        return

    if usable <= 0 and needs_reauth > 0:
        await _notify_admins(
            bot,
            f"queue_pool:{owner_id}:all_reauth",
            f"{te(PE_WARNING, '⚠️')} <b>Очередь жалоб остановилась</b>\n\n"
            f"Все форумные аккаунты требуют повторный вход: <b>{needs_reauth}/{total}</b>.\n"
            f"Жалоба <code>#{queue_id}</code> на <b>{escape(target)}</b> ждёт рабочий аккаунт.\n\n"
            "Откройте <code>/accounts</code> и перелогиньте аккаунты через <code>/login</code>.",
            alert_cooldown,
        )
        return

    if available <= 0 and cooldown > 0:
        wait_text = (
            format_seconds(int(next_seconds))
            if next_seconds is not None else "неизвестно"
        )
        await _notify_admins(
            bot,
            f"queue_pool:{owner_id}:all_cooldown",
            "⏳ <b>Очередь жалоб ждёт кулдаун</b>\n\n"
            f"Все доступные аккаунты сейчас в кулдауне: <b>{cooldown}</b>.\n"
            f"Ближайший освободится через <b>{wait_text}</b>.\n"
            f"Жалоба <code>#{queue_id}</code> на <b>{escape(target)}</b> останется в pending.",
            alert_cooldown,
        )


async def _process_one(bot: Bot, item: dict, cfg: dict[str, int]) -> None:
    """Публикует одну жалобу из очереди."""
    qid = item["id"]
    target = item["target_nickname"]
    section_id = item["section_id"]
    telegram_id = item["telegram_id"]
    max_attempts = cfg["max_attempts"]
    account_cooldown = cfg["account_cooldown_seconds"]
    alert_cooldown = cfg["admin_alert_cooldown_seconds"]

    if item["attempts"] >= max_attempts:
        logger.warning("Жалоба #%s превысила лимит попыток (%d) — failed.",
                       qid, max_attempts)
        await mark_queue_failed(qid, "превышен лимит попыток")
        await _notify_user(bot, telegram_id,
            f"❌ Жалоба из очереди на <b>{escape(target)}</b> "
            f"не была опубликована после {max_attempts} попыток.")
        return

    # Берём свободный аккаунт админа (общий пул)
    owner_id = ADMIN_IDS[0] if ADMIN_IDS else telegram_id
    account = await claim_available_account(owner_id, account_cooldown)

    if not account:
        # Все аккаунты в кулдауне или нет аккаунтов вообще.
        # Не помечаем failed — просто пропускаем итерацию, попробуем позже.
        # Перед этим уведомляем админов о проблеме пула (с антиспамом).
        logger.debug("Жалоба #%s ждёт свободный аккаунт.", qid)
        try:
            pool = await get_account_pool_status(owner_id)
            await _alert_pool_problem(
                bot, owner_id, pool, qid, target, alert_cooldown,
            )
        except Exception:
            logger.debug("alert_pool_problem failed", exc_info=True)
        await asyncio.sleep(10)
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
            f"{te(PE_TARGET, '🎯')} Цель: <b>{escape(target)}</b>\n"
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
            # Аккаунт валиден, просто не подходит для этого раздела —
            # возвращаем его в пул сразу, не держим 180с в кулдауне.
            await release_account_cooldown(account["id"])
            return

        # increment_queue_attempt поднимает attempts на 1.
        await increment_queue_attempt(qid, error=str(result))
        new_attempts = item["attempts"] + 1
        logger.warning("Жалоба #%s провалила попытку %d/%d: %s",
                       qid, new_attempts, max_attempts, result)
        if new_attempts >= max_attempts:
            await mark_queue_failed(qid, str(result))
            await _notify_user(bot, telegram_id,
                f"❌ <b>Жалоба из очереди не опубликована</b>\n\n"
                f"Цель: <b>{escape(target)}</b>\n"
                f"Причина: <code>{escape(str(result))}</code>")


async def queue_processor_loop(bot: Bot) -> None:
    """Бесконечный цикл обработки очереди.

    Обрабатывает до cfg["parallel_workers"] жалоб одновременно — у каждой
    свой аккаунт из пула (через claim_available_account), параллелизм не
    мешает кулдауну на стороне форума. Настройки (параллельность, интервал,
    лимит попыток, кулдаун) читаются из БД каждый проход — админ может
    менять их на лету через /settings.
    """
    logger.info("Запущен процессор очереди жалоб.")
    await asyncio.sleep(60)  # стартовая задержка, как у других циклов

    async def _run_one(item, cfg: dict[str, int], sem: asyncio.Semaphore):
        async with sem:
            try:
                await asyncio.wait_for(_process_one(bot, item, cfg), timeout=120)
            except asyncio.TimeoutError:
                logger.warning("Жалоба #%s — таймаут публикации.", item["id"])
                await increment_queue_attempt(item["id"], "таймаут")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка обработки жалобы #%s", item["id"])

    while True:
        try:
            # Настройки перечитываем каждый проход — дешёвый SELECT, зато
            # изменения из /settings применяются без рестарта бота.
            try:
                cfg = await get_queue_settings()
            except Exception:
                logger.debug("get_queue_settings failed, использую дефолты",
                             exc_info=True)
                cfg = {
                    "account_cooldown_seconds": ACCOUNT_COOLDOWN_SECONDS,
                    "process_interval_seconds": PROCESS_INTERVAL,
                    "max_attempts": MAX_ATTEMPTS,
                    "parallel_workers": PARALLEL_WORKERS,
                    "admin_alert_cooldown_seconds": 600,
                }
            interval = cfg["process_interval_seconds"]

            pending = await list_queue_pending()
            if not pending:
                await asyncio.sleep(interval)
                continue

            # Семафор создаём на каждый батч с актуальной параллельностью.
            sem = asyncio.Semaphore(max(1, cfg["parallel_workers"]))
            # Запускаем все pending параллельно (semaphore ограничит).
            await asyncio.gather(*(_run_one(item, cfg, sem) for item in pending))
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Процессор очереди остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка цикла процессора очереди.")
            await asyncio.sleep(PROCESS_INTERVAL)
