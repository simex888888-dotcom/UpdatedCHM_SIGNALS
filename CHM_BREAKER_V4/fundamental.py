"""
fundamental.py — Фундаментальный контекст рынка для сигналов.

Источники (бесплатные, без авторизации):
  • alternative.me/fng  — Fear & Greed Index
  • api.coingecko.com   — BTC доминанс, общая капитализация
  • (fallback)          — если API недоступен, возвращается кэш или пустая строка

Данные обновляются раз в 10 минут (TTL).
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger("CHM.Fundamental")

_CACHE_TTL = 600          # 10 минут
_TIMEOUT   = aiohttp.ClientTimeout(total=8)

_FNG_URL    = "https://api.alternative.me/fng/?limit=1"
_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

# ── Кэш ───────────────────────────────────────────────────────────────────────
_fng_cache:    Optional[dict] = None
_fng_updated:  float = 0.0

_global_cache:   Optional[dict] = None
_global_updated: float = 0.0

_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_TIMEOUT)
    return _session


# ── Fear & Greed ───────────────────────────────────────────────────────────────

async def get_fear_greed() -> dict:
    """
    Возвращает {"value": int, "class": str}
    Пример: {"value": 42, "class": "Fear"}
    """
    global _fng_cache, _fng_updated
    if _fng_cache and time.time() - _fng_updated < _CACHE_TTL:
        return _fng_cache
    try:
        s = _get_session()
        async with s.get(_FNG_URL) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                entry = data["data"][0]
                _fng_cache = {
                    "value": int(entry["value"]),
                    "class": entry["value_classification"],
                }
                _fng_updated = time.time()
                return _fng_cache
    except Exception as e:
        log.debug(f"Fear&Greed: {e}")
    return _fng_cache or {"value": 50, "class": "Neutral"}


# ── CoinGecko Global ───────────────────────────────────────────────────────────

async def get_global_data() -> dict:
    """
    Возвращает {"btc_dominance": float, "total_mcap_usd": float}
    Пример: {"btc_dominance": 54.3, "total_mcap_usd": 2.4e12}
    """
    global _global_cache, _global_updated
    if _global_cache and time.time() - _global_updated < _CACHE_TTL:
        return _global_cache
    try:
        s = _get_session()
        async with s.get(_GLOBAL_URL) as r:
            if r.status == 200:
                data = (await r.json(content_type=None)).get("data", {})
                _global_cache = {
                    "btc_dominance": round(data.get("btc_dominance", 0), 1),
                    "total_mcap_usd": data.get("total_market_cap", {}).get("usd", 0),
                }
                _global_updated = time.time()
                return _global_cache
    except Exception as e:
        log.debug(f"CoinGecko global: {e}")
    return _global_cache or {"btc_dominance": 0.0, "total_mcap_usd": 0.0}


# ── Форматирование ─────────────────────────────────────────────────────────────

def _fng_emoji(value: int) -> str:
    if value <= 25:  return "😱"
    if value <= 45:  return "😨"
    if value <= 55:  return "😐"
    if value <= 75:  return "😏"
    return "🤑"


def _fng_ru(cls: str) -> str:
    return {
        "Extreme Fear":  "Экстремальный страх",
        "Fear":          "Страх",
        "Neutral":       "Нейтрально",
        "Greed":         "Жадность",
        "Extreme Greed": "Экстремальная жадность",
    }.get(cls, cls)


def _mcap_str(v: float) -> str:
    if v >= 1e12:   return f"${v/1e12:.2f}T"
    if v >= 1e9:    return f"${v/1e9:.1f}B"
    return f"${v/1e6:.0f}M"


async def get_market_context_line() -> str:
    """
    Возвращает одну строку с ключевыми фундаментальными данными.
    Пример:
      🌡 Индекс страха: 42 (Страх 😨) | BTC доминанс: 54.3% | Капа: $2.40T
    Если оба запроса упали — возвращает пустую строку.
    """
    fng_task    = asyncio.create_task(get_fear_greed())
    global_task = asyncio.create_task(get_global_data())
    fng, glb = await asyncio.gather(fng_task, global_task)

    parts = []

    if fng.get("value"):
        em  = _fng_emoji(fng["value"])
        rus = _fng_ru(fng["class"])
        parts.append(f"🌡 Страх/Жадность: <b>{fng['value']}</b> ({rus} {em})")

    if glb.get("btc_dominance"):
        parts.append(f"₿ BTC доминанс: <b>{glb['btc_dominance']}%</b>")

    if glb.get("total_mcap_usd"):
        parts.append(f"💹 Крипторынок: <b>{_mcap_str(glb['total_mcap_usd'])}</b>")

    return " | ".join(parts)


async def get_market_context_block() -> str:
    """
    Возвращает блок текста для вставки в сигнал (с переносами строк).
    """
    fng_task    = asyncio.create_task(get_fear_greed())
    global_task = asyncio.create_task(get_global_data())
    fng, glb = await asyncio.gather(fng_task, global_task)

    lines = []

    if fng.get("value"):
        em  = _fng_emoji(fng["value"])
        rus = _fng_ru(fng["class"])
        # Добавляем контекстное пояснение для трейдера
        if fng["value"] <= 25:
            hint = "— рынок в панике, возможен разворот вверх"
        elif fng["value"] <= 45:
            hint = "— доминирует страх, осторожно с лонгами"
        elif fng["value"] <= 55:
            hint = "— рынок нейтрален"
        elif fng["value"] <= 75:
            hint = "— растущий оптимизм, следи за перегревом"
        else:
            hint = "— рынок перегрет, осторожно с лонгами"
        lines.append(f"🌡 Страх/Жадность: <b>{fng['value']}</b> ({rus} {em}) <i>{hint}</i>")

    if glb.get("btc_dominance"):
        dom = glb["btc_dominance"]
        if dom >= 58:
            dom_hint = "высокий — альты слабее BTC"
        elif dom >= 50:
            dom_hint = "умеренный"
        else:
            dom_hint = "низкий — альт-сезон возможен"
        lines.append(f"₿ BTC доминанс: <b>{dom}%</b> ({dom_hint})")

    if glb.get("total_mcap_usd"):
        lines.append(f"💹 Общая капитализация: <b>{_mcap_str(glb['total_mcap_usd'])}</b>")

    return "\n".join(lines)
