"""Проверка обязательной подписки на Telegram-каналы.

Список каналов редактируется админом через /subs и хранится в БД
(см. database.subscription_channels). Middleware блокирует все
сообщения/callback'и от пользователей, которые не подписаны на ВСЕ
каналы списка — им показывается inline-кнопка «Подписаться» +
«Проверить подписку».

Исключения (пропускаются без проверки):
- Админы бота (по ADMIN_IDS).
- Команда /start, /help и нажатие на кнопку перепроверки.
- Если список каналов пуст (фича отключена).
"""
import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, TelegramObject,
)

from src.config import ADMIN_IDS
from src.database import list_subscription_channels
from src.ui.premium_emoji import (
    te, BTN_SUCCESS, BTN_PRIMARY,
    PE_BELL, PE_CHECK, PE_MEGAPHONE,
)

logger = logging.getLogger(__name__)

# Callback-префикс для кнопки "Проверить подписку" в меню блокировки.
# Выбран отдельный префикс, чтобы не конфликтовать с /subs:* (админ-команда).
RESUB_CHECK_CB = "resub:check"

# Время кеширования результата проверки подписки на одного пользователя,
# чтобы не дёргать Telegram API на каждое сообщение в спам-режиме.
CHECK_TTL_SEC = 60.0


def _is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        # В дев-режиме без ADMIN_IDS пропускаем проверку, чтобы не
        # блокировать разработчика на ровном месте.
        return True
    return user_id in ADMIN_IDS


def _bypass_message(event: Message) -> bool:
    """True если это сообщение надо пропустить без проверки подписки."""
    if not event.text:
        return False
    # /start и /help — пропускаем всегда (пользователь должен иметь
    # возможность узнать, что бот существует и что от него требуется).
    txt = event.text.strip()
    if txt.startswith("/start") or txt.startswith("/help"):
        return True
    return False


def _bypass_callback(event: CallbackQuery) -> bool:
    """True если callback надо пропустить без проверки.

    Кнопка «Проверить подписку» пропускается, иначе при тапе на неё мы
    зациклимся: callback попадёт в middleware, а тот скажет «не подписан»
    и пошлёт новое сообщение, в котором опять эта кнопка.
    """
    data = event.data or ""
    if data == RESUB_CHECK_CB:
        return True
    # /subs:* — управление списком каналов; эти callback'и доступны
    # только админам, а их middleware пропускает без проверки. Но на
    # всякий случай — если админ не в ADMIN_IDS, пусть кнопка сработает.
    if data.startswith("subs:"):
        return True
    return False


def build_subscribe_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    """Собирает inline-клавиатуру: по кнопке-ссылке на каждый канал +
    кнопка «Проверить подписку» снизу."""
    rows: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        # Ссылка вида t.me/<channel> работает для публичных каналов и
        # автоматически открывает их в приложении Telegram.
        url = f"https://t.me/{ch}"
        rows.append([
            InlineKeyboardButton(
                text=f"Подписаться на @{ch}",
                url=url,
                icon_custom_emoji_id=PE_MEGAPHONE,
                style=BTN_PRIMARY,
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            text="Проверить подписку",
            callback_data=RESUB_CHECK_CB,
            icon_custom_emoji_id=PE_CHECK,
            style=BTN_SUCCESS,
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_subscribe_text(channels: list[str]) -> str:
    """Текст приглашения подписаться."""
    listed = "\n".join(f"  • @{ch}" for ch in channels)
    return (
        f"{te(PE_BELL, '🔔')} <b>Чтобы пользоваться ботом, подпишитесь "
        "на каналы наших спонсоров:</b>\n\n"
        f"{listed}\n\n"
        f"{te(PE_CHECK, '✅')} После подписки нажмите "
        "<b>«Проверить подписку»</b>."
    )


class SubscriptionMiddleware(BaseMiddleware):
    """Проверяет обязательную подписку перед передачей события в хендлеры.

    Кеш: user_id -> (missing_channels_list, expires_at_monotonic).
    При повторной проверке в течение CHECK_TTL_SEC возвращаем кеш.
    """

    def __init__(self) -> None:
        super().__init__()
        self._missing_cache: dict[int, tuple[list[str], float]] = {}
        # Чтобы при старте/добавлении канала все кеши сразу протухли.
        self._cache_epoch: int = 0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Сбросить весь кеш. Вызывается из /subs при изменении списка."""
        self._cache_epoch += 1
        self._missing_cache.clear()

    async def _check_user(self, bot, user_id: int) -> list[str]:
        """Возвращает список каналов, на которые user_id НЕ подписан.
        Пустой список = подписан на все.
        """
        async with self._lock:
            cached = self._missing_cache.get(user_id)
            if cached and cached[1] > time.monotonic():
                return cached[0]

        channels = await list_subscription_channels()
        if not channels:
            return []  # Список пуст — фича отключена

        missing: list[str] = []
        # Проверяем каналы параллельно — getChatMember это сетевой вызов.
        async def _one(ch: str) -> tuple[str, str | None]:
            try:
                member = await bot.get_chat_member(chat_id=f"@{ch}", user_id=user_id)
                # member.status ∈ {"creator","administrator","member","restricted","left","kicked"}
                if member.status in ("left", "kicked"):
                    return ch, "not_member"
                return ch, None
            except TelegramForbiddenError:
                # Бот сам не подписан / канал не существует — пропускаем
                # канал, чтобы не блокировать пользователя на ровном месте.
                logger.warning(
                    "get_chat_member вернул TelegramForbiddenError для @%s; "
                    "пропускаю канал из проверки.", ch,
                )
                return ch, "bot_forbidden"
            except TelegramAPIError as e:
                logger.warning(
                    "get_chat_member для @%s упал: %s; считаю канал пройденным.",
                    ch, e,
                )
                return ch, "api_error"
            except Exception as e:
                logger.exception(
                    "Неожиданная ошибка проверки подписки на @%s: %s", ch, e,
                )
                return ch, "api_error"

        results = await asyncio.gather(*(_one(c) for c in channels))
        for ch, err in results:
            if err == "not_member":
                missing.append(ch)

        async with self._lock:
            self._missing_cache[user_id] = (missing, time.monotonic() + CHECK_TTL_SEC)
        return missing

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        bot = data.get("bot")
        if user is None or bot is None:
            return await handler(event, data)

        # Админы проходят без проверки
        if _is_admin(user.id):
            return await handler(event, data)

        # События-исключения
        if isinstance(event, Message) and _bypass_message(event):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and _bypass_callback(event):
            return await handler(event, data)

        missing = await self._check_user(bot, user.id)
        if not missing:
            return await handler(event, data)

        # Не подписан — блокируем.
        text = build_subscribe_text(missing)
        kb = build_subscribe_keyboard(missing)
        try:
            if isinstance(event, Message):
                await event.answer(text, reply_markup=kb, disable_web_page_preview=True)
            elif isinstance(event, CallbackQuery):
                # Для callback нельзя редактировать, если сообщение — не наше;
                # на всякий случай пробуем ответить alert'ом + показать текст.
                try:
                    await event.message.answer(
                        text, reply_markup=kb, disable_web_page_preview=True,
                    )
                except Exception:
                    pass
                try:
                    await event.answer(
                        "Сначала подпишитесь на каналы спонсоров.",
                        show_alert=True,
                    )
                except Exception:
                    pass
        except Exception:
            logger.exception("Не смог отправить сообщение о подписке")
        return None


async def recheck_and_reply(bot, callback: CallbackQuery) -> bool:
    """Перепроверяет подписку для callback.message.from_user.id и
    редактирует исходное сообщение (убирая кнопки подписки), если
    пользователь подписался.

    Возвращает True если подписка в порядке, False если всё ещё не подписан.
    """
    user_id = callback.from_user.id
    mw = _get_active_middleware()
    if mw is not None:
        # Сбросить кеш по этому юзеру — пусть проверит заново.
        mw._missing_cache.pop(user_id, None)

    missing = []
    if mw is not None:
        missing = await mw._check_user(bot, user_id)
    else:
        # Фолбэк — на случай если middleware не зарегистрирован (не должно быть).
        from src.database import list_subscription_channels
        for ch in await list_subscription_channels():
            try:
                m = await bot.get_chat_member(chat_id=f"@{ch}", user_id=user_id)
                if m.status in ("left", "kicked"):
                    missing.append(ch)
            except Exception:
                pass

    if not missing:
        try:
            await callback.message.edit_text(
                f"{te(PE_CHECK, '✅')} Спасибо! Подписка подтверждена. "
                "Добро пожаловать!",
            )
        except Exception:
            await callback.message.answer(
                f"{te(PE_CHECK, '✅')} Спасибо! Подписка подтверждена. "
                "Добро пожаловать!",
            )
        try:
            await callback.answer()
        except Exception:
            pass
        return True

    # Всё ещё не подписан
    text = build_subscribe_text(missing)
    kb = build_subscribe_keyboard(missing)
    try:
        await callback.message.edit_text(
            text, reply_markup=kb, disable_web_page_preview=True,
        )
    except Exception:
        try:
            await callback.message.answer(
                text, reply_markup=kb, disable_web_page_preview=True,
            )
        except Exception:
            pass
    try:
        await callback.answer(
            "Вы ещё не подписаны на все каналы.", show_alert=True,
        )
    except Exception:
        pass
    return False


# Хранилище активного middleware — нужно, чтобы callback-кнопка
# «Проверить подписку» могла попросить middleware перепроверить
# пользователя и не использовать свой кеш.
_active_mw: "SubscriptionMiddleware | None" = None


def set_middleware(mw: "SubscriptionMiddleware") -> None:
    global _active_mw
    _active_mw = mw


def _get_active_middleware() -> "SubscriptionMiddleware | None":
    return _active_mw
