"""
orderbook_analyzer.py — анализ стакана заявок.

Сигналы:
  - Дисбаланс bid/ask (imbalance)
  - Расширение спреда без движения цены (уход маркетмейкера)
  - Крупные заявки-стены
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional

from pump_dump.pd_config import (
    IMBALANCE_PUMP, IMBALANCE_DUMP,
    SPREAD_WIDENING_M, WALL_PCT, SPOOF_SECS,
)
from pump_dump.market_monitor import OrderBook


@dataclass
class OBResult:
    imbalance: float                       # bid_vol / total_vol
    imbalance_signal: bool
    imbalance_dir: Optional[Literal["PUMP", "DUMP"]]

    spread_pct: float                      # (ask - bid) / mid * 100
    spread_signal: bool                    # спред расширился без движения цены

    buy_wall: bool                         # крупная bid-заявка
    sell_wall: bool                        # крупная ask-заявка


# Храним историю спредов для каждой монеты (для EWMA baseline)
_spread_history: dict[str, deque] = {}


def analyze(ob: Optional[OrderBook], price_change_1m: float) -> OBResult:
    """
    ob             — снапшот стакана
    price_change_1m — изменение цены за последнюю свечу (для фильтра spread widening)
    """
    if ob is None or not ob.bids or not ob.asks:
        return _empty_result()

    sym = ob.symbol

    # ── Imbalance ────────────────────────────────────────────────────────────
    bid_vol = sum(float(q) for _, q in ob.bids)
    ask_vol = sum(float(q) for _, q in ob.asks)
    total   = bid_vol + ask_vol
    imbalance = bid_vol / total if total > 0 else 0.5

    imb_signal = False
    imb_dir    = None
    if imbalance > IMBALANCE_PUMP:
        imb_signal, imb_dir = True, "PUMP"
    elif imbalance < IMBALANCE_DUMP:
        imb_signal, imb_dir = True, "DUMP"

    # ── Spread widening ──────────────────────────────────────────────────────
    best_bid = float(ob.bids[0][0]) if ob.bids else 0.0
    best_ask = float(ob.asks[0][0]) if ob.asks else 0.0
    mid      = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
    spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 0.0

    if sym not in _spread_history:
        _spread_history[sym] = deque(maxlen=30)
    _spread_history[sym].append(spread_pct)

    spread_signal = False
    if len(_spread_history[sym]) >= 10:
        baseline = sum(_spread_history[sym]) / len(_spread_history[sym])
        # Спред расширился И цена не движется (маркетмейкер убирает ликвидность)
        if spread_pct > baseline * SPREAD_WIDENING_M and abs(price_change_1m) < 0.005:
            spread_signal = True

    # ── Стены ────────────────────────────────────────────────────────────────
    buy_wall  = any(float(q) / bid_vol > WALL_PCT for _, q in ob.bids)  if bid_vol > 0 else False
    sell_wall = any(float(q) / ask_vol > WALL_PCT for _, q in ob.asks)  if ask_vol > 0 else False

    return OBResult(
        imbalance=imbalance,
        imbalance_signal=imb_signal,
        imbalance_dir=imb_dir,
        spread_pct=spread_pct,
        spread_signal=spread_signal,
        buy_wall=buy_wall,
        sell_wall=sell_wall,
    )


def _empty_result() -> OBResult:
    return OBResult(
        imbalance=0.5, imbalance_signal=False, imbalance_dir=None,
        spread_pct=0.0, spread_signal=False,
        buy_wall=False, sell_wall=False,
    )
