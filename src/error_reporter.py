"""Отправка ошибок (ERROR/CRITICAL) в Telegram личным сообщением админу.

Подключается как logging.Handler. Использует тот же бэкап-канал, либо
ADMIN_IDS[0] если задана переменная LOG_TO_ADMIN=1.

Чтобы избежать рекурсии (handler вызывает send_message → если оно упадёт,
снова сработает handler), при отправке мы временно отключаем handler.
"""
import asyncio
import logging
import os
from collections import deque
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)

# Куда слать ошибки. По умолчанию — первому админу в личку.
# Можно переопределить:
#   ERROR_LOG_CHAT_ID — id чата (например, тот же что STORAGE_CHANNEL_ID)
#   LOG_TO_ADMIN=1    — слать первому из ADMIN_IDS
def _resolve_target_chat_id() -> Optional[int]:
    raw = os.getenv("ERROR_LOG_CHAT_ID", "").strip()
    if raw:
        try:
            return int(raw.replace("'", "").replace('"', ""))
        except ValueError:
            pass
    if os.getenv("LOG_TO_ADMIN", "").strip() == "1":
        from src.config import ADMIN_IDS
        if ADMIN_IDS:
            return ADMIN_IDS[0]
    return None


# Чтобы не заспамить чат — антифлуд: одно сообщение об одной и той же
# ошибке не чаще раза в N секунд.
_recent_keys: deque = deque(maxlen=100)
_recent_lock = asyncio.Lock()
SUPPRESS_REPEAT_SECONDS = 60


class TelegramErrorHandler(logging.Handler):
    """Шлёт ERROR/CRITICAL в Telegram. Использует bot который ему выдадут
    через set_bot()."""

    _bot: Optional[Bot] = None
    _chat_id: Optional[int] = None
    _enabled: bool = False

    def __init__(self, level: int = logging.ERROR):
        super().__init__(level=level)

    @classmethod
    def configure(cls, bot: Bot) -> bool:
        """Привязывает бот и активирует handler. Возвращает True если включено."""
        chat_id = _resolve_target_chat_id()
        if chat_id is None:
            return False
        cls._bot = bot
        cls._chat_id = chat_id
        cls._enabled = True
        logger.info("TelegramErrorHandler активирован → chat_id=%s", chat_id)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        if not TelegramErrorHandler._enabled:
            return
        if not TelegramErrorHandler._bot or not TelegramErrorHandler._chat_id:
            return
        # Игнорируем ошибки от самого aiogram-сетевого слоя — они часто
        # самовосстановимы (Server disconnected) и спамят.
        if record.name in ("aiogram.dispatcher", "aiogram.event"):
            if "Server disconnected" in record.getMessage():
                return

        try:
            text = self.format(record)
        except Exception:
            return

        # Запускаем отправку в фоне — emit() не должен блокировать
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._send(record, text))
        except RuntimeError:
            pass

    @staticmethod
    async def _send(record: logging.LogRecord, text: str) -> None:
        bot = TelegramErrorHandler._bot
        chat_id = TelegramErrorHandler._chat_id
        if not bot or not chat_id:
            return

        # Антифлуд по ключу (модуль + сообщение)
        key = f"{record.name}::{record.getMessage()[:120]}"
        import time
        async with _recent_lock:
            now = time.monotonic()
            for k, t in list(_recent_keys):
                if k == key and (now - t) < SUPPRESS_REPEAT_SECONDS:
                    return
            _recent_keys.append((key, now))

        # Формат: уровень + место + сообщение + (если есть) traceback
        level = record.levelname
        location = f"{record.name}:{record.funcName}:{record.lineno}"
        body = record.getMessage()
        if record.exc_info:
            import traceback
            tb = "".join(traceback.format_exception(*record.exc_info))
            tb = tb[-2500:]  # обрезаем до влезающего в Telegram-сообщение
        else:
            tb = ""

        from html import escape
        text_full = (
            f"<b>🚨 {level}</b>\n"
            f"<code>{escape(location)}</code>\n\n"
            f"{escape(body[:1500])}"
        )
        if tb:
            text_full += f"\n\n<pre>{escape(tb)}</pre>"
        # Telegram-лимит 4096
        if len(text_full) > 4000:
            text_full = text_full[:3950] + "\n\n<i>...обрезано</i></pre>"

        try:
            await bot.send_message(
                chat_id, text_full,
                disable_web_page_preview=True,
                disable_notification=False,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        except Exception:
            # Глушим — handler не должен сам себя ронять
            pass


def install(bot: Bot) -> bool:
    """Устанавливает TelegramErrorHandler в root logger.
    Если ERROR_LOG_CHAT_ID не задан и LOG_TO_ADMIN!=1 — не активирует."""
    handler = TelegramErrorHandler(level=logging.ERROR)
    if not handler.configure(bot):
        return False
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    return True
