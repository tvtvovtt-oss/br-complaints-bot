"""Авто-перелогин на форум по сохранённому паролю.

Что делает:
1. Берёт куки из БД для аккаунта, проверяет — живые ли через check_auth.
2. Если куки умерли — берёт зашифрованный пароль (`encrypted_password`),
   расшифровывает Fernet-ключом из SECRET_KEY, логинится на форум.
3. Если форум требует 2FA — отказ, авто-перелогин невозможен (нужен email-код
   от пользователя). В лог пишем что нужен ручной /login.
4. Свежие куки сохраняются в БД через update_account_cookies.

Запускается:
- По расписанию (`auto_relogin_loop`, раз в 12 часов).
- При публикации жалобы, если все аккаунты вернули 403 (можно дёрнуть вручную).
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Интервал фонового цикла в секундах. 12 часов — куки на BR живут несколько дней,
# но раз в полдня имеет смысл проверять.
RELOGIN_LOOP_INTERVAL = 12 * 60 * 60


async def relogin_account(account_id: int) -> tuple[bool, str]:
    """Пробует авторизовать аккаунт по сохранённому паролю.

    Возвращает (успех, причина).

    Не работает если:
    - SECRET_KEY не настроен (шифрование выключено)
    - У аккаунта нет сохранённого пароля
    - Форум требует 2FA (нужен ручной /login для ввода кода с email)
    """
    from src.crypto import is_available as crypto_available, decrypt
    from src.database import (
        get_account, get_account_encrypted_password,
        update_account_cookies,
    )
    from src.forum.xenforo import forum_login

    if not crypto_available():
        return False, "SECRET_KEY не настроен — шифрование недоступно"

    account = await get_account(account_id)
    if not account:
        return False, "аккаунт не найден"

    encrypted = await get_account_encrypted_password(account_id)
    if not encrypted:
        return False, "нет сохранённого пароля (нужен ручной /login)"

    password = decrypt(encrypted)
    if not password:
        return False, "не удалось расшифровать пароль (SECRET_KEY изменился?)"

    login_value = account.get("login") or account.get("username")
    if not login_value:
        return False, "не сохранён логин/email"

    logger.info("Авто-перелогин для аккаунта «%s» (id=%s)...",
                account.get("username"), account_id)
    result = await forum_login(login_value, password)

    if result["status"] == "ok":
        await update_account_cookies(account_id, result["cookies"])
        logger.info("Авто-перелогин «%s» успешен — куки обновлены.",
                    account.get("username"))
        return True, "куки обновлены"

    if result["status"] == "2fa":
        # Закрываем httpx-клиент чтобы не утекал
        client = result.get("client")
        if client:
            try:
                await client.aclose()
            except Exception:
                pass
        return False, ("форум требует 2FA — авто-перелогин невозможен, "
                       "войдите вручную через /login")

    # status == "error"
    return False, result.get("message", "неизвестная ошибка")


async def relogin_all_with_passwords() -> tuple[int, int]:
    """Перебирает все аккаунты с сохранёнными паролями и проверяет/обновляет
    их сессии. Если активная сессия валидна — пропускает.

    Возвращает (relogined, failed).
    """
    from src.database import list_accounts_with_passwords
    from src.forum.xenforo import _check_auth_with_cookies

    relogined = 0
    failed = 0

    accounts = await list_accounts_with_passwords()
    if not accounts:
        logger.debug("Авто-перелогин: нет аккаунтов с сохранёнными паролями.")
        return 0, 0

    logger.info("Авто-перелогин: обхожу %d аккаунт(ов) с сохранёнными паролями.",
                len(accounts))

    for acc in accounts:
        # Сначала проверяем — живая ли сессия. Если да, пропускаем.
        ok, _ = await _check_auth_with_cookies(acc["cookies"])
        if ok:
            logger.debug("Аккаунт «%s» (id=%s) — куки ещё живы, пропускаю.",
                         acc["username"], acc["id"])
            continue

        # Куки умерли — пробуем перелогин
        success, reason = await relogin_account(acc["id"])
        if success:
            relogined += 1
        else:
            failed += 1
            logger.warning("Авто-перелогин «%s» (id=%s) не удался: %s",
                            acc["username"], acc["id"], reason)
        # Между аккаунтами небольшая пауза, чтобы DDoS-Guard не блокнул
        await asyncio.sleep(2.0)

    logger.info("Авто-перелогин завершён: успешно %d, не удалось %d.",
                relogined, failed)
    return relogined, failed


async def auto_relogin_loop() -> None:
    """Фоновая задача: каждые 12 часов перебирает все аккаунты с сохранёнными
    паролями и обновляет умершие сессии."""
    logger.info("Запущен фоновый цикл авто-перелогина "
                "(раз в %d часов).", RELOGIN_LOOP_INTERVAL // 3600)
    # Стартовая задержка, чтобы не тыкаться в форум сразу при перезапуске
    await asyncio.sleep(60)
    while True:
        try:
            await relogin_all_with_passwords()
        except asyncio.CancelledError:
            logger.info("Авто-перелогин остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка в цикле авто-перелогина, продолжаю.")
        await asyncio.sleep(RELOGIN_LOOP_INTERVAL)
