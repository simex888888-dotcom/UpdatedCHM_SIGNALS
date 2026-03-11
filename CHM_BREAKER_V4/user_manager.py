"""
user_manager.py — управление пользователями через SQLite

Каждый пользователь имеет:
  - Общие настройки (shared) — используются как дефолт
  - long_cfg  — переопределения для ЛОНГ сканера (JSON)
  - short_cfg — переопределения для ШОРТ сканера (JSON)
  - long_active / short_active — независимые флаги

Эффективный конфиг = shared + override из direction cfg.
"""

import json
import time
import logging
from dataclasses import dataclass, field, fields, asdict
from typing import Optional
import database as db

log = logging.getLogger("CHM.Users")


# ── Полный набор торговых настроек ───────────────────
# Используется и как shared, и как long_cfg / short_cfg

@dataclass
class TradeCfg:
    """Торговые параметры одного направления или общие."""
    timeframe:       str   = "1h"
    scan_interval:   int   = 3600
    pivot_strength:  int   = 7
    max_level_age:   int   = 100
    max_retest_bars: int   = 30
    zone_buffer:     float = 0.3
    ema_fast:        int   = 50
    ema_slow:        int   = 200
    htf_ema_period:  int   = 50
    rsi_period:      int   = 14
    rsi_ob:          int   = 65
    rsi_os:          int   = 35
    vol_mult:        float = 1.0
    vol_len:         int   = 20
    use_rsi:         bool  = True
    use_volume:      bool  = True
    use_pattern:     bool  = False
    use_htf:         bool  = False
    atr_period:      int   = 14
    atr_mult:        float = 1.0
    max_risk_pct:    float = 1.5
    tp1_rr:          float = 2.0
    tp2_rr:          float = 3.0
    tp3_rr:          float = 4.5
    min_volume_usdt: float = 1_000_000
    min_quality:     int   = 3
    cooldown_bars:   int   = 5
    trend_only:      bool  = False
    # ── Протокол уровней (Price Action) ──────────────
    zone_pct:        float = 0.7   # Ширина зоны уровня в % от цены
    max_dist_pct:    float = 1.5   # Макс. дистанция до уровня для входа (%)
    min_rr:          float = 2.0   # Минимальный R:R
    max_level_tests: int   = 4     # Макс. тестов уровня до пропуска сигнала (ожидается пробой)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "TradeCfg":
        try:
            d = json.loads(s or "{}")
            valid = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in d.items() if k in valid})
        except Exception:
            return cls()

    def merged_with(self, override: "TradeCfg") -> "TradeCfg":
        """Применяем override поверх self (только непустые значения)."""
        base  = asdict(self)
        odict = asdict(override)
        defs  = asdict(TradeCfg())          # дефолтные значения
        # берём из override только то что отличается от дефолта
        merged = {k: (odict[k] if odict[k] != defs[k] else base[k]) for k in base}
        return TradeCfg(**merged)


# ── SMC-настройки пользователя ────────────────────────

@dataclass
class SMCUserCfg:
    """Персональный конфиг SMC-сканера."""
    tf_key:           str   = "1H"        # "15m" | "1H" | "4H" — основной таймфрейм
    scan_interval:    int   = 900         # интервал сканирования (сек)
    direction:        str   = "BOTH"      # "LONG" | "SHORT" | "BOTH"
    min_confirmations: int  = 2           # мин. подтверждений из 5 для сигнала (было 3)
    min_rr:           float = 1.5         # мин. R:R (было 2.0)
    sl_buffer_pct:    float = 0.15        # буфер SL от экстремума OB (%)
    min_volume_usdt:  float = 5_000_000   # мин. объём монеты ($)
    # ── Фильтры структуры ──────────────────────────
    fvg_enabled:      bool  = True        # учитывать FVG в подтверждениях
    choch_enabled:    bool  = True        # учитывать CHoCH
    ob_use_breaker:   bool  = True        # Breaker Blocks
    ob_max_age:       int   = 80          # макс. возраст OB в свечах (было 50)
    sweep_close_req:  bool  = False       # liquidity sweep — не требуем закрытия (было True)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "SMCUserCfg":
        try:
            d = json.loads(s or "{}")
            valid = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in d.items() if k in valid})
        except Exception:
            return cls()


@dataclass
class UserSettings:
    user_id:          int
    username:         str   = ""
    active:           bool  = False       # legacy — оставляем для совместимости

    sub_status:       str   = "expired"   # Без триала — сразу expired
    sub_expires:      float = 0.0
    trial_started:    float = 0.0
    trial_used:       bool  = True        # Триал не предоставляется

    # Общие настройки (используются в режиме "both" и как база для long/short)
    timeframe:        str   = "1h"
    scan_interval:    int   = 3600
    pivot_strength:   int   = 7
    max_level_age:    int   = 100
    max_retest_bars:  int   = 30
    zone_buffer:      float = 0.3
    ema_fast:         int   = 50
    ema_slow:         int   = 200
    htf_ema_period:   int   = 50
    rsi_period:       int   = 14
    rsi_ob:           int   = 65
    rsi_os:           int   = 35
    vol_mult:         float = 1.0
    vol_len:          int   = 20
    use_rsi:          bool  = True
    use_volume:       bool  = True
    use_pattern:      bool  = False
    use_htf:          bool  = False
    atr_period:       int   = 14
    atr_mult:         float = 1.0
    max_risk_pct:     float = 1.5
    tp1_rr:           float = 2.0
    tp2_rr:           float = 3.0
    tp3_rr:           float = 4.5
    min_volume_usdt:  float = 1_000_000
    min_quality:      int   = 3
    cooldown_bars:    int   = 5
    trend_only:       bool  = False
    # ── Протокол уровней (Price Action) ──────────────
    zone_pct:         float = 0.7
    max_dist_pct:     float = 1.5
    min_rr:           float = 2.0
    max_level_tests:  int   = 4

    notify_signal:    bool  = True
    notify_breakout:  bool  = False

    # Режим (legacy — храним для совместимости)
    scan_mode:        str   = "both"
    long_tf:          str   = "1h"
    long_interval:    int   = 3600
    short_tf:         str   = "1h"
    short_interval:   int   = 3600

    # ── Мультисканнинг LEVELS ─────────────────────
    long_active:      bool  = False   # лонг сканер включён
    short_active:     bool  = False   # шорт сканер включён

    # ── Мультисканнинг SMC ────────────────────────
    smc_long_active:  bool  = False   # SMC лонг сканер
    smc_short_active: bool  = False   # SMC шорт сканер

    # JSON-строки с независимыми настройками
    long_cfg:         str   = "{}"
    short_cfg:        str   = "{}"
    smc_cfg:          str   = "{}"

    signals_received:     int   = 0
    trial_reminder_sent:  bool  = False
    expired_notified:     bool  = False

    # ── Фильтр монеты (пустая строка = все монеты) ────
    watch_coin:           str   = ""   # например "BTC-USDT-SWAP"

    # ── Стратегия сканера ─────────────────────────
    strategy:             str   = "LEVELS"   # "LEVELS" | "SMC"

    # ── Авто-трейдинг Bybit ───────────────────────
    bybit_api_key:        str   = ""
    bybit_api_secret:     str   = ""
    auto_trade:           bool  = False
    auto_trade_mode:      str   = "confirm"   # "auto" | "confirm"
    trade_risk_pct:       float = 1.0         # % от баланса на сделку
    trade_leverage:       int   = 10          # плечо
    max_trades_limit:     int   = 5           # макс. одновременных открытых сделок

    # ── Хелперы конфигов ─────────────────────────

    def shared_cfg(self) -> TradeCfg:
        """Общие настройки как TradeCfg."""
        return TradeCfg(
            timeframe=self.timeframe, scan_interval=self.scan_interval,
            pivot_strength=self.pivot_strength, max_level_age=self.max_level_age,
            max_retest_bars=self.max_retest_bars, zone_buffer=self.zone_buffer,
            ema_fast=self.ema_fast, ema_slow=self.ema_slow,
            htf_ema_period=self.htf_ema_period,
            rsi_period=self.rsi_period, rsi_ob=self.rsi_ob, rsi_os=self.rsi_os,
            vol_mult=self.vol_mult, vol_len=self.vol_len,
            use_rsi=self.use_rsi, use_volume=self.use_volume,
            use_pattern=self.use_pattern, use_htf=self.use_htf,
            atr_period=self.atr_period, atr_mult=self.atr_mult,
            max_risk_pct=self.max_risk_pct,
            tp1_rr=self.tp1_rr, tp2_rr=self.tp2_rr, tp3_rr=self.tp3_rr,
            min_volume_usdt=self.min_volume_usdt,
            min_quality=self.min_quality, cooldown_bars=self.cooldown_bars,
            trend_only=self.trend_only,
            zone_pct=self.zone_pct, max_dist_pct=self.max_dist_pct,
            min_rr=self.min_rr, max_level_tests=self.max_level_tests,
        )

    def get_long_cfg(self) -> TradeCfg:
        """Эффективный конфиг для лонг сканера."""
        override = TradeCfg.from_json(self.long_cfg)
        base = self.shared_cfg()
        merged = base.merged_with(override)
        # TF и интервал берём из long_tf/long_interval
        merged.timeframe     = self.long_tf
        merged.scan_interval = self.long_interval
        return merged

    def get_short_cfg(self) -> TradeCfg:
        """Эффективный конфиг для шорт сканера."""
        override = TradeCfg.from_json(self.short_cfg)
        base = self.shared_cfg()
        merged = base.merged_with(override)
        merged.timeframe     = self.short_tf
        merged.scan_interval = self.short_interval
        return merged

    def set_long_cfg(self, cfg: TradeCfg):
        self.long_cfg = cfg.to_json()

    def set_short_cfg(self, cfg: TradeCfg):
        self.short_cfg = cfg.to_json()

    def get_smc_cfg(self) -> "SMCUserCfg":
        return SMCUserCfg.from_json(self.smc_cfg)

    def set_smc_cfg(self, cfg: "SMCUserCfg"):
        self.smc_cfg = cfg.to_json()

    def any_active(self) -> bool:
        return self.long_active or self.short_active or (self.active and self.scan_mode == "both")

    # ── Подписка ─────────────────────────────────

    def check_access(self) -> tuple[bool, str]:
        if self.sub_status == "banned":
            return False, "banned"
        if self.sub_status in ("trial", "active"):
            if time.time() < self.sub_expires:
                return True, self.sub_status
            self.sub_status = "expired"
        return False, "expired"

    def grant_access(self, days: int):
        now  = time.time()
        base = max(self.sub_expires, now) if self.sub_status == "active" else now
        self.sub_expires = base + days * 86400
        self.sub_status  = "active"

    def time_left_str(self) -> str:
        left = self.sub_expires - time.time()
        if left <= 0:    return "истёк"
        if left < 3600:  return str(int(left // 60)) + " мин."
        if left < 86400:
            h = int(left // 3600); m = int((left % 3600) // 60)
            return str(h) + "ч " + str(m) + "м"
        d = int(left // 86400); h = int((left % 86400) // 3600)
        return str(d) + "д " + str(h) + "ч"

    def to_db(self) -> dict:
        bool_fields = {
            "active", "trial_used", "use_rsi", "use_volume",
            "use_pattern", "use_htf", "notify_signal", "notify_breakout",
            "long_active", "short_active", "smc_long_active", "smc_short_active",
            "trend_only", "trial_reminder_sent", "expired_notified",
            "auto_trade",
        }
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            d[f.name] = int(v) if (isinstance(v, bool) or f.name in bool_fields) else v
        return d


def _from_db(row: dict) -> UserSettings:
    u = UserSettings(user_id=row["user_id"])
    bool_fields = {
        "active", "trial_used", "use_rsi", "use_volume",
        "use_pattern", "use_htf", "notify_signal", "notify_breakout",
        "long_active", "short_active", "smc_long_active", "smc_short_active",
        "trend_only", "trial_reminder_sent", "expired_notified",
        "auto_trade",
    }
    for f in fields(u):
        if f.name in row and row[f.name] is not None:
            v = row[f.name]
            setattr(u, f.name, bool(v) if f.name in bool_fields else v)
    return u


class UserManager:

    async def get(self, user_id: int) -> Optional[UserSettings]:
        row = await db.db_get_user(user_id)
        return _from_db(row) if row else None

    async def get_or_create(self, user_id: int, username: str = "") -> UserSettings:
        row = await db.db_get_user(user_id)
        if row:
            return _from_db(row)
        # Новый пользователь — без триала, сразу expired (нужна подписка)
        user = UserSettings(
            user_id=user_id, username=username,
            sub_status="expired", sub_expires=0, trial_used=True,
        )
        log.info("Новый пользователь: @" + username + " (" + str(user_id) + ")")
        await db.db_upsert_user(user.to_db())
        return user

    async def save(self, user: UserSettings):
        await db.db_upsert_user(user.to_db())

    async def get_active_users(self) -> list[UserSettings]:
        rows = await db.db_get_active_users()
        return [_from_db(r) for r in rows]

    async def all_users(self) -> list[UserSettings]:
        rows = await db.db_get_all_users()
        return [_from_db(r) for r in rows]

    async def get_active_auto_trade_users(self) -> list[UserSettings]:
        """Пользователи у которых включён авто-трейдинг и есть API-ключи."""
        rows = await db.db_get_active_users()
        result = []
        for r in rows:
            u = _from_db(r)
            if getattr(u, "auto_trade", False) and getattr(u, "bybit_api_key", ""):
                result.append(u)
        return result

    async def stats_summary(self) -> dict:
        return await db.db_stats_summary()
