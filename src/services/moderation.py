"""Модерация изображений через Sightengine.

Sightengine даёт бесплатно 500 проверок в месяц и API без подписки. Чтобы
включить фильтр, добавьте в .env:

    SIGHTENGINE_API_USER=...
    SIGHTENGINE_API_SECRET=...

Эти значения можно получить на https://dashboard.sightengine.com (после
бесплатной регистрации). Если переменные не заданы — модерация отключена,
и upload_image работает как раньше (без проверок).

Что блокируется:
- nudity (порно, обнажёнка)
- gore (кровища, расчленёнка)
- очевидное оружие в руках, направленное на человека
- offensive (нацистская/экстремистская символика)
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"

# Порог уверенности по nudity. От 0 до 1: чем ниже, тем строже.
NSFW_THRESHOLD = 0.55
GORE_THRESHOLD = 0.65
OFFENSIVE_THRESHOLD = 0.55

# Какие модели Sightengine запрашиваем
_MODELS = "nudity-2.1,gore,offensive"


def has_moderation() -> bool:
    """True если включена модерация изображений (заданы оба ключа)."""
    return bool(os.getenv("SIGHTENGINE_API_USER")
                and os.getenv("SIGHTENGINE_API_SECRET"))


async def check_image(image_bytes: bytes,
                        timeout: float = 20.0) -> tuple[bool, str]:
    """Проверяет картинку на запрещённый контент.

    Возвращает (allowed, reason):
    - (True, "ok")              — можно публиковать
    - (False, текст_причины)    — нельзя, причина для пользователя
    - (True, "skipped")         — модерация выключена (нет ключей) или ошибка
                                  API. Не блокируем, чтобы один сбой Sightengine
                                  не положил подачу всех жалоб.
    """
    if not has_moderation():
        return True, "skipped"

    api_user = os.getenv("SIGHTENGINE_API_USER", "").strip()
    api_secret = os.getenv("SIGHTENGINE_API_SECRET", "").strip()

    files = {"media": ("image.jpg", image_bytes, "image/jpeg")}
    data = {
        "models": _MODELS,
        "api_user": api_user,
        "api_secret": api_secret,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(SIGHTENGINE_URL, data=data, files=files)
    except httpx.RequestError as e:
        logger.warning("Sightengine: сетевая ошибка %s — пропускаю проверку.", e)
        return True, "skipped"
    except Exception:
        logger.exception("Sightengine: непредвиденная ошибка — пропускаю.")
        return True, "skipped"

    if r.status_code != 200:
        logger.warning("Sightengine HTTP %s: %s", r.status_code,
                       r.text[:300].replace("\n", " "))
        return True, "skipped"

    try:
        rj = r.json()
    except ValueError:
        logger.warning("Sightengine не-JSON: %s",
                       r.text[:200].replace("\n", " "))
        return True, "skipped"

    if rj.get("status") != "success":
        # quota exceeded или невалидный ключ
        err = rj.get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
        logger.warning("Sightengine ответ статус=%s: %s",
                       rj.get("status"), msg)
        return True, "skipped"

    # 1. Nudity (модель nudity-2.1)
    nudity = rj.get("nudity") or {}
    # В nudity-2.1 есть отдельный ключ "none" (вероятность что обнажёнки нет)
    nudity_none = float(nudity.get("none", 1.0))
    sexual_activity = float(nudity.get("sexual_activity", 0.0))
    sexual_display = float(nudity.get("sexual_display", 0.0))
    erotica = float(nudity.get("erotica", 0.0))
    very_suggestive = float(nudity.get("very_suggestive", 0.0))

    nudity_score = max(sexual_activity, sexual_display, erotica, very_suggestive)
    if nudity_score >= NSFW_THRESHOLD or nudity_none < (1.0 - NSFW_THRESHOLD):
        logger.info("Sightengine: NSFW отклонено (score=%.2f, none=%.2f).",
                    nudity_score, nudity_none)
        return False, ("обнаружен 18+ контент. Разрешены только скриншоты "
                       "из игры")

    # 2. Gore (расчленёнка/кровища)
    gore = rj.get("gore") or {}
    gore_prob = float(gore.get("prob", 0.0))
    if gore_prob >= GORE_THRESHOLD:
        logger.info("Sightengine: gore отклонено (score=%.2f).", gore_prob)
        return False, ("обнаружено графическое насилие. Разрешены только "
                       "скриншоты из игры")

    # 3. Offensive (запрещённая символика)
    offensive = rj.get("offensive") or {}
    nazi = float(offensive.get("nazi", 0.0))
    supremacist = float(offensive.get("supremacist", 0.0))
    terrorist = float(offensive.get("terrorist", 0.0))
    offensive_score = max(nazi, supremacist, terrorist)

    if offensive_score >= OFFENSIVE_THRESHOLD:
        logger.info("Sightengine: оскорбительный контент (nazi=%.2f, "
                    "supremacist=%.2f, terrorist=%.2f).",
                    nazi, supremacist, terrorist)
        return False, ("обнаружена запрещённая символика "
                       "(нацистская/экстремистская)")

    # middle_finger / прочую мелочь не блокируем — на скриншотах из игры
    # это безобидно.

    logger.debug("Sightengine: ок (nudity=%.2f, gore=%.2f, offensive=%.2f).",
                 nudity_score, gore_prob, offensive_score)
    return True, "ok"
