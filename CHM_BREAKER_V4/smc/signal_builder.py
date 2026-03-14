"""
smc/signal_builder.py — Signal Scoring, Entry/SL/TP, Narrative Generation
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("CHM.SMC.SignalBuilder")


def _fp(v: float) -> str:
    """Форматирует цену без научной нотации, сохраняя полную точность."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v <= 0:      return "0"
    if v >= 10_000: return f"{v:,.0f}"
    if v >= 100:    return f"{v:,.1f}"
    if v >= 1:      return f"{v:.4f}".rstrip("0").rstrip(".")
    decimals = -math.floor(math.log10(v)) + 3
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")

GRADES = {5: "🔥 A+", 4: "✅ A", 3: "⚡ B"}


@dataclass
class SMCSignalResult:
    symbol:        str
    direction:     str        # "LONG" | "SHORT"
    score:         int        # 0–5
    grade:         str        # "A+" | "A" | "B"
    entry_low:     float
    entry_high:    float
    entry:         float      # mid of entry zone
    sl:            float
    tp1:           float
    tp2:           float
    tp3:           float
    rr:            float      # R:R to TP2
    risk_pct:      float
    confirmations: list       # list of (label, passed)
    narrative:     str
    session:       str = ""
    tf_htf:        str = "4H"
    tf_mtf:        str = "1H"
    tf_ltf:        str = "15m"


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_bullish(analysis: dict) -> tuple[int, list]:
    """
    5 условий LONG:
    [1] HTF структура бычья или CHoCH вверх
    [2] Liquidity Sweep вниз + rejection вверх
    [3] Bullish OB найден и митигирован
    [4] Bullish FVG или IFVG в зоне
    [5] Цена в Discount Zone
    """
    s   = analysis.get("structure", {})
    liq = analysis.get("liquidity", {})
    ob  = analysis.get("ob",        {}).get("bull_ob", {})
    fvg = analysis.get("fvg",       {})
    pd  = analysis.get("pd_zone",   {})

    c1 = (s.get("trend") == "BULLISH"
          or (s.get("choch", {}).get("detected") and s["choch"]["direction"] == "UP"))
    c2 = liq.get("sweep_up", {}).get("swept", False)
    c3 = ob.get("found") and ob.get("mitigated")
    c4 = fvg.get("bull_found", False)
    c5 = pd.get("zone") == "DISCOUNT"

    confirmations = [
        ("HTF структура: бычья / CHoCH вверх", c1),
        ("Liquidity Sweep: ликвидность снята снизу", c2),
        ("Order Block: бычий OB митигирован", c3),
        ("FVG/IFVG: дисбаланс в зоне входа", c4),
        ("Discount Zone: цена в зоне скидки", c5),
    ]
    score = sum(1 for _, v in confirmations if v)
    return score, confirmations


def score_bearish(analysis: dict) -> tuple[int, list]:
    """
    5 условий SHORT:
    [1] HTF структура медвежья или CHoCH вниз
    [2] Liquidity Sweep вверх + rejection вниз
    [3] Bearish OB найден и митигирован
    [4] Bearish FVG или IFVG в зоне
    [5] Цена в Premium Zone
    """
    s   = analysis.get("structure", {})
    liq = analysis.get("liquidity", {})
    ob  = analysis.get("ob",        {}).get("bear_ob", {})
    fvg = analysis.get("fvg",       {})
    pd  = analysis.get("pd_zone",   {})

    c1 = (s.get("trend") == "BEARISH"
          or (s.get("choch", {}).get("detected") and s["choch"]["direction"] == "DOWN"))
    c2 = liq.get("sweep_down", {}).get("swept", False)
    c3 = ob.get("found") and ob.get("mitigated")
    c4 = fvg.get("bear_found", False)
    c5 = pd.get("zone") == "PREMIUM"

    confirmations = [
        ("HTF структура: медвежья / CHoCH вниз", c1),
        ("Liquidity Sweep: ликвидность снята сверху", c2),
        ("Order Block: медвежий OB митигирован", c3),
        ("FVG/IFVG: дисбаланс в зоне входа", c4),
        ("Premium Zone: цена в зоне премии", c5),
    ]
    score = sum(1 for _, v in confirmations if v)
    return score, confirmations


# ── Entry / SL / TP ───────────────────────────────────────────────────────────

def calculate_levels(analysis: dict, direction: str, cfg) -> Optional[dict]:
    """
    Entry  = зона OB
    SL     = экстремум OB ± buffer_pct
    TP1–TP3 = FVG / Structure / Liquidity
    """
    ob_key = "bull_ob" if direction == "LONG" else "bear_ob"
    ob     = analysis.get("ob", {}).get(ob_key, {})
    fvg    = analysis.get("fvg", {})
    liq    = analysis.get("liquidity", {})
    s      = analysis.get("structure", {})

    if not ob.get("found"):
        # Если OB не найден — используем текущую цену как entry
        return None

    entry_low  = ob["ob_low"]
    entry_high = ob["ob_high"]
    entry_mid  = (entry_low + entry_high) / 2
    buf        = cfg.SL_BUFFER_PCT / 100

    if direction == "LONG":
        sl = entry_low * (1.0 - buf)
    else:
        sl = entry_high * (1.0 + buf)

    risk = abs(entry_mid - sl)
    if risk <= 0:
        return None

    # ── Фильтр стопа (ATR + минимум по типу монеты) ───────────────────────
    _MEMCOIN_KW = ("FLOKI","PEPE","SHIB","DOGE","WIF","BONK","NEIRO",
                   "MEME","SATS","TURBO","CATS","ACT","BOME","BOOK")
    _sym_up    = analysis.get("symbol", "").upper()
    is_memcoin = any(k in _sym_up for k in _MEMCOIN_KW)
    is_major   = "BTC" in _sym_up or "ETH" in _sym_up

    cp = analysis.get("current_price", 0.0) or entry_mid
    risk_pct_raw = risk / cp * 100 if cp > 0 else 0.0

    if is_major and risk_pct_raw < 0.15:      # было 0.4
        return None
    if is_memcoin and risk_pct_raw < 0.8:    # было 1.5
        return None
    if not is_major and not is_memcoin and risk_pct_raw < 0.4:  # было 0.8
        return None

    atr = analysis.get("atr", 0.0)
    if atr > 0 and risk < atr * 0.8:         # было 1.5
        return None

    # TP1 = FVG zone или fallback
    # Используем FVG только если он ПОЛНОСТЬЮ за пределами зоны входа
    _MIN_TP1_RR = 1.0  # TP1 минимум 1.0R от entry_mid
    fvg_obj = fvg.get("bull_fvg") if direction == "LONG" else fvg.get("bear_fvg")
    if fvg_obj:
        if direction == "LONG":
            # FVG-цель должна быть выше ВЕРХНЕЙ границы зоны входа
            if fvg_obj["fvg_high"] > entry_high:
                tp1 = fvg_obj["fvg_high"]
            else:
                tp1 = None  # FVG внутри/ниже зоны — не использовать
        else:
            # FVG-цель должна быть ниже НИЖНЕЙ границы зоны входа
            if fvg_obj["fvg_low"] < entry_low:
                tp1 = fvg_obj["fvg_low"]
            else:
                tp1 = None  # FVG внутри/выше зоны — не использовать
    else:
        tp1 = None

    # Fallback: минимум 1.5R от entry_mid
    if tp1 is None:
        if direction == "LONG":
            tp1 = entry_mid + risk * 1.5
        else:
            tp1 = entry_mid - risk * 1.5

    # Гарантируем минимум _MIN_TP1_RR от entry_mid независимо от источника TP1
    if direction == "LONG":
        tp1 = max(tp1, entry_mid + risk * _MIN_TP1_RR)
    else:
        tp1 = min(tp1, entry_mid - risk * _MIN_TP1_RR)

    # TP2 = swing high/low (должен быть за пределами зоны входа)
    sh = s.get("last_swing_high")
    sl_sw = s.get("last_swing_low")
    if direction == "LONG" and sh and sh["price"] > entry_high:
        tp2 = sh["price"]
    elif direction == "SHORT" and sl_sw and sl_sw["price"] < entry_low:
        tp2 = sl_sw["price"]
    else:
        tp2 = entry_high + risk * 2.0 if direction == "LONG" \
              else entry_low - risk * 2.0

    # TP3 = следующая зона ликвидности
    eq_highs = liq.get("equal_highs", [])
    eq_lows  = liq.get("equal_lows",  [])
    tp3 = None
    if direction == "LONG" and eq_highs:
        cands = [e["price"] for e in eq_highs if e["price"] > tp2]
        tp3 = min(cands) if cands else None
    elif direction == "SHORT" and eq_lows:
        cands = [e["price"] for e in eq_lows if e["price"] < tp2]
        tp3 = max(cands) if cands else None
    if tp3 is None:
        tp3 = entry_high + risk * 4.0 if direction == "LONG" \
              else entry_low - risk * 4.0

    # Проверка порядка TP — база от entry_mid с гарантированным расстоянием
    if direction == "LONG":
        if not (entry_high < tp1 <= tp2 <= tp3):
            tp1 = entry_mid + risk * 1.2
            tp2 = entry_mid + risk * 2.5
            tp3 = entry_mid + risk * 4.0
    else:
        if not (entry_low > tp1 >= tp2 >= tp3):
            tp1 = entry_mid - risk * 1.2
            tp2 = entry_mid - risk * 2.5
            tp3 = entry_mid - risk * 4.0

    rr = abs(tp2 - entry_mid) / risk if risk > 0 else 0.0
    # Фильтр RR применяется единожды в build_smc_signal, не здесь

    if entry_mid <= 0:
        return None
    risk_pct = abs(sl - entry_mid) / entry_mid * 100
    return {
        "entry_low":  entry_low,
        "entry_high": entry_high,
        "entry_mid":  entry_mid,
        "sl":         sl,
        "tp1":        tp1,
        "tp2":        tp2,
        "tp3":        tp3,
        "rr":         round(rr, 2),
        "risk_pct":   round(risk_pct, 3),
    }


# ── Narrative ─────────────────────────────────────────────────────────────────

def _trend_sentence(structure: dict, tf_htf: str) -> str:
    trend = structure.get("trend", "RANGING")
    choch = structure.get("choch", {})
    if choch.get("detected") and choch["direction"] == "UP":
        return (f"На {tf_htf} зафиксирована смена характера движения вверх (CHoCH) "
                f"на уровне {_fp(choch['price'])} — первый признак смены тренда "
                f"в пользу покупателей.")
    if choch.get("detected") and choch["direction"] == "DOWN":
        return (f"На {tf_htf} зафиксирована смена характера движения вниз (CHoCH) "
                f"на уровне {_fp(choch['price'])}.")
    if trend == "BULLISH":
        return (f"На {tf_htf} рынок находится в устойчивом восходящем тренде "
                f"с чёткой HH/HL структурой.")
    if trend == "BEARISH":
        return (f"На {tf_htf} рынок находится в нисходящем тренде (LH/LL).")
    return f"На {tf_htf} рынок находится в боковике без чёткой структуры."


def _sweep_sentence(liquidity: dict, direction: str) -> str:
    if direction == "LONG":
        sw = liquidity.get("sweep_up", {})
        if sw.get("swept"):
            lvl = sw.get("level", 0)
            return (f"Перед этим цена сделала sweep ниже уровня {_fp(lvl)}, "
                    f"сняв ликвидность продавцов и stop-loss'ы лонгов, после чего "
                    f"резко отбила вверх — классический манипуляционный сбор "
                    f"ликвидности перед институциональным входом.")
    else:
        sw = liquidity.get("sweep_down", {})
        if sw.get("swept"):
            lvl = sw.get("level", 0)
            return (f"Цена совершила sweep выше {_fp(lvl)}, собрала ликвидность "
                    f"покупателей (equal highs) и резко развернулась — типичная "
                    f"ловушка для розничных лонгов.")
    bos_price = liquidity.get("sweep_up", {}).get("level", 0) or \
                liquidity.get("sweep_down", {}).get("level", 0)
    if bos_price:
        return f"Пробой структуры (BOS) на уровне {_fp(bos_price)} подтвердил намерение рынка."
    return "Явного sweep ликвидности на данном таймфрейме не зафиксировано."


def _ob_sentence(ob_data: dict, direction: str, tf_mtf: str) -> str:
    ob_key = "bull_ob" if direction == "LONG" else "bear_ob"
    ob     = ob_data.get(ob_key, {})
    if not ob.get("found"):
        return "Order Block в зоне входа не обнаружен."
    lo, hi   = ob["ob_low"], ob["ob_high"]
    ob_type  = ob.get("type", "")
    if ob.get("is_breaker"):
        prev = "бычий" if "bullish" in ob_type else "медвежий"
        new  = "медвежий" if direction == "LONG" else "бычий"
        return (f"Бывший {prev} OB пробит и стал Breaker Block'ом "
                f"{_fp(lo)}–{_fp(hi)} — теперь работает как {new} уровень.")
    if direction == "LONG":
        return (f"Цена вернулась в бычий ордер-блок {_fp(lo)}–{_fp(hi)} на {tf_mtf} "
                f"— зону, где институционалы формировали позицию перед последним "
                f"бычьим импульсом с BOS.")
    return (f"Цена поднялась в медвежий ордер-блок {_fp(lo)}–{_fp(hi)} на {tf_mtf} "
            f"— зону предложения, откуда начался последний медвежий импульс.")


def _fvg_sentence(fvg: dict, direction: str) -> str:
    obj = fvg.get("bull_fvg") if direction == "LONG" else fvg.get("bear_fvg")
    if not obj:
        return "FVG в данной зоне отсутствует, вход основан на чистом OB."
    lo, hi   = obj["fvg_low"], obj["fvg_high"]
    inv      = obj.get("inversed", False)
    if inv:
        role = "поддержки" if direction == "LONG" else "сопротивления"
        return (f"Перевёрнутый FVG {_fp(lo)}–{_fp(hi)} выступает как усиленный "
                f"уровень {role} после смены роли.")
    ftype = "bullish" if direction == "LONG" else "bearish"
    return (f"Внутри зоны присутствует {ftype} FVG {_fp(lo)}–{_fp(hi)} "
            f"— дисбаланс, который рынок стремится заполнить, добавляя "
            f"магнетизм к точке входа.")


def _sl_sentence(levels: dict, direction: str, buf_pct: float) -> str:
    sl = levels["sl"]
    if direction == "LONG":
        return (f"Стоп размещён под нижним экстремумом OB с буфером {buf_pct}% "
                f"на уровне {_fp(sl)} — ниже этого уровня бычий контекст теряется.")
    return (f"Стоп размещён над верхним экстремумом OB с буфером {buf_pct}% "
            f"на уровне {_fp(sl)} — выше этого уровня медвежий контекст нарушен.")


def _tp_sentence(levels: dict, direction: str, fvg: dict) -> str:
    tp1, tp3 = levels["tp1"], levels["tp3"]
    fvg_obj  = fvg.get("bull_fvg") if direction == "LONG" else fvg.get("bear_fvg")
    tp1_desc = f"закрытие FVG ({_fp(tp1)})" if fvg_obj else f"структурный уровень ({_fp(tp1)})"
    pct3 = abs(tp3 - levels["entry_mid"]) / levels["entry_mid"] * 100
    return (f"Первая цель — {tp1_desc}, "
            f"финальная цель — следующая зона ликвидности {_fp(tp3)} "
            f"(+{pct3:.1f}%).")


def _invalidation_sentence(levels: dict, direction: str, tf_ltf: str) -> str:
    sl = levels["sl"]
    if direction == "LONG":
        return (f"Сетап теряет силу, если {tf_ltf}-свеча закроется ниже {_fp(sl)} "
                f"— это полная митигация OB и потеря бычьей структуры.")
    return (f"Сетап теряет силу, если {tf_ltf}-свеча закроется выше {_fp(sl)} "
            f"— медвежья структура будет нарушена.")


def generate_narrative(analysis: dict, levels: dict, direction: str,
                       tf_htf: str = "4H", tf_mtf: str = "1H",
                       tf_ltf: str = "15m",
                       show_invalidation: bool = True,
                       cfg = None) -> str:
    """Генерирует полный нарратив на русском языке."""
    buf_pct = cfg.SL_BUFFER_PCT if cfg else 0.15
    parts = [
        _trend_sentence(analysis["structure"], tf_htf),
        _sweep_sentence(analysis["liquidity"], direction),
        _ob_sentence(analysis["ob"], direction, tf_mtf),
    ]
    if analysis["fvg"].get("bull_found" if direction == "LONG" else "bear_found"):
        parts.append(_fvg_sentence(analysis["fvg"], direction))
    parts.append(_sl_sentence(levels, direction, buf_pct))
    parts.append(_tp_sentence(levels, direction, analysis["fvg"]))
    if show_invalidation:
        parts.append(_invalidation_sentence(levels, direction, tf_ltf))
    return " ".join(parts)


# ── Build Signal ──────────────────────────────────────────────────────────────

def build_smc_signal(symbol: str, analysis: dict, cfg,
                     tf_htf: str = "4H", tf_mtf: str = "1H",
                     tf_ltf: str = "15m") -> Optional[SMCSignalResult]:
    """
    Пробует оба направления. Возвращает сигнал с лучшим score.
    Минимум: score >= MIN_CONFIRMATIONS и R:R >= MIN_RR.
    """
    if analysis.get("error"):
        return None

    best: Optional[SMCSignalResult] = None
    for direction in ("LONG", "SHORT"):
        if direction == "LONG":
            score, confirmations = score_bullish(analysis)
        else:
            score, confirmations = score_bearish(analysis)

        if score < cfg.MIN_CONFIRMATIONS:
            continue

        levels = calculate_levels(analysis, direction, cfg)
        if levels is None:
            continue

        narrative = generate_narrative(
            analysis, levels, direction,
            tf_htf=tf_htf, tf_mtf=tf_mtf, tf_ltf=tf_ltf,
            show_invalidation=True, cfg=cfg,
        )
        grade = GRADES.get(score, f"⚡ {score}/5")

        # ── Взвешенный R:R фильтр ─────────────────────────────────────────
        rr_val  = levels["rr"]
        min_rr  = getattr(cfg, "MIN_RR", 0.8)
        if rr_val < min_rr:
            continue  # ниже пользовательского минимума
        adjusted_score = score
        if rr_val < max(min_rr, 1.2):
            adjusted_score = max(0, score - 1)  # низкий R:R снижает оценку

        # ── Мемкоин: максимум 3 подтверждения ────────────────────────────
        _MEMCOIN_KW = ("FLOKI","PEPE","SHIB","DOGE","WIF","BONK","NEIRO",
                       "MEME","SATS","TURBO","CATS","ACT","BOME","BOOK")
        _sym_up    = symbol.upper()
        is_memcoin = any(k in _sym_up for k in _MEMCOIN_KW)
        if is_memcoin:
            adjusted_score = min(adjusted_score, 3)

        final_grade = GRADES.get(adjusted_score, f"⚡ {adjusted_score}/5")

        sig = SMCSignalResult(
            symbol        = symbol,
            direction     = direction,
            score         = adjusted_score,
            grade         = final_grade,
            entry_low     = levels["entry_low"],
            entry_high    = levels["entry_high"],
            entry         = levels["entry_mid"],
            sl            = levels["sl"],
            tp1           = levels["tp1"],
            tp2           = levels["tp2"],
            tp3           = levels["tp3"],
            rr            = levels["rr"],
            risk_pct      = levels["risk_pct"],
            confirmations = confirmations,
            narrative     = narrative,
            tf_htf        = tf_htf,
            tf_mtf        = tf_mtf,
            tf_ltf        = tf_ltf,
        )
        if best is None or sig.score > best.score:
            best = sig

    return best
