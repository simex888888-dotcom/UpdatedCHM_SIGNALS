"""
market_monitor.py — WebSocket-монитор BingX.

Подключается к BingX WS, декомпрессирует gzip-сообщения,
поддерживает буферы свечей (200 штук) и снапшоты стакана.
После каждого закрытия свечи пушит событие в asyncio.Queue.
"""

import asyncio
import gzip
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import pandas as pd

from pump_dump.pd_config import (
    BINGX_WS_URL, BINGX_REST_FUTURES,
    MIN_VOLUME_24H_USDT,
    CANDLE_BUFFER, WS_RECONNECT_MAX, HEARTBEAT_INTERVAL, WS_SYMBOLS_PER_CONN,
)

log = logging.getLogger("CHM.PD.Monitor")


@dataclass
class Candle:
    ts: int       # timestamp (мс)
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_vol: float   # объём покупателей


@dataclass
class OrderBook:
    symbol: str
    bids: list   # [[price, qty], ...]
    asks: list
    ts: float = field(default_factory=time.time)


@dataclass
class MarketEvent:
    """Событие, пушимое в очередь анализаторов."""
    symbol: str
    candles: pd.DataFrame          # последние CANDLE_BUFFER свечей
    orderbook: Optional[OrderBook]
    trades_buy_vol: float          # суммарный buy volume за последние 30 тиков
    trades_sell_vol: float
    last_price: float


class MarketMonitor:
    def __init__(self, queue: asyncio.Queue):
        self._queue      = queue
        self._candles:    dict[str, deque]      = defaultdict(lambda: deque(maxlen=CANDLE_BUFFER))
        self._orderbooks: dict[str, OrderBook]  = {}
        self._buy_vols:   dict[str, deque]      = defaultdict(lambda: deque(maxlen=30))
        self._sell_vols:  dict[str, deque]      = defaultdict(lambda: deque(maxlen=30))
        self._symbols:    list[str]             = []
        self._running     = False
        self._dropped_events: int = 0

    # ─── Публичный интерфейс ─────────────────────────────────────────────────

    async def run_forever(self):
        """Главный цикл: получает список монет, затем держит WS соединение."""
        self._running = True
        log.info("🔌 PD Monitor запускается…")

        # Баг 1 фикс: retry вместо return — сетевая ошибка не должна убивать систему навсегда
        while self._running and not self._symbols:
            await self._fetch_top_symbols()
            if not self._symbols:
                log.error("❌ PD Monitor: не удалось получить список монет, повтор через 30с")
                await asyncio.sleep(30)

        _prewarm_done = False  # Баг 4 фикс: прогрев только при первом старте
        backoff = 1
        while self._running:
            try:
                # Загружаем историю только если буфер пустой (первый старт).
                # При реконнекте данные в памяти сохраняются — не тратим 30+ сек.
                needs_history = not any(
                    len(v) >= 10 for v in self._candles.values()
                )
                if needs_history:
                    await self._fetch_historical_candles()
                else:
                    log.info("♻️  PD Monitor: реконнект — исторические свечи уже в памяти, пропускаем загрузку")
                if not _prewarm_done:
                    await self._push_initial_events()
                    _prewarm_done = True
                await self._ws_loop()
                backoff = 1
            except Exception as exc:
                exc_str = str(exc)
                # "Cannot write to closing transport" — штатное закрытие BingX WS,
                # не настоящая ошибка — логируем тихо
                if "closing transport" in exc_str or "ConnectionResetError" in exc_str:
                    log.debug(f"PD WS закрыт сервером. Реконнект через {backoff}с")
                else:
                    log.warning(f"PD WS ошибка: {exc_str}. Реконнект через {backoff}с")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_RECONNECT_MAX)

    def stop(self):
        self._running = False

    def get_symbols(self) -> list[str]:
        return list(self._symbols)

    # ─── Получение топ монет ─────────────────────────────────────────────────

    async def _fetch_top_symbols(self):
        url = f"{BINGX_REST_FUTURES}/quote/ticker"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    data = await r.json()
            tickers = data.get("data", [])
            filtered = [
                t for t in tickers
                if float(t.get("quoteVolume", 0)) >= MIN_VOLUME_24H_USDT
                and t.get("symbol", "").endswith("-USDT")
            ]
            filtered.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
            self._symbols = [t["symbol"] for t in filtered]
            log.info(f"✅ PD Monitor: {len(self._symbols)} монет для мониторинга")
        except Exception as e:
            log.error(f"PD fetch_top_symbols: {e}")

    # ─── Прогрев: загрузка исторических свечей ───────────────────────────────

    async def _fetch_historical_candles(self):
        """Загружает 200 исторических 1m-свечей для каждой монеты."""
        url = f"{BINGX_REST_FUTURES}/quote/klines"
        async with aiohttp.ClientSession() as session:
            tasks = [
                self._load_history_one(session, url, sym)
                for sym in self._symbols
            ]
            # Запускаем порциями по 10 чтобы не перегрузить API
            for i in range(0, len(tasks), 10):
                await asyncio.gather(*tasks[i:i+10], return_exceptions=True)
                await asyncio.sleep(0.5)
        loaded = sum(1 for s in self._symbols if len(self._candles[s]) >= 10)
        log.info(f"📊 PD Monitor: исторические свечи загружены для {loaded}/{len(self._symbols)} монет")

    async def _push_initial_events(self):
        """Прогрев: сразу публикуем события из исторических данных.

        Без этого _current_scores пуст до получения первого WS-события (до 60с),
        и пользователь видит «Анализ ещё не завершён».
        """
        pushed = 0
        for sym in self._symbols:
            if len(self._candles[sym]) >= 30:
                await self._push_event(sym)
                pushed += 1
        log.info(f"🔥 PD Monitor: прогрев завершён, отправлено {pushed} начальных событий")

    async def _load_history_one(self, session, url, symbol):
        try:
            params = {"symbol": symbol, "interval": "1m", "limit": CANDLE_BUFFER}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
            candles_raw = data.get("data", [])
            for c in candles_raw:
                self._candles[symbol].append(Candle(
                    ts=int(c.get("time", 0)),
                    open=float(c.get("open", 0)),
                    high=float(c.get("high", 0)),
                    low=float(c.get("low", 0)),
                    close=float(c.get("close", 0)),
                    volume=float(c.get("volume", 0)),
                    buy_vol=float(c.get("volume", 0)) * 0.5,  # BingX не даёт buy/sell в REST
                ))
        except Exception as e:
            log.debug(f"history {symbol}: {e}")

    # ─── WebSocket цикл ──────────────────────────────────────────────────────

    async def _ws_loop(self):
        """Разбиваем символы на чанки и держим отдельное WS-соединение на каждый.

        BingX ограничивает одно соединение ~1024 подписками.
        При 3 стримах на монету (kline + depth + trade) максимум ~340 монет/коннект.
        WS_SYMBOLS_PER_CONN = 300 даёт 900 подписок — запас безопасности.
        Если любой коннект падает — исключение выходит наружу, run_forever перезапускает всё.
        """
        chunks = [
            self._symbols[i: i + WS_SYMBOLS_PER_CONN]
            for i in range(0, len(self._symbols), WS_SYMBOLS_PER_CONN)
        ]
        log.info(f"🔌 PD WS: {len(self._symbols)} монет → {len(chunks)} соединений")
        await asyncio.gather(*[
            self._ws_connection(chunk, conn_id)
            for conn_id, chunk in enumerate(chunks)
        ])

    async def _ws_connection(self, symbols: list[str], conn_id: int):
        """Одно WS-соединение для заданного списка символов."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                BINGX_WS_URL,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as ws:
                log.info(f"✅ PD WS[{conn_id}] подключён ({len(symbols)} монет)")
                await self._subscribe_chunk(ws, symbols, conn_id)
                heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                try:
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await self._handle_raw(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            text = msg.data
                            if text in ("Ping", "ping"):
                                try:
                                    await ws.send_str("Pong")
                                except Exception:
                                    break  # WS уже закрывается — выходим штатно
                            elif text.startswith("{"):
                                await self._handle_text(text)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            raise ConnectionError(f"WS[{conn_id}] closed")
                finally:
                    heartbeat_task.cancel()

    async def _heartbeat(self, ws):
        """Периодически шлём Pong чтобы BingX не закрыл соединение."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send_str("Pong")
            except Exception:
                break

    async def _subscribe_chunk(self, ws, symbols: list[str], conn_id: int = 0):
        """Подписываемся на kline_1m, depth20, trade для переданного списка монет."""
        uid_base = conn_id * WS_SYMBOLS_PER_CONN * 3
        uid = uid_base
        for sym in symbols:
            for stream in (f"{sym}@kline_1m", f"{sym}@depth20", f"{sym}@trade"):
                uid += 1
                payload = json.dumps({"id": str(uid), "reqType": "sub", "dataType": stream})
                await ws.send_str(payload)
                await asyncio.sleep(0.02)  # не флудим

    # ─── Обработка входящих сообщений ────────────────────────────────────────

    async def _handle_raw(self, raw: bytes):
        try:
            try:
                text = gzip.decompress(raw).decode("utf-8")
            except Exception:
                # Не gzip — пробуем как plaintext
                text = raw.decode("utf-8", errors="ignore")
            data = json.loads(text)
        except Exception:
            return
        await self._dispatch(data)

    async def _handle_text(self, text: str):
        try:
            data = json.loads(text)
        except Exception:
            return
        await self._dispatch(data)

    async def _dispatch(self, data: dict):
        dtype = data.get("dataType", "")
        if not dtype:
            return

        symbol = dtype.split("@")[0]
        if symbol not in self._symbols:
            return

        if "@kline_1m" in dtype:
            await self._on_kline(symbol, data.get("data", {}))
        elif "@depth20" in dtype:
            self._on_depth(symbol, data.get("data", {}))
        elif "@trade" in dtype:
            self._on_trade(symbol, data.get("data", {}))

    def _on_depth(self, symbol: str, data: dict):
        bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
        self._orderbooks[symbol] = OrderBook(symbol=symbol, bids=bids, asks=asks)

    def _on_trade(self, symbol: str, data: dict):
        """m=true → продажа (покупатель — маркет-мейкер → sell), m=false → покупка."""
        qty = float(data.get("q", 0))
        price = float(data.get("p", 0))
        vol = qty * price
        if data.get("m", False):
            self._sell_vols[symbol].append(vol)
        else:
            self._buy_vols[symbol].append(vol)

    async def _on_kline(self, symbol: str, data: dict):
        """Обновляем свечу. Если свеча закрылась — пушим событие в очередь."""
        if not data:
            return
        # BingX использует 't' (open time) как идентификатор периода.
        # 'T' может быть close time в некоторых версиях API — берём оба.
        ts_raw = data.get("t") or data.get("T") or 0
        candle = Candle(
            ts=int(ts_raw),
            open=float(data.get("o", 0)),
            high=float(data.get("h", 0)),
            low=float(data.get("l", 0)),
            close=float(data.get("c", 0)),
            volume=float(data.get("v", 0)),
            buy_vol=float(data.get("Q", data.get("v", 0))) * 0.5,
        )

        buf = self._candles[symbol]
        # Заменяем последнюю или добавляем новую
        if buf and buf[-1].ts == candle.ts:
            buf[-1] = candle
        else:
            buf.append(candle)
            # Новая свеча → прежняя закрылась → публикуем событие
            if len(buf) >= 30:
                await self._push_event(symbol)

    async def _push_event(self, symbol: str):
        buf  = self._candles[symbol]
        if len(buf) < 10:
            return

        df = pd.DataFrame([
            {"ts": c.ts, "open": c.open, "high": c.high,
             "low": c.low, "close": c.close,
             "volume": c.volume, "buy_vol": c.buy_vol}
            for c in buf
        ])
        buy_vol  = sum(self._buy_vols[symbol])
        sell_vol = sum(self._sell_vols[symbol])

        event = MarketEvent(
            symbol=symbol,
            candles=df,
            orderbook=self._orderbooks.get(symbol),
            trades_buy_vol=buy_vol,
            trades_sell_vol=sell_vol,
            last_price=float(buf[-1].close),
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_events += 1
            if self._dropped_events % 50 == 1:
                log.warning(
                    f"📛 Queue переполнена! Пропущено {self._dropped_events} событий. "
                    f"Анализаторы не успевают (queue maxsize={self._queue.maxsize})."
                )
