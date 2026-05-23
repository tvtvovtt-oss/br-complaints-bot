"""Режим обслуживания (maintenance mode).

Когда включён — бот для не-админов работает только на /start (с пояснением)
и игнорирует всё остальное. Состояние хранится в БД (таблица settings)
с ключом 'maintenance' = 'on'/'off'.

Кэшируется в памяти на 30 секунд чтобы не дёргать БД на каждое сообщение.
"""
import logging
import time
from typing import Optional

from src.database import get_setting, set_setting

logger = logging.getLogger(__name__)

KEY = "maintenance"
_cached_value: Optional[bool] = None
_cached_at: float = 0.0
_TTL = 30.0  # секунд


async def is_enabled() -> bool:
    """True если режим обслуживания включён (бот доступен только админам)."""
    global _cached_value, _cached_at
    now = time.monotonic()
    if _cached_value is not None and (now - _cached_at) < _TTL:
        return _cached_value
    val = await get_setting(KEY, "off")
    _cached_value = (val == "on")
    _cached_at = now
    return _cached_value


async def enable() -> None:
    """Включает режим обслуживания."""
    global _cached_value, _cached_at
    await set_setting(KEY, "on")
    _cached_value = True
    _cached_at = time.monotonic()
    logger.info("🔒 Режим обслуживания ВКЛЮЧЁН — бот доступен только админам.")


async def disable() -> None:
    """Выключает режим обслуживания."""
    global _cached_value, _cached_at
    await set_setting(KEY, "off")
    _cached_value = False
    _cached_at = time.monotonic()
    logger.info("🔓 Режим обслуживания ВЫКЛЮЧЕН — бот снова доступен всем.")


def invalidate_cache() -> None:
    """Сбрасывает кэш — следующий вызов is_enabled прочитает из БД."""
    global _cached_value, _cached_at
    _cached_value = None
    _cached_at = 0.0
