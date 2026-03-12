"""
cache_gc.py — периодическая очистка in-memory кэшей.

Все unbounded dict'ы в боте никогда не чистятся — только проверяются.
После нескольких дней работы они накапливают тысячи старых записей.
Этот модуль запускает фоновую задачу, которая каждый час удаляет устаревшие
записи из всех кэшей.

Кэши под управлением:
  smc.scanner._sent_signals          — дедупликация сигналов (TTL 2ч)
  pump_dump.signal_aggregator._last_signal_ts — антиспам (TTL 15 мин)
  handlers._analyze_cooldown         — кулдаун /analyze (TTL 10с)
  poly_handlers._bet_ts              — кулдаун ставок (TTL 3с)
  polymarket_service._market_cache   — кэш маркетов (TTL 15 мин)
  polymarket_service._short_keys / _cid_to_short — ключи callback (лимит 2000)
"""

import asyncio
import logging
import time

log = logging.getLogger("CHM.CacheGC")

GC_INTERVAL = 3600       # запускать раз в час
POLY_KEYS_MAX = 2000     # максимум записей в _short_keys / _cid_to_short


def _cleanup_once() -> dict[str, int]:
    """
    Выполняет одноразовую очистку всех кэшей.
    Возвращает словарь {имя_кэша: удалено_записей} для логирования.
    """
    now = time.time()
    freed: dict[str, int] = {}

    # ── smc.scanner._sent_signals (TTL 2ч) ───────────────────────────────────
    try:
        from smc.scanner import _sent_signals, _DEDUP_HOURS
        cutoff = now - _DEDUP_HOURS * 3600
        stale = [k for k, ts in list(_sent_signals.items()) if ts < cutoff]
        for k in stale:
            _sent_signals.pop(k, None)
        if stale:
            freed["smc._sent_signals"] = len(stale)
    except Exception as e:
        log.debug(f"GC smc._sent_signals: {e}")

    # ── signal_aggregator._last_signal_ts (TTL ANTI_SPAM_MINUTES) ────────────
    try:
        from pump_dump.signal_aggregator import _last_signal_ts
        from pump_dump.pd_config import ANTI_SPAM_MINUTES
        cutoff = now - ANTI_SPAM_MINUTES * 60
        stale = [k for k, ts in list(_last_signal_ts.items()) if ts < cutoff]
        for k in stale:
            _last_signal_ts.pop(k, None)
        if stale:
            freed["pd._last_signal_ts"] = len(stale)
    except Exception as e:
        log.debug(f"GC pd._last_signal_ts: {e}")

    # ── handlers._analyze_cooldown (TTL 10с — чистим старше 60с) ─────────────
    try:
        from handlers import _analyze_cooldown
        stale = [k for k, ts in list(_analyze_cooldown.items()) if now - ts > 60]
        for k in stale:
            _analyze_cooldown.pop(k, None)
        if stale:
            freed["handlers._analyze_cooldown"] = len(stale)
    except Exception as e:
        log.debug(f"GC handlers._analyze_cooldown: {e}")

    # ── poly_handlers._bet_ts (TTL 3с — чистим старше 60с) ───────────────────
    try:
        from poly_handlers import _bet_ts
        stale = [k for k, ts in list(_bet_ts.items()) if now - ts > 60]
        for k in stale:
            _bet_ts.pop(k, None)
        if stale:
            freed["poly_handlers._bet_ts"] = len(stale)
    except Exception as e:
        log.debug(f"GC poly_handlers._bet_ts: {e}")

    # ── polymarket_service._market_cache (TTL 15 мин) ────────────────────────
    try:
        from polymarket_service import _market_cache, _CACHE_TTL
        stale = [k for k, (ts, _) in list(_market_cache.items()) if now - ts > _CACHE_TTL]
        for k in stale:
            _market_cache.pop(k, None)
        if stale:
            freed["poly._market_cache"] = len(stale)
    except Exception as e:
        log.debug(f"GC poly._market_cache: {e}")

    # ── polymarket_service._short_keys / _cid_to_short (лимит 2000) ──────────
    # Эти словари растут бесконечно: каждый уникальный маркет добавляет запись.
    # Ограничиваем: оставляем только последние POLY_KEYS_MAX записей
    # (с наибольшими ключами — они соответствуют самым свежим маркетам).
    try:
        import polymarket_service as _ps
        sk = _ps._short_keys
        cid = _ps._cid_to_short
        if len(sk) > POLY_KEYS_MAX:
            keep_from = max(sk) - POLY_KEYS_MAX + 1
            old_keys = [k for k in list(sk) if k < keep_from]
            for k in old_keys:
                cid_val = sk.pop(k, None)
                if cid_val is not None:
                    cid.pop(cid_val, None)
            if old_keys:
                freed["poly._short_keys"] = len(old_keys)
    except Exception as e:
        log.debug(f"GC poly._short_keys: {e}")

    return freed


async def gc_loop():
    """Фоновая задача: чистит кэши каждые GC_INTERVAL секунд."""
    log.info(f"🧹 Cache GC запущен (интервал {GC_INTERVAL}с)")
    await asyncio.sleep(300)   # первый запуск через 5 минут после старта
    while True:
        try:
            freed = _cleanup_once()
            if freed:
                details = ", ".join(f"{k}={v}" for k, v in freed.items())
                log.info(f"🧹 Cache GC: удалено записей — {details}")
            else:
                log.debug("🧹 Cache GC: нечего удалять")
        except Exception as e:
            log.warning(f"Cache GC error: {e}")
        await asyncio.sleep(GC_INTERVAL)
