"""
scanner_mid.py — мультисканнинг для 50-500 пользователей

МУЛЬТИСКАННИНГ:
  Каждый пользователь может иметь одновременно активными:
    • ЛОНГ сканер — своя TF, интервал, настройки
    • ШОРТ сканер — своя TF, интервал, настройки
    • ОБА — общие настройки (режим совместимости)

  Сканер создаёт ScanJob на каждую активную комбинацию (user, direction).
  Группировка по TF сохраняется — одни свечи для всех.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import cache
import database as db
from config import Config
from user_manager import UserManager, UserSettings, TradeCfg
from fetcher import OKXFetcher
from indicator import CHMIndicator, SignalResult

log = logging.getLogger("CHM.Scanner")

Direction = Literal["LONG", "SHORT", "BOTH"]


# ── Задание сканирования ─────────────────────────────

@dataclass
class ScanJob:
    """Один прогон сканера: пользователь + направление + конфиг."""
    user:      UserSettings
    direction: Direction     # "LONG" | "SHORT" | "BOTH"
    cfg:       TradeCfg

    @property
    def job_key(self) -> str:
        return str(self.user.user_id) + "_" + self.direction

    @property
    def tf(self) -> str:
        return self.cfg.timeframe

    @property
    def interval(self) -> int:
        return self.cfg.scan_interval


# ── IndConfig из TradeCfg ─────────────────────────────

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
    HTF_EMA_PERIOD:     int  = 50
    HTF_TIMEFRAME:      str  = "1d"
    USE_RSI_FILTER:     bool = True
    USE_VOLUME_FILTER:  bool = True
    USE_PATTERN_FILTER: bool = False
    USE_HTF_FILTER:     bool = False


def _cfg_to_ind(cfg: TradeCfg) -> IndConfig:
    return IndConfig(
        TIMEFRAME=cfg.timeframe, PIVOT_STRENGTH=cfg.pivot_strength,
        ATR_PERIOD=cfg.atr_period, ATR_MULT=cfg.atr_mult,
        MAX_RISK_PCT=cfg.max_risk_pct, EMA_FAST=cfg.ema_fast, EMA_SLOW=cfg.ema_slow,
        RSI_PERIOD=cfg.rsi_period, RSI_OB=cfg.rsi_ob, RSI_OS=cfg.rsi_os,
        VOL_MULT=cfg.vol_mult, VOL_LEN=cfg.vol_len,
        MAX_LEVEL_AGE=cfg.max_level_age, MAX_RETEST_BARS=cfg.max_retest_bars,
        COOLDOWN_BARS=cfg.cooldown_bars, ZONE_BUFFER=cfg.zone_buffer,
        TP1_RR=cfg.tp1_rr, TP2_RR=cfg.tp2_rr, TP3_RR=cfg.tp3_rr,
        HTF_EMA_PERIOD=cfg.htf_ema_period,
        USE_RSI_FILTER=cfg.use_rsi, USE_VOLUME_FILTER=cfg.use_volume,
        USE_PATTERN_FILTER=cfg.use_pattern, USE_HTF_FILTER=cfg.use_htf,
    )


# ── Telegram ─────────────────────────────────────────

def _tv_url(symbol: str) -> str:
    """Конвертирует OKX символ в ссылку TradingView.
    BTC-USDT-SWAP → https://www.tradingview.com/chart/?symbol=OKX:BTCUSDT.P
    """
    clean = symbol.replace("-SWAP", "").replace("-", "")
    return "https://www.tradingview.com/chart/?symbol=OKX:" + clean + ".P"


def signal_compact_keyboard(trade_id: str, symbol: str) -> InlineKeyboardMarkup:
    """Компактная клавиатура под сигналом: График | Статистика | Результат →"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 График",     url=_tv_url(symbol)),
            InlineKeyboardButton(text="📊 Статистика", callback_data="my_stats"),
        ],
        [
            InlineKeyboardButton(text="📋 Записать результат ▾", callback_data="sig_records_" + trade_id),
        ],
    ])


def trade_records_keyboard(trade_id: str) -> InlineKeyboardMarkup:
    """Подменю записи результата сделки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 TP1", callback_data="res_TP1_" + trade_id),
            InlineKeyboardButton(text="🎯 TP2", callback_data="res_TP2_" + trade_id),
            InlineKeyboardButton(text="🏆 TP3", callback_data="res_TP3_" + trade_id),
        ],
        [
            InlineKeyboardButton(text="❌ Стоп-лосс",  callback_data="res_SL_"   + trade_id),
            InlineKeyboardButton(text="⏭ Пропустил",  callback_data="res_SKIP_" + trade_id),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад",      callback_data="sig_back_" + trade_id),
        ],
    ])


def signal_text(sig: SignalResult, cfg: TradeCfg) -> str:
    stars  = "⭐" * sig.quality + "☆" * (5 - sig.quality)
    header = "🟢 <b>LONG СИГНАЛ</b>" if sig.direction == "LONG" else "🔴 <b>SHORT СИГНАЛ</b>"
    emoji  = "📈" if sig.direction == "LONG" else "📉"
    
    counter_trend_warn = (
        "\n🔶 <b>━━━ ⚠️ КОНТР-ТРЕНД ━━━</b> 🔶"
        "\n<i>Сделка идёт ПРОТИВ основного тренда — повышенный риск!</i>"
    ) if sig.is_counter_trend else ""

    def pct(t): return abs((t - sig.entry) / sig.entry * 100)

    NL = "\n"
    quality_factors = (
        "📋 <b>Факторы качества:</b>" + NL + NL.join(sig.reasons)
    ) if sig.reasons else ""
    return (
        header + NL + NL +
        "💎 <b>" + sig.symbol + "</b>  " + emoji + "  <b>" + sig.breakout_type + "</b>" +
        counter_trend_warn + NL +
        "⭐ Качество: " + stars + NL +
        quality_factors + NL + NL +
        "🧠 <b>Анализ:</b> <i>" + sig.human_explanation + "</i>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "💰 Вход:    <code>" + "{:.6g}".format(sig.entry) + "</code>" + NL +
        "🛑 Стоп:    <code>" + "{:.6g}".format(sig.sl) + "</code>  <i>(-" + "{:.2f}".format(sig.risk_pct) + "%)</i>" + NL + NL +
        "🎯 Цель 1: <code>" + "{:.6g}".format(sig.tp1) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp1)) + "%)</i>" + NL +
        "🎯 Цель 2: <code>" + "{:.6g}".format(sig.tp2) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp2)) + "%)</i>" + NL +
        "🏆 Цель 3: <code>" + "{:.6g}".format(sig.tp3) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp3)) + "%)</i>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL + NL +
        "📊 " + sig.trend_local + "  |  RSI: <code>" + "{:.1f}".format(sig.rsi) + "</code>  |  Vol: <code>x" + "{:.1f}".format(sig.volume_ratio) + "</code>" + NL + NL +
        "⚡ <i>CHM Laboratory — CHM BREAKER</i>" + NL + NL +
        "👇 <i>Отметь результат когда сделка закроется:</i>"
    )


# ── Основной сканер ──────────────────────────────────

class MidScanner:

    def __init__(self, config: Config, bot: Bot, um: UserManager):
        self.cfg     = config
        self.bot     = bot
        self.um      = um
        self.fetcher = OKXFetcher()

        # Кэш индикаторов: job_key → CHMIndicator
        self._indicators:  dict[str, CHMIndicator] = {}
        self._ind_configs: dict[str, IndConfig]    = {}

        # Когда последний раз сканировали (job_key → timestamp)
        self._last_scan: dict[str, float] = {}

        self._api_sem = asyncio.Semaphore(config.API_CONCURRENCY)
        self._queue:   asyncio.Queue = asyncio.Queue()

        self._perf = {
            "cycles": 0, "users": 0,
            "signals": 0, "api_calls": 0,
        }

        # Глобальный тренд
        self._global_trend:     dict  = {}
        self._trend_updated_at: float = 0
        self._trend_ttl:        int   = 3600

    # ── Индикатор ────────────────────────────────────

    def _indicator(self, job: ScanJob) -> CHMIndicator:
        ic = _cfg_to_ind(job.cfg)
        if self._ind_configs.get(job.job_key) != ic:
            self._indicators[job.job_key]  = CHMIndicator(ic)
            self._ind_configs[job.job_key] = ic
        return self._indicators[job.job_key]

    # ── Глобальный тренд ─────────────────────────────

    async def _update_trend_if_needed(self):
        if time.time() - self._trend_updated_at > self._trend_ttl:
            try:
                self._global_trend     = await self.fetcher.get_global_trend()
                self._trend_updated_at = time.time()
                btc = self._global_trend.get("BTC", {})
                eth = self._global_trend.get("ETH", {})
                log.info(
                    "🌍 Тренд: BTC=" + btc.get("trend", "?") +
                    " ETH=" + eth.get("trend", "?")
                )
            except Exception as e:
                log.warning("Тренд: " + str(e))

    def get_trend(self) -> dict:
        return self._global_trend

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
            log.info("   Монет: " + str(len(coins)))
        return coins or []

    # ── Свечи (кэш → OKX) ────────────────────────────

    async def _fetch(self, symbol: str, tf: str):
        df = await cache.get_candles(symbol, tf)
        if df is not None:
            return df
        async with self._api_sem:
            df = await cache.get_candles(symbol, tf)
            if df is not None:
                return df
            self._perf["api_calls"] += 1
            df = await self.fetcher.get_candles(symbol, tf, limit=300)
            if df is not None:
                await cache.set_candles(symbol, tf, df, self.cfg.CACHE_TTL)
            return df

    # ── Загрузка свечей для TF ────────────────────────

    async def _load_tf_candles(self, tf: str, coins: list) -> dict:
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

    # ── Анализ одного задания ─────────────────────────

    async def _run_job(self, job: ScanJob, candles: dict):
        ind     = self._indicator(job)
        user    = job.user
        cfg     = job.cfg
        signals = 0

        for sym, df in candles.items():
            df_htf = await self._fetch(sym, "1D") if cfg.use_htf else None
            try:
                sig = ind.analyze(sym, df, df_htf)
            except Exception as e:
                log.debug(sym + ": " + str(e))
                continue
            if sig is None or sig.quality < cfg.min_quality:
                continue
            # Фильтр направления
            if job.direction == "LONG"  and sig.direction != "LONG":  continue
            if job.direction == "SHORT" and sig.direction != "SHORT": continue

            # Фильтр тренд-сигналов — пропускаем контр-трендовые если включено
            if cfg.trend_only and sig.is_counter_trend:
                continue

            if user.notify_signal:
                await self._send(user, sig, cfg)
            signals += 1

        self._perf["users"] += 1
        return signals

    # ── Отправка сигнала ──────────────────────────────

    async def _send(self, user: UserSettings, sig: SignalResult, cfg: TradeCfg):
        trade_id = str(user.user_id) + "_" + str(int(time.time() * 1000))
        risk     = abs(sig.entry - sig.sl)
        sign     = 1 if sig.direction == "LONG" else -1
        await db.db_add_trade({
            "trade_id":      trade_id,
            "user_id":       user.user_id,
            "symbol":        sig.symbol,
            "direction":     sig.direction,
            "entry":         sig.entry,
            "sl":            sig.sl,
            "tp1":           sig.entry + sign * risk * cfg.tp1_rr,
            "tp2":           sig.entry + sign * risk * cfg.tp2_rr,
            "tp3":           sig.entry + sign * risk * cfg.tp3_rr,
            "tp1_rr":        cfg.tp1_rr,
            "tp2_rr":        cfg.tp2_rr,
            "tp3_rr":        cfg.tp3_rr,
            "quality":       sig.quality,
            "timeframe":     cfg.timeframe,
            "breakout_type": sig.breakout_type,
            "created_at":    time.time(),
        })
        try:
            await self.bot.send_message(
                user.user_id,
                signal_text(sig, cfg),
                parse_mode="HTML",
                reply_markup=signal_compact_keyboard(trade_id, sig.symbol),
            )
            user.signals_received += 1
            await self.um.save(user)
            self._perf["signals"] += 1
            log.info(
                "✅ " + sig.symbol + " " + sig.direction +
                " ⭐" + str(sig.quality) +
                " → @" + (user.username or str(user.user_id))
            )
        except TelegramForbiddenError:
            user.long_active = False
            user.short_active = False
            user.active = False
            await self.um.save(user)
        except Exception as e:
            log.error("Ошибка отправки " + str(user.user_id) + ": " + str(e))

    # ── Воркер ───────────────────────────────────────

    async def _worker(self, wid: int, candles_by_tf: dict):
        while True:
            try:
                job: ScanJob = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                break
            try:
                candles = candles_by_tf.get(job.tf, {})
                await self._run_job(job, candles)
            except Exception as e:
                log.error("Воркер " + str(wid) + " ошибка: " + str(e))
            finally:
                self._queue.task_done()

    # ── Уведомление об истечении ──────────────────────

    async def _notify_expired(self, user: UserSettings):
        try:
            was_trial     = user.sub_status == "trial"
            user.long_active  = False
            user.short_active = False
            user.active       = False
            await self.um.save(user)
            cfg = self.cfg
            if was_trial:
                text = (
                    "⏰ <b>Пробный период завершён!</b>\n\n"
                    "📅 30 дней  — <b>" + cfg.PRICE_30_DAYS + "</b>\n"
                    "📅 90 дней  — <b>" + cfg.PRICE_90_DAYS + "</b>\n\n"
                    "💳 " + cfg.PAYMENT_INFO
                )
            else:
                text = (
                    "⏰ <b>Подписка истекла!</b>\n\n"
                    "📅 30 дней  — <b>" + cfg.PRICE_30_DAYS + "</b>\n"
                    "💳 " + cfg.PAYMENT_INFO
                )
            await self.bot.send_message(user.user_id, text, parse_mode="HTML")
        except Exception:
            pass

    # ── Построить список заданий для пользователя ─────

    @staticmethod
    def _build_jobs(user: UserSettings, now: float, last_scan: dict) -> list[ScanJob]:
        """
        Возвращает список ScanJob для всех активных направлений пользователя.
        Задание включается если прошёл нужный интервал.
        """
        jobs = []

        # ЛОНГ сканер
        if user.long_active:
            cfg = user.get_long_cfg()
            key = str(user.user_id) + "_LONG"
            if now - last_scan.get(key, 0) >= cfg.scan_interval:
                jobs.append(ScanJob(user=user, direction="LONG", cfg=cfg))

        # ШОРТ сканер
        if user.short_active:
            cfg = user.get_short_cfg()
            key = str(user.user_id) + "_SHORT"
            if now - last_scan.get(key, 0) >= cfg.scan_interval:
                jobs.append(ScanJob(user=user, direction="SHORT", cfg=cfg))

        # Режим ОБА (legacy / совместимость)
        if user.active and user.scan_mode == "both":
            cfg = user.shared_cfg()
            key = str(user.user_id) + "_BOTH"
            if now - last_scan.get(key, 0) >= cfg.scan_interval:
                jobs.append(ScanJob(user=user, direction="BOTH", cfg=cfg))

        return jobs

    # ── Главный цикл ──────────────────────────────────

    async def _cycle(self):
        start = time.time()
        await self._update_trend_if_needed()

        users = await self.um.get_active_users()
        if not users:
            return

        now = time.time()

        # Строим все задания
        all_jobs: list[ScanJob] = []
        for u in users:
            has, _ = u.check_access()
            if not has:
                await self._notify_expired(u)
                continue
            jobs = self._build_jobs(u, now, self._last_scan)
            all_jobs.extend(jobs)

        if not all_jobs:
            return

        log.info(
            "🔍 Цикл #" + str(self._perf["cycles"] + 1) +
            ": " + str(len(all_jobs)) + " заданий (" +
            str(len(users)) + " юзеров)"
        )

        # Группируем задания по TF
        tf_groups: dict[str, list[ScanJob]] = defaultdict(list)
        for job in all_jobs:
            tf_groups[job.tf].append(job)

        min_vol = min(j.cfg.min_volume_usdt for j in all_jobs)
        coins   = await self._load_coins(min_vol)

        # Загружаем свечи один раз для каждого TF
        candles_by_tf: dict[str, dict] = {}
        for tf, tf_jobs in tf_groups.items():
            log.info(
                "  📥 TF=" + tf + ": " + str(len(coins)) +
                " монет для " + str(len(tf_jobs)) + " заданий"
            )
            candles_by_tf[tf] = await self._load_tf_candles(tf, coins)

        # Ставим в очередь и обновляем last_scan
        for job in all_jobs:
            self._last_scan[job.job_key] = now
            await self._queue.put(job)

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
            "  ✅ " + "{:.1f}".format(elapsed) + "с | " +
            "Сигналов: " + str(self._perf["signals"]) + " | " +
            "API: " + str(self._perf["api_calls"]) + " | " +
            "Кэш: " + str(cs.get("size", 0)) + " ключей, " +
            str(cs.get("ratio", 0)) + "% хит"
        )

    async def run_forever(self):
        log.info(
            "🚀 MidScanner v4 | Воркеров: " + str(self.cfg.SCAN_WORKERS) +
            " | API: " + str(self.cfg.API_CONCURRENCY)
        )
        while True:
            try:
                await self._cycle()
            except Exception as e:
                log.error("Ошибка цикла: " + str(e), exc_info=True)
            await asyncio.sleep(self.cfg.SCAN_LOOP_SLEEP)

    def get_perf(self) -> dict:
        cs = cache.cache_stats()
        return {**self._perf, "cache": cs}
