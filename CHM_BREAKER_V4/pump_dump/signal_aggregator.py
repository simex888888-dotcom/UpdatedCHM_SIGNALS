"""
signal_aggregator.py — агрегатор сигналов.

Собирает результаты всех слоёв, считает взвешенный score,
применяет фильтры, форматирует сообщение для Telegram.

Правила отправки:
  1. score >= 70%
  2. Минимум 4 из 8 слоёв активны
  3. ML обязателен (если модель готова), иначе MIN_ACTIVE_LAYERS=3 из 7
  4. Anti-spam: не повторять по монете 20 минут
  5. ATR < 5% (памп ещё не идёт)
  6. 24h объём >= 500K USDT
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Literal

from pump_dump.pd_config import (
    LAYER_WEIGHTS, MIN_SIGNAL_SCORE, MIN_ACTIVE_LAYERS,
    ANTI_SPAM_MINUTES, MAX_ATR_PCT,
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


# Anti-spam: symbol → last_signal_ts
_last_signal_ts: dict[str, float] = {}


def aggregate(
    symbol: str,
    price: float,
    an: AnomalyResult,
    ob: OBResult,
    hs: HiddenResult,
    ind: IndicatorResult,
) -> Optional[SignalPayload]:
    """
    Запускает все слои, считает score, возвращает SignalPayload или None.
    """
    # ── Anti-spam ─────────────────────────────────────────────────────────────
    now = time.time()
    last = _last_signal_ts.get(symbol, 0)
    if now - last < ANTI_SPAM_MINUTES * 60:
        return None

    # ── Фильтр: памп уже идёт ─────────────────────────────────────────────────
    if an.atr_pct > MAX_ATR_PCT:
        return None

    # ── Определяем вероятное направление и оцениваем слои ────────────────────
    ml    = get_model().predict(build_feature_vector(an, ob, hs, ind))
    votes = _count_direction_votes(an, ob, hs, ind, ml)

    if votes["PUMP"] == 0 and votes["DUMP"] == 0:
        return None
    direction: Literal["PUMP", "DUMP"] = "PUMP" if votes["PUMP"] >= votes["DUMP"] else "DUMP"

    active, inactive, raw_score = _score_layers(direction, an, ob, hs, ind, ml)

    # ML обязателен если модель готова
    ml_ready = get_model().is_ready()
    min_layers = MIN_ACTIVE_LAYERS
    if ml_ready and "ml" not in active:
        return None         # ML говорит нет — отклоняем
    if not ml_ready:
        min_layers = 3      # без ML модели достаточно 3 из 7

    if len(active) < min_layers:
        return None
    if raw_score < MIN_SIGNAL_SCORE:
        return None

    _last_signal_ts[symbol] = now

    return SignalPayload(
        symbol=symbol,
        direction=direction,
        score=raw_score,
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


def _count_direction_votes(an, ob, hs, ind, ml) -> dict:
    v = {"PUMP": 0, "DUMP": 0}
    for d in [an.spike_dir, an.cvd_dir, ob.imbalance_dir,
              hs.funding_dir, hs.oi_dir, hs.cvd_div_dir,
              hs.ls_dir, ind.macd_dir, ind.rsi_div_dir]:
        if d in v:
            v[d] += 1
    if ml and ml.predicted in v:
        v[ml.predicted] += 2   # ML голосует вдвойне
    return v


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
        inactive.append(f"ML модель: {'нет сигнала' if ml else 'не обучена'}")

    # ── Взвешенный score ──────────────────────────────────────────────────────
    total_w = sum(w[k] for k in active)
    # Бонус за резкое изменение funding
    total_w += hs.funding_delta_bonus if "funding" in active else 0.0
    # Нормируем к 100%
    max_w = sum(w.values())
    score = min(total_w / max_w * 100, 100.0)

    return active, inactive, round(score, 1)


# ─── Форматирование Telegram-сообщения ───────────────────────────────────────

def format_alert(sig: SignalPayload) -> str:
    NL = "\n"
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
        f"🔍 <b>Активные сигналы ({len(sig.active_layers)}/8):</b>",
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

    import datetime
    ts_str = datetime.datetime.utcfromtimestamp(sig.ts).strftime("%H:%M:%S UTC")
    sym_url = sig.symbol.replace("-", "")
    lines += [
        "",
        f"🎯 <b>Уверенность: {sig.score:.0f}%</b>",
        f"⏰ {ts_str}",
        f'🔗 <a href="https://bingx.com/en/futures/{sym_url}/">BingX</a>',
    ]
    return NL.join(lines)
