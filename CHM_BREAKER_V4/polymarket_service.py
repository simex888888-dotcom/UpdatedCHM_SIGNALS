"""
polymarket_service.py — интеграция с Polymarket prediction market.

Зависимости:
  aiohttp (уже есть в requirements.txt)
  py-clob-client (опционально, только для торговли)
    pip install py-clob-client

Env vars (добавить в .env / docker-compose):
  POLY_PRIVATE_KEY      — приватный ключ Polygon кошелька (0x...)
  POLY_FUNDER_ADDRESS   — адрес кошелька (0x...)
  POLY_API_KEY          — из Polymarket Account → Export API Key
  POLY_API_SECRET
  POLY_API_PASSPHRASE
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiohttp

log = logging.getLogger("CHM.Poly")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
CHAIN_ID   = 137   # Polygon mainnet

# ─── Локальный кэш маркетов ──────────────────────────────────────────────────
# condition_id (str) → market dict.  Живёт 15 минут.
_market_cache: dict[str, tuple[float, dict]] = {}   # key → (ts, data)
_CACHE_TTL = 900

# Короткие int-ключи для callback_data (64-байтный лимит Telegram)
# short_key (int) → condition_id (str)
_short_keys: dict[int, str] = {}
_cid_to_short: dict[str, int] = {}
_sk_counter = 0


def _get_short_key(condition_id: str) -> int:
    global _sk_counter
    if condition_id in _cid_to_short:
        return _cid_to_short[condition_id]
    _sk_counter += 1
    _short_keys[_sk_counter] = condition_id
    _cid_to_short[condition_id] = _sk_counter
    return _sk_counter


def get_condition_id(short_key: int) -> Optional[str]:
    return _short_keys.get(short_key)


def _cache_put(condition_id: str, data: dict):
    _market_cache[condition_id] = (time.time(), data)


def _cache_get(condition_id: str) -> Optional[dict]:
    entry = _market_cache.get(condition_id)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


# ─── Правило-основанный анализ ───────────────────────────────────────────────

def _parse_prices(market: dict) -> tuple[float, float]:
    """Возвращает (yes_price, no_price) из разных форматов API."""
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = None
    if isinstance(prices, list) and len(prices) >= 2:
        try:
            return float(prices[0]), float(prices[1])
        except Exception:
            pass
    return 0.5, 0.5


def _get_tokens(market: dict) -> list[dict]:
    """Возвращает список {token_id, outcome}."""
    tokens = market.get("tokens", [])
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            return []
    return tokens if isinstance(tokens, list) else []


def analyze_market(market: dict) -> dict:
    """Правило-основанный анализ без вызова внешнего AI."""
    yes_price, no_price = _parse_prices(market)
    volume_24h = float(market.get("volume24hr", 0) or 0)
    liquidity  = float(market.get("liquidityNum", 0) or 0)

    if yes_price < 0.35 and volume_24h > 50_000:
        rec   = "BUY NO"
        conf  = "HIGH"
        edge  = round(0.35 - yes_price, 3)
        reason = (
            f"Рынок переоценивает YES ({yes_price:.0%}). "
            f"Объём ${volume_24h:,.0f} за 24ч подтверждает ликвидность. "
            f"Потенциальное преимущество ~{edge:.1%}."
        )
    elif yes_price > 0.65 and volume_24h > 50_000:
        rec   = "BUY YES"
        conf  = "HIGH"
        edge  = round(yes_price - 0.65, 3)
        reason = (
            f"Рынок недооценивает YES ({yes_price:.0%}). "
            f"Объём ${volume_24h:,.0f} за 24ч указывает на устойчивый тренд. "
            f"Потенциальное преимущество ~{edge:.1%}."
        )
    elif 0.35 < yes_price < 0.65 and liquidity < 10_000:
        rec   = "SKIP"
        conf  = "LOW"
        edge  = 0.0
        reason = (
            f"Рынок сбалансирован (YES {yes_price:.0%} / NO {no_price:.0%}) "
            f"при низкой ликвидности ${liquidity:,.0f}. Высокий спред."
        )
    else:
        rec   = "SKIP"
        conf  = "MEDIUM"
        edge  = 0.0
        reason = (
            f"Рынок в равновесии: YES {yes_price:.0%} / NO {no_price:.0%}. "
            f"Ликвидность ${liquidity:,.0f}. Нет явного преимущества."
        )

    return {
        "yes_price":      yes_price,
        "no_price":       no_price,
        "volume_24h":     volume_24h,
        "liquidity":      liquidity,
        "recommendation": rec,
        "confidence":     conf,
        "reasoning":      reason,
        "edge":           edge,
    }


# ─── Сервис ───────────────────────────────────────────────────────────────────

class PolymarketService:

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._configured = bool(os.getenv("POLY_PRIVATE_KEY"))

    def is_trading_enabled(self) -> bool:
        return self._configured

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Gamma API: маркеты ────────────────────────────────────────────────────

    async def get_trending_markets(self, limit: int = 10, offset: int = 0) -> list[dict]:
        s = await self._sess()
        params = {
            "active": "true", "closed": "false",
            "limit": limit, "offset": offset,
            "order": "volume24hr", "ascending": "false",
        }
        async with s.get(f"{GAMMA_BASE}/markets", params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"Gamma API {r.status}")
            raw = await r.json()
        markets = raw if isinstance(raw, list) else raw.get("markets", [])
        for m in markets:
            _cache_put(m.get("id", ""), m)
        return markets

    async def search_markets(self, query: str, limit: int = 10) -> list[dict]:
        s = await self._sess()
        params = {"active": "true", "closed": "false", "limit": 50, "_c": query}
        async with s.get(f"{GAMMA_BASE}/markets", params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"Gamma API {r.status}")
            raw = await r.json()
        markets = raw if isinstance(raw, list) else raw.get("markets", [])
        ql = query.lower()
        result = [m for m in markets if ql in m.get("question", "").lower()]
        for m in result:
            _cache_put(m.get("id", ""), m)
        return result[:limit]

    async def get_market_by_id(self, condition_id: str) -> Optional[dict]:
        cached = _cache_get(condition_id)
        if cached:
            return cached
        s = await self._sess()
        async with s.get(f"{GAMMA_BASE}/markets", params={"id": condition_id}) as r:
            if r.status != 200:
                return None
            raw = await r.json()
        markets = raw if isinstance(raw, list) else raw.get("markets", [])
        if not markets:
            return None
        _cache_put(condition_id, markets[0])
        return markets[0]

    async def get_market_price(self, token_id: str) -> float:
        s = await self._sess()
        async with s.get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id}) as r:
            if r.status != 200:
                return 0.5
            data = await r.json()
        return float(data.get("mid", 0.5))

    async def analyze_market(self, market: dict) -> dict:
        """AI-анализ через Groq с fallback на rule-based."""
        yes_price, no_price = _parse_prices(market)
        volume_24h = float(market.get("volume24hr", 0) or 0)
        liquidity  = float(market.get("liquidityNum", 0) or 0)
        end_date   = (market.get("endDate") or "неизвестно")[:10]

        data = {
            "question":   market.get("question", ""),
            "yes_price":  yes_price,
            "no_price":   no_price,
            "volume_24h": volume_24h,
            "liquidity":  liquidity,
            "end_date":   end_date,
        }

        try:
            from groq_analyzer import analyze_with_groq
            ai = await analyze_with_groq(data)
        except Exception as e:
            log.warning(f"Groq fallback: {e}")
            ai = self._rule_based_analysis(data)

        return {**data, **ai}

    @staticmethod
    def _rule_based_analysis(data: dict) -> dict:
        y = data["yes_price"]
        v = data["volume_24h"]
        if y < 0.35 and v > 50_000:
            return {"recommendation": "BUY NO",  "confidence": "HIGH",
                    "reasoning": "Цена YES завышена относительно объёма торгов.",
                    "edge": f"~{(0.35 - y) * 100:.0f}%", "risk": "MEDIUM"}
        if y > 0.65 and v > 50_000:
            return {"recommendation": "BUY YES", "confidence": "HIGH",
                    "reasoning": "Цена NO завышена относительно объёма торгов.",
                    "edge": f"~{(y - 0.65) * 100:.0f}%", "risk": "MEDIUM"}
        return     {"recommendation": "SKIP",    "confidence": "LOW",
                    "reasoning": "Рынок сбалансирован, нет явного перекоса.",
                    "edge": "0%", "risk": "HIGH"}

    # ── Торговля (требует POLY_PRIVATE_KEY) ───────────────────────────────────

    def _make_client(self):
        """Создаёт ClobClient. Вызывается в asyncio.to_thread()."""
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        return ClobClient(
            host=CLOB_BASE,
            chain_id=CHAIN_ID,
            key=os.getenv("POLY_PRIVATE_KEY"),
            creds=ApiCreds(
                api_key=os.getenv("POLY_API_KEY", ""),
                api_secret=os.getenv("POLY_API_SECRET", ""),
                api_passphrase=os.getenv("POLY_API_PASSPHRASE", ""),
            ),
            signature_type=0,
            funder=os.getenv("POLY_FUNDER_ADDRESS", ""),
        )

    async def get_balance(self) -> float:
        if not self._configured:
            return 0.0
        try:
            def _fn():
                c = self._make_client()
                return c.get_balance()
            raw = await asyncio.to_thread(_fn)
            return float(raw) / 1e6
        except Exception as e:
            log.warning(f"get_balance: {e}")
            return 0.0

    async def get_my_positions(self) -> list[dict]:
        if not self._configured:
            return []
        try:
            def _fn():
                c = self._make_client()
                return c.get_positions() or []
            return await asyncio.to_thread(_fn)
        except Exception as e:
            log.warning(f"get_positions: {e}")
            return []

    async def place_bet(self, token_id: str, amount_usdc: float) -> dict:
        """Размещает рыночный ордер. Возвращает dict ответа от API."""
        if not self._configured:
            raise RuntimeError("Торговля не настроена (POLY_PRIVATE_KEY не задан)")

        def _fn():
            from py_clob_client.client import ClobClient  # noqa: F401 (import check)
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            c = self._make_client()
            order = c.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount_usdc)
            )
            return c.post_order(order, OrderType.FOK)

        return await asyncio.to_thread(_fn)
