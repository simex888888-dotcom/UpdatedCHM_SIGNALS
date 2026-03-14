"""
signal_aggregator.py — агрегатор сигналов.

Собирает результаты всех слоёв, считает взвешенный score,
применяет фильтры, форматирует сообщение для Telegram.

Правила отправки:
  1. score >= 50%
  2. Минимум 3 из 8 слоёв активны
  3. ML — дополнительный слой (штраф -15 если модель готова и не согласна, но не блок)
  4. Anti-spam: не повторять по монете 15 минут
  5. ATR < 5% (памп ещё не идёт)
  6. 24h объём >= 500K USDT
"""

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Optional, Literal

log = logging.getLogger("CHM.PD.Aggregator")

from pump_dump.pd_config import (
    LAYER_WEIGHTS, MIN_SIGNAL_SCORE, MIN_ACTIVE_LAYERS,
    ANTI_SPAM_MINUTES, MAX_ATR_PCT,
    LVL1_ZSCORE_MIN, LVL1_SPAM_MINUTES,
    LVL2_MIN_LAYERS, LVL2_MIN_SCORE, LVL2_SPAM_MINUTES,
    LVL3_MIN_LAYERS, LVL3_MIN_SCORE, LVL3_SPAM_MINUTES,
)
from pump_dump.anomaly_detector  import AnomalyResult
from pump_dump.orderbook_analyzer import OBResult
from pump_dump.hidden_signals    import HiddenResult
from pump_dump.indicators        import IndicatorResult
from pump_dump.ml_model          import MLResult, get_model, build_feature_vector


@dataclass
class SignalPayload:
    symbol: str
    direction: Literal["PUMP", "DUMP"]
    score: float          # 0–100
    active_layers: dict   # layer_name → описание
    inactive_layers: list
    ml_result: Optional[MLResult]
    price: float
    price_change_1m: float
    price_change_3m: float
    funding_rate: float
    oi_change: float
    imbalance: float
    volume_zscore: float
    ts: float


# Anti-spam: symbol → last_signal_ts (финальный сигнал уровень 3)
_last_signal_ts: dict[str, float] = {}

# Anti-spam по уровням: level → symbol → timestamp
_level_spam: dict[int, dict[str, float]] = {1: {}, 2: {}, 3: {}}

_LEVEL_SPAM_MINUTES = {
    1: LVL1_SPAM_MINUTES,
    2: LVL2_SPAM_MINUTES,
    3: LVL3_SPAM_MINUTES,
}


def is_spam(symbol: str, level: int) -> bool:
    """Проверяет, был ли недавно отправлен сигнал данного уровня по символу."""
    last = _level_spam.get(level, {}).get(symbol, 0)
    return (time.time() - last) < _LEVEL_SPAM_MINUTES.get(level, 15) * 60


def _mark_sent(symbol: str, level: int) -> None:
    """Отмечает, что сигнал данного уровня был отправлен."""
    _level_spam.setdefault(level, {})[symbol] = time.time()


def _direction_simple(an: "AnomalyResult", buy_ratio: float) -> Optional[Literal["PUMP", "DUMP"]]:
    """Определяет направление по ценовому спайку, объёму торгов или CVD.

    Порядок приоритета:
      1. Ценовой спайк (самый надёжный)
      2. Дисбаланс покупателей/продавцов из @trade потока (≥60% / ≤40%)
      3. CVD-направление как последний резерв — позволяет выявить
         накопление/распределение ещё до движения цены.
    """
    if an.spike_dir is not None:
        return an.spike_dir
    if buy_ratio >= 0.60:
        return "PUMP"
    if buy_ratio <= 0.40:
        return "DUMP"
    # Резерв: CVD-направление (критично для раннего обнаружения накопления)
    if an.cvd_dir is not None:
        return an.cvd_dir
    return None


def check_alert_level(
    active: dict,
    score: float,
    an: "AnomalyResult",
    ml: Optional["MLResult"],
) -> Optional[int]:
    """
    Определяет уровень сигнала на основе активных слоёв и score.
    Возвращает 1, 2, 3 или None.
    Уровень 3 требует подтверждения ML если модель готова.
    """
    n = len(active)

    # Уровень 3: финальный сигнал.
    # ML не блокирует уровень 3 жёстко — она учитывается через штраф -15 к score
    # в analyze_levels(). Раньше ML-блок полностью подавлял сигналы когда
    # модель говорила NEUTRAL (что происходит при недостатке обучающих данных).
    if n >= LVL3_MIN_LAYERS and score >= LVL3_MIN_SCORE:
        return 3

    # Уровень 2: наблюдение
    if n >= LVL2_MIN_LAYERS and score >= LVL2_MIN_SCORE:
        return 2

    # Уровень 1: внимание — только аномальный объём
    if an.volume_anomaly and an.volume_zscore >= LVL1_ZSCORE_MIN:
        return 1

    return None


def analyze_levels(
    symbol: str,
    price: float,
    an: "AnomalyResult",
    ob: "OBResult",
    hs: "HiddenResult",
    ind: "IndicatorResult",
    buy_ratio: float,
) -> list[tuple[int, str, Optional[SignalPayload]]]:
    """
    Анализирует все уровни сигналов для символа.
    Возвращает список (level, formatted_text, payload_or_None).
    payload заполнен только для уровня 3 — используется для сохранения в БД.

    Если уровень 3 готов — помечаем 1, 2, 3 как отправленные.
    Если уровень 2 — помечаем 1 и 2.
    Если уровень 1 — только 1.
    """
    # Не рассматриваем символы где памп уже идёт
    if an.atr_pct > MAX_ATR_PCT:
        return []

    direction = _direction_simple(an, buy_ratio)
    if direction is None:
        return []

    # Вычисляем слои и score
    ml = get_model().predict(build_feature_vector(an, ob, hs, ind))
    active, inactive, score = _score_layers(direction, an, ob, hs, ind, ml)

    # ML penalty для уровня 3
    ml_ready = get_model().is_ready()
    if ml_ready and "ml" not in active:
        score = max(0.0, score - 15.0)

    level = check_alert_level(active, score, an, ml)
    if level is None:
        return []

    results = []

    if level == 3:
        if not is_spam(symbol, 3):
            now = time.time()
            text3 = _format_level3_from_parts(
                symbol, direction, price, score, active, inactive, ml, an, ob, hs
            )
            payload = SignalPayload(
                symbol=symbol,
                direction=direction,
                score=score,
                active_layers=active,
                inactive_layers=inactive,
                ml_result=ml,
                price=price,
                price_change_1m=an.price_change_1m,
                price_change_3m=an.price_change_3m,
                funding_rate=hs.funding_rate,
                oi_change=hs.oi_change_10m,
                imbalance=ob.imbalance,
                volume_zscore=an.volume_zscore,
                ts=now,
            )
            results.append((3, text3, payload))
            _mark_sent(symbol, 1)
            _mark_sent(symbol, 2)
            _mark_sent(symbol, 3)
            _last_signal_ts[symbol] = now
    elif level == 2:
        if not is_spam(symbol, 2):
            text2 = format_level2(symbol, direction, price, an, active, score)
            results.append((2, text2, None))
            _mark_sent(symbol, 1)
            _mark_sent(symbol, 2)
    elif level == 1:
        if not is_spam(symbol, 1):
            text1 = format_level1(symbol, direction, price, an)
            results.append((1, text1, None))
            _mark_sent(symbol, 1)

    return results


def format_level1(
    symbol: str,
    direction: Literal["PUMP", "DUMP"],
    price: float,
    an: "AnomalyResult",
) -> str:
    """Форматирует сообщение уровня 1 — ВНИМАНИЕ."""
    emoji = "📈" if direction == "PUMP" else "📉"
    dir_ru = "ПАМП" if direction == "PUMP" else "ДАМП"
    mean_mult = an.volume_zscore / 2.0 if an.volume_zscore > 0 else 1.0
    change_pct = an.price_change_1m * 100

    lines = [
        f"👀 <b>ВНИМАНИЕ — {emoji} {dir_ru}?</b>",
        f"🪙 <b>${symbol}/USDT</b>  •  BingX Futures",
        f"💰 Цена: <b>${price:,.4f}</b>",
        f"📊 Объём: Z-score {an.volume_zscore:.1f} (в {mean_mult:.1f}x выше нормы)",
        f"{'📈' if change_pct >= 0 else '📉'} Изменение: {change_pct:+.1f}% (1m)",
        "⏳ Наблюдаю... жду подтверждения других слоёв",
    ]
    return "\n".join(lines)


def format_level2(
    symbol: str,
    direction: Literal["PUMP", "DUMP"],
    price: float,
    an: "AnomalyResult",
    active_layers: dict,
    score: float,
) -> str:
    """Форматирует сообщение уровня 2 — НАБЛЮДЕНИЕ."""
    emoji = "🔺" if direction == "PUMP" else "🔻"
    dir_ru = "ПАМП" if direction == "PUMP" else "ДАМП"
    change_pct = an.price_change_1m * 100

    layer_labels = {
        "volume":    "📦 Объём аномальный",
        "price":     "📊 Ценовой спайк",
        "cvd":       "🌊 CVD дивергенция",
        "orderbook": "📖 Стакан дисбаланс",
        "spread":    "↔️ Спред расширен",
        "funding":   "💸 Funding аномалия",
        "oi":        "📉 OI дивергенция",
        "ml":        "🤖 ML подтверждение",
        "bb_squeeze": "📐 BB Squeeze",
        "rsi_div":   "📉 RSI Divergence",
    }

    _total_layers = len(LAYER_WEIGHTS)
    lines = [
        f"⚠️ <b>НАБЛЮДЕНИЕ — {emoji} {dir_ru}</b>",
        f"🪙 <b>${symbol}/USDT</b>  •  BingX Futures",
        f"💰 Цена: <b>${price:,.4f}</b>",
        f"{'📈' if change_pct >= 0 else '📉'} Изменение: {change_pct:+.1f}% (1m)",
        "",
        f"🔍 <b>Активных слоёв: {len(active_layers)}/{_total_layers}:</b>",
    ]
    items = list(active_layers.keys())
    for i, key in enumerate(items):
        prefix = "└" if i == len(items) - 1 else "├"
        label = layer_labels.get(key, key)
        lines.append(f"{prefix} {label} ✅")

    lines += [
        "",
        f"🎯 Score: <b>{score:.0f}%</b>  (нужно {LVL3_MIN_SCORE}% для сигнала)",
    ]
    return "\n".join(lines)


def _format_level3_from_parts(
    symbol: str,
    direction: str,
    price: float,
    score: float,
    active: dict,
    inactive: list,
    ml,
    an: "AnomalyResult",
    ob: "OBResult",
    hs: "HiddenResult",
) -> str:
    """Форматирует уровень 3 через существующий format_alert после создания SignalPayload."""
    NL = "\n"
    emoji = "⚡️" if direction == "PUMP" else "💥"
    dir_ru = "ПАМП" if direction == "PUMP" else "ДАМП"
    sign = "📈" if direction == "PUMP" else "📉"

    layer_labels = {
        "volume":    "📦 Объём",
        "price":     "📊 Цена",
        "cvd":       "🌊 CVD",
        "orderbook": "📖 Стакан",
        "spread":    "↔️ Спред",
        "funding":   "💸 Funding / L/S",
        "oi":        "📉 Open Interest",
        "ml":        "🤖 ML модель",
        "bb_squeeze": "📐 BB Squeeze",
        "rsi_div":   "📉 RSI Divergence",
    }

    _total_layers = len(LAYER_WEIGHTS)
    lines = [
        f"{emoji} <b>HIGH CONFIDENCE {dir_ru} SIGNAL</b>" + NL,
        f"🪙 <b>${symbol}</b>  •  BingX Futures",
        f"💰 Цена: <b>${price:,.4f}</b>  {sign} "
        f"<b>{an.price_change_1m*100:+.1f}%</b> (1m) / "
        f"<b>{an.price_change_3m*100:+.1f}%</b> (3m)",
        "",
        f"🔍 <b>Активные сигналы ({len(active)}/{_total_layers}):</b>",
    ]
    items = list(active.items())
    for i, (key, desc) in enumerate(items):
        prefix = "└" if i == len(items) - 1 else "├"
        label = layer_labels.get(key, key)
        lines.append(f"{prefix} {label}:  {desc}")

    if inactive:
        lines += ["", f"⚠️ <b>Неактивно ({len(inactive)}/8):</b>"]
        for i, item in enumerate(inactive):
            prefix = "└" if i == len(inactive) - 1 else "├"
            lines.append(f"{prefix} {item}")

    ts_str = datetime.datetime.utcnow().strftime("%H:%M:%S UTC")
    sym_url = symbol.replace("-", "")
    lines += [
        "",
        f"🎯 <b>Уверенность: {score:.0f}%</b>",
        f"⏰ {ts_str}",
        f'🔗 <a href="https://bingx.com/en/futures/{sym_url}/">BingX</a>',
    ]
    return NL.join(lines)



def _score_layers(direction, an, ob, hs, ind, ml) -> tuple[dict, list, float]:
    w    = LAYER_WEIGHTS
    active   = {}
    inactive = []

    # ── Слой 1: объём (двойное кондиционирование) ─────────────────────────────
    if an.volume_double_cond and an.volume_anomaly:
        active["volume"] = f"Z={an.volume_zscore:.1f}, Double cond ✅"
    else:
        inactive.append("Объём: нет двойного подтверждения")

    # ── Слой 2: ценовой спайк ─────────────────────────────────────────────────
    if an.price_spike and an.spike_dir == direction:
        active["price"] = (
            f"{an.price_change_1m*100:+.1f}% (1m) / {an.price_change_3m*100:+.1f}% (3m) ✅"
        )
    else:
        inactive.append("Ценовой спайк: нет подтверждения")

    # ── Слой 3: CVD ───────────────────────────────────────────────────────────
    cvd_active = (an.cvd_signal and an.cvd_dir == direction) or \
                 (hs.cvd_divergence and hs.cvd_div_dir == direction)
    if cvd_active:
        if hs.cvd_divergence and hs.cvd_div_dir == direction:
            active["cvd"] = "Дивергенция Smart Money ✅"
        else:
            active["cvd"] = f"CVD {'растёт' if direction=='PUMP' else 'падает'} 5 свечей ✅"
    else:
        inactive.append("CVD: нет подтверждения")

    # ── Слой 4: стакан ────────────────────────────────────────────────────────
    if ob.imbalance_signal and ob.imbalance_dir == direction:
        pct = int(ob.imbalance * 100)
        active["orderbook"] = f"Imbalance {pct}% ✅"
    else:
        inactive.append("Стакан: imbalance в норме")

    # ── Слой 5: spread widening ───────────────────────────────────────────────
    if ob.spread_signal:
        active["spread"] = f"Спред расширился × {ob.spread_pct:.2f}% ✅"
    else:
        inactive.append("Спред: в норме")

    # ── Слой 6: funding rate ──────────────────────────────────────────────────
    if hs.funding_signal and hs.funding_dir == direction:
        pct = hs.funding_rate * 100
        desc = "шорты перегружены" if direction == "PUMP" else "лонги перегружены"
        active["funding"] = f"{pct:+.4f}% ({desc}) ✅"
    elif hs.ls_signal and hs.ls_dir == direction:
        active["funding"] = f"L/S={hs.long_short_ratio:.2f} (толпа проиграет) ✅"
    else:
        inactive.append("Funding Rate / L/S: в норме")

    # ── Слой 7: OI дивергенция ────────────────────────────────────────────────
    if hs.oi_signal and hs.oi_dir == direction:
        chg = hs.oi_change_10m * 100
        active["oi"] = f"OI {chg:+.1f}% без движения цены ✅"
    else:
        inactive.append("Open Interest: нет дивергенции")

    # ── Слой 8: ML ────────────────────────────────────────────────────────────
    if ml and ml.predicted == direction and ml.pump_prob >= 0.70:
        active["ml"] = f"{ml.confidence*100:.0f}% {direction} (precision {ml.precision:.2f}) ✅"
    elif ml is None:
        pass  # модель не обучена — слой не участвует
    else:
        inactive.append("ML модель: нет сигнала")

    # ── Слой 9: BB Squeeze ────────────────────────────────────────────────────
    if ind.bb_squeeze:
        active["bb_squeeze"] = f"BB Width перцентиль {ind.bb_width_pct:.0f}% (сжатие) ✅"
    else:
        inactive.append("BB Squeeze: ширина в норме")

    # ── Слой 10: RSI Divergence ───────────────────────────────────────────────
    if ind.rsi_divergence and ind.rsi_div_dir == direction:
        div_type = "бычья" if direction == "PUMP" else "медвежья"
        active["rsi_div"] = f"RSI {div_type} дивергенция (RSI={ind.rsi:.1f}) ✅"
    else:
        inactive.append("RSI Divergence: нет дивергенции")

    # ── Взвешенный score ──────────────────────────────────────────────────────
    total_w = sum(w[k] for k in active)
    # Бонус за резкое изменение funding
    total_w += hs.funding_delta_bonus if "funding" in active else 0.0

    # Нормируем к 100%.
    # Ключевое исправление: если ML-модель не обучена — исключаем её вес
    # из знаменателя. Иначе при max_w=1.12 порог 45% требует суммы весов 0.504,
    # что с 3 лучшими слоями (0.45) недостижимо. Без ML: max_w=0.97, 0.45/0.97=46.4% ✓
    ml_trained = get_model().is_ready()
    max_w = sum(v for k, v in w.items() if k != "ml" or ml_trained)
    score = min(total_w / max_w * 100, 100.0) if max_w > 0 else 0.0

    return active, inactive, round(score, 1)


# ─── Форматирование Telegram-сообщения ───────────────────────────────────────

def format_alert(sig: SignalPayload) -> str:
    NL = "\n"
    _total_layers = len(LAYER_WEIGHTS)
    emoji = "⚡️" if sig.direction == "PUMP" else "💥"
    dir_ru = "ПАМП" if sig.direction == "PUMP" else "ДАМП"
    sign = "📈" if sig.direction == "PUMP" else "📉"

    lines = [
        f"{emoji} <b>HIGH CONFIDENCE {dir_ru} SIGNAL</b>" + NL,
        f"🪙 <b>${sig.symbol}</b>  •  BingX Futures",
        f"💰 Цена: <b>${sig.price:,.4f}</b>  {sign} "
        f"<b>{sig.price_change_1m*100:+.1f}%</b> (1m) / "
        f"<b>{sig.price_change_3m*100:+.1f}%</b> (3m)",
        "",
        f"🔍 <b>Активные сигналы ({len(sig.active_layers)}/{_total_layers}):</b>",
    ]

    layer_labels = {
        "volume":    "📦 Объём",
        "price":     "📊 Цена",
        "cvd":       "🌊 CVD",
        "orderbook": "📖 Стакан",
        "spread":    "↔️ Спред",
        "funding":   "💸 Funding / L/S",
        "oi":        "📉 Open Interest",
        "ml":        "🤖 ML модель",
        "bb_squeeze": "📐 BB Squeeze",
        "rsi_div":   "📉 RSI Divergence",
    }
    items = list(sig.active_layers.items())
    for i, (key, desc) in enumerate(items):
        prefix = "└" if i == len(items) - 1 else "├"
        label  = layer_labels.get(key, key)
        lines.append(f"{prefix} {label}:  {desc}")

    if sig.inactive_layers:
        lines += ["", f"⚠️ <b>Неактивно ({len(sig.inactive_layers)}/8):</b>"]
        for i, item in enumerate(sig.inactive_layers):
            prefix = "└" if i == len(sig.inactive_layers) - 1 else "├"
            lines.append(f"{prefix} {item}")

    ts_str = datetime.datetime.utcfromtimestamp(sig.ts).strftime("%H:%M:%S UTC")
    sym_url = sig.symbol.replace("-", "")
    lines += [
        "",
        f"🎯 <b>Уверенность: {sig.score:.0f}%</b>",
        f"⏰ {ts_str}",
        f'🔗 <a href="https://bingx.com/en/futures/{sym_url}/">BingX</a>',
    ]
    return NL.join(lines)
