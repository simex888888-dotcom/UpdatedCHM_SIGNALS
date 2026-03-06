"""
smc/order_block.py — Order Blocks & Breaker Blocks
OB = последняя противоположная свеча перед импульсом (BOS).
"""
import pandas as pd
import numpy as np
import logging
from typing import Optional

log = logging.getLogger("CHM.SMC.OrderBlock")

_EMPTY_OB = {
    "found":     False,
    "ob_low":    0.0,
    "ob_high":   0.0,
    "type":      "",       # "bullish" | "bearish"
    "mitigated": False,
    "bar_ago":   0,
    "is_breaker": False,
}


def _find_impulse_start(df: pd.DataFrame, bos_price: float,
                        direction: str, lookback: int = 50) -> Optional[int]:
    """Ищет начало импульса (бар перед BOS)."""
    closes = df["close"].values
    n = len(closes)
    start = max(0, n - lookback)
    if direction == "BULLISH":
        for i in range(n - 1, start, -1):
            if closes[i] < bos_price and closes[i - 1] < bos_price:
                return i
    else:
        for i in range(n - 1, start, -1):
            if closes[i] > bos_price and closes[i - 1] > bos_price:
                return i
    return None


def find_bullish_ob(df: pd.DataFrame,
                    bos_price: float,
                    min_impulse_pct: float = 0.3,
                    max_age_candles: int = 50) -> dict:
    """
    Bullish OB = последняя медвежья свеча перед бычьим BOS-импульсом.
    Условие: импульс от неё >= min_impulse_pct%.
    """
    result = dict(_EMPTY_OB)
    result["type"] = "bullish"
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    opens  = df["open"].values
    n      = len(df)

    impulse_start = _find_impulse_start(df, bos_price, "BULLISH", max_age_candles)
    if impulse_start is None:
        return result

    # Ищем последнюю медвежью свечу перед impulse_start
    for i in range(impulse_start, max(0, impulse_start - max_age_candles), -1):
        if closes[i] < opens[i]:   # медвежья свеча
            impulse_pct = abs(closes[-1] - closes[i]) / closes[i] * 100
            if impulse_pct >= min_impulse_pct:
                bar_ago = n - 1 - i
                result.update({
                    "found":   True,
                    "ob_low":  float(lows[i]),
                    "ob_high": float(highs[i]),
                    "bar_ago": bar_ago,
                })
                log.debug(f"Bullish OB: {result['ob_low']:.4f}–{result['ob_high']:.4f} @ bar_ago={bar_ago}")
                return result
    return result


def find_bearish_ob(df: pd.DataFrame,
                    bos_price: float,
                    min_impulse_pct: float = 0.3,
                    max_age_candles: int = 50) -> dict:
    """Bearish OB = последняя бычья свеча перед медвежьим BOS-импульсом."""
    result = dict(_EMPTY_OB)
    result["type"] = "bearish"
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    opens  = df["open"].values
    n      = len(df)

    impulse_start = _find_impulse_start(df, bos_price, "BEARISH", max_age_candles)
    if impulse_start is None:
        return result

    for i in range(impulse_start, max(0, impulse_start - max_age_candles), -1):
        if closes[i] > opens[i]:   # бычья свеча
            impulse_pct = abs(closes[i] - closes[-1]) / closes[i] * 100
            if impulse_pct >= min_impulse_pct:
                bar_ago = n - 1 - i
                result.update({
                    "found":   True,
                    "ob_low":  float(lows[i]),
                    "ob_high": float(highs[i]),
                    "bar_ago": bar_ago,
                })
                log.debug(f"Bearish OB: {result['ob_low']:.4f}–{result['ob_high']:.4f} @ bar_ago={bar_ago}")
                return result
    return result


def check_ob_mitigation(df: pd.DataFrame, ob: dict) -> bool:
    """
    Цена мигрировала в зону OB (пришла в неё).
    Bullish OB: цена снизилась до ob_low–ob_high.
    Bearish OB: цена поднялась до ob_low–ob_high.
    """
    if not ob["found"]:
        return False
    lo, hi = ob["ob_low"], ob["ob_high"]
    c_low   = df["low"].iloc[-1]
    c_high  = df["high"].iloc[-1]
    if ob["type"] == "bullish":
        # Митигация — цена зашла в зону сверху вниз
        return c_low <= hi and c_high >= lo
    else:
        # Митигация — цена зашла в зону снизу вверх
        return c_high >= lo and c_low <= hi


def find_breaker_block(df: pd.DataFrame, ob: dict, trend: str) -> dict:
    """
    Breaker Block = мигрированный OB, сменивший роль.
    Bullish OB был митигирован (пробит вниз) → стал медвежьим Breaker.
    Bearish OB был митигирован (пробит вверх) → стал бычьим Breaker.
    """
    result = dict(ob)
    result["is_breaker"] = False
    if not ob["found"] or not ob["mitigated"]:
        return result
    # Если цена полностью пробила OB
    lo, hi = ob["ob_low"], ob["ob_high"]
    current_close = df["close"].iloc[-1]
    if ob["type"] == "bullish" and current_close < lo:
        result["is_breaker"] = True
        result["type"]       = "bearish_breaker"
        log.debug(f"Breaker Block (bearish): {lo:.4f}–{hi:.4f}")
    elif ob["type"] == "bearish" and current_close > hi:
        result["is_breaker"] = True
        result["type"]       = "bullish_breaker"
        log.debug(f"Breaker Block (bullish): {lo:.4f}–{hi:.4f}")
    return result


def get_order_blocks(df: pd.DataFrame,
                     bos: dict,
                     min_impulse_pct: float = 0.3,
                     max_age_candles: int   = 50,
                     mitigated_invalid: bool = True,
                     use_breaker_blocks: bool = True) -> dict:
    """Полный анализ OB для обоих направлений."""
    bull_ob = bear_ob = dict(_EMPTY_OB)

    if bos["detected"]:
        if bos["direction"] == "BULLISH":
            bull_ob = find_bullish_ob(df, bos["price"], min_impulse_pct, max_age_candles)
        else:
            bear_ob = find_bearish_ob(df, bos["price"], min_impulse_pct, max_age_candles)
    else:
        # Без BOS ищем оба OB
        last_high = df["high"].iloc[-1]
        last_low  = df["low"].iloc[-1]
        bull_ob = find_bullish_ob(df, last_high, min_impulse_pct, max_age_candles)
        bear_ob = find_bearish_ob(df, last_low,  min_impulse_pct, max_age_candles)

    # Проверка митигации
    bull_ob["mitigated"] = check_ob_mitigation(df, bull_ob)
    bear_ob["mitigated"] = check_ob_mitigation(df, bear_ob)

    # Инвалидация если полностью митигирован
    if mitigated_invalid:
        c_now = df["close"].iloc[-1]
        if bull_ob["found"] and c_now < bull_ob["ob_low"]:
            bull_ob["found"] = False
        if bear_ob["found"] and c_now > bear_ob["ob_high"]:
            bear_ob["found"] = False

    # Breaker Blocks
    if use_breaker_blocks:
        if bull_ob["mitigated"]:
            bull_ob = find_breaker_block(df, bull_ob, "BULLISH")
        if bear_ob["mitigated"]:
            bear_ob = find_breaker_block(df, bear_ob, "BEARISH")

    return {"bull_ob": bull_ob, "bear_ob": bear_ob}
