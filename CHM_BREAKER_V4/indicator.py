"""
CHM BREAKER v5.1 — Hybrid Edition (Full Rewrite)
Добавлено: FVG, Order Blocks, BoS/ChoCH, Volume Delta,
           динамические TP, исправление бага SFP шортов.
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
    symbol:            str
    direction:         str
    entry:             float
    sl:                float
    tp1:               float
    tp2:               float
    tp3:               float
    risk_pct:          float
    quality:           int
    reasons:           list  = field(default_factory=list)
    rsi:               float = 50.0
    volume_ratio:      float = 1.0
    vol_delta:         float = 0.0
    trend_local:       str   = ""
    trend_htf:         str   = ""
    pattern:           str   = ""
    breakout_type:     str   = ""
    is_counter_trend:  bool  = False
    human_explanation: str   = ""
    fvg_near:          bool  = False
    ob_near:           bool  = False
    bos_type:          str   = ""
    confluence_score:  int   = 0


class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}

    # ══════════════════════════════════════════════
    # БАЗОВЫЕ МАТЕМАТИЧЕСКИЕ ФУНКЦИИ
    # ══════════════════════════════════════════════

    @staticmethod
    def _ema(s: pd.Series, n: int) -> pd.Series:
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
        d  = s.diff()
        g  = d.clip(lower=0).ewm(span=n, adjust=False).mean()
        l  = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
        rs = g / l.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
        h, l, pc = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat(
            [(h - l), (h - pc).abs(), (l - pc).abs()], axis=1
        ).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    # ══════════════════════════════════════════════
    # ЗОНЫ (ПОДДЕРЖКА / СОПРОТИВЛЕНИЕ)
    # ══════════════════════════════════════════════

    def _get_zones(
        self, df: pd.DataFrame, strength: int, atr_now: float
    ) -> tuple[list[dict], list[dict]]:
        """Кластеризация пивотов в зоны поддержки и сопротивления."""
        highs = df["high"].values
        lows  = df["low"].values

        res_points, sup_points = [], []

        for i in range(strength, len(df) - strength):
            if highs[i] == max(highs[i - strength: i + strength + 1]):
                res_points.append(highs[i])
            if lows[i] == min(lows[i - strength: i + strength + 1]):
                sup_points.append(lows[i])

        buffer = atr_now * self.cfg.ZONE_BUFFER

        def cluster_levels(points: list) -> list[dict]:
            if not points:
                return []
            points.sort()
            clusters   = []
            curr       = [points[0]]
            for p in points[1:]:
                if p - curr[-1] <= buffer:
                    curr.append(p)
                else:
                    clusters.append({
                        "price": sum(curr) / len(curr),
                        "hits":  len(curr),
                    })
                    curr = [p]
            clusters.append({
                "price": sum(curr) / len(curr),
                "hits":  len(curr),
            })
            return [c for c in clusters if c["hits"] >= 2]

        return cluster_levels(sup_points), cluster_levels(res_points)

    # ══════════════════════════════════════════════
    # FAIR VALUE GAP (FVG)
    # ══════════════════════════════════════════════

    def _get_fvg(
        self, df: pd.DataFrame, atr_now: float
    ) -> list[dict]:
        """
        Bullish FVG: low[i] > high[i-2]  — пробел вверх (потенциальная поддержка).
        Bearish FVG: high[i] < low[i-2]  — пробел вниз (потенциальное сопротивление).
        Возвращает последние 10 незаполненных FVG.
        """
        fvgs = []
        close = df["close"].values

        for i in range(2, len(df) - 1):
            bull_gap = df["low"].iloc[i] - df["high"].iloc[i - 2]
            bear_gap = df["low"].iloc[i - 2] - df["high"].iloc[i]

            # Проверяем, не был ли пробел закрыт позже
            if bull_gap > atr_now * 0.3:
                top    = df["low"].iloc[i]
                bottom = df["high"].iloc[i - 2]
                filled = any(
                    df["low"].iloc[j] <= bottom
                    for j in range(i + 1, len(df))
                )
                if not filled:
                    fvgs.append({
                        "type":   "bull",
                        "top":    top,
                        "bottom": bottom,
                        "idx":    i,
                    })

            elif bear_gap > atr_now * 0.3:
                top    = df["low"].iloc[i - 2]
                bottom = df["high"].iloc[i]
                filled = any(
                    df["high"].iloc[j] >= top
                    for j in range(i + 1, len(df))
                )
                if not filled:
                    fvgs.append({
                        "type":   "bear",
                        "top":    top,
                        "bottom": bottom,
                        "idx":    i,
                    })

        # Только последние 10 незаполненных FVG
        recent = [f for f in fvgs if f["idx"] > len(df) - 80]
        return recent[-10:]

    # ══════════════════════════════════════════════
    # ORDER BLOCKS (OB)
    # ══════════════════════════════════════════════

    def _get_order_blocks(
        self, df: pd.DataFrame, atr_now: float
    ) -> list[dict]:
        """
        Bullish OB: последняя медвежья свеча перед сильным бычьим импульсом.
        Bearish OB: последняя бычья свеча перед сильным медвежьим импульсом.
        """
        obs      = []
        lookback = min(60, len(df) - 3)

        for i in range(lookback, len(df) - 2):
            fwd_bull = df["close"].iloc[i + 1] - df["open"].iloc[i + 1]
            fwd_bear = df["open"].iloc[i + 1] - df["close"].iloc[i + 1]

            if (
                df["close"].iloc[i] < df["open"].iloc[i]  # медвежья свеча
                and fwd_bull > atr_now * 1.5              # сильный рост следом
            ):
                obs.append({
                    "type":   "bull",
                    "top":    df["open"].iloc[i],
                    "bottom": df["low"].iloc[i],
                    "idx":    i,
                })
            elif (
                df["close"].iloc[i] > df["open"].iloc[i]  # бычья свеча
                and fwd_bear > atr_now * 1.5               # сильное падение следом
            ):
                obs.append({
                    "type":   "bear",
                    "top":    df["high"].iloc[i],
                    "bottom": df["open"].iloc[i],
                    "idx":    i,
                })

        return obs[-8:]  # последние 8 OB

    # ══════════════════════════════════════════════
    # BREAK OF STRUCTURE / CHANGE OF CHARACTER
    # ══════════════════════════════════════════════

    @staticmethod
    def _detect_bos_choch(df: pd.DataFrame) -> tuple[str, str]:
        """
        BoS  = продолжение тренда (пробой последнего HH или LL).
        ChoCH = смена структуры (пробой в противоположную сторону).
        Возвращает: (тип, направление) → ("BoS", "BULL") и т.д.
        """
        if len(df) < 20:
            return "None", "NEUTRAL"

        highs  = df["high"].values[-20:]
        lows   = df["low"].values[-20:]
        closes = df["close"].values[-20:]

        last_hh = float(np.max(highs[:-3]))
        last_ll = float(np.min(lows[:-3]))
        c, p    = closes[-1], closes[-2]

        if p < last_hh and c > last_hh:
            return "BoS",   "BULL"
        if p > last_ll and c < last_ll:
            return "BoS",   "BEAR"
        # ChoCH: цена пробила структуру против текущего тренда
        bull_trend = closes[-5] < closes[-1]
        if bull_trend and c < last_ll:
            return "ChoCH", "BEAR"
        if not bull_trend and c > last_hh:
            return "ChoCH", "BULL"

        return "None", "NEUTRAL"

    # ══════════════════════════════════════════════
    # ПАТТЕРНЫ СВЕЧЕЙ
    # ══════════════════════════════════════════════

    @staticmethod
    def _detect_pattern(df: pd.DataFrame) -> tuple[str, str]:
        c     = df.iloc[-1]
        p     = df.iloc[-2]
        body  = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total < 1e-10:
            return "", ""

        uw     = c["high"] - max(c["close"], c["open"])
        lw     = min(c["close"], c["open"]) - c["low"]
        p_body = abs(p["close"] - p["open"])

        bull = bear = ""

        if lw >= body * 1.5 and c["close"] >= c["open"]:
            bull = "🟢 Бычий пин-бар"
        elif uw >= body * 1.5 and c["close"] <= c["open"]:
            bear = "🔴 Медвежий пин-бар"
        elif (
            c["close"] > c["open"]
            and p["close"] < p["open"]
            and c["open"] <= p["close"]
            and c["close"] > p["open"]
            and body >= p_body * 0.8
        ):
            bull = "🟢 Бычье поглощение"
        elif (
            c["close"] < c["open"]
            and p["close"] > p["open"]
            and c["open"] >= p["close"]
            and c["close"] < p["open"]
            and body >= p_body * 0.8
        ):
            bear = "🔴 Медвежье поглощение"
        elif not bull and c["close"] > c["open"] and body >= total * 0.4:
            bull = "🟢 Бычья свеча"
        elif not bear and c["close"] < c["open"] and body >= total * 0.4:
            bear = "🔴 Медвежья свеча"

        return bull, bear

    # ══════════════════════════════════════════════
    # VOLUME DELTA BIAS
    # ══════════════════════════════════════════════

    @staticmethod
    def _vol_delta_bias(df: pd.DataFrame, n: int = 5) -> float:
        """
        Аппроксимация дельты объёма через тело свечи.
        +1.0 = 100% бычий объём, -1.0 = 100% медвежий.
        """
        recent   = df.iloc[-n:]
        bull_vol = recent.loc[
            recent["close"] >= recent["open"], "volume"
        ].sum()
        bear_vol = recent.loc[
            recent["close"] < recent["open"], "volume"
        ].sum()
        total = bull_vol + bear_vol
        return (bull_vol - bear_vol) / total if total > 0 else 0.0

    # ══════════════════════════════════════════════
    # ДИНАМИЧЕСКИЕ TP
    # ══════════════════════════════════════════════

    def _smart_targets(
        self,
        signal:  str,
        entry:   float,
        risk:    float,
        fvgs:    list[dict],
        obs:     list[dict],
    ) -> tuple[float, float, float]:
        """
        TP ставятся на ближайшие структурные уровни (FVG / OB).
        Если структурных целей < 3 — заполняем через RR из конфига.
        """
        cfg = self.cfg
        if signal == "LONG":
            candidates = sorted(
                set(
                    [f["bottom"] for f in fvgs if f["type"] == "bear" and f["bottom"] > entry]
                    + [o["bottom"] for o in obs if o["type"] == "bear" and o["bottom"] > entry]
                )
            )
        else:
            candidates = sorted(
                set(
                    [f["top"] for f in fvgs if f["type"] == "bull" and f["top"] < entry]
                    + [o["top"] for o in obs if o["type"] == "bull" and o["top"] < entry]
                ),
                reverse=True,
            )

        # Фильтр: TP должен быть > 1R
        min_tp_dist = risk * cfg.TP1_RR * 0.8
        candidates  = [
            c for c in candidates
            if abs(c - entry) >= min_tp_dist
        ][:3]

        fallback = [
            entry + risk * r if signal == "LONG" else entry - risk * r
            for r in [cfg.TP1_RR, cfg.TP2_RR, cfg.TP3_RR]
        ]

        while len(candidates) < 3:
            candidates.append(fallback[len(candidates)])

        return candidates[0], candidates[1], candidates[2]

    # ══════════════════════════════════════════════
    # ГЛАВНЫЙ МЕТОД АНАЛИЗА
    # ══════════════════════════════════════════════

    def analyze(
        self,
        symbol: str,
        df: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None,
    ) -> Optional[SignalResult]:

        cfg = self.cfg
        if df is None or len(df) < max(cfg.EMA_SLOW, 100):
            return None

        bar_idx = len(df) - 1

        # Cooldown
        if bar_idx - self._last_signal.get(symbol, -999) < cfg.COOLDOWN_BARS:
            return None

        # ── Базовые серии ──
        close   = df["close"]
        atr     = self._atr(df, cfg.ATR_PERIOD)
        ema50   = self._ema(close, cfg.EMA_FAST)
        ema200  = self._ema(close, cfg.EMA_SLOW)
        rsi     = self._rsi(close, cfg.RSI_PERIOD)
        vol_ma  = df["volume"].rolling(cfg.VOL_LEN).mean()

        c_now    = close.iloc[-1]
        atr_now  = atr.iloc[-1]
        rsi_now  = rsi.iloc[-1]
        vol_now  = df["volume"].iloc[-1]
        vol_avg  = vol_ma.iloc[-1]
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

        # ── Локальный тренд ──
        bull_local  = c_now > ema50.iloc[-1] > ema200.iloc[-1]
        bear_local  = c_now < ema50.iloc[-1] < ema200.iloc[-1]
        trend_local = (
            "📈 Бычий" if bull_local
            else ("📉 Медвежий" if bear_local else "↔️ Боковик")
        )

        # ── HTF тренд ──
        htf_bull = htf_bear = True
        trend_htf = "⏸ Выкл"
        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) > 50:
            htf_ema  = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD)
            htf_bull = df_htf["close"].iloc[-1] > htf_ema.iloc[-1]
            htf_bear = df_htf["close"].iloc[-1] < htf_ema.iloc[-1]
            trend_htf = "📈 Бычий" if htf_bull else "📉 Медвежий"

        # ── Зоны / FVG / OB / BoS / Дельта ──
        sup_zones, res_zones = self._get_zones(df, cfg.PIVOT_STRENGTH, atr_now)
        if not sup_zones and not res_zones:
            return None

        fvgs      = self._get_fvg(df, atr_now)
        obs       = self._get_order_blocks(df, atr_now)
        bos_type, bos_dir = self._detect_bos_choch(df)
        vol_delta = self._vol_delta_bias(df, 5)

        bull_pat, bear_pat = self._detect_pattern(df)
        zone_buf = atr_now * self.cfg.ZONE_BUFFER

        signal = s_level = None
        s_type = explanation = final_pattern = ""
        is_counter = False

        # ══════════════════════════════════════════
        # 1. ЛОНГИ
        # ══════════════════════════════════════════
        for sup in reversed(sup_zones):
            lvl = sup["price"]

            # SFP: ложный пробой поддержки
            if (
                df["low"].iloc[-1] < lvl - zone_buf
                and df["close"].iloc[-1] > lvl       # ← закрылись ВЫШЕ уровня
                and vol_ratio > 1.2
            ):
                signal, s_level, s_type = "LONG", lvl, "SFP (Ложный пробой)"
                explanation = (
                    f"Собрали стопы за поддержкой (касаний: {sup['hits']}) "
                    f"и вернулись на объеме x{vol_ratio:.1f}."
                )
                final_pattern = bull_pat or "🟢 Пин-бар SFP"
                is_counter = bear_local
                break

            # Отбой от поддержки
            if (
                abs(c_now - lvl) < zone_buf * 1.5
                and bull_pat
                and vol_ratio >= cfg.VOL_MULT
            ):
                signal, s_level, s_type = "LONG", lvl, "Отбой от поддержки"
                explanation = (
                    f"Удержание зоны поддержки (касаний: {sup['hits']}). "
                    f"Паттерн: {bull_pat}."
                )
                final_pattern = bull_pat
                is_counter = bear_local
                break

        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]

                # Пробой сопротивления
                if (
                    df["close"].iloc[-2] < lvl
                    and c_now > lvl + zone_buf
                    and vol_ratio > 1.5
                ):
                    signal, s_level, s_type = "LONG", lvl, "Пробой сопротивления"
                    explanation = (
                        f"Импульсный пробой зоны (касаний: {res['hits']}) "
                        f"на объеме x{vol_ratio:.1f}."
                    )
                    final_pattern = bull_pat or "🟢 Импульсная свеча"
                    is_counter = bear_local
                    break

                # Ретест пробитого сопротивления
                if (
                    (df["close"].iloc[-6:-1] > lvl).any()
                    and abs(df["low"].iloc[-1] - lvl) < zone_buf
                    and bull_pat
                ):
                    signal, s_level, s_type = "LONG", lvl, "Ретест сопротивления"
                    explanation = (
                        f"Мягкий возврат к пробитому сопротивлению. "
                        f"Подтверждение: {bull_pat}."
                    )
                    final_pattern = bull_pat
                    is_counter = bear_local
                    break

        # ══════════════════════════════════════════
        # 2. ШОРТЫ
        # ══════════════════════════════════════════
        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]

                # SFP: ложный закол сопротивления — ИСПРАВЛЕНО
                if (
                    df["high"].iloc[-1] > lvl + zone_buf
                    and df["close"].iloc[-1] < lvl   # ← закрылись НИЖЕ уровня
                    and vol_ratio > 1.2
                ):
                    signal, s_level, s_type = "SHORT", lvl, "SFP (Ложный пробой)"
                    explanation = (
                        f"Ложный закол свинг-хая (касаний: {res['hits']}) "
                        f"на объеме x{vol_ratio:.1f}."
                    )
                    final_pattern = bear_pat or "🔴 Пин-бар SFP"
                    is_counter = bull_local
                    break

                # Отбой от сопротивления
                if (
                    abs(c_now - lvl) < zone_buf * 1.5
                    and bear_pat
                    and vol_ratio >= cfg.VOL_MULT
                ):
                    signal, s_level, s_type = "SHORT", lvl, "Отбой от сопротивления"
                    explanation = (
                        f"Остановка у зоны сопротивления. "
                        f"Паттерн: {bear_pat}."
                    )
                    final_pattern = bear_pat
                    is_counter = bull_local
                    break

        if not signal:
            for sup in reversed(sup_zones):
                lvl = sup["price"]

                # Пробой поддержки
                if (
                    df["close"].iloc[-2] > lvl
                    and c_now < lvl - zone_buf
                    and vol_ratio > 1.5
                ):
                    signal, s_level, s_type = "SHORT", lvl, "Пробой поддержки"
                    explanation = (
                        f"Пробой сильной поддержки вниз "
                        f"на объеме x{vol_ratio:.1f}."
                    )
                    final_pattern = bear_pat or "🔴 Импульсная свеча"
                    is_counter = bull_local
                    break

                # Ретест пробитой поддержки
                if (
                    (df["close"].iloc[-6:-1] < lvl).any()
                    and abs(df["high"].iloc[-1] - lvl) < zone_buf
                    and bear_pat
                ):
                    signal, s_level, s_type = "SHORT", lvl, "Ретест поддержки"
                    explanation = (
                        f"Откат к пробитой поддержке снизу вверх. "
                        f"Появился продавец: {bear_pat}."
                    )
                    final_pattern = bear_pat
                    is_counter = bull_local
                    break

        if not signal:
            return None

        # ══════════════════════════════════════════
        # 3. ЖЁСТКИЕ ФИЛЬТРЫ
        # ══════════════════════════════════════════
        if cfg.USE_HTF_FILTER:
            if signal == "LONG"  and not htf_bull: return None
            if signal == "SHORT" and not htf_bear: return None

        if cfg.USE_RSI_FILTER:
            if signal == "LONG"  and rsi_now > cfg.RSI_OB: return None
            if signal == "SHORT" and rsi_now < cfg.RSI_OS: return None

        # BoS/ChoCH фильтр: блокируем сигнал против структуры
        if bos_type == "BoS":
            if signal == "LONG"  and bos_dir == "BEAR": return None
            if signal == "SHORT" and bos_dir == "BULL": return None

        # ══════════════════════════════════════════
        # 4. РАСЧЁТ ВХОДА, SL
        # ══════════════════════════════════════════
        entry = c_now

        if signal == "LONG":
            sl = min(
                df["low"].iloc[-3:].min(),
                s_level - zone_buf,
            ) - atr_now * cfg.ATR_MULT * 0.5
            sl   = min(sl, entry * (1 - cfg.MAX_RISK_PCT / 100))
            risk = entry - sl
        else:
            sl = max(
                df["high"].iloc[-3:].max(),
                s_level + zone_buf,
            ) + atr_now * cfg.ATR_MULT * 0.5
            sl   = max(sl, entry * (1 + cfg.MAX_RISK_PCT / 100))
            risk = sl - entry

        # ── Динамические TP ──
        tp1, tp2, tp3 = self._smart_targets(signal, entry, risk, fvgs, obs)

        risk_pct = abs((sl - entry) / entry * 100)

        # ══════════════════════════════════════════
        # 5. CONFLUENCE — ОЦЕНКА КАЧЕСТВА
        # ══════════════════════════════════════════
        quality = 1
        reasons = [f"✅ {s_type}"]

        if vol_ratio >= cfg.VOL_MULT:
            quality += 1
            reasons.append(f"✅ Объем x{vol_ratio:.1f}")

        if not is_counter:
            quality += 1
            reasons.append("✅ По локальному тренду")

        if (signal == "LONG" and htf_bull) or (signal == "SHORT" and htf_bear):
            quality += 1
            reasons.append("✅ HTF тренд подтверждает")

        if (signal == "LONG" and rsi_now < 50) or (signal == "SHORT" and rsi_now > 50):
            quality += 1
            reasons.append(f"✅ RSI {rsi_now:.1f}")

        # Volume delta
        if signal == "LONG" and vol_delta > 0.2:
            quality += 1
            reasons.append(f"✅ Дельта объёма бычья ({vol_delta:.2f})")
        elif signal == "SHORT" and vol_delta < -0.2:
            quality += 1
            reasons.append(f"✅ Дельта объёма медвежья ({vol_delta:.2f})")

        # BoS/ChoCH подтверждение
        if bos_type != "None":
            if (signal == "LONG" and bos_dir == "BULL") or (signal == "SHORT" and bos_dir == "BEAR"):
                quality += 1
                reasons.append(f"✅ {bos_type} подтверждает ({bos_dir})")

        # FVG рядом с входом
        fvg_near = any(
            (
                f["type"] == "bull" and f["bottom"] <= entry <= f["top"]
                if signal == "LONG"
                else f["type"] == "bear" and f["bottom"] <= entry <= f["top"]
            )
            for f in fvgs
        )
        if fvg_near:
            quality += 1
            reasons.append("✅ Вход в зону FVG")
            explanation += " Усилено незаполненным FVG."

        # OB рядом с входом
        ob_near = any(
            (
                o["type"] == "bull" and o["bottom"] <= entry <= o["top"]
                if signal == "LONG"
                else o["type"] == "bear" and o["bottom"] <= entry <= o["top"]
            )
            for o in obs
        )
        if ob_near:
            quality += 1
            reasons.append("✅ Вход в Order Block")
            explanation += " Зона Order Block."

        quality          = min(quality, 5)
        confluence_score = quality  # для UI в Telegram

        self._last_signal[symbol] = bar_idx

        return SignalResult(
            symbol            = symbol,
            direction         = signal,
            entry             = entry,
            sl                = sl,
            tp1               = tp1,
            tp2               = tp2,
            tp3               = tp3,
            risk_pct          = risk_pct,
            quality           = quality,
            reasons           = reasons,
            rsi               = rsi_now,
            volume_ratio      = vol_ratio,
            vol_delta         = vol_delta,
            trend_local       = trend_local,
            trend_htf         = trend_htf,
            pattern           = final_pattern,
            breakout_type     = s_type,
            is_counter_trend  = is_counter,
            human_explanation = explanation,
            fvg_near          = fvg_near,
            ob_near           = ob_near,
            bos_type          = f"{bos_type} {bos_dir}".strip(),
            confluence_score  = confluence_score,
        )
