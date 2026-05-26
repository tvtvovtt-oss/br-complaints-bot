"""Режим обслуживания (maintenance mode).

Когда включён — бот для не-админов отказывает в обслуживании. Состояние
хранится в БД (таблица settings) с ключом 'maintenance' = 'on'/'off'.

Кэшируется в памяти на 30 секунд чтобы не дёргать БД на каждое сообщение.
Все обращения к кэшу идут под asyncio.Lock — даже параллельный
read-through из 100 корутин даст ровно один поход в БД.
"""
import asyncio
import logging
import time
from typing import Optional

from src.database import get_setting, set_setting

logger = logging.getLogger(__name__)

KEY = "maintenance"
_cached_value: Optional[bool] = None
_cached_at: float = 0.0
_TTL = 30.0  # секунд
_lock = asyncio.Lock()


async def is_enabled() -> bool:
    """True если режим обслуживания включён (бот доступен только админам)."""
    global _cached_value, _cached_at
    now = time.monotonic()
    # Быстрый путь без лока — кэш свежий, читаем атомарно (Bool в CPython)
    cached = _cached_value
    if cached is not None and (now - _cached_at) < _TTL:
        return cached

    async with _lock:
        # Повторная проверка под локом: пока ждали, кто-то уже мог обновить
        cached = _cached_value
        if cached is not None and (time.monotonic() - _cached_at) < _TTL:
            return cached
        val = await get_setting(KEY, "off")
        _cached_value = (val == "on")
        _cached_at = time.monotonic()
        return _cached_value


async def enable() -> None:
    """Включает режим обслуживания."""
    global _cached_value, _cached_at
    await set_setting(KEY, "on")
    async with _lock:
        _cached_value = True
        _cached_at = time.monotonic()
    logger.info("🔒 Режим обслуживания ВКЛЮЧЁН — бот доступен только админам.")


async def disable() -> None:
    """Выключает режим обслуживания."""
    global _cached_value, _cached_at
    await set_setting(KEY, "off")
    async with _lock:
        _cached_value = False
        _cached_at = time.monotonic()
    logger.info("🔓 Режим обслуживания ВЫКЛЮЧЕН — бот снова доступен всем.")


def invalidate_cache() -> None:
    """Сбрасывает кэш — следующий вызов is_enabled прочитает из БД."""
    global _cached_value, _cached_at
    _cached_value = None
    _cached_at = 0.0
