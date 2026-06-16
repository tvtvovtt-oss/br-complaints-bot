"""Точечные уведомления админам о состоянии форумных аккаунтов.

Используется и в фоновом процессоре очереди, и в ручной подаче жалобы,
поэтому вынесено отдельно с антифлудом (один аккаунт — не чаще раза в
REAUTH_COOLDOWN секунд), чтобы повторные сбои не спамили админам.
"""
import logging
import time
from html import escape

from aiogram import Bot

from src.config import ADMIN_IDS
from src.ui.premium_emoji import te, PE_LOCK_CLOSED, PE_WARNING

logger = logging.getLogger(__name__)

# Антифлуд по account_id: не чаще раза в 30 минут на аккаунт.
REAUTH_COOLDOWN = 1800.0
_last_alert: dict[int, float] = {}


async def alert_account_reauth(bot: Bot, account_id: int, username: str) -> None:
    """Шлёт всем админам уведомление, что куки аккаунта протухли и нужен
    повторный /login. С антифлудом по account_id."""
    now = time.monotonic()
    if now - _last_alert.get(account_id, 0.0) < REAUTH_COOLDOWN:
        return
    _last_alert[account_id] = now

    text = (
        f"{te(PE_WARNING, '❗️')} <b>Куки форумного аккаунта протухли</b>\n\n"
        f"Аккаунт <b>{escape(username or '?')}</b> "
        f"(<code>id={account_id}</code>) больше не авторизован на форуме.\n"
        f"Он исключён из пула публикаций до перелогина.\n\n"
        f"{te(PE_LOCK_CLOSED, '🔐')} Зайдите заново: <code>/login</code> "
        "(или кнопка <b>Войти по паролю</b>)."
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.debug("reauth alert админу %s не доставлен", admin_id)
