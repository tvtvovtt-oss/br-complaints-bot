"""Middleware-защита от спама и злоупотреблений.

Подключается в Dispatcher на уровне message и callback_query.
"""
import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject, User

logger = logging.getLogger(__name__)


# ---------- Конфиг ----------

# Минимальный интервал (секунды) между сообщениями одного пользователя.
# Если шлёт быстрее — бот мягко отвечает, что слишком часто.
MESSAGE_RATE_LIMIT = 0.4

# Минимальный интервал между callback одного пользователя (тапы по кнопкам)
CALLBACK_RATE_LIMIT = 0.25

# Сколько раз пользователь может пробить лимит за минуту, после чего
# бот игнорирует его сообщения 30 сек (мягкий бан)
SOFT_BAN_THRESHOLD = 8
SOFT_BAN_DURATION = 30.0

# Максимальная длина текста сообщения. Длиннее — игнорируем (Telegram
# и так режет на 4096 символов, но защищаемся от копипасты "стен текста").
MAX_TEXT_LENGTH = 4500


class ThrottleMiddleware(BaseMiddleware):
    """Глобальный rate-limit для сообщений и callback.

    Хранит время последнего обращения каждого пользователя в памяти.
    Не использует Redis/БД — для мелкого бота этого достаточно.
    """

    def __init__(self) -> None:
        super().__init__()
        # user_id -> last_seen_time
        self._last_message: dict[int, float] = defaultdict(float)
        self._last_callback: dict[int, float] = defaultdict(float)
        # Подсчёт превышений за последнюю минуту
        self._violations: dict[int, list[float]] = defaultdict(list)
        # Активные мягкие баны
        self._soft_ban_until: dict[int, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        uid = user.id
        now = time.monotonic()

        async with self._lock:
            # Проверяем мягкий бан
            ban_until = self._soft_ban_until[uid]
            if ban_until > now:
                logger.debug("Игнорю событие от %s — soft-ban ещё %.1f с.",
                             uid, ban_until - now)
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳ Слишком часто. Подождите немного.",
                                             show_alert=False)
                    except Exception:
                        pass
                return  # Молча игнорируем

            # Выбор лимита по типу события
            if isinstance(event, Message):
                last = self._last_message[uid]
                limit = MESSAGE_RATE_LIMIT
                # Защита от очень длинного текста
                if event.text and len(event.text) > MAX_TEXT_LENGTH:
                    try:
                        await event.answer("Сообщение слишком длинное, "
                                            "сократите до 4500 символов.")
                    except Exception:
                        pass
                    return
            elif isinstance(event, CallbackQuery):
                last = self._last_callback[uid]
                limit = CALLBACK_RATE_LIMIT
            else:
                return await handler(event, data)

            elapsed = now - last
            if elapsed < limit:
                # Пробил лимит — фиксируем нарушение
                violations = self._violations[uid]
                violations.append(now)
                # Чистим старые (>60 сек)
                violations[:] = [t for t in violations if now - t < 60.0]

                if len(violations) >= SOFT_BAN_THRESHOLD:
                    self._soft_ban_until[uid] = now + SOFT_BAN_DURATION
                    logger.warning("Soft-ban для user_id=%s на %.0f с "
                                    "(нарушений за минуту: %d).",
                                    uid, SOFT_BAN_DURATION, len(violations))
                    if isinstance(event, Message):
                        try:
                            await event.answer(
                                f"⏳ Слишком много действий за короткое время.\n"
                                f"Подождите {int(SOFT_BAN_DURATION)} секунд."
                            )
                        except Exception:
                            pass
                    elif isinstance(event, CallbackQuery):
                        try:
                            await event.answer("⏳ Слишком часто.", show_alert=False)
                        except Exception:
                            pass
                    return

                # Просто молча игнорим — слишком частое нажатие
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return

            # Обновляем время последнего события
            if isinstance(event, Message):
                self._last_message[uid] = now
            else:
                self._last_callback[uid] = now

        # Сам обработчик. Любые сетевые/телеграмные ошибки от заблокировавших
        # бот пользователей и подобные — глушим.
        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            logger.info("Пользователь %s заблокировал бота — игнорю.", uid)
        except TelegramBadRequest as e:
            # "message is not modified" и подобные — не страшны, в DEBUG
            if "not modified" in str(e).lower():
                logger.debug("Telegram: %s", e)
            else:
                logger.warning("TelegramBadRequest при обработке от %s: %s", uid, e)
        except Exception:
            logger.exception("Непредвиденная ошибка обработки события от user_id=%s", uid)


class CleanupMiddleware(BaseMiddleware):
    """Периодически чистит словари ThrottleMiddleware от давно неактивных
    пользователей, чтобы не разрастаться в памяти. Запускается раз в 1000
    сообщений."""

    def __init__(self, throttle: ThrottleMiddleware) -> None:
        super().__init__()
        self._throttle = throttle
        self._counter = 0

    async def __call__(self, handler, event, data):
        self._counter += 1
        if self._counter % 1000 == 0:
            await self._cleanup()
        return await handler(event, data)

    async def _cleanup(self) -> None:
        now = time.monotonic()
        cutoff = 3600  # час неактивности — забываем пользователя
        async with self._throttle._lock:
            for store in (self._throttle._last_message,
                          self._throttle._last_callback):
                # list(store.items()) — обязательно, иначе RuntimeError при
                # модификации dict во время итерации в pop().
                stale = [uid for uid, t in list(store.items()) if now - t > cutoff]
                for uid in stale:
                    store.pop(uid, None)
            for uid, t in list(self._throttle._soft_ban_until.items()):
                if t < now:
                    self._throttle._soft_ban_until.pop(uid, None)
            for uid, vlist in list(self._throttle._violations.items()):
                if not vlist or now - max(vlist) > 60:
                    self._throttle._violations.pop(uid, None)
        logger.debug("Throttle cache очищен.")


class MaintenanceMiddleware(BaseMiddleware):
    """Если включён режим обслуживания — пропускаем только админов.
    Остальным шлём короткое сообщение «бот на техработах».

    Подключать ПОСЛЕ ThrottleMiddleware (троттлинг должен сработать первым,
    чтобы не было спама). Не блокирует команду /start (там админ может
    включать/выключать режим).
    """

    async def __call__(self, handler, event, data):
        from src.maintenance import is_enabled
        from src.config import ADMIN_IDS

        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Админ всегда проходит
        if ADMIN_IDS and user.id in ADMIN_IDS:
            return await handler(event, data)
        # Если ADMIN_IDS пуст — все админы (для отладки), пропускаем
        if not ADMIN_IDS:
            return await handler(event, data)

        if not await is_enabled():
            return await handler(event, data)

        # Режим обслуживания включён, пользователь не админ — отказ.
        from aiogram.types import Message, CallbackQuery
        text = (
            "🔒 <b>Бот временно на техработах.</b>\n\n"
            "Подача жалоб приостановлена. Зайдите чуть позже — мы скоро "
            "снова откроем доступ. Спасибо за понимание!"
        )
        try:
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "🔒 Бот на техработах. Попробуйте позже.",
                    show_alert=True,
                )
        except Exception:
            pass
        # Не зовём handler — событие игнорируется
        return None
