"""
turso_sync.py — синхронизация SQLite с Turso (облачный SQLite).

Принцип работы (embedded replica):
  1. При старте: скачивает последнюю версию БД из Turso → локальный файл.
  2. В фоне: каждые SYNC_INTERVAL секунд пушит изменения в Turso.
  3. При завершении: финальный пуш.

aiosqlite при этом работает ПОЛНОСТЬЮ без изменений — читает/пишет
тот же локальный .db файл. Turso лишь зеркалирует его в облако.

Настройка (переменные окружения в bothost.ru → Settings → Variables):
  TURSO_URL    = libsql://your-db-name.turso.io
  TURSO_TOKEN  = eyJ...    (токен доступа из Turso Dashboard)

Если переменные не заданы — модуль молча отключается, бот работает
как раньше (только локально).
"""

import asyncio
import logging
import os

log = logging.getLogger("CHM.Turso")

TURSO_URL    = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN  = os.getenv("TURSO_TOKEN", "").strip()
SYNC_INTERVAL = int(os.getenv("TURSO_SYNC_INTERVAL", "300"))  # секунд


def _is_configured() -> bool:
    return bool(TURSO_URL and TURSO_TOKEN)


def _do_sync(db_path: str) -> bool:
    """
    Синхронный вызов libsql_experimental.
    Открывает локальный db_path как embedded replica, выполняет sync(),
    затем закрывает соединение. Безопасно вызывать пока aiosqlite не работает.
    """
    try:
        import libsql_experimental as libsql  # pip install libsql-experimental
    except ImportError:
        log.error(
            "libsql-experimental не установлен. "
            "Выполните: pip install libsql-experimental"
        )
        return False
    try:
        conn = libsql.connect(db_path, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.sync()
        conn.close()
        return True
    except Exception as exc:
        log.warning(f"Turso sync error: {exc}")
        return False


async def turso_pull(db_path: str) -> bool:
    """
    Скачивает последнюю версию БД из Turso перед стартом бота.
    Вызывать ДО database.init_db().
    """
    if not _is_configured():
        log.debug("Turso не настроен — пропускаем pull")
        return False

    log.info("⬇️  Turso: загружаем БД из облака...")
    ok = await asyncio.to_thread(_do_sync, db_path)
    if ok:
        log.info("✅ Turso: БД восстановлена из облака")
    else:
        log.warning("⚠️  Turso: pull не удался — работаем с локальной копией")
    return ok


async def turso_push(db_path: str) -> bool:
    """
    Пушит текущую БД в Turso.
    Вызывать когда aiosqlite не держит активных транзакций (безопасно в idle).
    """
    if not _is_configured():
        return False

    ok = await asyncio.to_thread(_do_sync, db_path)
    if ok:
        log.info("☁️  Turso: БД сохранена в облако")
    return ok


async def turso_sync_loop(db_path: str):
    """
    Фоновая задача: пушит БД в Turso каждые SYNC_INTERVAL секунд.
    Запускать через asyncio.gather() вместе с остальными задачами бота.
    """
    if not _is_configured():
        log.info("Turso sync loop отключён (TURSO_URL/TURSO_TOKEN не заданы)")
        return

    log.info(f"☁️  Turso sync loop запущен (интервал {SYNC_INTERVAL}с)")
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        await turso_push(db_path)
