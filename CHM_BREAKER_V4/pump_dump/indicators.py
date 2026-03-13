"""
indicators.py — технические индикаторы для предсказания памп/дамп.

RSI Divergence, MACD histogram expanding, BB Squeeze, VWAP deviation.
Вычисляются на 200 свечах 1m через pandas вручную (без внешних TA-библиотек).
"""

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd


@dataclass
class IndicatorResult:
    rsi: float
    rsi_divergence: bool
    rsi_div_dir: Optional[Literal["PUMP", "DUMP"]]

    macd_histogram: float
    macd_signal: bool          # пересечение + гистограмма растёт 3 свечи
    macd_dir: Optional[Literal["PUMP", "DUMP"]]

    bb_width_pct: float        # BB Width percentile (0–100), низкий = сжатие
    bb_squeeze: bool           # BB Width < 10-й перцентиль → взрыв близко

    volume_ma_ratio: float     # current_vol / ewma_vol(span=20)
    vwap_deviation: float      # отклонение цены от VWAP (%)


def analyze(df: pd.DataFrame) -> IndicatorResult:
    if len(df) < 30:
        return _empty_result()

    close  = df["close"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    rsi_val           = _rsi(close)
    rsi_div, rsi_dir  = _rsi_divergence(close, rsi_val, df)
    macd_h, macd_sig, macd_dir = _macd(close)
    bb_pct, bb_sq     = _bb_squeeze(close)
    vol_ratio         = _vol_ratio(volume)
    vwap_dev          = _vwap_deviation(df)

    return IndicatorResult(
        rsi=rsi_val,
        rsi_divergence=rsi_div,
        rsi_div_dir=rsi_dir,
        macd_histogram=macd_h,
        macd_signal=macd_sig,
        macd_dir=macd_dir,
        bb_width_pct=bb_pct,
        bb_squeeze=bb_sq,
        volume_ma_ratio=vol_ratio,
        vwap_deviation=vwap_dev,
    )


# ── RSI ──────────────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _rsi_series(close: pd.Series, period: int = 14) -> np.ndarray:
    """Вычисляет полную серию RSI за O(n), без повторного перебора."""
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)
    return rsi.values


def _rsi_divergence(close: pd.Series, rsi_now: float,
                    df: pd.DataFrame) -> tuple[bool, Optional[str]]:
    """
    Бычья: цена делает новый минимум, RSI — более высокий минимум.
    Медвежья: цена делает новый максимум, RSI — более низкий максимум.

    O(n) — RSI вычисляется один раз для всей серии.
    """
    if len(df) < 20:
        return False, None

    n = min(len(close), 20)
    prices    = close.values[-n:]
    rsi_slice = _rsi_series(close)[-n:]   # одноразовый O(n) расчёт

    p_min2_idx = n - 1

    # Ищем минимумы (бычья дивергенция)
    p_min1_idx = int(np.argmin(prices[:-3]))
    if prices[p_min2_idx] < prices[p_min1_idx] and rsi_slice[-1] > rsi_slice[p_min1_idx]:
        return True, "PUMP"

    # Ищем максимумы (медвежья дивергенция)
    p_max1_idx = int(np.argmax(prices[:-3]))
    if prices[p_min2_idx] > prices[p_max1_idx] and rsi_slice[-1] < rsi_slice[p_max1_idx]:
        return True, "DUMP"

    return False, None


# ── MACD ─────────────────────────────────────────────────────────────────────

def _macd(close: pd.Series) -> tuple[float, bool, Optional[str]]:
    if len(close) < 35:
        return 0.0, False, None

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    hist  = (macd - sig).values

    cur_h = float(hist[-1])

    # Пересечение произошло в последних 3 свечах
    crossed_up   = any(hist[i] < 0 <= hist[i+1] for i in range(-4, -1))
    crossed_down = any(hist[i] > 0 >= hist[i+1] for i in range(-4, -1))

    # Гистограмма должна расти/падать 3 свечи подряд после пересечения
    growing  = all(hist[-3+i] < hist[-3+i+1] for i in range(2))
    shrinking = all(hist[-3+i] > hist[-3+i+1] for i in range(2))

    if crossed_up and growing:
        return cur_h, True, "PUMP"
    if crossed_down and shrinking:
        return cur_h, True, "DUMP"

    return cur_h, False, None


# ── Bollinger Bands Squeeze ───────────────────────────────────────────────────

def _bb_squeeze(close: pd.Series, period: int = 20) -> tuple[float, bool]:
    if len(close) < period + 10:
        return 50.0, False

    rolling = close.rolling(period)
    upper   = rolling.mean() + 2 * rolling.std()
    lower   = rolling.mean() - 2 * rolling.std()
    width   = (upper - lower) / rolling.mean() * 100  # BB Width %

    width = width.dropna()
    if len(width) < 10:
        return 50.0, False

    cur_w    = float(width.iloc[-1])
    pct_rank = float((width < cur_w).mean() * 100)   # перцентиль текущей ширины

    return pct_rank, pct_rank < 10.0  # BB Squeeze если в нижних 10%


# ── Volume MA Ratio ───────────────────────────────────────────────────────────

def _vol_ratio(volume: pd.Series) -> float:
    ewma_vol = float(volume.ewm(span=20).mean().iloc[-1])
    if ewma_vol <= 0:
        return 1.0
    return float(volume.iloc[-1]) / ewma_vol


# ── VWAP Deviation ────────────────────────────────────────────────────────────

def _vwap_deviation(df: pd.DataFrame) -> float:
    if len(df) < 5:
        return 0.0
    tp    = (df["high"] + df["low"] + df["close"]) / 3
    vol   = df["volume"].replace(0, np.nan)
    vwap  = (tp * vol).cumsum() / vol.cumsum()
    dev   = (df["close"].iloc[-1] - float(vwap.iloc[-1])) / float(vwap.iloc[-1]) * 100 \
            if float(vwap.iloc[-1]) > 0 else 0.0
    return float(dev)


def _empty_result() -> IndicatorResult:
    return IndicatorResult(
        rsi=50.0, rsi_divergence=False, rsi_div_dir=None,
        macd_histogram=0.0, macd_signal=False, macd_dir=None,
        bb_width_pct=50.0, bb_squeeze=False,
        volume_ma_ratio=1.0, vwap_deviation=0.0,
    )
