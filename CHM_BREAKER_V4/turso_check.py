"""
turso_check.py — диагностика состояния Turso и локального SQLite.

Запуск на Bothost (через консоль контейнера) или локально:
  python turso_check.py                   # только Turso (env vars)
  python turso_check.py /data/chm_bot.db  # Turso + сравнение с локальной БД

Показывает:
  - Подключение к Turso работает?
  - Какие таблицы есть в Turso и сколько строк?
  - Сравнение с локальной БД (если путь передан)
  - Несколько строк из ключевых таблиц (users, kv)
"""

import asyncio
import os
import sqlite3
import sys

import aiohttp

TURSO_URL   = os.getenv("TURSO_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "").strip()

SYNC_TABLES = [
    "users", "trial_ids", "trades",
    "promo_codes", "promo_uses",
    "referrals", "ref_rewards",
    "pd_users", "kv",
    "poly_settings", "poly_wallets",
]


def _http_url(url: str) -> str:
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url.rstrip("/")


async def _pipeline(session, base_url, token, stmts):
    if not stmts:
        return []
    requests = [{"type": "execute", "stmt": s} for s in stmts]
    requests.append({"type": "close"})
    async with session.post(
        f"{base_url}/v2/pipeline",
        json={"requests": requests},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status not in (200, 207):
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
        data = await resp.json()

    results = []
    for item in data.get("results", []):
        if item.get("type") == "error":
            results.append(None)  # таблица не существует или ошибка
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


async def check(db_path: str | None):
    print(f"\n{'='*60}")
    print("  CHM Turso Diagnostic")
    print(f"{'='*60}\n")

    # ── Конфигурация ────────────────────────────────────────────────────────
    if not TURSO_URL or not TURSO_TOKEN:
        print("❌ TURSO_URL и/или TURSO_TOKEN не заданы!")
        print("   Установите env vars и повторите.")
        return

    print(f"🔗 TURSO_URL:   {TURSO_URL}")
    print(f"🔑 TURSO_TOKEN: {'*' * 8}{TURSO_TOKEN[-6:] if len(TURSO_TOKEN) > 6 else '???'}")
    base_url = _http_url(TURSO_URL)

    # ── Тест подключения ────────────────────────────────────────────────────
    print("\n📡 Проверяем подключение к Turso...")
    async with aiohttp.ClientSession() as session:
        try:
            result = await _pipeline(session, base_url, TURSO_TOKEN,
                                     [{"sql": "SELECT 1 as ok"}])
            if result and result[0] and result[0][0].get("ok") == "1":
                print("   ✅ Подключение работает")
            else:
                print(f"   ⚠️  Неожиданный ответ: {result}")
        except Exception as e:
            print(f"   ❌ Ошибка подключения: {e}")
            return

        # ── Таблицы в Turso ─────────────────────────────────────────────────
        print("\n📋 Таблицы в Turso:")
        count_stmts = [{"sql": f"SELECT COUNT(*) as n FROM {t}"} for t in SYNC_TABLES]
        count_results = await _pipeline(session, base_url, TURSO_TOKEN, count_stmts)

        turso_counts: dict[str, int] = {}
        for table, res in zip(SYNC_TABLES, count_results):
            if res is None:
                print(f"   ❌ {table}: таблица не существует в Turso!")
                turso_counts[table] = -1
            elif res:
                n = int(res[0].get("n", 0))
                turso_counts[table] = n
                status = "✅" if n > 0 else "○ "
                print(f"   {status} {table}: {n} строк")
            else:
                turso_counts[table] = 0
                print(f"   ○  {table}: 0 строк")

        total_turso = sum(v for v in turso_counts.values() if v >= 0)
        tables_missing = [t for t, v in turso_counts.items() if v == -1]

        if tables_missing:
            print(f"\n   ⚠️  Таблиц нет в Turso: {tables_missing}")
            print("   → Запустите migrate_to_turso.py для первичной миграции")

        # ── Выборка из users ─────────────────────────────────────────────────
        print("\n👥 Примеры из users (первые 5):")
        try:
            res = await _pipeline(session, base_url, TURSO_TOKEN,
                                  [{"sql": "SELECT user_id, username, sub_status, sub_expires FROM users LIMIT 5"}])
            if res and res[0]:
                for row in res[0]:
                    import datetime
                    exp = float(row.get("sub_expires", 0) or 0)
                    exp_str = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp > 0 else "—"
                    print(f"   • uid={row.get('user_id')} @{row.get('username','?')} "
                          f"status={row.get('sub_status')} expires={exp_str}")
            else:
                print("   (пусто)")
        except Exception as e:
            print(f"   ⚠️  {e}")

        # ── Выборка из kv ────────────────────────────────────────────────────
        print("\n🗝️  Содержимое kv:")
        try:
            res = await _pipeline(session, base_url, TURSO_TOKEN,
                                  [{"sql": "SELECT key, value FROM kv LIMIT 10"}])
            if res and res[0]:
                for row in res[0]:
                    val = str(row.get("value", ""))[:60]
                    print(f"   • {row.get('key')}: {val}")
            else:
                print("   (пусто)")
        except Exception as e:
            print(f"   ⚠️  {e}")

    # ── Локальная БД ─────────────────────────────────────────────────────────
    if db_path:
        print(f"\n💾 Локальная БД: {db_path}")
        if not os.path.exists(db_path):
            print("   ❌ Файл не найден!")
        else:
            size_mb = os.path.getsize(db_path) / 1024 / 1024
            print(f"   Размер: {size_mb:.2f} MB")
            try:
                with sqlite3.connect(db_path, timeout=10) as conn:
                    print("\n   Сравнение local vs turso:")
                    all_match = True
                    for table in SYNC_TABLES:
                        try:
                            n_local = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        except sqlite3.OperationalError:
                            n_local = -1
                        n_turso = turso_counts.get(table, -1)
                        if n_local == n_turso:
                            status = "✅"
                        elif n_turso == -1:
                            status = "❌ нет в Turso"
                            all_match = False
                        elif n_local == -1:
                            status = "❌ нет в local"
                            all_match = False
                        else:
                            status = f"⚠️  local={n_local} vs turso={n_turso}"
                            all_match = False
                        if n_local > 0 or n_turso > 0:
                            print(f"   {status}   {table}: local={n_local}, turso={n_turso}")
                    if all_match:
                        print("   ✅ Все таблицы синхронизированы!")
            except Exception as e:
                print(f"   ❌ Ошибка чтения локальной БД: {e}")

    print(f"\n{'='*60}")
    print(f"  Итого строк в Turso: {total_turso}")
    if total_turso == 0:
        print("  ⚠️  Turso пустой! Запустите migrate_to_turso.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(check(db_path))
