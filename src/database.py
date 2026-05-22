import logging
import aiosqlite
from src.config import DB_PATH

logger = logging.getLogger(__name__)


async def init_db():
    """Инициализация базы данных и создание таблиц."""
    logger.info("Инициализирую SQLite-базу данных: %s", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        # История поданных жалоб
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                nickname TEXT NOT NULL,
                description TEXT NOT NULL,
                proof_link TEXT NOT NULL,
                forum_thread_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                last_status_check TIMESTAMP,
                notified_status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Миграции для существующих БД
        async with db.execute("PRAGMA table_info(complaints)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        for col, ddl in [
            ("status", "ALTER TABLE complaints ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"),
            ("last_status_check", "ALTER TABLE complaints ADD COLUMN last_status_check TIMESTAMP"),
            ("notified_status", "ALTER TABLE complaints ADD COLUMN notified_status TEXT"),
        ]:
            if col not in cols:
                logger.info("Миграция: добавляю колонку '%s' в complaints.", col)
                await db.execute(ddl)

        # Кэш серверов форума (RED, GREEN и т.д.)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                name TEXT PRIMARY KEY,
                node_id INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Совместимость со старыми БД: добавляем колонку position, если ее нет
        async with db.execute("PRAGMA table_info(servers)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if "position" not in cols:
            logger.info("Миграция: добавляю колонку 'position' в таблицу servers.")
            await db.execute("ALTER TABLE servers ADD COLUMN position INTEGER NOT NULL DEFAULT 0")

        # Кэш подразделов жалоб для каждого сервера
        # category_key — короткий ключ (players / admins / leaders / appeals)
        # category_name — оригинальное название с форума
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaint_categories (
                server_node_id INTEGER NOT NULL,
                category_key TEXT NOT NULL,
                category_name TEXT NOT NULL,
                category_node_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (server_node_id, category_key)
            )
        """)

        # Форумные аккаунты пользователя бота. Каждый Telegram-юзер может
        # иметь несколько аккаунтов на форуме; ровно один помечен как
        # активный (is_active=1) — его куки используются для всех запросов.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                login TEXT,
                cookies_json TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                cooldown_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id, username)
            )
        """)
        # Миграция: добавляем cooldown_until если её ещё нет (старые БД)
        async with db.execute("PRAGMA table_info(accounts)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if "cooldown_until" not in cols:
            logger.info("Миграция: добавляю колонку 'cooldown_until' в accounts.")
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN cooldown_until TIMESTAMP"
            )
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_accounts_active
                ON accounts(telegram_id, is_active)
        """)

        # Пользовательские шаблоны жалоб (привязаны к telegram_id юзера)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                category_key TEXT NOT NULL,
                name TEXT NOT NULL,
                summary TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_templates_owner
                ON user_templates(telegram_id, category_key)
        """)
        await db.commit()
    logger.info("База данных готова. Таблицы: complaints, servers, "
                "complaint_categories, accounts.")


# ---------- Жалобы ----------

async def add_complaint(telegram_id: int, nickname: str, description: str, proof_link: str, forum_thread_url: str = None) -> int:
    """Добавление записи о поданной жалобе. Возвращает id записи."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO complaints (telegram_id, nickname, description, proof_link, forum_thread_url, status, notified_status)
            VALUES (?, ?, ?, ?, ?, 'pending', 'pending')
            """,
            (telegram_id, nickname, description, proof_link, forum_thread_url)
        )
        complaint_id = cur.lastrowid
        await db.commit()
    logger.info("Сохранил жалобу в БД: id=%s, telegram_id=%s, цель=«%s», ссылка: %s",
                complaint_id, telegram_id, nickname, forum_thread_url or "—")
    return complaint_id


async def get_user_complaints(telegram_id: int):
    """Получение истории жалоб конкретного пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, nickname, description, proof_link, forum_thread_url, "
            "status, created_at "
            "FROM complaints WHERE telegram_id = ? ORDER BY id DESC",
            (telegram_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "nickname": row[1],
                    "description": row[2],
                    "proof_link": row[3],
                    "forum_thread_url": row[4],
                    "status": row[5] or "pending",
                    "created_at": row[6],
                }
                for row in rows
            ]


async def get_complaint(complaint_id: int) -> dict | None:
    """Возвращает жалобу по id (или None)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, description, proof_link, "
            "forum_thread_url, status, notified_status, created_at "
            "FROM complaints WHERE id = ?",
            (complaint_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "telegram_id": row[1],
                "nickname": row[2],
                "description": row[3],
                "proof_link": row[4],
                "forum_thread_url": row[5],
                "status": row[6] or "pending",
                "notified_status": row[7],
                "created_at": row[8],
            }


async def list_complaints_for_status_check() -> list[dict]:
    """Возвращает все жалобы с forum_thread_url, у которых статус ещё
    не финальный — нужно проверить состояние на форуме."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, forum_thread_url, status, notified_status "
            "FROM complaints "
            "WHERE forum_thread_url IS NOT NULL "
            "AND status IN ('pending', 'unknown')"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0], "telegram_id": r[1], "nickname": r[2],
                    "forum_thread_url": r[3],
                    "status": r[4] or "pending",
                    "notified_status": r[5],
                }
                for r in rows
            ]


async def update_complaint_status(complaint_id: int, status: str) -> None:
    """Обновляет статус жалобы и время последней проверки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE complaints SET status = ?, "
            "last_status_check = CURRENT_TIMESTAMP WHERE id = ?",
            (status, complaint_id),
        )
        await db.commit()


async def mark_complaint_notified(complaint_id: int, status: str) -> None:
    """Помечает что пользователь уведомлён об этом статусе."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE complaints SET notified_status = ? WHERE id = ?",
            (status, complaint_id),
        )
        await db.commit()


async def delete_complaint(telegram_id: int, complaint_id: int) -> bool:
    """Удаляет жалобу из истории пользователя (только из БД, тему на форуме
    не трогаем). Возвращает True если успешно."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM complaints WHERE id = ? AND telegram_id = ?",
            (complaint_id, telegram_id),
        )
        await db.commit()
        deleted = cur.rowcount > 0
    if deleted:
        logger.info("Удалена жалоба id=%s (telegram_id=%s).",
                    complaint_id, telegram_id)
    return deleted


# ---------- Серверы ----------

async def save_servers(servers: list[tuple[str, int]]):
    """Перезапись кэша серверов. Принимает список [(name, node_id), ...]
    в том порядке, в котором они идут на форуме."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM servers")
        await db.executemany(
            "INSERT INTO servers (name, node_id, position) VALUES (?, ?, ?)",
            [(name, node_id, idx) for idx, (name, node_id) in enumerate(servers)],
        )
        await db.commit()
    logger.info("Кэш серверов обновлён: %d записей.", len(servers))


async def get_servers() -> list[tuple[str, int]]:
    """Возвращает список [(name, node_id), ...] в порядке с форума."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, node_id FROM servers ORDER BY position, name"
        ) as cursor:
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]


# ---------- Категории жалоб ----------

async def save_complaint_categories(server_node_id: int, categories: dict[str, tuple[str, int]]):
    """Сохранение категорий жалоб для сервера.
    categories: {category_key: (category_name, category_node_id)}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM complaint_categories WHERE server_node_id = ?",
            (server_node_id,),
        )
        await db.executemany(
            """
            INSERT INTO complaint_categories
                (server_node_id, category_key, category_name, category_node_id)
            VALUES (?, ?, ?, ?)
            """,
            [(server_node_id, key, name, node_id) for key, (name, node_id) in categories.items()],
        )
        await db.commit()
    logger.debug("Сервер node=%s: сохранено категорий жалоб %d.",
                 server_node_id, len(categories))


async def get_complaint_categories(server_node_id: int) -> dict[str, tuple[str, int]]:
    """Возвращает {category_key: (category_name, category_node_id)} для сервера."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT category_key, category_name, category_node_id "
            "FROM complaint_categories WHERE server_node_id = ?",
            (server_node_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: (row[1], row[2]) for row in rows}


# ---------- Форумные аккаунты ----------

import json as _json


async def upsert_account(telegram_id: int, username: str, login: str | None,
                          cookies: dict, make_active: bool = True) -> int:
    """Добавляет новый аккаунт или обновляет cookies/login существующего
    (по уникальной паре telegram_id + username).

    Если make_active=True (по умолчанию), аккаунт становится активным,
    а все остальные аккаунты этого пользователя помечаются неактивными.

    Возвращает id записи в таблице accounts.
    """
    cookies_str = _json.dumps(cookies, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        # Сбрасываем активность у других аккаунтов этого пользователя
        if make_active:
            await db.execute(
                "UPDATE accounts SET is_active = 0 WHERE telegram_id = ?",
                (telegram_id,),
            )

        # UPSERT по (telegram_id, username)
        await db.execute(
            """
            INSERT INTO accounts (telegram_id, username, login, cookies_json, is_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id, username) DO UPDATE SET
                login = excluded.login,
                cookies_json = excluded.cookies_json,
                is_active = excluded.is_active,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_id, username, login, cookies_str, 1 if make_active else 0),
        )
        await db.commit()

        # Возвращаем id вставленной/обновлённой записи
        async with db.execute(
            "SELECT id FROM accounts WHERE telegram_id = ? AND username = ?",
            (telegram_id, username),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def list_accounts(telegram_id: int) -> list[dict]:
    """Возвращает список аккаунтов пользователя:
    [{id, username, login, is_active, cooldown_until, created_at, updated_at}, ...]
    Активный аккаунт идёт первым."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, username, login, is_active, cooldown_until, created_at, updated_at
            FROM accounts
            WHERE telegram_id = ?
            ORDER BY is_active DESC, updated_at DESC
            """,
            (telegram_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "username": r[1],
                    "login": r[2],
                    "is_active": bool(r[3]),
                    "cooldown_until": r[4],
                    "created_at": r[5],
                    "updated_at": r[6],
                }
                for r in rows
            ]


async def get_active_account(telegram_id: int) -> dict | None:
    """Возвращает активный аккаунт пользователя со всеми полями включая cookies,
    либо None если аккаунтов нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, username, login, cookies_json, is_active,
                   cooldown_until, created_at, updated_at
            FROM accounts
            WHERE telegram_id = ? AND is_active = 1
            LIMIT 1
            """,
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "username": row[1],
                "login": row[2],
                "cookies": _json.loads(row[3]),
                "is_active": bool(row[4]),
                "cooldown_until": row[5],
                "created_at": row[6],
                "updated_at": row[7],
            }


async def get_account(account_id: int) -> dict | None:
    """Возвращает аккаунт по id."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, telegram_id, username, login, cookies_json, is_active,
                   cooldown_until
            FROM accounts WHERE id = ?
            """,
            (account_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "telegram_id": row[1],
                "username": row[2],
                "login": row[3],
                "cookies": _json.loads(row[4]),
                "is_active": bool(row[5]),
                "cooldown_until": row[6],
            }


async def set_active_account(telegram_id: int, account_id: int) -> bool:
    """Делает аккаунт активным. Возвращает True если успешно."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM accounts WHERE id = ? AND telegram_id = ?",
            (account_id, telegram_id),
        ) as cur:
            if not await cur.fetchone():
                return False
        await db.execute(
            "UPDATE accounts SET is_active = 0 WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.execute(
            "UPDATE accounts SET is_active = 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (account_id,),
        )
        await db.commit()
    logger.info("Активный аккаунт telegram_id=%s переключён на account_id=%s.",
                telegram_id, account_id)
    return True


async def delete_account(telegram_id: int, account_id: int) -> bool:
    """Удаляет аккаунт. Если он был активным, активным становится
    самый недавно использованный из оставшихся (если такие есть)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_active FROM accounts WHERE id = ? AND telegram_id = ?",
            (account_id, telegram_id),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            was_active = bool(row[0])

        await db.execute(
            "DELETE FROM accounts WHERE id = ? AND telegram_id = ?",
            (account_id, telegram_id),
        )

        # Если удалили активный — активируем самый свежий из оставшихся
        if was_active:
            async with db.execute(
                """
                SELECT id FROM accounts
                WHERE telegram_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (telegram_id,),
            ) as cur:
                next_row = await cur.fetchone()
            if next_row:
                await db.execute(
                    "UPDATE accounts SET is_active = 1 WHERE id = ?",
                    (next_row[0],),
                )

        await db.commit()
    logger.info("Удалён аккаунт account_id=%s (telegram_id=%s).",
                account_id, telegram_id)
    return True


async def update_account_cookies(account_id: int, cookies: dict) -> None:
    """Обновляет куки аккаунта (после реауторизации)."""
    cookies_str = _json.dumps(cookies, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET cookies_json = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (cookies_str, account_id),
        )
        await db.commit()


async def set_account_cooldown(account_id: int, seconds: int) -> None:
    """Ставит кулдаун на аккаунт: cooldown_until = now + seconds.
    После этой метки аккаунт снова можно использовать."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts "
            "SET cooldown_until = datetime('now', ? || ' seconds') "
            "WHERE id = ?",
            (f"+{int(seconds)}", account_id),
        )
        await db.commit()
    logger.info("Аккаунт id=%s ушёл в кулдаун на %d сек.", account_id, seconds)


async def find_available_account(telegram_id: int) -> dict | None:
    """Возвращает аккаунт пользователя, у которого кулдаун уже закончился
    (или его не было), упорядочивая по самой ранней `updated_at` —
    чтобы заявки распределялись равномерно по аккаунтам.

    Если все аккаунты в кулдауне — возвращает тот, у которого ближайшее
    окончание кулдауна (с полем cooldown_remaining_seconds).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Сначала ищем свободный (cooldown_until IS NULL ИЛИ уже истёк)
        async with db.execute(
            """
            SELECT id, username, login, cookies_json, cooldown_until,
                   CAST((julianday(cooldown_until) - julianday('now')) * 86400 AS INTEGER)
                   AS remaining
            FROM accounts
            WHERE telegram_id = ?
              AND (cooldown_until IS NULL
                   OR cooldown_until <= datetime('now'))
            ORDER BY updated_at ASC
            LIMIT 1
            """,
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "id": row[0],
                    "username": row[1],
                    "login": row[2],
                    "cookies": _json.loads(row[3]),
                    "cooldown_until": row[4],
                    "cooldown_remaining_seconds": 0,
                    "available": True,
                }

        # Все аккаунты в кулдауне — возвращаем с минимальным остатком
        async with db.execute(
            """
            SELECT id, username, login, cookies_json, cooldown_until,
                   CAST((julianday(cooldown_until) - julianday('now')) * 86400 AS INTEGER)
                   AS remaining
            FROM accounts
            WHERE telegram_id = ?
            ORDER BY cooldown_until ASC
            LIMIT 1
            """,
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "username": row[1],
                "login": row[2],
                "cookies": _json.loads(row[3]),
                "cooldown_until": row[4],
                "cooldown_remaining_seconds": max(0, row[5] or 0),
                "available": False,
            }


# ---------- Пользовательские шаблоны жалоб ----------

async def add_user_template(telegram_id: int, category_key: str,
                             name: str, summary: str, description: str) -> int:
    """Сохраняет пользовательский шаблон. Возвращает id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO user_templates
                (telegram_id, category_key, name, summary, description)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, category_key, name, summary, description),
        )
        tid = cur.lastrowid
        await db.commit()
    logger.info("Сохранил пользовательский шаблон id=%s (telegram_id=%s, "
                "категория=%s, имя=«%s»).",
                tid, telegram_id, category_key, name)
    return tid


async def list_user_templates(telegram_id: int, category_key: str) -> list[dict]:
    """Список пользовательских шаблонов для категории."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, summary, description "
            "FROM user_templates WHERE telegram_id = ? AND category_key = ? "
            "ORDER BY id DESC",
            (telegram_id, category_key),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"id": r[0], "name": r[1], "summary": r[2], "description": r[3]}
                for r in rows
            ]


async def get_user_template(template_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, category_key, name, summary, description "
            "FROM user_templates WHERE id = ?",
            (template_id,),
        ) as cur:
            r = await cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0], "telegram_id": r[1], "category_key": r[2],
                "name": r[3], "summary": r[4], "description": r[5],
            }


async def delete_user_template(telegram_id: int, template_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM user_templates WHERE id = ? AND telegram_id = ?",
            (template_id, telegram_id),
        )
        await db.commit()
        return cur.rowcount > 0
