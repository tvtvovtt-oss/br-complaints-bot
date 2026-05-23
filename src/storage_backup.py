"""Бэкап SQLite-базы в приватный Telegram-канал.

Используется когда у хостинга нет постоянного хранилища: при каждом
значимом изменении бот шлёт свежий bot_database.db файлом в канал;
при старте — скачивает оттуда последний бэкап и кладёт на место БД.

Канал должен быть приватным, бот — админом канала с правом постить
и читать историю.

Подключение через переменную окружения STORAGE_CHANNEL_ID.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, FSInputFile

from src.config import DB_PATH

logger = logging.getLogger(__name__)


# Антифлуд: отправляем бэкап не чаще раза в N секунд (даже если изменений
# было несколько подряд). Реальная отправка идёт по дебаунсу.
BACKUP_DEBOUNCE_SECONDS = 30

# Имя файла в канале — фиксированное, чтобы при восстановлении было ясно что
# это именно наш бэкап.
BACKUP_FILE_NAME = "bot_database.db"

# Внутренний state — задача дебаунса
_pending_backup_task: Optional[asyncio.Task] = None
_pending_lock = asyncio.Lock()
# Ссылка на бот — устанавливается через set_bot() при старте, чтобы хендлеры
# могли вызывать schedule_backup() без явной передачи Bot.
_bot_ref: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    """Запоминает ссылку на bot. Вызывается один раз при старте."""
    global _bot_ref
    _bot_ref = bot


def get_channel_id() -> Optional[int]:
    """Возвращает int chat_id канала из переменной окружения или None."""
    raw = os.getenv("STORAGE_CHANNEL_ID", "").strip()
    if not raw:
        return None
    raw = raw.replace("'", "").replace('"', "").strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning("STORAGE_CHANNEL_ID=%r не парсится как int.", raw)
        return None


def is_enabled() -> bool:
    return get_channel_id() is not None


async def restore_db_from_channel(bot: Bot) -> bool:
    """При старте бота — пытается скачать последний бэкап из канала и
    положить его как DB_PATH (если локальной БД ещё нет или она пустая).

    Возвращает True если БД была восстановлена.
    """
    channel_id = get_channel_id()
    if channel_id is None:
        return False

    db_path = Path(DB_PATH)

    # Если БД уже существует и не пустая (>0 байт) — не трогаем, считаем
    # что это актуальное состояние.
    if db_path.exists() and db_path.stat().st_size > 0:
        logger.info("Локальная БД существует (%d байт) — пропускаю восстановление.",
                    db_path.stat().st_size)
        return False

    logger.info("Локальной БД нет — пытаюсь восстановить из канала %s ...",
                channel_id)

    # Telegram-API не позволяет напрямую перебирать сообщения канала через бота
    # (только если пометить сообщения вручную). Простой и надёжный путь —
    # хранить id последнего бэкапа в "закреплённом" сообщении канала.
    try:
        chat = await bot.get_chat(channel_id)
        pinned = chat.pinned_message
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось прочитать канал %s: %s", channel_id, e)
        return False

    if not pinned:
        logger.info("В канале нет закреплённого бэкапа — восстанавливать нечего.")
        return False

    if not pinned.document:
        logger.info("Закреплённое сообщение в канале — не файл, игнорирую.")
        return False

    if pinned.document.file_name != BACKUP_FILE_NAME:
        logger.info("Закреплён файл «%s», ожидался «%s» — игнорирую.",
                    pinned.document.file_name, BACKUP_FILE_NAME)
        return False

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Скачиваем сразу в файл по DB_PATH
        await bot.download(pinned.document, destination=str(db_path))
        size = db_path.stat().st_size if db_path.exists() else 0
        logger.info("✅ База восстановлена из канала: %d байт → %s",
                    size, db_path)
        return True
    except Exception as e:
        logger.exception("Не удалось скачать бэкап из канала: %s", e)
        return False


async def _send_backup_now(bot: Bot) -> None:
    """Шлёт текущий DB_PATH в канал и закрепляет сообщение."""
    channel_id = get_channel_id()
    if channel_id is None:
        return

    db_path = Path(DB_PATH)
    if not db_path.exists() or db_path.stat().st_size == 0:
        logger.debug("Бэкап пропущен: БД пуста или не существует.")
        return

    # SQLite в WAL-режиме держит часть данных в .db-wal/.db-shm. Чтобы
    # снимок .db был согласован, делаем checkpoint(TRUNCATE) перед чтением.
    try:
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        # Не страшно если PRAGMA упала — checkpoint всё равно происходит
        # автоматически при закрытии последнего соединения. В худшем случае
        # бэкап будет немного устаревшим.
        logger.debug("WAL checkpoint перед бэкапом не удался", exc_info=True)

    try:
        # SQLite может писать в файл прямо сейчас. Чтобы не получить
        # повреждённую копию, читаем содержимое и шлём как BufferedInputFile.
        # При маленьких БД (несколько МБ) это быстрее любых блокировок.
        data = db_path.read_bytes()
    except Exception as e:
        logger.warning("Не удалось прочитать БД для бэкапа: %s", e)
        return

    file = BufferedInputFile(data, filename=BACKUP_FILE_NAME)
    caption = (
        f"💾 <b>Бэкап БД</b>\n"
        f"Размер: {len(data):,} байт"
    ).replace(",", " ")

    try:
        msg = await bot.send_document(
            chat_id=channel_id,
            document=file,
            caption=caption,
            disable_notification=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось отправить бэкап в канал %s: %s",
                       channel_id, e)
        return
    except Exception as e:
        logger.exception("Непредвиденная ошибка отправки бэкапа: %s", e)
        return

    logger.info("✅ Бэкап БД отправлен в канал (msg_id=%s, %d байт).",
                msg.message_id, len(data))

    # Открепляем старые бэкапы и закрепляем новый — так restore возьмёт
    # именно последний файл.
    try:
        await bot.unpin_all_chat_messages(chat_id=channel_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.debug("unpin_all_chat_messages: %s", e)
    try:
        await bot.pin_chat_message(
            chat_id=channel_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось закрепить бэкап #%s: %s. "
                       "При следующем старте restore не найдёт его.",
                       msg.message_id, e)


async def schedule_backup(bot: Bot | None = None) -> None:
    """Ставит отложенную отправку бэкапа.

    Если уже есть запланированная задача — ничего не делает (дебаунс).
    Так серия событий за короткий промежуток времени = один бэкап в конце.

    Аргумент bot опциональный: если не передан, берётся из set_bot().
    """
    if not is_enabled():
        return

    target_bot = bot or _bot_ref
    if target_bot is None:
        logger.debug("schedule_backup вызван до set_bot() — пропускаю.")
        return

    global _pending_backup_task
    async with _pending_lock:
        if _pending_backup_task and not _pending_backup_task.done():
            # Уже запланировано — пропускаем
            return

        async def _runner():
            try:
                await asyncio.sleep(BACKUP_DEBOUNCE_SECONDS)
                await _send_backup_now(target_bot)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Ошибка в фоновом таске бэкапа")

        _pending_backup_task = asyncio.create_task(_runner())


async def force_backup(bot: Bot | None = None) -> None:
    """Немедленный синхронный бэкап (для /backup и при остановке)."""
    if not is_enabled():
        return
    target_bot = bot or _bot_ref
    if target_bot is None:
        return
    await _send_backup_now(target_bot)


async def periodic_backup_loop(bot: Bot, interval_seconds: int = 600) -> None:
    """Фоновый цикл: каждые N секунд (по умолчанию 10 минут) шлёт бэкап в
    канал, если БД изменилась с момента последней отправки.

    Это гарантия, что даже если хендлер забудет вызвать schedule_backup,
    данные не пропадут больше чем на interval_seconds.
    """
    if not is_enabled():
        logger.info("periodic_backup отключён: STORAGE_CHANNEL_ID не задан.")
        return

    set_bot(bot)
    logger.info("Запущен периодический бэкап БД в канал (раз в %d сек).",
                interval_seconds)

    db_path = Path(DB_PATH)
    last_mtime = 0.0
    # Стартовая задержка чтобы не дёргать прямо при старте
    await asyncio.sleep(interval_seconds)

    while True:
        try:
            if db_path.exists():
                mtime = db_path.stat().st_mtime
                if mtime > last_mtime:
                    await _send_backup_now(bot)
                    last_mtime = mtime
        except asyncio.CancelledError:
            logger.info("Периодический бэкап остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка в цикле периодического бэкапа.")
        await asyncio.sleep(interval_seconds)
