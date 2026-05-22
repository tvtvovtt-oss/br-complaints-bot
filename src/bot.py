import asyncio
import logging
import platform
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.config import BOT_TOKEN, ADMIN_IDS, FORUM_URL
from src.database import init_db
from src.handlers import common, complaint
from src.logger import setup_logging
from src.status_monitor import status_monitor_loop

# Настраиваем логирование до создания любых дочерних логгеров
setup_logging()
logger = logging.getLogger(__name__)


async def main():
    logger.info("=" * 60)
    logger.info("Запуск бота для подачи жалоб на форум Black Russia")
    logger.info("Python %s, платформа: %s", platform.python_version(), platform.platform())
    logger.info("Адрес форума: %s", FORUM_URL)
    if ADMIN_IDS:
        logger.info("Доступ ограничен пользователями: %s", ", ".join(map(str, ADMIN_IDS)))
    else:
        logger.warning("Список ADMIN_IDS пуст — бот доступен ВСЕМ. Это небезопасно!")
    logger.info("=" * 60)

    logger.info("Инициализирую базу данных...")
    await init_db()
    logger.info("База данных готова к работе.")

    logger.info("Создаю экземпляр бота и диспетчера aiogram...")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.include_router(common.router)
    dp.include_router(complaint.router)
    logger.info("Подключены роутеры: common, complaint.")

    try:
        bot_info = await bot.get_me()
        logger.info("Бот авторизован в Telegram: @%s (id=%s)", bot_info.username, bot_info.id)
    except Exception as e:
        logger.error("Не удалось авторизоваться в Telegram: %s", e)
        raise

    logger.info("Удаляю webhook и сбрасываю накопившиеся обновления...")
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Запускаю long-polling. Для остановки нажмите Ctrl+C.")
    monitor_task = asyncio.create_task(status_monitor_loop(bot))
    logger.info("Фоновая задача мониторинга статусов жалоб запущена.")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Останавливаю фоновый мониторинг...")
        monitor_task.cancel()
        try:
            await monitor_task
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
