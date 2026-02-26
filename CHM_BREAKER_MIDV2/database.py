"""
database.py — SQLite через aiosqlite
Не требует сервера. Идеально для 50-500 пользователей.
Выдерживает ~1000 запросов/сек на запись, ~10k на чтение.
"""

import aiosqlite
import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("CHM.DB")

_db_path: str = "chm_bot.db"
_lock = asyncio.Lock()   # SQLite не любит параллельные записи


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=10000;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS users (
    user_id          INTEGER PRIMARY KEY,
    username         TEXT    DEFAULT '',
    active           INTEGER DEFAULT 0,

    sub_status       TEXT    DEFAULT 'trial',
    sub_expires      REAL    DEFAULT 0,
    trial_started    REAL    DEFAULT 0,
    trial_used       INTEGER DEFAULT 0,

    timeframe        TEXT    DEFAULT '1h',
    scan_interval    INTEGER DEFAULT 3600,

    pivot_strength   INTEGER DEFAULT 7,
    max_level_age    INTEGER DEFAULT 100,
    max_retest_bars  INTEGER DEFAULT 30,
    zone_buffer      REAL    DEFAULT 0.3,

    ema_fast         INTEGER DEFAULT 50,
    ema_slow         INTEGER DEFAULT 200,
    htf_ema_period   INTEGER DEFAULT 50,

    rsi_period       INTEGER DEFAULT 14,
    rsi_ob           INTEGER DEFAULT 65,
    rsi_os           INTEGER DEFAULT 35,
    vol_mult         REAL    DEFAULT 1.0,
    vol_len          INTEGER DEFAULT 20,

    use_rsi          INTEGER DEFAULT 1,
    use_volume       INTEGER DEFAULT 1,
    use_pattern      INTEGER DEFAULT 0,
    use_htf          INTEGER DEFAULT 0,
    use_session      INTEGER DEFAULT 0,
    smc_use_bos      INTEGER DEFAULT 1,
    smc_use_ob       INTEGER DEFAULT 1,
    smc_use_fvg      INTEGER DEFAULT 0,
    smc_use_sweep    INTEGER DEFAULT 0,
    smc_use_choch    INTEGER DEFAULT 0,
    smc_use_conf     INTEGER DEFAULT 0,

    atr_period       INTEGER DEFAULT 14,
    atr_mult         REAL    DEFAULT 1.0,
    max_risk_pct     REAL    DEFAULT 1.5,

    tp1_rr           REAL    DEFAULT 0.8,
    tp2_rr           REAL    DEFAULT 1.5,
    tp3_rr           REAL    DEFAULT 2.5,

    min_volume_usdt  REAL    DEFAULT 1000000,
    min_quality      INTEGER DEFAULT 2,
    cooldown_bars    INTEGER DEFAULT 5,

    notify_signal    INTEGER DEFAULT 1,
    notify_breakout  INTEGER DEFAULT 0,

    scan_mode        TEXT    DEFAULT 'both',
    long_tf          TEXT    DEFAULT '1h',
    long_interval    INTEGER DEFAULT 3600,
    short_tf         TEXT    DEFAULT '1h',
    short_interval   INTEGER DEFAULT 3600,

    long_active      INTEGER DEFAULT 0,
    short_active     INTEGER DEFAULT 0,
    long_cfg         TEXT    DEFAULT '{}',
    short_cfg        TEXT    DEFAULT '{}',

    signals_received INTEGER DEFAULT 0,
    created_at       REAL    DEFAULT 0,
    updated_at       REAL    DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id      TEXT    PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    entry         REAL    NOT NULL,
    sl            REAL    NOT NULL,
    tp1           REAL    NOT NULL,
    tp2           REAL    NOT NULL,
    tp3           REAL    NOT NULL,
    tp1_rr        REAL    DEFAULT 0.8,
    tp2_rr        REAL    DEFAULT 1.5,
    tp3_rr        REAL    DEFAULT 2.5,
    quality       INTEGER DEFAULT 1,
    timeframe     TEXT    DEFAULT '1h',
    breakout_type TEXT    DEFAULT '',
    result        TEXT    DEFAULT '',
    result_rr     REAL    DEFAULT 0,
    created_at    REAL    DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(active, sub_status, sub_expires);
CREATE INDEX IF NOT EXISTS idx_users_tf ON users(timeframe);
"""


async def init_db(path: str):
    global _db_path
    _db_path = path
    async with aiosqlite.connect(_db_path) as db:
        await db.executescript(SCHEMA)
        # Миграция: добавляем новые колонки если их нет (для существующих БД)
        migrations = [
            "ALTER TABLE users ADD COLUMN scan_mode TEXT DEFAULT 'both'",
            "ALTER TABLE users ADD COLUMN long_tf TEXT DEFAULT '1h'",
            "ALTER TABLE users ADD COLUMN long_interval INTEGER DEFAULT 3600",
            "ALTER TABLE users ADD COLUMN short_tf TEXT DEFAULT '1h'",
            "ALTER TABLE users ADD COLUMN short_interval INTEGER DEFAULT 3600",
            "ALTER TABLE users ADD COLUMN long_active INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN short_active INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN long_cfg TEXT DEFAULT '{}'",
            "ALTER TABLE users ADD COLUMN short_cfg TEXT DEFAULT '{}'",
            "ALTER TABLE users ADD COLUMN use_session INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN smc_use_bos   INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN smc_use_ob    INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN smc_use_fvg   INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN smc_use_sweep INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN smc_use_choch INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN smc_use_conf  INTEGER DEFAULT 0",
        ]
        for sql in migrations:
            try:
                await db.execute(sql)
            except Exception:
                pass  # колонка уже существует
        await db.commit()
    log.info(f"✅ SQLite инициализирована: {path}")


def _row_to_dict(description, row) -> dict:
    return {description[i][0]: row[i] for i in range(len(description))}


# ── Пользователи ────────────────────────────────────

async def db_get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_upsert_user(data: dict):
    data["updated_at"] = time.time()
    cols = list(data.keys())
    vals = list(data.values())
    placeholders = ", ".join("?" * len(vals))
    col_names    = ", ".join(cols)
    updates      = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "user_id")
    sql = (
        f"INSERT INTO users ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(user_id) DO UPDATE SET {updates}"
    )
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(sql, vals)
            await db.commit()


async def db_get_active_users() -> list[dict]:
    now = time.time()
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM users
               WHERE sub_status IN ('trial','active') AND sub_expires > ?
               AND (active=1 OR long_active=1 OR short_active=1)""",
            (now,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def db_get_all_users() -> list[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def db_stats_summary() -> dict:
    async with aiosqlite.connect(_db_path) as db:
        async def count(where=""):
            sql = f"SELECT COUNT(*) FROM users{' WHERE ' + where if where else ''}"
            async with db.execute(sql) as cur:
                return (await cur.fetchone())[0]
        return {
            "total":    await count(),
            "trial":    await count("sub_status='trial'"),
            "active":   await count("sub_status='active'"),
            "expired":  await count("sub_status='expired'"),
            "banned":   await count("sub_status='banned'"),
            "scanning": await count("active=1"),
        }


# ── Сделки ──────────────────────────────────────────

async def db_add_trade(data: dict):
    cols = list(data.keys())
    vals = list(data.values())
    placeholders = ", ".join("?" * len(vals))
    col_names    = ", ".join(cols)
    sql = f"INSERT OR IGNORE INTO trades ({col_names}) VALUES ({placeholders})"
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(sql, vals)
            await db.commit()


async def db_get_trade(trade_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_set_trade_result(trade_id: str, result: str, result_rr: float) -> Optional[dict]:
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "UPDATE trades SET result=?, result_rr=? WHERE trade_id=?",
                (result, result_rr, trade_id)
            )
            await db.commit()
    return await db_get_trade(trade_id)


async def db_get_user_trades(user_id: int) -> list[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE user_id=? AND result != '' AND result != 'SKIP' ORDER BY created_at",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def db_reset_user_trades(user_id: int):
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute("DELETE FROM trades WHERE user_id=?", (user_id,))
            await db.commit()


async def db_get_user_stats(user_id: int) -> dict:
    trades = await db_get_user_trades(user_id)
    if not trades:
        return {}

    wins   = [t for t in trades if t["result"] in ("TP1", "TP2", "TP3")]
    losses = [t for t in trades if t["result"] == "SL"]
    total  = len(trades)

    winrate  = len(wins) / total * 100 if total else 0
    avg_rr   = sum(t["result_rr"] for t in trades) / total if total else 0
    total_rr = sum(t["result_rr"] for t in trades)

    # По монетам
    sym: dict = {}
    for t in trades:
        s = t["symbol"]
        sym.setdefault(s, {"wins": 0, "total": 0})
        sym[s]["total"] += 1
        if t["result"] in ("TP1", "TP2", "TP3"):
            sym[s]["wins"] += 1
    best = sorted(
        sym.items(),
        key=lambda x: x[1]["wins"] / x[1]["total"] if x[1]["total"] >= 2 else 0,
        reverse=True
    )[:5]

    # Серии
    sw = sl = cw = cl = 0
    for t in trades:
        if t["result"] in ("TP1", "TP2", "TP3"):
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        sw = max(sw, cw); sl = max(sl, cl)

    longs  = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]
    lw = sum(1 for t in longs  if t["result"] in ("TP1","TP2","TP3"))
    sw2= sum(1 for t in shorts if t["result"] in ("TP1","TP2","TP3"))

    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "winrate": winrate, "avg_rr": avg_rr, "total_rr": total_rr,
        "streak_w": sw, "streak_l": sl, "best_symbols": best,
        "longs_total": len(longs),   "longs_wins": lw,
        "shorts_total": len(shorts), "shorts_wins": sw2,
        "tp1_cnt": sum(1 for t in wins if t["result"] == "TP1"),
        "tp2_cnt": sum(1 for t in wins if t["result"] == "TP2"),
        "tp3_cnt": sum(1 for t in wins if t["result"] == "TP3"),
    }
