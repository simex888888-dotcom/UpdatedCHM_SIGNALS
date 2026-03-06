"""
smc/liquidity.py — Liquidity Sweeps & Equal Highs/Lows
"""
import pandas as pd
import logging

log = logging.getLogger("CHM.SMC.Liquidity")


def find_equal_levels(levels: list[dict],
                      threshold_pct: float = 0.05) -> list[dict]:
    """
    Находит кластеры уровней с одинаковой ценой (±threshold_pct%).
    Возвращает список групп equal levels.
    """
    if not levels:
        return []
    sorted_lvls = sorted(levels, key=lambda x: x["price"])
    groups: list[dict] = []
    group = [sorted_lvls[0]]
    for lvl in sorted_lvls[1:]:
        ref   = group[0]["price"]
        diff  = abs(lvl["price"] - ref) / ref * 100
        if diff <= threshold_pct:
            group.append(lvl)
        else:
            if len(group) >= 2:
                avg_price = sum(x["price"] for x in group) / len(group)
                groups.append({
                    "price":  avg_price,
                    "count":  len(group),
                    "levels": group,
                    "type":   group[0].get("type", ""),
                })
            group = [lvl]
    if len(group) >= 2:
        avg_price = sum(x["price"] for x in group) / len(group)
        groups.append({
            "price":  avg_price,
            "count":  len(group),
            "levels": group,
            "type":   group[0].get("type", ""),
        })
    return groups


def detect_liquidity_sweep(df: pd.DataFrame,
                           equal_level: dict,
                           direction: str,
                           close_required: bool = True,
                           wick_ratio: float = 0.6) -> dict:
    """
    Sweep LONG (direction=UP):
      - цена прошла ниже equal_low, потом закрылась выше
      - wick_ratio: тело свечи / фитиль >= wick_ratio
    Sweep SHORT (direction=DOWN):
      - аналогично для equal_high
    """
    result = {"swept": False, "level": equal_level["price"],
              "direction": direction, "wick_ratio": 0.0}
    if len(df) < 5:
        return result
    lvl = equal_level["price"]
    c   = df.iloc[-1]
    body = abs(c["close"] - c["open"])
    rng  = max(c["high"] - c["low"], 1e-10)

    if direction == "UP":
        # Вышли ниже уровня, потом закрылись выше
        swept_below = c["low"] < lvl
        closed_back = c["close"] > lvl if close_required else True
        lw = min(c["close"], c["open"]) - c["low"]   # нижний фитиль
        wr = lw / rng
        if swept_below and closed_back and wr >= wick_ratio:
            result.update({"swept": True, "wick_ratio": round(wr, 3)})
    else:
        # Вышли выше уровня, потом закрылись ниже
        swept_above = c["high"] > lvl
        closed_back = c["close"] < lvl if close_required else True
        uw = c["high"] - max(c["close"], c["open"])   # верхний фитиль
        wr = uw / rng
        if swept_above and closed_back and wr >= wick_ratio:
            result.update({"swept": True, "wick_ratio": round(wr, 3)})
    return result


def find_liquidity_sweeps(df: pd.DataFrame,
                          swing_highs: list[dict],
                          swing_lows: list[dict],
                          threshold_pct: float = 0.05,
                          close_required: bool = True,
                          wick_ratio: float = 0.6) -> dict:
    """Полный поиск ликвидности: Equal H/L + Sweep detection."""
    # Тегируем типы
    for sh in swing_highs:
        sh["type"] = "high"
    for sl in swing_lows:
        sl["type"] = "low"

    eq_highs = find_equal_levels(swing_highs, threshold_pct)
    eq_lows  = find_equal_levels(swing_lows,  threshold_pct)

    sweep_up   = {"swept": False, "level": 0.0, "direction": "UP"}
    sweep_down = {"swept": False, "level": 0.0, "direction": "DOWN"}

    for eq in eq_lows:
        res = detect_liquidity_sweep(df, eq, "UP", close_required, wick_ratio)
        if res["swept"]:
            sweep_up = res
            break

    for eq in reversed(eq_highs):
        res = detect_liquidity_sweep(df, eq, "DOWN", close_required, wick_ratio)
        if res["swept"]:
            sweep_down = res
            break

    log.debug(
        f"Liquidity: eq_highs={len(eq_highs)} eq_lows={len(eq_lows)} "
        f"sweep_up={sweep_up['swept']} sweep_down={sweep_down['swept']}"
    )
    return {
        "equal_highs": eq_highs,
        "equal_lows":  eq_lows,
        "sweep_up":    sweep_up,
        "sweep_down":  sweep_down,
    }
