"""
smc/structure.py — Market Structure Analysis
BOS (Break of Structure), CHoCH (Change of Character), Swing H/L
"""
import numpy as np
import pandas as pd
import logging
from typing import Optional

log = logging.getLogger("CHM.SMC.Structure")


def find_swing_highs(df: pd.DataFrame, lookback: int = 10) -> list[dict]:
    """Swing High: highs[i] == max(highs[i-n:i+n+1])."""
    highs = df["high"].values
    result = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        window = highs[max(0, i - lookback): i + lookback + 1]
        if highs[i] == window.max():
            result.append({
                "idx":   i,
                "price": float(highs[i]),
                "bar":   n - 1 - i,          # bars ago
                "ts":    df.index[i],
            })
    return result


def find_swing_lows(df: pd.DataFrame, lookback: int = 10) -> list[dict]:
    """Swing Low: lows[i] == min(lows[i-n:i+n+1])."""
    lows = df["low"].values
    result = []
    n = len(lows)
    for i in range(lookback, n - lookback):
        window = lows[max(0, i - lookback): i + lookback + 1]
        if lows[i] == window.min():
            result.append({
                "idx":   i,
                "price": float(lows[i]),
                "bar":   n - 1 - i,
                "ts":    df.index[i],
            })
    return result


def detect_trend(swing_highs: list[dict], swing_lows: list[dict]) -> str:
    """
    HH + HL → BULLISH
    LH + LL → BEARISH
    Иначе → RANGING
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "RANGING"
    # Последние 2 свинга
    sh = sorted(swing_highs, key=lambda x: x["idx"])[-2:]
    sl = sorted(swing_lows,  key=lambda x: x["idx"])[-2:]
    hh = sh[-1]["price"] > sh[-2]["price"]   # Higher High
    hl = sl[-1]["price"] > sl[-2]["price"]   # Higher Low
    lh = sh[-1]["price"] < sh[-2]["price"]   # Lower High
    ll = sl[-1]["price"] < sl[-2]["price"]   # Lower Low
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "RANGING"


def detect_bos(df: pd.DataFrame,
               swing_highs: list[dict],
               swing_lows: list[dict],
               confirm_close: bool = True) -> dict:
    """
    BOS BULLISH: закрытие выше предыдущего swing high.
    BOS BEARISH: закрытие ниже предыдущего swing low.
    """
    close = df["close"].values
    result = {"detected": False, "price": 0.0, "direction": "", "bar_ago": 0}
    if not swing_highs or not swing_lows:
        return result

    last_sh = sorted(swing_highs, key=lambda x: x["idx"])
    last_sl = sorted(swing_lows,  key=lambda x: x["idx"])

    # BOS UP: текущая цена выше последнего swing high
    if last_sh:
        prev_sh = last_sh[-1]
        if confirm_close:
            crossed = close[-1] > prev_sh["price"]
        else:
            crossed = df["high"].iloc[-1] > prev_sh["price"]
        if crossed:
            result = {
                "detected": True,
                "price":    prev_sh["price"],
                "direction": "BULLISH",
                "bar_ago":   prev_sh["bar"],
            }
            return result

    # BOS DOWN
    if last_sl:
        prev_sl = last_sl[-1]
        if confirm_close:
            crossed = close[-1] < prev_sl["price"]
        else:
            crossed = df["low"].iloc[-1] < prev_sl["price"]
        if crossed:
            result = {
                "detected": True,
                "price":    prev_sl["price"],
                "direction": "BEARISH",
                "bar_ago":   prev_sl["bar"],
            }
    return result


def detect_choch(df: pd.DataFrame,
                 swing_highs: list[dict],
                 swing_lows: list[dict]) -> dict:
    """
    CHoCH — первый признак смены тренда (противоположный BOS).
    CHoCH UP:   в нисходящем тренде цена пробивает предыдущий LH.
    CHoCH DOWN: в восходящем тренде цена пробивает предыдущий HL.
    """
    trend = detect_trend(swing_highs, swing_lows)
    close = df["close"].values
    result = {"detected": False, "price": 0.0, "direction": "", "bar_ago": 0}

    if trend == "BEARISH" and swing_highs:
        # CHoCH UP: закрытие выше последнего LH
        prev_sh = sorted(swing_highs, key=lambda x: x["idx"])[-1]
        if close[-1] > prev_sh["price"]:
            result = {
                "detected": True,
                "price":    prev_sh["price"],
                "direction": "UP",
                "bar_ago":   prev_sh["bar"],
            }
    elif trend == "BULLISH" and swing_lows:
        # CHoCH DOWN: закрытие ниже последнего HL
        prev_sl = sorted(swing_lows, key=lambda x: x["idx"])[-1]
        if close[-1] < prev_sl["price"]:
            result = {
                "detected": True,
                "price":    prev_sl["price"],
                "direction": "DOWN",
                "bar_ago":   prev_sl["bar"],
            }
    return result


def get_market_structure(df: pd.DataFrame,
                         lookback: int = 10,
                         bos_confirm: bool = True,
                         choch_enabled: bool = True) -> dict:
    """Полный анализ рыночной структуры."""
    lookback = max(lookback, 2)
    if len(df) < lookback * 3:
        return {
            "trend": "RANGING",
            "swing_highs": [], "swing_lows": [],
            "bos": {"detected": False, "price": 0.0, "direction": "", "bar_ago": 0},
            "choch": {"detected": False, "price": 0.0, "direction": "", "bar_ago": 0},
            "last_swing_high": None,
            "last_swing_low":  None,
        }
    sh = find_swing_highs(df, lookback)
    sl = find_swing_lows(df, lookback)
    trend = detect_trend(sh, sl)
    bos   = detect_bos(df, sh, sl, bos_confirm)
    choch = detect_choch(df, sh, sl) if choch_enabled else {"detected": False}

    last_sh = sorted(sh, key=lambda x: x["idx"])[-1] if sh else None
    last_sl = sorted(sl, key=lambda x: x["idx"])[-1] if sl else None

    log.debug(f"Structure: trend={trend} bos={bos['detected']} choch={choch['detected']}")
    return {
        "trend":          trend,
        "swing_highs":    sh,
        "swing_lows":     sl,
        "bos":            bos,
        "choch":          choch,
        "last_swing_high": last_sh,
        "last_swing_low":  last_sl,
    }
