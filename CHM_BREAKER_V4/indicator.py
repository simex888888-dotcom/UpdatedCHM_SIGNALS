"""
CHM BREAKER v5 — Hybrid Edition
Объединяет математическую точность v4 и человеческую логику чтения графика (Зоны, SFP).
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
    symbol:           str
    direction:        str
    entry:            float
    sl:               float
    tp1:              float
    tp2:              float
    tp3:              float
    risk_pct:         float
    quality:          int
    reasons:          list  = field(default_factory=list)
    rsi:              float = 50.0
    volume_ratio:     float = 1.0
    trend_local:      str   = ""
    trend_htf:        str   = ""
    pattern:          str   = ""
    breakout_type:    str   = ""
    is_counter_trend: bool  = False 
    human_explanation: str  = ""


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}

    # ── БАЗОВЫЕ МАТЕМАТИЧЕСКИЕ ФУНКЦИИ (ИЗ v4) ──

    @staticmethod
    def _ema(s, n):
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s, n=14):
        d  = s.diff()
        g  = d.clip(lower=0).ewm(span=n, adjust=False).mean()
        l  = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
        rs = g / l.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _atr(df, n=14):
        h, l, pc = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h-l), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    def _get_zones(self, df: pd.DataFrame, strength: int, atr_now: float):
        """Кластеризация пивотов в зоны (Новая логика на базе массивов v4)"""
        highs = df["high"].values
        lows = df["low"].values
        
        res_points = []
        sup_points = []
        
        for i in range(strength, len(df) - strength):
            if highs[i] == max(highs[i - strength: i + strength + 1]):
                res_points.append(highs[i])
            if lows[i] == min(lows[i - strength: i + strength + 1]):
                sup_points.append(lows[i])
                
        buffer = atr_now * self.cfg.ZONE_BUFFER
        
        def cluster_levels(points):
            if not points: return []
            points.sort()
            clusters = []
            curr_cluster = [points[0]]
            for p in points[1:]:
                if p - curr_cluster[-1] <= buffer:
                    curr_cluster.append(p)
                else:
                    clusters.append({"price": sum(curr_cluster)/len(curr_cluster), "hits": len(curr_cluster)})
                    curr_cluster = [p]
            clusters.append({"price": sum(curr_cluster)/len(curr_cluster), "hits": len(curr_cluster)})
            return [c for c in clusters if c["hits"] >= 2]
            
        return cluster_levels(sup_points), cluster_levels(res_points)

    def _detect_pattern(self, df) -> tuple[str, str]:
        """Строгое определение паттернов (Из v4)"""
        c = df.iloc[-1]
        p = df.iloc[-2]
        body = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total < 1e-10: return "", ""
        
        uw = c["high"] - max(c["close"], c["open"])
        lw = min(c["close"], c["open"]) - c["low"]
        p_body = abs(p["close"] - p["open"])

        bull, bear = "", ""
        if lw >= body * 1.5 and c["close"] >= c["open"]: bull = "🟢 Бычий пин-бар"
        elif uw >= body * 1.5 and c["close"] <= c["open"]: bear = "🔴 Медвежий пин-бар"
        elif c["close"] > c["open"] and p["close"] < p["open"] and c["open"] <= p["close"] and c["close"] > p["open"] and body >= p_body * 0.8: bull = "🟢 Бычье поглощение"
        elif c["close"] < c["open"] and p["close"] > p["open"] and c["open"] >= p["close"] and c["close"] < p["open"] and body >= p_body * 0.8: bear = "🔴 Медвежье поглощение"
        elif not bull and c["close"] > c["open"] and body >= total * 0.4: bull = "🟢 Бычья свеча"
        elif not bear and c["close"] < c["open"] and body >= total * 0.4: bear = "🔴 Медвежья свеча"
        
        return bull, bear

    # ── ОСНОВНОЙ АНАЛИЗ ──

    def analyze(self, symbol: str, df: pd.DataFrame, df_htf=None) -> Optional[SignalResult]:
        cfg = self.cfg
        if df is None or len(df) < max(cfg.EMA_SLOW, 100): return None
        bar_idx = len(df) - 1

        if bar_idx - self._last_signal.get(symbol, -999) < cfg.COOLDOWN_BARS: return None

        close = df["close"]
        atr = self._atr(df, cfg.ATR_PERIOD)
        ema50 = self._ema(close, cfg.EMA_FAST)
        ema200 = self._ema(close, cfg.EMA_SLOW)
        rsi = self._rsi(close, cfg.RSI_PERIOD)
        vol_ma = df["volume"].rolling(cfg.VOL_LEN).mean()

        c_now = close.iloc[-1]
        atr_now = atr.iloc[-1]
        rsi_now = rsi.iloc[-1]
        vol_now = df["volume"].iloc[-1]
        vol_avg = vol_ma.iloc[-1]
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

        # Тренд локальный
        bull_local = c_now > ema50.iloc[-1] > ema200.iloc[-1]
        bear_local = c_now < ema50.iloc[-1] < ema200.iloc[-1]
        trend_local = "📈 Бычий" if bull_local else ("📉 Медвежий" if bear_local else "↔️ Боковик")

        # Тренд старшего ТФ (ИЗ v4)
        htf_bull = htf_bear = True
        trend_htf = "⏸ Выкл"
        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) > 50:
            htf_ema = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD)
            htf_bull = df_htf["close"].iloc[-1] > htf_ema.iloc[-1]
            htf_bear = df_htf["close"].iloc[-1] < htf_ema.iloc[-1]
            trend_htf = "📈 Бычий" if htf_bull else "📉 Медвежий"

        sup_zones, res_zones = self._get_zones(df, cfg.PIVOT_STRENGTH, atr_now)
        if not sup_zones and not res_zones: return None

        bull_pat, bear_pat = self._detect_pattern(df)
        zone_buf = atr_now * cfg.ZONE_BUFFER

        signal, s_level, s_type, explanation = None, None, "", ""
        is_counter = False
        final_pattern = ""

        # ── 1. ЛОНГИ (SFP, Отскок, Пробой, Ретест) ──
        for sup in reversed(sup_zones):
            lvl = sup["price"]
            
            if df["low"].iloc[-1] < lvl - zone_buf and c_now > lvl and vol_ratio > 1.2:
                signal, s_level, s_type = "LONG", lvl, "SFP (Ложный пробой)"
                explanation = f"Собрали стопы за поддержкой (касаний: {sup['hits']}) и вернулись на объеме x{vol_ratio:.1f}."
                final_pattern = bull_pat or "🟢 Пин-бар SFP"
                is_counter = bear_local
                break
                
            if abs(c_now - lvl) < zone_buf * 1.5 and bull_pat and vol_ratio >= cfg.VOL_MULT:
                signal, s_level, s_type = "LONG", lvl, "Отбой от поддержки"
                explanation = f"Удержание зоны поддержки (касаний: {sup['hits']}). Паттерн: {bull_pat}."
                final_pattern = bull_pat
                is_counter = bear_local
                break

        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]
                
                if df["close"].iloc[-2] < lvl and c_now > lvl + zone_buf and vol_ratio > 1.5:
                    signal, s_level, s_type = "LONG", lvl, "Пробой сопротивления"
                    explanation = f"Импульсный пробой зоны (касаний: {res['hits']}) на объеме x{vol_ratio:.1f}."
                    final_pattern = bull_pat or "🟢 Импульсная свеча"
                    is_counter = bear_local
                    break

                if (df["close"].iloc[-6:-1] > lvl).any() and abs(df["low"].iloc[-1] - lvl) < zone_buf and bull_pat:
                    signal, s_level, s_type = "LONG", lvl, "Ретест сопротивления"
                    explanation = f"Мягкий возврат к пробитому сопротивлению. Подтверждение паттерном {bull_pat}."
                    final_pattern = bull_pat
                    is_counter = bear_local
                    break

        # ── 2. ШОРТЫ (Зеркально) ──
        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]
                
                if df["high"].iloc[-1] > lvl + zone_buf and c_now < lvl and vol_ratio > 1.2:
                    signal, s_level, s_type = "SHORT", lvl, "SFP (Ложный пробой)"
                    explanation = f"Ложный закол свинг-хая (касаний: {res['hits']}) на объеме x{vol_ratio:.1f}."
                    final_pattern = bear_pat or "🔴 Пин-бар SFP"
                    is_counter = bull_local
                    break
                    
                if abs(c_now - lvl) < zone_buf * 1.5 and bear_pat and vol_ratio >= cfg.VOL_MULT:
                    signal, s_level, s_type = "SHORT", lvl, "Отбой от сопротивления"
                    explanation = f"Остановка у зоны сопротивления. Паттерн: {bear_pat}."
                    final_pattern = bear_pat
                    is_counter = bull_local
                    break

            for sup in reversed(sup_zones):
                lvl = sup["price"]
                if df["close"].iloc[-2] > lvl and c_now < lvl - zone_buf and vol_ratio > 1.5:
                    signal, s_level, s_type = "SHORT", lvl, "Пробой поддержки"
                    explanation = f"Пробой сильной поддержки вниз на объеме x{vol_ratio:.1f}."
                    final_pattern = bear_pat or "🔴 Импульсная свеча"
                    is_counter = bull_local
                    break

                if (df["close"].iloc[-6:-1] < lvl).any() and abs(df["high"].iloc[-1] - lvl) < zone_buf and bear_pat:
                    signal, s_level, s_type = "SHORT", lvl, "Ретест поддержки"
                    explanation = f"Откат к пробитой поддержке снизу вверх. Появился продавец: {bear_pat}."
                    final_pattern = bear_pat
                    is_counter = bull_local
                    break

        if not signal: return None

        # ── 3. ЖЕСТКИЕ ФИЛЬТРЫ (ИЗ v4) ──
        if cfg.USE_HTF_FILTER:
            if signal == "LONG" and not htf_bull: return None
            if signal == "SHORT" and not htf_bear: return None

        if cfg.USE_RSI_FILTER:
            if signal == "LONG" and rsi_now > cfg.RSI_OB: return None
            if signal == "SHORT" and rsi_now < cfg.RSI_OS: return None

        # Расчет риска и целей
        entry = c_now
        if signal == "LONG":
            sl = min(df["low"].iloc[-3:].min(), s_level - zone_buf) - atr_now * cfg.ATR_MULT * 0.5
            sl = min(sl, entry * (1 - cfg.MAX_RISK_PCT / 100))
            risk = entry - sl
            tp1, tp2, tp3 = entry + risk*cfg.TP1_RR, entry + risk*cfg.TP2_RR, entry + risk*cfg.TP3_RR
        else:
            sl = max(df["high"].iloc[-3:].max(), s_level + zone_buf) + atr_now * cfg.ATR_MULT * 0.5
            sl = max(sl, entry * (1 + cfg.MAX_RISK_PCT / 100))
            risk = sl - entry
            tp1, tp2, tp3 = entry - risk*cfg.TP1_RR, entry - risk*cfg.TP2_RR, entry - risk*cfg.TP3_RR

        risk_pct = abs((sl - entry) / entry * 100)
        
        # Оценка качества
        quality = 1
        reasons = [f"✅ {s_type}"]
        if vol_ratio >= cfg.VOL_MULT: quality += 1; reasons.append(f"✅ Объем x{vol_ratio:.1f}")
        if not is_counter: quality += 1; reasons.append("✅ По локальному тренду")
        if (signal=="LONG" and htf_bull) or (signal=="SHORT" and htf_bear): quality += 1; reasons.append("✅ HTF Тренд подтверждает")
        if (signal=="LONG" and rsi_now < 50) or (signal=="SHORT" and rsi_now > 50): quality += 1; reasons.append(f"✅ RSI {rsi_now:.1f}")

        self._last_signal[symbol] = bar_idx

        return SignalResult(
            symbol=symbol, direction=signal, entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            risk_pct=risk_pct, quality=min(quality, 5), reasons=reasons, rsi=rsi_now,
            volume_ratio=vol_ratio, trend_local=trend_local, trend_htf=trend_htf,
            pattern=final_pattern, breakout_type=s_type, is_counter_trend=is_counter, human_explanation=explanation
        )
