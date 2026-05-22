import os
from pathlib import Path
from dotenv import load_dotenv

# Определение базовой папки проекта
BASE_DIR = Path(__file__).resolve().parent.parent

# Загрузка переменных окружения из .env
load_dotenv(dotenv_path=BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
FORUM_URL = os.getenv("FORUM_URL", "https://forum.blackrussia.online").rstrip("/")
DB_PATH = BASE_DIR / os.getenv("DB_PATH", "bot_database.db")
COOKIES_PATH = BASE_DIR / "cookies.json"

# ID раздела форума по умолчанию (используется только как fallback)
DEFAULT_FORUM_SECTION_ID = int(os.getenv("DEFAULT_FORUM_SECTION_ID", "0"))

def _parse_admin_ids(raw: str | None) -> list[int]:
    """Парсит ADMIN_IDS из строки переменной окружения. Принимает разделители
    `,` `;` `space`, игнорирует кавычки и BOM, оставляет только цифровые id."""
    if not raw:
        return []
    # Убираем кавычки/BOM/прочую невидимую дрянь, которой богаты UI-формы хостингов
    cleaned = raw.replace("\ufeff", "").replace("'", "").replace('"', "").strip()
    # Разбиваем по любому из разделителей
    import re as _re
    parts = _re.split(r"[,;\s]+", cleaned)
    result: list[int] = []
    for p in parts:
        p = p.strip()
        if p.isdigit():
            result.append(int(p))
    return result


# Список разрешенных Telegram ID для администрирования/пользования ботом
_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = _parse_admin_ids(_ADMIN_IDS_RAW)

# User-Agent для прохождения защиты DDoS-Guard (должен совпадать с браузером пользователя)
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в файле .env!")

# Сервера форума и подкатегории жалоб больше не хранятся в коде —
# они автоматически синхронизируются с форума командой /sync и сохраняются
# в SQLite-базу (см. src/database.py и src/forum/xenforo.py).

# Понятные подписи для коротких ключей категорий жалоб
COMPLAINT_CATEGORY_LABELS: dict[str, str] = {
    "players": "🎮 Жалоба на игрока",
    "admins":  "🛡 Жалоба на администрацию",
    "leaders": "👑 Жалоба на лидера",
    "appeals": "⚖️ Обжалование наказания",
}
