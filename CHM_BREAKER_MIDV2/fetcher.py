"""
fetcher.py — загрузка данных с OKX
v4.3 — rate limiting + exponential backoff на 429
"""

import logging
import asyncio
import ssl
import time
import certifi
import aiohttp
import pandas as pd
from typing import Optional

log = logging.getLogger("CHM.Fetcher")

OKX_CANDLES  = "https://www.okx.com/api/v5/market/candles"
OKX_TICKERS  = "https://www.okx.com/api/v5/market/tickers"
OKX_SYMBOLS  = "https://www.okx.com/api/v5/public/instruments"

TIMEFRAME_MAP = {
    "1m":  "1m",  "3m":  "3m",  "5m":  "5m",  "15m": "15m",
    "30m": "30m", "1h":  "1H",  "2h":  "2H",  "4h":  "4H",
    "6h":  "6H",  "12h": "12H", "1d":  "1D",  "1w":  "1W",
}


class BinanceFetcher:  # имя оставляем чтобы не менять другие файлы

    # OKX лимит: 20 req/sec на candles, 10 req/sec на tickers
    # Ставим 6 параллельных + 0.15 сек между запросами = ~6 req/sec — безопасно
    _CONCURRENCY = 6
    _MIN_DELAY   = 0.15   # минимум секунд между любыми двумя запросами

    def __init__(self):
        self._session:           Optional[aiohttp.ClientSession] = None
        self._semaphore:         Optional[asyncio.Semaphore]     = None
        self._last_request_time: float = 0.0

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._CONCURRENCY)
        return self._semaphore

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout   = aiohttp.ClientTimeout(total=15)
            ssl_ctx   = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=12)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        return self._session

    async def _throttle(self):
        """Принудительная пауза между запросами — защита от 429."""
        now     = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._MIN_DELAY:
            await asyncio.sleep(self._MIN_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    async def _get(self, url: str, params: dict, retries: int = 4) -> Optional[dict]:
        """
        Универсальный GET с throttle + exponential backoff на 429.
        Возвращает распарсенный JSON или None.
        """
        sem = self._get_semaphore()
        for attempt in range(1, retries + 1):
            async with sem:
                await self._throttle()
                try:
                    session = await self._get_session()
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            return await resp.json()

                        if resp.status == 429:
                            # Rate limit — ждём экспоненциально: 2, 4, 8, 16 сек
                            wait = 2 ** attempt
                            log.warning(
                                f"429 Too Many Requests → ждём {wait}с "
                                f"(попытка {attempt}/{retries})"
                            )
                            await asyncio.sleep(wait)
                            continue

                        text = await resp.text()
                        log.warning(f"HTTP {resp.status}: {text[:120]}")
                        return None

                except asyncio.TimeoutError:
                    log.warning(f"Таймаут (попытка {attempt}/{retries})")
                except Exception as e:
                    log.warning(f"Ошибка запроса: {e} (попытка {attempt}/{retries})")

                if attempt < retries:
                    await asyncio.sleep(attempt)

        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─────────────────────────────────────────────
    #  Автозагрузка списка монет с OKX
    # ─────────────────────────────────────────────

    async def get_all_usdt_pairs(
        self,
        min_volume_usdt: float = 1_000_000,
        blacklist: list = None,
        max_coins: int = 0,
    ) -> list:
        blacklist = blacklist or []
        log.info("🌐 Загружаю список всех монет с OKX...")

        data = await self._get(OKX_SYMBOLS, {"instType": "SWAP"})
        if not data:
            return []

        all_usdt = {
            s["instId"]
            for s in data.get("data", [])
            if s["instId"].endswith("USDT-SWAP")
            and s["state"] == "live"
            and s["instId"] not in blacklist
        }
        log.info(f"  Найдено активных USDT пар: {len(all_usdt)}")

        tdata = await self._get(OKX_TICKERS, {"instType": "SWAP"})
        if not tdata:
            return sorted(all_usdt)

        filtered = []
        for t in tdata.get("data", []):
            sym = t.get("instId", "")
            if sym not in all_usdt:
                continue
            try:
                vol = float(t.get("volCcy24h", 0))
            except Exception:
                vol = 0
            if vol >= min_volume_usdt:
                filtered.append((sym, vol))

        filtered.sort(key=lambda x: x[1], reverse=True)
        coins = [sym for sym, _ in filtered]
        if max_coins and max_coins > 0:
            coins = coins[:max_coins]

        log.info(f"  После фильтра по объёму: {len(coins)} монет")
        return coins

    # ─────────────────────────────────────────────
    #  Свечи с OKX
    # ─────────────────────────────────────────────

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 300,
    ) -> Optional[pd.DataFrame]:
        interval   = TIMEFRAME_MAP.get(timeframe, "1H")
        okx_symbol = self._to_okx(symbol)

        params = {
            "instId": okx_symbol,
            "bar":    interval,
            "limit":  str(min(limit, 300)),
        }

        data = await self._get(OKX_CANDLES, params, retries=4)
        if not data:
            return None

        rows = data.get("data", [])
        if not rows:
            return None

        # OKX возвращает новые первыми — разворачиваем
        rows = list(reversed(rows))

        df = pd.DataFrame(rows, columns=[
            "open_time", "open", "high", "low", "close",
            "vol", "volCcy", "volCcyQuote", "confirm"
        ])
        df = df[["open_time", "open", "high", "low", "close", "volCcyQuote"]].copy()
        df.rename(columns={"volCcyQuote": "volume"}, inplace=True)
        df[["open", "high", "low", "close", "volume"]] = \
            df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df.assign(
            open_time=pd.to_datetime(df["open_time"].astype(float), unit="ms")
        )
        df.set_index("open_time", inplace=True)

        # Убираем незакрытую свечу (confirm == "0")
        df = df.iloc[:-1]
        return df

    # ─────────────────────────────────────────────
    #  Вспомогательные
    # ─────────────────────────────────────────────

    @staticmethod
    def _to_okx(symbol: str) -> str:
        """BTCUSDT → BTC-USDT-SWAP"""
        symbol = symbol.replace(" ", "")
        if symbol.endswith("USDT") and "-" not in symbol:
            base = symbol[:-4]
            return f"{base}-USDT-SWAP"
        return symbol

    async def get_ticker_price(self, symbol: str) -> Optional[float]:
        data = await self._get(OKX_TICKERS, {"instId": self._to_okx(symbol)})
        if data and data.get("data"):
            try:
                return float(data["data"][0]["last"])
            except Exception:
                pass
        return None

    async def get_24h_change(self, symbol: str) -> Optional[dict]:
        data = await self._get(OKX_TICKERS, {"instId": self._to_okx(symbol)})
        if not data or not data.get("data"):
            return None
        try:
            t     = data["data"][0]
            last  = float(t.get("last",    0))
            open_ = float(t.get("open24h", last))
            chg   = ((last - open_) / open_ * 100) if open_ else 0
            return {
                "change_pct":  chg,
                "volume_usdt": float(t.get("volCcy24h", 0)),
                "high":        float(t.get("high24h",   0)),
                "low":         float(t.get("low24h",    0)),
            }
        except Exception:
            return None
