"""
bybit_trader.py — Bybit Unified Account API
Авто-трейдинг: открытие позиции по сигналу с SL и TP1.

Поддержка малых депозитов от $10 (минимальная позиция $5 notional).
Все вызовы API — синхронные (pybit), запускаем через run_in_executor.
"""

import asyncio
import logging
import math
from typing import Optional

log = logging.getLogger("CHM.Bybit")

MIN_NOTIONAL = 5.0   # Минимальный notional (USDT) — меньше Bybit не даст
MAX_LEVERAGE  = 50   # Ограничение плеча


# ── Конвертация символа ──────────────────────────────

def _to_bybit_symbol(symbol: str) -> str:
    """
    OKX формат → Bybit формат:
      BTC-USDT-SWAP  →  BTCUSDT
      ETH-USDT       →  ETHUSDT
      BTCUSDT        →  BTCUSDT (уже правильный)
    """
    s = symbol.upper()
    if s.endswith("-USDT-SWAP"):
        return s[:-10] + "USDT"
    if s.endswith("-USDT"):
        return s[:-5] + "USDT"
    return s.replace("-", "")


# ── Получение qtyStep для символа ────────────────────

def _get_qty_step(session, symbol: str) -> float:
    """
    Запрашивает реальный lotSizeFilter.qtyStep у Bybit.
    При ошибке — возвращает запасное значение по цене.
    """
    try:
        resp = session.get_instruments_info(category="linear", symbol=symbol)
        if resp.get("retCode", -1) == 0:
            items = resp["result"].get("list", [])
            if items:
                step = items[0].get("lotSizeFilter", {}).get("qtyStep", "")
                if step:
                    return float(step)
    except Exception as e:
        log.debug(f"get_instruments_info {symbol}: {e}")
    return 0.001   # fallback — перестрахуемся минимальным шагом


def _round_qty(qty: float, qty_step: float) -> str:
    """
    Округляем размер позиции вниз до ближайшего шага qtyStep.
    """
    if qty_step <= 0:
        qty_step = 0.001
    # Определяем количество знаков после запятой в шаге
    step_str = f"{qty_step:.10f}".rstrip("0")
    if "." in step_str:
        decimals = len(step_str.split(".")[1])
    else:
        decimals = 0
    factor = 1.0 / qty_step
    qty_floor = math.floor(qty * factor) / factor
    return f"{qty_floor:.{decimals}f}"


# ── Сессия Bybit ─────────────────────────────────────

def _get_session(api_key: str, api_secret: str):
    """Создаёт HTTP-сессию Bybit Unified Account."""
    try:
        from pybit.unified_trading import HTTP
    except ImportError:
        raise RuntimeError(
            "pybit не установлен. Выполни: pip install pybit"
        )
    return HTTP(
        testnet=False,
        api_key=api_key,
        api_secret=api_secret,
    )


# ── Получение баланса ─────────────────────────────────

def _get_balance_sync(api_key: str, api_secret: str) -> float:
    """Синхронно возвращает доступный баланс USDT."""
    session = _get_session(api_key, api_secret)
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    if resp.get("retCode", -1) != 0:
        raise RuntimeError(resp.get("retMsg", "Balance error"))
    coins = resp["result"]["list"][0]["coin"]
    for coin in coins:
        if coin["coin"] == "USDT":
            val = (coin.get("availableToWithdraw") or
                   coin.get("walletBalance") or "0")
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0
    return 0.0


async def get_balance(api_key: str, api_secret: str) -> float:
    """Асинхронная обёртка для получения баланса."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _get_balance_sync, api_key, api_secret
    )


# ── Тест соединения ──────────────────────────────────

async def test_connection(api_key: str, api_secret: str) -> dict:
    """
    Проверяет API ключи и возвращает баланс.
    {"ok": True, "balance": 123.45}  или  {"ok": False, "error": "..."}
    """
    try:
        balance = await get_balance(api_key, api_secret)
        return {"ok": True, "balance": balance}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Открытие позиции ─────────────────────────────────

def _place_trade_sync(
    api_key: str,
    api_secret: str,
    symbol: str,
    direction: str,
    entry: float,
    sl: float,
    tp1: float,
    risk_pct: float,
    leverage: int,
    tp2: float = 0.0,
    tp3: float = 0.0,
) -> dict:
    """
    Синхронно открывает Market-ордер на Bybit с SL.
    Если tp2/tp3 переданы — ставит 3 частичных TP (reduce-only limit orders).
    Возвращает {"ok": True/False, ...}
    """
    session    = _get_session(api_key, api_secret)
    bb_symbol  = _to_bybit_symbol(symbol)
    side       = "Buy" if direction == "LONG" else "Sell"
    lev        = min(max(1, leverage), MAX_LEVERAGE)

    # Устанавливаем плечо (ошибка если уже выставлено — игнорируем)
    try:
        session.set_leverage(
            category="linear",
            symbol=bb_symbol,
            buyLeverage=str(lev),
            sellLeverage=str(lev),
        )
    except Exception as e:
        log.debug(f"set_leverage {bb_symbol}: {e}")

    # Получаем баланс
    balance = _get_balance_sync(api_key, api_secret)
    if balance < 1.0:
        return {
            "ok": False,
            "error": f"Недостаточно средств: ${balance:.2f} USDT\n"
                     f"Пополни счёт и попробуй снова."
        }

    # Рассчитываем размер позиции через риск
    risk_amount = balance * (risk_pct / 100.0)
    price_diff  = abs(entry - sl)
    if price_diff <= 0:
        return {"ok": False, "error": "Некорректные entry/SL"}

    qty_raw  = risk_amount / price_diff
    notional = qty_raw * entry

    # Гарантируем минимальный notional
    if notional < MIN_NOTIONAL:
        qty_raw  = MIN_NOTIONAL / entry
        notional = MIN_NOTIONAL

    # Получаем реальный шаг лота с Bybit и округляем
    qty_step = _get_qty_step(session, bb_symbol)
    qty_str  = _round_qty(qty_raw, qty_step)
    if float(qty_str) <= 0:
        return {
            "ok": False,
            "error": f"Размер позиции слишком мал (${notional:.2f}). "
                     f"Нужен депозит от ${MIN_NOTIONAL / (risk_pct / 100):.0f} при риске {risk_pct}%."
        }

    # Проверяем, хватит ли маржи (notional / leverage) на балансе
    margin_required = (float(qty_str) * entry) / lev
    if margin_required > balance * 0.95:
        return {
            "ok": False,
            "error": (
                f"Недостаточно маржи для открытия позиции.\n"
                f"Требуется: ${margin_required:.2f} USDT\n"
                f"Доступно:  ${balance:.2f} USDT\n"
                f"Уменьши риск (сейчас {risk_pct}%) или пополни счёт."
            )
        }

    # Выставляем ордер.
    # positionIdx: 0 = One-Way Mode, 1 = Hedge Buy, 2 = Hedge Sell.
    # Пробуем One-Way, при ошибке режима — переключаемся на Hedge.
    hedge_idx = 1 if side == "Buy" else 2

    def _do_place(position_idx: int) -> dict:
        return session.place_order(
            category="linear",
            symbol=bb_symbol,
            side=side,
            orderType="Market",
            qty=qty_str,
            stopLoss=str(round(sl, 8)),
            slTriggerBy="MarkPrice",
            positionIdx=position_idx,
        )

    try:
        resp = _do_place(0)
        # Bybit retCode 130125 = "position idx not match position mode" (Hedge Mode)
        if resp.get("retCode") == 130125:
            log.info(f"{bb_symbol}: One-Way Mode failed, retrying with Hedge Mode (idx={hedge_idx})")
            resp = _do_place(hedge_idx)
            # Определяем итоговый positionIdx
            used_hedge = True
        else:
            used_hedge = False

        if resp.get("retCode", -1) == 0:
            order_id      = resp["result"].get("orderId", "")
            qty_f         = float(qty_str)
            notional_real = qty_f * entry
            final_pos_idx = hedge_idx if used_hedge else 0

            # ── 3 частичных TP (reduce-only limit orders) ────────────────
            close_side = "Sell" if side == "Buy" else "Buy"
            qty_tp     = qty_f / 3.0
            qty_tp_str = _round_qty(qty_tp, qty_step)
            tp_prices  = [tp1, tp2, tp3]

            for tp_price in tp_prices:
                if tp_price is None or tp_price <= 0:
                    continue
                try:
                    session.place_order(
                        category    = "linear",
                        symbol      = bb_symbol,
                        side        = close_side,
                        orderType   = "Limit",
                        qty         = qty_tp_str,
                        price       = str(round(tp_price, 8)),
                        reduceOnly  = True,
                        timeInForce = "GTC",
                        positionIdx = final_pos_idx,
                    )
                except Exception as e_tp:
                    log.warning(f"TP order @{tp_price} {bb_symbol}: {e_tp}")

            return {
                "ok":          True,
                "order_id":    order_id,
                "qty":         qty_str,
                "notional":    notional_real,
                "balance":     balance,
                "symbol":      bb_symbol,
                "side":        side,
                "leverage":    lev,
                "pos_idx":     final_pos_idx,
            }
        else:
            return {"ok": False, "error": resp.get("retMsg", "Неизвестная ошибка Bybit")}
    except Exception as e:
        log.error(f"place_order {symbol}: {e}")
        return {"ok": False, "error": str(e)}


async def place_trade(
    api_key:    str,
    api_secret: str,
    symbol:     str,
    direction:  str,   # "LONG" | "SHORT"
    entry:      float,
    sl:         float,
    tp1:        float,
    risk_pct:   float,  # % от баланса
    leverage:   int   = 10,
    tp2:        float = 0.0,
    tp3:        float = 0.0,
) -> dict:
    """
    Асинхронно открывает позицию на Bybit.
    Если tp2/tp3 переданы — ставит 3 частичных TP (reduce-only).
    Возвращает {"ok": True, "order_id": ..., "qty": ..., ...}
              или {"ok": False, "error": "..."}
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        _place_trade_sync,
        api_key, api_secret,
        symbol, direction,
        entry, sl, tp1,
        risk_pct, leverage,
        tp2, tp3,
    )


# ── Безубыток ─────────────────────────────────────────

def _set_breakeven_sync(api_key: str, api_secret: str,
                        symbol: str, entry: float,
                        direction: str, pos_idx: int = 0) -> dict:
    """Синхронно переносит SL на цену входа (безубыток)."""
    session   = _get_session(api_key, api_secret)
    bb_symbol = _to_bybit_symbol(symbol)
    try:
        resp = session.set_trading_stop(
            category    = "linear",
            symbol      = bb_symbol,
            stopLoss    = str(round(entry, 8)),
            slTriggerBy = "MarkPrice",
            positionIdx = pos_idx,
        )
        if resp.get("retCode", -1) == 0:
            return {"ok": True}
        return {"ok": False, "error": resp.get("retMsg", "BE error")}
    except Exception as e:
        log.error(f"set_breakeven {symbol}: {e}")
        return {"ok": False, "error": str(e)}


async def set_breakeven(api_key: str, api_secret: str,
                        symbol: str, entry: float,
                        direction: str, pos_idx: int = 0) -> dict:
    """Асинхронно переносит SL на безубыток."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _set_breakeven_sync,
        api_key, api_secret, symbol, entry, direction, pos_idx,
    )


# ── Проверка позиций (для мониторинга BE) ─────────────

def _get_positions_sync(api_key: str, api_secret: str, symbol: str = "") -> list:
    """Возвращает список открытых позиций (linear перпы)."""
    session = _get_session(api_key, api_secret)
    try:
        kwargs = {"category": "linear", "settleCoin": "USDT"}
        if symbol:
            kwargs["symbol"] = _to_bybit_symbol(symbol)
        resp = session.get_positions(**kwargs)
        if resp.get("retCode", -1) == 0:
            return resp["result"].get("list", [])
    except Exception as e:
        log.debug(f"get_positions: {e}")
    return []


async def get_positions(api_key: str, api_secret: str, symbol: str = "") -> list:
    """Асинхронно возвращает список открытых позиций."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _get_positions_sync, api_key, api_secret, symbol,
    )


def format_trade_result(result: dict, direction: str, symbol: str,
                        entry: float, sl: float, tp1: float,
                        risk_pct: float, leverage: int,
                        tp2: float = 0.0, tp3: float = 0.0) -> str:
    """Форматирует результат открытия позиции для отправки в Telegram."""
    if result["ok"]:
        bb_sym   = result.get("symbol", _to_bybit_symbol(symbol))
        qty      = result.get("qty", "?")
        notional = result.get("notional", 0.0)
        balance  = result.get("balance", 0.0)
        order_id = result.get("order_id", "?")
        risk_amt = balance * risk_pct / 100
        dir_em   = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"

        tp_lines = f"🎯 TP1: <code>{tp1}</code>  (1/3 позиции)\n"
        if tp2:
            tp_lines += f"🎯 TP2: <code>{tp2}</code>  (1/3 позиции)\n"
        if tp3:
            tp_lines += f"🏆 TP3: <code>{tp3}</code>  (1/3 позиции)\n"
        be_note = "\n♻️ <i>После TP1 — стоп автоматически перенесётся в БУ</i>" if tp2 else ""

        return (
            f"✅ <b>СДЕЛКА ОТКРЫТА на Bybit</b>\n\n"
            f"💎 <b>{bb_sym}</b>   {dir_em}   x{leverage}\n\n"
            f"💰 Вход:      <code>{entry}</code>\n"
            f"🛑 Стоп:      <code>{sl}</code>\n\n"
            f"{tp_lines}"
            f"{be_note}\n\n"
            f"📦 Объём:  <code>{qty}</code>  (~${notional:.2f})\n"
            f"⚠️ Риск:   <code>${risk_amt:.2f}</code> ({risk_pct}% от ${balance:.2f})\n\n"
            f"🆔 Order ID: <code>{order_id}</code>"
        )
    else:
        return (
            f"❌ <b>Не удалось открыть сделку</b>\n\n"
            f"Причина: {result.get('error', 'Неизвестная ошибка')}"
        )
