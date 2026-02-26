"""
CHM BREAKER — логика индикатора на Python
Полный аналог Pine Script версии
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
    direction:     str        # "LONG" или "SHORT"
    entry:         float
    sl:            float
    tp1:           float
    tp2:           float
    tp3:           float
    risk_pct:      float
    quality:       int        # 1-5 звёзд
    reasons:       list       # причины сигнала
    rsi:           float
    volume_ratio:  float      # объём / средний объём
    trend_local:   str        # Бычий / Медвежий
    trend_htf:     str        # Бычий / Медвежий / Выкл
    pattern:       str        # название паттерна
    breakout_type: str        # тип пробоя


@dataclass
class BreakoutState:
    """Состояние ожидания ретеста для одной монеты"""
    up_pending:   bool  = False
    dn_pending:   bool  = False
    res_level:    float = 0.0
    sup_level:    float = 0.0
    up_bar:       int   = 0
    dn_bar:       int   = 0
    last_long:    int   = -999
    last_short:   int   = -999


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        # Состояние пробоев по каждой монете
        self._states: dict[str, BreakoutState] = {}

    def _state(self, symbol: str) -> BreakoutState:
        if symbol not in self._states:
            self._states[symbol] = BreakoutState()
        return self._states[symbol]

    # ─────────────────────────────────────────────
    #  Технические индикаторы
    # ─────────────────────────────────────────────

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _pivot_high(high: pd.Series, strength: int) -> pd.Series:
        result = pd.Series(np.nan, index=high.index)
        arr = high.values
        for i in range(strength, len(arr) - strength):
            window = arr[i - strength: i + strength + 1]
            if arr[i] == max(window):
                result.iloc[i] = arr[i]
        return result

    @staticmethod
    def _pivot_low(low: pd.Series, strength: int) -> pd.Series:
        result = pd.Series(np.nan, index=low.index)
        arr = low.values
        for i in range(strength, len(arr) - strength):
            window = arr[i - strength: i + strength + 1]
            if arr[i] == min(window):
                result.iloc[i] = arr[i]
        return result

    # ─────────────────────────────────────────────
    #  Свечные паттерны
    # ─────────────────────────────────────────────

    @staticmethod
    def _detect_patterns(df: pd.DataFrame) -> tuple[str, str]:
        """Возвращает (bull_pattern, bear_pattern) на последней свече"""
        c = df.iloc[-1]
        p = df.iloc[-2]

        body       = abs(c["close"] - c["open"])
        total      = c["high"] - c["low"]
        upper_wick = c["high"] - max(c["close"], c["open"])
        lower_wick = min(c["close"], c["open"]) - c["low"]

        bull_pattern = ""
        bear_pattern = ""

        if total == 0:
            return bull_pattern, bear_pattern

        # Пин-бар бычий
        if lower_wick >= body * 2.0 and lower_wick >= total * 0.5 and c["close"] > c["open"]:
            bull_pattern = "🟢 Бычий пин-бар"

        # Пин-бар медвежий
        if upper_wick >= body * 2.0 and upper_wick >= total * 0.5 and c["close"] < c["open"]:
            bear_pattern = "🔴 Медвежий пин-бар"

        # Бычье поглощение
        p_body = abs(p["close"] - p["open"])
        if (c["close"] > c["open"] and p["close"] < p["open"]
                and c["close"] > p["open"] and c["open"] < p["close"]
                and body > p_body):
            bull_pattern = "🟢 Бычье поглощение"

        # Медвежье поглощение
        if (c["close"] < c["open"] and p["close"] > p["open"]
                and c["close"] < p["open"] and c["open"] > p["close"]
                and body > p_body):
            bear_pattern = "🔴 Медвежье поглощение"

        # Молот
        if lower_wick >= total * 0.6 and upper_wick <= total * 0.1:
            bull_pattern = "🟢 Молот"

        # Падающая звезда
        if upper_wick >= total * 0.6 and lower_wick <= total * 0.1:
            bear_pattern = "🔴 Падающая звезда"

        return bull_pattern, bear_pattern

    # ─────────────────────────────────────────────
    #  Основной анализ
    # ─────────────────────────────────────────────

    def analyze(
        self,
        symbol: str,
        df: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None,
    ) -> Optional[SignalResult]:
        """
        Анализирует DataFrame и возвращает SignalResult если есть сигнал,
        иначе None.
        """
        cfg = self.cfg
        if df is None or len(df) < cfg.PIVOT_STRENGTH * 2 + 50:
            return None

        state = self._state(symbol)
        bar_idx = len(df) - 1

        # ── Индикаторы ───────────────────────────────────
        atr   = self._atr(df, cfg.ATR_PERIOD)
        ema50 = self._ema(df["close"], cfg.EMA_FAST)
        ema200= self._ema(df["close"], cfg.EMA_SLOW)
        rsi   = self._rsi(df["close"], cfg.RSI_PERIOD)
        avg_vol = df["volume"].rolling(cfg.VOL_LEN).mean()

        atr_now    = atr.iloc[-1]
        ema50_now  = ema50.iloc[-1]
        ema200_now = ema200.iloc[-1]
        rsi_now    = rsi.iloc[-1]
        vol_now    = df["volume"].iloc[-1]
        avg_vol_now= avg_vol.iloc[-1]
        close_now  = df["close"].iloc[-1]
        high_now   = df["high"].iloc[-1]
        low_now    = df["low"].iloc[-1]
        open_now   = df["open"].iloc[-1]

        vol_ratio  = vol_now / avg_vol_now if avg_vol_now > 0 else 0

        # ── Тренд локальный ──────────────────────────────
        bull_local = close_now > ema50_now and ema50_now > ema200_now
        bear_local = close_now < ema50_now and ema50_now < ema200_now
        trend_local = "📈 Бычий" if bull_local else ("📉 Медвежий" if bear_local else "↔️ Боковик")

        # ── HTF тренд ────────────────────────────────────
        trend_htf = "⏸ Выкл"
        htf_bull = True
        htf_bear = True

        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) > cfg.HTF_EMA_PERIOD:
            htf_ema = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD)
            htf_close = df_htf["close"].iloc[-1]
            htf_ema_now = htf_ema.iloc[-1]
            htf_bull = htf_close > htf_ema_now
            htf_bear = htf_close < htf_ema_now
            trend_htf = "📈 Бычий" if htf_bull else "📉 Медвежий"

        bull_trend = bull_local and htf_bull
        bear_trend = bear_local and htf_bear

        # ── Пивоты ───────────────────────────────────────
        ph = self._pivot_high(df["high"], cfg.PIVOT_STRENGTH)
        pl = self._pivot_low(df["low"],   cfg.PIVOT_STRENGTH)

        # Последний уровень сопротивления
        res_vals = ph.dropna()
        sup_vals = pl.dropna()

        if len(res_vals) == 0 or len(sup_vals) == 0:
            return None

        res_level = res_vals.iloc[-1]
        sup_level = sup_vals.iloc[-1]
        res_age   = bar_idx - df.index.get_loc(res_vals.index[-1])
        sup_age   = bar_idx - df.index.get_loc(sup_vals.index[-1])

        res_valid = res_age <= cfg.MAX_LEVEL_AGE
        sup_valid = sup_age <= cfg.MAX_LEVEL_AGE

        sr_zone   = atr_now * cfg.ZONE_BUFFER
        res_hi    = res_level + sr_zone
        res_lo    = res_level - sr_zone
        sup_hi    = sup_level + sr_zone
        sup_lo    = sup_level - sr_zone

        # ── Фильтры ──────────────────────────────────────
        vol_ok      = (vol_ratio >= cfg.VOL_MULT) if cfg.USE_VOLUME_FILTER else True
        rsi_long_ok = (rsi_now < cfg.RSI_OB)       if cfg.USE_RSI_FILTER   else True
        rsi_short_ok= (rsi_now > cfg.RSI_OS)       if cfg.USE_RSI_FILTER   else True

        bull_pat, bear_pat = self._detect_patterns(df)
        pat_long_ok  = bool(bull_pat) if cfg.USE_PATTERN_FILTER else True
        pat_short_ok = bool(bear_pat) if cfg.USE_PATTERN_FILTER else True

        # ── Cooldown ─────────────────────────────────────
        cd_long  = (bar_idx - state.last_long)  >= cfg.COOLDOWN_BARS
        cd_short = (bar_idx - state.last_short) >= cfg.COOLDOWN_BARS

        # ── Логика пробоя и ретеста ───────────────────────
        close_prev = df["close"].iloc[-2]

        # Пробой вверх
        if (res_valid and close_prev > res_hi and close_now > res_hi
                and bull_trend and not state.up_pending):
            state.up_pending = True
            state.res_level  = res_level
            state.up_bar     = bar_idx
            log.debug(f"{symbol}: 🔔 Пробой вверх уровня {res_level:.4f}")

        # Пробой вниз
        if (sup_valid and close_prev < sup_lo and close_now < sup_lo
                and bear_trend and not state.dn_pending):
            state.dn_pending = True
            state.sup_level  = sup_level
            state.dn_bar     = bar_idx
            log.debug(f"{symbol}: 🔔 Пробой вниз уровня {sup_level:.4f}")

        # Таймаут ретеста
        if state.up_pending and (bar_idx - state.up_bar) > cfg.MAX_RETEST_BARS:
            state.up_pending = False
        if state.dn_pending and (bar_idx - state.dn_bar) > cfg.MAX_RETEST_BARS:
            state.dn_pending = False

        # Ретест вверх (цена вернулась к уровню, отскочила вверх)
        retest_up = (
            state.up_pending
            and low_now <= (state.res_level + sr_zone)
            and close_now > state.res_level
            and close_now > open_now
        )

        # Ретест вниз
        retest_dn = (
            state.dn_pending
            and high_now >= (state.sup_level - sr_zone)
            and close_now < state.sup_level
            and close_now < open_now
        )

        # ── Итоговые сигналы ─────────────────────────────
        long_signal  = retest_up and pat_long_ok  and vol_ok and rsi_long_ok  and cd_long
        short_signal = retest_dn and pat_short_ok and vol_ok and rsi_short_ok and cd_short

        if not long_signal and not short_signal:
            return None

        direction = "LONG" if long_signal else "SHORT"

        # ── Расчёт SL и TP ───────────────────────────────
        if long_signal:
            entry = close_now
            sl    = low_now - atr_now * cfg.ATR_MULT
            risk  = entry - sl
            if (risk / entry * 100) > cfg.MAX_RISK_PCT:
                sl   = entry * (1 - cfg.MAX_RISK_PCT / 100)
                risk = entry - sl
            tp1 = entry + risk * cfg.TP1_RR
            tp2 = entry + risk * cfg.TP2_RR
            tp3 = entry + risk * cfg.TP3_RR
            state.up_pending = False
            state.last_long  = bar_idx
            pattern = bull_pat or "—"
        else:
            entry = close_now
            sl    = high_now + atr_now * cfg.ATR_MULT
            risk  = sl - entry
            if (risk / entry * 100) > cfg.MAX_RISK_PCT:
                sl   = entry * (1 + cfg.MAX_RISK_PCT / 100)
                risk = sl - entry
            tp1 = entry - risk * cfg.TP1_RR
            tp2 = entry - risk * cfg.TP2_RR
            tp3 = entry - risk * cfg.TP3_RR
            state.dn_pending = False
            state.last_short = bar_idx
            pattern = bear_pat or "—"

        risk_pct = abs((sl - entry) / entry * 100)

        # ── Качество сигнала (1-5 ⭐) ─────────────────────
        quality = 1
        reasons = []

        if vol_ok:
            quality += 1
            reasons.append(f"✅ Объём x{vol_ratio:.1f}")
        if bool(bull_pat if long_signal else bear_pat):
            quality += 1
            reasons.append(f"✅ Паттерн: {pattern}")
        if (long_signal and rsi_now < 50) or (short_signal and rsi_now > 50):
            quality += 1
            reasons.append(f"✅ RSI в зоне ({rsi_now:.1f})")
        if (long_signal and htf_bull) or (short_signal and htf_bear):
            quality += 1
            reasons.append(f"✅ HTF тренд совпадает")

        quality = min(quality, 5)

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
            reasons       = reasons,
            rsi           = rsi_now,
            volume_ratio  = vol_ratio,
            trend_local   = trend_local,
            trend_htf     = trend_htf,
            pattern       = pattern,
            breakout_type = "Ретест сопротивления" if long_signal else "Ретест поддержки",
        )
