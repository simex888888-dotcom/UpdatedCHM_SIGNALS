"""
CHM BREAKER — индикатор v4.2
Новое:
  • CHOCH — смена характера структуры (ранний сигнал разворота)
  • HTF Daily Confluence — совпадение с уровнями дневного таймфрейма
  • Session scoring — бонус качества за торговлю в прайм-сессии
"""

import logging
from datetime import datetime, timezone

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
    # ── Новые поля v4.2 ────────────────────────────
    has_choch:      bool  = False   # Change of Character обнаружен
    htf_confluence: bool  = False   # Совпадение с уровнями дневного TF
    session_name:   str   = ""      # Название текущей сессии
    session_prime:  bool  = False   # True = прайм-сессия (Лондон / Нью-Йорк)


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}

    # ── Базовые инструменты ─────────────────────────────────────────────────

    @staticmethod
    def _ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s, n=14):
        d = s.diff()
        g = d.clip(lower=0).ewm(span=n, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
        rs = g / l.replace(0, np.nan)
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

    def _detect_pattern(self, df) -> tuple:
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

    # ── ═══════════════════════════════════════════════════════════════════ ──
    #    НОВОЕ v4.2: три улучшения точности
    # ── ═══════════════════════════════════════════════════════════════════ ──

    # ── 1. CHOCH — Change of Character ──────────────────────────────────────
    #
    #  CHOCH определяет первый пробой структуры в ПРОТИВОПОЛОЖНУЮ сторону.
    #  Это самый ранний сигнал смены тренда — ещё до того как BOS подтвердил.
    #
    #  BULL CHOCH: рынок делал lower-highs (медвежья структура),
    #              затем цена закрылась выше последнего lower-high → смена характера.
    #  BEAR CHOCH: рынок делал higher-lows (бычья структура),
    #              затем цена закрылась ниже последнего higher-low → смена характера.

    def _detect_choch(self, df: pd.DataFrame, direction: str) -> bool:
        strength = max(3, self.cfg.PIVOT_STRENGTH // 2)  # чуть мягче чем основные пивоты
        ph = self._pivot_highs(df["high"], strength)
        pl = self._pivot_lows(df["low"],   strength)

        recent_h = ph.dropna().iloc[-5:]
        recent_l = pl.dropna().iloc[-5:]

        if len(recent_h) < 3 or len(recent_l) < 3:
            return False

        c_now = df["close"].iloc[-1]

        if direction == "LONG":
            h_vals = recent_h.values
            # Была медвежья структура: последние 2 свинг-хая понижались
            had_lower_highs = len(h_vals) >= 3 and h_vals[-3] > h_vals[-2]
            # Теперь цена пробила последний lower-high вверх
            broke_above = c_now > h_vals[-2]
            # Дополнительно проверяем что последний свинг-лоу выше предыдущего (рост дна)
            l_vals = recent_l.values
            rising_lows = len(l_vals) >= 2 and l_vals[-1] > l_vals[-2]
            return had_lower_highs and broke_above and rising_lows

        else:  # SHORT
            l_vals = recent_l.values
            # Была бычья структура: последние 2 свинг-лоу повышались
            had_higher_lows = len(l_vals) >= 3 and l_vals[-3] < l_vals[-2]
            # Теперь цена пробила последний higher-low вниз
            broke_below = c_now < l_vals[-2]
            # Дополнительно: последний свинг-хай ниже предыдущего (снижение вершин)
            h_vals = recent_h.values
            falling_highs = len(h_vals) >= 2 and h_vals[-1] < h_vals[-2]
            return had_higher_lows and broke_below and falling_highs

    # ── 2. HTF Daily Confluence ──────────────────────────────────────────────
    #
    #  Проверяет, находится ли зона входа вблизи значимого уровня дневного TF.
    #  Уровни: Floor Pivot (классический), Daily S/R свинги, Camarilla Pivots.
    #  Совпадение с дневным уровнем — самый сильный дополнительный фактор.

    def _htf_daily_confluence(
        self, entry: float, df_htf: pd.DataFrame, atr_daily: float, direction: str
    ) -> bool:
        if df_htf is None or len(df_htf) < 5:
            return False

        # Последний завершённый дневной бар ([-2], т.к. [-1] ещё не закрыт)
        d = df_htf.iloc[-2]
        H, L, C = d["high"], d["low"], d["close"]

        # ─── Floor Pivot Points (классика) ────────────────────
        P  = (H + L + C) / 3
        R1 = 2 * P - L
        S1 = 2 * P - H
        R2 = P + (H - L)
        S2 = P - (H - L)
        R3 = H + 2 * (P - L)
        S3 = L - 2 * (H - P)

        # ─── Camarilla Pivots (лучше работают внутри дня) ─────
        cam_H4 = C + (H - L) * 1.1 / 2
        cam_L4 = C - (H - L) * 1.1 / 2
        cam_H3 = C + (H - L) * 1.1 / 4
        cam_L3 = C - (H - L) * 1.1 / 4

        # ─── Дневные свинг-уровни ─────────────────────────────
        daily_ph = self._pivot_highs(df_htf["high"], 3)
        daily_pl = self._pivot_lows(df_htf["low"],   3)
        swing_levels = (
            list(daily_ph.dropna().iloc[-3:].values) +
            list(daily_pl.dropna().iloc[-3:].values)
        )

        all_levels = [P, R1, S1, R2, S2, R3, S3, cam_H4, cam_L4, cam_H3, cam_L3] + swing_levels

        # Зона совпадения = 1.5 ATR от дневной свечи
        zone = atr_daily * 1.5 if atr_daily > 0 else abs(H - L) * 0.3

        for level in all_levels:
            dist = abs(entry - level)
            if dist < zone:
                if direction == "LONG" and level <= entry + zone:
                    return True
                if direction == "SHORT" and level >= entry - zone:
                    return True

        return False

    # ── 3. Session Filter ────────────────────────────────────────────────────
    #
    #  Время торговых сессий (UTC):
    #    Азия:          00:00 – 07:00  слабый объём, много шума
    #    Лондон-пре:    06:00 – 07:00  подготовка к открытию
    #    Лондон:        07:00 – 10:00  ⭐ прайм — крупные движения
    #    Лондон-мид:    10:00 – 12:00  затухание лондонской активности
    #    NY-пре:        12:00 – 13:00  подготовка
    #    NY открытие:   13:00 – 16:00  ⭐⭐ лучшее время — перекрытие
    #    NY-мид:        16:00 – 18:00  затухание
    #    Вечер/ночь:    18:00 – 00:00  межсессия, малый объём

    @staticmethod
    def _get_session() -> tuple:
        """Возвращает (session_name, is_prime).
           is_prime = True во время Лондон-open и NY-open.
        """
        utc_h = datetime.now(timezone.utc).hour

        if 7 <= utc_h < 10:
            return "🇬🇧 Лондон", True
        elif 13 <= utc_h < 17:
            return "🇺🇸 Нью-Йорк", True      # включает перекрытие с Лондоном
        elif 10 <= utc_h < 13:
            return "🌐 Лондон-мид", False
        elif 17 <= utc_h < 20:
            return "🌐 NY-мид", False
        elif 6 <= utc_h < 7:
            return "🌐 Пре-Лондон", False
        elif 12 <= utc_h < 13:
            return "🌐 Пре-NY", False
        else:
            return "🌏 Азия / ночь", False

    # ── Определение SMC условий ─────────────────────────────────────────────

    def _detect_bos(self, df: pd.DataFrame, direction: str) -> bool:
        """BOS — Break of Structure.
        LONG: последняя закрытая свеча пробила предыдущий свинг-хай вверх.
        SHORT: последняя закрытая свеча пробила предыдущий свинг-лоу вниз.
        """
        strength = max(3, self.cfg.PIVOT_STRENGTH // 2)
        if direction == "LONG":
            ph = self._pivot_highs(df["high"], strength)
            recent = ph.dropna()
            if len(recent) < 1:
                return False
            last_swing_high = recent.iloc[-1]
            # Цена закрытия последней свечи выше последнего свинг-хая
            return df["close"].iloc[-1] > last_swing_high
        else:
            pl = self._pivot_lows(df["low"], strength)
            recent = pl.dropna()
            if len(recent) < 1:
                return False
            last_swing_low = recent.iloc[-1]
            return df["close"].iloc[-1] < last_swing_low

    def _detect_ob(self, df: pd.DataFrame, direction: str, atr_now: float) -> bool:
        """Order Block — зона интереса крупного игрока.
        LONG OB: последний импульсный медвежий бар перед сильным бычьим движением,
                 цена вернулась в эту зону.
        SHORT OB: последний импульсный бычий бар перед сильным медвежьим движением,
                  цена вернулась в эту зону.
        """
        if len(df) < 10:
            return False
        c_now = df["close"].iloc[-1]
        zone  = atr_now * max(self.cfg.ZONE_BUFFER, 0.3)

        if direction == "LONG":
            # Ищем последний медвежий бар (close < open) с сильным телом
            for i in range(len(df) - 4, max(len(df) - 25, 0), -1):
                bar = df.iloc[i]
                body = bar["open"] - bar["close"]
                if body > atr_now * 0.4 and bar["close"] < bar["open"]:
                    # После этого бара должно быть движение вверх
                    future = df.iloc[i + 1: i + 4]
                    if len(future) > 0 and future["close"].max() > bar["open"]:
                        ob_high = bar["open"]
                        ob_low  = bar["low"]
                        # Цена сейчас в зоне OB
                        if ob_low - zone <= c_now <= ob_high + zone:
                            return True
        else:  # SHORT
            for i in range(len(df) - 4, max(len(df) - 25, 0), -1):
                bar = df.iloc[i]
                body = bar["close"] - bar["open"]
                if body > atr_now * 0.4 and bar["close"] > bar["open"]:
                    future = df.iloc[i + 1: i + 4]
                    if len(future) > 0 and future["close"].min() < bar["open"]:
                        ob_low  = bar["open"]
                        ob_high = bar["high"]
                        if ob_low - zone <= c_now <= ob_high + zone:
                            return True
        return False

    def _detect_fvg(self, df: pd.DataFrame, direction: str) -> bool:
        """Fair Value Gap — ценовой дисбаланс (имбаланс).
        Три последовательные свечи, между первой и третьей есть незакрытый зазор.
        LONG FVG: high[i-2] < low[i]   (бычий имбаланс — цена прыгнула вверх)
        SHORT FVG: low[i-2] > high[i]  (медвежий имбаланс — цена прыгнула вниз)
        Ищем незаполненный FVG в последних 20 свечах ближе к текущей цене.
        """
        if len(df) < 5:
            return False
        c_now = df["close"].iloc[-1]
        highs = df["high"].values
        lows  = df["low"].values

        for i in range(len(df) - 2, max(len(df) - 21, 2), -1):
            if direction == "LONG":
                # Бычий FVG: high[i-2] < low[i]
                if highs[i - 2] < lows[i]:
                    gap_top = lows[i]
                    gap_bot = highs[i - 2]
                    # FVG не заполнен если между gap_bot и gap_top нет цены закрытия
                    subsequent = df["low"].iloc[i + 1:].min() if i + 1 < len(df) else gap_top
                    if subsequent > gap_bot:  # не заполнен
                        # Цена сейчас вблизи или внутри FVG
                        if gap_bot * 0.995 <= c_now <= gap_top * 1.02:
                            return True
            else:  # SHORT
                # Медвежий FVG: low[i-2] > high[i]
                if lows[i - 2] > highs[i]:
                    gap_bot = highs[i]
                    gap_top = lows[i - 2]
                    subsequent = df["high"].iloc[i + 1:].max() if i + 1 < len(df) else gap_bot
                    if subsequent < gap_top:  # не заполнен
                        if gap_bot * 0.98 <= c_now <= gap_top * 1.005:
                            return True
        return False

    def _detect_sweep(self, df: pd.DataFrame, direction: str, atr_now: float) -> bool:
        """Liquidity Sweep — ложный пробой уровня ликвидности.
        LONG: цена ушла ниже недавнего свинг-лоу (выбила стопы),
              а затем вернулась выше и закрылась выше этого уровня.
        SHORT: цена ушла выше недавнего свинг-хая и вернулась ниже.
        """
        if len(df) < 6:
            return False
        strength = max(3, self.cfg.PIVOT_STRENGTH // 2)

        if direction == "LONG":
            pl = self._pivot_lows(df["low"], strength)
            recent = pl.dropna().iloc[-4:]
            if len(recent) < 1:
                return False
            for level in recent.values:
                # Смотрим последние 5 свечей
                for i in range(max(1, len(df) - 6), len(df)):
                    bar = df.iloc[i]
                    # Тень ушла ниже уровня
                    swept = bar["low"] < level - atr_now * 0.1
                    # Но закрылась выше
                    recovered = bar["close"] > level
                    if swept and recovered:
                        return True
        else:  # SHORT
            ph = self._pivot_highs(df["high"], strength)
            recent = ph.dropna().iloc[-4:]
            if len(recent) < 1:
                return False
            for level in recent.values:
                for i in range(max(1, len(df) - 6), len(df)):
                    bar = df.iloc[i]
                    swept    = bar["high"] > level + atr_now * 0.1
                    recovered = bar["close"] < level
                    if swept and recovered:
                        return True
        return False

    # ── Основной метод анализа ──────────────────────────────────────────────

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

        # ── Пивоты ────────────────────────────────────────
        strength  = cfg.PIVOT_STRENGTH
        ph        = self._pivot_highs(df["high"], strength)
        pl        = self._pivot_lows(df["low"],   strength)
        res_vals  = ph.dropna().iloc[-5:]
        sup_vals  = pl.dropna().iloc[-5:]

        if len(res_vals) == 0 or len(sup_vals) == 0:
            return None

        zone = atr_now * cfg.ZONE_BUFFER

        # ── Паттерн ──────────────────────────────────────
        bull_pat, bear_pat = self._detect_pattern(df)

        # ── 3. Сессия (получаем один раз для обоих направлений) ─────────────
        session_name, session_prime = self._get_session()

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
            # Даём сигнал если тренд и RSI ok + хотя бы объём или паттерн
            long_signal = trend_ok and htf_ok and rsi_ok and (vol_ok or pattern_ok)

        # ── СИГНАЛ SHORT ─────────────────────────────────
        short_signal = False
        short_level  = None
        short_type   = ""

        for res in res_vals.values[::-1]:
            dist     = abs(c_now - res) / atr_now
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
            short_signal = trend_ok and htf_ok and rsi_ok and (vol_ok or pattern_ok)

        if long_signal and short_signal:
            if rsi_now >= 50:
                long_signal  = False
            else:
                short_signal = False

        if not long_signal and not short_signal:
            return None

        # ── Расчёт уровней ────────────────────────────────
        direction = "LONG" if long_signal else "SHORT"

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

        # ── НОВОЕ v4.6: определяем все SMC условия ──────────────────────────

        # 1. BOS — Break of Structure
        has_bos = self._detect_bos(df, direction) if cfg.SMC_USE_BOS else False

        # 2. Order Block
        has_ob  = self._detect_ob(df, direction, atr_now) if cfg.SMC_USE_OB else False

        # 3. FVG — Fair Value Gap
        has_fvg = self._detect_fvg(df, direction) if cfg.SMC_USE_FVG else False

        # 4. Liquidity Sweep
        has_liq_sweep = self._detect_sweep(df, direction, atr_now) if cfg.SMC_USE_SWEEP else False

        # 5. CHOCH — только если включён
        has_choch = self._detect_choch(df, direction) if cfg.SMC_USE_CHOCH else False

        # 6. HTF Daily Confluence — только если включён
        atr_daily = 0.0
        if df_htf is not None and len(df_htf) > 20:
            daily_atr = self._atr(df_htf, 14)
            atr_daily = daily_atr.iloc[-1]
        htf_confluence = (
            self._htf_daily_confluence(entry, df_htf, atr_daily, direction)
            if cfg.SMC_USE_CONF else False
        )

        # 7. Сессия
        session_name, session_prime = self._get_session()

        # ── Индивидуальные условия для чеклиста ──────────────────────────────
        # Эти же условия используются для расчёта качества — полное соответствие
        ok_rsi = (
            (rsi_now < cfg.RSI_OS) if long_signal else (rsi_now > cfg.RSI_OB)
        ) if cfg.USE_RSI_FILTER else (
            (rsi_now < 50) if long_signal else (rsi_now > 50)
        )
        ok_vol  = vol_ratio >= cfg.VOL_MULT
        ok_pat  = bool(bull_pat if long_signal else bear_pat)
        ok_htf  = bool(htf_bull if long_signal else htf_bear)

        # ── 11 условий — полный список (только те что включены считаются) ────
        conditions = [
            has_bos,
            has_ob,
            has_fvg        if cfg.SMC_USE_FVG    else None,
            has_liq_sweep  if cfg.SMC_USE_SWEEP  else None,
            has_choch      if cfg.SMC_USE_CHOCH  else None,
            ok_rsi         if cfg.USE_RSI_FILTER  else None,
            ok_vol         if cfg.USE_VOLUME_FILTER else None,
            ok_pat         if cfg.USE_PATTERN_FILTER else None,
            ok_htf         if cfg.USE_HTF_FILTER  else None,
            htf_confluence if cfg.SMC_USE_CONF   else None,
            session_prime  if cfg.USE_SESSION_FILTER else None,
        ]

        # Убираем None (выключенные условия), считаем совпавшие
        active    = [c for c in conditions if c is not None]
        # BOS и OB всегда считаются (они основные)
        # Если оба выключены, добавляем базовые условия
        if len(active) == 0:
            active = [True]  # хотя бы 1

        matched = sum(1 for c in active if c)
        total   = len(active)

        # ── Качество 1–5 из тех же условий ───────────────────────────────────
        # Минимум 1, далее пропорционально
        if total == 0:
            quality = 1
        else:
            ratio = matched / total
            if ratio >= 0.85:
                quality = 5
            elif ratio >= 0.65:
                quality = 4
            elif ratio >= 0.45:
                quality = 3
            elif ratio >= 0.25:
                quality = 2
            else:
                quality = 1

        # Гарантируем минимум 1 если сигнал прошёл фильтры
        quality = max(1, min(5, quality))

        reasons = []
        if has_bos:       reasons.append("✅ BOS")
        if has_ob:        reasons.append("✅ Order Block")
        if has_fvg:       reasons.append("✅ FVG")
        if has_liq_sweep: reasons.append("✅ Sweep")
        if has_choch:     reasons.append("✅ CHOCH")
        if ok_rsi:        reasons.append(f"✅ RSI {rsi_now:.1f}")
        if ok_vol:        reasons.append(f"✅ Объём ×{vol_ratio:.1f}")
        if ok_pat:        reasons.append(f"✅ {pattern}")
        if ok_htf:        reasons.append("✅ HTF тренд")
        if htf_confluence:reasons.append("✅ Daily Confluence")
        if session_prime: reasons.append(f"✅ {session_name}")

        self._last_signal[symbol] = bar_idx

        return SignalResult(
            symbol         = symbol,
            direction      = direction,
            entry          = entry,
            sl             = sl,
            tp1            = tp1,
            tp2            = tp2,
            tp3            = tp3,
            risk_pct       = risk_pct,
            quality        = quality,
            smc_score      = matched,
            total_score    = total,
            reasons        = reasons,
            rsi            = rsi_now,
            volume_ratio   = vol_ratio,
            trend_local    = trend_local,
            trend_htf      = trend_htf,
            pattern        = pattern,
            breakout_type  = btype,
            has_bos        = has_bos,
            has_ob         = has_ob,
            has_fvg        = has_fvg,
            has_liq_sweep  = has_liq_sweep,
            has_choch      = has_choch,
            htf_confluence = htf_confluence,
            session_name   = session_name,
            session_prime  = session_prime,
        )
