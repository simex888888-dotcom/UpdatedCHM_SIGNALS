"""
smc/fvg.py — Fair Value Gap (FVG) & Inverted FVG (IFVG)
Bullish FVG:  candle[i-2].high < candle[i].low  (gap between candle i-2 top and candle i bottom)
Bearish FVG:  candle[i-2].low  > candle[i].high
"""
import pandas as pd
import logging

log = logging.getLogger("CHM.SMC.FVG")


def find_fvgs(df: pd.DataFrame,
              min_gap_pct: float = 0.1,
              direction: str = "both") -> list[dict]:
    """
    Сканирует весь датафрейм на FVG.
    direction: "bullish" | "bearish" | "both"
    Возвращает список FVG, самые свежие первыми.
    """
    result = []
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    n      = len(df)

    for i in range(2, n):
        if direction in ("bullish", "both"):
            # Bullish FVG: gap вверх
            gap_low  = float(highs[i - 2])
            gap_high = float(lows[i])
            if gap_high > gap_low:
                gap_pct = (gap_high - gap_low) / gap_low * 100
                if gap_pct >= min_gap_pct:
                    result.append({
                        "type":     "bullish",
                        "fvg_low":  gap_low,
                        "fvg_high": gap_high,
                        "gap_pct":  round(gap_pct, 3),
                        "bar_ago":  n - 1 - i,
                        "idx":      i,
                        "filled":   False,
                        "inversed": False,
                    })

        if direction in ("bearish", "both"):
            # Bearish FVG: gap вниз
            gap_high = float(lows[i - 2])
            gap_low  = float(highs[i])
            if gap_high > gap_low:
                gap_pct = (gap_high - gap_low) / gap_high * 100
                if gap_pct >= min_gap_pct:
                    result.append({
                        "type":     "bearish",
                        "fvg_low":  gap_low,
                        "fvg_high": gap_high,
                        "gap_pct":  round(gap_pct, 3),
                        "bar_ago":  n - 1 - i,
                        "idx":      i,
                        "filled":   False,
                        "inversed": False,
                    })

    # Проверяем какие FVG уже заполнены (частично или полностью)
    current_high = df["high"].iloc[-1]
    current_low  = df["low"].iloc[-1]
    for fvg in result:
        if fvg["type"] == "bullish":
            if current_low <= fvg["fvg_low"]:
                fvg["filled"] = True
        else:
            if current_high >= fvg["fvg_high"]:
                fvg["filled"] = True

    # Возвращаем только незаполненные, свежие первыми
    active = [f for f in result if not f["filled"]]
    active.sort(key=lambda x: x["idx"], reverse=True)
    log.debug(f"FVGs found: {len(active)} active (from {len(result)} total)")
    return active


def find_ifvgs(df: pd.DataFrame, fvg_list: list[dict]) -> list[dict]:
    """
    Inverted FVG: FVG был полностью пройден ценой и теперь работает
    как обратный уровень (поддержка → сопротивление или наоборот).
    """
    current_high = df["high"].iloc[-1]
    current_low  = df["low"].iloc[-1]
    ifvgs = []
    for fvg in fvg_list:
        if not fvg.get("filled"):
            continue
        inv = dict(fvg)
        inv["inversed"] = True
        if fvg["type"] == "bullish":
            # Bullish FVG инвертирован → работает как сопротивление
            inv["type"]    = "ifvg_bearish"
        else:
            inv["type"]    = "ifvg_bullish"
        ifvgs.append(inv)
    return ifvgs


def nearest_fvg(fvg_list: list[dict], price: float,
                direction: str = "bullish") -> dict | None:
    """Возвращает ближайший FVG нужного типа к текущей цене."""
    matching = [f for f in fvg_list
                if f["type"] in (direction, "ifvg_" + direction)
                and not f["filled"]]
    if not matching:
        return None
    return min(matching, key=lambda f: abs((f["fvg_low"] + f["fvg_high"]) / 2 - price))


def get_fvg_analysis(df: pd.DataFrame,
                     min_gap_pct: float = 0.1,
                     inversed_fvg: bool = True,
                     partial_fill_invalid: bool = False) -> dict:
    """Полный FVG анализ для обоих направлений."""
    all_fvgs = find_fvgs(df, min_gap_pct, "both")
    ifvgs    = find_ifvgs(df, all_fvgs) if inversed_fvg else []

    price    = float(df["close"].iloc[-1])
    bull_fvg = nearest_fvg(all_fvgs + ifvgs, price, "bullish")
    bear_fvg = nearest_fvg(all_fvgs + ifvgs, price, "bearish")

    return {
        "all_fvgs":     all_fvgs,
        "ifvgs":        ifvgs,
        "bull_fvg":     bull_fvg,
        "bear_fvg":     bear_fvg,
        "bull_found":   bull_fvg is not None,
        "bear_found":   bear_fvg is not None,
    }
