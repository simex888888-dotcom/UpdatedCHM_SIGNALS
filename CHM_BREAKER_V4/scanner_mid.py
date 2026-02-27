"""
scanner_mid.py — мультисканнинг для 50-500 пользователей
CHM BREAKER v4.2 Classic (без SMC)
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
    user:      UserSettings
    direction: Direction
    cfg:       TradeCfg

    @property
    def job_key(self) -> str:
        return f"{self.user.user_id}_{self.direction}"

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
        TIMEFRAME=cfg.timeframe,
        PIVOT_STRENGTH=cfg.pivot_strength,
        ATR_PERIOD=cfg.atr_period,
        ATR_MULT=cfg.atr_mult,
        MAX_RISK_PCT=cfg.max_risk_pct,
        EMA_FAST=cfg.ema_fast,
        EMA_SLOW=cfg.ema_slow,
        RSI_PERIOD=cfg.rsi_period,
        RSI_OB=cfg.rsi_ob,
        RSI_OS=cfg.rsi_os,
        VOL_MULT=cfg.vol_mult,
        VOL_LEN=cfg.vol_len,
        MAX_LEVEL_AGE=cfg.max_level_age,
        MAX_RETEST_BARS=cfg.max_retest_bars,
        COOLDOWN_BARS=cfg.cooldown_bars,
        ZONE_BUFFER=cfg.zone_buffer,
        TP1_RR=cfg.tp1_rr,
        TP2_RR=cfg.tp2_rr,
        TP3_RR=cfg.tp3_rr,
        HTF_EMA_PERIOD=cfg.htf_ema_period,
        USE_RSI_FILTER=cfg.use_rsi,
        USE_VOLUME_FILTER=cfg.use_volume,
        USE_PATTERN_FILTER=cfg.use_pattern,
        USE_HTF_FILTER=cfg.use_htf,
    )


# ══════════════════════════════════════════════════════
# TELEGRAM — КНОПКИ И ТЕКСТ СИГНАЛА
# ══════════════════════════════════════════════════════

def result_keyboard(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎯 TP1",       callback_data=f"res_TP1_{trade_id}"),
            InlineKeyboardButton(text="🎯 TP2",       callback_data=f"res_TP2_{trade_id}"),
            InlineKeyboardButton(text="🏆 TP3",       callback_data=f"res_TP3_{trade_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ SL",        callback_data=f"res_SL_{trade_id}"),
            InlineKeyboardButton(text="⏭ Пропустил", callback_data=f"res_SKIP_{trade_id}"),
        ],
    ])


def _fmt(p: float) -> str:
    return f"{p:.6g}"


def _pct(value: float, entry: float) -> str:
    return f"{abs((value - entry) / entry * 100):.2f}"


def signal_text(sig: SignalResult, cfg: TradeCfg) -> str:
    is_long     = sig.direction == "LONG"
    emoji_dir   = "📈" if is_long else "📉"
    header      = "🟢 <b>LONG СИГНАЛ</b>" if is_long else "🔴 <b>SHORT СИГНАЛ</b>"
    stars       = "⭐" * sig.quality + "☆" * (5 - sig.quality)
    trend_label = (
        "⚠️ <b>КОНТР-ТРЕНД</b>"
        if sig.is_counter_trend
        else "✅ <b>ПО ТРЕНДУ</b>"
    )

    explanation = sig.human_explanation or "Сигнал по стратегии."
    trend_htf   = sig.trend_htf or "⏸ Выкл"

    # Причины качества
    reasons_block = ""
    if sig.reasons:
        reasons_block = (
            "\n📋 <b>Факторы:</b>\n"
            + "\n".join(f"  {r}" for r in sig.reasons)
            + "\n"
        )

    lines = [
        f"{header}  {emoji_dir}  {trend_label}",
        "",
        f"💎 <b>{sig.symbol}</b>  |  {sig.breakout_type}",
        f"⭐ Качество: {stars}",
        "",
        "💬 <b>Логика входа:</b>",
        f"<i>{explanation}</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Вход:     <code>{_fmt(sig.entry)}</code>",
        f"🛑 Стоп:     <code>{_fmt(sig.sl)}</code>  "
        f"<i>(-{sig.risk_pct:.2f}%)</i>",
        "",
        f"🎯 Цель 1:  <code>{_fmt(sig.tp1)}</code>  "
        f"<i>(+{_pct(sig.tp1, sig.entry)}%  ×{cfg.tp1_rr}R)</i>",
        f"🎯 Цель 2:  <code>{_fmt(sig.tp2)}</code>  "
        f"<i>(+{_pct(sig.tp2, sig.entry)}%  ×{cfg.tp2_rr}R)</i>",
        f"🏆 Цель 3:  <code>{_fmt(sig.tp3)}</code>  "
        f"<i>(+{_pct(sig.tp3, sig.entry)}%  ×{cfg.tp3_rr}R)</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 Тренд:   Локал {sig.trend_local}  |  HTF {trend_htf}",
        f"🎛 RSI: <code>{sig.rsi:.1f}</code>  "
        f"|  Объём: <code>×{sig.volume_ratio:.1f}</code>",
        f"🕯 Паттерн: {sig.pattern or '—'}",
    ]

    if reasons_block:
        lines.append(reasons_block.strip())

    lines += [
        "",
        "⚡ <i>CHM Laboratory — CHM GEL SIGNALS</i>",
        "",
        "👇 <i>Отметь результат, когда сделка закроется:</i>",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# ОСНОВНОЙ СКАНЕР
# ══════════════════════════════════════════════════════

class MidScanner:

    def __init__(self, config: Config, bot: Bot, um: UserManager):
        self.cfg     = config
        self.bot     = bot
        self.um      = um
        self.fetcher = OKXFetcher()

        self._indicators:  dict[str, CHMIndicator] = {}
        self._ind_configs: dict[str, IndConfig]    = {}
        self._last_scan:   dict[str, float]        = {}

        self._api_sem = asyncio.Semaphore(config.API_CONCURRENCY)
        self._queue:   asyncio.Queue = asyncio.Queue()

        self._perf = {
            "cycles":    0,
            "users":     0,
            "signals":   0,
            "api_calls": 0,
        }

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
                    f"🌍 Тренд: BTC={btc.get('trend', '?')} "
                    f"ETH={eth.get('trend', '?')}"
                )
            except Exception as e:
                log.warning(f"Тренд: {e}")

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
            log.info(f"   Монет: {len(coins)}")
        return coins or []

    # ── Свечи ────────────────────────────────────────

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
        ind  = self._indicator(job)
        user = job.user
        cfg  = job.cfg

        for sym, df in candles.items():
            df_htf = await self._fetch(sym, "1D") if cfg.use_htf else None
            try:
                sig = ind.analyze(sym, df, df_htf)
            except Exception as e:
                log.debug(f"{sym}: {e}")
                continue

            if sig is None or sig.quality < cfg.min_quality:
                continue

            if job.direction == "LONG"  and sig.direction != "LONG":  continue
            if job.direction == "SHORT" and sig.direction != "SHORT": continue

            if user.notify_signal:
                await self._send(user, sig, cfg)

        self._perf["users"] += 1

    # ── Отправка сигнала ──────────────────────────────

    async def _send(self, user: UserSettings, sig: SignalResult, cfg: TradeCfg):
        trade_id = f"{user.user_id}_{int(time.time() * 1000)}"

        await db.db_add_trade({
            "trade_id":      trade_id,
            "user_id":       user.user_id,
            "symbol":        sig.symbol,
            "direction":     sig.direction,
            "entry":         sig.entry,
            "sl":            sig.sl,
            "tp1":           sig.tp1,
            "tp2":           sig.tp2,
            "tp3":           sig.tp3,
            "tp1_rr":        cfg.tp1_rr,
            "tp2_rr":        cfg.tp2_rr,
            "tp3_rr":        cfg.tp3_rr,
            "quality":       sig.quality,
            "timeframe":     cfg.timeframe,
            "breakout_type": sig.breakout_type,
            "pattern":       sig.pattern,
            "rsi":           sig.rsi,
            "vol_ratio":     sig.volume_ratio,
            "is_counter":    sig.is_counter_trend,
            "created_at":    time.time(),
        })

        try:
            await self.bot.send_message(
                user.user_id,
                signal_text(sig, cfg),
                parse_mode="HTML",
                reply_markup=result_keyboard(trade_id),
            )
            user.signals_received += 1
            await self.um.save(user)
            self._perf["signals"] += 1
            log.info(
                f"✅ {sig.symbol} {sig.direction} ⭐{sig.quality} "
                f"RSI={sig.rsi:.1f} Vol=×{sig.volume_ratio:.1f} "
                f"→ @{user.username or user.user_id}"
            )
        except TelegramForbiddenError:
            user.long_active  = False
            user.short_active = False
            user.active       = False
            await self.um.save(user)
        except Exception as e:
            log.error(f"Ошибка отправки {user.user_id}: {e}")

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
                await self._run_job(job, candles_by_tf.get(job.tf, {}))
            except Exception as e:
                log.error(f"Воркер {wid} ошибка: {e}")
            finally:
                self._queue.task_done()

    # ── Уведомление об истечении подписки ─────────────

    async def _notify_expired(self, user: UserSettings):
        try:
            was_trial         = user.sub_status == "trial"
            user.long_active  = False
            user.short_active = False
            user.active       = False
            await self.um.save(user)
            cfg = self.cfg
            text = (
                "⏰ <b>Пробный период завершён!</b>\n\n"
                f"📅 30 дней — <b>{cfg.PRICE_30_DAYS}</b>\n"
                f"📅 90 дней — <b>{cfg.PRICE_90_DAYS}</b>\n\n"
                f"💳 {cfg.PAYMENT_INFO}"
            ) if was_trial else (
                "⏰ <b>Подписка истекла!</b>\n\n"
                f"📅 30 дней — <b>{cfg.PRICE_30_DAYS}</b>\n"
                f"💳 {cfg.PAYMENT_INFO}"
            )
            await self.bot.send_message(user.user_id, text, parse_mode="HTML")
        except Exception:
            pass

    # ── Построить задания для пользователя ───────────

    @staticmethod
    def _build_jobs(
        user: UserSettings,
        now: float,
        last_scan: dict,
    ) -> list[ScanJob]:
        jobs = []

        if user.long_active:
            cfg = user.get_long_cfg()
            key = f"{user.user_id}_LONG"
            if now - last_scan.get(key, 0) >= cfg.scan_interval:
                jobs.append(ScanJob(user=user, direction="LONG", cfg=cfg))

        if user.short_active:
            cfg = user.get_short_cfg()
            key = f"{user.user_id}_SHORT"
            if now - last_scan.get(key, 0) >= cfg.scan_interval:
                jobs.append(ScanJob(user=user, direction="SHORT", cfg=cfg))

        if user.active and user.scan_mode == "both":
            cfg = user.shared_cfg()
            key = f"{user.user_id}_BOTH"
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

        now      = time.time()
        all_jobs: list[ScanJob] = []

        for u in users:
            has, _ = u.check_access()
            if not has:
                await self._notify_expired(u)
                continue
            all_jobs.extend(self._build_jobs(u, now, self._last_scan))

        if not all_jobs:
            return

        log.info(
            f"🔍 Цикл #{self._perf['cycles'] + 1}: "
            f"{len(all_jobs)} заданий ({len(users)} юзеров)"
        )

        tf_groups: dict[str, list[ScanJob]] = defaultdict(list)
        for job in all_jobs:
            tf_groups[job.tf].append(job)

        min_vol = min(j.cfg.min_volume_usdt for j in all_jobs)
        coins   = await self._load_coins(min_vol)

        candles_by_tf: dict[str, dict] = {}
        for tf, tf_jobs in tf_groups.items():
            log.info(
                f"  📥 TF={tf}: {len(coins)} монет "
                f"для {len(tf_jobs)} заданий"
            )
            candles_by_tf[tf] = await self._load_tf_candles(tf, coins)

        for job in all_jobs:
            self._last_scan[job.job_key] = now
            await self._queue.put(job)

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
            f"  ✅ {elapsed:.1f}с | "
            f"Сигналов: {self._perf['signals']} | "
            f"API: {self._perf['api_calls']} | "
            f"Кэш: {cs.get('size', 0)} ключей, "
            f"{cs.get('ratio', 0)}% хит"
        )

    async def run_forever(self):
        log.info(
            f"🚀 MidScanner v4.2 Classic | "
            f"Воркеров: {self.cfg.SCAN_WORKERS} | "
            f"API: {self.cfg.API_CONCURRENCY}"
        )
        while True:
            try:
                await self._cycle()
            except Exception as e:
                log.error(f"Ошибка цикла: {e}", exc_info=True)
            await asyncio.sleep(self.cfg.SCAN_LOOP_SLEEP)

    def get_perf(self) -> dict:
        return {**self._perf, "cache": cache.cache_stats()}
