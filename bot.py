"""
╔══════════════════════════════════════════════════════════════╗
║        CHM BREAKER BOT — Telegram Multi-User Edition        ║
║              by CHM Laboratory                              ║
╚══════════════════════════════════════════════════════════════╝

Запуск: python3 bot.py
"""

import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import Config
from user_manager import UserManager
from handlers import register_handlers
from scanner_multi import MultiScanner
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chm_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("CHM")


async def main():
    config  = Config()

    # ── Инициализация базы данных (создаёт таблицы если нет) ──
    await db.init_db(config.DB_PATH)

    bot     = Bot(token=config.TELEGRAM_TOKEN)
    storage = MemoryStorage()
    dp      = Dispatcher(storage=storage)

    user_manager = UserManager()
    scanner      = MultiScanner(config, bot, user_manager)

    # Регистрируем все хэндлеры
    register_handlers(dp, bot, user_manager, scanner, config)

    log.info("✅ v4.6 — Компактный сигнал + Статистика + Чеклист")
    log.info("🚀 CHM BREAKER BOT запускается (multi-user режим)...")

    # Запускаем сканер и бота параллельно
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        scanner.run_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
