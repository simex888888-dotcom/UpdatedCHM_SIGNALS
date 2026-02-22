"""
scanner_mid.py — сканер для 50-500 пользователей

АРХИТЕКТУРА (без Redis и PostgreSQL):
──────────────────────────────────────────────────────────
  SQLite      — хранит пользователей и сделки
  RAM кэш     — свечи в памяти с TTL, переиспользуются
  Группировка — пользователи → по таймфреймам
               один запрос к OKX = данные для всех на этом TF
  Семафор     — ограничивает параллельность API запросов
  Воркеры     — 6 asyncio задач обрабатывают очередь
──────────────────────────────────────────────────────────
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import cache
import database as db
from config import Config
from user_manager import UserManager, UserSettings
from fetcher import OKXFetcher
from indicator import CHMIndicator, SignalResult

log = logging.getLogger("CHM.Scanner")


# ── Изолированный конфиг (нет мутации глобального) ──

@dataclass
class IndConfig:
    TIMEFRAME:          str
    PIVOT_STRENGTH:     int
    ATR_PERIOD:         int
    ATR_MULT:           float
    MAX_RISK_PCT:       float
    EMA_FAST:           int
    EMA_SLOW:           int
    RSI_PERIOD:         int
    RSI_OB:             int
    RSI_OS:             int
    VOL_MULT:           float
    VOL_LEN:            int
    MAX_LEVEL_AGE:      int
    MAX_RETEST_BARS:    int
    COOLDOWN_BARS:      int
    ZONE_BUFFER:        float
    TP1_RR:             float
    TP2_RR:             float
    TP3_RR:             float
    HTF_EMA_PERIOD:     int   = 50
    HTF_TIMEFRAME:      str   = "1d"
    USE_RSI_FILTER:     bool  = True
    USE_VOLUME_FILTER:  bool  = True
    USE_PATTERN_FILTER: bool  = False
    USE_HTF_FILTER:     bool  = False


def _make_cfg(u: UserSettings) -> IndConfig:
    return IndConfig(
        TIMEFRAME=u.timeframe, PIVOT_STRENGTH=u.pivot_strength,
        ATR_PERIOD=u.atr_period, ATR_MULT=u.atr_mult,
        MAX_RISK_PCT=u.max_risk_pct, EMA_FAST=u.ema_fast, EMA_SLOW=u.ema_slow,
        RSI_PERIOD=u.rsi_period, RSI_OB=u.rsi_ob, RSI_OS=u.rsi_os,
        VOL_MULT=u.vol_mult, VOL_LEN=u.vol_len,
        MAX_LEVEL_AGE=u.max_level_age, MAX_RETEST_BARS=u.max_retest_bars,
        COOLDOWN_BARS=u.cooldown_bars, ZONE_BUFFER=u.zone_buffer,
        TP1_RR=u.tp1_rr, TP2_RR=u.tp2_rr, TP3_RR=u.tp3_rr,
        HTF_EMA_PERIOD=u.htf_ema_period,
        USE_RSI_FILTER=u.use_rsi, USE_VOLUME_FILTER=u.use_volume,
        USE_PATTERN_FILTER=u.use_pattern, USE_HTF_FILTER=u.use_htf,
    )


# ── Telegram ─────────────────────────────────────────

def result_keyboard(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 TP1", callback_data=f"res_TP1_{trade_id}"),
            InlineKeyboardButton(text="🎯 TP2", callback_data=f"res_TP2_{trade_id}"),
            InlineKeyboardButton(text="🏆 TP3", callback_data=f"res_TP3_{trade_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ SL",       callback_data=f"res_SL_{trade_id}"),
            InlineKeyboardButton(text="⏭ Пропустил", callback_data=f"res_SKIP_{trade_id}"),
        ],
    ])


def signal_text(sig: SignalResult, user: UserSettings) -> str:
    stars  = "⭐" * sig.quality + "☆" * (5 - sig.quality)
    header = "🟢 <b>LONG СИГНАЛ</b>" if sig.direction == "LONG" else "🔴 <b>SHORT СИГНАЛ</b>"
    emoji  = "📈" if sig.direction == "LONG" else "📉"
    risk   = abs(sig.entry - sig.sl)
    sign   = 1 if sig.direction == "LONG" else -1
    tp1    = sig.entry + sign * risk * user.tp1_rr
    tp2    = sig.entry + sign * risk * user.tp2_rr
    tp3    = sig.entry + sign * risk * user.tp3_rr

    def pct(t): return abs((t - sig.entry) / sig.entry * 100)

    return "\n".join([
        header, "",
        f"💎 <b>{sig.symbol}</b>  {emoji}  {sig.breakout_type}",
        f"⭐ Качество: {stars}", "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💰 Вход:    <code>{sig.entry:.6g}</code>",
        f"🛑 Стоп:    <code>{sig.sl:.6g}</code>  <i>(-{sig.risk_pct:.2f}%)</i>", "",
        f"🎯 Цель 1: <code>{tp1:.6g}</code>  <i>(+{pct(tp1):.2f}%)</i>",
        f"🎯 Цель 2: <code>{tp2:.6g}</code>  <i>(+{pct(tp2):.2f}%)</i>",
        f"🏆 Цель 3: <code>{tp3:.6g}</code>  <i>(+{pct(tp3):.2f}%)</i>",
        "━━━━━━━━━━━━━━━━━━━━", "",
        f"📊 {sig.trend_local}  |  RSI: <code>{sig.rsi:.1f}</code>  |  Vol: <code>x{sig.volume_ratio:.1f}</code>",
        f"🕯 Паттерн: {sig.pattern}", "",
        "⚡ <i>CHM Laboratory — CHM BREAKER</i>", "",
        "👇 <i>Отметь результат когда сделка закроется:</i>",
    ])


# ── Основной сканер ──────────────────────────────────

class MidScanner:

    def __init__(self, config: Config, bot: Bot, um: UserManager):
        self.cfg     = config
        self.bot     = bot
        self.um      = um
        self.fetcher = OKXFetcher()

        self._indicators:  dict[int, CHMIndicator] = {}
        self._ind_configs: dict[int, IndConfig]    = {}
        self._last_scan:   dict[int, float]        = {}
        self._api_sem = asyncio.Semaphore(config.API_CONCURRENCY)

        self._queue: asyncio.Queue = asyncio.Queue()

        self._perf = {
            "cycles": 0, "users": 0,
            "signals": 0, "api_calls": 0,
        }

    # ── Индикатор ────────────────────────────────────

    def _indicator(self, user: UserSettings) -> CHMIndicator:
        ic = _make_cfg(user)
        if self._ind_configs.get(user.user_id) != ic:
            self._indicators[user.user_id]  = CHMIndicator(ic)
            self._ind_configs[user.user_id] = ic
        return self._indicators[user.user_id]

    # ── Монеты ───────────────────────────────────────

    async def _load_coins(self, min_vol: float) -> list:
        cached = await cache.get_coins()
        if cached:
            return cached
        log.info("📋 Загружаю список монет...")
        coins = await self.fetcher.get_all_usdt_pairs(
            min_volume_usdt=min_vol,
            blacklist=self.cfg.AUTO_BLACKLIST,
        )
        if coins:
            await cache.set_coins(coins)
            log.info(f"   Монет: {len(coins)}")
        return coins or []

    # ── Свечи (кэш → OKX) ────────────────────────────

    async def _fetch(self, symbol: str, tf: str):
        df = await cache.get_candles(symbol, tf)
        if df is not None:
            return df

        async with self._api_sem:
            # Двойная проверка после семафора
            df = await cache.get_candles(symbol, tf)
            if df is not None:
                return df

            self._perf["api_calls"] += 1
            df = await self.fetcher.get_candles(symbol, tf, limit=300)
            if df is not None:
                await cache.set_candles(symbol, tf, df, self.cfg.CACHE_TTL)
            return df

    # ── Загрузка всех свечей для TF-группы ───────────

    async def _load_tf_candles(self, tf: str, coins: list) -> dict:
        """
        Загружает свечи для всех монет на данном TF параллельно.
        Результат кэшируется — следующие пользователи на том же TF
        берут данные из памяти без новых запросов к OKX.
        """
        result   = {}
        chunk_sz = self.cfg.CHUNK_SIZE

        for i in range(0, len(coins), chunk_sz):
            batch = coins[i: i + chunk_sz]
            dfs   = await asyncio.gather(
                *[self._fetch(s, tf) for s in batch],
                return_exceptions=True,
            )
            for sym, df in zip(batch, dfs):
                if isinstance(df, Exception) or df is None or len(df) < 60:
                    continue
                result[sym] = df
            await asyncio.sleep(self.cfg.CHUNK_SLEEP)

        return result

    # ── Анализ одного пользователя ────────────────────

    async def _scan_user(self, user: UserSettings, candles: dict):
        ind     = self._indicator(user)
        signals = 0
        for sym, df in candles.items():
            df_htf = await self._fetch(sym, "1D") if user.use_htf else None
            try:
                sig = ind.analyze(sym, df, df_htf)
            except Exception as e:
                log.debug(f"{sym}: {e}")
                continue
            if sig is None or sig.quality < user.min_quality:
                continue
            if user.notify_signal:
                await self._send(user, sig)
            signals += 1
        self._perf["users"] += 1
        return signals

    # ── Отправка сигнала ──────────────────────────────

    async def _send(self, user: UserSettings, sig: SignalResult):
        trade_id = f"{user.user_id}_{int(time.time() * 1000)}"
        risk     = abs(sig.entry - sig.sl)
        sign     = 1 if sig.direction == "LONG" else -1
        await db.db_add_trade({
            "trade_id":     trade_id,
            "user_id":      user.user_id,
            "symbol":       sig.symbol,
            "direction":    sig.direction,
            "entry":        sig.entry,
            "sl":           sig.sl,
            "tp1":          sig.entry + sign * risk * user.tp1_rr,
            "tp2":          sig.entry + sign * risk * user.tp2_rr,
            "tp3":          sig.entry + sign * risk * user.tp3_rr,
            "tp1_rr":       user.tp1_rr,
            "tp2_rr":       user.tp2_rr,
            "tp3_rr":       user.tp3_rr,
            "quality":      sig.quality,
            "timeframe":    user.timeframe,
            "breakout_type":sig.breakout_type,
            "created_at":   time.time(),
        })
        try:
            await self.bot.send_message(
                user.user_id,
                signal_text(sig, user),
                parse_mode="HTML",
                reply_markup=result_keyboard(trade_id),
            )
            user.signals_received += 1
            await self.um.save(user)
            self._perf["signals"] += 1
            log.info(f"✅ {sig.symbol} {sig.direction} ⭐{sig.quality} → @{user.username or user.user_id}")
        except TelegramForbiddenError:
            user.active = False
            await self.um.save(user)
        except Exception as e:
            log.error(f"Ошибка отправки {user.user_id}: {e}")

    # ── Воркер ───────────────────────────────────────

    async def _worker(self, wid: int, candles_by_tf: dict):
        while True:
            try:
                user: UserSettings = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                break
            try:
                candles = candles_by_tf.get(user.timeframe, {})
                await self._scan_user(user, candles)
            except Exception as e:
                log.error(f"Воркер {wid} ошибка {user.user_id}: {e}")
            finally:
                self._queue.task_done()

    # ── Уведомление об истечении доступа ──────────────

    async def _notify_expired(self, user: UserSettings):
        try:
            was_trial   = user.sub_status == "trial"
            user.active = False
            await self.um.save(user)
            cfg = self.cfg
            if was_trial:
                text = (
                    "⏰ <b>Пробный период завершён!</b>\n\n"
                    f"📅 30 дней  — <b>{cfg.PRICE_30_DAYS}</b>\n"
                    f"📅 90 дней  — <b>{cfg.PRICE_90_DAYS}</b>\n\n"
                    f"💳 {cfg.PAYMENT_INFO}"
                )
            else:
                text = (
                    "⏰ <b>Подписка истекла!</b>\n\n"
                    f"📅 30 дней  — <b>{cfg.PRICE_30_DAYS}</b>\n"
                    f"💳 {cfg.PAYMENT_INFO}"
                )
            await self.bot.send_message(user.user_id, text, parse_mode="HTML")
        except Exception:
            pass

    # ── Главный цикл ──────────────────────────────────

    async def _cycle(self):
        start = time.time()
        users = await self.um.get_active_users()
        if not users:
            return

        now = time.time()
        due = [u for u in users
               if now - self._last_scan.get(u.user_id, 0) >= u.scan_interval]
        if not due:
            return

        log.info(f"🔍 Цикл #{self._perf['cycles']+1}: {len(due)}/{len(users)} пользователей")

        # Группируем по TF
        tf_groups: dict[str, list[UserSettings]] = defaultdict(list)
        for u in due:
            tf_groups[u.timeframe].append(u)

        min_vol = min(u.min_volume_usdt for u in due)
        coins   = await self._load_coins(min_vol)

        # Загружаем свечи для каждого TF один раз
        candles_by_tf: dict[str, dict] = {}
        for tf, tf_users in tf_groups.items():
            log.info(f"  📥 TF={tf}: {len(coins)} монет для {len(tf_users)} юзеров")
            candles_by_tf[tf] = await self._load_tf_candles(tf, coins)
            log.info(f"     Загружено: {len(candles_by_tf[tf])} монет из кэша/OKX")

        # Проверяем истёкшие подписки
        for u in due:
            has, reason = u.check_access()
            if not has:
                await self._notify_expired(u)
                continue
            self._last_scan[u.user_id] = now
            await self._queue.put(u)

        # Запускаем воркеров
        n = min(self.cfg.SCAN_WORKERS, self._queue.qsize())
        if n == 0:
            return

        workers = [
            asyncio.create_task(self._worker(i, candles_by_tf))
            for i in range(n)
        ]
        await self._queue.join()
        for w in workers:
            w.cancel()

        elapsed = time.time() - start
        cs      = cache.cache_stats()
        self._perf["cycles"] += 1
        log.info(
            f"  ✅ {elapsed:.1f}с | Сигналов: {self._perf['signals']} | "
            f"API: {self._perf['api_calls']} | "
            f"Кэш: {cs.get('size', 0)} ключей, хит {cs.get('ratio', 0)}%"
        )

    async def run_forever(self):
        log.info(
            f"🚀 MidScanner запущен | "
            f"Воркеров: {self.cfg.SCAN_WORKERS} | "
            f"API: {self.cfg.API_CONCURRENCY} concurrent"
        )
        while True:
            try:
                await self._cycle()
            except Exception as e:
                log.error(f"Ошибка цикла: {e}", exc_info=True)
            await asyncio.sleep(self.cfg.SCAN_LOOP_SLEEP)

    def get_perf(self) -> dict:
        cs = cache.cache_stats()
        return {**self._perf, "cache": cs}
