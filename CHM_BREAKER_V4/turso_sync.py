"""
turso_sync.py — резервное копирование SQLite в Turso через HTTP API.

ПОЧЕМУ HTTP API, а не libsql embedded replica:
  aiosqlite пишет в локальный файл напрямую через стандартный sqlite3.
  libsql embedded replica видит ТОЛЬКО свои транзакции — записи aiosqlite
  для неё невидимы и в Turso не попадают. conn.sync() = только PULL, не PUSH.
  HTTP API работает правильно: читаем локальный SQLite → отправляем в Turso.

Настройка (bothost.ru → Settings → Variables):
  TURSO_URL    = libsql://your-db-name-orgname.turso.io
  TURSO_TOKEN  = eyJ...токен из Turso Dashboard...

Если переменные не заданы — модуль молча отключается.
"""

import asyncio
import json
import logging
import os
import sqlite3

import aiohttp

log = logging.getLogger("CHM.Turso")

TURSO_URL     = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN   = os.getenv("TURSO_TOKEN", "").strip()
SYNC_INTERVAL = int(os.getenv("TURSO_SYNC_INTERVAL", "300"))  # секунд

# Таблицы для синхронизации (порядок важен — users перед pd_users и т.п.)
SYNC_TABLES = [
    "users", "trial_ids", "promo_codes", "promo_uses",
    "referrals", "ref_rewards", "pd_users", "kv",
]


def _is_configured() -> bool:
    return bool(TURSO_URL and TURSO_TOKEN)


def _http_url() -> str:
    """Конвертирует libsql:// → https:// для HTTP API."""
    url = TURSO_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url.rstrip("/")


def _arg(v) -> dict:
    """Конвертирует Python-значение в Turso API arg."""
    if v is None:
        return {"type": "null", "value": None}
    if isinstance(v, bool):
        return {"type": "integer", "value": 1 if v else 0}
    if isinstance(v, int):
        return {"type": "integer", "value": v}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


async def _pipeline(session: aiohttp.ClientSession, stmts: list[dict]) -> list:
    """
    Отправляет список SQL-запросов в Turso через pipeline API.
    stmts: [{"sql": "...", "args": [...]}, ...]
    Возвращает список результирующих строк (list[list[dict]]) — по одному на stmt.
    """
    requests = [{"type": "execute", "stmt": s} for s in stmts]
    requests.append({"type": "close"})

    url = f"{_http_url()}/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json",
    }

    async with session.post(
        url, json={"requests": requests}, headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        if resp.status not in (200, 207):
            text = await resp.text()
            raise RuntimeError(f"Turso HTTP {resp.status}: {text[:300]}")
        data = await resp.json()

    results = []
    for item in data.get("results", []):
        if item.get("type") == "error":
            msg = item.get("error", {}).get("message", "")
            if "no such table" not in msg.lower():
                log.debug(f"Turso stmt error: {msg}")
            results.append([])
            continue
        r = item.get("response", {}).get("result", {})
        cols = [c["name"] for c in r.get("cols", [])]
        rows = []
        for raw_row in r.get("rows", []):
            row = {}
            for i, col in enumerate(cols):
                cell = raw_row[i]
                row[col] = cell.get("value") if isinstance(cell, dict) else cell
            rows.append(row)
        results.append(rows)
    return results


async def turso_pull(db_path: str) -> bool:
    """
    Восстанавливает таблицы из Turso в локальный SQLite.
    Вызывать ПОСЛЕ database.init_db() — таблицы уже должны существовать.
    """
    if not _is_configured():
        log.warning("⚠️  Turso не настроен (TURSO_URL/TURSO_TOKEN не заданы) — облачное восстановление отключено. "
                    "Подписки сохраняются ТОЛЬКО в локальном SQLite + subs_backup.txt. "
                    "При редеплое (пересборке контейнера) данные могут быть потеряны!")
        return False

    log.info("⬇️  Turso: загружаем БД из облака...")

    try:
        async with aiohttp.ClientSession() as session:
            stmts = [{"sql": f"SELECT * FROM {t}"} for t in SYNC_TABLES]
            results = await _pipeline(session, stmts)

        total = sum(len(r) for r in results)
        if total == 0:
            log.info("⬇️  Turso: облако пустое, начинаем с чистой БД")
            return False

        with sqlite3.connect(db_path) as conn:
            for table, rows in zip(SYNC_TABLES, results):
                if not rows:
                    continue
                cols = list(rows[0].keys())
                col_names = ", ".join(cols)
                placeholders = ", ".join(["?" for _ in cols])
                conn.execute(f"DELETE FROM {table}")
                for row in rows:
                    vals = [row[c] for c in cols]
                    conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                        vals,
                    )
            conn.commit()

        log.info(f"✅ Turso: восстановлено {total} строк из облака")
        return True
    except Exception as exc:
        log.warning(f"Turso pull error: {exc}")
        return False


async def turso_push(db_path: str) -> bool:
    """
    Пушит все критические таблицы из локального SQLite в Turso.
    Безопасно вызывать в фоне во время работы бота.
    """
    if not _is_configured():
        return False

    try:
        # Читаем локальный SQLite
        tables_data: dict[str, list[dict]] = {}
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            for table in SYNC_TABLES:
                try:
                    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
                    tables_data[table] = rows
                except Exception:
                    tables_data[table] = []

        total = sum(len(v) for v in tables_data.values())

        async with aiohttp.ClientSession() as session:
            for table, rows in tables_data.items():
                stmts: list[dict] = [{"sql": f"DELETE FROM {table}"}]
                # Батчи по 50 строк чтобы не превысить лимит запроса
                for i in range(0, len(rows), 50):
                    for row in rows[i:i + 50]:
                        cols = list(row.keys())
                        stmts.append({
                            "sql": (
                                f"INSERT OR REPLACE INTO {table} "
                                f"({', '.join(cols)}) VALUES ({', '.join(['?' for _ in cols])})"
                            ),
                            "args": [_arg(row[c]) for c in cols],
                        })
                    # Флашим батч
                    try:
                        await _pipeline(session, stmts)
                    except Exception as e:
                        log.debug(f"Turso push {table} batch: {e}")
                    stmts = []

        log.info(f"☁️  Turso: сохранено {total} строк в облако")
        return True
    except Exception as exc:
        log.warning(f"Turso push error: {exc}")
        return False


async def turso_sync_loop(db_path: str):
    """
    Фоновая задача: пушит БД в Turso каждые SYNC_INTERVAL секунд.
    Первый пуш через 15 секунд после старта (не через SYNC_INTERVAL)
    чтобы данные попали в облако до возможного SIGKILL.
    """
    if not _is_configured():
        log.info("Turso sync loop отключён (TURSO_URL/TURSO_TOKEN не заданы)")
        return

    log.info(f"☁️  Turso sync loop запущен (интервал {SYNC_INTERVAL}с, первый пуш через 15с)")
    await asyncio.sleep(15)
    await turso_push(db_path)
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        await turso_push(db_path)
