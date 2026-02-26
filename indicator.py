"""
CHM BREAKER — упрощённая логика для частых сигналов
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from config import Config

log = logging.getLogger("CHM.Indicator")


@dataclass
class SignalResult:
    symbol:        str
    direction:     str
    entry:         float
    sl:            float
    tp1:           float
    tp2:           float
    tp3:           float
    risk_pct:      float
    quality:       int
    smc_score:     int   = 0
    total_score:   int   = 0
    reasons:       list  = field(default_factory=list)
    smc_reasons:   list  = field(default_factory=list)
    rsi:           float = 50.0
    volume_ratio:  float = 1.0
    trend_local:   str   = ""
    trend_htf:     str   = ""
    pattern:       str   = ""
    breakout_type: str   = ""
    has_ob:        bool  = False
    has_fvg:       bool  = False
    has_liq_sweep: bool  = False
    has_bos:       bool  = False


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}   # symbol -> bar_idx последнего сигнала

    # ── Технические инструменты ─────────────────────────

    @staticmethod
    def _ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s, n=14):
        d    = s.diff()
        g    = d.clip(lower=0).ewm(span=n, adjust=False).mean()
        l    = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
        rs   = g / l.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _atr(df, n=14):
        h, l, pc = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h-l), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _pivot_highs(high: pd.Series, strength: int) -> pd.Series:
        out = pd.Series(np.nan, index=high.index)
        arr = high.values
        for i in range(strength, len(arr) - strength):
            window = arr[i - strength: i + strength + 1]
            if arr[i] >= max(window):
                out.iloc[i] = arr[i]
        return out

    @staticmethod
    def _pivot_lows(low: pd.Series, strength: int) -> pd.Series:
        out = pd.Series(np.nan, index=low.index)
        arr = low.values
        for i in range(strength, len(arr) - strength):
            window = arr[i - strength: i + strength + 1]
            if arr[i] <= min(window):
                out.iloc[i] = arr[i]
        return out

    def _detect_pattern(self, df) -> tuple[str, str]:
        """Определяет свечной паттерн. Возвращает (bull_pat, bear_pat)"""
        c = df.iloc[-1]
        p = df.iloc[-2]
        body       = abs(c["close"] - c["open"])
        total      = c["high"] - c["low"]
        if total < 1e-10:
            return "", ""
        upper_wick = c["high"] - max(c["close"], c["open"])
        lower_wick = min(c["close"], c["open"]) - c["low"]
        p_body     = abs(p["close"] - p["open"])

        bull, bear = "", ""

        # Пин-бары (смягчённые условия)
        if lower_wick >= body * 1.5 and c["close"] >= c["open"]:
            bull = "🟢 Бычий пин-бар"
        if upper_wick >= body * 1.5 and c["close"] <= c["open"]:
            bear = "🔴 Медвежий пин-бар"

        # Поглощение
        if (c["close"] > c["open"] and p["close"] < p["open"]
                and c["close"] > p["open"] and c["open"] < p["close"] and body >= p_body * 0.8):
            bull = "🟢 Бычье поглощение"
        if (c["close"] < c["open"] and p["close"] > p["open"]
                and c["close"] < p["open"] and c["open"] > p["close"] and body >= p_body * 0.8):
            bear = "🔴 Медвежье поглощение"

        # Молот / падающая звезда
        if lower_wick >= total * 0.55 and upper_wick <= total * 0.15:
            bull = "🟢 Молот"
        if upper_wick >= total * 0.55 and lower_wick <= total * 0.15:
            bear = "🔴 Падающая звезда"

        # Бычья / медвежья свеча (fallback — просто направление)
        if not bull and c["close"] > c["open"] and body >= total * 0.4:
            bull = "🟢 Бычья свеча"
        if not bear and c["close"] < c["open"] and body >= total * 0.4:
            bear = "🔴 Медвежья свеча"

        return bull, bear

    # ── Главная функция анализа ─────────────────────────

    def analyze(self, symbol: str, df: pd.DataFrame, df_htf=None) -> Optional[SignalResult]:
        cfg = self.cfg
        if df is None or len(df) < 60:
            return None

        bar_idx = len(df) - 1

        # Кулдаун — не давать сигнал по одной монете слишком часто
        last = self._last_signal.get(symbol, -999)
        if bar_idx - last < cfg.COOLDOWN_BARS:
            return None

        # ── Индикаторы ──────────────────────────────────
        close  = df["close"]
        atr    = self._atr(df, cfg.ATR_PERIOD)
        ema50  = self._ema(close, cfg.EMA_FAST)
        ema200 = self._ema(close, cfg.EMA_SLOW)
        rsi    = self._rsi(close, cfg.RSI_PERIOD)
        vol_ma = df["volume"].rolling(cfg.VOL_LEN).mean()

        c_now      = close.iloc[-1]
        atr_now    = atr.iloc[-1]
        rsi_now    = rsi.iloc[-1]
        vol_now    = df["volume"].iloc[-1]
        vol_avg    = vol_ma.iloc[-1]
        ema50_now  = ema50.iloc[-1]
        ema200_now = ema200.iloc[-1]
        vol_ratio  = vol_now / vol_avg if vol_avg > 0 else 1.0

        # ── Тренд ────────────────────────────────────────
        bull_local = c_now > ema50_now and ema50_now > ema200_now
        bear_local = c_now < ema50_now and ema50_now < ema200_now
        neutral    = not bull_local and not bear_local

        trend_local = "📈 Бычий" if bull_local else ("📉 Медвежий" if bear_local else "↔️ Боковик")

        # HTF тренд
        htf_bull = htf_bear = True   # по умолчанию не блокируем
        trend_htf = "⏸ Выкл"
        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) > 50:
            htf_ema   = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD)
            htf_c     = df_htf["close"].iloc[-1]
            htf_e     = htf_ema.iloc[-1]
            htf_bull  = htf_c > htf_e
            htf_bear  = htf_c < htf_e
            trend_htf = "📈 Бычий" if htf_bull else "📉 Медвежий"

        # ── Пивоты для уровней ───────────────────────────
        strength = cfg.PIVOT_STRENGTH
        ph = self._pivot_highs(df["high"], strength)
        pl = self._pivot_lows(df["low"],   strength)
        res_vals = ph.dropna().iloc[-5:]   # последние 5 сопротивлений
        sup_vals = pl.dropna().iloc[-5:]   # последние 5 поддержек

        if len(res_vals) == 0 or len(sup_vals) == 0:
            return None

        zone = atr_now * cfg.ZONE_BUFFER

        # ── Паттерн ──────────────────────────────────────
        bull_pat, bear_pat = self._detect_pattern(df)

        # ── СИГНАЛ LONG ──────────────────────────────────
        # Условие: цена у поддержки + бычий разворот
        long_signal = False
        long_level  = None
        long_type   = ""

        for sup in sup_vals.values[::-1]:   # ищем ближайшую поддержку
            dist = abs(c_now - sup) / atr_now
            near_support = dist < 1.5   # цена в пределах 1.5 ATR от уровня

            # Цена отбилась от поддержки (была ниже, теперь выше)
            prev_low  = df["low"].iloc[-3:-1].min()
            bounced   = prev_low <= sup + zone and c_now > sup

            if near_support or bounced:
                long_level = sup
                long_type  = "Отбой от поддержки"
                break

        # Проверяем пробой снизу вверх (цена закрылась выше сопротивления)
        for res in res_vals.values[::-1]:
            if df["close"].iloc[-2] < res and c_now > res + zone * 0.5:
                long_level = res
                long_type  = "Пробой сопротивления"
                break

        if long_level is not None:
            # Тренд: разрешаем в боковике тоже
            trend_ok   = bull_local or neutral or not cfg.USE_HTF_FILTER
            htf_ok     = htf_bull or not cfg.USE_HTF_FILTER
            rsi_ok     = (rsi_now < cfg.RSI_OB) if cfg.USE_RSI_FILTER else True
            vol_ok     = (vol_ratio >= cfg.VOL_MULT) if cfg.USE_VOLUME_FILTER else True
            pattern_ok = bool(bull_pat) if cfg.USE_PATTERN_FILTER else True
            bullish_c  = df["close"].iloc[-1] > df["open"].iloc[-1]

            long_signal = trend_ok and htf_ok and rsi_ok and (vol_ok or pattern_ok) and (bullish_c or bull_pat)

        # ── СИГНАЛ SHORT ─────────────────────────────────
        short_signal = False
        short_level  = None
        short_type   = ""

        for res in res_vals.values[::-1]:
            dist = abs(c_now - res) / atr_now
            near_res = dist < 1.5

            prev_high = df["high"].iloc[-3:-1].max()
            rejected  = prev_high >= res - zone and c_now < res

            if near_res or rejected:
                short_level = res
                short_type  = "Отбой от сопротивления"
                break

        for sup in sup_vals.values[::-1]:
            if df["close"].iloc[-2] > sup and c_now < sup - zone * 0.5:
                short_level = sup
                short_type  = "Пробой поддержки"
                break

        if short_level is not None:
            trend_ok   = bear_local or neutral or not cfg.USE_HTF_FILTER
            htf_ok     = htf_bear or not cfg.USE_HTF_FILTER
            rsi_ok     = (rsi_now > cfg.RSI_OS) if cfg.USE_RSI_FILTER else True
            vol_ok     = (vol_ratio >= cfg.VOL_MULT) if cfg.USE_VOLUME_FILTER else True
            pattern_ok = bool(bear_pat) if cfg.USE_PATTERN_FILTER else True
            bearish_c  = df["close"].iloc[-1] < df["open"].iloc[-1]

            short_signal = trend_ok and htf_ok and rsi_ok and (vol_ok or pattern_ok) and (bearish_c or bear_pat)

        # Приоритет: если оба — выбираем по RSI
        if long_signal and short_signal:
            if rsi_now >= 50:
                long_signal  = False
            else:
                short_signal = False

        if not long_signal and not short_signal:
            return None

        # ── Расчёт уровней входа ─────────────────────────
        direction = "LONG" if long_signal else "SHORT"

        if long_signal:
            entry = c_now
            sl    = min(df["low"].iloc[-3:].min(), long_level - zone) - atr_now * cfg.ATR_MULT * 0.5
            sl    = min(sl, entry * (1 - cfg.MAX_RISK_PCT / 100))
            risk  = entry - sl
            tp1   = entry + risk * cfg.TP1_RR
            tp2   = entry + risk * cfg.TP2_RR
            tp3   = entry + risk * cfg.TP3_RR
            pattern = bull_pat or "🟢 Бычья свеча"
            btype   = long_type
        else:
            entry = c_now
            sl    = max(df["high"].iloc[-3:].max(), short_level + zone) + atr_now * cfg.ATR_MULT * 0.5
            sl    = max(sl, entry * (1 + cfg.MAX_RISK_PCT / 100))
            risk  = sl - entry
            tp1   = entry - risk * cfg.TP1_RR
            tp2   = entry - risk * cfg.TP2_RR
            tp3   = entry - risk * cfg.TP3_RR
            pattern = bear_pat or "🔴 Медвежья свеча"
            btype   = short_type

        risk_pct = abs((sl - entry) / entry * 100)

        # ── Качество (1-5 звёзд) ─────────────────────────
        quality  = 1
        reasons  = []

        vol_ok_q = vol_ratio >= cfg.VOL_MULT
        pat_ok_q = bool(bull_pat if long_signal else bear_pat)
        rsi_ok_q = (rsi_now < 50) if long_signal else (rsi_now > 50)
        htf_ok_q = (htf_bull if long_signal else htf_bear)
        trend_q  = (bull_local if long_signal else bear_local)

        if vol_ok_q:
            quality += 1
            reasons.append(f"✅ Объём x{vol_ratio:.1f}")
        if pat_ok_q:
            quality += 1
            reasons.append(f"✅ {pattern}")
        if rsi_ok_q:
            quality += 1
            reasons.append(f"✅ RSI {rsi_now:.1f}")
        if trend_q and htf_ok_q:
            quality += 1
            reasons.append("✅ Тренд подтверждает")

        quality = min(quality, 5)

        self._last_signal[symbol] = bar_idx

        return SignalResult(
            symbol        = symbol,
            direction     = direction,
            entry         = entry,
            sl            = sl,
            tp1           = tp1,
            tp2           = tp2,
            tp3           = tp3,
            risk_pct      = risk_pct,
            quality       = quality,
            smc_score     = 0,
            total_score   = quality,
            reasons       = reasons,
            rsi           = rsi_now,
            volume_ratio  = vol_ratio,
            trend_local   = trend_local,
            trend_htf     = trend_htf,
            pattern       = pattern,
            breakout_type = btype,
        )
