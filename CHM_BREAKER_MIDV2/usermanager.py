"""
usermanager.py — управление пользователями и настройками
Версия для CHM BREAKER BOT v4.8
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeCfg:
    """Конфигурация для LONG или SHORT направления"""
    # SMC
    smcusebos: bool = True
    smcuseob: bool = True
    smcusefvg: bool = True
    smcusesweep: bool = True
    smcusechoch: bool = False
    smcuseconf: bool = False

    # Levels
    pivotstrength: int = 5
    maxlevelage: int = 50
    maxretestbars: int = 10
    zonebuffer: float = 0.5

    # EMA
    emafast: int = 20
    emaslow: int = 50
    htfemaperiod: int = 200

    # RSI
    rsiperiod: int = 14
    rsiob: int = 70
    rsios: int = 30

    # Volume
    volmult: float = 1.5
    vollen: int = 20

    # Risk/TP
    atrperiod: int = 14
    atrmult: float = 1.5
    maxriskpct: float = 2.0
    maxsignalriskpct: float = 0.0
    tp1rr: float = 1.5
    tp2rr: float = 2.5
    tp3rr: float = 4.0

    # Cooldown
    cooldownbars: int = 3

    # Filters
    usersi: bool = False
    usevolume: bool = False
    usepattern: bool = False
    usehtf: bool = False
    usesession: bool = False

    # Quality & Volume
    minquality: int = 3
    minvolumeusdt: float = 1_000_000.0

    analysismode: str = "both"  # "levels", "smc", "both"


class UserSettings:
    """Настройки пользователя"""

    def __init__(self, userid: int, username: str = ""):
        self.userid = userid
        self.username = username

        # Access
        self.substatus = "trial"  # "trial", "active", "expired", "banned"
        self.expiresat = 0.0
        self.signalsreceived = 0

        # Scan mode
        self.active = False
        self.scanmode = "both"  # "both" или раздельно
        self.longactive = False
        self.shortactive = False

        # Timeframes
        self.timeframe = "15m"
        self.longtf = "15m"
        self.shorttf = "15m"

        # Intervals
        self.scaninterval = 300  # seconds
        self.longinterval = 300
        self.shortinterval = 300

        # Configs
        self.sharedcfg = TradeCfg()
        self.longcfg: Optional[TradeCfg] = None
        self.shortcfg: Optional[TradeCfg] = None

        # Notifications
        self.notifysignal = True
        self.notifybreakout = False

        # Risk level filter
        self.minrisklevel = "all"  # "all", "low", "medium", "high"

    # ── Методы доступа ──

    def check_access(self) -> tuple:
        """Проверка доступа. Возвращает (ok: bool, status: str)"""
        if self.substatus == "banned":
            return False, "Доступ заблокирован"

        now = time.time()
        if self.substatus == "trial":
            # Trial всегда ok
            return True, "trial"

        if self.substatus == "active":
            if self.expiresat > now:
                return True, "active"
            else:
                self.substatus = "expired"
                return False, "Подписка истекла"

        if self.substatus == "expired":
            return False, "Подписка истекла"

        return False, "Неизвестный статус"

    def grantaccess(self, days: int):
        """Выдать доступ на N дней"""
        now = time.time()
        self.substatus = "active"
        self.expiresat = now + days * 86400

    @property
    def timeleftstr(self) -> str:
        """Строка с оставшимся временем"""
        if self.substatus != "active":
            return "—"
        now = time.time()
        left = self.expiresat - now
        if left <= 0:
            return "истекла"
        days = int(left / 86400)
        hours = int((left % 86400) / 3600)
        if days > 0:
            return f"{days}д {hours}ч"
        return f"{hours}ч"

    # ── Per-direction configs ──

    def getlongcfg(self) -> TradeCfg:
        """Получить LONG конфиг (или shared)"""
        if self.longcfg:
            return self.longcfg
        return self.sharedcfg

    def getshortcfg(self) -> TradeCfg:
        """Получить SHORT конфиг (или shared)"""
        if self.shortcfg:
            return self.shortcfg
        return self.sharedcfg

    def setlongcfg(self, cfg: TradeCfg):
        """Установить LONG конфиг"""
        self.longcfg = cfg

    def setshortcfg(self, cfg: TradeCfg):
        """Установить SHORT конфиг"""
        self.shortcfg = cfg

    # ── Proxy к sharedcfg для обратной совместимости ──

    @property
    def pivotstrength(self): return self.sharedcfg.pivotstrength
    @pivotstrength.setter
    def pivotstrength(self, v): self.sharedcfg.pivotstrength = v

    @property
    def maxlevelage(self): return self.sharedcfg.maxlevelage
    @maxlevelage.setter
    def maxlevelage(self, v): self.sharedcfg.maxlevelage = v

    @property
    def maxretestbars(self): return self.sharedcfg.maxretestbars
    @maxretestbars.setter
    def maxretestbars(self, v): self.sharedcfg.maxretestbars = v

    @property
    def zonebuffer(self): return self.sharedcfg.zonebuffer
    @zonebuffer.setter
    def zonebuffer(self, v): self.sharedcfg.zonebuffer = v

    @property
    def emafast(self): return self.sharedcfg.emafast
    @emafast.setter
    def emafast(self, v): self.sharedcfg.emafast = v

    @property
    def emaslow(self): return self.sharedcfg.emaslow
    @emaslow.setter
    def emaslow(self, v): self.sharedcfg.emaslow = v

    @property
    def htfemaperiod(self): return self.sharedcfg.htfemaperiod
    @htfemaperiod.setter
    def htfemaperiod(self, v): self.sharedcfg.htfemaperiod = v

    @property
    def rsiperiod(self): return self.sharedcfg.rsiperiod
    @rsiperiod.setter
    def rsiperiod(self, v): self.sharedcfg.rsiperiod = v

    @property
    def rsiob(self): return self.sharedcfg.rsiob
    @rsiob.setter
    def rsiob(self, v): self.sharedcfg.rsiob = v

    @property
    def rsios(self): return self.sharedcfg.rsios
    @rsios.setter
    def rsios(self, v): self.sharedcfg.rsios = v

    @property
    def volmult(self): return self.sharedcfg.volmult
    @volmult.setter
    def volmult(self, v): self.sharedcfg.volmult = v

    @property
    def vollen(self): return self.sharedcfg.vollen
    @vollen.setter
    def vollen(self, v): self.sharedcfg.vollen = v

    @property
    def atrperiod(self): return self.sharedcfg.atrperiod
    @atrperiod.setter
    def atrperiod(self, v): self.sharedcfg.atrperiod = v

    @property
    def atrmult(self): return self.sharedcfg.atrmult
    @atrmult.setter
    def atrmult(self, v): self.sharedcfg.atrmult = v

    @property
    def maxriskpct(self): return self.sharedcfg.maxriskpct
    @maxriskpct.setter
    def maxriskpct(self, v): self.sharedcfg.maxriskpct = v

    @property
    def maxsignalriskpct(self): return self.sharedcfg.maxsignalriskpct
    @maxsignalriskpct.setter
    def maxsignalriskpct(self, v): self.sharedcfg.maxsignalriskpct = v

    @property
    def tp1rr(self): return self.sharedcfg.tp1rr
    @tp1rr.setter
    def tp1rr(self, v): self.sharedcfg.tp1rr = v

    @property
    def tp2rr(self): return self.sharedcfg.tp2rr
    @tp2rr.setter
    def tp2rr(self, v): self.sharedcfg.tp2rr = v

    @property
    def tp3rr(self): return self.sharedcfg.tp3rr
    @tp3rr.setter
    def tp3rr(self, v): self.sharedcfg.tp3rr = v

    @property
    def cooldownbars(self): return self.sharedcfg.cooldownbars
    @cooldownbars.setter
    def cooldownbars(self, v): self.sharedcfg.cooldownbars = v

    @property
    def usersi(self): return self.sharedcfg.usersi
    @usersi.setter
    def usersi(self, v): self.sharedcfg.usersi = v

    @property
    def usevolume(self): return self.sharedcfg.usevolume
    @usevolume.setter
    def usevolume(self, v): self.sharedcfg.usevolume = v

    @property
    def usepattern(self): return self.sharedcfg.usepattern
    @usepattern.setter
    def usepattern(self, v): self.sharedcfg.usepattern = v

    @property
    def usehtf(self): return self.sharedcfg.usehtf
    @usehtf.setter
    def usehtf(self, v): self.sharedcfg.usehtf = v

    @property
    def usesession(self): return self.sharedcfg.usesession
    @usesession.setter
    def usesession(self, v): self.sharedcfg.usesession = v

    @property
    def minquality(self): return self.sharedcfg.minquality
    @minquality.setter
    def minquality(self, v): self.sharedcfg.minquality = v

    @property
    def minvolumeusdt(self): return self.sharedcfg.minvolumeusdt
    @minvolumeusdt.setter
    def minvolumeusdt(self, v): self.sharedcfg.minvolumeusdt = v

    @property
    def analysismode(self): return self.sharedcfg.analysismode
    @analysismode.setter
    def analysismode(self, v): self.sharedcfg.analysismode = v

    @property
    def smcusebos(self): return self.sharedcfg.smcusebos
    @smcusebos.setter
    def smcusebos(self, v): self.sharedcfg.smcusebos = v

    @property
    def smcuseob(self): return self.sharedcfg.smcuseob
    @smcuseob.setter
    def smcuseob(self, v): self.sharedcfg.smcuseob = v

    @property
    def smcusefvg(self): return self.sharedcfg.smcusefvg
    @smcusefvg.setter
    def smcusefvg(self, v): self.sharedcfg.smcusefvg = v

    @property
    def smcusesweep(self): return self.sharedcfg.smcusesweep
    @smcusesweep.setter
    def smcusesweep(self, v): self.sharedcfg.smcusesweep = v

    @property
    def smcusechoch(self): return self.sharedcfg.smcusechoch
    @smcusechoch.setter
    def smcusechoch(self, v): self.sharedcfg.smcusechoch = v

    @property
    def smcuseconf(self): return self.sharedcfg.smcuseconf
    @smcuseconf.setter
    def smcuseconf(self, v): self.sharedcfg.smcuseconf = v


class UserManager:
    """Менеджер пользователей (in-memory для простоты)"""

    def __init__(self):
        self.users: dict[int, UserSettings] = {}

    async def get(self, userid: int) -> Optional[UserSettings]:
        """Получить пользователя по ID"""
        return self.users.get(userid)

    async def getorcreate(self, userid: int, username: str = "") -> UserSettings:
        """Получить или создать пользователя"""
        if userid not in self.users:
            self.users[userid] = UserSettings(userid, username)
        return self.users[userid]

    async def saveuser(self, user: UserSettings):
        """Сохранить пользователя (в реальной версии — в БД)"""
        self.users[user.userid] = user

    async def getactiveusers(self) -> list[UserSettings]:
        """Получить всех активных пользователей для сканирования"""
        result = []
        for user in self.users.values():
            ok, _ = user.check_access()
            if ok and (user.active or user.longactive or user.shortactive):
                result.append(user)
        return result

    async def statssummary(self) -> dict:
        """Статистика по всем пользователям"""
        total = len(self.users)
        trial = sum(1 for u in self.users.values() if u.substatus == "trial")
        active = sum(1 for u in self.users.values() if u.substatus == "active")
        expired = sum(1 for u in self.users.values() if u.substatus == "expired")
        banned = sum(1 for u in self.users.values() if u.substatus == "banned")
        scanning = sum(1 for u in self.users.values() if u.active or u.longactive or u.shortactive)

        return {
            "total": total,
            "trial": trial,
            "active": active,
            "expired": expired,
            "banned": banned,
            "scanning": scanning,
        }
