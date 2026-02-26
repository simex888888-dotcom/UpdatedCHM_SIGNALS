# scanner_multi.py — v4.8 (multi-scan: LONG + SHORT независимо)
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


# ─────────────────────────── helpers ────────────────────────────

def fmt(v: float) -> str:
    if v >= 1000:   return f"{v:,.2f}"
    if v >= 1:      return f"{v:.4f}".rstrip("0").rstrip(".")
    if v >= 0.001:  return f"{v:.6f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def pct(entry: float, target: float) -> str:
    return f"{abs(target - entry) / entry * 100:.1f}%"


def risk_level(quality: int) -> str:
    if quality >= 5: return "low"
    if quality >= 4: return "low"
    if quality >= 3: return "medium"
    return "high"


# ─────────────────────────── signal text ────────────────────────

def make_signal_text(sig: SignalResult, user: UserSettings, change24h=None) -> str:
    is_long = sig.direction == "LONG"
    risk = abs(sig.entry - sig.sl)
    tp1 = sig.entry + risk * user.tp1rr if is_long else sig.entry - risk * user.tp1rr
    tp2 = sig.entry + risk * user.tp2rr if is_long else sig.entry - risk * user.tp2rr
    tp3 = sig.entry + risk * user.tp3rr if is_long else sig.entry - risk * user.tp3rr

    header  = "📈 <b>LONG</b>"  if is_long else "📉 <b>SHORT</b>"
    stars   = "⭐" * sig.quality + "☆" * (5 - sig.quality)
    sl_sign = "▼" if is_long else "▲"
    tp_sign = "▲" if is_long else "▼"

    if   sig.quality == 5: risk_mark = "🟢"
    elif sig.quality == 4: risk_mark = "🟡"
    elif sig.quality == 3: risk_mark = "🟠"
    else:                  risk_mark = "🔴"

    sess_nm = getattr(sig, "sessionname", "") or ""
    tf = getattr(user, "_scan_tf", user.timeframe)   # активный TF при скане

    lines = [
        f"{header} <b>{sig.symbol}</b>  {stars}",
        "",
        f"  ● <code>{fmt(sig.entry)}</code>",
        f"  {sl_sign} SL <code>{fmt(sig.sl)}</code>  <i>{sl_sign}{pct(sig.entry, sig.sl)}</i>",
        "",
        f"  {tp_sign} 1 <code>{fmt(tp1)}</code>  <i>{tp_sign}{pct(sig.entry, tp1)}  {user.tp1rr}R</i>",
        f"  {tp_sign} 2 <code>{fmt(tp2)}</code>  <i>{tp_sign}{pct(sig.entry, tp2)}  {user.tp2rr}R</i>",
        f"  {tp_sign} 3 <code>{fmt(tp3)}</code>  <i>{tp_sign}{pct(sig.entry, tp3)}  {user.tp3rr}R</i>",
        "",
        f"  {risk_mark}  TF: {tf}  {sess_nm}",
    ]

    if change24h:
        ch  = change24h.get("changepct", 0)
        vol = change24h.get("volumeusdt", 0)
        em  = "🟢" if ch > 0 else "🔴"
        if   vol >= 1_000_000_000: vol_str = f"{vol/1_000_000_000:.1f}B"
        elif vol >= 1_000_000:     vol_str = f"{vol/1_000_000:.1f}M"
        else:                      vol_str = f"{vol:,.0f}"
        lines += ["", f"  24h {em} {ch:.2f}%  {vol_str}"]

    lines += ["", "<i>CHM Laboratory · CHM BREAKER</i>"]
    return "\n".join(lines)


# ─────────────────────────── conditions ─────────────────────────

def eval_conditions(sig: SignalResult, user: UserSettings) -> tuple:
    """
    Возвращает (all_conds, matched, total).
    Учитывает какие условия включены у пользователя.
    """
    is_long   = sig.direction == "LONG"
    rsi_val   = getattr(sig, "rsi",          50.0)
    vol_ratio = getattr(sig, "volumeratio",   1.0)
    pattern   = getattr(sig, "pattern",       "") or ""
    trend_htf = getattr(sig, "trendhtf",      "") or ""
    sess_nm   = getattr(sig, "sessionname",   "") or ""

    # ── SMC ──
    ok_bos   = bool(getattr(sig, "hasbos",       False))
    ok_ob    = bool(getattr(sig, "hasob",         False))
    ok_fvg   = bool(getattr(sig, "hasfvg",        False))
    ok_liq   = bool(getattr(sig, "hasliqsweep",   False))
    ok_choch = bool(getattr(sig, "haschoch",      False))

    # ── Tech filters ──
    ok_rsi = rsi_val < user.rsios  if is_long else rsi_val > user.rsiob
    ok_vol = vol_ratio >= user.volmult
    ok_pat = bool(pattern)
    ok_htf = ("бык" in trend_htf.lower() or "bull" in trend_htf.lower())              if is_long else              ("медв" in trend_htf.lower() or "bear" in trend_htf.lower())

    # ── Context ──
    ok_conf = bool(getattr(sig, "htfconfluence",  False))
    ok_sess = bool(getattr(sig, "sessionprime",   False))

    # ── Labels — всегда реальное значение ──
    rsi_lbl  = f"RSI {rsi_val:.1f}"
    vol_lbl  = f"Vol {vol_ratio:.1f}x"
    pat_lbl  = f"🕯 {pattern}" if pattern else "Pattern"
    htf_lbl  = f"HTF {trend_htf}" if trend_htf else "HTF"
    sess_lbl = f"⏰ {sess_nm}"   if sess_nm   else "Session"

    all_conds = [
        # (ok,      label,          enabled)          index
        (ok_bos,   "BOS",          user.smcusebos),   # 0
        (ok_ob,    "Order Block",  user.smcuseob),    # 1
        (ok_fvg,   "FVG",          user.smcusefvg),   # 2
        (ok_liq,   "Sweep",        user.smcusesweep), # 3
        (ok_choch, "CHOCH",        user.smcusechoch), # 4
        (ok_rsi,   rsi_lbl,        user.usersi),      # 5
        (ok_vol,   vol_lbl,        user.usevolume),   # 6
        (ok_pat,   pat_lbl,        user.usepattern),  # 7
        (ok_htf,   htf_lbl,        user.usehtf),      # 8
        (ok_conf,  "Daily Conf",   user.smcuseconf),  # 9
        (ok_sess,  sess_lbl,       user.usesession),  # 10
    ]

    matched = sum(ok  for ok, lbl, enabled in all_conds if enabled)
    total   = sum(1   for ok, lbl, enabled in all_conds if enabled)
    return all_conds, matched, total


def make_checklist_text(sig: SignalResult, user: UserSettings) -> str:
    is_long = sig.direction == "LONG"
    all_conds, matched, total = eval_conditions(sig, user)

    smc_group  = all_conds[0:5]
    tech_group = all_conds[5:9]
    ctx_group  = all_conds[9:11]

    bar = "█" * matched + "░" * (total - matched)

    def row(ok, lbl, enabled):
        if not enabled:
            return f"  <i>{lbl}</i>"
        return f"  {'✅' if ok else '❌'} {lbl}"

    direction = "LONG 📈" if is_long else "SHORT 📉"
    lines = [
        f"<b>{sig.symbol} {direction}</b>",
        "",
        "<b>▸ SMC</b>",
    ]
    for ok, lbl, enabled in smc_group:
        lines.append(row(ok, lbl, enabled))

    lines += ["", "<b>▸ Фильтры</b>"]
    for ok, lbl, enabled in tech_group:
        lines.append(row(ok, lbl, enabled))

    lines += ["", "<b>▸ Контекст</b>"]
    for ok, lbl, enabled in ctx_group:
        lines.append(row(ok, lbl, enabled))

    lines += ["", f"<code>{bar}  {matched}/{total}</code>"]
    return "\n".join(lines)


def make_signal_keyboard(trade_id: str, matched: int, total: int) -> InlineKeyboardMarkup:
    def b(text, cb):
        return InlineKeyboardButton(text=text, callback_data=cb)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(f"📋 {matched}/{total}", f"sigchecks{trade_id}"),
         b("📊", f"sigchart{trade_id}")],
        [b("🙈 Скрыть", f"sighide{trade_id}"),
         b("📈 Статы", f"sigstats{trade_id}")],
    ])


def count_conditions(sig: SignalResult, user: UserSettings) -> tuple:
    _, matched, total = eval_conditions(sig, user)
    return matched, total


# ─────────────────────────── helpers для get_indicator ──────────

def _apply_user_cfg(cfg: Config, user: UserSettings, direction: str):
    """
    Применяет настройки пользователя к cfg.
    Если direction == LONG — берёт longcfg (если есть), иначе shortcfg.
    """
    # Получаем per-direction cfg если есть
    if direction == "LONG":
        dcfg = user.getlongcfg() if hasattr(user, "getlongcfg") else None
    else:
        dcfg = user.getshortcfg() if hasattr(user, "getshortcfg") else None

    def v(field):
        """Берём из dcfg если есть, иначе из user."""
        if dcfg and hasattr(dcfg, field):
            return getattr(dcfg, field)
        return getattr(user, field)

    cfg.TIMEFRAME        = v("longtf")   if direction == "LONG"  else v("shorttf")
    cfg.PIVOTSTRENGTH    = v("pivotstrength")
    cfg.MAXLEVELAGE      = v("maxlevelage")
    cfg.ZONEBUFFER       = v("zonebuffer")
    cfg.EMAFAST          = v("emafast")
    cfg.EMASLOW          = v("emaslow")
    cfg.HTFEMAPERIOD     = v("htfemaperiod")
    cfg.RSIPERIOD        = v("rsiperiod")
    cfg.RSIOB            = v("rsiob")
    cfg.RSIOS            = v("rsios")
    cfg.VOLMULT          = v("volmult")
    cfg.VOLLEN           = v("vollen")
    cfg.ATRPERIOD        = v("atrperiod")
    cfg.ATRMULT          = v("atrmult")
    cfg.MAXRISKPCT       = v("maxriskpct")
    cfg.TP1RR            = v("tp1rr")
    cfg.TP2RR            = v("tp2rr")
    cfg.TP3RR            = v("tp3rr")
    cfg.COOLDOWNBARS     = v("cooldownbars")
    cfg.USERSIFILTER     = v("usersi")
    cfg.USEVOLUMEFILTER  = v("usevolume")
    cfg.USEPATTERNFILTER = v("usepattern")
    cfg.USEHTFFILTER     = v("usehtf")
    cfg.USESESSIONFILTER = v("usesession")
    cfg.SMCUSEBOS        = v("smcusebos")
    cfg.SMCUSEOB         = v("smcuseob")
    cfg.SMCUSEFVG        = v("smcusefvg")
    cfg.SMCUSESWEEP      = v("smcusesweep")
    cfg.SMCUSECHOCH      = v("smcusechoch")
    cfg.SMCUSECONF       = v("smcuseconf")
    cfg.ANALYSISMODE     = v("analysismode")


# ─────────────────────────── scanners ───────────────────────────

class UserScanner:
    def __init__(self, user_id: int):
        self.user_id        = user_id
        self.last_scan_long  = 0.0
        self.last_scan_short = 0.0
        self.last_scan_both  = 0.0


class MultiScanner:
    def __init__(self, config: Config, bot: Bot, um: UserManager):
        self.config          = config
        self.bot             = bot
        self.um              = um
        self.fetcher         = BinanceFetcher()
        self.candle_cache:   dict = {}
        self.htf_cache:      dict = {}
        self.coins_cache:    list = []
        self.coins_loaded_at: float = 0.0
        self.user_scanners:  dict = {}
        self.indicators:     dict = {}   # (userid, direction) → CHMIndicator
        self.trend_cache:    dict = {}
        self.sig_cache:      dict = {}   # trade_id → (sig, user)
        self.perf = {"cycles": 0, "signals": 0, "apicalls": 0}

    def get_trend(self) -> dict:
        return self.trend_cache

    def get_perf(self) -> dict:
        total = len(self.candle_cache)
        return self.perf, {"cache_size": total, "ratio": 0}

    def get_sig_cache(self) -> dict:
        return self.sig_cache

    # ── Trend BTC/ETH ──────────────────────────────────────────

    async def update_trend(self):
        result = {}
        for symbol in ("BTCUSDT", "ETHUSDT"):
            sym_data = {}
            for tf, label in [("1h", "1h"), ("4h", "4h"), ("1D", "1D")]:
                try:
                    df = await self.fetcher.get_candles(symbol, tf, limit=250)
                    if df is None or len(df) < 60:
                        sym_data[label] = {"emoji": "➖", "trend": "—"}
                        continue
                    close  = df["close"]
                    ema50  = CHMIndicator.ema(close, 50).iloc[-1]
                    ema200 = CHMIndicator.ema(close, 200).iloc[-1]
                    c_now  = close.iloc[-1]
                    if c_now > ema50 and ema50 > ema200:
                        sym_data[label] = {"emoji": "🟢", "trend": "Бычий"}
                    elif c_now < ema50 and ema50 < ema200:
                        sym_data[label] = {"emoji": "🔴", "trend": "Медвежий"}
                    else:
                        sym_data[label] = {"emoji": "🟡", "trend": "Нейтральный"}
                except Exception:
                    sym_data[label] = {"emoji": "➖", "trend": "—"}
            key = "BTC" if "BTC" in symbol else "ETH"
            result[key] = sym_data
        self.trend_cache = result

    # ── User scanner ────────────────────────────────────────────

    def get_us(self, user_id: int) -> UserScanner:
        if user_id not in self.user_scanners:
            self.user_scanners[user_id] = UserScanner(user_id)
        return self.user_scanners[user_id]

    # ── Indicator factory (per direction) ───────────────────────

    def get_indicator(self, user: UserSettings, direction: str) -> CHMIndicator:
        key = (user.userid, direction)
        cfg = self.config
        _apply_user_cfg(cfg, user, direction)
        if key not in self.indicators:
            self.indicators[key] = CHMIndicator(cfg)
        return self.indicators[key]

    # ── Coin list ───────────────────────────────────────────────

    async def load_coins(self, min_vol: float) -> list:
        now = time.time()
        if self.coins_cache and now - self.coins_loaded_at < 3600 * 6:
            return self.coins_cache
        coins = await self.fetcher.get_all_usdt_pairs(
            min_volume_usdt=min_vol,
            blacklist=self.config.AUTOBLACKLIST,
        )
        if not coins:
            coins = self.config.COINS
        self.coins_cache      = coins
        self.coins_loaded_at  = now
        log.info(f"Монет загружено: {len(coins)}")
        return coins

    # ── Candles cache ───────────────────────────────────────────

    async def get_candles(self, symbol: str, tf: str):
        key    = f"{symbol}{tf}"
        now    = time.time()
        cached = self.candle_cache.get(key)
        if cached and now - cached[1] < 60:
            return cached[0]
        df = await self.fetcher.get_candles(symbol, tf, limit=300)
        if df is not None:
            self.candle_cache[key] = (df, now)
            self.perf["apicalls"] += 1
        return df

    async def get_htf(self, symbol: str):
        key    = f"{symbol}1d"
        now    = time.time()
        cached = self.htf_cache.get(key)
        if cached and now - cached[1] < 3600:
            return cached[0]
        df = await self.fetcher.get_candles(symbol, "1D", limit=100)
        if df is not None:
            self.htf_cache[key] = (df, now)
        return df

    # ── Send signal ─────────────────────────────────────────────

    async def send_signal(self, user: UserSettings, sig: SignalResult, tf: str):
        change24h = await self.fetcher.get_24h_change(sig.symbol)

        # Временно сохраняем активный TF в user чтобы make_signal_text показал его
        user._scan_tf = tf
        text = make_signal_text(sig, user, change24h)

        trade_id = hashlib.md5(
            f"{user.userid}{sig.symbol}{sig.direction}{int(time.time())}".encode()
        ).hexdigest()[:12]

        import database as db
        is_long = sig.direction == "LONG"
        risk    = abs(sig.entry - sig.sl)
        try:
            await db.db_add_trade(
                trade_id=trade_id,
                user_id=user.userid,
                symbol=sig.symbol,
                direction=sig.direction,
                entry=sig.entry,
                sl=sig.sl,
                tp1=sig.entry + risk * user.tp1rr if is_long else sig.entry - risk * user.tp1rr,
                tp2=sig.entry + risk * user.tp2rr if is_long else sig.entry - risk * user.tp2rr,
                tp3=sig.entry + risk * user.tp3rr if is_long else sig.entry - risk * user.tp3rr,
                tp1rr=user.tp1rr,
                tp2rr=user.tp2rr,
                tp3rr=user.tp3rr,
                quality=sig.quality,
                timeframe=tf,
                created_at=time.time(),
            )
        except Exception as e:
            log.debug(f"db_add_trade: {e}")

        self.sig_cache[trade_id] = (sig, user)
        if len(self.sig_cache) > 500:
            for k in list(self.sig_cache.keys())[:100]:
                del self.sig_cache[k]

        matched, total = count_conditions(sig, user)

        log.debug(
            f"[CONDITIONS] {sig.symbol} {sig.direction} {tf} | "
            f"bos={user.smcusebos} ob={user.smcuseob} fvg={user.smcusefvg} "
            f"sweep={user.smcusesweep} choch={user.smcusechoch} "
            f"rsi={user.usersi} vol={user.usevolume} pat={user.usepattern} "
            f"htf={user.usehtf} conf={user.smcuseconf} sess={user.usesession} "
            f"=> {matched}/{total}"
        )

        kb = make_signal_keyboard(trade_id, matched, total)
        try:
            await self.bot.send_message(
                user.userid, text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            user.signalsreceived += 1
            await self.um.saveuser(user)
            self.perf["signals"] += 1
            log.info(
                f"✅ {user.username or user.userid} | "
                f"{sig.symbol} {sig.direction} Q{sig.quality} TF={tf} | {matched}/{total}"
            )
        except TelegramForbiddenError:
            log.warning(f"Бот заблокирован пользователем {user.userid}")
            user.active = False
            await self.um.saveuser(user)
        except Exception as e:
            log.error(f"send_message {user.userid}: {e}")

    # ── Scan one direction ───────────────────────────────────────

    async def _scan_direction(
        self,
        user: UserSettings,
        coins: list,
        direction: str,    # "LONG" | "SHORT"
        tf: str,
    ) -> int:
        """
        Сканирует все монеты в одном направлении с нужным TF.
        Возвращает кол-во отправленных сигналов.
        """
        indicator = self.get_indicator(user, direction)
        signals   = 0
        chunk     = self.config.CHUNKSIZE

        # Session filter
        if user.usesession:
            _, session_prime = CHMIndicator.get_session()
            if not session_prime:
                return 0

        for i in range(0, len(coins), chunk):
            batch = coins[i: i + chunk]
            dfs   = await asyncio.gather(
                *[self.get_candles(s, tf) for s in batch]
            )
            for symbol, df in zip(batch, dfs):
                if df is None or len(df) < 60:
                    continue
                df_htf = await self.get_htf(symbol) if user.usehtf else None
                try:
                    sig = indicator.analyze(symbol, df, df_htf)
                except Exception as e:
                    log.debug(f"{symbol}: {e}")
                    continue

                if sig is None:
                    continue

                # Фильтр направления
                if sig.direction != direction:
                    continue

                if sig.quality < user.minquality:
                    continue
                if user.maxsignalriskpct > 0 and sig.riskpct > user.maxsignalriskpct:
                    continue

                if user.minrisklevel != "all":
                    rl = risk_level(sig.quality)
                    if user.minrisklevel == "low"    and rl != "low":              continue
                    if user.minrisklevel == "medium" and rl not in ("low", "medium"): continue

                if user.notifysignal:
                    await self.send_signal(user, sig, tf)
                    signals += 1

            await asyncio.sleep(self.config.CHUNKSLEEP)
        return signals

    # ── Scan for user (multi-mode) ───────────────────────────────

    async def scan_for_user(self, user: UserSettings, coins: list) -> int:
        """
        Режимы сканирования:
          scanmode = "both"  → один проход, оба направления, общий TF
          longactive = True  → сканирует только LONG на longtf
          shortactive = True → сканирует только SHORT на shorttf
          active = True (scanmode both) → оба направления на timeframe
        """
        signals = 0
        mode    = getattr(user, "scanmode", "both")

        if mode == "both" and getattr(user, "active", False):
            # Оба направления — один TF
            tf = user.timeframe
            s_long  = await self._scan_direction(user, coins, "LONG",  tf)
            s_short = await self._scan_direction(user, coins, "SHORT", tf)
            signals = s_long + s_short

        else:
            # LONG — отдельный TF и интервал
            if getattr(user, "longactive", False):
                tf = getattr(user, "longtf", user.timeframe)
                # Применяем longcfg к индикатору
                if hasattr(user, "getlongcfg"):
                    lcfg = user.getlongcfg()
                    if lcfg:
                        for field in vars(lcfg):
                            if hasattr(user, field):
                                setattr(user, field, getattr(lcfg, field))
                signals += await self._scan_direction(user, coins, "LONG", tf)

            # SHORT — отдельный TF и интервал
            if getattr(user, "shortactive", False):
                tf = getattr(user, "shorttf", user.timeframe)
                if hasattr(user, "getshortcfg"):
                    scfg = user.getshortcfg()
                    if scfg:
                        for field in vars(scfg):
                            if hasattr(user, field):
                                setattr(user, field, getattr(scfg, field))
                signals += await self._scan_direction(user, coins, "SHORT", tf)

        return signals

    # ── Scan all users ───────────────────────────────────────────

    async def scan_all_users(self):
        active = await self.um.getactiveusers()
        if not active:
            return

        now = time.time()
        self.perf["cycles"] += 1

        try:
            await self.update_trend()
        except Exception as e:
            log.debug(f"trend update error: {e}")

        for user in active:
            us   = self.get_us(user.userid)
            mode = getattr(user, "scanmode", "both")

            # Определяем нужно ли сканировать сейчас
            should_scan = False

            if mode == "both" and getattr(user, "active", False):
                if now - us.last_scan_both >= user.scaninterval:
                    us.last_scan_both = now
                    should_scan = True
            else:
                long_interval  = getattr(user, "longinterval",  user.scaninterval)
                short_interval = getattr(user, "shortinterval", user.scaninterval)
                if getattr(user, "longactive",  False) and now - us.last_scan_long  >= long_interval:
                    us.last_scan_long  = now
                    should_scan = True
                if getattr(user, "shortactive", False) and now - us.last_scan_short >= short_interval:
                    us.last_scan_short = now
                    should_scan = True

            if not should_scan:
                continue

            log.info(
                f"▶ {user.username or user.userid}  "
                f"mode={mode}  TF={user.timeframe}"
            )
            coins   = await self.load_coins(user.minvolumeusdt)
            signals = await self.scan_for_user(user, coins)
            log.info(f"  └ сигналов отправлено: {signals}")

    # ── Main loop ────────────────────────────────────────────────

    async def run_forever(self):
        log.info("🚀 MultiScanner запущен — v4.8")
        while True:
            try:
                await self.scan_all_users()
            except Exception as e:
                log.error(f"scan_all_users: {e}")
            await asyncio.sleep(30)
