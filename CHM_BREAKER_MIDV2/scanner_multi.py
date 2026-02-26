"""
scanner_multi.py — Мульти-пользовательский сканер v4.7
Компактный формат сигнала + 4 кнопки: Подтверждения / График / Скрыть / Статистика
"""

import asyncio
import hashlib
import logging
import time
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import Config
from usermanager import UserManager, UserSettings
from fetcher import BinanceFetcher
from indicator import CHMIndicator, SignalResult

log = logging.getLogger("CHM.MultiScanner")


# ── Утилиты форматирования ────────────────────────────────────────────────

def _fmt(v: float) -> str:
    if v >= 1000:  return f"{v:,.2f}"
    if v >= 1:     return f"{v:.4f}".rstrip("0").rstrip(".")
    if v >= 0.001: return f"{v:.6f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")

def _pct(entry: float, target: float) -> str:
    return f"{abs((target - entry) / entry * 100):.1f}%"


def _risk_level(quality: int) -> str:
    """Уровень риска по качеству сигнала (совпадает с отображением в сигнале)."""
    if quality >= 5: return "low"
    if quality >= 4: return "low"
    if quality >= 3: return "medium"
    return "high"


# ── Компактный текст сигнала ──────────────────────────────────────────────

def make_signal_text(sig: SignalResult, user: UserSettings, change_24h=None) -> str:
    is_long = sig.direction == "LONG"
    risk    = abs(sig.entry - sig.sl)
    tp1     = sig.entry + risk * user.tp1_rr if is_long else sig.entry - risk * user.tp1_rr
    tp2     = sig.entry + risk * user.tp2_rr if is_long else sig.entry - risk * user.tp2_rr
    tp3     = sig.entry + risk * user.tp3_rr if is_long else sig.entry - risk * user.tp3_rr

    header  = "🟢 <b>LONG</b>"  if is_long else "🔴 <b>SHORT</b>"
    stars   = "⭐" * sig.quality + "☆" * (5 - sig.quality)
    sl_sign = "−"   # стоп всегда со знаком минус (убыток)
    tp_sign = "+"   # цели всегда со знаком плюс (прибыль)

    if sig.quality >= 5:   risk_mark = "🟢 Низкий"
    elif sig.quality >= 4: risk_mark = "🟡 Умеренный"
    elif sig.quality >= 3: risk_mark = "🟠 Средний"
    else:                  risk_mark = "🔴 Высокий"

    session_nm = getattr(sig, "session_name", "") or "—"

    lines = [
        f"{header} · <b>{sig.symbol}</b>",
        stars,
        "",
        f"💰 Вход       <code>{_fmt(sig.entry)}</code>",
        f"🛑 Стоп       <code>{_fmt(sig.sl)}</code>  <i>{sl_sign}{_pct(sig.entry, sig.sl)}</i>",
        "",
        f"🎯 Цель 1     <code>{_fmt(tp1)}</code>  <i>{tp_sign}{_pct(sig.entry, tp1)} · {user.tp1_rr}R</i>",
        f"🎯 Цель 2     <code>{_fmt(tp2)}</code>  <i>{tp_sign}{_pct(sig.entry, tp2)} · {user.tp2_rr}R</i>",
        f"🏆 Цель 3     <code>{_fmt(tp3)}</code>  <i>{tp_sign}{_pct(sig.entry, tp3)} · {user.tp3_rr}R</i>",
        "",
        f"⚠️ Риск       {risk_mark}",
        f"⏱ Таймфрейм  {user.timeframe}",
        f"🕐 Сессия     {session_nm}",
    ]

    if change_24h:
        ch  = change_24h.get("change_pct", 0)
        vol = change_24h.get("volume_usdt", 0)
        em  = "🔺" if ch > 0 else "🔻"
        if vol >= 1_000_000_000: vol_str = f"${vol/1_000_000_000:.1f}B"
        elif vol >= 1_000_000:   vol_str = f"${vol/1_000_000:.1f}M"
        else:                    vol_str = f"${vol:,.0f}"
        lines += ["", f"📅 24h  {em} {ch:+.2f}%   {vol_str}"]

    lines += ["", "⚡ <i>CHM Laboratory — CHM BREAKER</i>"]
    return "\n".join(lines)


# ── Вспомогательная: вычислить все условия с учётом флагов пользователя ───
#
# Возвращает:
#   active_conds  — list of (ok: bool, label: str, enabled: bool)
#   matched       — int, число True среди enabled
#   total         — int, число enabled условий
#
# ВАЖНО: логика определения ok_* должна быть идентична indicator.py.
# Только enabled=True условия входят в счёт matched/total.
# Disabled условия показываются в чеклисте как ⬜ и не влияют на счёт.

def _eval_conditions(sig: SignalResult, user: UserSettings) -> tuple:
    is_long    = sig.direction == "LONG"
    rsi_val    = getattr(sig, "rsi", 50.0)
    vol_ratio  = getattr(sig, "volume_ratio", 1.0)
    pattern    = getattr(sig, "pattern", "") or ""
    trend_htf  = getattr(sig, "trend_htf", "") or ""
    session_nm = getattr(sig, "session_name", "") or "—"

    # ── Вычисляем результат каждого условия ───────────────────────────────
    ok_bos   = bool(getattr(sig, "has_bos",      False))
    ok_ob    = bool(getattr(sig, "has_ob",        False))
    ok_fvg   = bool(getattr(sig, "has_fvg",       False))
    ok_liq   = bool(getattr(sig, "has_liq_sweep", False))
    ok_choch = bool(getattr(sig, "has_choch",      False))
    ok_conf  = bool(getattr(sig, "htf_confluence", False))
    ok_sess  = bool(getattr(sig, "session_prime",  False))

    # RSI: перепродан для лонга, перекуплен для шорта
    ok_rsi = (rsi_val < user.rsi_os) if is_long else (rsi_val > user.rsi_ob)

    # Объём: выше порога пользователя
    ok_vol = vol_ratio >= user.vol_mult

    # Паттерн: любая непустая строка
    ok_pat = bool(pattern)

    # HTF тренд: совпадает с направлением
    ok_htf = ("бычий" in trend_htf.lower() or "bull" in trend_htf.lower()) if is_long \
             else ("медвежий" in trend_htf.lower() or "bear" in trend_htf.lower())

    # ── Метки для чеклиста ────────────────────────────────────────────────
    rsi_lbl = (
        f"RSI {rsi_val:.1f} — {'перепродан 🔽' if is_long else 'перекуплен 🔼'}"
        if ok_rsi
        else f"RSI {rsi_val:.1f} — нейтральный"
    )
    vol_lbl  = f"Объём ×{vol_ratio:.1f} {'— выше порога' if ok_vol else '— ниже порога'}"
    pat_lbl  = f"Паттерн: {pattern}" if ok_pat else "Паттерн — не обнаружен"
    htf_lbl  = f"HTF тренд: {trend_htf}" if trend_htf else "HTF тренд — нет данных"
    sess_lbl = f"Сессия: {session_nm}"

    # ── Список (ok, label, enabled) — порядок фиксирован ─────────────────
    # enabled определяется флагами пользователя
    all_conds = [
        # SMC структура
        (ok_bos,   "BOS — пробой структуры рынка",        user.smc_use_bos),
        (ok_ob,    "Order Block — зона интереса",          user.smc_use_ob),
        (ok_fvg,   "FVG — дисбаланс / имбаланс",          user.smc_use_fvg),
        (ok_liq,   "Sweep ликвидности",                    user.smc_use_sweep),
        (ok_choch, "CHOCH — смена характера структуры",    user.smc_use_choch),
        # Технические фильтры
        (ok_rsi,   rsi_lbl,                                user.use_rsi),
        (ok_vol,   vol_lbl,                                user.use_volume),
        (ok_pat,   pat_lbl,                                user.use_pattern),
        (ok_htf,   htf_lbl,                                user.use_htf),
        # Контекст рынка
        (ok_conf,  "Daily Confluence",                     user.smc_use_conf),
        (ok_sess,  sess_lbl,                               user.use_session),
    ]

    matched = sum(ok for ok, _lbl, enabled in all_conds if enabled)
    total   = sum(1  for _ok, _lbl, enabled in all_conds if enabled)
    return all_conds, matched, total


# ── Текст чеклиста подтверждений ──────────────────────────────────────────

def make_checklist_text(sig: SignalResult, user: UserSettings) -> str:
    is_long = sig.direction == "LONG"
    all_conds, matched, total = _eval_conditions(sig, user)

    # Сортируем: сначала SMC (индексы 0-4), потом техника (5-8), потом контекст (9-10)
    smc_group  = all_conds[0:5]
    tech_group = all_conds[5:9]
    ctx_group  = all_conds[9:11]

    bar = "▓" * matched + "░" * (total - matched)

    def row(ok, lbl, enabled):
        if not enabled:
            return f"⬜  <i>{lbl} — выключено</i>"
        return ("✅" if ok else "❌") + "  " + lbl

    direction = "LONG" if is_long else "SHORT"
    lines = [
        f"📋 <b>Подтверждения · {sig.symbol} {direction}</b>",
        "",
        "<b>── SMC Структура ──</b>",
    ]
    for ok, lbl, enabled in smc_group:
        lines.append(row(ok, lbl, enabled))

    lines += ["", "<b>── Технические фильтры ──</b>"]
    for ok, lbl, enabled in tech_group:
        lines.append(row(ok, lbl, enabled))

    lines += ["", "<b>── Контекст рынка ──</b>"]
    for ok, lbl, enabled in ctx_group:
        lines.append(row(ok, lbl, enabled))

    lines += [
        "",
        f"<code>[{bar}]  {matched}/{total} активных условий</code>",
    ]
    return "\n".join(lines)


# ── Клавиатура под сигналом (4 кнопки 2×2) ───────────────────────────────

def make_signal_keyboard(trade_id: str, matched: int, total: int) -> InlineKeyboardMarkup:
    def b(text, cb): return InlineKeyboardButton(text=text, callback_data=cb)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            b(f"📋 Усл. {matched}/{total}", f"sig_checks_{trade_id}"),
            b("📊 График",                              f"sig_chart_{trade_id}"),
        ],
        [
            b("🔕 Скрыть",     f"sig_hide_{trade_id}"),
            b("📈 Статистика", f"sig_stats_{trade_id}"),
        ],
    ])


# ── Подсчёт совпадений для кнопки ─────────────────────────────────────────

def count_conditions(sig: SignalResult, user: UserSettings) -> tuple:
    """Возвращает (matched, total) — только по включённым условиям."""
    _, matched, total = _eval_conditions(sig, user)
    return matched, total


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
        self._sig_cache:       dict = {}  # trade_id → (sig, user) для хендлеров
        self._perf = {"cycles": 0, "signals": 0, "api_calls": 0}

    def get_trend(self) -> dict:
        return self._trend_cache

    def get_perf(self) -> dict:
        total = len(self._candle_cache)
        return {**self._perf, "cache": {"size": total, "ratio": 0}}

    def get_sig_cache(self) -> dict:
        return self._sig_cache

    async def _update_trend(self):
        """Обновляет тренд BTC и ETH по трём таймфреймам: 1h, 4h, 1D."""
        _ema = CHMIndicator._ema
        result = {}
        for symbol in ("BTCUSDT", "ETHUSDT"):
            sym_data = {}
            for tf, label in [("1h", "1h"), ("4h", "4h"), ("1D", "1D")]:
                try:
                    df = await self.fetcher.get_candles(symbol, tf, limit=250)
                    if df is None or len(df) < 60:
                        sym_data[label] = {"emoji": "❓", "trend": "—"}
                        continue
                    close   = df["close"]
                    ema50   = _ema(close, 50).iloc[-1]
                    ema200  = _ema(close, 200).iloc[-1]
                    c_now   = close.iloc[-1]
                    if c_now > ema50 and ema50 > ema200:
                        sym_data[label] = {"emoji": "📈", "trend": "Бычий"}
                    elif c_now < ema50 and ema50 < ema200:
                        sym_data[label] = {"emoji": "📉", "trend": "Медвежий"}
                    else:
                        sym_data[label] = {"emoji": "↔️", "trend": "Боковик"}
                except Exception:
                    sym_data[label] = {"emoji": "❓", "trend": "—"}
            key = "BTC" if "BTC" in symbol else "ETH"
            result[key] = sym_data
        self._trend_cache = result

    def _get_us(self, user_id: int) -> UserScanner:
        if user_id not in self._user_scanners:
            self._user_scanners[user_id] = UserScanner(user_id)
        return self._user_scanners[user_id]

    def _get_indicator(self, user: UserSettings) -> CHMIndicator:
        cfg = self.config
        cfg.TIMEFRAME          = user.timeframe
        cfg.PIVOT_STRENGTH     = user.pivot_strength
        cfg.MAX_LEVEL_AGE      = user.max_level_age
        cfg.ZONE_BUFFER        = user.zone_buffer
        cfg.EMA_FAST           = user.ema_fast
        cfg.EMA_SLOW           = user.ema_slow
        cfg.HTF_EMA_PERIOD     = user.htf_ema_period
        cfg.RSI_PERIOD         = user.rsi_period
        cfg.RSI_OB             = user.rsi_ob
        cfg.RSI_OS             = user.rsi_os
        cfg.VOL_MULT           = user.vol_mult
        cfg.VOL_LEN            = user.vol_len
        cfg.ATR_PERIOD         = user.atr_period
        cfg.ATR_MULT           = user.atr_mult
        cfg.MAX_RISK_PCT       = user.max_risk_pct
        cfg.TP1_RR             = user.tp1_rr
        cfg.TP2_RR             = user.tp2_rr
        cfg.TP3_RR             = user.tp3_rr
        cfg.COOLDOWN_BARS      = user.cooldown_bars
        cfg.USE_RSI_FILTER     = user.use_rsi
        cfg.USE_VOLUME_FILTER  = user.use_volume
        cfg.USE_PATTERN_FILTER = user.use_pattern
        cfg.USE_HTF_FILTER     = user.use_htf
        cfg.USE_SESSION_FILTER = user.use_session
        cfg.SMC_USE_BOS        = user.smc_use_bos
        cfg.SMC_USE_OB         = user.smc_use_ob
        cfg.SMC_USE_FVG        = user.smc_use_fvg
        cfg.SMC_USE_SWEEP      = user.smc_use_sweep
        cfg.SMC_USE_CHOCH      = user.smc_use_choch
        cfg.SMC_USE_CONF       = user.smc_use_conf
        cfg.ANALYSIS_MODE      = user.analysis_mode
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

        trade_id = hashlib.md5(
            f"{user.user_id}{sig.symbol}{sig.direction}{int(time.time())}".encode()
        ).hexdigest()[:12]

        import database as db
        is_long = sig.direction == "LONG"
        risk    = abs(sig.entry - sig.sl)
        try:
            await db.db_add_trade({
                "trade_id":   trade_id,
                "user_id":    user.user_id,
                "symbol":     sig.symbol,
                "direction":  sig.direction,
                "entry":      sig.entry,
                "sl":         sig.sl,
                "tp1":        sig.entry + risk * user.tp1_rr if is_long else sig.entry - risk * user.tp1_rr,
                "tp2":        sig.entry + risk * user.tp2_rr if is_long else sig.entry - risk * user.tp2_rr,
                "tp3":        sig.entry + risk * user.tp3_rr if is_long else sig.entry - risk * user.tp3_rr,
                "tp1_rr":     user.tp1_rr,
                "tp2_rr":     user.tp2_rr,
                "tp3_rr":     user.tp3_rr,
                "quality":    sig.quality,
                "timeframe":  user.timeframe,
                "created_at": time.time(),
            })
        except Exception as e:
            log.debug(f"db_add_trade: {e}")

        # Кэшируем сигнал для хендлеров кнопки «Подтверждения»
        self._sig_cache[trade_id] = (sig, user)
        if len(self._sig_cache) > 500:
            for k in list(self._sig_cache.keys())[:100]:
                del self._sig_cache[k]

        matched, total = count_conditions(sig, user)
        kb = make_signal_keyboard(trade_id, matched, total)

        try:
            await self.bot.send_message(
                user.user_id, text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            user.signals_received += 1
            await self.um.save(user)
            self._perf["signals"] += 1
            log.info(
                f"✅ Сигнал → {user.username or user.user_id}: "
                f"{sig.symbol} {sig.direction} ⭐{sig.quality}"
            )
        except TelegramForbiddenError:
            log.warning(f"Пользователь {user.user_id} заблокировал бота")
            user.active = False
            await self.um.save(user)
        except Exception as e:
            log.error(f"Ошибка отправки {user.user_id}: {e}")

    async def _scan_for_user(self, user: UserSettings, coins: list):
        indicator = self._get_indicator(user)
        signals   = 0
        chunk     = self.config.CHUNK_SIZE

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

                # Фильтр по риску: пропускаем если стоп слишком далеко
                if user.max_signal_risk_pct > 0 and sig.risk_pct > user.max_signal_risk_pct:
                    continue

                # Фильтр по уровню риска (low/medium/high/all)
                if user.min_risk_level != "all":
                    _rl = _risk_level(sig.quality)
                    if user.min_risk_level == "low"    and _rl != "low":    continue
                    if user.min_risk_level == "medium" and _rl not in ("low", "medium"): continue
                    if user.min_risk_level == "high":  pass  # любой уровень проходит

                if user.notify_signal:
                    await self._send_signal(user, sig)
                signals += 1

            await asyncio.sleep(self.config.CHUNK_SLEEP)

        return signals

    async def scan_all_users(self):
        active = await self.um.get_active_users()
        if not active:
            return

        now = time.time()
        self._perf["cycles"] += 1

        # Обновляем тренд BTC/ETH раз в цикл
        try:
            await self._update_trend()
        except Exception as e:
            log.debug(f"trend update error: {e}")

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
        log.info("🔄 Мульти-сканер запущен v4.7")
        while True:
            try:
                await self.scan_all_users()
            except Exception as e:
                log.error(f"Ошибка сканера: {e}")
            await asyncio.sleep(30)
