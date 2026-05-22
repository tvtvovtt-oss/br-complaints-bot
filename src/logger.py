"""Единая настройка логирования бота.

- Подробные русские сообщения в консоль и файл logs/bot.log с ротацией.
- Шумные библиотеки (httpx, httpcore, aiogram.event) приглушены до WARNING.
- В консоли уровни подсвечиваются цветом для быстрого восприятия.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import BASE_DIR


# ---- Маппинг английских уровней на русские подписи ----
_LEVEL_RU = {
    "DEBUG":    "ОТЛАДКА",
    "INFO":     "ИНФО   ",
    "WARNING":  "ВНИМ.  ",
    "ERROR":    "ОШИБКА ",
    "CRITICAL": "КРИТ.  ",
}

# ANSI цвета для уровней (только в консоли)
_LEVEL_COLOR = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[1;31m", # bold red
}
_RESET = "\033[0m"


class RussianFormatter(logging.Formatter):
    """Форматтер с русским уровнем и опциональной подсветкой."""

    def __init__(self, *, use_color: bool):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        # Время в локальном формате
        ts = self.formatTime(record, datefmt="%d.%m.%Y %H:%M:%S")
        level_ru = _LEVEL_RU.get(record.levelname, record.levelname)
        if self.use_color:
            color = _LEVEL_COLOR.get(record.levelname, "")
            level_ru = f"{color}{level_ru}{_RESET}"

        msg = record.getMessage()
        if record.exc_info:
            # Сохраняем traceback как у стандартного форматтера
            msg = f"{msg}\n{self.formatException(record.exc_info)}"

        # Имя логгера сокращаем для читаемости (src.handlers.complaint -> handlers.complaint)
        name = record.name.replace("src.", "")
        return f"{ts} | {level_ru} | {name:24s} | {msg}"


def _is_color_supported() -> bool:
    """Поддерживает ли текущий терминал ANSI-цвета."""
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # На современных Windows-терминалах поддержка есть, но включается флагом
        try:
            import os
            os.system("")  # активирует виртуальный терминал в cmd/PowerShell
            return True
        except Exception:
            return False
    return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Настраивает корневой логгер. Идемпотентно: повторные вызовы безопасны."""
    root = logging.getLogger()

    # Если уже настроено (есть наши хендлеры) — выходим
    if any(getattr(h, "_kiro_setup", False) for h in root.handlers):
        return root

    # Очищаем чужие хендлеры, чтобы не было дубликатов
    root.handlers.clear()
    root.setLevel(level)

    use_color = _is_color_supported()

    # ---- Консольный хендлер ----
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(RussianFormatter(use_color=use_color))
    console._kiro_setup = True  # type: ignore[attr-defined]
    root.addHandler(console)

    # ---- Файловый хендлер с ротацией ----
    logs_dir = Path(BASE_DIR) / "logs"
    logs_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        logs_dir / "bot.log",
        maxBytes=2 * 1024 * 1024,  # 2 МБ
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)  # в файл пишем подробнее
    file_handler.setFormatter(RussianFormatter(use_color=False))
    file_handler._kiro_setup = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    # ---- Подавляем шум библиотек ----
    for noisy in ("httpx", "httpcore", "aiogram.event", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


def describe_user(user) -> str:
    """Краткое описание пользователя Telegram для логов."""
    if user is None:
        return "<неизвестный>"
    parts = [f"id={user.id}"]
    if getattr(user, "username", None):
        parts.append(f"@{user.username}")
    name = (getattr(user, "full_name", "") or "").strip()
    if name:
        parts.append(f"«{name}»")
    return ", ".join(parts)
