"""
anomaly_detector.py — детектор аномалий объёма и цены.

Реализует двойное кондиционирование (DOUBLE CONDITIONING):
  - volume > ewma_mean * 1.30  И  volume > rolling_max * 0.60

Z-score через EWMA вместо SMA для быстрой адаптации к рынку.
"""

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from pump_dump.pd_config import (
    EWMA_SPAN, ZSCORE_THRESHOLD,
    DOUBLE_COND_MEAN_M, DOUBLE_COND_MAX_M,
    PRICE_SPIKE_1M, PRICE_SPIKE_3M,
)


@dataclass
class AnomalyResult:
    # Объём
    volume_double_cond: bool        # двойное кондиционирование
    volume_zscore: float            # z-score текущего объёма
    volume_anomaly: bool            # zscore >= порог

    # Цена
    price_change_1m: float          # изменение за 1 свечу
    price_change_3m: float          # изменение за 3 свечи
    price_spike: bool               # оба условия выполнены
    spike_dir: Optional[Literal["PUMP", "DUMP"]]

    # CVD (Cumulative Volume Delta)
    cvd_10: float                   # CVD за последние 10 свечей
    cvd_signal: bool                # CVD растёт/падает 5 свечей подряд
    cvd_dir: Optional[Literal["PUMP", "DUMP"]]

    # ATR
    atr_pct: float                  # ATR нормализованный (ATR/close*100)


def detect(df: pd.DataFrame) -> AnomalyResult:
    """
    df — DataFrame со столбцами: open, high, low, close, volume, buy_vol
    Минимум 30 строк.
    """
    if len(df) < 30:
        return _empty_result()

    close   = df["close"].values.astype(float)
    volume  = df["volume"].values.astype(float)
    buy_vol = df["buy_vol"].values.astype(float)
    high    = df["high"].values.astype(float)
    low     = df["low"].values.astype(float)

    cur_vol = volume[-1]

    # ── EWMA baseline ────────────────────────────────────────────────────────
    s = pd.Series(volume)
    ewma_mean = s.ewm(span=EWMA_SPAN).mean().iloc[-1]
    ewma_std  = s.ewm(span=EWMA_SPAN).std().iloc[-1]
    roll_max  = s.rolling(min(len(s), 200)).max().iloc[-1]

    # Двойное кондиционирование
    cond1 = cur_vol > ewma_mean * DOUBLE_COND_MEAN_M
    cond2 = cur_vol > roll_max  * DOUBLE_COND_MAX_M
    double_cond = bool(cond1 and cond2)

    # Z-score
    zscore = float((cur_vol - ewma_mean) / ewma_std) if ewma_std > 0 else 0.0
    vol_anomaly = zscore >= ZSCORE_THRESHOLD

    # ── Ценовые изменения ────────────────────────────────────────────────────
    p1m = float((close[-1] - close[-2]) / close[-2]) if close[-2] > 0 else 0.0
    p3m = float((close[-1] - close[-4]) / close[-4]) if len(close) >= 4 and close[-4] > 0 else 0.0

    spike     = False
    spike_dir = None
    if p1m >= PRICE_SPIKE_1M and p3m >= PRICE_SPIKE_3M:
        spike, spike_dir = True, "PUMP"
    elif p1m <= -PRICE_SPIKE_1M and p3m <= -PRICE_SPIKE_3M:
        spike, spike_dir = True, "DUMP"

    # ── CVD за 10 свечей ─────────────────────────────────────────────────────
    sell_vol = volume - buy_vol
    cvd_series = (buy_vol - sell_vol)[-10:]
    cvd_10  = float(np.sum(cvd_series))

    # CVD растёт или падает последние 5 свечей
    cvd_dir   = None
    cvd_signal = False
    if len(cvd_series) >= 5:
        cumulative = np.cumsum(buy_vol[-10:] - sell_vol[-10:])
        last5 = cumulative[-5:]
        if all(last5[i] < last5[i+1] for i in range(4)):
            cvd_signal, cvd_dir = True, "PUMP"
        elif all(last5[i] > last5[i+1] for i in range(4)):
            cvd_signal, cvd_dir = True, "DUMP"

    # ── ATR ──────────────────────────────────────────────────────────────────
    n    = min(14, len(close) - 1)
    tr   = np.maximum(high[-n:] - low[-n:],
           np.maximum(abs(high[-n:] - close[-n-1:-1]),
                      abs(low[-n:]  - close[-n-1:-1])))
    atr  = float(np.mean(tr))
    atr_pct = atr / close[-1] * 100 if close[-1] > 0 else 0.0

    return AnomalyResult(
        volume_double_cond=double_cond,
        volume_zscore=zscore,
        volume_anomaly=vol_anomaly,
        price_change_1m=p1m,
        price_change_3m=p3m,
        price_spike=spike,
        spike_dir=spike_dir,
        cvd_10=cvd_10,
        cvd_signal=cvd_signal,
        cvd_dir=cvd_dir,
        atr_pct=atr_pct,
    )


def _empty_result() -> AnomalyResult:
    return AnomalyResult(
        volume_double_cond=False, volume_zscore=0.0, volume_anomaly=False,
        price_change_1m=0.0, price_change_3m=0.0,
        price_spike=False, spike_dir=None,
        cvd_10=0.0, cvd_signal=False, cvd_dir=None,
        atr_pct=0.0,
    )
