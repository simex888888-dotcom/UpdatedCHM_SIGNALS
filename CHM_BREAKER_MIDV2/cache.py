"""
cache.py — in-memory кэш свечей с TTL
Заменяет Redis для 50-500 пользователей.
Не нужно ничего устанавливать.

Ключевая оптимизация: все пользователи на одном таймфрейме
разделяют одни и те же данные в памяти — никаких дублирующих запросов.
"""

import asyncio
import time
import logging
from typing import Optional
import pandas as pd
from collections import OrderedDict

log = logging.getLogger("CHM.Cache")


class TTLCache:
    """
    Thread-safe кэш с TTL и ограничением размера (LRU eviction).
    Хранит DataFrame-ы свечей в памяти.
    Для 200 монет × 1 TF: ~40-60 MB RAM.
    Для 200 монет × 3 TF: ~120-180 MB RAM.
    """

    def __init__(self, max_size: int = 300):
        self._data:     OrderedDict = OrderedDict()  # key -> (df, expires_at)
        self._max_size: int         = max_size
        self._lock:     asyncio.Lock = asyncio.Lock()
        self._hits   = 0
        self._misses = 0

    async def get(self, key: str) -> Optional[pd.DataFrame]:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            df, expires_at = entry
            if time.time() > expires_at:
                del self._data[key]
                self._misses += 1
                return None
            # LRU — перемещаем в конец
            self._data.move_to_end(key)
            self._hits += 1
            return df

    async def set(self, key: str, df: pd.DataFrame, ttl: int):
        async with self._lock:
            # Если достигли лимита — удаляем самый старый
            if len(self._data) >= self._max_size and key not in self._data:
                self._data.popitem(last=False)
            self._data[key] = (df, time.time() + ttl)

    async def delete(self, key: str):
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self):
        async with self._lock:
            self._data.clear()

    def size(self) -> int:
        return len(self._data)

    def stats(self) -> dict:
        total = self._hits + self._misses
        ratio = self._hits / total * 100 if total else 0
        return {
            "size":   self.size(),
            "hits":   self._hits,
            "misses": self._misses,
            "ratio":  round(ratio, 1),
        }


# ── Глобальный кэш ──────────────────────────────────

_candle_cache: Optional[TTLCache] = None
_coins_cache:  Optional[tuple]    = None   # (list, expires_at)
_COINS_TTL = 6 * 3600


def init_cache(max_symbols: int = 300):
    global _candle_cache
    _candle_cache = TTLCache(max_size=max_symbols)
    log.info(f"✅ In-memory кэш инициализирован (max {max_symbols} символов)")


def _candle_key(symbol: str, tf: str) -> str:
    return f"{symbol}_{tf}"


async def get_candles(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    return await _candle_cache.get(_candle_key(symbol, tf))


async def set_candles(symbol: str, tf: str, df: pd.DataFrame, ttl_map: dict):
    ttl = ttl_map.get(tf, 3600)
    await _candle_cache.set(_candle_key(symbol, tf), df, ttl)


async def get_coins() -> Optional[list]:
    global _coins_cache
    if _coins_cache and time.time() < _coins_cache[1]:
        return _coins_cache[0]
    return None


async def set_coins(coins: list):
    global _coins_cache
    _coins_cache = (coins, time.time() + _COINS_TTL)


def cache_stats() -> dict:
    return _candle_cache.stats() if _candle_cache else {}
