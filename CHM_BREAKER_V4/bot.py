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
import cache_gc
import wallet_service
import poly_scheduler
from config import Config
from user_manager import UserManager
from scanner_mid import MidScanner
from handlers import register_handlers
from pump_dump.pd_runner import PDRunner
from polymarket_service import PolymarketService
from poly_handlers import register_poly_handlers
from gerchik_runner import GerchikScanner


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


async def notify_restart(bot: Bot, admin_ids: list):
    """Уведомление о перезапуске — только администраторам."""
    text = "🔄 <b>Бот обновлён</b> (новый код задеплоен)."
    sent = failed = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            log.warning("notify_restart admin=" + str(admin_id) + ": " + str(e))
            failed += 1
    log.info("🔄 Перезапуск: отправлено " + str(sent) + ", ошибок " + str(failed))


async def main():
    config = Config()

    import os as _os
    _db_dir     = _os.path.dirname(_os.path.abspath(config.DB_PATH))
    _db_writable = _os.access(_db_dir, _os.W_OK)
    log.info("🚀 CHM BREAKER MID запускается...")
    log.info(f"   SQLite:      {config.DB_PATH}  [{'writable' if _db_writable else '⚠️ NOT WRITABLE — check DB_PATH env var'}]")
    log.info(f"   Воркеров:    {config.SCAN_WORKERS}")
    log.info(f"   API conc.:   {config.API_CONCURRENCY}")
    log.info(f"   Кэш монет:   {config.CACHE_MAX_SYMBOLS} символов")

    # ─── ШАГ 1: Бэкап локального SQLite (до любых изменений) ────────────────
    _backup_db(config.DB_PATH)

    # ─── ШАГ 2: Инициализация SQLite — создаём правильную схему (типы!) ─────
    # ВАЖНО: init_db ПЕРЕД restore, иначе restore создаёт таблицы с col TEXT
    # для всех колонок (включая INTEGER/REAL), и данные читаются как строки.
    log.info("⏳ Инициализация SQLite...")
    await database.init_db(config.DB_PATH)

    # ─── ШАГ 3: Восстанавливаем данные из Turso (в уже правильную схему) ────
    # Если Turso пустой — оставляем локальную БД как есть.
    # После вызова _restore_attempted=True → turso_push разблокируется.
    turso_had_data = await turso_sync.restore_from_turso_if_needed(config.DB_PATH)

    # ─── ШАГ 4: Всегда пушим при старте ─────────────────────────────────────
    # turso_push защищён флагом _restore_attempted — не выполнится до restore.
    # Без безусловного пуша при частых рестартах (< SYNC_INTERVAL=300с)
    # данные никогда не попадают в Turso (kv:1 — типичный симптом).
    if turso_sync.is_configured():
        log.info("⬆️  Turso: первичный пуш при старте...")
        await turso_sync.turso_push(config.DB_PATH)

    log.info("⏳ Инициализация кэша...")
    cache.init_cache(max_symbols=config.CACHE_MAX_SYMBOLS)

    bot      = Bot(token=config.TELEGRAM_TOKEN)
    dp       = Dispatcher(storage=MemoryStorage())
    um       = UserManager()
    scanner  = MidScanner(config, bot, um)
    pd_runner = PDRunner(bot, config.DB_PATH)

    register_handlers(dp, bot, um, scanner, config, pd_runner=pd_runner)

    gerchik_scanner = GerchikScanner(bot, um, fetcher=scanner.fetcher)

    poly = PolymarketService()
    register_poly_handlers(dp, bot, um, config, poly)
    log.info(f"📊 Polymarket: {'торговля включена ✅' if poly.is_trading_enabled() else 'только просмотр (POLY_PRIVATE_KEY не задан)'}")
    log.info(f"👛 Кастодиальные кошельки: {'✅ активны' if wallet_service.is_configured() else '❌ WALLET_ENCRYPTION_KEY не задан'}")

    # ─── Авто-восстановление подписок при старте ─────────────────────────────
    async def _auto_restore_subs():
        """Восстанавливает подписки из subs_backup.txt при каждом старте.

        Логика слияния (merge): обновляем пользователя только если
        бэкап содержит более позднюю дату истечения, чем в БД.
        Это безопасно при любом сценарии (пустая БД, частично заполненная,
        или полностью заполненная — лишних перезаписей не будет).
        """
        import os, time as _time
        # Ищем subs_backup.txt: сначала рядом с БД, потом рядом со скриптом
        db_dir     = os.path.dirname(os.path.abspath(config.DB_PATH))
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(db_dir, "subs_backup.txt")
        if not os.path.exists(path):
            # Фоллбэк: попробовать рядом со скриптом (помогает при смене DB_PATH)
            alt_path = os.path.join(script_dir, "subs_backup.txt")
            if os.path.exists(alt_path):
                log.info(f"🔄 subs_backup.txt найден в {alt_path} (fallback)")
                path = alt_path
            else:
                log.warning(f"⚠️  subs_backup.txt не найден ({path}) — подписки не восстановлены из backup!")
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
            await notify_restart(bot, config.ADMIN_IDS)
            await database.db_kv_set("bot_code_hash", current_hash)
            # Немедленно пушим хэш в Turso, чтобы следующий рестарт
            # не считал код «новым» из-за 60-секундной задержки sync_loop.
            if turso_sync.is_configured():
                await turso_sync.turso_push(config.DB_PATH)
        else:
            log.info("♻️ Рестарт без изменений кода — рассылка пропущена.")

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
                content = "\n".join(lines)
                db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
                script_dir = os.path.dirname(os.path.abspath(__file__))
                for save_dir in {db_dir, script_dir}:
                    try:
                        with open(os.path.join(save_dir, "subs_backup.txt"), "w", encoding="utf-8") as f:
                            f.write(content)
                    except Exception:
                        pass
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
            content = "\n".join(lines)
            db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
            script_dir = os.path.dirname(os.path.abspath(__file__))
            for save_dir in {db_dir, script_dir}:
                try:
                    with open(os.path.join(save_dir, "subs_backup.txt"), "w", encoding="utf-8") as f:
                        f.write(content)
                except Exception:
                    pass
            log.info(f"💾 subs_backup.txt финальное сохранение ({len(lines)-1} подписок)")
        except Exception as exc:
            log.warning(f"subs_backup final save error: {exc}")

    async def _guarded(name: str, coro):
        """Оборачивает корутину: одна упавшая задача не убивает остальные."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.critical(f"💀 Задача '{name}' завершилась с необработанным исключением!", exc_info=True)

    try:
        await asyncio.gather(
            _guarded("polling",          dp.start_polling(bot, allowed_updates=["message", "callback_query"])),
            _guarded("scanner",          scanner.run_forever()),
            _guarded("pd_runner",        pd_runner.run_forever()),
            _guarded("turso_sync",       turso_sync.turso_sync_loop(config.DB_PATH)),
            _guarded("subs_backup",      _subs_backup_loop()),
            _guarded("cache_gc",         cache_gc.gc_loop()),
            _guarded("poly_digest",      poly_scheduler.digest_loop(bot, poly, um)),
            _guarded("poly_alerts",      poly_scheduler.alerts_loop(bot, poly)),
            _guarded("gerchik_scanner",  gerchik_scanner.run_forever()),
        )
    finally:
        log.info("🛑 Завершение — отменяем фоновые задачи...")
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        log.info("🛑 Закрываем соединения...")
        await scanner.fetcher.close()
        await bot.session.close()
        await poly.close()
        # ─── Финальное сохранение перед выходом ──────────────────────────────
        await _save_subs_backup_once()
        await turso_sync.turso_push(config.DB_PATH)


if __name__ == "__main__":
    asyncio.run(main())
