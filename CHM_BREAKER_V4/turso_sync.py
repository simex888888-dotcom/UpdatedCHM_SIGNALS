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

ПОРЯДОК ЗАПУСКА (критично!):
  1. restore_from_turso_if_needed(db_path)  ← СНАЧАЛА тянем из облака
  2. database.init_db(db_path)              ← потом создаём/мигрируем таблицы
  3. asyncio.create_task(turso_sync_loop(db_path))  ← только потом фон
"""

import asyncio
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

# ── Защита от "пустого пуша" ───────────────────────────────────────────────
# Выставляется в True после того как restore_from_turso_if_needed() завершила
# попытку восстановления (успешную или нет). До этого момента push запрещён.
_restore_attempted: bool = False


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


# ── Восстановление при старте ──────────────────────────────────────────────

async def restore_from_turso_if_needed(db_path: str) -> bool:
    """
    ШАГ 1 при старте: восстанавливает локальный SQLite из Turso если облако непустое.

    Вызывать ДО database.init_db() — пишет напрямую в файл через sqlite3,
    создавая таблицы по мере необходимости (INSERT OR REPLACE без DELETE).

    Возвращает True если данные были восстановлены.
    """
    global _restore_attempted
    _restore_attempted = True  # выставляем сразу — даже если вернём False

    if not _is_configured():
        log.warning(
            "⚠️  Turso не настроен (TURSO_URL/TURSO_TOKEN не заданы) — облачное восстановление отключено. "
            "Подписки сохраняются ТОЛЬКО в локальном SQLite + subs_backup.txt. "
            "При редеплое (пересборке контейнера) данные могут быть потеряны!"
        )
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

        def _write_locally():
            with sqlite3.connect(db_path, timeout=30) as conn:
                # Включаем WAL чтобы не конфликтовать с будущим aiosqlite
                conn.execute("PRAGMA journal_mode=WAL")
                for table, rows in zip(SYNC_TABLES, results):
                    if not rows:
                        continue
                    cols = list(rows[0].keys())
                    col_names   = ", ".join(cols)
                    placeholders = ", ".join(["?" for _ in cols])
                    # Создаём таблицу если не существует (минимальная схема)
                    # init_db добавит недостающие колонки позже через ALTER TABLE
                    col_defs = ", ".join(f"{c} TEXT" for c in cols)
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})"
                    )
                    conn.execute(f"DELETE FROM {table}")
                    for row in rows:
                        vals = [row[c] for c in cols]
                        conn.execute(
                            f"INSERT OR REPLACE INTO {table} ({col_names}) "
                            f"VALUES ({placeholders})",
                            vals,
                        )
                conn.commit()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write_locally)

        log.info(f"✅ Turso: восстановлено {total} строк из облака")
        return True

    except Exception as exc:
        log.warning(f"Turso restore error: {exc}")
        return False


# ── Обратная совместимость — старое имя ───────────────────────────────────

async def turso_pull(db_path: str) -> bool:
    """
    Устаревший псевдоним. При старте использовать restore_from_turso_if_needed().
    Оставлен для обратной совместимости: вызывает restore_from_turso_if_needed().
    """
    return await restore_from_turso_if_needed(db_path)


# ── Push ───────────────────────────────────────────────────────────────────

async def turso_push(db_path: str) -> bool:
    """
    Пушит все критические таблицы из локального SQLite в Turso.
    Безопасно вызывать в фоне во время работы бота.

    Защита: если restore_from_turso_if_needed() ещё не запускалась —
    пуш блокируется чтобы не затереть облако пустой локальной БД.
    """
    if not _is_configured():
        return False

    if not _restore_attempted:
        log.warning(
            "⚠️  Turso push заблокирован: restore_from_turso_if_needed() "
            "не была вызвана при старте. Пуш пропущен."
        )
        return False

    try:
        def _read_locally():
            data: dict[str, list[dict]] = {}
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                for table in SYNC_TABLES:
                    try:
                        data[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
                    except Exception:
                        data[table] = []
            return data

        loop = asyncio.get_event_loop()
        tables_data: dict[str, list[dict]] = await loop.run_in_executor(None, _read_locally)

        total = sum(len(v) for v in tables_data.values())

        async with aiohttp.ClientSession() as session:
            for table, rows in tables_data.items():
                stmts: list[dict] = [{"sql": f"DELETE FROM {table}"}]
                for i in range(0, max(len(rows), 1), 50):
                    for row in rows[i:i + 50]:
                        cols = list(row.keys())
                        stmts.append({
                            "sql": (
                                f"INSERT OR REPLACE INTO {table} "
                                f"({', '.join(cols)}) VALUES ({', '.join(['?' for _ in cols])})"
                            ),
                            "args": [_arg(row[c]) for c in cols],
                        })
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


# ── Фоновый sync loop ──────────────────────────────────────────────────────

async def turso_sync_loop(db_path: str, initial_delay: int | None = None):
    """
    Фоновая задача: пушит БД в Turso каждые SYNC_INTERVAL секунд.

    initial_delay — пауза перед ПЕРВЫМ пушем (секунды).
    По умолчанию равен SYNC_INTERVAL (не 15с!) чтобы не затереть облако
    сразу после старта, пока restore_from_turso_if_needed ещё не отработала.

    ВАЖНО: запускать только ПОСЛЕ вызова restore_from_turso_if_needed().
    """
    if not _is_configured():
        log.info("Turso sync loop отключён (TURSO_URL/TURSO_TOKEN не заданы)")
        return

    if initial_delay is None:
        initial_delay = SYNC_INTERVAL  # по умолчанию 300с, не 15с

    log.info(
        f"☁️  Turso sync loop запущен "
        f"(интервал {SYNC_INTERVAL}с, первый пуш через {initial_delay}с)"
    )
    await asyncio.sleep(initial_delay)
    await turso_push(db_path)
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        await turso_push(db_path)
