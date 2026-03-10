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
    TOP_COINS_COUNT, MIN_VOLUME_24H_USDT,
    CANDLE_BUFFER, WS_RECONNECT_MAX, HEARTBEAT_INTERVAL,
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

    # ─── Публичный интерфейс ─────────────────────────────────────────────────

    async def run_forever(self):
        """Главный цикл: получает список монет, затем держит WS соединение."""
        self._running = True
        log.info("🔌 PD Monitor запускается…")
        await self._fetch_top_symbols()
        if not self._symbols:
            log.error("❌ PD Monitor: не удалось получить список монет")
            return

        backoff = 1
        while self._running:
            try:
                await self._fetch_historical_candles()
                await self._ws_loop()
                backoff = 1
            except Exception as exc:
                log.warning(f"PD WS ошибка: {exc}. Реконнект через {backoff}с")
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
            self._symbols = [t["symbol"] for t in filtered[:TOP_COINS_COUNT]]
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
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                BINGX_WS_URL,
                heartbeat=HEARTBEAT_INTERVAL,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as ws:
                log.info("✅ PD WS подключён")
                await self._subscribe_all(ws)
                heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                try:
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await self._handle_raw(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            if msg.data == "Ping":
                                await ws.send_str("Pong")
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
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

    async def _subscribe_all(self, ws):
        """Подписываемся на kline_1m, depth20, trade для каждой монеты."""
        uid = 0
        for sym in self._symbols:
            for stream in (f"{sym}@kline_1m", f"{sym}@depth20", f"{sym}@trade"):
                uid += 1
                payload = json.dumps({"id": str(uid), "reqType": "sub", "dataType": stream})
                await ws.send_str(payload)
                await asyncio.sleep(0.02)  # не флудим

    # ─── Обработка входящих сообщений ────────────────────────────────────────

    async def _handle_raw(self, raw: bytes):
        try:
            data = json.loads(gzip.decompress(raw))
        except Exception:
            return
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
        candle = Candle(
            ts=int(data.get("T", 0)),
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
            pass  # анализаторы не успевают — пропускаем
