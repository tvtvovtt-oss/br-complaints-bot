"""Шифрование секретных данных (паролей форумных аккаунтов).

Использует cryptography.fernet (AES-128-CBC + HMAC-SHA256). Мастер-ключ
читается из переменной окружения SECRET_KEY. Если ключ не задан, шифрование
отключается и сохранять пароли нельзя.

Генерация нового ключа:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Сохраните ключ в .env как SECRET_KEY=... и не публикуйте.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_fernet = None  # ленивая инициализация
_init_attempted = False


def _init_fernet() -> None:
    """Ленивая инициализация Fernet — пытаемся прочитать ключ при первом
    обращении. Делаем это лениво, чтобы импорт модуля не падал при отсутствии
    переменной."""
    global _fernet, _init_attempted
    if _init_attempted:
        return
    _init_attempted = True

    key = os.getenv("SECRET_KEY", "").strip()
    if not key:
        logger.warning(
            "SECRET_KEY не задан — шифрование паролей отключено. "
            "Авто-перелогин будет недоступен."
        )
        return

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.error("Пакет cryptography не установлен.")
        return

    try:
        # Если ключ короткий или не в формате Fernet — выдаст ValueError
        _fernet = Fernet(key.encode())
        logger.info("Шифрование инициализировано (Fernet AES-128).")
    except Exception as e:
        logger.error("Не удалось инициализировать Fernet: %s. "
                     "Проверьте формат SECRET_KEY (должен быть base64 url-safe, 32 байта).", e)


def is_available() -> bool:
    """True если шифрование доступно и можно сохранять секреты."""
    _init_fernet()
    return _fernet is not None


def encrypt(plaintext: str) -> Optional[str]:
    """Шифрует строку. Возвращает base64-токен или None при ошибке/отсутствии ключа."""
    _init_fernet()
    if _fernet is None or plaintext is None:
        return None
    try:
        token = _fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")
    except Exception:
        logger.exception("Ошибка шифрования.")
        return None


def decrypt(token: str) -> Optional[str]:
    """Расшифровывает строку. Возвращает None если ключ не подходит/токен битый."""
    _init_fernet()
    if _fernet is None or not token:
        return None
    try:
        from cryptography.fernet import InvalidToken
        plaintext = _fernet.decrypt(token.encode("ascii"))
        return plaintext.decode("utf-8")
    except InvalidToken:
        logger.warning("Не удалось расшифровать токен — ключ не совпадает или "
                       "токен повреждён. Возможно, изменился SECRET_KEY.")
        return None
    except Exception:
        logger.exception("Ошибка расшифровки.")
        return None
