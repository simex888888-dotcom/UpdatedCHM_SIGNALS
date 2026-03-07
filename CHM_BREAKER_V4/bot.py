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


async def notify_restart(bot: Bot, um: UserManager, admin_ids: list):
    """Рассылка уведомления о перезапуске:
    - всем пользователям из БД (кроме banned)
    - администраторам всегда, даже если не в БД
    """
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Открыть меню", callback_data="back_main"),
    ]])
    text = "🔄 <b>Бот был обновлён!</b>\n\nНажмите /start чтобы продолжить работу."

    users     = await um.all_users()
    notified  = set()   # чтобы не слать дважды
    sent = failed = 0

    log.info("🔄 Пользователей в БД: " + str(len(users)))

    # 1. Всем пользователям из БД
    for user in users:
        if user.sub_status == "banned":
            continue
        try:
            await bot.send_message(user.user_id, text, parse_mode="HTML", reply_markup=markup)
            notified.add(user.user_id)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning("notify_restart uid=" + str(user.user_id) + ": " + str(e))
            failed += 1

    # 2. Администраторам — всегда, даже если их нет в БД
    for admin_id in admin_ids:
        if admin_id in notified:
            continue
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=markup)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning("notify_restart admin=" + str(admin_id) + ": " + str(e))
            failed += 1

    log.info("🔄 Перезапуск: отправлено " + str(sent) + ", ошибок " + str(failed))


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

    # ─── Авто-восстановление подписок при старте ─────────────────────────────
    async def _auto_restore_subs():
        """Восстанавливает подписки из subs_backup.txt если БД пуста."""
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subs_backup.txt")
        if not os.path.exists(path):
            return
        existing = await um.all_users()
        active_ids = {u.user_id for u in existing if u.sub_status in ("active", "trial")}
        if active_ids:
            log.info(f"✅ В БД уже есть {len(active_ids)} активных пользователей — авторестор не нужен")
            return
        import time as _time
        restored = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 4:
                        continue
                    uid = int(parts[0]); username = parts[1]
                    sub_status = parts[2]; sub_expires = float(parts[3])
                    if sub_expires <= _time.time():
                        continue
                    user = await um.get_or_create(uid, username)
                    user.sub_status = sub_status
                    user.sub_expires = sub_expires
                    user.username = username or user.username
                    await um.save(user)
                    restored += 1
            log.info(f"🔄 Авторестор подписок: восстановлено {restored}")
        except Exception as e:
            log.error(f"Авторестор ошибка: {e}")

    # Рассылка при запуске — после того как aiogram установит соединение с Telegram
    @dp.startup()
    async def on_startup():
        await _auto_restore_subs()
        log.info("🔄 Рассылка уведомлений о перезапуске...")
        await notify_restart(bot, um, config.ADMIN_IDS)

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
