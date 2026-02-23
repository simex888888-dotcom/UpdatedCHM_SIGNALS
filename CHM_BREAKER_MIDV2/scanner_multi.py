"""
Мульти-пользовательский сканер
"""

import asyncio
import logging
import time
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from config import Config
from user_manager import UserManager, UserSettings
from fetcher import BinanceFetcher
from indicator import CHMIndicator, SignalResult

log = logging.getLogger("CHM.MultiScanner")


def make_signal_text(sig: SignalResult, user: UserSettings, change_24h=None) -> str:
    """Профессиональный сигнал с чеклистом подтверждений."""
    is_long  = sig.direction == "LONG"
    stars    = "⭐" * sig.quality + "☆" * (5 - sig.quality)

    # Заголовок
    if is_long:
        header = "╔══════════════════════╗\n║   🟢  LONG  СИГНАЛ   ║\n╚══════════════════════╝"
    else:
        header = "╔══════════════════════╗\n║   🔴  SHORT СИГНАЛ   ║\n╚══════════════════════╝"

    # Уровни с пользовательскими RR
    risk  = abs(sig.entry - sig.sl)
    tp1   = sig.entry + risk * user.tp1_rr if is_long else sig.entry - risk * user.tp1_rr
    tp2   = sig.entry + risk * user.tp2_rr if is_long else sig.entry - risk * user.tp2_rr
    tp3   = sig.entry + risk * user.tp3_rr if is_long else sig.entry - risk * user.tp3_rr

    def pct(t):  return abs((t - sig.entry) / sig.entry * 100)
    def fmt(v):  return f"{v:.6g}"

    # 24h данные
    ch24_line = ""
    if change_24h:
        ch  = change_24h.get("change_pct", 0)
        vol = change_24h.get("volume_usdt", 0)
        em  = "🔺" if ch > 0 else "🔻"
        ch24_line = f"\n📅 <b>24h:</b> {em} <b>{ch:+.2f}%</b>  |  Vol: <b>${vol:,.0f}</b>"

    # ── ЧЕКЛИСТ подтверждений ─────────────────────────────
    # Каждый критерий: ✅ если выполнен, ❌ если нет
    vol_ok    = sig.volume_ratio >= 1.2
    rsi_bull  = sig.rsi < 50
    rsi_bear  = sig.rsi > 50
    rsi_ok    = rsi_bull if is_long else rsi_bear
    rsi_zone  = sig.rsi < 40 if is_long else sig.rsi > 60
    pat_ok    = bool(sig.pattern and "Бычья свеча" not in sig.pattern and "Медвежья свеча" not in sig.pattern)
    trend_ok  = "Бычий" in sig.trend_local if is_long else "Медвежий" in sig.trend_local
    htf_ok    = "Бычий" in sig.trend_htf if is_long else ("Медвежий" in sig.trend_htf if "Выкл" not in sig.trend_htf else None)

    def ck(v) -> str: return "✅" if v else "❌"
    def ck3(v) -> str: return "✅" if v else ("➖" if v is None else "❌")

    rsi_str  = f"RSI {sig.rsi:.1f}"
    vol_str  = f"Объём ×{sig.volume_ratio:.1f}"
    htf_str  = sig.trend_htf if "Выкл" not in sig.trend_htf else "HTF выкл"

    checklist = (
        f"{ck(trend_ok)} Тренд: <b>{sig.trend_local}</b>\n"
        f"{ck3(htf_ok)} HTF тренд: <b>{htf_str}</b>\n"
        f"{ck(rsi_ok)} {rsi_str} {'< 50 ↙️' if is_long else '> 50 ↗️'}"
        + (f"  <i>({'зона' if rsi_zone else 'слабый'})</i>\n" if True else "\n")
        + f"{ck(vol_ok)} {vol_str}{'  🔥' if sig.volume_ratio >= 2 else ''}\n"
        + f"{ck(pat_ok)} Паттерн: <b>{sig.pattern}</b>\n"
        + f"━━━━━━━━━━━━━━━━━━━━\n"
        + f"{'✅' if sig.has_bos else '❌'} BOS (Break of Structure)\n"
        + f"{'✅' if sig.has_ob  else '❌'} Order Block"
        + (f" @ <code>{sig.ob_level:.4g}</code>" if sig.has_ob else "") + "\n"
        + f"{'✅' if sig.has_fvg else '❌'} FVG / Имбаланс"
        + (f" <i>({sig.fvg_size_pct:.2f}%)</i>" if sig.has_fvg else "") + "\n"
        + f"{'✅' if sig.has_liq_sweep else '❌'} Liquidity Sweep\n"
        + f"{'✅' if sig.has_divergence else '❌'} RSI Дивергенция"
    )

    # Итоговый счёт
    smc_hits = sum([sig.has_bos, sig.has_ob, sig.has_fvg, sig.has_liq_sweep, sig.has_divergence])
    score_bar = "█" * sig.quality + "░" * (5 - sig.quality)
    smc_bar   = "▓" * smc_hits + "░" * (5 - smc_hits)
    quality_line = (
        f"⭐ <b>Качество:</b> {stars}  [{score_bar}] {sig.quality}/5\n"
        f"🔮 <b>SMC Score:</b>  [{smc_bar}] {smc_hits}/5"
    )

    text = (
        f"{header}\n\n"
        f"💎 <b>{sig.symbol}</b>   {sig.breakout_type}{ch24_line}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Вход:</b>    <code>{fmt(sig.entry)}</code>\n"
        f"🛑 <b>Стоп:</b>    <code>{fmt(sig.sl)}</code>  <i>(-{sig.risk_pct:.2f}%)</i>\n"
        f"\n"
        f"🎯 <b>Цель 1:</b>  <code>{fmt(tp1)}</code>  <i>(+{pct(tp1):.2f}% / {user.tp1_rr}R)</i>\n"
        f"🎯 <b>Цель 2:</b>  <code>{fmt(tp2)}</code>  <i>(+{pct(tp2):.2f}% / {user.tp2_rr}R)</i>\n"
        f"🏆 <b>Цель 3:</b>  <code>{fmt(tp3)}</code>  <i>(+{pct(tp3):.2f}% / {user.tp3_rr}R)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"📋 <b>ПОДТВЕРЖДЕНИЯ:</b>\n"
        f"{checklist}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{quality_line}\n"
        f"\n"
        f"⚡ <i>CHM Laboratory — CHM BREAKER</i>"
    )
    return text


class UserScanner:
    def __init__(self, user_id: int):
        self.user_id   = user_id
        self.last_scan = 0.0


class MultiScanner:

    def __init__(self, config: Config, bot: Bot, um: UserManager):
        self.config  = config
        self.bot     = bot
        self.um      = um
        self.fetcher = BinanceFetcher()

        self._candle_cache:    dict = {}   # "symbol_tf" -> (df, timestamp)
        self._htf_cache:       dict = {}
        self._coins_cache:     list = []
        self._coins_loaded_at: float = 0.0
        self._user_scanners:   dict = {}
        self._indicators:      dict = {}   # user_id -> CHMIndicator

    def _get_us(self, user_id: int) -> UserScanner:
        if user_id not in self._user_scanners:
            self._user_scanners[user_id] = UserScanner(user_id)
        return self._user_scanners[user_id]

    def _get_indicator(self, user: UserSettings) -> CHMIndicator:
        cfg = self.config
        cfg.TIMEFRAME          = user.timeframe
        cfg.USE_RSI_FILTER     = user.use_rsi
        cfg.USE_VOLUME_FILTER  = user.use_volume
        cfg.USE_PATTERN_FILTER = user.use_pattern
        cfg.USE_HTF_FILTER     = user.use_htf
        cfg.ATR_MULT           = user.atr_mult
        cfg.MAX_RISK_PCT       = user.max_risk_pct
        cfg.TP1_RR             = user.tp1_rr
        cfg.TP2_RR             = user.tp2_rr
        cfg.TP3_RR             = user.tp3_rr
        if user.user_id not in self._indicators:
            self._indicators[user.user_id] = CHMIndicator(cfg)
        return self._indicators[user.user_id]

    async def _load_coins(self, min_vol: float) -> list:
        now = time.time()
        if self._coins_cache and (now - self._coins_loaded_at) < 3600 * 6:
            return self._coins_cache
        coins = await self.fetcher.get_all_usdt_pairs(
            min_volume_usdt=min_vol,
            blacklist=self.config.AUTO_BLACKLIST,
        )
        if not coins:
            coins = self.config.COINS
        self._coins_cache     = coins
        self._coins_loaded_at = now
        log.info(f"📋 Монет загружено: {len(coins)}")
        return coins

    async def _get_candles(self, symbol: str, tf: str):
        key = f"{symbol}_{tf}"
        now = time.time()
        cached = self._candle_cache.get(key)
        if cached and (now - cached[1]) < 60:
            return cached[0]
        df = await self.fetcher.get_candles(symbol, tf, limit=300)
        if df is not None:
            self._candle_cache[key] = (df, now)
        return df

    async def _get_htf(self, symbol: str):
        key = f"{symbol}_1d"
        now = time.time()
        cached = self._htf_cache.get(key)
        if cached and (now - cached[1]) < 3600:
            return cached[0]
        df = await self.fetcher.get_candles(symbol, "1D", limit=100)
        if df is not None:
            self._htf_cache[key] = (df, now)
        return df

    async def _send_signal(self, user: UserSettings, sig: SignalResult):
        change_24h = await self.fetcher.get_24h_change(sig.symbol)
        text = make_signal_text(sig, user, change_24h)
        try:
            await self.bot.send_message(user.user_id, text, parse_mode="HTML")
            user.signals_received += 1
            self.um.save_user(user)
            log.info(f"✅ Сигнал → {user.username or user.user_id}: {sig.symbol} {sig.direction} ⭐{sig.quality}")
        except TelegramForbiddenError:
            log.warning(f"Пользователь {user.user_id} заблокировал бота")
            user.active = False
            self.um.save_user(user)
        except Exception as e:
            log.error(f"Ошибка отправки {user.user_id}: {e}")

    async def _scan_for_user(self, user: UserSettings, coins: list):
        indicator = self._get_indicator(user)
        signals   = 0
        chunk     = self.config.CHUNK_SIZE

        for i in range(0, len(coins), chunk):
            batch = coins[i: i + chunk]
            dfs   = await asyncio.gather(*[self._get_candles(s, user.timeframe) for s in batch])

            for symbol, df in zip(batch, dfs):
                if df is None or len(df) < 60:
                    continue
                df_htf = await self._get_htf(symbol) if user.use_htf else None

                try:
                    sig = indicator.analyze(symbol, df, df_htf)
                except Exception as e:
                    log.debug(f"{symbol}: {e}")
                    continue

                if sig is None or sig.quality < user.min_quality:
                    continue

                if user.notify_signal:
                    await self._send_signal(user, sig)
                signals += 1

            await asyncio.sleep(0.1)

        return signals

    async def scan_all_users(self):
        active = self.um.get_active_users()
        if not active:
            return

        now = time.time()
        for user in active:
            us = self._get_us(user.user_id)
            if now - us.last_scan < user.scan_interval:
                continue
            us.last_scan = now
            log.info(f"🔍 Скан для {user.username or user.user_id} (TF={user.timeframe})")
            coins   = await self._load_coins(user.min_volume_usdt)
            signals = await self._scan_for_user(user, coins)
            log.info(f"  → Сигналов: {signals}")

    async def run_forever(self):
        log.info("🔄 Мульти-сканер запущен")
        while True:
            try:
                await self.scan_all_users()
            except Exception as e:
                log.error(f"Ошибка сканера: {e}")
            await asyncio.sleep(30)
