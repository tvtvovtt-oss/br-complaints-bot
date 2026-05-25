import asyncio
import logging
import platform
import sys
from pathlib import Path

# Чтобы запуск работал и через `python -m src.bot`, и через `python src/bot.py`
# (некоторые хостинги стартуют именно так), добавляем корень проекта в sys.path,
# если его там ещё нет.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

from src.config import BOT_TOKEN, ADMIN_IDS, FORUM_URL
from src.config import DB_PATH, COOKIES_PATH
from src.database import init_db
from src.handlers import common, complaint, bugreport, admin
from src.logger import setup_logging
from src.middleware import (
    ThrottleMiddleware, CleanupMiddleware, MaintenanceMiddleware, BanMiddleware,
    UserTrackingMiddleware,
)
from src.status_monitor import status_monitor_loop
from src.queue_processor import queue_processor_loop
from src.auto_relogin import auto_relogin_loop
from src.error_reporter import install as install_error_reporter
from src.storage_backup import (
    is_enabled as backup_is_enabled,
    restore_db_from_channel,
    force_backup,
    set_bot as set_backup_bot,
    periodic_backup_loop,
)

# Настраиваем логирование до создания любых дочерних логгеров
setup_logging()
logger = logging.getLogger(__name__)


async def main():
    logger.info("=" * 60)
    logger.info("Запуск бота для подачи жалоб на форум Black Russia")
    logger.info("Python %s, платформа: %s", platform.python_version(), platform.platform())
    logger.info("Адрес форума: %s", FORUM_URL)
    logger.info("Путь к БД:   %s", DB_PATH)
    logger.info("Путь к кукам: %s", COOKIES_PATH)
    if ADMIN_IDS:
        logger.info("Доступ ограничен пользователями: %s", ", ".join(map(str, ADMIN_IDS)))
    else:
        # Покажем что реально пришло из переменной — для диагностики на хостингах
        from src.config import _ADMIN_IDS_RAW
        logger.warning(
            "СПИСОК ADMIN_IDS ПУСТ — все пользователи будут админами!\n"
            "  Сырое значение переменной ADMIN_IDS: %r\n"
            "  Если задаёте на хостинге, проверьте: только цифры через запятую, "
            "без кавычек и пробелов. Пример: 7218741941",
            _ADMIN_IDS_RAW,
        )
    logger.info("=" * 60)

    logger.info("Создаю экземпляр бота и диспетчера aiogram...")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Защита от спама и обработка ошибок
    throttle = ThrottleMiddleware()
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)
    cleanup = CleanupMiddleware(throttle)
    dp.message.middleware(cleanup)
    dp.callback_query.middleware(cleanup)
    # Бан забаненных пользователей (до проверки maintenance — иначе бан
    # без причины «обнулится» сообщением о техработах)
    from src.handlers.admin import set_ban_middleware
    ban = BanMiddleware()
    set_ban_middleware(ban)
    dp.message.middleware(ban)
    dp.callback_query.middleware(ban)
    # Режим обслуживания — отказ обычным юзерам если админ включил
    maintenance = MaintenanceMiddleware()
    dp.message.middleware(maintenance)
    dp.callback_query.middleware(maintenance)
    # Учёт всех пользователей бота — записываем в БД любого, кто прошёл
    # антиспам, бан и maintenance. Так попадают и те, кто просто /start нажал.
    tracker = UserTrackingMiddleware()
    dp.message.middleware(tracker)
    dp.callback_query.middleware(tracker)
    logger.info("Подключены middleware: Throttle, Ban, Maintenance, UserTracking.")

    dp.include_router(common.router)
    dp.include_router(complaint.router)
    dp.include_router(bugreport.router)
    dp.include_router(admin.router)
    logger.info("Подключены роутеры: common, complaint, bugreport, admin.")

    try:
        bot_info = await bot.get_me()
        logger.info("Бот авторизован в Telegram: @%s (id=%s)", bot_info.username, bot_info.id)
    except Exception as e:
        logger.error("Не удалось авторизоваться в Telegram: %s", e)
        raise

    # Регистрируем bot в модуле бэкапа — теперь хендлеры могут вызывать
    # schedule_backup() без аргументов.
    set_backup_bot(bot)

    # Подключаем отправку ошибок (ERROR/CRITICAL) в Telegram-чат админу,
    # если настроена переменная ERROR_LOG_CHAT_ID или LOG_TO_ADMIN=1.
    if install_error_reporter(bot):
        logger.info("Подключён TelegramErrorHandler — все ERROR-ы будут "
                    "приходить вам в личку.")
    else:
        logger.info("TelegramErrorHandler не активирован "
                    "(ERROR_LOG_CHAT_ID/LOG_TO_ADMIN не заданы).")

    # Восстановление БД из канала (если включён бэкап и локальной БД нет).
    # ДО init_db, чтобы не затереть восстановленную базу пустой схемой.
    if backup_is_enabled():
        try:
            await restore_db_from_channel(bot)
        except Exception:
            logger.exception("Ошибка при восстановлении БД из канала — продолжаю без.")
    else:
        logger.info("Бэкап в Telegram-канал не настроен (STORAGE_CHANNEL_ID пуст).")

    logger.info("Инициализирую базу данных...")
    await init_db()
    logger.info("База данных готова к работе.")

    logger.info("Удаляю webhook и сбрасываю накопившиеся обновления...")
    await bot.delete_webhook(drop_pending_updates=True)
    # Небольшая пауза чтобы Telegram успел освободить полл-слот от
    # предыдущего экземпляра (если был запущен где-то ещё).
    await asyncio.sleep(2)

    # Регистрируем команды для автокомплита в Telegram (когда юзер набирает /)
    public_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="new_complaint", description="📝 Подать жалобу"),
        BotCommand(command="templates", description="📋 Мои шаблоны"),
        BotCommand(command="bug", description="🐞 Сообщить о баге"),
        BotCommand(command="me", description="👤 Мой профиль"),
        BotCommand(command="cancel", description="❌ Отменить действие"),
        BotCommand(command="help", description="📖 Справка"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="login", description="🔐 Войти по паролю"),
        BotCommand(command="accounts", description="👥 Аккаунты форума"),
        BotCommand(command="relogin", description="🔄 Перелогинить аккаунты"),
        BotCommand(command="sync", description="🔄 Синхронизировать форум"),
        BotCommand(command="check", description="🔍 Проверить статусы жалоб"),
        BotCommand(command="checkurl", description="🔍 Проверить статус темы"),
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="broadcast", description="📢 Рассылка"),
        BotCommand(command="queue", description="📦 Очередь жалоб"),
        BotCommand(command="complaints", description="📋 Все жалобы (просмотр)"),
        BotCommand(command="delcomplaint", description="🗂 Удалить жалобу по id"),
        BotCommand(command="bugs", description="🐞 Список баг-репортов"),
        BotCommand(command="ban", description="🚫 Забанить пользователя"),
        BotCommand(command="unban", description="✅ Разбанить пользователя"),
        BotCommand(command="banlist", description="🚫 Список забаненных"),
        BotCommand(command="maintenance", description="🔒 Режим обслуживания"),
        BotCommand(command="dbinfo", description="🛠 Состояние БД"),
    ]
    try:
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    admin_commands, scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception as e:
                logger.warning("Не смог установить admin-команды для %s: %s",
                               admin_id, e)
        logger.info("Команды бота зарегистрированы (default: %d, admin: %d).",
                    len(public_commands), len(admin_commands))
    except Exception:
        logger.exception("Ошибка регистрации команд")

    logger.info("Запускаю long-polling. Для остановки нажмите Ctrl+C.")
    monitor_task = asyncio.create_task(status_monitor_loop(bot))
    queue_task = asyncio.create_task(queue_processor_loop(bot))
    relogin_task = asyncio.create_task(auto_relogin_loop())
    logger.info("Запущены фоновые задачи: мониторинг статусов, "
                "процессор очереди, авто-перелогин.")
    backup_task = None
    if backup_is_enabled():
        backup_task = asyncio.create_task(periodic_backup_loop(bot))
        logger.info("Фоновая задача периодического бэкапа БД запущена.")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Делаю финальный бэкап БД перед остановкой...")
        try:
            await force_backup(bot)
        except Exception:
            logger.exception("Финальный бэкап не удался.")
        logger.info("Останавливаю фоновые задачи...")
        for task in (monitor_task, queue_task, relogin_task, backup_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("Закрываю HTTP-сессию бота...")
        await bot.session.close()
        logger.info("Сессия закрыта. Завершение работы.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки от пользователя. Бот остановлен.")
    except Exception:
        logger.exception("Фатальная ошибка при запуске бота")
        raise
