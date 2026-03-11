"""
hidden_signals.py — скрытые сигналы (ключевое преимущество).

Модуль анализирует данные, которые игнорирует большинство трейдеров:
  A. Funding Rate — перегруженность лонгов/шортов
  B. Open Interest + дивергенция цены — накопление без движения
  C. CVD Divergence — скрытое накопление/распределение Smart Money
  D. Long/Short Ratio — крайний перекос позиций (контртрендовый сетап)
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal, Optional

import aiohttp
import numpy as np
import pandas as pd

from pump_dump.pd_config import (
    BINGX_REST_FUTURES,
    FUNDING_PUMP_THR, FUNDING_DUMP_THR, FUNDING_DELTA_THR,
    OI_PUMP_PCT, OI_DUMP_PCT,
    LONG_SHORT_PUMP, LONG_SHORT_DUMP,
    FUNDING_FETCH_EVERY, OI_FETCH_EVERY,
)

log = logging.getLogger("CHM.PD.Hidden")


@dataclass
class HiddenResult:
    # Funding rate
    funding_rate: float
    funding_signal: bool
    funding_dir: Optional[Literal["PUMP", "DUMP"]]
    funding_delta_bonus: float     # дополнительный вес если funding резко изменился

    # Open Interest
    oi_change_10m: float           # изменение OI за 10 мин (%)
    oi_signal: bool
    oi_dir: Optional[Literal["PUMP", "DUMP"]]

    # CVD Divergence (из свечей)
    cvd_divergence: bool
    cvd_div_dir: Optional[Literal["PUMP", "DUMP"]]

    # Long/Short Ratio
    long_short_ratio: float
    ls_signal: bool
    ls_dir: Optional[Literal["PUMP", "DUMP"]]


class HiddenSignalsCache:
    """Кэш REST-данных (funding, OI, L/S) с периодическим обновлением."""

    def __init__(self):
        self._funding:    dict[str, deque] = defaultdict(lambda: deque(maxlen=2))  # deque[(rate, ts)]
        self._oi_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=15))
        self._ls_ratio:   dict[str, float] = {}
        self._last_funding_fetch: float = 0
        self._last_oi_fetch:     float = 0
        self._last_ls_fetch:     float = 0

    async def refresh_if_needed(self, symbols: list[str]):
        """Обновляет кэш если истёк TTL."""
        now = time.time()
        tasks = []
        if now - self._last_funding_fetch > FUNDING_FETCH_EVERY:
            self._last_funding_fetch = now
            tasks.append(self._fetch_funding(symbols))
        if now - self._last_oi_fetch > OI_FETCH_EVERY:
            self._last_oi_fetch = now
            tasks.append(self._fetch_oi(symbols))
        if now - self._last_ls_fetch > 120:  # каждые 2 минуты
            self._last_ls_fetch = now
            tasks.append(self._fetch_ls_ratio(symbols))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_funding(self, symbols: list[str]):
        url = f"{BINGX_REST_FUTURES}/quote/fundingRate"
        async with aiohttp.ClientSession() as s:
            for sym in symbols:
                try:
                    async with s.get(url, params={"symbol": sym},
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        data = await r.json()
                    rate = float(data.get("data", {}).get("fundingRate", 0))
                    self._funding[sym].append((rate, time.time()))
                except Exception:
                    pass
                await asyncio.sleep(0.05)

    async def _fetch_oi(self, symbols: list[str]):
        url = f"{BINGX_REST_FUTURES}/quote/openInterest"
        async with aiohttp.ClientSession() as s:
            for sym in symbols:
                try:
                    async with s.get(url, params={"symbol": sym},
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        data = await r.json()
                    oi = float(data.get("data", {}).get("openInterest", 0))
                    self._oi_history[sym].append((oi, time.time()))
                except Exception:
                    pass
                await asyncio.sleep(0.05)

    async def _fetch_ls_ratio(self, symbols: list[str]):
        # BingX: GET /openApi/swap/v2/quote/globalLongShortPositionRatio
        url = f"{BINGX_REST_FUTURES}/quote/globalLongShortPositionRatio"
        async with aiohttp.ClientSession() as s:
            for sym in symbols:
                try:
                    async with s.get(url, params={"symbol": sym, "period": "5m", "limit": 1},
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
                        data = await r.json()
                    entries = data.get("data", [])
                    if entries:
                        ratio = float(entries[0].get("longShortRatio", 1.0))
                        self._ls_ratio[sym] = ratio
                except Exception:
                    pass
                await asyncio.sleep(0.05)

    def get_funding(self, sym: str) -> tuple[float, float]:
        """Возвращает (current_rate, prev_rate)."""
        hist = self._funding.get(sym)
        if not hist:
            return 0.0, 0.0
        current_rate = hist[-1][0]
        prev_rate    = hist[0][0]  # первая запись (предыдущая если их 2, иначе та же)
        return current_rate, prev_rate

    def get_oi_change_10m(self, sym: str) -> float:
        """Изменение OI за последние 10 записей (%)."""
        hist = self._oi_history.get(sym)
        if not hist or len(hist) < 2:
            return 0.0
        oldest = hist[0][0]
        newest = hist[-1][0]
        if oldest <= 0:
            return 0.0
        return (newest - oldest) / oldest

    def get_ls_ratio(self, sym: str) -> float:
        return self._ls_ratio.get(sym, 1.0)


# Синглтон кэша
_cache = HiddenSignalsCache()


async def analyze(symbol: str, df: pd.DataFrame, all_symbols: list[str]) -> HiddenResult:
    """
    Асинхронный анализ скрытых сигналов.
    df — DataFrame свечей (для CVD divergence).
    """
    await _cache.refresh_if_needed(all_symbols)

    # ── Funding rate ─────────────────────────────────────────────────────────
    fund_cur, fund_prev = _cache.get_funding(symbol)
    fund_signal = False
    fund_dir    = None
    if fund_cur < FUNDING_PUMP_THR:
        fund_signal, fund_dir = True, "PUMP"    # шорты перегружены → short squeeze
    elif fund_cur > FUNDING_DUMP_THR:
        fund_signal, fund_dir = True, "DUMP"    # лонги перегружены → long squeeze

    fund_delta       = abs(fund_cur - fund_prev)
    fund_delta_bonus = 0.10 if fund_delta > FUNDING_DELTA_THR else 0.0

    # ── Open Interest ────────────────────────────────────────────────────────
    oi_chg = _cache.get_oi_change_10m(symbol)
    price_change = float((df["close"].iloc[-1] - df["close"].iloc[-11]) / df["close"].iloc[-11]) \
        if len(df) >= 11 and df["close"].iloc[-11] > 0 else 0.0

    oi_signal = False
    oi_dir    = None
    if oi_chg > OI_PUMP_PCT and abs(price_change) < 0.005:
        oi_signal, oi_dir = True, "PUMP"   # OI растёт, цена стоит → взрыв близко
    elif oi_chg < -OI_DUMP_PCT and price_change < -0.005:
        oi_signal, oi_dir = True, "DUMP"   # принудительное закрытие лонгов

    # ── CVD Divergence ───────────────────────────────────────────────────────
    cvd_div, cvd_div_dir = _compute_cvd_divergence(df)

    # ── Long/Short Ratio ─────────────────────────────────────────────────────
    ls_ratio  = _cache.get_ls_ratio(symbol)
    ls_signal = False
    ls_dir    = None
    if ls_ratio < LONG_SHORT_PUMP:
        ls_signal, ls_dir = True, "PUMP"   # толпа шортит → short squeeze
    elif ls_ratio > LONG_SHORT_DUMP:
        ls_signal, ls_dir = True, "DUMP"   # толпа лонгует → long squeeze

    return HiddenResult(
        funding_rate=fund_cur,
        funding_signal=fund_signal,
        funding_dir=fund_dir,
        funding_delta_bonus=fund_delta_bonus,
        oi_change_10m=oi_chg,
        oi_signal=oi_signal,
        oi_dir=oi_dir,
        cvd_divergence=cvd_div,
        cvd_div_dir=cvd_div_dir,
        long_short_ratio=ls_ratio,
        ls_signal=ls_signal,
        ls_dir=ls_dir,
    )


def _compute_cvd_divergence(df: pd.DataFrame) -> tuple[bool, Optional[str]]:
    """
    CVD Divergence — Smart Money покупает/продаёт против движения цены.

    Бычья: цена делает новый локальный минимум, CVD — более высокий минимум
    Медвежья: цена делает новый локальный максимум, CVD — более низкий максимум
    """
    if len(df) < 30:
        return False, None

    close   = df["close"].values[-30:]
    buy_vol = df["buy_vol"].values[-30:]
    vol     = df["volume"].values[-30:]
    sell_vol = vol - buy_vol
    cvd     = np.cumsum(buy_vol - sell_vol)

    # Ищем 2 локальных минимума/максимума за последние 30 свечей
    def local_mins(arr, n=5):
        mins = []
        for i in range(n, len(arr) - n):
            if arr[i] == min(arr[i-n:i+n+1]):
                mins.append((i, arr[i]))
        return mins[-2:] if len(mins) >= 2 else []

    def local_maxs(arr, n=5):
        maxs = []
        for i in range(n, len(arr) - n):
            if arr[i] == max(arr[i-n:i+n+1]):
                maxs.append((i, arr[i]))
        return maxs[-2:] if len(maxs) >= 2 else []

    # Бычья дивергенция
    price_mins = local_mins(close)
    if len(price_mins) == 2:
        p1_idx, p1 = price_mins[0]
        p2_idx, p2 = price_mins[1]
        c1, c2 = cvd[p1_idx], cvd[p2_idx]
        if p2 < p1 and c2 > c1:   # цена ниже, CVD выше
            return True, "PUMP"

    # Медвежья дивергенция
    price_maxs = local_maxs(close)
    if len(price_maxs) == 2:
        p1_idx, p1 = price_maxs[0]
        p2_idx, p2 = price_maxs[1]
        c1, c2 = cvd[p1_idx], cvd[p2_idx]
        if p2 > p1 and c2 < c1:   # цена выше, CVD ниже
            return True, "DUMP"

    return False, None
