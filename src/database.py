import logging
import aiosqlite
from src.config import DB_PATH

logger = logging.getLogger(__name__)


# Триггер фонового бэкапа в Telegram-канал. Импортируется лениво, чтобы
# избежать циклического импорта (storage_backup → aiogram → ...).
async def _trigger_backup() -> None:
    try:
        from src.storage_backup import schedule_backup
        await schedule_backup()
    except Exception:
        # Бэкап не должен ломать основные операции — глушим всё
        logger.debug("trigger_backup failed", exc_info=True)


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
                account_id INTEGER,
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
            ("account_id", "ALTER TABLE complaints ADD COLUMN account_id INTEGER"),
            ("your_nickname", "ALTER TABLE complaints ADD COLUMN your_nickname TEXT"),
            ("summary", "ALTER TABLE complaints ADD COLUMN summary TEXT"),
            ("category_key", "ALTER TABLE complaints ADD COLUMN category_key TEXT"),
            ("punishment_date", "ALTER TABLE complaints ADD COLUMN punishment_date TEXT"),
            ("server_node_id", "ALTER TABLE complaints ADD COLUMN server_node_id INTEGER"),
            ("server_name", "ALTER TABLE complaints ADD COLUMN server_name TEXT"),
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
        if "encrypted_password" not in cols:
            logger.info("Миграция: добавляю колонку 'encrypted_password' в accounts.")
            await db.execute(
                "ALTER TABLE accounts ADD COLUMN encrypted_password TEXT"
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

        # Баг-репорты от пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bug_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                text TEXT NOT NULL,
                photo_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                admin_reply TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                replied_at TIMESTAMP
            )
        """)

        # Очередь жалоб для отложенной публикации
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaint_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                section_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                bb_code TEXT NOT NULL,
                target_nickname TEXT NOT NULL,
                description TEXT NOT NULL,
                proof_link TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                forum_thread_url TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_pending
                ON complaint_queue(status, created_at)
        """)

        # Черновики жалоб — снимок FSM-состояния, чтобы пользователь мог
        # вернуться к незаконченной жалобе после рестарта бота.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                telegram_id INTEGER PRIMARY KEY,
                state_data TEXT NOT NULL,
                step TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    logger.info("База данных готова. Таблицы: complaints, servers, "
                "complaint_categories, accounts.")


# ---------- Жалобы ----------

async def add_complaint(telegram_id: int, nickname: str, description: str,
                          proof_link: str, forum_thread_url: str = None,
                          account_id: int | None = None,
                          your_nickname: str | None = None,
                          summary: str | None = None,
                          category_key: str | None = None,
                          punishment_date: str | None = None,
                          server_node_id: int | None = None,
                          server_name: str | None = None) -> int:
    """Добавление записи о поданной жалобе. Возвращает id записи.

    Дополнительные поля (`your_nickname`, `summary`, `category_key`,
    `punishment_date`, `server_*`) нужны чтобы при редактировании жалобы
    можно было пересобрать корректный BB-код и заголовок, а также для
    статистики по серверам.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO complaints
                (telegram_id, nickname, description, proof_link,
                 forum_thread_url, status, notified_status, account_id,
                 your_nickname, summary, category_key, punishment_date,
                 server_node_id, server_name)
            VALUES (?, ?, ?, ?, ?, 'pending', 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, nickname, description, proof_link,
             forum_thread_url, account_id, your_nickname, summary,
             category_key, punishment_date, server_node_id, server_name),
        )
        complaint_id = cur.lastrowid
        await db.commit()
    logger.info("Сохранил жалобу в БД: id=%s, telegram_id=%s, цель=«%s», "
                "сервер=«%s», account_id=%s, ссылка: %s",
                complaint_id, telegram_id, nickname, server_name or "—",
                account_id, forum_thread_url or "—")
    await _trigger_backup()
    return complaint_id


async def get_user_complaints(telegram_id: int):
    """Получение истории жалоб конкретного пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, nickname, description, proof_link, forum_thread_url, "
            "status, created_at, summary "
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
                    "summary": row[7],
                }
                for row in rows
            ]


async def get_complaint(complaint_id: int) -> dict | None:
    """Возвращает жалобу по id (или None)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, description, proof_link, "
            "forum_thread_url, status, notified_status, created_at, "
            "account_id, your_nickname, summary, category_key, punishment_date "
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
                "account_id": row[9],
                "your_nickname": row[10],
                "summary": row[11],
                "category_key": row[12],
                "punishment_date": row[13],
            }


async def list_complaints_for_status_check() -> list[dict]:
    """Возвращает все жалобы с forum_thread_url, у которых статус ещё
    не финальный — нужно проверить состояние на форуме."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, forum_thread_url, status, "
            "notified_status, account_id "
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
                    "account_id": r[6],
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

    Атомарно через BEGIN IMMEDIATE — без транзакции при одновременных
    логинах могло бы получиться, что is_active у всех = 0.

    Возвращает id записи в таблице accounts.
    """
    cookies_str = _json.dumps(cookies, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            if make_active:
                await db.execute(
                    "UPDATE accounts SET is_active = 0 WHERE telegram_id = ?",
                    (telegram_id,),
                )

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
        except Exception:
            await db.rollback()
            raise

        async with db.execute(
            "SELECT id FROM accounts WHERE telegram_id = ? AND username = ?",
            (telegram_id, username),
        ) as cur:
            row = await cur.fetchone()
            account_id = row[0] if row else 0
    await _trigger_backup()
    return account_id


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

    Использует BEGIN IMMEDIATE чтобы исключить гонку при параллельной
    подаче нескольких жалоб разными пользователями.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Эксклюзивная транзакция — гарантирует, что между SELECT и решением
        # не вмешается другой коннект и не выберет тот же аккаунт.
        await db.execute("BEGIN IMMEDIATE")
        try:
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
                    result = {
                        "id": row[0],
                        "username": row[1],
                        "login": row[2],
                        "cookies": _json.loads(row[3]),
                        "cooldown_until": row[4],
                        "cooldown_remaining_seconds": 0,
                        "available": True,
                    }
                    await db.commit()
                    return result

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

            await db.commit()
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
        except Exception:
            await db.rollback()
            raise


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


# ---------- Баг-репорты ----------

async def add_bug_report(telegram_id: int, username: str | None,
                          full_name: str | None, text: str,
                          photo_file_id: str | None = None) -> int:
    """Сохраняет баг-репорт. Возвращает id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO bug_reports
                (telegram_id, username, full_name, text, photo_file_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, username, full_name, text, photo_file_id),
        )
        bid = cur.lastrowid
        await db.commit()
    logger.info("Баг-репорт #%s от telegram_id=%s сохранён.", bid, telegram_id)
    return bid


async def get_bug_report(report_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, telegram_id, username, full_name, text, photo_file_id,
                   status, admin_reply, created_at, replied_at
            FROM bug_reports WHERE id = ?
            """,
            (report_id,),
        ) as cur:
            r = await cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0], "telegram_id": r[1], "username": r[2],
                "full_name": r[3], "text": r[4], "photo_file_id": r[5],
                "status": r[6], "admin_reply": r[7],
                "created_at": r[8], "replied_at": r[9],
            }


async def list_bug_reports(only_open: bool = False, limit: int = 20) -> list[dict]:
    """Возвращает последние N баг-репортов. only_open — только в статусах
    new/in_progress."""
    where = "WHERE status IN ('new', 'in_progress')" if only_open else ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT id, telegram_id, username, full_name, text, status, created_at
            FROM bug_reports
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0], "telegram_id": r[1], "username": r[2],
                    "full_name": r[3], "text": r[4], "status": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]


async def set_bug_report_status(report_id: int, status: str,
                                  admin_reply: str | None = None) -> None:
    """Обновляет статус и опционально текст ответа админа."""
    async with aiosqlite.connect(DB_PATH) as db:
        if admin_reply is not None:
            await db.execute(
                "UPDATE bug_reports SET status = ?, admin_reply = ?, "
                "replied_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, admin_reply, report_id),
            )
        else:
            await db.execute(
                "UPDATE bug_reports SET status = ? WHERE id = ?",
                (status, report_id),
            )
        await db.commit()
    logger.info("Баг-репорт #%s: статус → %s", report_id, status)


async def recent_complaint_against(telegram_id: int, target_nickname: str,
                                     within_minutes: int = 30) -> dict | None:
    """Возвращает последнюю жалобу пользователя на тот же ник, поданную не
    более `within_minutes` минут назад. Используется для антиспама — нельзя
    жаловаться на одного и того же игрока дважды подряд."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, created_at, forum_thread_url
            FROM complaints
            WHERE telegram_id = ?
              AND LOWER(nickname) = LOWER(?)
              AND created_at >= datetime('now', ? || ' minutes')
            ORDER BY id DESC LIMIT 1
            """,
            (telegram_id, target_nickname, f"-{int(within_minutes)}"),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "created_at": row[1],
                    "forum_thread_url": row[2]}


async def count_recent_bug_reports(telegram_id: int, within_minutes: int) -> int:
    """Сколько баг-репортов от пользователя за последние N минут."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM bug_reports
            WHERE telegram_id = ?
              AND created_at >= datetime('now', ? || ' minutes')
            """,
            (telegram_id, f"-{int(within_minutes)}"),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def update_complaint_content(complaint_id: int, telegram_id: int,
                                     description: str | None = None,
                                     proof_link: str | None = None) -> bool:
    """Обновляет описание и/или доказательства локальной записи о жалобе.

    Имена столбцов — фиксированные литералы, без склеивания через f-строку,
    чтобы исключить даже теоретическую возможность SQL-инъекции при будущих
    изменениях кода.
    """
    if description is None and proof_link is None:
        return False

    if description is not None and proof_link is not None:
        sql = ("UPDATE complaints SET description = ?, proof_link = ? "
               "WHERE id = ? AND telegram_id = ?")
        params: tuple = (description, proof_link, complaint_id, telegram_id)
    elif description is not None:
        sql = "UPDATE complaints SET description = ? WHERE id = ? AND telegram_id = ?"
        params = (description, complaint_id, telegram_id)
    else:
        sql = "UPDATE complaints SET proof_link = ? WHERE id = ? AND telegram_id = ?"
        params = (proof_link, complaint_id, telegram_id)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount > 0


async def claim_available_account(telegram_id: int,
                                    cooldown_seconds: int) -> dict | None:
    """Атомарно «забронировать» свободный аккаунт под публикацию.

    UPDATE с RETURNING делает выбор и постановку кулдауна одним запросом,
    исключая гонку: даже если две жалобы запустятся в одну миллисекунду,
    каждая получит свой аккаунт (или None если свободных не осталось).

    Если свободных нет — возвращает None и НЕ ставит кулдаун.

    SQLite 3.35+ требуется для RETURNING.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """
                UPDATE accounts
                SET cooldown_until = datetime('now', ? || ' seconds'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM accounts
                    WHERE telegram_id = ?
                      AND (cooldown_until IS NULL
                           OR cooldown_until <= datetime('now'))
                    ORDER BY updated_at ASC
                    LIMIT 1
                )
                RETURNING id, username, login, cookies_json
                """,
                (f"+{int(cooldown_seconds)}", telegram_id),
            ) as cur:
                row = await cur.fetchone()
            await db.commit()
            if not row:
                return None
            logger.info("Аккаунт id=%s «забронирован» с кулдауном %d с.",
                        row[0], cooldown_seconds)
            return {
                "id": row[0],
                "username": row[1],
                "login": row[2],
                "cookies": _json.loads(row[3]),
                "available": True,
            }
        except Exception:
            await db.rollback()
            raise


async def release_account_cooldown(account_id: int) -> None:
    """Сбрасывает кулдаун аккаунта (если публикация провалилась —
    возвращаем аккаунт в пул сразу, не ждём 180 сек)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET cooldown_until = NULL WHERE id = ?",
            (account_id,),
        )
        await db.commit()
    logger.info("Кулдаун аккаунта id=%s сброшен.", account_id)


# ---------- Шифрованный пароль для авто-перелогина ----------

async def set_account_encrypted_password(account_id: int,
                                           encrypted_password: str | None) -> None:
    """Сохраняет/удаляет зашифрованный пароль аккаунта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET encrypted_password = ? WHERE id = ?",
            (encrypted_password, account_id),
        )
        await db.commit()
    await _trigger_backup()


async def get_account_encrypted_password(account_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT encrypted_password FROM accounts WHERE id = ?",
            (account_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_accounts_with_passwords() -> list[dict]:
    """Возвращает все аккаунты у которых есть encrypted_password.
    Используется в авто-перелогине."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, telegram_id, username, login, cookies_json,
                   encrypted_password
            FROM accounts
            WHERE encrypted_password IS NOT NULL AND encrypted_password != ''
            """
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0], "telegram_id": r[1], "username": r[2],
                    "login": r[3], "cookies": _json.loads(r[4]),
                    "encrypted_password": r[5],
                }
                for r in rows
            ]


# ---------- Очередь жалоб (отложенная публикация) ----------

async def enqueue_complaint(telegram_id: int, section_id: int, title: str,
                              bb_code: str, target_nickname: str,
                              description: str, proof_link: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO complaint_queue
                (telegram_id, section_id, title, bb_code,
                 target_nickname, description, proof_link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, section_id, title, bb_code,
             target_nickname, description, proof_link),
        )
        qid = cur.lastrowid
        await db.commit()
    logger.info("В очередь добавлена жалоба #%s (telegram_id=%s, цель=«%s»).",
                qid, telegram_id, target_nickname)
    await _trigger_backup()
    return qid


async def list_queue_pending() -> list[dict]:
    """Возвращает все жалобы в статусе pending, отсортированные по времени."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, telegram_id, section_id, title, bb_code,
                   target_nickname, description, proof_link,
                   attempts, created_at
            FROM complaint_queue
            WHERE status = 'pending'
            ORDER BY created_at ASC
            """
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0], "telegram_id": r[1], "section_id": r[2],
                    "title": r[3], "bb_code": r[4],
                    "target_nickname": r[5], "description": r[6],
                    "proof_link": r[7], "attempts": r[8],
                    "created_at": r[9],
                }
                for r in rows
            ]


async def list_user_queue(telegram_id: int) -> list[dict]:
    """Очередь конкретного пользователя со всеми статусами."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, section_id, target_nickname, status, last_error,
                   attempts, created_at, processed_at, forum_thread_url
            FROM complaint_queue
            WHERE telegram_id = ?
            ORDER BY id DESC LIMIT 30
            """,
            (telegram_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0], "section_id": r[1],
                    "target_nickname": r[2], "status": r[3],
                    "last_error": r[4], "attempts": r[5],
                    "created_at": r[6], "processed_at": r[7],
                    "forum_thread_url": r[8],
                }
                for r in rows
            ]


async def mark_queue_done(queue_id: int, forum_thread_url: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE complaint_queue "
            "SET status = 'done', forum_thread_url = ?, "
            "processed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (forum_thread_url, queue_id),
        )
        await db.commit()
    await _trigger_backup()


async def mark_queue_failed(queue_id: int, error: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE complaint_queue "
            "SET status = 'failed', last_error = ?, "
            "processed_at = CURRENT_TIMESTAMP, attempts = attempts + 1 "
            "WHERE id = ?",
            (error[:500], queue_id),
        )
        await db.commit()
    await _trigger_backup()


async def increment_queue_attempt(queue_id: int, error: str | None = None) -> None:
    """Увеличить счётчик попыток (для retry в pending)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE complaint_queue "
            "SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            ((error or "")[:500], queue_id),
        )
        await db.commit()


async def cancel_queue_item(telegram_id: int, queue_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM complaint_queue "
            "WHERE id = ? AND telegram_id = ? AND status = 'pending'",
            (queue_id, telegram_id),
        )
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        await _trigger_backup()
    return ok


# ---------- Все пользователи бота (для рассылок) ----------

async def list_all_users() -> list[int]:
    """Возвращает уникальные telegram_id всех, кто хоть раз взаимодействовал
    с ботом (записал жалобу, шаблон, баг-репорт, добавил аккаунт)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT telegram_id FROM complaints
            UNION SELECT telegram_id FROM user_templates
            UNION SELECT telegram_id FROM bug_reports
            UNION SELECT telegram_id FROM accounts
            UNION SELECT telegram_id FROM complaint_queue
        """) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows if r[0]]


# ---------- Статистика ----------

async def get_stats(within_days: int = 7) -> dict:
    """Возвращает агрегированную статистику за последние N дней."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Всего пользователей
        async with db.execute(
            "SELECT COUNT(DISTINCT telegram_id) FROM "
            "(SELECT telegram_id FROM complaints "
            " UNION SELECT telegram_id FROM bug_reports)"
        ) as cur:
            total_users = (await cur.fetchone())[0]

        # Жалобы за период
        async with db.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) "
            "FROM complaints WHERE created_at >= datetime('now', ?)",
            (f"-{int(within_days)} days",),
        ) as cur:
            row = await cur.fetchone()
            total = row[0] or 0
            accepted = row[1] or 0
            rejected = row[2] or 0
            pending = row[3] or 0

        # ТОП-5 пользователей по количеству жалоб
        async with db.execute(
            "SELECT telegram_id, COUNT(*) AS n FROM complaints "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY telegram_id ORDER BY n DESC LIMIT 5",
            (f"-{int(within_days)} days",),
        ) as cur:
            top_users = await cur.fetchall()

        # ТОП-5 нарушителей (на кого больше всего жалоб)
        async with db.execute(
            "SELECT nickname, COUNT(*) AS n FROM complaints "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY LOWER(nickname) ORDER BY n DESC LIMIT 5",
            (f"-{int(within_days)} days",),
        ) as cur:
            top_targets = await cur.fetchall()

        # Топ-5 серверов по количеству жалоб (за период)
        async with db.execute(
            "SELECT server_name, COUNT(*) AS n FROM complaints "
            "WHERE created_at >= datetime('now', ?) "
            "AND server_name IS NOT NULL AND server_name != '' "
            "GROUP BY server_name ORDER BY n DESC LIMIT 5",
            (f"-{int(within_days)} days",),
        ) as cur:
            top_servers_rows = await cur.fetchall()
        top_servers: list[tuple[str, int]] = [
            (r[0], r[1]) for r in top_servers_rows
        ]

        # Активность по дням (последние 7 дней)
        async with db.execute(
            "SELECT DATE(created_at) AS d, COUNT(*) FROM complaints "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY d ORDER BY d ASC",
            (f"-{int(within_days)} days",),
        ) as cur:
            by_day = await cur.fetchall()

        # В очереди
        async with db.execute(
            "SELECT COUNT(*) FROM complaint_queue WHERE status='pending'"
        ) as cur:
            queue_pending = (await cur.fetchone())[0]

    return {
        "within_days": within_days,
        "total_users": total_users,
        "total": total,
        "accepted": accepted,
        "rejected": rejected,
        "pending": pending,
        "top_users": [(r[0], r[1]) for r in top_users],
        "top_targets": [(r[0], r[1]) for r in top_targets],
        "top_servers": top_servers,
        "by_day": [(r[0], r[1]) for r in by_day],
        "queue_pending": queue_pending,
    }


async def list_all_complaints(limit: int = 30) -> list[dict]:
    """Возвращает последние N жалоб всех пользователей. Используется админом
    в /check и /stats."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, description, proof_link, "
            "forum_thread_url, status, created_at "
            "FROM complaints ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": row[0], "telegram_id": row[1],
                    "nickname": row[2], "description": row[3],
                    "proof_link": row[4], "forum_thread_url": row[5],
                    "status": row[6] or "pending",
                    "created_at": row[7],
                }
                for row in rows
            ]


async def count_complaints_by_status() -> dict:
    """Возвращает {status: count} по всем жалобам в БД (для админ-сводки)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(status, 'pending'), COUNT(*) "
            "FROM complaints GROUP BY COALESCE(status, 'pending')"
        ) as cur:
            rows = await cur.fetchall()
            return {row[0]: row[1] for row in rows}


# ---------- Черновики жалоб ----------

async def save_draft(telegram_id: int, state_data: dict, step: str | None = None) -> None:
    """Сохраняет/обновляет черновик жалобы пользователя."""
    # Из state удаляем то, что не сериализуется (httpx clients и т.д.)
    safe_data = {}
    for k, v in (state_data or {}).items():
        if k.startswith("_"):
            continue
        if k in ("twofa", "client", "_save_password", "_password_temp",
                  "_creating_template_category", "_tpl_name", "_tpl_summary"):
            continue
        try:
            _json.dumps(v)
            safe_data[k] = v
        except (TypeError, ValueError):
            pass

    payload = _json.dumps(safe_data, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO drafts (telegram_id, state_data, step, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                state_data = excluded.state_data,
                step = excluded.step,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_id, payload, step),
        )
        await db.commit()


async def get_draft(telegram_id: int) -> dict | None:
    """Возвращает черновик пользователя {state_data, step, updated_at} или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT state_data, step, updated_at FROM drafts WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            try:
                data = _json.loads(row[0])
            except Exception:
                return None
            return {"state_data": data, "step": row[1], "updated_at": row[2]}


async def delete_draft(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM drafts WHERE telegram_id = ?", (telegram_id,)
        )
        await db.commit()


# ---------- Настройки бота (key/value) ----------

async def _ensure_settings_table(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


async def get_setting(key: str, default: str | None = None) -> str | None:
    """Получает значение настройки бота. None если не задано."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_settings_table(db)
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Устанавливает значение настройки бота."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_settings_table(db)
        await db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        await db.commit()
    await _trigger_backup()
