from __future__ import annotations

from math import ceil

from src.settings import format_seconds


def estimate_wait_seconds(
    *,
    position: int,
    pool: dict,
    account_cooldown_seconds: int,
    process_interval_seconds: int,
    parallel_workers: int,
) -> int | None:
    """Rough ETA for a queue item by 1-based position."""
    if position <= 1:
        if pool.get("available", 0) > 0:
            return process_interval_seconds
        next_seconds = pool.get("next_available_seconds")
        return int(next_seconds) if next_seconds is not None else None

    total = int(pool.get("total", 0) or 0)
    usable = int(pool.get("usable", 0) or 0)
    available = int(pool.get("available", 0) or 0)
    if total <= 0 or usable <= 0:
        return None

    capacity = max(1, min(usable, max(1, parallel_workers)))
    if available > 0:
        available_capacity = min(available, capacity)
        if position <= available_capacity:
            return process_interval_seconds
        remaining_position = position - available_capacity
        cycles = ceil(remaining_position / capacity)
        return process_interval_seconds + cycles * account_cooldown_seconds

    next_seconds = pool.get("next_available_seconds")
    if next_seconds is None:
        return None
    cycles = ceil(position / capacity)
    return int(next_seconds) + max(0, cycles - 1) * account_cooldown_seconds


def queue_delay_reason(position: int, item: dict, pool: dict) -> str:
    total = int(pool.get("total", 0) or 0)
    usable = int(pool.get("usable", 0) or 0)
    reauth = int(pool.get("needs_reauth", 0) or 0)
    available = int(pool.get("available", 0) or 0)
    next_seconds = pool.get("next_available_seconds")

    if total <= 0:
        return "нет форумных аккаунтов в пуле"
    if usable <= 0 and reauth > 0:
        return "все аккаунты требуют повторный /login"
    if available <= 0 and next_seconds is not None:
        return f"все аккаунты в кулдауне, ближайший через {format_seconds(int(next_seconds))}"
    if position > 1:
        return f"перед ней в очереди {position - 1}"
    if item.get("last_error"):
        return f"повтор после ошибки: {str(item['last_error'])[:120]}"
    return "ждёт ближайший проход обработчика"


def enrich_queue_items(
    pending: list[dict],
    *,
    pool: dict,
    account_cooldown_seconds: int,
    process_interval_seconds: int,
    parallel_workers: int,
) -> list[dict]:
    enriched: list[dict] = []
    for position, item in enumerate(pending, 1):
        copy = dict(item)
        eta = estimate_wait_seconds(
            position=position,
            pool=pool,
            account_cooldown_seconds=account_cooldown_seconds,
            process_interval_seconds=process_interval_seconds,
            parallel_workers=parallel_workers,
        )
        copy["position"] = position
        copy["eta_seconds"] = eta
        copy["delay_reason"] = queue_delay_reason(position, item, pool)
        enriched.append(copy)
    return enriched


def format_eta(seconds: int | None) -> str:
    if seconds is None:
        return "неизвестно"
    if seconds <= 10:
        return "в ближайший проход"
    return f"примерно {format_seconds(seconds)}"
