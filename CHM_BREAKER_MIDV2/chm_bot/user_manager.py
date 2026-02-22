"""
Управление пользователями и их настройками
"""

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("CHM.Users")
USERS_FILE = "users.json"


@dataclass
class UserSettings:
    user_id:        int
    username:       str    = ""
    active:         bool   = False   # включён ли сканер

    # Таймфрейм и интервал
    timeframe:      str    = "1h"
    scan_interval:  int    = 3600

    # Фильтры
    use_rsi:        bool   = True
    use_volume:     bool   = True
    use_pattern:    bool   = True
    use_htf:        bool   = True
    min_quality:    int    = 3

    # Параметры сигнала
    tp1_rr:         float  = 0.8
    tp2_rr:         float  = 1.5
    tp3_rr:         float  = 2.5
    atr_mult:       float  = 1.0
    max_risk_pct:   float  = 1.5
    min_volume_usdt:float  = 1_000_000

    # Уведомления
    notify_signal:   bool  = True
    notify_breakout: bool  = False

    # Статистика
    signals_received: int  = 0


class UserManager:

    def __init__(self):
        self._users: dict[int, UserSettings] = {}
        self._load()

    def _load(self):
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for uid, d in data.items():
                    self._users[int(uid)] = UserSettings(**d)
                log.info(f"Загружено пользователей: {len(self._users)}")
            except Exception as e:
                log.error(f"Ошибка загрузки users.json: {e}")

    def _save(self):
        try:
            data = {str(uid): asdict(u) for uid, u in self._users.items()}
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка сохранения users.json: {e}")

    def get(self, user_id: int) -> Optional[UserSettings]:
        return self._users.get(user_id)

    def get_or_create(self, user_id: int, username: str = "") -> UserSettings:
        if user_id not in self._users:
            self._users[user_id] = UserSettings(user_id=user_id, username=username)
            self._save()
            log.info(f"Новый пользователь: {username} ({user_id})")
        return self._users[user_id]

    def save_user(self, user: UserSettings):
        self._users[user.user_id] = user
        self._save()

    def get_active_users(self) -> list[UserSettings]:
        return [u for u in self._users.values() if u.active]

    def all_users(self) -> list[UserSettings]:
        return list(self._users.values())
