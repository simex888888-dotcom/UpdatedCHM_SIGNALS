"""
gerchik_runner.py — Фоновый сканер стратегии Герчика для Telegram-бота.

Запускается как отдельная asyncio-задача рядом с MidScanner.
Раз в SCAN_INTERVAL секунд проверяет всех пользователей у которых
gerchik_active=True, сканирует монеты и отправляет сигналы.

Паттерн входа: БСУ → БПУ-1 → БПУ-2 у уровня поддержки / сопротивления.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import cache
import database as db
from fetcher import OKXFetcher
from user_manager import UserManager, UserSettings
from gerchik_strategy import GerchikStrategy, GerchikConfig, Level

log = logging.getLogger("CHM.Gerchik")

# Интервал между циклами сканирования (секунды)
SCAN_INTERVAL   = 1800   # 30 минут
# Таймфрейм для поиска уровней
SCAN_TIMEFRAME  = "1d"
# Минимальный суточный объём монеты
MIN_VOL_USDT    = 5_000_000
# Минимальная сила уровня для входа
MIN_LEVEL_STR   = 2
# Максимум сигналов на пользователя за один цикл (защита от флуда)
MAX_SIGNALS_PER_CYCLE = 3


def _tv_url(symbol: str) -> str:
    """Ссылка TradingView для OKX-символа. BTC-USDT-SWAP → OKX:BTCUSDT.P"""
    clean = symbol.replace("-SWAP", "").replace("-", "")
    return "https://www.tradingview.com/chart/?symbol=OKX:" + clean + ".P"


def _fmt_price(v: float) -> str:
    """Форматирует цену убирая лишние нули."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v <= 0:      return "0"
    if v >= 10_000: return f"{v:,.0f}"
    if v >= 100:    return f"{v:,.1f}"
    if v >= 1:      return f"{v:.4f}".rstrip("0").rstrip(".")
    decimals = -math.floor(math.log10(v)) + 3
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")


def _signal_text(
    symbol:    str,
    direction: str,
    entry:     float,
    sl:        float,
    tp1:       float,
    tp2:       float,
    level:     Level,
    rr:        float,
) -> str:
    """Форматирует текст Telegram-сигнала."""
    dir_header   = "🟢 <b>ЛОНГ — ГЕРЧИК</b>" if direction == "LONG" else "🔴 <b>ШОРТ — ГЕРЧИК</b>"
    dir_emoji    = "📈" if direction == "LONG" else "📉"
    lvl_type     = "поддержка" if level.level_type == "support" else "сопротивление"
    stars        = "⭐" * min(level.strength, 6)
    mirror_tag   = "  🪞 <i>зеркальный</i>" if level.is_mirror else ""
    risk_pct     = abs(entry - sl) / entry * 100 if entry else 0
    tp1_pct      = abs(tp1 - entry) / entry * 100
    tp2_pct      = abs(tp2 - entry) / entry * 100

    NL = "\n"
    return (
        dir_header + NL + NL +
        f"💎 <b>{symbol.replace('-USDT-SWAP','').replace('-USDT','')}</b>"
        f"  {dir_emoji}  {lvl_type.upper()}{mirror_tag}" + NL +
        f"💪 Сила уровня: {stars} ({level.strength}/6)" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        f"💰 Вход:       <code>{_fmt_price(entry)}</code>" + NL +
        f"🛑 Стоп:       <code>{_fmt_price(sl)}</code>"
        f"  <i>(-{risk_pct:.2f}%)</i>" + NL + NL +
        f"🎯 TP1 (3R):  <code>{_fmt_price(tp1)}</code>"
        f"  <i>(+{tp1_pct:.2f}%  55% позиции)</i>" + NL +
        f"🏆 TP2 (4R):  <code>{_fmt_price(tp2)}</code>"
        f"  <i>(+{tp2_pct:.2f}%  45% позиции)</i>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        f"📐 R:R = 1:<b>{rr:.1f}</b>  |  После TP1 — стоп в безубыток" + NL + NL +
        "⚡ <i>CHM Laboratory — Стратегия Герчика</i>" + NL + NL +
        "👇 <i>Отметь результат когда сделка закроется:</i>"
    )


def _signal_kb(trade_id: str, symbol: str) -> InlineKeyboardMarkup:
    """Клавиатура под сигналом Герчика."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 График",     url=_tv_url(symbol)),
            InlineKeyboardButton(text="📊 Статистика", callback_data="my_stats"),
        ],
        [
            InlineKeyboardButton(
                text="📋 Записать результат ▾",
                callback_data="sig_records_" + trade_id,
            ),
        ],
    ])


class GerchikScanner:
    """
    Фоновый сканер стратегии Герчика.

    Каждые SCAN_INTERVAL секунд:
      1. Загружает монеты (кэш или OKX)
      2. Загружает дневные свечи
      3. Для каждого активного пользователя запускает GerchikStrategy
      4. Найденные сигналы отправляет в Telegram
    """

    def __init__(self, bot: Bot, um: UserManager, fetcher: Optional[OKXFetcher] = None):
        self._bot     = bot
        self._um      = um
        self._fetcher = fetcher or OKXFetcher()
        self._strat   = GerchikStrategy(config=GerchikConfig())
        self._api_sem = asyncio.Semaphore(4)

        # Антиспам: (user_id, symbol) → timestamp последнего сигнала
        self._sent: dict[tuple, float] = {}
        self._COOLDOWN = 4 * 3600   # 4 часа — не повторяем сигнал по той же монете

    # ── Загрузка свечей ────────────────────────────────────────────────────

    async def _fetch(self, symbol: str, tf: str):
        df = await cache.get_candles(symbol, tf)
        if df is not None:
            return df
        async with self._api_sem:
            df = await cache.get_candles(symbol, tf)
            if df is not None:
                return df
            df = await self._fetcher.get_candles(symbol, tf, limit=300)
            if df is not None:
                await cache.set_candles(symbol, tf, df, ttl=900)
            return df

    # ── Анализ одной монеты (последний бар) ────────────────────────────────

    def _check_signal(self, symbol: str, df) -> Optional[dict]:
        """
        Проверяет последний бар на наличие паттерна Герчика.

        Использует методы GerchikStrategy напрямую (не backtest):
          find_levels → cluster_levels → determine_trend →
          find_bsu_bpu_pattern → check_atr_filter → рассчитывает уровни
        """
        cfg   = self._strat.cfg
        strat = self._strat

        if len(df) < cfg.level_lookback + 5:
            return None

        i     = len(df) - 1    # последний (текущий) бар
        bar   = df.iloc[i]
        close = float(bar["close"])
        atr_s = strat.compute_atr(df)
        atr   = float(atr_s.iloc[i])

        # Уровни из lookback-окна (исключая последний бар, чтобы не заглядывать в будущее)
        window = df.iloc[i - cfg.level_lookback: i]
        raw    = strat.find_levels(window)
        if not raw:
            return None

        levels = strat.cluster_levels(raw, close)
        if not levels:
            return None

        strat._detect_mirror_levels(levels)
        for lvl in levels:
            lvl.strength = strat.level_strength(lvl, window, len(window) - 1)

        # Фильтр слабых уровней
        levels = [l for l in levels if l.strength >= MIN_LEVEL_STR]
        if not levels:
            return None

        trend = strat.determine_trend(close, levels)
        if trend is None:
            return None

        # Кандидатные уровни по тренду (цена должна быть рядом)
        prox  = cfg.level_touch_threshold * 12   # 2.4% — рядом с уровнем
        if trend == "LONG":
            candidates = [
                l for l in levels
                if l.level_type == "support"
                and 0 <= (close - l.price) / max(l.price, 1e-9) <= prox
            ]
        else:
            candidates = [
                l for l in levels
                if l.level_type == "resistance"
                and 0 <= (l.price - close) / max(l.price, 1e-9) <= prox
            ]

        if not candidates:
            return None

        best = max(candidates, key=lambda l: (l.strength, -abs(close - l.price)))

        # Паттерн БПУ-1 → БПУ-2 (bar_idx = последний бар)
        if not strat.find_bsu_bpu_pattern(df, best.price, best.level_type, i):
            return None

        # ATR-фильтр: не входить если уже прошли ≥75% дневного диапазона
        open_price = float(bar["open"])
        if not strat.check_atr_filter(close, open_price, atr, trend):
            return None

        # Расчёт ТВХ и уровней
        level_price = best.price
        if trend == "LONG":
            stop_dist = max(close - level_price, atr * 0.05)
            lft       = stop_dist * cfg.lft_pct
            entry     = level_price + lft
            sl        = level_price - lft
            tp1       = entry + stop_dist * cfg.tp1_rr
            tp2       = entry + stop_dist * cfg.tp2_rr
        else:
            stop_dist = max(level_price - close, atr * 0.05)
            lft       = stop_dist * cfg.lft_pct
            entry     = level_price - lft
            sl        = level_price + lft
            tp1       = entry - stop_dist * cfg.tp1_rr
            tp2       = entry - stop_dist * cfg.tp2_rr

        # Запас хода до противоположного уровня (≥ 3R)
        opp_type = "resistance" if trend == "LONG" else "support"
        opp_lvls = [
            l for l in levels
            if l.level_type == opp_type and (
                (trend == "LONG"  and l.price > entry) or
                (trend == "SHORT" and l.price < entry)
            )
        ]
        if opp_lvls:
            nearest = (min(opp_lvls, key=lambda l: l.price) if trend == "LONG"
                       else max(opp_lvls, key=lambda l: l.price))
            room = abs(nearest.price - entry)
            risk = abs(entry - sl)
            if risk > 0 and room / risk < cfg.min_rr_ratio:
                return None

        rr = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0.0

        return {
            "symbol":    symbol,
            "direction": trend,
            "entry":     round(entry, 6),
            "sl":        round(sl,    6),
            "tp1":       round(tp1,   6),
            "tp2":       round(tp2,   6),
            "level":     best,
            "rr":        round(rr, 2),
        }

    # ── Отправка сигнала пользователю ─────────────────────────────────────

    async def _send_signal(self, user: UserSettings, sig: dict):
        """Сохраняет сделку в БД и отправляет сигнал в Telegram."""
        uid      = user.user_id
        symbol   = sig["symbol"]

        # Антиспам
        key = (uid, symbol)
        if time.time() - self._sent.get(key, 0) < self._COOLDOWN:
            return
        self._sent[key] = time.time()

        # Сохраняем в trades (для статистики и авто-трейдинга)
        trade_id = f"{uid}_{int(time.time() * 1000)}"
        await db.db_add_trade({
            "trade_id":      trade_id,
            "user_id":       uid,
            "symbol":        symbol,
            "direction":     sig["direction"],
            "entry":         sig["entry"],
            "sl":            sig["sl"],
            "tp1":           sig["tp1"],
            "tp2":           sig["tp2"],
            "tp3":           sig["tp2"],   # TP3 = TP2 (Герчик использует 2 цели)
            "tp1_rr":        3.0,
            "tp2_rr":        4.0,
            "tp3_rr":        4.0,
            "quality":       sig["level"].strength,
            "timeframe":     SCAN_TIMEFRAME,
            "breakout_type": "ГЕРЧИК",
            "created_at":    time.time(),
        })

        text = _signal_text(
            symbol    = symbol,
            direction = sig["direction"],
            entry     = sig["entry"],
            sl        = sig["sl"],
            tp1       = sig["tp1"],
            tp2       = sig["tp2"],
            level     = sig["level"],
            rr        = sig["rr"],
        )

        try:
            await self._bot.send_message(
                uid,
                text,
                parse_mode      = "HTML",
                reply_markup    = _signal_kb(trade_id, symbol),
                protect_content = True,
            )
            user.signals_received += 1
            await self._um.save(user)
            sym_short = symbol.replace("-USDT-SWAP","").replace("-USDT","")
            log.info(
                f"✅ Герчик сигнал: {sym_short} {sig['direction']} "
                f"⭐{sig['level'].strength} → uid={uid}"
            )
        except TelegramForbiddenError:
            user.gerchik_active = False
            await self._um.save(user)
        except Exception as e:
            log.error(f"Ошибка отправки сигнала Герчика uid={uid}: {e}")

    # ── Один цикл сканирования ─────────────────────────────────────────────

    async def _cycle(self):
        start = time.time()

        # Пользователи с активным сканером Герчика
        all_users = await self._um.get_active_users()
        active = [u for u in all_users if getattr(u, "gerchik_active", False)]
        if not active:
            return

        log.info(f"🎯 Герчик цикл: {len(active)} активных пользователей")

        # Загружаем список монет (кэш или OKX)
        coins = await cache.get_coins()
        if not coins:
            coins = await self._fetcher.get_all_usdt_pairs(
                min_volume_usdt = MIN_VOL_USDT,
                blacklist       = [],
            )
            if coins:
                await cache.set_coins(coins)

        if not coins:
            log.warning("Герчик: монеты не загружены")
            return

        log.info(f"   Монет для сканирования: {len(coins)}")

        # Загружаем свечи пакетами
        candles: dict = {}
        chunk = 20
        for i in range(0, len(coins), chunk):
            batch = coins[i: i + chunk]
            dfs = await asyncio.gather(
                *[self._fetch(s, SCAN_TIMEFRAME) for s in batch],
                return_exceptions=True,
            )
            for sym, df in zip(batch, dfs):
                if isinstance(df, Exception) or df is None or len(df) < 60:
                    continue
                candles[sym] = df
            await asyncio.sleep(0.5)

        log.info(f"   Свечи загружены: {len(candles)} монет")

        # Сканируем
        signals_found = 0
        for sym, df in candles.items():
            try:
                sig = self._check_signal(sym, df)
            except Exception as e:
                log.debug(f"Герчик {sym}: {e}")
                continue

            if sig is None:
                continue

            signals_found += 1

            # Рассылаем всем активным пользователям
            for user in active:
                # Фильтр монеты пользователя
                watch = getattr(user, "watch_coin", "").strip().upper()
                if watch and sym.upper() != watch:
                    continue
                await self._send_signal(user, sig)

        # Очищаем старые записи антиспама
        now = time.time()
        self._sent = {
            k: ts for k, ts in self._sent.items()
            if now - ts < self._COOLDOWN
        }

        elapsed = time.time() - start
        log.info(
            f"   ✅ Герчик цикл завершён: {signals_found} сигналов за {elapsed:.1f}с"
        )

    # ── Вечный цикл ───────────────────────────────────────────────────────

    async def run_forever(self):
        """Запускает сканер в бесконечном цикле."""
        log.info(
            f"🚀 GerchikScanner запущен | "
            f"TF={SCAN_TIMEFRAME} | "
            f"Интервал={SCAN_INTERVAL//60} мин | "
            f"Мин. сила уровня={MIN_LEVEL_STR}"
        )
        # Первый запуск с задержкой, чтобы бот успел полностью инициализироваться
        await asyncio.sleep(120)
        while True:
            try:
                await self._cycle()
            except Exception as e:
                log.error(f"GerchikScanner цикл ошибка: {e}", exc_info=True)
            await asyncio.sleep(SCAN_INTERVAL)
