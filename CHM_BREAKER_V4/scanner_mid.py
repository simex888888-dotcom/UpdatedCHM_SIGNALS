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
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Literal

import numpy as np
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import cache
import database as db
from config import Config
from user_manager import UserManager, UserSettings, TradeCfg
from fetcher import OKXFetcher
from indicator import CHMIndicator, SignalResult
from keyboards import kb_contact_admin
from watermark import wm_inject
try:
    import fundamental as _fund
    _FUND_OK = True
except ImportError:
    _FUND_OK = False


def _compute_correlation(df1, df2, periods: int = 30) -> float:
    """Корреляция Пирсона по % доходности за последние N периодов."""
    try:
        r1 = df1["close"].pct_change().dropna().tail(periods)
        r2 = df2["close"].pct_change().dropna().tail(periods)
        n = min(len(r1), len(r2))
        if n < 10:
            return 0.0
        v1 = r1.tail(n).values
        v2 = r2.tail(n).values
        corr = float(np.corrcoef(v1, v2)[0, 1])
        return round(corr, 2) if not np.isnan(corr) else 0.0
    except Exception:
        return 0.0


def _corr_label(btc_corr: float, eth_corr: float) -> str:
    """Текстовый ярлык зависимости монеты от BTC/ETH."""
    HIGH = 0.65
    follows_btc = btc_corr >= HIGH
    follows_eth = eth_corr >= HIGH
    if follows_btc and follows_eth:
        return f"📡 Следует за BTC ({btc_corr:+.2f}) и ETH ({eth_corr:+.2f})"
    elif follows_btc:
        return f"📡 Следует за BTC ({btc_corr:+.2f})"
    elif follows_eth:
        return f"📡 Следует за ETH ({eth_corr:+.2f})"
    elif btc_corr < 0.3 and eth_corr < 0.3:
        return f"🔮 Движется самостоятельно (BTC {btc_corr:+.2f} / ETH {eth_corr:+.2f})"
    else:
        return f"📊 Слабая связь с рынком (BTC {btc_corr:+.2f} / ETH {eth_corr:+.2f})"

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
    HTF_EMA_PERIOD:     int   = 50
    HTF_TIMEFRAME:      str   = "1d"
    USE_RSI_FILTER:     bool  = True
    USE_VOLUME_FILTER:  bool  = True
    USE_PATTERN_FILTER: bool  = False
    USE_HTF_FILTER:     bool  = False
    # ── Протокол уровней (Price Action) ──────────────
    ZONE_PCT:           float = 0.7   # Ширина зоны уровня в % от цены
    MAX_DIST_PCT:       float = 1.5   # Макс. дистанция до уровня для входа (%)
    MIN_RR:             float = 2.0   # Минимальный R:R
    MAX_LEVEL_TESTS:    int   = 4     # Макс. тестов уровня (при >= — ожидается пробой)


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
        ZONE_PCT=cfg.zone_pct, MAX_DIST_PCT=cfg.max_dist_pct,
        MIN_RR=cfg.min_rr, MAX_LEVEL_TESTS=cfg.max_level_tests,
    )


# ── Telegram ─────────────────────────────────────────

def _tv_url(symbol: str) -> str:
    """Конвертирует OKX символ в ссылку TradingView.
    BTC-USDT-SWAP → https://www.tradingview.com/chart/?symbol=OKX:BTCUSDT.P
    """
    clean = symbol.replace("-SWAP", "").replace("-", "")
    return "https://www.tradingview.com/chart/?symbol=OKX:" + clean + ".P"


def signal_compact_keyboard(trade_id: str, symbol: str,
                            show_trade_btn: bool = False) -> InlineKeyboardMarkup:
    """Компактная клавиатура под сигналом.
    show_trade_btn=True добавляет кнопку ручного подтверждения входа.
    """
    rows = [
        [
            InlineKeyboardButton(text="📈 График",     url=_tv_url(symbol)),
            InlineKeyboardButton(text="📊 Статистика", callback_data="my_stats"),
        ],
        [
            InlineKeyboardButton(text="📋 Записать результат ▾", callback_data="sig_records_" + trade_id),
        ],
    ]
    if show_trade_btn:
        rows.insert(0, [
            InlineKeyboardButton(
                text="✅ Открыть сделку на Bybit",
                callback_data="exec_trade_" + trade_id,
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    def _fp(v: float) -> str:
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

    return (
        header + NL + NL +
        "💎 <b>" + sig.symbol + "</b>  " + emoji + "  <b>" + sig.breakout_type + "</b>" +
        counter_trend_warn + NL +
        "⭐ Качество: " + stars + NL +
        quality_factors + NL + NL +
        "🧠 <b>Анализ:</b> <i>" + sig.human_explanation + "</i>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "💰 Вход:    <code>" + _fp(sig.entry) + "</code>" + NL +
        "🛑 Стоп:    <code>" + _fp(sig.sl) + "</code>  <i>(-" + "{:.2f}".format(sig.risk_pct) + "%)</i>" + NL + NL +
        "🎯 Цель 1: <code>" + _fp(sig.tp1) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp1)) + "%)</i>" + NL +
        "🎯 Цель 2: <code>" + _fp(sig.tp2) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp2)) + "%)</i>" + NL +
        "🏆 Цель 3: <code>" + _fp(sig.tp3) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp3)) + "%)</i>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL + NL +
        "📊 " + sig.trend_local + "  |  RSI: <code>" + "{:.1f}".format(sig.rsi) + "</code>  |  Vol: <code>x" + "{:.1f}".format(sig.volume_ratio) + "</code>" + NL +
        _corr_label(sig.btc_corr, sig.eth_corr) + NL + NL +
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

        # Фундаментальный контекст (обновляется раз в цикл)
        self._fund_block: str = ""

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
                    "🌍 Тренд: BTC=" + btc.get("trend_text", "?") +
                    " ETH=" + eth.get("trend_text", "?")
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

        # Загружаем BTC и ETH для корреляции один раз на всё задание
        btc_df = candles.get("BTC-USDT-SWAP")
        if btc_df is None:
            btc_df = await self._fetch("BTC-USDT-SWAP", job.tf)
        eth_df = candles.get("ETH-USDT-SWAP")
        if eth_df is None:
            eth_df = await self._fetch("ETH-USDT-SWAP", job.tf)

        # Фильтр выбранной монеты
        watch = getattr(user, "watch_coin", "").strip().upper()

        for sym, df in candles.items():
            if watch and sym.upper() != watch:
                continue
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

            # Корреляция с BTC/ETH (не для самих BTC/ETH)
            if sym not in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
                if btc_df is not None:
                    sig.btc_corr = _compute_correlation(df, btc_df)
                if eth_df is not None:
                    sig.eth_corr = _compute_correlation(df, eth_df)

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
        tp1      = sig.entry + sign * risk * cfg.tp1_rr
        tp2      = sig.entry + sign * risk * cfg.tp2_rr
        tp3      = sig.entry + sign * risk * cfg.tp3_rr
        await db.db_add_trade({
            "trade_id":      trade_id,
            "user_id":       user.user_id,
            "symbol":        sig.symbol,
            "direction":     sig.direction,
            "entry":         sig.entry,
            "sl":            sig.sl,
            "tp1":           tp1,
            "tp2":           tp2,
            "tp3":           tp3,
            "tp1_rr":        cfg.tp1_rr,
            "tp2_rr":        cfg.tp2_rr,
            "tp3_rr":        cfg.tp3_rr,
            "quality":       sig.quality,
            "timeframe":     cfg.timeframe,
            "breakout_type": sig.breakout_type,
            "created_at":    time.time(),
        })

        # ── Авто-трейдинг ────────────────────────────
        auto_trade      = getattr(user, "auto_trade",      False)
        auto_trade_mode = getattr(user, "auto_trade_mode", "confirm")
        api_key         = getattr(user, "bybit_api_key",    "")
        api_secret      = getattr(user, "bybit_api_secret", "")
        risk_pct        = getattr(user, "trade_risk_pct",   1.0)
        leverage        = getattr(user, "trade_leverage",   10)
        show_trade_btn  = False

        if auto_trade and api_key and api_secret:
            # Не открываем вторую сделку по той же монете
            if await db.db_has_open_trade_for_symbol(user.user_id, sig.symbol):
                log.info(
                    f"auto_trade skip duplicate: {sig.symbol} уже открыта у uid={user.user_id}"
                )
                show_trade_btn = False
                auto_trade = False  # сигнал покажем, сделку пропустим

        if auto_trade and api_key and api_secret:
            max_trades = getattr(user, "max_trades_limit", 5)
            open_count = await db.db_count_open_trades(user.user_id)
            # max_trades=0 означает «без лимита»
            if max_trades > 0 and open_count >= max_trades:
                await self.bot.send_message(
                    user.user_id,
                    f"⛔ Авто-трейд отклонён: достигнут лимит открытых сделок "
                    f"({open_count}/{max_trades}).\n"
                    f"Сигнал: {sig.symbol} {sig.direction}"
                )
                show_trade_btn = False
                auto_trade = False  # пропускаем блок ниже

        if auto_trade and api_key and api_secret:
            if auto_trade_mode == "auto":
                # Открываем сразу (3 TP + BE мониторинг)
                try:
                    import bybit_trader
                    result = await bybit_trader.place_trade(
                        api_key, api_secret,
                        sig.symbol, sig.direction,
                        sig.entry, sig.sl, tp1,
                        risk_pct, leverage,
                        tp2=tp2, tp3=tp3,
                    )
                    # Сохраняем pos_idx для корректного BE-мониторинга
                    if result.get("ok"):
                        await db.db_update_trade_pos_idx(trade_id, result.get("pos_idx", 0))
                    # Сохраняем order_id и pos_idx — критично для BE-монитора
                    if result.get("ok"):
                        # Сохраняем order_id и pos_idx — дедупликация и BE-монитор
                        await db.db_update_trade_bybit(
                            trade_id,
                            result.get("order_id", ""),
                            result.get("pos_idx", 0),
                        )
                    else:
                        # Сделка не открыта — помечаем SKIP чтобы не блокировать следующий сигнал
                        await db.db_set_trade_result(trade_id, "SKIP", 0.0)
                    trade_msg = bybit_trader.format_trade_result(
                        result, sig.direction, sig.symbol,
                        sig.entry, sig.sl, tp1, risk_pct, leverage,
                        tp2=tp2, tp3=tp3,
                    )
                    await self.bot.send_message(
                        user.user_id, trade_msg, parse_mode="HTML"
                    )
                except Exception as e:
                    log.error(f"auto_trade {sig.symbol}: {e}")
                    await db.db_set_trade_result(trade_id, "SKIP", 0.0)
                    await self.bot.send_message(
                        user.user_id,
                        f"⚠️ Авто-трейд: ошибка открытия {sig.symbol}: {e}"
                    )
            else:
                # Режим подтверждения — показать кнопку
                show_trade_btn = True

        try:
            _base_text = signal_text(sig, cfg)
            if self._fund_block:
                _base_text += (
                    "\n━━━━━━━━━━━━━━━━━━━━\n"
                    "📌 <b>Фундаментал рынка:</b>\n" +
                    self._fund_block + "\n"
                )
            await self.bot.send_message(
                user.user_id,
                wm_inject(_base_text, user.user_id),
                parse_mode="HTML",
                reply_markup=signal_compact_keyboard(
                    trade_id, sig.symbol, show_trade_btn=show_trade_btn
                ),
                protect_content=True,
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
        """Уведомление об окончании подписки — останавливаем сканеры."""
        user.long_active  = False
        user.short_active = False
        user.active       = False
        user.sub_status   = "expired"
        if user.expired_notified:
            await self.um.save(user)
            return
        try:
            user.expired_notified = True
            await self.um.save(user)
            cfg = self.cfg
            text = (
                "🚫 <b>Подписка истекла!</b>\n\n"
                "📦 <b>Тариф:</b>\n"
                "🤖 CHM BOT — 3 месяца — <b>" + cfg.BOT_PRICE_90 + "</b>\n"
                "🤖 CHM BOT — 12 месяцев — <b>" + cfg.BOT_PRICE_365 + "</b>\n\n"
                "💳 <b>Оплата подписки</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🔗 <b>Сеть:</b> BEP20 (BSC)\n\n"
                "📋 <b>Адрес для перевода:</b>\n"
                "<code>0xb5116aa7d7a20d7c45a8a5ff10bc1d86437df985</code>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "✅ После оплаты отправь скриншот + свой Telegram ID администратору:\n\n"
                "🆔 <b>Твой ID:</b> <code>" + str(user.user_id) + "</code>"
            )
            await self.bot.send_message(
                user.user_id, text,
                parse_mode="HTML", reply_markup=kb_contact_admin(),
            )
        except Exception:
            pass

    # ── Фоновая проверка подписок ─────────────────────

    async def _sub_check_loop(self):
        """Каждые 5 минут проверяет всех пользователей:
        - отправляет уведомление об окончании подписки
        """
        await asyncio.sleep(60)  # пауза после старта
        while True:
            try:
                now   = time.time()
                users = await self.um.all_users()
                for user in users:
                    if user.sub_status not in ("trial", "active"):
                        continue
                    left = user.sub_expires - now
                    if left <= 0 and not user.expired_notified:
                        await self._notify_expired(user)
            except Exception as e:
                log.error("_sub_check_loop: " + str(e))
            await asyncio.sleep(300)  # каждые 5 минут

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

        # Обновляем фундаментальный контекст один раз на цикл
        if _FUND_OK:
            try:
                self._fund_block = await _fund.get_market_context_block()
            except Exception as _fe:
                log.debug("fundamental: " + str(_fe))

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
            if u.strategy == "SMC":
                continue  # SMC-пользователи обрабатываются smc/scanner.py
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

    async def _scan_loop(self):
        while True:
            try:
                await self._cycle()
            except Exception as e:
                log.error("Ошибка цикла: " + str(e), exc_info=True)
            await asyncio.sleep(self.cfg.SCAN_LOOP_SLEEP)

    # ── Мониторинг безубытка (BE) ─────────────────────────

    async def _be_monitor_loop(self):
        """Каждые 60 сек проверяет открытые авто-сделки и переносит SL в БУ после TP1."""
        await asyncio.sleep(90)   # начальная задержка при старте бота
        while True:
            try:
                await self._check_breakevens()
            except Exception as e:
                log.debug(f"BE monitor error: {e}")
            await asyncio.sleep(60)

    @staticmethod
    def _trade_result_from_exit(trade: dict, exit_price: float) -> tuple[str, float]:
        """Определяет результат сделки (TP1/TP2/TP3/SL/BE) по цене выхода."""
        direction = trade.get("direction", "LONG")
        entry     = float(trade.get("entry", 0))
        sl        = float(trade.get("sl",    0))
        tp1       = float(trade.get("tp1",   0))
        tp2       = float(trade.get("tp2",   0))
        tp3       = float(trade.get("tp3",   0))
        be_set    = bool(trade.get("be_set", 0))
        risk      = abs(entry - sl)
        if risk <= 0 or entry <= 0:
            return "CLOSED", 0.0
        tol = 0.012   # 1.2% допуск (учитывает проскальзывание и маркет-прайс)
        if direction == "LONG":
            if tp3 > 0 and exit_price >= tp3 * (1 - tol):
                return "TP3", round((tp3 - entry) / risk, 2)
            if tp2 > 0 and exit_price >= tp2 * (1 - tol):
                return "TP2", round((tp2 - entry) / risk, 2)
            if tp1 > 0 and exit_price >= tp1 * (1 - tol):
                return "TP1", round((tp1 - entry) / risk, 2)
            if be_set and exit_price >= entry * (1 - tol):
                return "BE", 0.0
            return "SL",  round((entry - exit_price) / risk, 2)
        else:
            if tp3 > 0 and exit_price <= tp3 * (1 + tol):
                return "TP3", round((entry - tp3) / risk, 2)
            if tp2 > 0 and exit_price <= tp2 * (1 + tol):
                return "TP2", round((entry - tp2) / risk, 2)
            if tp1 > 0 and exit_price <= tp1 * (1 + tol):
                return "TP1", round((entry - tp1) / risk, 2)
            if be_set and exit_price <= entry * (1 + tol):
                return "BE", 0.0
            return "SL",  round((exit_price - entry) / risk, 2)

    async def _check_breakevens(self):
        import bybit_trader
        users = await self.um.get_active_auto_trade_users()
        for user in users:
            api_key    = getattr(user, "bybit_api_key",    "")
            api_secret = getattr(user, "bybit_api_secret", "")
            if not api_key or not api_secret:
                continue
            # Все незакрытые сделки (не только be_set=0)
            trades = await db.db_get_all_open_trades(user.user_id)
            if not trades:
                continue
            try:
                positions = await bybit_trader.get_positions(api_key, api_secret)
            except Exception:
                continue
            pos_map = {p["symbol"]: p for p in positions if float(p.get("size", 0)) > 0}

            for trade in trades:
                bb_sym    = bybit_trader._to_bybit_symbol(trade["symbol"])
                pos       = pos_map.get(bb_sym)
                direction = trade.get("direction", "LONG")
                entry     = float(trade.get("entry", 0))

                if not pos:
                    # Позиция закрыта — дождёмся минуты после открытия чтобы избежать
                    # ложного срабатывания сразу после создания сделки
                    if time.time() - float(trade.get("created_at", 0)) < 60:
                        continue

                    # Запрашиваем историю закрытых позиций.
                    # Bybit иногда задерживает появление записи — делаем 2 попытки.
                    expected_side = "Buy" if direction == "LONG" else "Sell"
                    created_ms    = float(trade.get("created_at", 0)) * 1000
                    matched_exit  = None

                    # Пробуем получить цену выхода из истории закрытых позиций.
                    # Bybit задерживает появление записи — 3 попытки с паузами.
                    for _attempt in range(3):
                        try:
                            closed = await bybit_trader.get_closed_pnl(
                                api_key, api_secret, trade["symbol"]
                            )
                        except Exception:
                            closed = []

                        for rec in closed:
                            # side в get_closed_pnl = сторона открытия позиции
                            if rec.get("side") != expected_side:
                                continue
                            if float(rec.get("updatedTime", 0)) < created_ms:
                                continue
                            exit_val = float(rec.get("avgExitPrice") or 0)
                            if exit_val > 0:
                                matched_exit = exit_val
                            break

                        if matched_exit:
                            break
                        # Запись ещё не появилась — ждём и повторяем
                        wait_secs = (3, 5, 0)[_attempt]
                        if wait_secs:
                            await asyncio.sleep(wait_secs)

                    # Последний резерв: история исполнений (executions)
                    if not matched_exit:
                        try:
                            matched_exit = await bybit_trader.get_execution_exit_price(
                                api_key, api_secret, trade["symbol"], created_ms
                            )
                        except Exception:
                            pass

                    if matched_exit and matched_exit > 0:
                        result_str, result_rr = self._trade_result_from_exit(
                            trade, matched_exit
                        )
                    else:
                        result_str, result_rr = "CLOSED", 0.0

                    # Отменяем оставшиеся TP-ордера (reduce-only могут висеть после SL)
                    try:
                        cancel_res = await bybit_trader.cancel_all_orders(
                            api_key, api_secret, trade["symbol"]
                        )
                        if cancel_res.get("ok"):
                            n = cancel_res.get("cancelled", 0)
                            if n:
                                log.info(f"Cancelled {n} open orders for {bb_sym} after close")
                    except Exception as e_cancel:
                        log.debug(f"cancel_all_orders {bb_sym}: {e_cancel}")

                    await db.db_set_trade_result(
                        trade["trade_id"], result_str, result_rr
                    )
                    sym_label = trade["symbol"].replace("-USDT-SWAP", "").replace("-USDT", "")
                    emoji = "✅" if result_str.startswith("TP") else ("🛑" if result_str == "SL" else ("♻️" if result_str == "BE" else "📋"))
                    rr_text = f"  R:R {result_rr:+.2f}" if result_rr != 0 else ""
                    if matched_exit:
                        exit_display = f"<code>{matched_exit}</code>"
                    else:
                        # Не удалось получить точную цену — показываем нейтральный текст
                        exit_display = "<i>по рыночной цене</i>"
                    # Запрашиваем текущий баланс после закрытия
                    balance_line = ""
                    try:
                        new_balance = await bybit_trader.get_balance(api_key, api_secret)
                        balance_line = f"\n💼 Баланс: <code>${new_balance:.2f} USDT</code>"
                    except Exception:
                        pass
                    try:
                        await self.bot.send_message(
                            user.user_id,
                            f"{emoji} <b>Сделка закрыта: {result_str}</b>{rr_text}\n\n"
                            f"💎 {sym_label}  |  {direction}\n"
                            f"💰 Вход: <code>{entry}</code>\n"
                            f"📤 Выход: {exit_display}"
                            f"{balance_line}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    log.info(f"Trade closed: {bb_sym} {direction} → {result_str}{rr_text} "
                             f"exit={matched_exit} (uid={user.user_id})")
                    continue

                # ── Позиция ещё открыта: проверяем безубыток ──────────────
                if trade.get("be_set", 0):
                    continue   # BE уже выставлен

                mark_price = float(pos.get("markPrice", 0))
                if mark_price <= 0:
                    continue
                tp1_price = float(trade.get("tp1", 0))
                if tp1_price <= 0 or entry <= 0:
                    continue

                tp1_reached = (
                    (direction == "LONG"  and mark_price >= tp1_price) or
                    (direction == "SHORT" and mark_price <= tp1_price)
                )
                if not tp1_reached:
                    continue

                pos_idx = int(trade.get("pos_idx", 0))
                result  = await bybit_trader.set_breakeven(
                    api_key, api_secret,
                    trade["symbol"], entry, direction, pos_idx,
                )
                if result.get("ok"):
                    await db.db_set_trade_be(trade["trade_id"])
                    sym_label = trade["symbol"].replace("-USDT-SWAP", "").replace("-USDT", "")
                    try:
                        await self.bot.send_message(
                            user.user_id,
                            f"♻️ <b>Безубыток выставлен</b>\n\n"
                            f"💎 {sym_label}  |  TP1 достигнут\n"
                            f"🛑 Стоп перенесён на вход: <code>{entry}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                else:
                    log.warning(f"BE set failed {bb_sym}: {result.get('error')}")

    async def run_forever(self):
        log.info(
            "🚀 MidScanner v4 | Воркеров: " + str(self.cfg.SCAN_WORKERS) +
            " | API: " + str(self.cfg.API_CONCURRENCY)
        )
        await asyncio.gather(
            self._scan_loop(),
            self._sub_check_loop(),
            self._be_monitor_loop(),
        )

    def get_perf(self) -> dict:
        cs = cache.cache_stats()
        return {**self._perf, "cache": cs}

    # ── Анализ монеты по запросу пользователя ────────────

    async def analyze_on_demand(
        self, symbol: str, cfg: TradeCfg
    ) -> Optional[tuple[SignalResult, str]]:
        """
        Анализирует монету по запросу. Возвращает (SignalResult, текст сигнала)
        или None если сигнала нет.
        symbol — в формате "BTC", "BTCUSDT" или "BTC-USDT-SWAP"
        """
        # Нормализуем символ к OKX формату
        sym = symbol.upper().strip()
        if not sym.endswith("-SWAP"):
            if sym.endswith("USDT"):
                sym = sym[:-4] + "-USDT-SWAP"
            else:
                sym = sym + "-USDT-SWAP"

        tf = cfg.timeframe
        df = await self.fetcher.get_candles(sym, tf, limit=300)
        if df is None or len(df) < 60:
            return None

        ind_cfg = _cfg_to_ind(cfg)
        ind     = CHMIndicator(ind_cfg)

        df_htf = None
        if cfg.use_htf:
            df_htf = await self.fetcher.get_candles(sym, "1D", limit=100)

        try:
            sig = ind.analyze(sym, df, df_htf)
        except Exception as e:
            log.warning("analyze_on_demand %s: %s", sym, e)
            return None

        if sig is None:
            return None

        # Корреляция
        if sym not in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
            btc_df = await self.fetcher.get_candles("BTC-USDT-SWAP", tf, limit=60)
            eth_df = await self.fetcher.get_candles("ETH-USDT-SWAP", tf, limit=60)
            if btc_df is not None:
                sig.btc_corr = _compute_correlation(df, btc_df)
            if eth_df is not None:
                sig.eth_corr = _compute_correlation(df, eth_df)

        text = signal_text(sig, cfg)
        return sig, text
