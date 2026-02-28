"""
CHM BREAKER — Человеческая логика (SFP, Пробой+Ретест, Зоны)
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
    reasons:       list  = field(default_factory=list)
    rsi:           float = 50.0
    volume_ratio:  float = 1.0
    trend_local:   str   = ""
    trend_htf:     str   = ""
    pattern:       str   = ""
    breakout_type: str   = ""
    is_counter_trend: bool = False # Флаг контр-тренда
    human_explanation: str = ""    # Человеческое объяснение сделки


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}

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
        """Кластеризация пивотов в ЗОНЫ (как видит толпа)"""
        highs = df["high"].values
        lows = df["low"].values
        
        res_points = []
        sup_points = []
        
        # Находим экстремумы
        for i in range(strength, len(df) - strength):
            if highs[i] == max(highs[i - strength: i + strength + 1]):
                res_points.append(highs[i])
            if lows[i] == min(lows[i - strength: i + strength + 1]):
                sup_points.append(lows[i])
                
        # Группировка близких уровней (создание зон)
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
            return [c for c in clusters if c["hits"] >= 2] # Берем только те, где 2+ касания
            
        return cluster_levels(sup_points), cluster_levels(res_points)

    def _detect_pattern(self, df) -> tuple[str, str]:
        c = df.iloc[-1]
        p = df.iloc[-2]
        body = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total < 1e-10: return "", ""
        
        uw = c["high"] - max(c["close"], c["open"])
        lw = min(c["close"], c["open"]) - c["low"]
        p_body = abs(p["close"] - p["open"])

        bull, bear = "", ""
        if lw >= body * 1.5 and uw < body and c["close"] >= c["open"]: bull = "Пин-бар покупок"
        elif uw >= body * 1.5 and lw < body and c["close"] <= c["open"]: bear = "Пин-бар продаж"
        elif c["close"] > c["open"] and p["close"] < p["open"] and c["open"] <= p["close"] and c["close"] > p["open"]: bull = "Бычье поглощение"
        elif c["close"] < c["open"] and p["close"] > p["open"] and c["open"] >= p["close"] and c["close"] < p["open"]: bear = "Медвежье поглощение"
        
        return bull, bear

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

        bull_local = c_now > ema50.iloc[-1] > ema200.iloc[-1]
        bear_local = c_now < ema50.iloc[-1] < ema200.iloc[-1]
        trend_local = "📈 Бычий" if bull_local else ("📉 Медвежий" if bear_local else "↔️ Боковик")

        sup_zones, res_zones = self._get_zones(df, cfg.PIVOT_STRENGTH, atr_now)
        if not sup_zones and not res_zones: return None

        bull_pat, bear_pat = self._detect_pattern(df)
        zone_buf = atr_now * cfg.ZONE_BUFFER

        signal, s_level, s_type, explanation = None, None, "", ""
        is_counter = False
        s_hits = 0

        # 1. ЛОЖНЫЙ ПРОБОЙ (SFP) И ОТСКОК ОТ ПОДДЕРЖКИ
        for sup in reversed(sup_zones):
            lvl = sup["price"]
            hits = sup["hits"]

            # SFP (Захват ликвидности)
            if df["low"].iloc[-1] < lvl - zone_buf and c_now > lvl and vol_ratio > 1.2:
                signal, s_level = "LONG", lvl
                s_type, explanation = "SFP (Захват ликвидности)", f"Сильная поддержка (касаний: {hits}). Цена проколола уровень, собрала стопы и вернулась обратно на высоком объеме x{vol_ratio:.1f}."
                is_counter = bear_local; s_hits = hits
                break

            # Классический отскок
            if abs(c_now - lvl) < zone_buf * 2 and bull_pat and vol_ratio >= cfg.VOL_MULT:
                signal, s_level = "LONG", lvl
                s_type, explanation = "Отскок от уровня", f"Цена подошла к зоне поддержки (тест #{hits+1}). Появился паттерн {bull_pat} без давления продавцов."
                is_counter = bear_local; s_hits = hits
                break

        # 2. ПРОБОЙ СОПРОТИВЛЕНИЯ + РЕТЕСТ
        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]
                hits = res["hits"]

                # Честный пробой (свеча закрылась выше, импульс, объем)
                if df["close"].iloc[-2] < lvl and c_now > lvl + zone_buf and vol_ratio > 1.5:
                    signal, s_level = "LONG", lvl
                    s_type, explanation = "Пробой уровня", f"Импульсный пробой сильного сопротивления (касаний: {hits}) на повышенном объеме x{vol_ratio:.1f}. Свеча уверенно закрылась над зоной."
                    is_counter = bear_local; s_hits = hits
                    break

                # Ретест пробитого уровня
                recent_closes = df["close"].iloc[-6:-1]
                if (recent_closes > lvl).any() and abs(df["low"].iloc[-1] - lvl) < zone_buf and bull_pat and vol_ratio < 1.5:
                    signal, s_level = "LONG", lvl
                    s_type, explanation = "Ретест уровня", f"Цена мягко вернулась к пробитому сопротивлению, которое теперь стало поддержкой. Подтверждение паттерном {bull_pat} без агрессивных продаж."
                    is_counter = bear_local; s_hits = hits
                    break

        # 3. ШОРТ СЦЕНАРИИ (Зеркально)
        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]
                hits = res["hits"]

                # SFP Short
                if df["high"].iloc[-1] > lvl + zone_buf and c_now < lvl and vol_ratio > 1.2:
                    signal, s_level = "SHORT", lvl
                    s_type, explanation = "SFP (Ложный пробой)", f"Свинг-хай проколот (забрали ликвидность), но цена быстро вернулась под сопротивление (касаний: {hits}) на объеме x{vol_ratio:.1f}."
                    is_counter = bull_local; s_hits = hits
                    break

                # Отскок Short
                if abs(c_now - lvl) < zone_buf * 2 and bear_pat and vol_ratio >= cfg.VOL_MULT:
                    signal, s_level = "SHORT", lvl
                    s_type, explanation = "Отскок от сопротивления", f"Остановка у зоны сопротивления (тест #{hits+1}). Защита продавцов подтверждается паттерном {bear_pat}."
                    is_counter = bull_local; s_hits = hits
                    break

            for sup in reversed(sup_zones):
                lvl = sup["price"]
                hits = sup["hits"]
                # Пробой поддержки
                if df["close"].iloc[-2] > lvl and c_now < lvl - zone_buf and vol_ratio > 1.5:
                    signal, s_level = "SHORT", lvl
                    s_type, explanation = "Пробой поддержки", f"Честный пробой сильной поддержки вниз на объеме x{vol_ratio:.1f}. Возврата назад нет."
                    is_counter = bull_local; s_hits = hits
                    break
                # Ретест пробитой поддержки
                recent_closes = df["close"].iloc[-6:-1]
                if (recent_closes < lvl).any() and abs(df["high"].iloc[-1] - lvl) < zone_buf and bear_pat and vol_ratio < 1.5:
                    signal, s_level = "SHORT", lvl
                    s_type, explanation = "Ретест уровня", f"Мягкий откат к пробитой поддержке снизу вверх. Появился продавец: {bear_pat}."
                    is_counter = bull_local; s_hits = hits
                    break

        if not signal: return None

        # Фильтры
        if cfg.USE_RSI_FILTER:
            if signal == "LONG" and rsi_now > cfg.RSI_OB: return None
            if signal == "SHORT" and rsi_now < cfg.RSI_OS: return None

        # Расчет риска и целей
        entry = c_now
        if signal == "LONG":
            sl = min(df["low"].iloc[-3:].min(), s_level - zone_buf) - atr_now * cfg.ATR_MULT
            sl = min(sl, entry * (1 - cfg.MAX_RISK_PCT / 100))
            risk = entry - sl
            tp1, tp2, tp3 = entry + risk*cfg.TP1_RR, entry + risk*cfg.TP2_RR, entry + risk*cfg.TP3_RR
        else:
            sl = max(df["high"].iloc[-3:].max(), s_level + zone_buf) + atr_now * cfg.ATR_MULT
            sl = max(sl, entry * (1 + cfg.MAX_RISK_PCT / 100))
            risk = sl - entry
            tp1, tp2, tp3 = entry - risk*cfg.TP1_RR, entry - risk*cfg.TP2_RR, entry - risk*cfg.TP3_RR

        risk_pct = abs((sl - entry) / entry * 100)
        
        # Оценка качества
        quality = 2
        reasons = [f"✅ {s_type}"]
        if s_hits > 0:                 reasons.append(f"✅ Уровень тестировался {s_hits}x")
        if vol_ratio >= cfg.VOL_MULT:  quality += 1; reasons.append(f"✅ Объём x{vol_ratio:.1f}")
        if not is_counter:             quality += 1; reasons.append("✅ По локальному тренду")
        if (signal=="LONG" and bull_pat) or (signal=="SHORT" and bear_pat):
            quality += 1; reasons.append(f"✅ Паттерн: {bull_pat or bear_pat}")
        # HTF качество — только если HTF фильтр включён и данные есть
        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) >= cfg.HTF_EMA_PERIOD:
            htf_ema_val = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD).iloc[-1]
            htf_price   = df_htf["close"].iloc[-1]
            if (signal == "LONG" and htf_price > htf_ema_val) or \
               (signal == "SHORT" and htf_price < htf_ema_val):
                quality += 1
                reasons.append("✅ HTF тренд подтверждает")

        self._last_signal[symbol] = bar_idx

        return SignalResult(
            symbol=symbol, direction=signal, entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            risk_pct=risk_pct, quality=min(quality, 5), reasons=reasons, rsi=rsi_now,
            volume_ratio=vol_ratio, trend_local=trend_local, breakout_type=s_type,
            is_counter_trend=is_counter, human_explanation=explanation
        )
