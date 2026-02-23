"""
CHM BREAKER — индикатор v5
+ BOS/CHoCH, Order Block, FVG, RSI-дивергенция
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
    # Новые SMC поля
    has_ob:        bool  = False   # Order Block
    has_fvg:       bool  = False   # Fair Value Gap
    has_liq_sweep: bool  = False   # Liquidity Sweep (ложный пробой)
    has_bos:       bool  = False   # Break of Structure
    has_divergence:bool  = False   # RSI дивергенция
    ob_level:      float = 0.0     # уровень OB
    fvg_size_pct:  float = 0.0     # размер FVG в %


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}

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

    # ── BOS / CHoCH ─────────────────────────────────────
    # Break of Structure: цена закрылась выше последнего структурного максимума (BOS бычий)
    # или ниже последнего структурного минимума (BOS медвежий)

    def _detect_bos(self, df: pd.DataFrame, direction: str, strength: int = 5) -> bool:
        """Возвращает True если есть BOS в нужном направлении на последних 30 свечах."""
        try:
            window = df.iloc[-30:]
            c_now  = df["close"].iloc[-1]
            if direction == "LONG":
                ph = self._pivot_highs(window["high"], strength).dropna()
                if len(ph) >= 2:
                    prev_high = ph.iloc[-2]          # предпоследний максимум
                    # Текущая цена пробила предпоследний максимум структуры
                    return c_now > prev_high
            else:
                pl = self._pivot_lows(window["low"], strength).dropna()
                if len(pl) >= 2:
                    prev_low = pl.iloc[-2]
                    return c_now < prev_low
        except Exception:
            pass
        return False

    # ── Order Block ─────────────────────────────────────
    # Последняя сильная медвежья свеча перед бычьим движением (бычий OB для лонга)
    # или последняя сильная бычья свеча перед медвежьим движением (медвежий OB для шорта)

    def _detect_order_block(self, df: pd.DataFrame, direction: str, atr: float) -> tuple[bool, float]:
        """Возвращает (ob_found, ob_level)."""
        try:
            c_now = df["close"].iloc[-1]
            if direction == "LONG":
                # Ищем медвежью свечу (close < open) за последние 20 свечей,
                # после которой пошёл рост. OB = верхняя граница той свечи.
                for i in range(-20, -3):
                    candle = df.iloc[i]
                    if candle["close"] < candle["open"]:                # медвежья
                        ob_top    = candle["open"]                      # верх OB
                        ob_bottom = candle["close"]                     # низ OB
                        # После этой свечи был рост
                        future_close = df["close"].iloc[i+1:i+5].max()
                        if future_close > ob_top:                       # вышел выше OB
                            # Текущая цена около OB
                            if ob_bottom - atr * 0.3 < c_now < ob_top + atr * 0.3:
                                return True, (ob_top + ob_bottom) / 2
            else:
                # Бычья свеча перед падением
                for i in range(-20, -3):
                    candle = df.iloc[i]
                    if candle["close"] > candle["open"]:                # бычья
                        ob_top    = candle["close"]
                        ob_bottom = candle["open"]
                        future_close = df["close"].iloc[i+1:i+5].min()
                        if future_close < ob_bottom:                    # вышел ниже OB
                            if ob_bottom - atr * 0.3 < c_now < ob_top + atr * 0.3:
                                return True, (ob_top + ob_bottom) / 2
        except Exception:
            pass
        return False, 0.0

    # ── Fair Value Gap (FVG / Imbalance) ─────────────────
    # Импульсная свеча, оставившая разрыв между high[i-1] и low[i+1]

    def _detect_fvg(self, df: pd.DataFrame, direction: str) -> tuple[bool, float]:
        """Ищет FVG в последних 15 свечах. Возвращает (fvg_found, gap_size_pct)."""
        try:
            c_now = df["close"].iloc[-1]
            for i in range(-15, -2):
                prev   = df.iloc[i - 1]
                middle = df.iloc[i]
                nxt    = df.iloc[i + 1]
                mid_body = abs(middle["close"] - middle["open"])
                mid_total = middle["high"] - middle["low"]
                if mid_total < 1e-10:
                    continue
                is_impulse = mid_body / mid_total > 0.6   # импульсная свеча

                if direction == "LONG" and is_impulse and middle["close"] > middle["open"]:
                    gap_low  = prev["high"]
                    gap_high = nxt["low"]
                    if gap_high > gap_low:    # реальный разрыв
                        gap_pct = (gap_high - gap_low) / gap_low * 100
                        # Цена вернулась в FVG (ретест)
                        mid_gap = (gap_low + gap_high) / 2
                        if gap_low - mid_body * 0.3 < c_now < gap_high + mid_body * 0.3:
                            return True, gap_pct

                if direction == "SHORT" and is_impulse and middle["close"] < middle["open"]:
                    gap_low  = nxt["high"]
                    gap_high = prev["low"]
                    if gap_high > gap_low:
                        gap_pct = (gap_high - gap_low) / gap_low * 100
                        mid_gap = (gap_low + gap_high) / 2
                        if gap_low - mid_body * 0.3 < c_now < gap_high + mid_body * 0.3:
                            return True, gap_pct
        except Exception:
            pass
        return False, 0.0

    # ── Liquidity Sweep ──────────────────────────────────
    # Ложный пробой: цена пробила уровень, но вернулась — шортисты/лонгисты ликвидированы

    def _detect_liquidity_sweep(self, df: pd.DataFrame, direction: str, level: float, atr: float) -> bool:
        """Проверяет был ли недавний ложный пробой уровня (sweep)."""
        try:
            # Смотрим последние 10 свечей
            recent = df.iloc[-10:-1]
            c_now  = df["close"].iloc[-1]
            if direction == "LONG":
                # Цена пробивала уровень вниз (шип ниже), но закрылась выше
                lows = recent["low"]
                if lows.min() < level - atr * 0.1:         # был шип ниже уровня
                    if c_now > level:                        # сейчас выше
                        return True
            else:
                highs = recent["high"]
                if highs.max() > level + atr * 0.1:
                    if c_now < level:
                        return True
        except Exception:
            pass
        return False

    # ── RSI Дивергенция ──────────────────────────────────
    # Цена обновила минимум/максимум, RSI — нет → скрытое давление разворота

    def _detect_divergence(self, df: pd.DataFrame, rsi: pd.Series, direction: str) -> bool:
        """Простая RSI-дивергенция на последних 20 свечах."""
        try:
            window_df  = df.iloc[-20:]
            window_rsi = rsi.iloc[-20:]
            if direction == "LONG":
                # Бычья дивергенция: цена сделала новый LOW, RSI — нет
                price_low1 = window_df["low"].iloc[:10].min()
                price_low2 = window_df["low"].iloc[10:].min()
                rsi_low1   = window_rsi.iloc[:10].min()
                rsi_low2   = window_rsi.iloc[10:].min()
                if price_low2 < price_low1 and rsi_low2 > rsi_low1:
                    return True
            else:
                # Медвежья дивергенция: цена сделала новый HIGH, RSI — нет
                price_hi1 = window_df["high"].iloc[:10].max()
                price_hi2 = window_df["high"].iloc[10:].max()
                rsi_hi1   = window_rsi.iloc[:10].max()
                rsi_hi2   = window_rsi.iloc[10:].max()
                if price_hi2 > price_hi1 and rsi_hi2 < rsi_hi1:
                    return True
        except Exception:
            pass
        return False

    # ── Паттерны ─────────────────────────────────────────

    def _detect_pattern(self, df) -> tuple[str, str]:
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

        if lower_wick >= body * 1.5 and c["close"] >= c["open"]:
            bull = "🟢 Бычий пин-бар"
        if upper_wick >= body * 1.5 and c["close"] <= c["open"]:
            bear = "🔴 Медвежий пин-бар"

        if (c["close"] > c["open"] and p["close"] < p["open"]
                and c["close"] > p["open"] and c["open"] < p["close"] and body >= p_body * 0.8):
            bull = "🟢 Бычье поглощение"
        if (c["close"] < c["open"] and p["close"] > p["open"]
                and c["close"] < p["open"] and c["open"] > p["close"] and body >= p_body * 0.8):
            bear = "🔴 Медвежье поглощение"

        if lower_wick >= total * 0.55 and upper_wick <= total * 0.15:
            bull = "🟢 Молот"
        if upper_wick >= total * 0.55 and lower_wick <= total * 0.15:
            bear = "🔴 Падающая звезда"

        if not bull and c["close"] > c["open"] and body >= total * 0.4:
            bull = "🟢 Бычья свеча"
        if not bear and c["close"] < c["open"] and body >= total * 0.4:
            bear = "🔴 Медвежья свеча"

        return bull, bear

    # ── Главный анализ ────────────────────────────────────

    def analyze(self, symbol: str, df: pd.DataFrame, df_htf=None) -> Optional[SignalResult]:
        cfg = self.cfg
        if df is None or len(df) < 60:
            return None

        bar_idx = len(df) - 1
        last    = self._last_signal.get(symbol, -999)
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

        htf_bull = htf_bear = True
        trend_htf = "⏸ Выкл"
        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) > 50:
            htf_ema  = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD)
            htf_c    = df_htf["close"].iloc[-1]
            htf_e    = htf_ema.iloc[-1]
            htf_bull = htf_c > htf_e
            htf_bear = htf_c < htf_e
            trend_htf = "📈 Бычий" if htf_bull else "📉 Медвежий"

        # ── Пивоты ───────────────────────────────────────
        strength = cfg.PIVOT_STRENGTH
        ph = self._pivot_highs(df["high"], strength)
        pl = self._pivot_lows(df["low"],   strength)
        res_vals = ph.dropna().iloc[-5:]
        sup_vals = pl.dropna().iloc[-5:]

        if len(res_vals) == 0 or len(sup_vals) == 0:
            return None

        zone = atr_now * cfg.ZONE_BUFFER

        # ── Паттерн ──────────────────────────────────────
        bull_pat, bear_pat = self._detect_pattern(df)

        # ── СИГНАЛ LONG ──────────────────────────────────
        long_signal = False
        long_level  = None
        long_type   = ""

        for sup in sup_vals.values[::-1]:
            dist = abs(c_now - sup) / atr_now
            near_support = dist < 1.5
            prev_low     = df["low"].iloc[-3:-1].min()
            bounced      = prev_low <= sup + zone and c_now > sup
            if near_support or bounced:
                long_level = sup
                long_type  = "Отбой от поддержки"
                break

        for res in res_vals.values[::-1]:
            if df["close"].iloc[-2] < res and c_now > res + zone * 0.5:
                long_level = res
                long_type  = "Пробой сопротивления"
                break

        if long_level is not None:
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
            near_res  = dist < 1.5
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

        if long_signal and short_signal:
            if rsi_now >= 50:
                long_signal  = False
            else:
                short_signal = False

        if not long_signal and not short_signal:
            return None

        direction = "LONG" if long_signal else "SHORT"
        level     = long_level if long_signal else short_level

        # ── SMC детекторы (новые) ────────────────────────
        has_bos       = self._detect_bos(df, direction, strength)
        has_ob, ob_lv = self._detect_order_block(df, direction, atr_now)
        has_fvg, fvg_pct = self._detect_fvg(df, direction)
        has_liq       = self._detect_liquidity_sweep(df, direction, level, atr_now)
        has_div       = self._detect_divergence(df, rsi, direction)

        # ── Расчёт уровней ───────────────────────────────
        if long_signal:
            entry   = c_now
            sl      = min(df["low"].iloc[-3:].min(), long_level - zone) - atr_now * cfg.ATR_MULT * 0.5
            sl      = min(sl, entry * (1 - cfg.MAX_RISK_PCT / 100))
            risk    = entry - sl
            tp1     = entry + risk * cfg.TP1_RR
            tp2     = entry + risk * cfg.TP2_RR
            tp3     = entry + risk * cfg.TP3_RR
            pattern = bull_pat or "🟢 Бычья свеча"
            btype   = long_type
        else:
            entry   = c_now
            sl      = max(df["high"].iloc[-3:].max(), short_level + zone) + atr_now * cfg.ATR_MULT * 0.5
            sl      = max(sl, entry * (1 + cfg.MAX_RISK_PCT / 100))
            risk    = sl - entry
            tp1     = entry - risk * cfg.TP1_RR
            tp2     = entry - risk * cfg.TP2_RR
            tp3     = entry - risk * cfg.TP3_RR
            pattern = bear_pat or "🔴 Медвежья свеча"
            btype   = short_type

        risk_pct = abs((sl - entry) / entry * 100)

        # ── Качество (базовые 1-5 + SMC бонусы) ──────────
        quality  = 1
        reasons  = []

        vol_ok_q = vol_ratio >= cfg.VOL_MULT
        pat_ok_q = bool(bull_pat if long_signal else bear_pat)
        rsi_ok_q = (rsi_now < 50) if long_signal else (rsi_now > 50)
        htf_ok_q = (htf_bull if long_signal else htf_bear)
        trend_q  = (bull_local if long_signal else bear_local)

        if vol_ok_q:
            quality += 1
            reasons.append(f"vol:{vol_ratio:.1f}x")
        if pat_ok_q:
            quality += 1
            reasons.append(f"pat:{pattern}")
        if rsi_ok_q:
            quality += 1
            reasons.append(f"rsi:{rsi_now:.1f}")
        if trend_q and htf_ok_q:
            quality += 1
            reasons.append("trend")

        # SMC бонусы (качество не выходит за 5)
        smc_score = 0
        smc_reasons = []
        if has_bos:
            smc_score += 1
            smc_reasons.append("BOS")
        if has_ob:
            smc_score += 1
            smc_reasons.append(f"OB@{ob_lv:.4g}")
        if has_fvg:
            smc_score += 1
            smc_reasons.append(f"FVG{fvg_pct:.1f}%")
        if has_liq:
            smc_score += 1
            smc_reasons.append("LiqSweep")
        if has_div:
            smc_score += 1
            smc_reasons.append("Div")

        total_score = quality + smc_score
        quality     = min(quality, 5)

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
            smc_score     = smc_score,
            total_score   = total_score,
            reasons       = reasons,
            smc_reasons   = smc_reasons,
            rsi           = rsi_now,
            volume_ratio  = vol_ratio,
            trend_local   = trend_local,
            trend_htf     = trend_htf,
            pattern       = pattern,
            breakout_type = btype,
            has_bos       = has_bos,
            has_ob        = has_ob,
            has_fvg       = has_fvg,
            has_liq_sweep = has_liq,
            has_divergence = has_div,
            ob_level      = ob_lv,
            fvg_size_pct  = fvg_pct,
        )
