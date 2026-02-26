"""
usermanager.py — управление пользователями с персистентностью через SQLite
CHM BREAKER BOT v4.8
"""

import json
import time
import logging
from typing import Optional

import database as db

log = logging.getLogger("CHM.UserManager")


# ─────────────────────────── TradeCfg ───────────────────────────────────────

class TradeCfg:
    """Per-direction конфигурация (long_cfg / short_cfg).
    Все поля snake_case — совпадают с именами в БД и handlers.py."""

    _DEFAULTS = {
        # SMC
        "smc_use_bos":         True,
        "smc_use_ob":          True,
        "smc_use_fvg":         False,
        "smc_use_sweep":       False,
        "smc_use_choch":       False,
        "smc_use_conf":        False,
        # Levels
        "pivot_strength":      7,
        "max_level_age":       100,
        "max_retest_bars":     30,
        "zone_buffer":         0.3,
        # EMA
        "ema_fast":            50,
        "ema_slow":            200,
        "htf_ema_period":      50,
        # RSI
        "rsi_period":          14,
        "rsi_ob":              65,
        "rsi_os":              35,
        # Volume
        "vol_mult":            1.0,
        "vol_len":             20,
        # Filters
        "use_rsi":             True,
        "use_volume":          True,
        "use_pattern":         False,
        "use_htf":             False,
        "use_session":         False,
        # Risk / TP
        "atr_period":          14,
        "atr_mult":            1.0,
        "max_risk_pct":        1.5,
        "max_signal_risk_pct": 0.0,
        "tp1_rr":              0.8,
        "tp2_rr":              1.5,
        "tp3_rr":              2.5,
        # Cooldown
        "cooldown_bars":       5,
        # Quality
        "min_quality":         2,
        "min_volume_usdt":     1_000_000.0,
        # Analysis
        "analysis_mode":       "both",
    }

    def __init__(self, data: dict = None):
        for k, v in self._DEFAULTS.items():
            setattr(self, k, (data or {}).get(k, v))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._DEFAULTS}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "TradeCfg":
        try:
            data = json.loads(s) if s and s.strip() not in ("", "{}") else {}
        except Exception:
            data = {}
        return cls(data)


# ─────────────────────────── UserSettings ───────────────────────────────────

class UserSettings:
    """Все поля snake_case — точно совпадают с тем, что ожидает handlers.py."""

    def __init__(self, row: dict):
        r = row

        # Identity
        self.user_id:          int   = r["user_id"]
        self.username:         str   = r.get("username", "")

        # Access
        self.sub_status:       str   = r.get("sub_status",    "trial")
        self.sub_expires:      float = r.get("sub_expires",   0.0)
        self.signals_received: int   = r.get("signals_received", 0)

        # Scan mode
        self.active:           bool  = bool(r.get("active",       0))
        self.scan_mode:        str   = r.get("scan_mode",         "both")
        self.long_active:      bool  = bool(r.get("long_active",  0))
        self.short_active:     bool  = bool(r.get("short_active", 0))

        # Timeframes
        self.timeframe:        str   = r.get("timeframe",     "1h")
        self.long_tf:          str   = r.get("long_tf",       "1h")
        self.short_tf:         str   = r.get("short_tf",      "1h")

        # Intervals
        self.scan_interval:    int   = r.get("scan_interval",   3600)
        self.long_interval:    int   = r.get("long_interval",   3600)
        self.short_interval:   int   = r.get("short_interval",  3600)

        # Shared flat settings (snake_case — как в БД и handlers.py)
        self.pivot_strength:      int   = r.get("pivot_strength",      7)
        self.max_level_age:       int   = r.get("max_level_age",       100)
        self.max_retest_bars:     int   = r.get("max_retest_bars",      30)
        self.zone_buffer:         float = r.get("zone_buffer",          0.3)
        self.ema_fast:            int   = r.get("ema_fast",             50)
        self.ema_slow:            int   = r.get("ema_slow",             200)
        self.htf_ema_period:      int   = r.get("htf_ema_period",       50)
        self.rsi_period:          int   = r.get("rsi_period",           14)
        self.rsi_ob:              int   = r.get("rsi_ob",               65)
        self.rsi_os:              int   = r.get("rsi_os",               35)
        self.vol_mult:            float = r.get("vol_mult",             1.0)
        self.vol_len:             int   = r.get("vol_len",              20)
        self.atr_period:          int   = r.get("atr_period",           14)
        self.atr_mult:            float = r.get("atr_mult",             1.0)
        self.max_risk_pct:        float = r.get("max_risk_pct",         1.5)
        self.max_signal_risk_pct: float = r.get("max_signal_risk_pct",  0.0)
        self.tp1_rr:              float = r.get("tp1_rr",               0.8)
        self.tp2_rr:              float = r.get("tp2_rr",               1.5)
        self.tp3_rr:              float = r.get("tp3_rr",               2.5)
        self.cooldown_bars:       int   = r.get("cooldown_bars",        5)
        self.min_volume_usdt:     float = r.get("min_volume_usdt",      1_000_000.0)
        self.min_quality:         int   = r.get("min_quality",          2)
        self.min_risk_level:      str   = r.get("min_risk_level",       "all")
        self.analysis_mode:       str   = r.get("analysis_mode",        "both")

        # Filters (shared)
        self.use_rsi:      bool = bool(r.get("use_rsi",      1))
        self.use_volume:   bool = bool(r.get("use_volume",   1))
        self.use_pattern:  bool = bool(r.get("use_pattern",  0))
        self.use_htf:      bool = bool(r.get("use_htf",      0))
        self.use_session:  bool = bool(r.get("use_session",  0))

        # SMC (shared) — snake_case совпадает с БД и с key в cb_smc_toggle
        self.smc_use_bos:   bool = bool(r.get("smc_use_bos",   1))
        self.smc_use_ob:    bool = bool(r.get("smc_use_ob",    1))
        self.smc_use_fvg:   bool = bool(r.get("smc_use_fvg",   0))
        self.smc_use_sweep: bool = bool(r.get("smc_use_sweep", 0))
        self.smc_use_choch: bool = bool(r.get("smc_use_choch", 0))
        self.smc_use_conf:  bool = bool(r.get("smc_use_conf",  0))

        # Notifications
        self.notify_signal:   bool = bool(r.get("notify_signal",   1))
        self.notify_breakout: bool = bool(r.get("notify_breakout", 0))

        # Per-direction configs (хранятся как JSON в БД)
        # handlers.py сбрасывает через user.long_cfg = "{}"
        # поэтому _long_cfg_raw — строка, _long_cfg — объект
        self._long_cfg_raw:  str = r.get("long_cfg",  "{}")
        self._short_cfg_raw: str = r.get("short_cfg", "{}")

        self._long_cfg:  Optional[TradeCfg] = (
            TradeCfg.from_json(self._long_cfg_raw)
            if self._long_cfg_raw and self._long_cfg_raw.strip() not in ("", "{}")
            else None
        )
        self._short_cfg: Optional[TradeCfg] = (
            TradeCfg.from_json(self._short_cfg_raw)
            if self._short_cfg_raw and self._short_cfg_raw.strip() not in ("", "{}")
            else None
        )

    # ── long_cfg / short_cfg как свойства ────────────────────────────────────
    # handlers.py делает: user.long_cfg = "{}"  для сброса
    # и get_long_cfg() / set_long_cfg() для работы с объектом

    @property
    def long_cfg(self) -> str:
        return self._long_cfg.to_json() if self._long_cfg else "{}"

    @long_cfg.setter
    def long_cfg(self, value):
        """Принимает строку '{}' (сброс) или TradeCfg объект"""
        if isinstance(value, str):
            self._long_cfg = None if value.strip() in ("", "{}") else TradeCfg.from_json(value)
        elif isinstance(value, TradeCfg):
            self._long_cfg = value
        else:
            self._long_cfg = None

    @property
    def short_cfg(self) -> str:
        return self._short_cfg.to_json() if self._short_cfg else "{}"

    @short_cfg.setter
    def short_cfg(self, value):
        if isinstance(value, str):
            self._short_cfg = None if value.strip() in ("", "{}") else TradeCfg.from_json(value)
        elif isinstance(value, TradeCfg):
            self._short_cfg = value
        else:
            self._short_cfg = None

    # ── Методы для работы с per-direction cfg ────────────────────────────────

    def get_long_cfg(self) -> TradeCfg:
        """LONG конфиг или shared (из flat полей)"""
        return self._long_cfg if self._long_cfg is not None else self._make_shared_cfg()

    def get_short_cfg(self) -> TradeCfg:
        """SHORT конфиг или shared (из flat полей)"""
        return self._short_cfg if self._short_cfg is not None else self._make_shared_cfg()

    def set_long_cfg(self, cfg: TradeCfg):
        self._long_cfg = cfg

    def set_short_cfg(self, cfg: TradeCfg):
        self._short_cfg = cfg

    @property
    def shared_cfg(self) -> TradeCfg:
        return self._make_shared_cfg()

    def _make_shared_cfg(self) -> TradeCfg:
        """Создаёт TradeCfg из текущих flat полей пользователя"""
        return TradeCfg({
            "smc_use_bos":         self.smc_use_bos,
            "smc_use_ob":          self.smc_use_ob,
            "smc_use_fvg":         self.smc_use_fvg,
            "smc_use_sweep":       self.smc_use_sweep,
            "smc_use_choch":       self.smc_use_choch,
            "smc_use_conf":        self.smc_use_conf,
            "pivot_strength":      self.pivot_strength,
            "max_level_age":       self.max_level_age,
            "max_retest_bars":     self.max_retest_bars,
            "zone_buffer":         self.zone_buffer,
            "ema_fast":            self.ema_fast,
            "ema_slow":            self.ema_slow,
            "htf_ema_period":      self.htf_ema_period,
            "rsi_period":          self.rsi_period,
            "rsi_ob":              self.rsi_ob,
            "rsi_os":              self.rsi_os,
            "vol_mult":            self.vol_mult,
            "vol_len":             self.vol_len,
            "use_rsi":             self.use_rsi,
            "use_volume":          self.use_volume,
            "use_pattern":         self.use_pattern,
            "use_htf":             self.use_htf,
            "use_session":         self.use_session,
            "atr_period":          self.atr_period,
            "atr_mult":            self.atr_mult,
            "max_risk_pct":        self.max_risk_pct,
            "max_signal_risk_pct": self.max_signal_risk_pct,
            "tp1_rr":              self.tp1_rr,
            "tp2_rr":              self.tp2_rr,
            "tp3_rr":              self.tp3_rr,
            "cooldown_bars":       self.cooldown_bars,
            "min_quality":         self.min_quality,
            "min_volume_usdt":     self.min_volume_usdt,
            "analysis_mode":       self.analysis_mode,
        })

    # ── Access ────────────────────────────────────────────────────────────────

    def check_access(self) -> tuple:
        if self.sub_status == "banned":
            return False, "Доступ заблокирован"
        if self.sub_status == "trial":
            return True, "trial"
        if self.sub_status == "active":
            if self.sub_expires > time.time():
                return True, "active"
            self.sub_status = "expired"
        return False, "Подписка истекла"

    def grant_access(self, days: int):
        self.sub_status  = "active"
        self.sub_expires = time.time() + days * 86400

    @property
    def time_left_str(self) -> str:
        if self.sub_status != "active":
            return "—"
        left = self.sub_expires - time.time()
        if left <= 0:
            return "истекла"
        d = int(left / 86400)
        h = int((left % 86400) / 3600)
        return f"{d}д {h}ч" if d > 0 else f"{h}ч"

    # ── Сериализация в dict для БД ────────────────────────────────────────────

    def to_db_dict(self) -> dict:
        return {
            "user_id":             self.user_id,
            "username":            self.username,
            "active":              int(self.active),
            "sub_status":          self.sub_status,
            "sub_expires":         self.sub_expires,
            "signals_received":    self.signals_received,
            "scan_mode":           self.scan_mode,
            "long_active":         int(self.long_active),
            "short_active":        int(self.short_active),
            "timeframe":           self.timeframe,
            "long_tf":             self.long_tf,
            "short_tf":            self.short_tf,
            "scan_interval":       self.scan_interval,
            "long_interval":       self.long_interval,
            "short_interval":      self.short_interval,
            "pivot_strength":      self.pivot_strength,
            "max_level_age":       self.max_level_age,
            "max_retest_bars":     self.max_retest_bars,
            "zone_buffer":         self.zone_buffer,
            "ema_fast":            self.ema_fast,
            "ema_slow":            self.ema_slow,
            "htf_ema_period":      self.htf_ema_period,
            "rsi_period":          self.rsi_period,
            "rsi_ob":              self.rsi_ob,
            "rsi_os":              self.rsi_os,
            "vol_mult":            self.vol_mult,
            "vol_len":             self.vol_len,
            "atr_period":          self.atr_period,
            "atr_mult":            self.atr_mult,
            "max_risk_pct":        self.max_risk_pct,
            "max_signal_risk_pct": self.max_signal_risk_pct,
            "tp1_rr":              self.tp1_rr,
            "tp2_rr":              self.tp2_rr,
            "tp3_rr":              self.tp3_rr,
            "cooldown_bars":       self.cooldown_bars,
            "min_volume_usdt":     self.min_volume_usdt,
            "min_quality":         self.min_quality,
            "min_risk_level":      self.min_risk_level,
            "analysis_mode":       self.analysis_mode,
            "use_rsi":             int(self.use_rsi),
            "use_volume":          int(self.use_volume),
            "use_pattern":         int(self.use_pattern),
            "use_htf":             int(self.use_htf),
            "use_session":         int(self.use_session),
            "smc_use_bos":         int(self.smc_use_bos),
            "smc_use_ob":          int(self.smc_use_ob),
            "smc_use_fvg":         int(self.smc_use_fvg),
            "smc_use_sweep":       int(self.smc_use_sweep),
            "smc_use_choch":       int(self.smc_use_choch),
            "smc_use_conf":        int(self.smc_use_conf),
            "notify_signal":       int(self.notify_signal),
            "notify_breakout":     int(self.notify_breakout),
            "long_cfg":            self._long_cfg.to_json()  if self._long_cfg  else "{}",
            "short_cfg":           self._short_cfg.to_json() if self._short_cfg else "{}",
        }


# ─────────────────────────── UserManager ────────────────────────────────────

class UserManager:
    """
    Кэш в памяти + SQLite персистентность.
    После деплоя данные загружаются из БД автоматически.
    """

    def __init__(self):
        self._cache: dict[int, UserSettings] = {}

    async def get(self, user_id: int) -> Optional[UserSettings]:
        if user_id in self._cache:
            return self._cache[user_id]
        row = await db.db_get_user(user_id)
        if row:
            u = UserSettings(row)
            self._cache[user_id] = u
            return u
        return None

    async def get_or_create(self, user_id: int, username: str = "") -> UserSettings:
        u = await self.get(user_id)
        if u:
            if username and u.username != username:
                u.username = username
                await self.save(u)
            return u
        now = time.time()
        await db.db_upsert_user({
            "user_id":    user_id,
            "username":   username,
            "created_at": now,
            "updated_at": now,
            "sub_status":  "trial",
            "sub_expires": now + 3 * 86400,
        })
        row = await db.db_get_user(user_id)
        u = UserSettings(row)
        self._cache[user_id] = u
        log.info(f"Новый пользователь: {username} ({user_id})")
        return u

    async def save(self, user: UserSettings):
        await db.db_upsert_user(user.to_db_dict())
        self._cache[user.user_id] = user

    async def get_active_users(self) -> list[UserSettings]:
        rows = await db.db_get_active_users()
        result = []
        for row in rows:
            u = UserSettings(row)
            self._cache[u.user_id] = u
            result.append(u)
        return result

    async def stats_summary(self) -> dict:
        return await db.db_stats_summary()
