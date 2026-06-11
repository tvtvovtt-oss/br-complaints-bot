from __future__ import annotations

from dataclasses import dataclass

from src.database import get_setting, set_setting


@dataclass(frozen=True)
class IntSetting:
    key: str
    label: str
    description: str
    default: int
    min_value: int
    max_value: int
    unit: str = "sec"


SETTINGS: dict[str, IntSetting] = {
    "queue_account_cooldown_seconds": IntSetting(
        key="queue_account_cooldown_seconds",
        label="Кулдаун аккаунта",
        description="Пауза после публикации жалобы одним форумным аккаунтом.",
        default=180,
        min_value=30,
        max_value=3600,
    ),
    "queue_process_interval_seconds": IntSetting(
        key="queue_process_interval_seconds",
        label="Интервал очереди",
        description="Пауза между проходами фонового обработчика очереди.",
        default=5,
        min_value=1,
        max_value=300,
    ),
    "queue_max_attempts": IntSetting(
        key="queue_max_attempts",
        label="Попыток на жалобу",
        description="Сколько раз очередь пробует опубликовать жалобу перед failed.",
        default=3,
        min_value=1,
        max_value=10,
        unit="шт.",
    ),
    "queue_parallel_workers": IntSetting(
        key="queue_parallel_workers",
        label="Параллельность очереди",
        description="Сколько жалоб очередь может публиковать одновременно.",
        default=2,
        min_value=1,
        max_value=10,
        unit="шт.",
    ),
    "status_check_interval_seconds": IntSetting(
        key="status_check_interval_seconds",
        label="Интервал статусов",
        description="Как часто фоновый мониторинг проверяет статусы жалоб.",
        default=300,
        min_value=60,
        max_value=3600,
    ),
    "admin_alert_cooldown_seconds": IntSetting(
        key="admin_alert_cooldown_seconds",
        label="Антиспам алертов",
        description="Минимальная пауза между одинаковыми уведомлениями админам.",
        default=600,
        min_value=60,
        max_value=86400,
    ),
}


def format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м {sec}с" if sec else f"{minutes}м"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes}м" if minutes else f"{hours}ч"


def format_setting_value(setting: IntSetting, value: int) -> str:
    if setting.unit == "sec":
        return format_seconds(value)
    return f"{value} {setting.unit}"


async def get_int_setting(key: str) -> int:
    setting = SETTINGS[key]
    raw = await get_setting(key, str(setting.default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return setting.default
    return min(setting.max_value, max(setting.min_value, value))


async def set_int_setting(key: str, value: int) -> int:
    setting = SETTINGS[key]
    value = min(setting.max_value, max(setting.min_value, int(value)))
    await set_setting(key, str(value))
    return value


async def get_settings_values() -> dict[str, int]:
    return {key: await get_int_setting(key) for key in SETTINGS}


async def get_queue_settings() -> dict[str, int]:
    return {
        "account_cooldown_seconds": await get_int_setting(
            "queue_account_cooldown_seconds",
        ),
        "process_interval_seconds": await get_int_setting(
            "queue_process_interval_seconds",
        ),
        "max_attempts": await get_int_setting("queue_max_attempts"),
        "parallel_workers": await get_int_setting("queue_parallel_workers"),
        "admin_alert_cooldown_seconds": await get_int_setting(
            "admin_alert_cooldown_seconds",
        ),
    }
