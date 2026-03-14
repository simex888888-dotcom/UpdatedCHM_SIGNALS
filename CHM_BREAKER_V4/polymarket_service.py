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
import re
import time
from typing import Optional

import aiohttp

log = logging.getLogger("CHM.Poly")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
CHAIN_ID   = 137   # Polygon mainnet

# ─── Кэш переводов ────────────────────────────────────────────────────────────
# original question → (timestamp, translated)
_translation_cache: dict[str, tuple[float, str]] = {}
_TRANS_TTL = 86400   # 24 часа (вопросы не меняются)

# ─── Локальный кэш маркетов ──────────────────────────────────────────────────
# condition_id (str) → market dict.  Живёт 15 минут.
_market_cache: dict[str, tuple[float, dict]] = {}   # key → (ts, data)
_CACHE_TTL = 900

# ─── Кэш AI-анализа ──────────────────────────────────────────────────────────
# condition_id → (timestamp, analysis_dict).  Живёт 60 минут.
_ai_cache: dict[str, tuple[float, dict]] = {}
_AI_CACHE_TTL = 3600   # 1 час

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
    # Периодически удаляем истёкшие записи (каждые 50 put'ов)
    if len(_market_cache) % 50 == 0:
        _now = time.time()
        expired = [k for k, (ts, _) in list(_market_cache.items()) if _now - ts >= _CACHE_TTL]
        for k in expired:
            _market_cache.pop(k, None)


def _cache_get(condition_id: str) -> Optional[dict]:
    entry = _market_cache.get(condition_id)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _cache_invalidate(condition_id: str):
    """Удаляет запись из кэша маркета И кэша AI-анализа (принудительное обновление)."""
    _market_cache.pop(condition_id, None)
    _ai_cache.pop(condition_id, None)


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


# ─── Перевод вопроса на русский ───────────────────────────────────────────────

async def translate_question(question: str) -> str:
    """
    Переводит вопрос маркета на русский через Groq llama-3.3-70b.
    Возвращает оригинал если Groq недоступен или ключ не задан.
    Результат кэшируется на 24 часа.
    """
    if not question or not question.strip():
        return question

    # Проверяем кэш
    cached = _translation_cache.get(question)
    if cached and time.time() - cached[0] < _TRANS_TTL:
        return cached[1]

    # Если вопрос уже на русском — не переводим
    # (эвристика: >30% кириллических символов)
    cyr_count = sum(1 for c in question if "\u0400" <= c <= "\u04ff")
    if cyr_count / max(len(question), 1) > 0.3:
        _translation_cache[question] = (time.time(), question)
        return question

    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        return question

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=groq_key,
            base_url="https://api.groq.com/openai/v1",
        )
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Переведи вопрос с prediction market на русский язык. "
                        "Отвечай ТОЛЬКО переводом, без пояснений и кавычек. "
                        "Сохраняй смысл и имена собственные (биткоин, Трамп и т.д. — по-русски)."
                    ),
                },
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            max_tokens=120,
        )
        translated = response.choices[0].message.content.strip()
        # Убираем кавычки если модель их добавила
        translated = re.sub(r'^["\'"«»]+|["\'"«»]+$', "", translated).strip()
        if translated:
            _translation_cache[question] = (time.time(), translated)
            # Ограничиваем кэш переводов — удаляем старые записи при превышении лимита
            if len(_translation_cache) > 3000:
                _cutoff = time.time() - _TRANS_TTL
                stale = [k for k, (ts, _) in list(_translation_cache.items()) if ts < _cutoff]
                for k in (stale or list(_translation_cache.keys())[:200]):
                    _translation_cache.pop(k, None)
            return translated
    except Exception as e:
        log.debug(f"translate_question: {e}")

    return question


async def translate_market(market: dict) -> dict:
    """
    Возвращает копию market с переведённым полем 'question'.
    """
    q = market.get("question", "")
    translated = await translate_question(q)
    if translated != q:
        market = dict(market)
        market["question"] = translated
        market["question_original"] = q
    return market


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

    async def get_market_by_id(self, condition_id: str,
                               force_refresh: bool = False) -> Optional[dict]:
        if not force_refresh:
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

    async def analyze_market(self, market: dict, force_refresh: bool = False) -> dict:
        """Глубокий AI-анализ через Groq с кэшированием и fallback на rule-based."""
        condition_id = market.get("id", "")

        # Возвращаем кэшированный анализ если не устарел
        if not force_refresh and condition_id:
            cached = _ai_cache.get(condition_id)
            if cached and time.time() - cached[0] < _AI_CACHE_TTL:
                log.debug(f"AI cache hit: {condition_id[:20]}")
                return cached[1]

        yes_price, no_price = _parse_prices(market)
        volume_24h = float(market.get("volume24hr", 0) or 0)
        liquidity  = float(market.get("liquidityNum", 0) or 0)
        end_date   = (market.get("endDate") or "неизвестно")[:10]

        # Передаём описание и категорию для более глубокого анализа
        description = str(market.get("description") or "").strip()
        category    = str(market.get("category")    or "").strip()

        data = {
            "question":    market.get("question", ""),
            "yes_price":   yes_price,
            "no_price":    no_price,
            "volume_24h":  volume_24h,
            "liquidity":   liquidity,
            "end_date":    end_date,
            "description": description,
            "category":    category,
        }

        try:
            from groq_analyzer import analyze_with_groq
            ai = await analyze_with_groq(data)
        except Exception as e:
            log.warning(f"Groq fallback: {e}")
            ai = self._rule_based_analysis(data)

        result = {**data, **ai}

        # Сохраняем в кэш (очищаем старые записи раз в 100 запросов)
        if condition_id:
            _ai_cache[condition_id] = (time.time(), result)
            if len(_ai_cache) % 100 == 0:
                _now = time.time()
                expired = [k for k, (ts, _) in list(_ai_cache.items())
                           if _now - ts >= _AI_CACHE_TTL]
                for k in expired:
                    _ai_cache.pop(k, None)

        return result

    @staticmethod
    def _rule_based_analysis(data: dict) -> dict:
        y = data["yes_price"]
        v = data["volume_24h"]
        if y < 0.35 and v > 50_000:
            return {"recommendation": "BUY NO",  "confidence": "HIGH",
                    "reasoning": f"YES торгуется по {y:.0%}, хотя объём ${v:,.0f} за 24ч не подтверждает такую высокую вероятность — рынок перекуплен по YES, выгоднее ставить NO.",
                    "edge": f"~{(0.35 - y) * 100:.0f}%", "risk": "MEDIUM"}
        if y > 0.65 and v > 50_000:
            return {"recommendation": "BUY YES", "confidence": "HIGH",
                    "reasoning": f"YES торгуется по {y:.0%} с объёмом ${v:,.0f} за 24ч — устойчивый спрос на YES подтверждает тренд, NO переоценён.",
                    "edge": f"~{(y - 0.65) * 100:.0f}%", "risk": "MEDIUM"}
        return     {"recommendation": "SKIP",    "confidence": "LOW",
                    "reasoning": f"YES {y:.0%} / NO {1-y:.0%} — рынок сбалансирован, явного ценового перекоса нет, входить невыгодно.",
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
        """Размещает рыночный ордер от имени бота (admin-кошелёк)."""
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

    async def place_bet_for_user(
        self, private_key: str, token_id: str, amount_usdc: float
    ) -> dict:
        """
        Размещает рыночный ордер от имени пользователя.
        private_key — расшифрованный приватный ключ (0x...).
        Ключ используется только внутри to_thread и не логируется.
        """
        def _fn():
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            # Создаём клиент с ключом пользователя
            c = ClobClient(
                host=CLOB_BASE,
                chain_id=CHAIN_ID,
                key=private_key,
                signature_type=0,
            )
            # Деривируем или создаём API credentials из приватного ключа
            try:
                c.set_api_creds(c.create_or_derive_api_creds())
            except Exception as e:
                raise RuntimeError(f"Не удалось создать API credentials: {e}")
            order = c.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount_usdc)
            )
            return c.post_order(order, OrderType.FOK)

        return await asyncio.to_thread(_fn)
