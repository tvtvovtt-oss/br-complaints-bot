import json as _json
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


async def _trigger_backup_immediate() -> None:
    """Немедленный бэкап без 30-секундного дебаунса. Используется для
    критичных изменений (логин, обновление кук, бан) — чтобы свежие
    данные точно ушли в канал до того, как хостинг сделает rolling-deploy
    и стартует с устаревшего бэкапа."""
    try:
        from src.storage_backup import force_backup, is_enabled
        if not is_enabled():
            return
        await force_backup()
    except Exception:
        logger.debug("trigger_backup_immediate failed", exc_info=True)


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
        # Миграции для существующих БД. cols пересчитываем после каждого
        # успешного ALTER — на случай если миграция упала посередине и
        # повторно запустилась, чтобы не пытаться добавить уже существующую
        # колонку. На всякий ловим OperationalError "duplicate column name"
        # как идемпотентную операцию.
        async def _column_exists(table: str, col: str) -> bool:
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                return any(row[1] == col for row in await cur.fetchall())

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
            ("admin_comment", "ALTER TABLE complaints ADD COLUMN admin_comment TEXT"),
        ]:
            if await _column_exists("complaints", col):
                continue
            logger.info("Миграция: добавляю колонку '%s' в complaints.", col)
            try:
                await db.execute(ddl)
            except aiosqlite.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" in msg:
                    logger.debug("Колонка %s уже существует — пропускаю.", col)
                else:
                    raise

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
        if not await _column_exists("servers", "position"):
            logger.info("Миграция: добавляю колонку 'position' в таблицу servers.")
            try:
                await db.execute(
                    "ALTER TABLE servers ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

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
        if not await _column_exists("accounts", "cooldown_until"):
            logger.info("Миграция: добавляю колонку 'cooldown_until' в accounts.")
            try:
                await db.execute(
                    "ALTER TABLE accounts ADD COLUMN cooldown_until TIMESTAMP"
                )
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        if not await _column_exists("accounts", "encrypted_password"):
            logger.info("Миграция: добавляю колонку 'encrypted_password' в accounts.")
            try:
                await db.execute(
                    "ALTER TABLE accounts ADD COLUMN encrypted_password TEXT"
                )
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        # needs_reauth — флаг «куки протухли, нужен ручной перелогин».
        # Ставится при HTTP 403/redirect на /login/ при попытке публикации.
        # Снимается при upsert_account (новый успешный логин).
        if not await _column_exists("accounts", "needs_reauth"):
            logger.info("Миграция: добавляю колонку 'needs_reauth' в accounts.")
            try:
                await db.execute(
                    "ALTER TABLE accounts ADD COLUMN needs_reauth INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            except aiosqlite.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_accounts_active
                ON accounts(telegram_id, is_active)
        """)

        # Индексы на жалобах — для быстрого запроса истории пользователя,
        # фильтра «pending» при мониторинге, поиска по нику-цели.
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_user
                ON complaints(telegram_id, id DESC)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_status
                ON complaints(status)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_complaints_nickname
                ON complaints(LOWER(nickname))
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
        # Таблица users (нужна для list_all_users без UNION) + бэкфил из
        # старых таблиц для проектов, обновляющихся с предыдущих версий.
        await _ensure_users_table(db)
        await _backfill_users(db)
        await db.commit()
    logger.info("База данных готова. Таблицы: complaints, servers, "
                "complaint_categories, accounts, users.")


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


async def search_complaints_by_nick(
    nickname: str,
    telegram_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Поиск жалоб по нику цели (частичное совпадение, без учёта регистра).

    Если telegram_id задан — ищем только в жалобах этого пользователя.
    Если None — по всем (для админов).
    Возвращает не более `limit` записей, новые первыми.
    """
    like = f"%{nickname.strip()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        if telegram_id is not None:
            async with db.execute(
                "SELECT id, telegram_id, nickname, forum_thread_url, "
                "status, created_at, summary "
                "FROM complaints "
                "WHERE LOWER(nickname) LIKE LOWER(?) AND telegram_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (like, telegram_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, telegram_id, nickname, forum_thread_url, "
                "status, created_at, summary "
                "FROM complaints "
                "WHERE LOWER(nickname) LIKE LOWER(?) "
                "ORDER BY id DESC LIMIT ?",
                (like, limit),
            ) as cur:
                rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "telegram_id": r[1],
            "nickname": r[2],
            "forum_thread_url": r[3],
            "status": r[4] or "pending",
            "created_at": r[5],
            "summary": r[6],
        }
        for r in rows
    ]


async def get_user_complaint_stats(telegram_id: int) -> dict:
    """Расширенная статистика жалоб пользователя: счётчики по статусам,
    процент успеха и топ-3 цели (по числу жалоб)."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Счётчики по статусам
        async with db.execute(
            "SELECT status, COUNT(*) FROM complaints "
            "WHERE telegram_id = ? GROUP BY status",
            (telegram_id,),
        ) as cur:
            status_rows = await cur.fetchall()

        # Топ-3 ника, на которого больше всего жалоб
        async with db.execute(
            "SELECT nickname, COUNT(*) AS cnt FROM complaints "
            "WHERE telegram_id = ? "
            "GROUP BY LOWER(nickname) ORDER BY cnt DESC LIMIT 3",
            (telegram_id,),
        ) as cur:
            top_rows = await cur.fetchall()

    counts = {row[0]: row[1] for row in status_rows}
    total = sum(counts.values())
    accepted = counts.get("accepted", 0)
    rejected = counts.get("rejected", 0)
    review = counts.get("review", 0)
    pending = counts.get("pending", 0)
    closed = counts.get("closed", 0)
    queue = counts.get("queue", 0)

    success_pct = round(accepted / total * 100) if total else 0

    return {
        "total": total,
        "accepted": accepted,
        "rejected": rejected,
        "review": review,
        "pending": pending,
        "closed": closed,
        "queue": queue,
        "success_pct": success_pct,
        "top_targets": [{"nickname": r[0], "count": r[1]} for r in top_rows],
    }


async def get_complaint(complaint_id: int) -> dict | None:
    """Возвращает жалобу по id (или None)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, description, proof_link, "
            "forum_thread_url, status, notified_status, created_at, "
            "account_id, your_nickname, summary, category_key, "
            "punishment_date, admin_comment "
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
                "admin_comment": row[14],
            }


async def list_complaints_for_status_check() -> list[dict]:
    """Возвращает все жалобы с forum_thread_url, у которых статус ещё
    не финальный — нужно проверить состояние на форуме.

    Финальные статусы — accepted/rejected/closed — не проверяются повторно
    (тема уже решена админом форума, нет смысла дёргать). Перепроверяем
    только pending/unknown/review."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, telegram_id, nickname, forum_thread_url, status, "
            "notified_status, account_id, admin_comment "
            "FROM complaints "
            "WHERE forum_thread_url IS NOT NULL "
            "AND status IN ('pending', 'unknown', 'review')"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0], "telegram_id": r[1], "nickname": r[2],
                    "forum_thread_url": r[3],
                    "status": r[4] or "pending",
                    "notified_status": r[5],
                    "account_id": r[6],
                    "admin_comment": r[7],
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


async def update_complaint_admin_comment(complaint_id: int,
                                            comment: str | None) -> None:
    """Сохраняет комментарий админа форума к жалобе (для показа автору
    и в карточке жалобы)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE complaints SET admin_comment = ? WHERE id = ?",
            (comment, complaint_id),
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
                INSERT INTO accounts (telegram_id, username, login, cookies_json, is_active, needs_reauth)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(telegram_id, username) DO UPDATE SET
                    login = excluded.login,
                    cookies_json = excluded.cookies_json,
                    is_active = excluded.is_active,
                    needs_reauth = 0,
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
    # Immediate-бэкап: новый аккаунт / обновлённые куки критичны для
    # rolling deploy.
    await _trigger_backup_immediate()
    return account_id


async def list_accounts(telegram_id: int) -> list[dict]:
    """Возвращает список аккаунтов пользователя:
    [{id, username, login, is_active, cooldown_until, needs_reauth,
      created_at, updated_at}, ...]
    Активный аккаунт идёт первым."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, username, login, is_active, cooldown_until,
                   COALESCE(needs_reauth, 0), created_at, updated_at
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
                    "needs_reauth": bool(r[5]),
                    "created_at": r[6],
                    "updated_at": r[7],
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
                   cooldown_until, COALESCE(needs_reauth, 0)
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
                "needs_reauth": bool(row[7]),
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
    """Обновляет куки аккаунта (после реауторизации).

    Делает немедленный бэкап (без 30-секундного дебаунса), чтобы при rolling
    deploy на хостинге свежие куки точно успели уйти в Telegram-канал и
    после рестарта восстановились актуальные, а не вчерашние."""
    cookies_str = _json.dumps(cookies, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET cookies_json = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (cookies_str, account_id),
        )
        await db.commit()
    await _trigger_backup_immediate()


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

    Аккаунты с needs_reauth=1 в выборку не попадают — после ручного
    перелогина флаг сбрасывается и аккаунт снова доступен.

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
                  AND COALESCE(needs_reauth, 0) = 0
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

            # Все аккаунты в кулдауне — возвращаем с минимальным остатком.
            # Аккаунты с needs_reauth=1 не предлагаем — они всё равно не сработают.
            async with db.execute(
                """
                SELECT id, username, login, cookies_json, cooldown_until,
                       CAST((julianday(cooldown_until) - julianday('now')) * 86400 AS INTEGER)
                       AS remaining
                FROM accounts
                WHERE telegram_id = ?
                  AND COALESCE(needs_reauth, 0) = 0
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


async def update_user_template(telegram_id: int, template_id: int,
                                 *, name: str | None = None,
                                 summary: str | None = None,
                                 description: str | None = None) -> bool:
    """Обновляет одно или несколько полей шаблона. Возвращает True если что-то
    обновилось. Имена столбцов фиксированные литералы — без склейки строк."""
    if name is None and summary is None and description is None:
        return False
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if summary is not None:
        sets.append("summary = ?")
        params.append(summary)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    params.extend([template_id, telegram_id])
    sql = (
        "UPDATE user_templates SET " + ", ".join(sets)
        + " WHERE id = ? AND telegram_id = ?"
    )
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
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

    Атомарность гарантируется одновременно тремя слоями:
    1. BEGIN IMMEDIATE — захватывает write-lock SQLite на всю транзакцию,
       параллельные коннекты на write-операциях ждут.
    2. SQLite сериализует write-транзакции (single writer model).
    3. UPDATE с подзапросом SELECT...LIMIT 1 + RETURNING выполняется как
       единая операция: между SELECT и UPDATE невозможно вклиниться.

    Поэтому даже если N воркеров одновременно вызовут эту функцию —
    каждому достанется свой аккаунт (или None если свободных нет).

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
                      AND COALESCE(needs_reauth, 0) = 0
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


async def mark_account_needs_reauth(account_id: int) -> None:
    """Помечает аккаунт как требующий ручного перелогина.

    Вызывается когда форум вернул 403 / редирект на /login/ при попытке
    публикации. Такие аккаунты исключаются из пула (find_available_account /
    claim_available_account) до тех пор, пока админ не сделает повторный
    /login — upsert_account автоматически сбрасывает флаг needs_reauth=0.

    Дополнительно ставим длинный кулдаун (24 часа), чтобы даже если флаг
    каким-то образом не отработал в фильтре — аккаунт хотя бы не дёргался
    в каждой жалобе.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts "
            "SET needs_reauth = 1, "
            "    cooldown_until = datetime('now', '+86400 seconds') "
            "WHERE id = ?",
            (account_id,),
        )
        await db.commit()
    logger.warning("Аккаунт id=%s помечен needs_reauth=1 — нужен ручной "
                   "перелогин (/login).", account_id)
    await _trigger_backup_immediate()


async def clear_account_needs_reauth(account_id: int) -> None:
    """Снимает флаг needs_reauth и сбрасывает кулдаун. Используется когда
    периодическая проверка обнаруживает, что куки на самом деле живы
    (это значит флаг был поставлен ложно, например на 403 от DDoS-Guard)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts "
            "SET needs_reauth = 0, "
            "    cooldown_until = NULL "
            "WHERE id = ?",
            (account_id,),
        )
        await db.commit()
    logger.info("Аккаунт id=%s: needs_reauth снят (куки живы).", account_id)
    await _trigger_backup_immediate()


# ---------- Шифрованный пароль для авто-перелогина ----------

async def set_account_encrypted_password(account_id: int,
                                           encrypted_password: str | None) -> None:
    """Сохраняет/удаляет зашифрованный пароль аккаунта.
    Делает немедленный бэкап — пароль слишком ценный, чтобы потерять при
    rolling deploy."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET encrypted_password = ? WHERE id = ?",
            (encrypted_password, account_id),
        )
        await db.commit()
    await _trigger_backup_immediate()


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
    """Возвращает уникальные telegram_id всех известных пользователей бота.

    Читает из таблицы `users` — она заполняется через middleware при любом
    взаимодействии и пополняется бэкфилом при init_db (см. `_backfill_users`).
    Это O(N) по индексу, без UNION'ов 5 таблиц.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_users_table(db)
        async with db.execute(
            "SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows if r[0]]


async def _backfill_users(db: aiosqlite.Connection) -> int:
    """Дозаполняет таблицу users telegram_id из старых таблиц (миграция).
    Запускается один раз при init_db. Использует INSERT OR IGNORE — повторный
    вызов идемпотентен и быстро отрабатывает."""
    cur = await db.execute("""
        INSERT OR IGNORE INTO users (telegram_id)
        SELECT telegram_id FROM (
            SELECT telegram_id FROM complaints
            UNION SELECT telegram_id FROM user_templates
            UNION SELECT telegram_id FROM bug_reports
            UNION SELECT telegram_id FROM accounts
            UNION SELECT telegram_id FROM complaint_queue
        ) WHERE telegram_id IS NOT NULL
    """)
    inserted = cur.rowcount
    await db.commit()
    if inserted and inserted > 0:
        logger.info("users-таблица: бэкфил добавил %d telegram_id из старых таблиц.",
                    inserted)
    return inserted or 0


# ---------- Статистика ----------

async def get_stats(within_days: int = 7) -> dict:
    """Возвращает агрегированную статистику за последние N дней."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_users_table(db)
        # Всего пользователей: считаем по таблице users (все кто хотя бы
        # нажимал /start) + UNION со старыми таблицами на случай миграции.
        async with db.execute(
            "SELECT COUNT(DISTINCT telegram_id) FROM ("
            " SELECT telegram_id FROM users"
            " UNION SELECT telegram_id FROM complaints"
            " UNION SELECT telegram_id FROM bug_reports"
            ")"
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

        # Новые пользователи (зарегистрированы в users) и активные (last_seen)
        # за выбранный период.
        async with db.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE first_seen_at >= datetime('now', ?)",
            (f"-{int(within_days)} days",),
        ) as cur:
            new_users = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE last_seen_at >= datetime('now', ?)",
            (f"-{int(within_days)} days",),
        ) as cur:
            active_users = (await cur.fetchone())[0]

    return {
        "within_days": within_days,
        "total_users": total_users,
        "new_users": new_users,
        "active_users": active_users,
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


# ---------- Бан пользователей бота ----------

async def _ensure_bans_table(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            telegram_id INTEGER PRIMARY KEY,
            reason TEXT,
            banned_by INTEGER,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


async def ban_user(telegram_id: int, reason: str | None = None,
                    banned_by: int | None = None) -> bool:
    """Добавляет пользователя в бан-лист. Возвращает True если добавлено
    впервые (False — уже был забанен и просто обновили причину)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_bans_table(db)
        async with db.execute(
            "SELECT 1 FROM banned_users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            already = await cur.fetchone() is not None

        await db.execute(
            """
            INSERT INTO banned_users (telegram_id, reason, banned_by, banned_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                reason = excluded.reason,
                banned_by = excluded.banned_by,
                banned_at = CURRENT_TIMESTAMP
            """,
            (telegram_id, reason, banned_by),
        )
        await db.commit()
    logger.info("Пользователь telegram_id=%s забанен (причина: %s, кем: %s).",
                telegram_id, reason or "—", banned_by)
    await _trigger_backup()
    return not already


async def unban_user(telegram_id: int) -> bool:
    """Снимает бан. True если пользователь был забанен."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_bans_table(db)
        cur = await db.execute(
            "DELETE FROM banned_users WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        logger.info("Пользователь telegram_id=%s разбанен.", telegram_id)
        await _trigger_backup()
    return ok


async def is_banned(telegram_id: int) -> dict | None:
    """Возвращает запись о бане {reason, banned_by, banned_at} или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_bans_table(db)
        async with db.execute(
            "SELECT reason, banned_by, banned_at FROM banned_users "
            "WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"reason": row[0], "banned_by": row[1], "banned_at": row[2]}


async def list_banned() -> list[dict]:
    """Все забаненные."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_bans_table(db)
        async with db.execute(
            "SELECT telegram_id, reason, banned_by, banned_at "
            "FROM banned_users ORDER BY banned_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"telegram_id": r[0], "reason": r[1],
                 "banned_by": r[2], "banned_at": r[3]}
                for r in rows
            ]


# ---------- Расширенный поиск жалоб для админа ----------

async def list_complaints_paginated(
    page: int = 1,
    page_size: int = 10,
    status: str | None = None,
    target_nickname: str | None = None,
    telegram_id: int | None = None,
) -> tuple[list[dict], int]:
    """Постраничный список жалоб для админа с фильтрами.

    Возвращает (список_жалоб, total_count).

    SQL составляется только из заранее определённых литералов внутри функции
    (нет конкатенации пользовательского ввода в SQL). Все динамические
    значения проходят как параметры (?, ...).
    """
    # Каждое условие — фиксированный литерал (никогда не приходит из ввода).
    conditions: list[str] = []
    params: list = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if target_nickname is not None:
        conditions.append("LOWER(nickname) LIKE LOWER(?)")
        params.append(f"%{target_nickname}%")
    if telegram_id is not None:
        conditions.append("telegram_id = ?")
        params.append(telegram_id)

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = max(0, (page - 1) * page_size)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM complaints {where_sql}", params,
        ) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(
            f"""
            SELECT id, telegram_id, nickname, status, forum_thread_url,
                   server_name, summary, created_at
            FROM complaints {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ) as cur:
            rows = await cur.fetchall()
            items = [
                {
                    "id": r[0], "telegram_id": r[1], "nickname": r[2],
                    "status": r[3] or "pending",
                    "forum_thread_url": r[4],
                    "server_name": r[5], "summary": r[6],
                    "created_at": r[7],
                }
                for r in rows
            ]
    return items, total


async def admin_delete_complaint(complaint_id: int) -> bool:
    """Админское удаление жалобы из БД без проверки автора (в отличие от
    delete_complaint, где telegram_id обязателен). Возвращает True если
    запись удалилась."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM complaints WHERE id = ?", (complaint_id,),
        )
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        logger.info("Админское удаление жалобы #%s из БД.", complaint_id)
        await _trigger_backup()
    return ok


# ---------- Учёт всех пользователей бота ----------

async def _ensure_users_table(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            language_code TEXT,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            interactions INTEGER NOT NULL DEFAULT 1
        )
    """)


async def track_user(telegram_id: int, username: str | None = None,
                      full_name: str | None = None,
                      language_code: str | None = None) -> None:
    """Отмечает пользователя как взаимодействовавшего с ботом. Если первый
    раз — создаёт запись, иначе обновляет last_seen_at и инкрементит счётчик."""
    if not telegram_id or telegram_id <= 0:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_users_table(db)
        await db.execute(
            """
            INSERT INTO users
                (telegram_id, username, full_name, language_code,
                 first_seen_at, last_seen_at, interactions)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                full_name = COALESCE(excluded.full_name, users.full_name),
                language_code = COALESCE(excluded.language_code, users.language_code),
                last_seen_at = CURRENT_TIMESTAMP,
                interactions = users.interactions + 1
            """,
            (telegram_id, username, full_name, language_code),
        )
        await db.commit()


async def list_tracked_users(limit: int | None = None) -> list[dict]:
    """Все известные пользователи с метаданными (для админ-команд)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_users_table(db)
        sql = (
            "SELECT telegram_id, username, full_name, first_seen_at, "
            "last_seen_at, interactions "
            "FROM users ORDER BY last_seen_at DESC"
        )
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "telegram_id": r[0], "username": r[1],
                    "full_name": r[2], "first_seen_at": r[3],
                    "last_seen_at": r[4], "interactions": r[5],
                }
                for r in rows
            ]


async def count_tracked_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_users_table(db)
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
