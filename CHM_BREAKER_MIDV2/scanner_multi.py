"""
scanner_multi.py — Мульти-пользовательский сканер
Версия 4.1 — профессиональный формат сигнала с ✅/❌ чеклистом
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


# ── Утилиты форматирования ─────────────────────────────────────────────────

def _fmt(v: float) -> str:
    """Умное форматирование цены без лишних нулей."""
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if v >= 0.001:
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _pct(entry: float, target: float) -> str:
    return f"{abs((target - entry) / entry * 100):.2f}%"


def _row(ok: bool, label: str) -> str:
    return f"{'✅' if ok else '❌'}  {label}"


# ── Главная функция формирования сигнала ──────────────────────────────────

def make_signal_text(sig: SignalResult, user: UserSettings, change_24h=None) -> str:
    NL     = "\n"
    is_long = sig.direction == "LONG"

    # Шапка направления
    if is_long:
        header   = "🟢  <b>LONG СИГНАЛ</b>"
        arrow_em = "📈"
        dir_txt  = "ЛОНГ"
    else:
        header   = "🔴  <b>SHORT СИГНАЛ</b>"
        arrow_em = "📉"
        dir_txt  = "ШОРТ"

    stars = "⭐" * sig.quality + "☆" * (5 - sig.quality)

    # Ценовые уровни
    risk = abs(sig.entry - sig.sl)
    tp1  = sig.entry + risk * user.tp1_rr if is_long else sig.entry - risk * user.tp1_rr
    tp2  = sig.entry + risk * user.tp2_rr if is_long else sig.entry - risk * user.tp2_rr
    tp3  = sig.entry + risk * user.tp3_rr if is_long else sig.entry - risk * user.tp3_rr

    # 24h данные
    ch24_line = ""
    if change_24h:
        ch  = change_24h.get("change_pct", 0)
        vol = change_24h.get("volume_usdt", 0)
        em  = "🔺" if ch > 0 else "🔻"
        if vol >= 1_000_000_000:
            vol_str = f"${vol/1_000_000_000:.1f}B"
        elif vol >= 1_000_000:
            vol_str = f"${vol/1_000_000:.1f}M"
        else:
            vol_str = f"${vol:,.0f}"
        ch24_line = f"📅  24h:  {em} {ch:+.2f}%   Объём: {vol_str}"

    # ── SMC чеклист ───────────────────────────────────

    # SMC структура
    ok_bos  = bool(getattr(sig, "has_bos", False))
    ok_ob   = bool(getattr(sig, "has_ob", False))
    ok_fvg  = bool(getattr(sig, "has_fvg", False))
    ok_liq  = bool(getattr(sig, "has_liq_sweep", False))

    row_bos = _row(ok_bos, "BOS — пробой структуры рынка")
    row_ob  = _row(ok_ob,  "Order Block — зона интереса SMC")
    row_fvg = _row(ok_fvg, "FVG — дисбаланс / имбаланс")
    row_liq = _row(ok_liq, "Sweep ликвидности (ложный пробой)")

    # ── Новые v4.2 ────────────────────────────────────
    ok_choch   = bool(getattr(sig, "has_choch", False))
    ok_conf    = bool(getattr(sig, "htf_confluence", False))
    session_nm = getattr(sig, "session_name", "")
    ok_sess    = bool(getattr(sig, "session_prime", False))

    row_choch  = _row(ok_choch, "CHOCH — смена характера структуры")
    row_conf   = _row(ok_conf,  "Daily Confluence — уровень дневного TF")
    row_sess   = _row(ok_sess,  f"Сессия: {session_nm}" if session_nm else "Сессия: нет данных")

    # RSI
    rsi_val = getattr(sig, "rsi", 50.0)
    rsi_os  = getattr(user, "rsi_os", 40)
    rsi_ob  = getattr(user, "rsi_ob", 60)
    if is_long:
        ok_rsi  = rsi_val < rsi_os
        rsi_lbl = f"RSI {rsi_val:.1f} — {'перепродан 🔽' if ok_rsi else 'нейтральный'}"
    else:
        ok_rsi  = rsi_val > rsi_ob
        rsi_lbl = f"RSI {rsi_val:.1f} — {'перекуплен 🔼' if ok_rsi else 'нейтральный'}"
    row_rsi = _row(ok_rsi, rsi_lbl)

    # Объём
    vol_ratio = getattr(sig, "volume_ratio", 1.0)
    ok_vol    = vol_ratio >= 1.2
    row_vol   = _row(ok_vol, f"Объём: x{vol_ratio:.1f} выше среднего" if ok_vol else f"Объём: x{vol_ratio:.1f} — слабый")

    # Паттерн
    pattern = getattr(sig, "pattern", "") or ""
    ok_pat  = bool(pattern)
    row_pat = _row(ok_pat, f"Паттерн: {pattern}" if ok_pat else "Паттерн: не подтверждён")

    # HTF тренд
    trend_htf = getattr(sig, "trend_htf", "") or ""
    if is_long:
        ok_htf = "бычий" in trend_htf.lower() or "bull" in trend_htf.lower()
    else:
        ok_htf = "медвежий" in trend_htf.lower() or "bear" in trend_htf.lower()
    if trend_htf:
        row_htf = _row(ok_htf, f"HTF тренд: {trend_htf}")
    else:
        row_htf = _row(False, "HTF тренд: нет данных")
        ok_htf  = False

    # ── Итоговый рейтинг совпадений ───────────────────
    conditions  = [ok_bos, ok_ob, ok_fvg, ok_liq, ok_rsi, ok_vol, ok_pat, ok_htf, ok_choch, ok_conf, ok_sess]
    matched     = sum(conditions)
    total_conds = len(conditions)
    bar_filled  = "▓" * matched
    bar_empty   = "░" * (total_conds - matched)
    score_line  = f"[{bar_filled}{bar_empty}]  {matched}/{total_conds} условий"

    # ── Метка риска ───────────────────────────────────
    if sig.quality >= 5:
        risk_mark = "🟢 НИЗКИЙ"
    elif sig.quality >= 4:
        risk_mark = "🟡 УМЕРЕННЫЙ"
    elif sig.quality >= 3:
        risk_mark = "🟠 СРЕДНИЙ"
    else:
        risk_mark = "🔴 ВЫСОКИЙ"

    trend_local = getattr(sig, "trend_local", "") or "—"
    break_type  = getattr(sig, "breakout_type", "") or dir_txt

    # ── Сборка ────────────────────────────────────────
    parts = [
        header,
        f"       {stars}",
        "",
        f"💎  <b>{sig.symbol}</b>   {arrow_em}  <i>{break_type}</i>",
        "",
        "┌─ ТОРГОВЫЙ ПЛАН ─────────────────────",
        f"│  💰 Вход:    <code>{_fmt(sig.entry)}</code>",
        f"│  🛑 Стоп:    <code>{_fmt(sig.sl)}</code>  (-{sig.risk_pct:.2f}%)",
        "│",
        f"│  🎯 Цель 1:  <code>{_fmt(tp1)}</code>  (+{_pct(sig.entry, tp1)})  [{user.tp1_rr}R]",
        f"│  🎯 Цель 2:  <code>{_fmt(tp2)}</code>  (+{_pct(sig.entry, tp2)})  [{user.tp2_rr}R]",
        f"│  🏆 Цель 3:  <code>{_fmt(tp3)}</code>  (+{_pct(sig.entry, tp3)})  [{user.tp3_rr}R]",
        "│",
        f"│  ⚠️  Риск: {risk_mark}",
        "└──────────────────────────────────────",
        "",
        "┌─ SMC СТРУКТУРА ─────────────────────",
        f"│  {row_bos}",
        f"│  {row_ob}",
        f"│  {row_fvg}",
        f"│  {row_liq}",
        f"│  {row_choch}",
        "├─ ТЕХНИЧЕСКИЕ ФИЛЬТРЫ ───────────────",
        f"│  {row_rsi}",
        f"│  {row_vol}",
        f"│  {row_pat}",
        f"│  {row_htf}",
        "├─ КОНТЕКСТ РЫНКА ────────────────────",
        f"│  {row_conf}",
        f"│  {row_sess}",
        "├─ ИТОГ ──────────────────────────────",
        f"│  {score_line}",
        "└──────────────────────────────────────",
        "",
        f"📊  Тренд (TF):  <b>{trend_local}</b>",
    ]

    if ch24_line:
        parts.append(ch24_line)

    parts += [
        "",
        "⚡ <i>CHM Laboratory — CHM BREAKER</i>",
    ]

    return NL.join(parts)


# ── Клавиатура под сигналом ────────────────────────────────────────────────

def make_signal_keyboard(trade_id: str):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    def btn(text: str, cb: str) -> list:
        return [InlineKeyboardButton(text=text, callback_data=cb)]

    return InlineKeyboardMarkup(inline_keyboard=[
        btn("🎯 TP1",        f"res_TP1_{trade_id}"),
        btn("🎯 TP2",        f"res_TP2_{trade_id}"),
        btn("🏆 TP3",        f"res_TP3_{trade_id}"),
        btn("❌ Стоп-лосс", f"res_SL_{trade_id}"),
        btn("⏭ Пропустить", f"res_SKIP_{trade_id}"),
    ])


# ── UserScanner ────────────────────────────────────────────────────────────

class UserScanner:
    def __init__(self, user_id: int):
        self.user_id   = user_id
        self.last_scan = 0.0


# ── MultiScanner ──────────────────────────────────────────────────────────

class MultiScanner:

    def __init__(self, config: Config, bot: Bot, um: UserManager):
        self.config  = config
        self.bot     = bot
        self.um      = um
        self.fetcher = BinanceFetcher()

        self._candle_cache:    dict = {}
        self._htf_cache:       dict = {}
        self._coins_cache:     list = []
        self._coins_loaded_at: float = 0.0
        self._user_scanners:   dict = {}
        self._indicators:      dict = {}
        self._trend_cache:     dict = {}
        self._perf = {"cycles": 0, "signals": 0, "api_calls": 0}

    def get_trend(self) -> dict:
        return self._trend_cache

    def get_perf(self) -> dict:
        total = len(self._candle_cache)
        return {**self._perf, "cache": {"size": total, "ratio": 0}}

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
        cfg.USE_SESSION_FILTER = user.use_session
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
        self._perf["api_calls"] += 1
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
        text       = make_signal_text(sig, user, change_24h)

        # Сохраняем сделку для трекинга
        import hashlib
        import database as db
        trade_id = hashlib.md5(
            f"{user.user_id}{sig.symbol}{sig.direction}{int(time.time())}".encode()
        ).hexdigest()[:12]

        try:
            await db.db_save_trade(
                trade_id  = trade_id,
                user_id   = user.user_id,
                symbol    = sig.symbol,
                direction = sig.direction,
                entry     = sig.entry,
                sl        = sig.sl,
                tp1_rr    = user.tp1_rr,
                tp2_rr    = user.tp2_rr,
                tp3_rr    = user.tp3_rr,
                quality   = sig.quality,
                timeframe = user.timeframe,
            )
        except Exception as e:
            log.debug(f"db_save_trade: {e}")

        kb = make_signal_keyboard(trade_id)

        try:
            await self.bot.send_message(
                user.user_id, text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            user.signals_received += 1
            self.um.save_user(user)
            self._perf["signals"] += 1
            log.info(
                f"✅ Сигнал → {user.username or user.user_id}: "
                f"{sig.symbol} {sig.direction} ⭐{sig.quality}"
            )
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

        # Фильтр сессий — если включён, пропускаем азиатскую/ночную сессию
        if user.use_session:
            from indicator import CHMIndicator as _Ind
            session_name, session_prime = _Ind._get_session()
            if not session_prime:
                log.info(
                    f"⏸ {user.username or user.user_id}: "
                    f"сессия '{session_name}' — скип (не прайм)"
                )
                return 0

        for i in range(0, len(coins), chunk):
            batch = coins[i: i + chunk]
            dfs   = await asyncio.gather(
                *[self._get_candles(s, user.timeframe) for s in batch]
            )

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
        self._perf["cycles"] += 1

        for user in active:
            us = self._get_us(user.user_id)
            if now - us.last_scan < user.scan_interval:
                continue
            us.last_scan = now
            log.info(f"🔍 Скан: {user.username or user.user_id} (TF={user.timeframe})")
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
