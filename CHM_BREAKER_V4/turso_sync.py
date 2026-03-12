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
  1. _backup_db(db_path)                            ← сначала бэкап локального
  2. database.init_db(db_path)                      ← создаём правильную схему
  3. restore_from_turso_if_needed(db_path)          ← пишем Turso-данные в схему
  4. asyncio.create_task(turso_sync_loop(db_path))  ← фоновый пуш

ИСПРАВЛЕНИЯ (v2):
  - turso_push теперь создаёт таблицы в Turso перед DELETE+INSERT
    (ранее таблиц в Turso не было → все пуши молча проваливались)
  - SYNC_TABLES расширен: добавлены trades, poly_wallets, poly_settings
  - restore не создаёт таблицы с типом TEXT — init_db запускается раньше
  - подробное логирование: количество строк по таблицам
"""

import asyncio
import logging
from typing import Optional
import os
import sqlite3

import aiohttp

log = logging.getLogger("CHM.Turso")

TURSO_URL     = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN   = os.getenv("TURSO_TOKEN", "").strip()
SYNC_INTERVAL = int(os.getenv("TURSO_SYNC_INTERVAL", "300"))  # секунд

# Таблицы для синхронизации (порядок важен — зависимые таблицы позже).
# trades включён: активные позиции нужны для корректного мониторинга после рестарта.
# poly_wallets включён: содержит зашифрованные ключи кастодиальных кошельков.
SYNC_TABLES = [
    "users", "trial_ids", "trades",
    "promo_codes", "promo_uses",
    "referrals", "ref_rewards",
    "pd_users", "kv",
    "poly_settings", "poly_wallets",
]

# Выставляется в True после завершения restore_from_turso_if_needed().
# Защита от "пустого пуша": sync_loop не стартует пуш пока restore не отработал.
_restore_attempted: bool = False


def is_configured() -> bool:
    return bool(TURSO_URL and TURSO_TOKEN)


def _is_configured() -> bool:
    """Псевдоним для обратной совместимости."""
    return is_configured()


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
        # bool перед int — иначе True/False попадут в ветку int
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        # Turso HTTP API требует value как строку даже для integer/float
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    return {"type": "text", "value": str(v)}


async def _pipeline(session: aiohttp.ClientSession, stmts: list[dict]) -> list:
    """
    Отправляет список SQL-запросов в Turso через pipeline API.
    Возвращает список результирующих строк (list[list[dict]]) — по одному на stmt.
    """
    if not stmts:
        return []

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


# ── Создание схемы в Turso ─────────────────────────────────────────────────

def _read_local_schema(db_path: str) -> list[str]:
    """
    Читает CREATE TABLE / CREATE INDEX SQL из sqlite_master для SYNC_TABLES.
    Возвращает список SQL-строк с IF NOT EXISTS.
    Вызывать в executor (блокирующий I/O).
    """
    stmts = []
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            for table in SYNC_TABLES:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if row and row[0]:
                    sql = row[0].strip()
                    if "IF NOT EXISTS" not in sql.upper():
                        sql = sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
                    stmts.append(sql)

                # Индексы для этой таблицы
                for idx_name, idx_sql in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                ).fetchall():
                    if idx_sql:
                        idx_sql = idx_sql.strip()
                        if "IF NOT EXISTS" not in idx_sql.upper():
                            idx_sql = idx_sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
                        stmts.append(idx_sql)
    except Exception as e:
        log.warning(f"_read_local_schema: {e}")
    return stmts


async def _ensure_turso_schema(session: aiohttp.ClientSession, db_path: str):
    """
    Создаёт таблицы и индексы в Turso если их нет.
    КРИТИЧНО: вызывать перед любым DELETE/INSERT в turso_push.
    """
    loop = asyncio.get_event_loop()
    schema_stmts = await loop.run_in_executor(None, _read_local_schema, db_path)
    if not schema_stmts:
        log.warning("_ensure_turso_schema: локальная схема пустая (init_db не запускался?)")
        return

    sql_stmts = [{"sql": s} for s in schema_stmts]
    # Tables first, indexes second (already ordered by _read_local_schema)
    table_stmts = [s for s in sql_stmts if "CREATE TABLE" in s["sql"]]
    idx_stmts   = [s for s in sql_stmts if "CREATE INDEX" in s["sql"]]

    if table_stmts:
        await _pipeline(session, table_stmts)
        log.debug(f"_ensure_turso_schema: создано/проверено {len(table_stmts)} таблиц")
    if idx_stmts:
        try:
            await _pipeline(session, idx_stmts)
        except Exception as e:
            log.debug(f"_ensure_turso_schema idx: {e}")


# ── Восстановление при старте ──────────────────────────────────────────────

async def restore_from_turso_if_needed(db_path: str) -> bool:
    """
    Восстанавливает локальный SQLite из Turso если облако непустое.

    ВАЖНО: вызывать ПОСЛЕ database.init_db() — таблицы должны
    уже существовать с правильными типами (INTEGER, REAL и т.д.).
    Если таблицы не существуют, создаёт их временно как TEXT и
    полагается на то что init_db исправит типы через ALTER TABLE.

    Возвращает True если данные были восстановлены.
    """
    global _restore_attempted
    _restore_attempted = True  # выставляем сразу — даже если вернём False

    if not is_configured():
        log.warning(
            "⚠️  Turso не настроен (TURSO_URL/TURSO_TOKEN не заданы) — "
            "облачное восстановление отключено. "
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
            log.info("⬇️  Turso: облако пустое, начинаем с локальной БД")
            return False

        # Подробная статистика по таблицам
        table_counts = {
            t: len(r) for t, r in zip(SYNC_TABLES, results) if r
        }
        n_tables = len(table_counts)
        details = ", ".join(f"{t}:{n}" for t, n in table_counts.items())
        log.info(f"⬇️  Turso: найдено {total} строк в {n_tables} таблицах ({details})")

        def _write_locally():
            with sqlite3.connect(db_path, timeout=30) as conn:
                # WAL совместим с aiosqlite который запустится позже
                conn.execute("PRAGMA journal_mode=WAL")
                for table, rows in zip(SYNC_TABLES, results):
                    if not rows:
                        continue
                    cols         = list(rows[0].keys())
                    col_names    = ", ".join(cols)
                    placeholders = ", ".join("?" for _ in cols)

                    # Если таблица не существует (restore до init_db),
                    # создаём временно с TEXT-типами; init_db потом мигрирует.
                    col_defs = ", ".join(f"{c} TEXT" for c in cols)
                    conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})"
                    )
                    # Очищаем локальные данные — Turso является источником правды
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


# ── Точечные запросы к Turso (для UI восстановления) ──────────────────────

async def turso_lookup_wallet(user_id: int) -> Optional[dict]:
    """
    Ищет кошелёк пользователя в Turso по user_id.
    Возвращает {'address': '0x...', 'encrypted_key': '...'} или None.

    Используется при ручном восстановлении кошелька через бота:
    пользователь вводит свой адрес → бот сверяет с Turso → восстанавливает.
    """
    if not is_configured():
        return None
    try:
        async with aiohttp.ClientSession() as session:
            results = await _pipeline(session, [{
                "sql": "SELECT address, encrypted_key FROM poly_wallets WHERE user_id=?",
                "args": [_arg(user_id)],
            }])
        if results and results[0]:
            return results[0][0]
        return None
    except Exception as e:
        log.warning(f"turso_lookup_wallet({user_id}): {e}")
        return None


# ── Обратная совместимость ─────────────────────────────────────────────────

async def turso_pull(db_path: str) -> bool:
    """Устаревший псевдоним. Использовать restore_from_turso_if_needed()."""
    return await restore_from_turso_if_needed(db_path)


# ── Push ───────────────────────────────────────────────────────────────────

async def turso_push(db_path: str) -> bool:
    """
    Пушит все SYNC_TABLES из локального SQLite в Turso.

    Исправления v2:
    - Сначала создаёт таблицы в Turso (_ensure_turso_schema) если их нет.
      Ранее DELETE/INSERT молча падали с "no such table" — данные не сохранялись.
    - Логирует количество строк по каждой таблице.
    - Батчи по 50 строк: DELETE+первые_50 в первом, остальные отдельно.
    """
    if not is_configured():
        return False

    if not _restore_attempted:
        log.warning(
            "⚠️  Turso push заблокирован: restore_from_turso_if_needed() "
            "не была вызвана при старте. Пуш пропущен."
        )
        return False

    try:
        def _read_locally() -> dict[str, list[dict]]:
            data: dict[str, list[dict]] = {}
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                for table in SYNC_TABLES:
                    try:
                        data[table] = [
                            dict(r) for r in conn.execute(f"SELECT * FROM {table}")
                        ]
                    except Exception:
                        data[table] = []
            return data

        loop = asyncio.get_event_loop()
        tables_data: dict[str, list[dict]] = await loop.run_in_executor(None, _read_locally)

        total_local = sum(len(v) for v in tables_data.values())

        async with aiohttp.ClientSession() as session:
            # ─── КРИТИЧНО: создаём таблицы в Turso если их нет ──────────────
            # Без этого шага все DELETE и INSERT молча падают с "no such table"
            # и функция ложно рапортует об успехе.
            await _ensure_turso_schema(session, db_path)

            total_pushed = 0
            for table, rows in tables_data.items():
                n = len(rows)

                if n == 0:
                    # Только очищаем таблицу в Turso
                    try:
                        await _pipeline(session, [{"sql": f"DELETE FROM {table}"}])
                    except Exception as e:
                        log.debug(f"Turso push {table} DELETE: {e}")
                    continue

                # Первый батч: DELETE + первые 50 строк
                batch: list[dict] = [{"sql": f"DELETE FROM {table}"}]
                _append_inserts(batch, table, rows[:50])
                try:
                    await _pipeline(session, batch)
                    total_pushed += min(n, 50)
                except Exception as e:
                    log.warning(f"Turso push {table} batch-0: {e}")
                    continue

                # Последующие батчи: только INSERT (DELETE уже был)
                for offset in range(50, n, 50):
                    chunk = rows[offset:offset + 50]
                    batch = []
                    _append_inserts(batch, table, chunk)
                    try:
                        await _pipeline(session, batch)
                        total_pushed += len(chunk)
                    except Exception as e:
                        log.warning(f"Turso push {table} batch-{offset}: {e}")

        log.info(
            f"☁️  Turso sync: сохранено {total_pushed}/{total_local} строк "
            f"({', '.join(f'{t}:{len(v)}' for t,v in tables_data.items() if v)})"
        )
        return True

    except Exception as exc:
        log.warning(f"Turso push error: {exc}")
        return False


def _append_inserts(batch: list[dict], table: str, rows: list[dict]):
    """Добавляет INSERT OR REPLACE statements в batch."""
    for row in rows:
        cols = list(row.keys())
        batch.append({
            "sql": (
                f"INSERT OR REPLACE INTO {table} "
                f"({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            ),
            "args": [_arg(row[c]) for c in cols],
        })


# ── Фоновый sync loop ──────────────────────────────────────────────────────

async def turso_sync_loop(db_path: str, initial_delay: int | None = None):
    """
    Фоновая задача: пушит БД в Turso каждые SYNC_INTERVAL секунд.

    initial_delay — пауза перед ПЕРВЫМ пушем цикла.
    По умолчанию 60с — первичный пуш уже выполнен в main() при старте,
    цикл нужен для последующей синхронизации изменений.

    ВАЖНО: запускать только ПОСЛЕ вызова restore_from_turso_if_needed().
    """
    if not is_configured():
        log.info("Turso sync loop отключён (TURSO_URL/TURSO_TOKEN не заданы)")
        return

    if initial_delay is None:
        initial_delay = 60  # первичный пуш уже выполнен в main()

    log.info(
        f"☁️  Turso sync loop запущен "
        f"(интервал {SYNC_INTERVAL}с, первый пуш через {initial_delay}с)"
    )
    await asyncio.sleep(initial_delay)
    await turso_push(db_path)
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        await turso_push(db_path)
