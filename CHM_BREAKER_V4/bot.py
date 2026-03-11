"""
bot.py — точка входа CHM BREAKER MID (50-500 пользователей)
"""

import asyncio
import hashlib
import logging
import os
import shutil
import time
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database
import cache
import turso_sync
from config import Config
from user_manager import UserManager
from scanner_mid import MidScanner
from handlers import register_handlers
from pump_dump.pd_runner import PDRunner


def _code_hash() -> str:
    """MD5 по ключевым .py файлам бота. Меняется только при обновлении кода."""
    base = os.path.dirname(os.path.abspath(__file__))
    files = sorted([
        "bot.py", "handlers.py", "scanner_mid.py",
        "keyboards.py", "database.py", "config.py",
    ])
    h = hashlib.md5()
    for fname in files:
        path = os.path.join(base, fname)
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()


def _backup_db(db_path: str):
    """Создаёт резервную копию БД при каждом запуске бота.

    Хранит последние 5 копий: chm_bot.db.bak1 … .bak5
    Это защищает от потери данных при случайном удалении файла.
    """
    if not os.path.exists(db_path):
        return
    bak_dir  = os.path.dirname(db_path) or "."
    bak_base = db_path + ".bak"
    # Сдвигаем старые бэкапы: bak4→bak5, bak3→bak4 …
    for n in range(4, 0, -1):
        src = bak_base + str(n)
        dst = bak_base + str(n + 1)
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass
    # Сохраняем текущую БД как bak1
    try:
        shutil.copy2(db_path, bak_base + "1")
        log_pre = logging.getLogger("CHM.Main")
        log_pre.info(f"💾 DB backup → {bak_base}1")
    except Exception as e:
        log_pre = logging.getLogger("CHM.Main")
        log_pre.warning(f"⚠️ DB backup failed: {e}")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "chm_mid.log", maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3, encoding="utf-8"
        ),
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
        if user.sub_status not in ("trial", "active"):
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

    # Резервная копия БД перед каждым запуском (5 ротаций)
    _backup_db(config.DB_PATH)

    # init_db сначала — создаёт таблицы (нужны для turso_pull)
    log.info("⏳ Инициализация SQLite...")
    await database.init_db(config.DB_PATH)

    # ─── Turso: восстанавливаем данные из облака (ПОСЛЕ init_db) ─────────────
    # HTTP API читает данные из Turso и пишет в уже созданные таблицы.
    await turso_sync.turso_pull(config.DB_PATH)

    log.info("⏳ Инициализация кэша...")
    cache.init_cache(max_symbols=config.CACHE_MAX_SYMBOLS)

    bot      = Bot(token=config.TELEGRAM_TOKEN)
    dp       = Dispatcher(storage=MemoryStorage())
    um       = UserManager()
    scanner  = MidScanner(config, bot, um)
    pd_runner = PDRunner(bot, config.DB_PATH)

    register_handlers(dp, bot, um, scanner, config, pd_runner=pd_runner)

    # ─── Авто-восстановление подписок при старте ─────────────────────────────
    async def _auto_restore_subs():
        """Восстанавливает подписки из subs_backup.txt при каждом старте.

        Логика слияния (merge): обновляем пользователя только если
        бэкап содержит более позднюю дату истечения, чем в БД.
        Это безопасно при любом сценарии (пустая БД, частично заполненная,
        или полностью заполненная — лишних перезаписей не будет).
        """
        import os, time as _time
        # Читаем из той же папки, куда handlers.py пишет (рядом с БД)
        db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
        path = os.path.join(db_dir, "subs_backup.txt")
        if not os.path.exists(path):
            return
        restored = 0
        skipped  = 0
        now = _time.time()
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # Загружаем текущих пользователей из БД одним запросом
            existing = {u.user_id: u for u in await um.all_users()}
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                try:
                    uid        = int(parts[0])
                    username   = parts[1]
                    sub_status = parts[2]
                    sub_expires = float(parts[3])
                except (ValueError, IndexError):
                    continue
                if sub_expires <= now:
                    continue  # подписка уже истекла — пропускаем
                # Применяем только если бэкап даёт более позднюю дату
                cur = existing.get(uid)
                if cur and cur.sub_expires >= sub_expires:
                    skipped += 1
                    continue  # в БД уже лучше или равно
                user = await um.get_or_create(uid, username)
                user.sub_status  = sub_status
                user.sub_expires = sub_expires
                if username:
                    user.username = username
                await um.save(user)
                existing[uid] = user  # обновляем кэш
                restored += 1
            if restored or skipped:
                log.info(f"🔄 Авторестор подписок: восстановлено {restored}, без изменений {skipped}")
        except Exception as e:
            log.error(f"Авторестор ошибка: {e}")

    # Рассылка при запуске — только если код изменился с последнего запуска
    @dp.startup()
    async def on_startup():
        await _auto_restore_subs()
        current_hash = _code_hash()
        saved_hash   = await database.db_kv_get("bot_code_hash")
        if current_hash != saved_hash:
            log.info(f"🔄 Обнаружено обновление кода ({saved_hash} → {current_hash}), рассылка...")
            await notify_restart(bot, um, config.ADMIN_IDS)
            await database.db_kv_set("bot_code_hash", current_hash)
        else:
            log.info("♻️ Рестарт без изменений кода — рассылка пропущена.")

    import os as _os
    _db_dir = _os.path.dirname(_os.path.abspath(config.DB_PATH))
    _db_writable = _os.access(_db_dir, _os.W_OK)
    log.info("🚀 CHM BREAKER MID запускается...")
    log.info(f"   SQLite:      {config.DB_PATH}  [{'writable' if _db_writable else '⚠️ NOT WRITABLE — check DB_PATH env var'}]")
    log.info(f"   Воркеров:    {config.SCAN_WORKERS}")
    log.info(f"   API conc.:   {config.API_CONCURRENCY}")
    log.info(f"   Кэш монет:   {config.CACHE_MAX_SYMBOLS} символов")

    async def _subs_backup_loop():
        """Периодически сохраняет subs_backup.txt рядом с БД (каждые 10 мин).

        Файл используется _auto_restore_subs() при следующем старте —
        это дополнительная защита на случай если Turso недоступен.
        """
        INTERVAL = 600  # 10 минут
        await asyncio.sleep(30)  # первый цикл через 30 с после старта
        while True:
            try:
                users = await um.all_users()
                import datetime
                lines = [f"# Backup: {datetime.datetime.utcnow().isoformat()}"]
                now = time.time()
                for u in users:
                    if u.sub_status in ("active", "trial") and u.sub_expires > now:
                        lines.append(
                            f"{u.user_id}\t{u.username or ''}\t{u.sub_status}\t{u.sub_expires:.0f}"
                        )
                db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
                path = os.path.join(db_dir, "subs_backup.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                log.debug(f"💾 subs_backup.txt обновлён ({len(lines)-1} подписок)")
            except Exception as exc:
                log.warning(f"subs_backup_loop error: {exc}")
            await asyncio.sleep(INTERVAL)

    async def _save_subs_backup_once():
        """Однократное сохранение subs_backup.txt (используется при завершении)."""
        try:
            users = await um.all_users()
            import datetime
            lines = [f"# Backup: {datetime.datetime.utcnow().isoformat()}"]
            now = time.time()
            for u in users:
                if u.sub_status in ("active", "trial") and u.sub_expires > now:
                    lines.append(
                        f"{u.user_id}\t{u.username or ''}\t{u.sub_status}\t{u.sub_expires:.0f}"
                    )
            db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
            path = os.path.join(db_dir, "subs_backup.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            log.info(f"💾 subs_backup.txt финальное сохранение ({len(lines)-1} подписок)")
        except Exception as exc:
            log.warning(f"subs_backup final save error: {exc}")

    try:
        await asyncio.gather(
            dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
            scanner.run_forever(),
            pd_runner.run_forever(),
            turso_sync.turso_sync_loop(config.DB_PATH),
            _subs_backup_loop(),
        )
    finally:
        log.info("🛑 Завершение...")
        await scanner.fetcher.close()
        await bot.session.close()
        # ─── Финальное сохранение перед выходом ──────────────────────────────
        await _save_subs_backup_once()
        await turso_sync.turso_push(config.DB_PATH)


if __name__ == "__main__":
    asyncio.run(main())
