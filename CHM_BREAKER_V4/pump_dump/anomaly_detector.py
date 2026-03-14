"""
anomaly_detector.py — детектор аномалий объёма и цены.

Реализует двойное кондиционирование (DOUBLE CONDITIONING):
  - volume > ewma_mean * 1.15  И  volume > rolling_max * 0.45

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


def detect(df: pd.DataFrame,
           trade_buy_vol: float = 0.0,
           trade_sell_vol: float = 0.0) -> AnomalyResult:
    """
    df             — DataFrame со столбцами: open, high, low, close, volume, buy_vol
    trade_buy_vol  — суммарный объём покупок из @trade WS (последние 30 тиков)
    trade_sell_vol — суммарный объём продаж из @trade WS (последние 30 тиков)
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

    # Двойное кондиционирование — OR-логика:
    # достаточно превысить EWMA-среднее ИЛИ 30% от 200-барного максимума.
    # Было AND → обе условия выполнялись крайне редко.
    cond1 = cur_vol > ewma_mean * DOUBLE_COND_MEAN_M
    cond2 = cur_vol > roll_max  * DOUBLE_COND_MAX_M
    double_cond = bool(cond1 or cond2)

    # Z-score
    zscore = float((cur_vol - ewma_mean) / ewma_std) if ewma_std > 0 else 0.0
    vol_anomaly = zscore >= ZSCORE_THRESHOLD

    # ── Ценовые изменения ────────────────────────────────────────────────────
    p1m = float((close[-1] - close[-2]) / close[-2]) if close[-2] > 0 else 0.0
    p3m = float((close[-1] - close[-4]) / close[-4]) if len(close) >= 4 and close[-4] > 0 else 0.0

    # Ценовой спайк — OR-логика: достаточно одного из двух условий.
    # Было AND (1.2% за 1m И 2.0% за 3m) — сигнал приходил уже в разгар пампа.
    spike     = False
    spike_dir = None
    if p1m >= PRICE_SPIKE_1M or p3m >= PRICE_SPIKE_3M:
        spike, spike_dir = True, "PUMP"
    elif p1m <= -PRICE_SPIKE_1M or p3m <= -PRICE_SPIKE_3M:
        spike, spike_dir = True, "DUMP"

    # ── CVD за 10 свечей ─────────────────────────────────────────────────────
    sell_vol = volume - buy_vol
    cvd_series = (buy_vol - sell_vol)[-10:]
    cvd_10  = float(np.sum(cvd_series))

    # CVD: проверяем инкрементальные дельты (buy-sell) за последние 5 баров.
    # Кумулятивная сумма не подходит — одно отрицательное значение ломает монотонность.
    # Достаточно 4 из 5 баров в нужном направлении.
    cvd_dir   = None
    cvd_signal = False
    if len(cvd_series) >= 5:
        deltas = (buy_vol - sell_vol)[-5:]
        positive = int((deltas > 0).sum())
        negative = int((deltas < 0).sum())
        if positive >= 4:
            cvd_signal, cvd_dir = True, "PUMP"
        elif negative >= 4:
            cvd_signal, cvd_dir = True, "DUMP"

    # Переопределяем CVD через реальный trade-поток (@trade WS) если данные есть.
    # buy_vol из свечей = volume*0.5 всегда (BingX REST не даёт taker split),
    # поэтому candle CVD = 0 навсегда. Trade stream — единственный честный источник.
    trade_total = trade_buy_vol + trade_sell_vol
    if trade_total > 0:
        buy_ratio = trade_buy_vol / trade_total
        if buy_ratio > 0.62:
            cvd_signal, cvd_dir = True, "PUMP"
        elif buy_ratio < 0.38:
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
