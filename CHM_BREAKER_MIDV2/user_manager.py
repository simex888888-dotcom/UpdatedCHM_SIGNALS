"""
user_manager.py — управление пользователями через SQLite
"""

import time
import logging
from dataclasses import dataclass, fields
from typing import Optional
import database as db

log = logging.getLogger("CHM.Users")

TRIAL_SECONDS = 6 * 3600


@dataclass
class UserSettings:
    user_id:          int
    username:         str   = ""
    active:           bool  = False

    sub_status:       str   = "trial"
    sub_expires:      float = 0.0
    trial_started:    float = 0.0
    trial_used:       bool  = False

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

    tp1_rr:           float = 0.8
    tp2_rr:           float = 1.5
    tp3_rr:           float = 2.5

    min_volume_usdt:  float = 1_000_000
    min_quality:      int   = 2
    cooldown_bars:    int   = 5

    notify_signal:    bool  = True
    notify_breakout:  bool  = False

    signals_received: int   = 0

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
        if left <= 0:   return "истёк"
        if left < 3600: return f"{int(left // 60)} мин."
        if left < 86400:
            h = int(left // 3600); m = int((left % 3600) // 60)
            return f"{h}ч {m}м"
        d = int(left // 86400); h = int((left % 86400) // 3600)
        return f"{d}д {h}ч"

    def to_db(self) -> dict:
        """Конвертация в dict для SQLite (bool → int)"""
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            d[f.name] = int(v) if isinstance(v, bool) else v
        return d


def _from_db(row: dict) -> UserSettings:
    """Конвертация из SQLite row в UserSettings (int → bool где нужно)"""
    u = UserSettings(user_id=row["user_id"])
    bool_fields = {
        "active", "trial_used", "use_rsi", "use_volume",
        "use_pattern", "use_htf", "notify_signal", "notify_breakout",
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

    async def get_or_create(self, userid: int, username: str) -> UserSettings:
        row = await db.db_get_user(userid)
        if row:
            return from_db_row(row)
    
        now = time.time()
        user = UserSettings(
            userid=userid,
            username=username,
            substatus="trial",
            trialstarted=now,
            subexpires=now + TRIAL_SECONDS,
            trialused=True,
        )
        await db.db_upsert_user(user.to_db())
        return user


        await db.db_upsert_user(user.to_db())
        log.info(f"Новый юзер: @{username} ({user_id})")
        return user

    async def save(self, user: UserSettings):
        await db.db_upsert_user(user.to_db())

    async def get_active_users(self) -> list[UserSettings]:
        rows = await db.db_get_active_users()
        return [_from_db(r) for r in rows]

    async def all_users(self) -> list[UserSettings]:
        rows = await db.db_get_all_users()
        return [_from_db(r) for r in rows]

    async def stats_summary(self) -> dict:
        return await db.db_stats_summary()
