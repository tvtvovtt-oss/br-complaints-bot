"""Анонимная загрузка изображений на хостинг.

postimages.org теперь блокирует автоматизированные загрузки, поэтому используем
imgbb.com — у них бесплатный API ключ (получить можно за минуту на
https://api.imgbb.com/, нужна только почта).

Если IMGBB_API_KEY не задан — функция возвращает ошибку и хендлер просит
пользователя вставить ссылку вручную.
"""
import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"


def has_uploader() -> bool:
    """True если загрузка картинок настроена и доступна."""
    return bool(os.getenv("IMGBB_API_KEY"))


async def upload_image(
    image_bytes: bytes,
    filename: str = "image.jpg",
    timeout: float = 60.0,
    expiration_seconds: Optional[int] = None,
) -> tuple[bool, str]:
    """Загружает картинку на imgbb.com.

    Возвращает (True, прямая_ссылка_на_изображение) или (False, текст_ошибки).

    expiration_seconds — опциональный TTL картинки (60..15552000). По умолчанию
    картинка хранится бессрочно.
    """
    api_key = os.getenv("IMGBB_API_KEY")
    if not api_key:
        return False, ("автозагрузка отключена: не задан IMGBB_API_KEY "
                       "в переменных окружения")

    encoded = base64.b64encode(image_bytes).decode("ascii")

    params = {"key": api_key}
    if expiration_seconds is not None:
        params["expiration"] = str(expiration_seconds)

    data = {"image": encoded, "name": filename}

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(IMGBB_UPLOAD_URL, params=params, data=data)
            try:
                rj = r.json()
            except ValueError:
                snippet = r.text[:200].replace("\n", " ")
                return False, f"imgbb вернул не-JSON: {snippet}"

            if r.status_code != 200 or not rj.get("success"):
                err = rj.get("error", {})
                msg = err.get("message") if isinstance(err, dict) else str(err)
                logger.warning("imgbb upload отказал HTTP %s: %s",
                               r.status_code, msg or rj)
                return False, f"imgbb отказал: {msg or 'неизвестная ошибка'}"

            data = rj.get("data", {})
            # Прямая ссылка на изображение (для пруфа на форум)
            url = data.get("url") or data.get("display_url") or data.get("image", {}).get("url")
            if not url:
                return False, "imgbb не вернул url"

            logger.info("Загружено на imgbb: %s (%d байт)", url, len(image_bytes))
            return True, url

        except httpx.RequestError as e:
            logger.error("imgbb: сетевая ошибка: %s", e)
            return False, f"ошибка сети: {e}"
        except Exception as e:
            logger.exception("imgbb upload failed")
            return False, f"ошибка: {e}"
