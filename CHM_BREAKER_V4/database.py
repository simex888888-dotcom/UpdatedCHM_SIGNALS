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

# Импорт отложен до первого вызова чтобы избежать circular import при старте
def _request_turso_push():
    try:
        import turso_sync
        turso_sync.request_push()
    except Exception:
        pass

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

    atr_period       INTEGER DEFAULT 14,
    atr_mult         REAL    DEFAULT 1.0,
    max_risk_pct     REAL    DEFAULT 1.5,

    tp1_rr           REAL    DEFAULT 2.0,
    tp2_rr           REAL    DEFAULT 3.0,
    tp3_rr           REAL    DEFAULT 4.5,

    zone_pct         REAL    DEFAULT 0.7,
    max_dist_pct     REAL    DEFAULT 1.5,
    min_rr           REAL    DEFAULT 2.0,
    max_level_tests  INTEGER DEFAULT 4,

    min_volume_usdt  REAL    DEFAULT 1000000,
    min_quality      INTEGER DEFAULT 3,
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
    smc_long_active  INTEGER DEFAULT 0,
    smc_short_active INTEGER DEFAULT 0,
    long_cfg         TEXT    DEFAULT '{}',
    short_cfg        TEXT    DEFAULT '{}',
    smc_cfg          TEXT    DEFAULT '{}',
    trend_only       INTEGER DEFAULT 0,

    signals_received    INTEGER DEFAULT 0,
    trial_reminder_sent INTEGER DEFAULT 0,
    expired_notified    INTEGER DEFAULT 0,
    created_at          REAL    DEFAULT 0,
    updated_at          REAL    DEFAULT 0,
    strategy            TEXT    DEFAULT 'LEVELS',

    -- Авто-трейдинг Bybit
    bybit_api_key       TEXT    DEFAULT '',
    bybit_api_secret    TEXT    DEFAULT '',
    auto_trade          INTEGER DEFAULT 0,
    auto_trade_mode     TEXT    DEFAULT 'confirm',
    trade_risk_pct      REAL    DEFAULT 1.0,
    trade_leverage      INTEGER DEFAULT 10,
    max_trades_limit    INTEGER DEFAULT 5,
    watch_coin          TEXT    DEFAULT ''
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

-- Постоянная таблица использованных пробных периодов.
-- Никогда не сбрасывается при миграциях — гарантирует однократность триала.
CREATE TABLE IF NOT EXISTS trial_ids (
    user_id  INTEGER PRIMARY KEY,
    used_at  REAL    NOT NULL
);

-- ═══════════════════════════════════════════════════════════════
--  ПРОМОКОДЫ
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS promo_codes (
    code           TEXT    PRIMARY KEY,
    created_by     INTEGER NOT NULL,
    created_at     REAL    NOT NULL,
    duration_hours INTEGER DEFAULT 2
);

-- Один промокод на пользователя (повторное использование запрещено)
CREATE TABLE IF NOT EXISTS promo_uses (
    user_id  INTEGER PRIMARY KEY,
    code     TEXT    NOT NULL,
    used_at  REAL    NOT NULL
);

-- ═══════════════════════════════════════════════════════════════
--  РЕФЕРАЛЬНАЯ ПРОГРАММА
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS referrals (
    referred_id  INTEGER PRIMARY KEY,   -- кто пришёл по ссылке
    referrer_id  INTEGER NOT NULL,      -- кто пригласил
    joined_at    REAL    NOT NULL,
    converted    INTEGER DEFAULT 0,     -- 1 = купил подписку
    converted_at REAL
);

-- История выданных наград (каждые 5 конверсий = +30 дней)
CREATE TABLE IF NOT EXISTS ref_rewards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    given_at   REAL    NOT NULL
);

-- ═══════════════════════════════════════════════════════════════
--  ПАМП/ДАМП ДЕТЕКТОР (BingX)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS pd_users (
    user_id       INTEGER PRIMARY KEY,
    pd_subscribed INTEGER DEFAULT 0,
    pd_threshold  INTEGER DEFAULT 50
);

CREATE TABLE IF NOT EXISTS pd_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    direction     TEXT    NOT NULL,   -- PUMP | DUMP
    score         REAL    NOT NULL,
    layers_json   TEXT    DEFAULT '{}',
    features_json TEXT    DEFAULT '[]',
    price_signal  REAL    DEFAULT 0,
    ts            REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS pd_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL,
    price_signal  REAL    DEFAULT 0,
    price_15m     REAL    DEFAULT 0,
    change_pct    REAL    DEFAULT 0,
    correct       INTEGER DEFAULT 0,
    ts            REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS pd_train_data (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL,
    features_json TEXT    DEFAULT '[]',
    actual_label  INTEGER,   -- 0=neutral,1=pump,2=dump
    ts            REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ═══════════════════════════════════════════════════════════════
--  POLYMARKET
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS poly_settings (
    user_id     INTEGER PRIMARY KEY,
    default_bet REAL    DEFAULT 5.0,
    digest_on   INTEGER DEFAULT 1,
    created_at  REAL    DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poly_bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    market_id     TEXT    DEFAULT '',
    question      TEXT    DEFAULT '',
    side          TEXT    DEFAULT '',
    amount_usdc   REAL    DEFAULT 0,
    shares        REAL    DEFAULT 0,
    price         REAL    DEFAULT 0,
    order_id      TEXT    DEFAULT '',
    status        TEXT    DEFAULT 'filled',
    created_at    REAL    DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_poly_bets_user ON poly_bets(user_id);

-- Кастодиальные кошельки пользователей (Polygon)
CREATE TABLE IF NOT EXISTS poly_wallets (
    user_id       INTEGER PRIMARY KEY,
    address       TEXT    NOT NULL UNIQUE,
    encrypted_key TEXT    NOT NULL,
    created_at    REAL    DEFAULT 0
);

-- Ценовые алерты
CREATE TABLE IF NOT EXISTS poly_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    market_id  TEXT    NOT NULL,
    question   TEXT    NOT NULL,
    yes_price  REAL    NOT NULL,   -- цена YES в момент создания алерта
    threshold  REAL    NOT NULL,   -- порог срабатывания в %
    active     INTEGER DEFAULT 1,
    created_at REAL    DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_poly_alerts_user ON poly_alerts(user_id, active);

-- Список наблюдения (избранные маркеты)
CREATE TABLE IF NOT EXISTS poly_watchlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    market_id TEXT    NOT NULL,
    question  TEXT    NOT NULL,
    added_at  REAL    DEFAULT 0,
    UNIQUE(user_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_poly_watchlist_user ON poly_watchlist(user_id);

-- Лог дайджестов (предотвращает повторную отправку)
CREATE TABLE IF NOT EXISTS poly_digest_log (
    user_id INTEGER NOT NULL,
    date    TEXT    NOT NULL,   -- формат YYYY-MM-DD
    PRIMARY KEY (user_id, date)
);
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
            "ALTER TABLE users ADD COLUMN trend_only INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN trial_reminder_sent INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN expired_notified INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN zone_pct REAL DEFAULT 0.7",
            "ALTER TABLE users ADD COLUMN max_dist_pct REAL DEFAULT 1.5",
            "ALTER TABLE users ADD COLUMN min_rr REAL DEFAULT 2.0",
            "ALTER TABLE users ADD COLUMN max_level_tests INTEGER DEFAULT 4",
            "ALTER TABLE users ADD COLUMN strategy TEXT DEFAULT 'LEVELS'",
            "ALTER TABLE users ADD COLUMN smc_cfg TEXT DEFAULT '{}'",
            "ALTER TABLE users ADD COLUMN smc_long_active INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN smc_short_active INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN bybit_api_key TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN bybit_api_secret TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN auto_trade INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN auto_trade_mode TEXT DEFAULT 'confirm'",
            "ALTER TABLE users ADD COLUMN trade_risk_pct REAL DEFAULT 1.0",
            "ALTER TABLE users ADD COLUMN trade_leverage INTEGER DEFAULT 10",
            "ALTER TABLE users ADD COLUMN max_trades_limit INTEGER DEFAULT 5",
            "ALTER TABLE users ADD COLUMN watch_coin TEXT DEFAULT ''",
            "ALTER TABLE trades ADD COLUMN be_set INTEGER DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN pos_idx INTEGER DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN order_id TEXT DEFAULT ''",
            # Снижаем порог существующих пользователей с дефолтного 70 → 50
            # чтобы они начали получать сигналы (MIN_SIGNAL_SCORE теперь 40%)
            "UPDATE pd_users SET pd_threshold=50 WHERE pd_threshold=70",
            # Polymarket таблицы (для существующих БД без миграции через SCHEMA)
            """CREATE TABLE IF NOT EXISTS poly_settings (
                user_id INTEGER PRIMARY KEY, default_bet REAL DEFAULT 5.0,
                digest_on INTEGER DEFAULT 1, created_at REAL DEFAULT 0
            )""",
            "ALTER TABLE poly_settings ADD COLUMN digest_on INTEGER DEFAULT 1",
            """CREATE TABLE IF NOT EXISTS poly_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                market_id TEXT DEFAULT '', question TEXT DEFAULT '', side TEXT DEFAULT '',
                amount_usdc REAL DEFAULT 0, shares REAL DEFAULT 0, price REAL DEFAULT 0,
                order_id TEXT DEFAULT '', status TEXT DEFAULT 'filled', created_at REAL DEFAULT 0
            )""",
            "CREATE INDEX IF NOT EXISTS idx_poly_bets_user ON poly_bets(user_id)",
            """CREATE TABLE IF NOT EXISTS poly_wallets (
                user_id INTEGER PRIMARY KEY, address TEXT NOT NULL UNIQUE,
                encrypted_key TEXT NOT NULL, created_at REAL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS poly_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                market_id TEXT NOT NULL, question TEXT NOT NULL,
                yes_price REAL NOT NULL, threshold REAL NOT NULL,
                active INTEGER DEFAULT 1, created_at REAL DEFAULT 0
            )""",
            "CREATE INDEX IF NOT EXISTS idx_poly_alerts_user ON poly_alerts(user_id, active)",
            """CREATE TABLE IF NOT EXISTS poly_digest_log (
                user_id INTEGER NOT NULL, date TEXT NOT NULL,
                PRIMARY KEY (user_id, date)
            )""",
            """CREATE TABLE IF NOT EXISTS poly_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, market_id TEXT NOT NULL,
                question TEXT NOT NULL, added_at REAL DEFAULT 0,
                UNIQUE(user_id, market_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_poly_watchlist_user ON poly_watchlist(user_id)",
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


# ── Однократный триал (выживает даже при пересоздании БД) ──

async def db_is_trial_used(user_id: int) -> bool:
    """Проверяет, использовал ли пользователь пробный период когда-либо."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT 1 FROM trial_ids WHERE user_id=?", (user_id,)
        ) as cur:
            return (await cur.fetchone()) is not None


async def db_mark_trial_used(user_id: int):
    """Помечает пользователя как использовавшего пробный период."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO trial_ids (user_id, used_at) VALUES (?, ?)",
                (user_id, time.time()),
            )
            await db.commit()


# ── Пользователи ────────────────────────────────────

async def db_get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


_ALLOWED_USER_COLS = {
    "user_id", "username", "active", "sub_status", "sub_expires",
    "trial_started", "trial_used", "timeframe", "scan_interval",
    "pivot_strength", "max_level_age", "max_retest_bars", "zone_buffer",
    "ema_fast", "ema_slow", "htf_ema_period", "rsi_period", "rsi_ob",
    "rsi_os", "vol_mult", "vol_len", "use_rsi", "use_volume",
    "use_pattern", "use_htf", "atr_period", "atr_mult", "max_risk_pct",
    "tp1_rr", "tp2_rr", "tp3_rr", "zone_pct", "max_dist_pct", "min_rr",
    "max_level_tests", "min_volume_usdt", "min_quality", "cooldown_bars",
    "notify_signal", "notify_breakout", "scan_mode", "long_tf",
    "long_interval", "short_tf", "short_interval", "long_active",
    "short_active", "smc_long_active", "smc_short_active", "long_cfg",
    "short_cfg", "smc_cfg", "trend_only", "signals_received",
    "trial_reminder_sent", "expired_notified", "updated_at", "strategy",
    "bybit_api_key", "bybit_api_secret", "auto_trade", "auto_trade_mode",
    "trade_risk_pct", "trade_leverage", "max_trades_limit", "watch_coin",
}


async def db_upsert_user(data: dict):
    data["updated_at"] = time.time()
    # Фильтруем только допустимые колонки во избежание SQL-инъекций
    data = {k: v for k, v in data.items() if k in _ALLOWED_USER_COLS}
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
    _request_turso_push()


async def db_get_active_users() -> list[dict]:
    now = time.time()
    for attempt in range(3):
        try:
            async with aiosqlite.connect(_db_path, timeout=30) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT * FROM users
                       WHERE sub_status IN ('trial','active') AND sub_expires > ?
                       AND (active=1 OR long_active=1 OR short_active=1
                            OR smc_long_active=1 OR smc_short_active=1)""",
                    (now,)
                ) as cur:
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]
        except Exception as e:
            if attempt == 2:
                raise
            log.warning(f"db_get_active_users retry {attempt + 1}/3: {e}")
            await asyncio.sleep(1 + attempt)


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


async def db_get_open_trades_for_be(user_id: int) -> list[dict]:
    """Возвращает незакрытые сделки у которых BE ещё не выставлен."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE user_id=? AND result='' AND be_set=0",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def db_get_all_open_trades(user_id: int) -> list[dict]:
    """Возвращает все незакрытые сделки пользователя."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM trades WHERE user_id=? AND result=''",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def db_update_trade_pos_idx(trade_id: str, pos_idx: int):
    """Обновляет pos_idx сделки после подтверждения открытия на бирже."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "UPDATE trades SET pos_idx=? WHERE trade_id=?",
                (pos_idx, trade_id)
            )
            await db.commit()


async def db_update_trade_bybit(trade_id: str, order_id: str, pos_idx: int):
    """Сохраняет order_id и pos_idx после успешного открытия позиции на Bybit."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "UPDATE trades SET order_id=?, pos_idx=? WHERE trade_id=?",
                (order_id, pos_idx, trade_id)
            )
            await db.commit()


async def db_set_trade_be(trade_id: str):
    """Помечает что безубыток по сделке уже выставлен."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "UPDATE trades SET be_set=1 WHERE trade_id=?", (trade_id,)
            )
            await db.commit()


async def db_count_open_trades(user_id: int, window_hours: int = 24) -> int:
    """Количество сделок без результата за последние window_hours часов."""
    since = time.time() - window_hours * 3600
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id=? AND result='' AND created_at>=?",
            (user_id, since),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_has_open_trade_for_symbol(user_id: int, symbol: str) -> bool:
    """Возвращает True если у пользователя уже есть незакрытая сделка по данному символу."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT 1 FROM trades WHERE user_id=? AND symbol=? AND result='' LIMIT 1",
            (user_id, symbol),
        ) as cur:
            row = await cur.fetchone()
            return row is not None


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


# ── Алиасы для handlers.py ──────────────────────────────────────────────────

async def get_user_stats(user_id: int) -> dict:
    return await db_get_user_stats(user_id)


async def get_signal(signal_id) -> Optional[dict]:
    """Получить сигнал/сделку по trade_id."""
    return await db_get_trade(str(signal_id))


async def get_signal_records(signal_id) -> list[dict]:
    """Получить записи результатов для сигнала (возвращает саму сделку если есть)."""
    trade = await db_get_trade(str(signal_id))
    return [trade] if trade else []


async def add_trade_record(user_id: int, signal_id, result: str, rr: float):
    """Записать результат сделки."""
    await db_set_trade_result(str(signal_id), result.upper(), rr)


async def get_user_records(user_id: int, limit: int = 50) -> list[dict]:
    """Получить последние сделки пользователя."""
    trades = await db_get_user_trades(user_id)
    return trades[-limit:] if len(trades) > limit else trades


async def update_signal_tp(signal_id, *, tp1=None, tp2=None, tp3=None):
    """Обновить TP1/TP2/TP3 для сигнала/сделки."""
    updates = {}
    if tp1 is not None: updates["tp1"] = tp1
    if tp2 is not None: updates["tp2"] = tp2
    if tp3 is not None: updates["tp3"] = tp3
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [str(signal_id)]
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(f"UPDATE trades SET {set_clause} WHERE trade_id=?", vals)
            await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  ПАМП/ДАМП ДЕТЕКТОР
# ═══════════════════════════════════════════════════════════════════════════════

async def db_pd_get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pd_users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_pd_upsert_user(user_id: int, subscribed: bool = None, threshold: int = None):
    from pump_dump.pd_config import DEFAULT_USER_THRESHOLD
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT INTO pd_users(user_id, pd_threshold) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, DEFAULT_USER_THRESHOLD)
            )
            if subscribed is not None:
                await db.execute(
                    "UPDATE pd_users SET pd_subscribed=? WHERE user_id=?",
                    (int(subscribed), user_id)
                )
            if threshold is not None:
                await db.execute(
                    "UPDATE pd_users SET pd_threshold=? WHERE user_id=?",
                    (threshold, user_id)
                )
            await db.commit()


async def db_pd_subscribers(min_threshold: int = 0) -> list[int]:
    """Возвращает user_id всех подписанных с threshold <= score."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT user_id FROM pd_users WHERE pd_subscribed=1 AND pd_threshold<=?",
            (min_threshold,)
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def db_pd_save_signal(
    symbol: str, direction: str, score: float,
    layers_json: str, features_json: str, price: float
) -> int:
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            cur = await db.execute(
                "INSERT INTO pd_signals(symbol,direction,score,layers_json,features_json,price_signal,ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (symbol, direction, score, layers_json, features_json, price, time.time())
            )
            await db.commit()
            return cur.lastrowid


async def db_pd_save_outcome(
    signal_id: int, price_signal: float,
    price_15m: float, change_pct: float, correct: bool
):
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT INTO pd_outcomes(signal_id,price_signal,price_15m,change_pct,correct,ts)"
                " VALUES(?,?,?,?,?,?)",
                (signal_id, price_signal, price_15m, change_pct, int(correct), time.time())
            )
            await db.commit()


async def db_pd_save_train(signal_id: int, label: int):
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            # Берём features из pd_signals
            async with db.execute(
                "SELECT features_json FROM pd_signals WHERE id=?", (signal_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return
            await db.execute(
                "INSERT INTO pd_train_data(signal_id,features_json,actual_label,ts)"
                " VALUES(?,?,?,?)",
                (signal_id, row[0], label, time.time())
            )
            await db.commit()


async def db_pd_pending_outcomes() -> list[dict]:
    """Сигналы старше 15 мин без исхода (для fallback трекинга)."""
    cutoff = time.time() - 900
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.id, s.symbol, s.direction, s.price_signal "
            "FROM pd_signals s "
            "LEFT JOIN pd_outcomes o ON o.signal_id=s.id "
            "WHERE s.ts < ? AND o.id IS NULL",
            (cutoff,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def db_pd_stats() -> dict:
    """Статистика точности памп/дамп сигналов."""
    now  = time.time()
    day  = now - 86400
    week = now - 604800

    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(correct) FROM pd_outcomes WHERE ts >= ?", (day,)
        ) as cur:
            day_row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*), SUM(correct) FROM pd_outcomes WHERE ts >= ?", (week,)
        ) as cur:
            week_row = await cur.fetchone()

    def _prec(total, correct):
        if not total:
            return 0.0
        return (correct or 0) / total * 100

    return {
        "day_total":   day_row[0]  or 0,
        "day_correct": day_row[1]  or 0,
        "day_prec":    _prec(day_row[0],  day_row[1]),
        "week_total":  week_row[0] or 0,
        "week_correct":week_row[1] or 0,
        "week_prec":   _prec(week_row[0], week_row[1]),
    }


async def db_pd_recent_signals(limit: int = 10) -> list[dict]:
    """Последние N сигналов с исходами для истории."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.id, s.symbol, s.direction, s.score, s.price_signal AS price,
                   s.ts AS created_at, o.correct
            FROM pd_signals s
            LEFT JOIN pd_outcomes o ON o.signal_id = s.id
            ORDER BY s.ts DESC
            LIMIT ?
            """,
            (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def poly_watchlist_add(user_id: int, market_id: str, question: str):
    """Добавляет маркет в список наблюдения. Дублирование игнорируется."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO poly_watchlist(user_id, market_id, question, added_at)"
                " VALUES(?,?,?,?)",
                (user_id, market_id, question, time.time())
            )
            await db.commit()


async def poly_watchlist_remove(user_id: int, market_id: str):
    """Удаляет маркет из списка наблюдения."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "DELETE FROM poly_watchlist WHERE user_id=? AND market_id=?",
                (user_id, market_id)
            )
            await db.commit()


async def poly_watchlist_get(user_id: int) -> list[dict]:
    """Возвращает список маркетов наблюдения пользователя (новейшие первыми)."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, market_id, question, added_at FROM poly_watchlist"
            " WHERE user_id=? ORDER BY added_at DESC LIMIT 20",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def poly_watchlist_has(user_id: int, market_id: str) -> bool:
    """Возвращает True если маркет уже в списке наблюдения."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT 1 FROM poly_watchlist WHERE user_id=? AND market_id=?",
            (user_id, market_id)
        ) as cur:
            return (await cur.fetchone()) is not None


# ═══════════════════════════════════════════════════════════════════════════════
#  ПРОМОКОДЫ
# ═══════════════════════════════════════════════════════════════════════════════

async def db_promo_create(code: str, created_by: int, duration_hours: int = 2):
    code = code.strip().upper()
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO promo_codes(code, created_by, created_at, duration_hours)"
                " VALUES(?, ?, ?, ?)",
                (code, created_by, time.time(), duration_hours)
            )
            await db.commit()


async def db_promo_delete(code: str) -> bool:
    """Удаляет промокод. Возвращает True если он существовал."""
    code = code.strip().upper()
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            cur = await db.execute(
                "DELETE FROM promo_codes WHERE code=?", (code,)
            )
            await db.commit()
            return cur.rowcount > 0


async def db_promo_list() -> list:
    """Список всех активных промокодов."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT code, created_by, duration_hours, created_at FROM promo_codes ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def db_promo_validate_and_use(user_id: int, code: str) -> tuple:
    """
    Проверяет промокод и применяет его.
    Возвращает (success: bool, message: str, duration_hours: int).
    """
    code = code.strip().upper()
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            # Код существует?
            async with db.execute(
                "SELECT duration_hours FROM promo_codes WHERE code=?", (code,)
            ) as cur:
                promo_row = await cur.fetchone()
            if not promo_row:
                return False, "❌ Промокод не найден или уже удалён", 0
            # Пользователь уже использовал промокод?
            async with db.execute(
                "SELECT user_id FROM promo_uses WHERE user_id=?", (user_id,)
            ) as cur:
                used_row = await cur.fetchone()
            if used_row:
                return False, "❌ Вы уже использовали промокод ранее", 0
            hours = promo_row[0]
            await db.execute(
                "INSERT INTO promo_uses(user_id, code, used_at) VALUES(?, ?, ?)",
                (user_id, code, time.time())
            )
            await db.commit()
    return True, f"✅ Промокод активирован! Доступ на {hours} ч.", hours


# ═══════════════════════════════════════════════════════════════════════════════
#  РЕФЕРАЛЬНАЯ ПРОГРАММА
# ═══════════════════════════════════════════════════════════════════════════════

async def db_ref_set(referred_id: int, referrer_id: int) -> bool:
    """
    Записывает реферала. Возвращает True если запись новая
    (чтобы не перезаписывать, если пользователь уже был приглашён кем-то).
    """
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            async with db.execute(
                "SELECT referred_id FROM referrals WHERE referred_id=?", (referred_id,)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                return False
            await db.execute(
                "INSERT INTO referrals(referred_id, referrer_id, joined_at) VALUES(?,?,?)",
                (referred_id, referrer_id, time.time())
            )
            await db.commit()
    return True


async def db_ref_on_purchase(referred_id: int) -> Optional[tuple]:
    """
    Вызывается когда пользователь получает подписку (/give).
    Помечает реферал как конвертированный.
    Возвращает (referrer_id, total_conversions) или None если реферала нет.
    """
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            async with db.execute(
                "SELECT referrer_id, converted FROM referrals WHERE referred_id=?",
                (referred_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row or row[1]:  # нет реферала или уже отмечен
                return None
            referrer_id = row[0]
            await db.execute(
                "UPDATE referrals SET converted=1, converted_at=? WHERE referred_id=?",
                (time.time(), referred_id)
            )
            await db.commit()
            async with db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND converted=1",
                (referrer_id,)
            ) as cur:
                cnt_row = await cur.fetchone()
            total = cnt_row[0] if cnt_row else 0
    return referrer_id, total


async def db_ref_check_and_claim_reward(referrer_id: int) -> bool:
    """
    Проверяет, положена ли реферу награда (каждые 5 конверсий = +30 дней).
    Если да — записывает награду и возвращает True.
    """
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND converted=1",
                (referrer_id,)
            ) as cur:
                row = await cur.fetchone()
            conv_count = row[0] if row else 0
            async with db.execute(
                "SELECT COUNT(*) FROM ref_rewards WHERE user_id=?", (referrer_id,)
            ) as cur:
                row2 = await cur.fetchone()
            rew_count = row2[0] if row2 else 0
            if conv_count // 5 > rew_count:
                await db.execute(
                    "INSERT INTO ref_rewards(user_id, given_at) VALUES(?, ?)",
                    (referrer_id, time.time())
                )
                await db.commit()
                return True
    return False


async def db_ref_stats(user_id: int) -> dict:
    """Статистика реферальной программы для пользователя."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(converted) FROM referrals WHERE referrer_id=?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM ref_rewards WHERE user_id=?", (user_id,)
        ) as cur:
            rew_row = await cur.fetchone()
    total     = row[0] or 0
    converted = row[1] or 0
    rewards   = rew_row[0] or 0
    # Сколько ещё нужно до следующей награды
    next_at   = ((rewards + 1) * 5)
    until_next = max(0, next_at - converted)
    return {
        "total":      total,
        "converted":  converted,
        "rewards":    rewards,
        "until_next": until_next,
    }


# ── KV-хранилище (общие настройки бота) ──────────────────────────────────

async def db_kv_get(key: str) -> Optional[str]:
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def db_kv_set(key: str, value: str):
    async with aiosqlite.connect(_db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()
    _request_turso_push()


# ═══════════════════════════════════════════════════════════════════════════════
#  POLYMARKET
# ═══════════════════════════════════════════════════════════════════════════════

async def poly_get_settings(user_id: int) -> dict:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poly_settings WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {"user_id": user_id, "default_bet": 5.0}


async def poly_save_settings(user_id: int, default_bet: float):
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT INTO poly_settings(user_id, default_bet, created_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET default_bet=excluded.default_bet",
                (user_id, default_bet, time.time()),
            )
            await db.commit()


async def poly_save_digest(user_id: int, digest_on: int):
    """Сохраняет настройку дайджеста (0 или 1)."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT INTO poly_settings(user_id, default_bet, digest_on, created_at) VALUES(?,5.0,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET digest_on=excluded.digest_on",
                (user_id, digest_on, time.time()),
            )
            await db.commit()


async def poly_save_bet(
    user_id: int, market_id: str, question: str, side: str,
    amount: float, shares: float, price: float, order_id: str,
) -> int:
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            cur = await db.execute(
                "INSERT INTO poly_bets"
                "(user_id, market_id, question, side, amount_usdc, shares, price, order_id, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, market_id, question, side, amount, shares, price, order_id, time.time()),
            )
            await db.commit()
            return cur.lastrowid


async def poly_get_bets(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poly_bets WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
#  КАСТОДИАЛЬНЫЕ КОШЕЛЬКИ
# ═══════════════════════════════════════════════════════════════════════════════

async def poly_wallet_get(user_id: int) -> Optional[dict]:
    """Возвращает кошелёк пользователя или None если не создан."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poly_wallets WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def poly_wallet_create(user_id: int, address: str, encrypted_key: str):
    """Сохраняет новый кошелёк пользователя."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO poly_wallets(user_id, address, encrypted_key, created_at)"
                " VALUES(?,?,?,?)",
                (user_id, address, encrypted_key, time.time()),
            )
            await db.commit()
    _request_turso_push()


async def poly_wallet_restore(user_id: int, address: str, encrypted_key: str):
    """
    Восстанавливает кошелёк пользователя (INSERT OR REPLACE).
    Используется для восстановления из Turso после редеплоя.
    В отличие от poly_wallet_create, перезаписывает существующую запись.
    """
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO poly_wallets(user_id, address, encrypted_key, created_at)"
                " VALUES(?,?,?,?)",
                (user_id, address, encrypted_key, time.time()),
            )
            await db.commit()
    _request_turso_push()


# ═══════════════════════════════════════════════════════════════════════════════
#  ЦЕНОВЫЕ АЛЕРТЫ
# ═══════════════════════════════════════════════════════════════════════════════

async def poly_alert_add(
    user_id: int, market_id: str, question: str,
    yes_price: float, threshold: float,
) -> int:
    """Создаёт ценовой алерт. Возвращает id."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            cur = await db.execute(
                "INSERT INTO poly_alerts(user_id, market_id, question, yes_price, threshold, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (user_id, market_id, question, yes_price, threshold, time.time()),
            )
            await db.commit()
            return cur.lastrowid


async def poly_alert_get_user(user_id: int) -> list[dict]:
    """Все активные алерты пользователя."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poly_alerts WHERE user_id=? AND active=1 ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def poly_alert_delete(alert_id: int):
    """Деактивирует алерт."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "UPDATE poly_alerts SET active=0 WHERE id=?", (alert_id,)
            )
            await db.commit()


async def poly_alerts_all_active() -> list[dict]:
    """Все активные алерты всех пользователей (для планировщика)."""
    async with aiosqlite.connect(_db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poly_alerts WHERE active=1"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════════
#  ДАЙДЖЕСТ ЛОГ
# ═══════════════════════════════════════════════════════════════════════════════

async def poly_digest_sent_today(user_id: int, date: str) -> bool:
    """Проверяет, был ли дайджест отправлен сегодня."""
    async with aiosqlite.connect(_db_path) as db:
        async with db.execute(
            "SELECT 1 FROM poly_digest_log WHERE user_id=? AND date=?", (user_id, date)
        ) as cur:
            return (await cur.fetchone()) is not None


async def poly_digest_mark_sent(user_id: int, date: str):
    """Помечает дайджест как отправленный."""
    async with _lock:
        async with aiosqlite.connect(_db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO poly_digest_log(user_id, date) VALUES(?,?)",
                (user_id, date),
            )
            await db.commit()
