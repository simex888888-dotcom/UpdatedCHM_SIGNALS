"""
bybit_trader.py — Bybit Unified Account API
Авто-трейдинг: открытие позиции по сигналу с SL и TP1.

Поддержка малых депозитов от $10 (минимальная позиция $5 notional).
Все вызовы API — синхронные (pybit), запускаем через run_in_executor.
"""

import asyncio
import logging
import math
import re
from typing import Optional

log = logging.getLogger("CHM.Bybit")

MIN_NOTIONAL  = 5.0    # Минимальный notional (USDT) — меньше Bybit не даст
MAX_LEVERAGE  = 50    # Ограничение плеча
TAKER_FEE     = 0.00060  # 0.06% комиссия маркет-ордера (opening + closing round-trip)


# ── Человекочитаемые ошибки Bybit ────────────────────────────────────────────

_BYBIT_ERROR_MAP: dict[int, str] = {
    10003: "Неверный API ключ. Проверьте ключ в настройках.",
    10004: "API ключ истёк или был отозван. Перевыпустите ключ на Bybit.",
    10005: "Недостаточно прав API. Включите разрешение «Contract Trading».",
    10006: "Превышен лимит запросов к API. Попробуйте ещё раз через несколько секунд.",
    10016: "Сервис Bybit временно недоступен. Повторите попытку позже.",
    110001: "Ордер не найден.",
    110003: "Цена ордера выходит за допустимый диапазон. Попробуйте повторно — цена могла измениться.",
    110007: "Недостаточно свободного баланса для открытия позиции. Пополните счёт или уменьшите объём.",
    110012: "Недостаточно доступного баланса. Освободите маржу или пополните счёт.",
    110013: "Не удалось установить маржу — позиция не найдена.",
    110014: "Превышен лимит позиций по данному инструменту.",
    110015: "Позиция уже закрыта или не существует.",
    110016: "Инструмент недоступен для торговли в данный момент.",
    110017: "Ордер нарушает правило reduce-only (позиция уже меньше объёма ордера).",
    110025: "Режим позиции уже установлен и не требует изменений.",
    110040: "Превышен максимальный объём ордера для данного инструмента.",
    110043: "Кредитное плечо уже установлено на данное значение.",
    110055: "Торговля по данному инструменту приостановлена.",
    110066: "Превышен лимит открытых ордеров.",
    130021: "Объём ордера превышает размер открытой позиции.",
    130125: "Несоответствие режима позиции (One-Way / Hedge). Выполняется автоматическая коррекция.",
}


def _humanize_bybit_error(raw: str) -> str:
    """
    Преобразует сырое сообщение об ошибке Bybit в понятный русскоязычный текст.
    Убирает технические суффиксы (ErrCode, ErrTime) и маппит коды на описания.
    """
    if not raw:
        return "Неизвестная ошибка Bybit."

    # Ищем числовой код ошибки в скобках: (ErrCode: 110007) или retCode в тексте
    code_match = re.search(r"\b(ErrCode|retCode)[:\s]+(\d+)", raw, re.IGNORECASE)
    if not code_match:
        # Попробуем найти просто число в скобках (некоторые SDK так форматируют)
        code_match = re.search(r"\((\d{5,6})\)", raw)
        code = int(code_match.group(1)) if code_match else None
    else:
        code = int(code_match.group(2))

    if code and code in _BYBIT_ERROR_MAP:
        return _BYBIT_ERROR_MAP[code]

    # Убираем технические суффиксы и оставляем только смысловую часть
    clean = re.sub(
        r"\s*\(ErrCode:\s*\d+\)\s*|\s*\(ErrTime:\s*[\d:]+\)\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip().rstrip(".")

    # Если осталось пустое или слишком короткое — общая фраза
    if len(clean) < 5:
        return f"Ошибка Bybit (код {code})." if code else "Неизвестная ошибка Bybit."

    # Первую букву — заглавная, добавляем точку
    return clean[0].upper() + clean[1:] + ("." if not clean.endswith(".") else "")


# ── Конвертация символа ──────────────────────────────

def to_bybit_symbol(symbol: str) -> str:
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


# Алиас для обратной совместимости
_to_bybit_symbol = to_bybit_symbol


# ── Получение qtyStep и tickSize для символа ─────────


# ── Получение qtyStep и tickSize для символа ─────────

def _get_instrument_filters(session, symbol: str) -> tuple[float, float]:
    """
    Запрашивает lotSizeFilter.qtyStep и priceFilter.tickSize у Bybit.
    Возвращает (qty_step, tick_size). При ошибке — безопасные дефолты.
    """
    try:
        resp = session.get_instruments_info(category="linear", symbol=symbol)
        if resp.get("retCode", -1) == 0:
            items = resp["result"].get("list", [])
            if items:
                lot  = items[0].get("lotSizeFilter", {})
                prc  = items[0].get("priceFilter",   {})
                qty_step  = float(lot.get("qtyStep",  "") or 0.001)
                tick_size = float(prc.get("tickSize", "") or 0.0001)
                return qty_step or 0.001, tick_size or 0.0001
    except Exception as e:
        log.debug(f"get_instruments_info {symbol}: {e}")
    return 0.001, 0.0001   # fallback


def _step_decimals(step: float) -> int:
    """Количество знаков после запятой в шаге (0.01 → 2, 0.001 → 3, 1 → 0)."""
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def _round_qty(qty: float, qty_step: float) -> str:
    """Округляем размер позиции вниз (floor) до шага qtyStep."""
    if qty_step <= 0:
        qty_step = 0.001
    decimals = _step_decimals(qty_step)
    factor   = 1.0 / qty_step
    qty_floor = math.floor(qty * factor) / factor
    return f"{qty_floor:.{decimals}f}"


def _round_price(price: float, tick_size: float) -> str:
    """Округляем цену до шага tickSize (ближайший, не floor — для SL/TP точности)."""
    if tick_size <= 0:
        tick_size = 0.0001
    decimals  = _step_decimals(tick_size)
    factor    = 1.0 / tick_size
    rounded   = round(price * factor) / factor
    return f"{rounded:.{decimals}f}"


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
            val = (coin.get("availableBalance") or
                   coin.get("availableToWithdraw") or
                   coin.get("walletBalance") or "0")
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0
    return 0.0


async def get_balance(api_key: str, api_secret: str) -> float:
    """Асинхронная обёртка для получения баланса."""
    loop = asyncio.get_running_loop()
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

    # Получаем реальный шаг лота и тик-сайз с Bybit и округляем
    qty_step, tick_size = _get_instrument_filters(session, bb_symbol)
    qty_str  = _round_qty(qty_raw, qty_step)
    if float(qty_str) <= 0:
        return {
            "ok": False,
            "error": f"Размер позиции слишком мал (${notional:.2f}). "
                     f"Нужен депозит от ${MIN_NOTIONAL / (risk_pct / 100):.0f} при риске {risk_pct}%."
        }

    # Проверяем, хватит ли маржи с учётом комиссии маркет-ордера.
    # Bybit 110007 = available balance < initial_margin + fees.
    # Используем 85% баланса как потолок (резерв на комиссии и maintenance margin).
    real_notional  = float(qty_str) * entry
    initial_margin = real_notional / lev
    fee_cost       = real_notional * TAKER_FEE * 2   # opening + closing round-trip
    margin_required = initial_margin + fee_cost
    if margin_required > balance * 0.85:
        return {
            "ok": False,
            "error": (
                f"Недостаточно маржи для открытия позиции.\n"
                f"Требуется: ${margin_required:.2f} USDT "
                f"(маржа ${initial_margin:.2f} + комиссия ~${fee_cost:.2f})\n"
                f"Доступно:  ${balance:.2f} USDT\n"
                f"Уменьши риск (сейчас {risk_pct}%) или пополни счёт."
            )
        }

    # Выставляем ордер.
    # positionIdx: 0 = One-Way Mode, 1 = Hedge Buy, 2 = Hedge Sell.
    # Пробуем One-Way, при ошибке режима — переключаемся на Hedge.
    hedge_idx = 1 if side == "Buy" else 2

    sl_str = _round_price(sl, tick_size)

    def _do_place(position_idx: int) -> dict:
        return session.place_order(
            category="linear",
            symbol=bb_symbol,
            side=side,
            orderType="Market",
            qty=qty_str,
            stopLoss=sl_str,
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

            # ── 3 частичных TP: 50% / 25% / 25% ─────────────────────────
            close_side = "Sell" if side == "Buy" else "Buy"
            # TP1 берёт всё что не вошло в TP2+TP3 (учёт ошибок округления)
            qty_tp2    = _round_qty(qty_f * 0.25, qty_step)
            qty_tp3    = _round_qty(qty_f * 0.25, qty_step)
            # Остаток (50%+ погрешность округления) — в TP1
            qty_tp1_f  = qty_f - float(qty_tp2) - float(qty_tp3)
            qty_tp1    = _round_qty(max(qty_tp1_f, float(qty_tp2)), qty_step)

            tp_orders = [
                (tp1, qty_tp1,  "50%"),
                (tp2, qty_tp2,  "25%"),
                (tp3, qty_tp3,  "25%"),
            ]
            for tp_price, qty_tp_str, _pct in tp_orders:
                if not tp_price or tp_price <= 0:
                    continue
                if float(qty_tp_str) <= 0:
                    continue
                try:
                    session.place_order(
                        category    = "linear",
                        symbol      = bb_symbol,
                        side        = close_side,
                        orderType   = "Limit",
                        qty         = qty_tp_str,
                        price       = _round_price(tp_price, tick_size),
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
    loop = asyncio.get_running_loop()
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
        _, tick_size = _get_instrument_filters(session, bb_symbol)
        resp = session.set_trading_stop(
            category    = "linear",
            symbol      = bb_symbol,
            stopLoss    = _round_price(entry, tick_size),
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
    loop = asyncio.get_running_loop()
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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _get_positions_sync, api_key, api_secret, symbol,
    )


# ── Отмена всех открытых ордеров по символу ──────────────

def _cancel_all_orders_sync(api_key: str, api_secret: str, symbol: str) -> dict:
    """Синхронно отменяет все открытые лимитные TP-ордера по символу."""
    session   = _get_session(api_key, api_secret)
    bb_symbol = _to_bybit_symbol(symbol)
    try:
        resp = session.cancel_all_orders(category="linear", symbol=bb_symbol)
        if resp.get("retCode", -1) == 0:
            return {"ok": True, "cancelled": len(resp.get("result", {}).get("list", []))}
        return {"ok": False, "error": resp.get("retMsg", "cancel error")}
    except Exception as e:
        log.debug(f"cancel_all_orders {symbol}: {e}")
        return {"ok": False, "error": str(e)}


async def cancel_all_orders(api_key: str, api_secret: str, symbol: str) -> dict:
    """Асинхронно отменяет все открытые ордера по символу (чистка после закрытия позиции)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _cancel_all_orders_sync, api_key, api_secret, symbol,
    )


# ── Закрытые позиции (для определения результата сделки) ──────────────────────

def _get_closed_pnl_sync(api_key: str, api_secret: str, symbol: str) -> list:
    """Возвращает последние закрытые позиции по символу (linear)."""
    session   = _get_session(api_key, api_secret)
    bb_symbol = _to_bybit_symbol(symbol)
    try:
        resp = session.get_closed_pnl(category="linear", symbol=bb_symbol, limit=10)
        if resp.get("retCode", -1) == 0:
            return resp["result"].get("list", [])
    except Exception as e:
        log.debug(f"get_closed_pnl {symbol}: {e}")
    return []


async def get_closed_pnl(api_key: str, api_secret: str, symbol: str) -> list:
    """Асинхронно возвращает последние закрытые позиции."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _get_closed_pnl_sync, api_key, api_secret, symbol,
    )


# ── Список открытых ордеров ───────────────────────────

def _get_open_orders_sync(api_key: str, api_secret: str) -> list:
    """Возвращает все открытые ордера по всем символам (linear USDT)."""
    session = _get_session(api_key, api_secret)
    try:
        resp = session.get_open_orders(category="linear", settleCoin="USDT", limit=50)
        if resp.get("retCode", -1) == 0:
            return resp["result"].get("list", [])
    except Exception as e:
        log.debug(f"get_open_orders: {e}")
    return []


async def get_open_orders(api_key: str, api_secret: str) -> list:
    """Асинхронно возвращает все открытые ордера."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_open_orders_sync, api_key, api_secret)


# ── Отмена одного ордера ──────────────────────────────

def _cancel_order_sync(api_key: str, api_secret: str, symbol: str, order_id: str) -> dict:
    """Синхронно отменяет один ордер по orderId."""
    session = _get_session(api_key, api_secret)
    bb_symbol = _to_bybit_symbol(symbol)
    try:
        resp = session.cancel_order(category="linear", symbol=bb_symbol, orderId=order_id)
        if resp.get("retCode", -1) == 0:
            return {"ok": True}
        return {"ok": False, "error": resp.get("retMsg", "cancel error")}
    except Exception as e:
        log.error(f"cancel_order {symbol} {order_id}: {e}")
        return {"ok": False, "error": str(e)}


async def cancel_order(api_key: str, api_secret: str, symbol: str, order_id: str) -> dict:
    """Асинхронно отменяет один ордер."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _cancel_order_sync, api_key, api_secret, symbol, order_id)


# ── Закрытие позиции по рынку ─────────────────────────

def _close_position_sync(api_key: str, api_secret: str,
                         symbol: str, side: str,
                         size: str, pos_idx: int = 0) -> dict:
    """
    Синхронно закрывает позицию маркет-ордером.
    side: текущая сторона позиции ('Buy' или 'Sell').
    """
    session = _get_session(api_key, api_secret)
    bb_symbol = _to_bybit_symbol(symbol)
    close_side = "Sell" if side == "Buy" else "Buy"
    try:
        resp = session.place_order(
            category="linear",
            symbol=bb_symbol,
            side=close_side,
            orderType="Market",
            qty=size,
            reduceOnly=True,
            positionIdx=pos_idx,
        )
        if resp.get("retCode", -1) == 0:
            return {"ok": True, "order_id": resp["result"].get("orderId", "")}
        return {"ok": False, "error": resp.get("retMsg", "close error")}
    except Exception as e:
        log.error(f"close_position {symbol}: {e}")
        return {"ok": False, "error": str(e)}


async def close_position(api_key: str, api_secret: str,
                         symbol: str, side: str,
                         size: str, pos_idx: int = 0) -> dict:
    """Асинхронно закрывает позицию маркет-ордером."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _close_position_sync,
        api_key, api_secret, symbol, side, size, pos_idx,
    )


# ── Сводка аккаунта (баланс + 24ч статистика) ────────

def _get_account_summary_sync(api_key: str, api_secret: str) -> dict:
    """
    Возвращает сводку аккаунта:
    equity, unrealized_pnl, available + 24ч closed PnL/trades/wins.
    """
    import time as _time
    session = _get_session(api_key, api_secret)
    result = {
        "equity": 0.0,
        "wallet_balance": 0.0,
        "unrealized_pnl": 0.0,
        "available": 0.0,
        "closed_pnl_24h": 0.0,
        "trades_24h": 0,
        "wins_24h": 0,
    }

    # Баланс
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        if resp.get("retCode", -1) == 0:
            acct = resp["result"]["list"][0]
            result["equity"]          = float(acct.get("totalEquity")        or 0)
            result["wallet_balance"]  = float(acct.get("totalWalletBalance") or 0)
            result["unrealized_pnl"]  = float(acct.get("totalUnrealisedPnl") or 0)
            for coin in acct.get("coin", []):
                if coin["coin"] == "USDT":
                    result["available"] = float(
                        coin.get("availableToWithdraw") or
                        coin.get("availableBalance") or 0
                    )
    except Exception as e:
        log.debug(f"account_summary wallet: {e}")

    # 24ч закрытые сделки
    try:
        start_ts = int((_time.time() - 86400) * 1000)
        resp = session.get_closed_pnl(
            category="linear",
            startTime=start_ts,
            limit=50,
        )
        if resp.get("retCode", -1) == 0:
            records = resp["result"].get("list", [])
            result["trades_24h"] = len(records)
            for r in records:
                pnl = float(r.get("closedPnl", 0))
                result["closed_pnl_24h"] += pnl
                if pnl > 0:
                    result["wins_24h"] += 1
    except Exception as e:
        log.debug(f"account_summary 24h: {e}")

    return result


async def get_account_summary(api_key: str, api_secret: str) -> dict:
    """Асинхронно возвращает сводку аккаунта."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_account_summary_sync, api_key, api_secret)


# ── Быстрый дашборд (одна сессия — все запросы) ───────

def _get_dashboard_sync(api_key: str, api_secret: str) -> tuple:
    """
    Загружает позиции, ордера и сводку счёта за одно подключение.
    Возвращает (positions, orders, summary).
    Намного быстрее чем 3 отдельных вызова.
    """
    import time as _time
    session = _get_session(api_key, api_secret)

    # ── Позиции ──
    positions = []
    try:
        resp = session.get_positions(category="linear", settleCoin="USDT")
        if resp.get("retCode", -1) == 0:
            positions = [p for p in resp["result"].get("list", [])
                         if float(p.get("size", 0)) > 0]
    except Exception as e:
        log.debug(f"dashboard positions: {e}")

    # ── Открытые ордера ──
    orders = []
    try:
        resp = session.get_open_orders(category="linear", settleCoin="USDT", limit=50)
        if resp.get("retCode", -1) == 0:
            orders = resp["result"].get("list", [])
    except Exception as e:
        log.debug(f"dashboard orders: {e}")

    # ── Сводка счёта ──
    summary = {
        "equity": 0.0, "wallet_balance": 0.0,
        "unrealized_pnl": 0.0, "available": 0.0,
        "closed_pnl_24h": 0.0, "trades_24h": 0, "wins_24h": 0,
    }
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        if resp.get("retCode", -1) == 0:
            acct = resp["result"]["list"][0]
            summary["equity"]         = float(acct.get("totalEquity")        or 0)
            summary["wallet_balance"] = float(acct.get("totalWalletBalance") or 0)
            summary["unrealized_pnl"] = float(acct.get("totalUnrealisedPnl") or 0)
            for coin in acct.get("coin", []):
                if coin["coin"] == "USDT":
                    summary["available"] = float(
                        coin.get("availableToWithdraw") or
                        coin.get("availableBalance") or 0
                    )
    except Exception as e:
        log.debug(f"dashboard wallet: {e}")

    try:
        start_ts = int((_time.time() - 86400) * 1000)
        resp = session.get_closed_pnl(category="linear", startTime=start_ts, limit=50)
        if resp.get("retCode", -1) == 0:
            records = resp["result"].get("list", [])
            summary["trades_24h"] = len(records)
            for r in records:
                pnl = float(r.get("closedPnl", 0))
                summary["closed_pnl_24h"] += pnl
                if pnl > 0:
                    summary["wins_24h"] += 1
    except Exception as e:
        log.debug(f"dashboard 24h: {e}")

    return positions, orders, summary


async def get_dashboard(api_key: str, api_secret: str) -> tuple:
    """Асинхронно возвращает (positions, orders, summary) за одно подключение."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_dashboard_sync, api_key, api_secret)
def _get_execution_exit_price_sync(
    api_key: str, api_secret: str, symbol: str, created_ms: float
) -> Optional[float]:
    """
    Ищет цену закрытия позиции в истории исполнений (executions).
    Возвращает средневзвешенную цену всех исполнений по закрывающей стороне после created_ms.
    """
    session   = _get_session(api_key, api_secret)
    bb_symbol = _to_bybit_symbol(symbol)
    try:
        resp = session.get_executions(
            category="linear", symbol=bb_symbol, limit=50
        )
        if resp.get("retCode", -1) != 0:
            return None
        items = resp.get("result", {}).get("list", [])
        # Фильтруем: только execType=Trade, после created_ms, closeOnDebit или reduceOnly
        prices, qtys = [], []
        for ex in items:
            if float(ex.get("execTime", 0)) < created_ms:
                continue
            # Закрывающие исполнения имеют closedSize > 0 или execType = "BustTrade"
            closed_size = float(ex.get("closedSize") or 0)
            if closed_size <= 0:
                continue
            price = float(ex.get("execPrice") or 0)
            if price > 0:
                prices.append(price)
                qtys.append(closed_size)
        if not prices:
            return None
        # Средневзвешенная цена выхода
        total_qty = sum(qtys)
        if total_qty <= 0:
            return None
        vwap = sum(p * q for p, q in zip(prices, qtys)) / total_qty
        return round(vwap, 8)
    except Exception as e:
        log.debug(f"get_execution_exit_price {symbol}: {e}")
        return None


async def get_execution_exit_price(
    api_key: str, api_secret: str, symbol: str, created_ms: float
) -> Optional[float]:
    """Асинхронно ищет цену выхода через историю исполнений."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _get_execution_exit_price_sync, api_key, api_secret, symbol, created_ms,
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

        tp_lines = f"🎯 TP1: <code>{tp1}</code>  (50% позиции)\n"
        if tp2:
            tp_lines += f"🎯 TP2: <code>{tp2}</code>  (25% позиции)\n"
        if tp3:
            tp_lines += f"🏆 TP3: <code>{tp3}</code>  (25% позиции)\n"
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
        raw_err = result.get("error", "")
        reason  = _humanize_bybit_error(raw_err)
        return (
            f"❌ <b>Не удалось открыть сделку</b>\n\n"
            f"⚠️ {reason}"
        )
