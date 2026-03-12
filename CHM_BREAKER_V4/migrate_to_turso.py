"""
migrate_to_turso.py — одноразовая миграция локального SQLite в Turso.

Запуск:
  python migrate_to_turso.py                         # использует env TURSO_URL/TURSO_TOKEN
  python migrate_to_turso.py /data/chm_bot.db        # явный путь к БД

Скрипт:
  1. Читает из локального SQLite все таблицы из SYNC_TABLES.
  2. Создаёт таблицы в Turso (из sqlite_master схемы).
  3. Пушит данные батчами по 50 строк (без .dump, чисто SELECT/INSERT).
  4. Верифицирует: считает строки в Turso и сравнивает с локальными.

Требования: pip install aiohttp
"""

import asyncio
import os
import sqlite3
import sys

import aiohttp

# ── Конфигурация ────────────────────────────────────────────────────────────

TURSO_URL   = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "").strip()

SYNC_TABLES = [
    "users", "trial_ids", "trades",
    "promo_codes", "promo_uses",
    "referrals", "ref_rewards",
    "pd_users", "kv",
    "poly_settings", "poly_wallets",
]

BATCH_SIZE = 50


# ── Helpers ─────────────────────────────────────────────────────────────────

def _http_url(turso_url: str) -> str:
    if turso_url.startswith("libsql://"):
        return "https://" + turso_url[len("libsql://"):]
    if turso_url.startswith("http://"):
        return "https://" + turso_url[len("http://"):]
    return turso_url.rstrip("/")


def _arg(v) -> dict:
    if v is None:
        return {"type": "null", "value": None}
    if isinstance(v, bool):
        return {"type": "integer", "value": 1 if v else 0}
    if isinstance(v, int):
        return {"type": "integer", "value": v}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


async def _pipeline(session: aiohttp.ClientSession, base_url: str, token: str,
                    stmts: list[dict]) -> list:
    if not stmts:
        return []
    requests = [{"type": "execute", "stmt": s} for s in stmts]
    requests.append({"type": "close"})

    async with session.post(
        f"{base_url}/v2/pipeline",
        json={"requests": requests},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=120),
    ) as resp:
        if resp.status not in (200, 207):
            text = await resp.text()
            raise RuntimeError(f"Turso HTTP {resp.status}: {text[:500]}")
        data = await resp.json()

    results = []
    for item in data.get("results", []):
        if item.get("type") == "error":
            msg = item.get("error", {}).get("message", "?")
            raise RuntimeError(f"Turso stmt error: {msg}")
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


# ── Миграция ─────────────────────────────────────────────────────────────────

def read_local_db(db_path: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """
    Читает данные и схему из локального SQLite.
    Возвращает (tables_data, schema_sql).
    schema_sql: {table_name: "CREATE TABLE IF NOT EXISTS ..."}
    """
    tables_data: dict[str, list[dict]] = {}
    schema_sql: dict[str, str] = {}

    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row

        for table in SYNC_TABLES:
            # Схема
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row and row[0]:
                sql = row[0].strip()
                if "IF NOT EXISTS" not in sql.upper():
                    sql = sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
                schema_sql[table] = sql

            # Данные
            try:
                tables_data[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            except sqlite3.OperationalError:
                tables_data[table] = []
                print(f"  ⚠️  Таблица '{table}' не найдена в локальной БД — пропускаем")

        # Индексы
        for table in SYNC_TABLES:
            for idx_name, idx_sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (table,),
            ).fetchall():
                if idx_sql:
                    idx_sql = idx_sql.strip()
                    if "IF NOT EXISTS" not in idx_sql.upper():
                        idx_sql = idx_sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
                    schema_sql[f"__idx_{idx_name}"] = idx_sql

    return tables_data, schema_sql


async def migrate(db_path: str, turso_url: str, turso_token: str):
    base_url = _http_url(turso_url)
    print(f"\n{'='*60}")
    print(f"  Миграция: {db_path} → {turso_url}")
    print(f"{'='*60}\n")

    # ── Шаг 1: Читаем локальную БД ───────────────────────────────────────────
    print("📖 Читаем локальную SQLite...")
    tables_data, schema_sql = read_local_db(db_path)

    total_local = sum(len(v) for v in tables_data.values())
    print(f"   Найдено строк: {total_local}")
    for table in SYNC_TABLES:
        n = len(tables_data.get(table, []))
        if n:
            print(f"   • {table}: {n} строк")

    if total_local == 0:
        print("\n⚠️  Локальная БД пустая. Нечего мигрировать.")
        return

    # ── Шаг 2: Создаём таблицы в Turso ───────────────────────────────────────
    print("\n🏗️  Создаём таблицы в Turso...")
    async with aiohttp.ClientSession() as session:
        table_stmts = [
            {"sql": sql}
            for key, sql in schema_sql.items()
            if not key.startswith("__idx_")
        ]
        idx_stmts = [
            {"sql": sql}
            for key, sql in schema_sql.items()
            if key.startswith("__idx_")
        ]
        if table_stmts:
            await _pipeline(session, base_url, turso_token, table_stmts)
            print(f"   Создано/проверено таблиц: {len(table_stmts)}")
        if idx_stmts:
            try:
                await _pipeline(session, base_url, turso_token, idx_stmts)
                print(f"   Создано/проверено индексов: {len(idx_stmts)}")
            except Exception as e:
                print(f"   ⚠️  Индексы: {e} (не критично)")

        # ── Шаг 3: Пушим данные ───────────────────────────────────────────────
        print("\n⬆️  Пушим данные в Turso...")
        total_pushed = 0
        for table in SYNC_TABLES:
            rows = tables_data.get(table, [])
            n = len(rows)
            if n == 0:
                # Очищаем таблицу в Turso если она есть
                try:
                    await _pipeline(session, base_url, turso_token,
                                    [{"sql": f"DELETE FROM {table}"}])
                except Exception:
                    pass
                continue

            # Первый батч: DELETE + первые BATCH_SIZE строк
            batch: list[dict] = [{"sql": f"DELETE FROM {table}"}]
            for row in rows[:BATCH_SIZE]:
                cols = list(row.keys())
                batch.append({
                    "sql": (
                        f"INSERT OR REPLACE INTO {table} "
                        f"({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
                    ),
                    "args": [_arg(row[c]) for c in cols],
                })
            await _pipeline(session, base_url, turso_token, batch)
            pushed = min(n, BATCH_SIZE)

            # Последующие батчи (без DELETE)
            for offset in range(BATCH_SIZE, n, BATCH_SIZE):
                chunk = rows[offset:offset + BATCH_SIZE]
                batch = []
                for row in chunk:
                    cols = list(row.keys())
                    batch.append({
                        "sql": (
                            f"INSERT OR REPLACE INTO {table} "
                            f"({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
                        ),
                        "args": [_arg(row[c]) for c in cols],
                    })
                await _pipeline(session, base_url, turso_token, batch)
                pushed += len(chunk)

            print(f"   ✅ {table}: {pushed}/{n} строк")
            total_pushed += pushed

        # ── Шаг 4: Верификация ────────────────────────────────────────────────
        print(f"\n🔍 Верификация (считаем строки в Turso)...")
        verify_stmts = [{"sql": f"SELECT COUNT(*) as n FROM {t}"} for t in SYNC_TABLES]
        verify_results = await _pipeline(session, base_url, turso_token, verify_stmts)

        total_turso = 0
        mismatch = False
        for table, result in zip(SYNC_TABLES, verify_results):
            n_turso = int(result[0]["n"]) if result else 0
            n_local = len(tables_data.get(table, []))
            total_turso += n_turso
            status = "✅" if n_turso == n_local else "⚠️ "
            if n_turso != n_local:
                mismatch = True
            if n_local > 0 or n_turso > 0:
                print(f"   {status} {table}: local={n_local}, turso={n_turso}")

        print(f"\n{'='*60}")
        if mismatch:
            print(f"  ⚠️  Расхождение! local={total_local}, turso={total_turso}")
        else:
            print(f"  ✅ Миграция успешна! {total_turso} строк в Turso")
        print(f"{'='*60}\n")


# ── Точка входа ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TURSO_URL or not TURSO_TOKEN:
        print("❌ Не заданы TURSO_URL и/или TURSO_TOKEN (env vars)")
        print("   export TURSO_URL='libsql://your-db.turso.io'")
        print("   export TURSO_TOKEN='eyJ...'")
        sys.exit(1)

    db_path = sys.argv[1] if len(sys.argv) > 1 else "/data/chm_bot.db"
    if not os.path.exists(db_path):
        print(f"❌ Файл БД не найден: {db_path}")
        sys.exit(1)

    asyncio.run(migrate(db_path, TURSO_URL, TURSO_TOKEN))
