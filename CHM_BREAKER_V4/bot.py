"""
bot.py — точка входа CHM BREAKER MID (50-500 пользователей)
"""

import asyncio
import logging
import time
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database
import cache
from config import Config
from user_manager import UserManager
from scanner_mid import MidScanner
from handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chm_mid.log", encoding="utf-8"),
    ],
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

log = logging.getLogger("CHM.Main")


async def notify_restart(bot: Bot, um: UserManager):
    """Рассылка уведомления о перезапуске бота всем активным/триальным пользователям."""
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Открыть меню", callback_data="back_main"),
    ]])
    users = await um.all_users()
    sent = 0
    for user in users:
        if user.sub_status in ("trial", "active") and user.sub_expires > time.time():
            try:
                await bot.send_message(
                    user.user_id,
                    "🔄 <b>Бот был обновлён!</b>\n\nНажмите /start чтобы продолжить работу.",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                sent += 1
                await asyncio.sleep(0.05)  # защита от флуд-лимита
            except Exception:
                pass
    log.info("🔄 Уведомлений о перезапуске отправлено: " + str(sent))


async def main():
    config = Config()

    log.info("⏳ Инициализация SQLite...")
    await database.init_db(config.DB_PATH)

    log.info("⏳ Инициализация кэша...")
    cache.init_cache(max_symbols=config.CACHE_MAX_SYMBOLS)

    bot     = Bot(token=config.TELEGRAM_TOKEN)
    dp      = Dispatcher(storage=MemoryStorage())
    um      = UserManager()
    scanner = MidScanner(config, bot, um)

    register_handlers(dp, bot, um, scanner, config)

    log.info("🔄 Рассылка уведомлений о перезапуске...")
    await notify_restart(bot, um)

    log.info("🚀 CHM BREAKER MID запускается...")
    log.info(f"   SQLite:      {config.DB_PATH}")
    log.info(f"   Воркеров:    {config.SCAN_WORKERS}")
    log.info(f"   API conc.:   {config.API_CONCURRENCY}")
    log.info(f"   Кэш монет:   {config.CACHE_MAX_SYMBOLS} символов")

    try:
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
            scanner.run_forever(),
        )
    finally:
        log.info("🛑 Завершение...")
        await scanner.fetcher.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
