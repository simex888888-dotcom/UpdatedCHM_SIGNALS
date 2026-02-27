"""
CHM BREAKER v4.2 — Classic Edition
Зоны поддержки/сопротивления, SFP, пробои, ретесты.
Без SMC (нет FVG, OB, BoS/ChoCH, Volume Delta).

v4.2.1 — добавлено:
  • _zone_quality()        — фильтр шумных уровней
  • _level_strength()      — старение пробитых уровней
  • _breakout_confirmed()  — подтверждение удержания после пробоя
  • _check_rr()            — фильтр R:R до входа
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
    trend_local:       str   = ""
    trend_htf:         str   = ""
    pattern:           str   = ""
    breakout_type:     str   = ""
    is_counter_trend:  bool  = False
    human_explanation: str   = ""


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
        d = s.diff()
        g = d.clip(lower=0).ewm(span=n, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
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
        """
        Кластеризация пивотов в зоны.
        Зона формируется если минимум 2 пивота попали в один кластер.
        """
        highs = df["high"].values
        lows  = df["low"].values

        res_points: list[float] = []
        sup_points: list[float] = []

        for i in range(strength, len(df) - strength):
            if highs[i] == max(highs[i - strength: i + strength + 1]):
                res_points.append(highs[i])
            if lows[i] == min(lows[i - strength: i + strength + 1]):
                sup_points.append(lows[i])

        buffer = atr_now * self.cfg.ZONE_BUFFER

        def cluster_levels(points: list[float]) -> list[dict]:
            if not points:
                return []
            points.sort()
            clusters = []
            curr     = [points[0]]
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
    # КАЧЕСТВО И СИЛА УРОВНЯ          ← НОВОЕ v4.2.1
    # ══════════════════════════════════════════════

    def _zone_quality(
        self, df: pd.DataFrame, level: float, atr_now: float
    ) -> float:
        """
        Оценка «чистоты» уровня: 0.0 (шум) → 1.0 (чёткий).

        Логика как у человека:
          - смотрим все свечи, которые касались зоны ±2*buffer
          - если вокруг уровня много свечей с маленьким телом (доджи,
            боковик) — уровень шумный, штраф
          - если свечи вокруг имеют крупные тела (чёткая реакция) — бонус
          - слишком много касаний (>15) — признак «каши», дополнительный штраф
        """
        buffer = atr_now * self.cfg.ZONE_BUFFER * 2
        near   = df[
            (df["high"] >= level - buffer) &
            (df["low"]  <= level + buffer)
        ]
        if len(near) == 0:
            return 1.0

        total    = len(near)
        avg_body = (
            (near["close"] - near["open"]).abs() /
            (near["high"]  - near["low"] + 1e-10)
        ).mean()

        # Штраф за переизбыток касаний (каша)
        noise_penalty = min(total / 20.0, 0.5)
        quality       = avg_body - noise_penalty
        return max(0.0, min(1.0, quality))

    def _level_strength(
        self, df: pd.DataFrame, level: float, atr_now: float
    ) -> int:
        """
        Считает «живые» касания уровня.

        Логика как у человека:
          - каждое касание без уверенного пробоя = +1 к силе уровня
          - уверенный пробой (свеча закрылась за уровнем на >0.5 ATR)
            = отнимает 2 касания (уровень ослабляется)
          - если живых касаний < 1 — уровень мёртвый, не используем

        Возвращает количество живых касаний (может быть 0 или отрицательным).
        """
        buffer       = atr_now * self.cfg.ZONE_BUFFER
        strong_break = atr_now * 0.5
        touches      = 0
        breaks       = 0

        for i in range(len(df) - 1):
            high  = df["high"].iloc[i]
            low   = df["low"].iloc[i]
            close = df["close"].iloc[i]

            near_res = abs(high  - level) < buffer
            near_sup = abs(low   - level) < buffer

            if near_res or near_sup:
                touches += 1
                if close > level + strong_break:
                    breaks += 1
                elif close < level - strong_break:
                    breaks += 1

        return max(0, touches - breaks)

    # ══════════════════════════════════════════════
    # ПОДТВЕРЖДЕНИЕ ПРОБОЯ             ← НОВОЕ v4.2.1
    # ══════════════════════════════════════════════

    def _breakout_confirmed(
        self,
        df:        pd.DataFrame,
        level:     float,
        direction: str,
        atr_now:   float,
    ) -> bool:
        """
        Проверяет что пробой реальный, а не шум:
          - свеча пробоя [-2] закрылась за уровнем
          - следующая свеча [-1] тоже удержалась за уровнем
          (не вернулась сразу назад — нет ложного пробоя)

        direction: "up" — пробой вверх (LONG), "down" — пробой вниз (SHORT).
        """
        if len(df) < 4:
            return False

        c_prev  = df["close"].iloc[-2]   # свеча пробоя
        c_now   = df["close"].iloc[-1]   # свеча после пробоя
        buffer  = atr_now * self.cfg.ZONE_BUFFER * 0.5

        if direction == "up":
            return c_prev > level + buffer and c_now > level
        else:
            return c_prev < level - buffer and c_now < level

    # ══════════════════════════════════════════════
    # ФИЛЬТР R:R                       ← НОВОЕ v4.2.1
    # ══════════════════════════════════════════════

    @staticmethod
    def _check_rr(
        entry:   float,
        sl:      float,
        target:  float,
        min_rr:  float = 2.0,
    ) -> bool:
        """
        Проверяет что потенциальная прибыль к риску ≥ min_rr.
        Если R:R хуже — сигнал не стоит брать.

        Пример: entry=100, sl=98, tp1=104 → risk=2, reward=4 → RR=2.0 ✅
        """
        risk   = abs(entry - sl)
        reward = abs(target - entry)
        if risk < 1e-10:
            return False
        return (reward / risk) >= min_rr

    # ══════════════════════════════════════════════
    # ПАТТЕРНЫ СВЕЧЕЙ
    # ══════════════════════════════════════════════

    @staticmethod
    def _detect_pattern(df: pd.DataFrame) -> tuple[str, str]:
        """
        Определяет паттерн последней свечи.
        Возвращает (bull_pattern, bear_pattern).
        """
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

        # Пин-бары
        if lw >= body * 1.5 and c["close"] >= c["open"]:
            bull = "🟢 Бычий пин-бар"
        elif uw >= body * 1.5 and c["close"] <= c["open"]:
            bear = "🔴 Медвежий пин-бар"

        # Поглощения
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

        # Сильные свечи (fallback)
        elif not bull and c["close"] > c["open"] and body >= total * 0.4:
            bull = "🟢 Бычья свеча"
        elif not bear and c["close"] < c["open"] and body >= total * 0.4:
            bear = "🔴 Медвежья свеча"

        return bull, bear

    # ══════════════════════════════════════════════
    # ГЛАВНЫЙ МЕТОД АНАЛИЗА
    # ══════════════════════════════════════════════

    def analyze(
        self,
        symbol:  str,
        df:      pd.DataFrame,
        df_htf:  Optional[pd.DataFrame] = None,
    ) -> Optional[SignalResult]:

        cfg = self.cfg
        if df is None or len(df) < max(cfg.EMA_SLOW, 100):
            return None

        bar_idx = len(df) - 1

        # Cooldown: не генерируем сигнал слишком часто
        if bar_idx - self._last_signal.get(symbol, -999) < cfg.COOLDOWN_BARS:
            return None

        # ── Базовые серии ──────────────────────────
        close  = df["close"]
        atr    = self._atr(df, cfg.ATR_PERIOD)
        ema50  = self._ema(close, cfg.EMA_FAST)
        ema200 = self._ema(close, cfg.EMA_SLOW)
        rsi    = self._rsi(close, cfg.RSI_PERIOD)
        vol_ma = df["volume"].rolling(cfg.VOL_LEN).mean()

        c_now     = close.iloc[-1]
        atr_now   = atr.iloc[-1]
        rsi_now   = rsi.iloc[-1]
        vol_now   = df["volume"].iloc[-1]
        vol_avg   = vol_ma.iloc[-1]
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

        # ── Локальный тренд ────────────────────────
        bull_local  = c_now > ema50.iloc[-1] > ema200.iloc[-1]
        bear_local  = c_now < ema50.iloc[-1] < ema200.iloc[-1]
        trend_local = (
            "📈 Бычий"   if bull_local
            else ("📉 Медвежий" if bear_local else "↔️ Боковик")
        )

        # ── HTF тренд ─────────────────────────────
        htf_bull = htf_bear = True
        trend_htf = "⏸ Выкл"
        if cfg.USE_HTF_FILTER and df_htf is not None and len(df_htf) > 50:
            htf_ema   = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD)
            htf_bull  = df_htf["close"].iloc[-1] > htf_ema.iloc[-1]
            htf_bear  = df_htf["close"].iloc[-1] < htf_ema.iloc[-1]
            trend_htf = "📈 Бычий" if htf_bull else "📉 Медвежий"

        # ── Зоны ──────────────────────────────────
        sup_zones, res_zones = self._get_zones(df, cfg.PIVOT_STRENGTH, atr_now)
        if not sup_zones and not res_zones:
            return None

        bull_pat, bear_pat = self._detect_pattern(df)
        zone_buf = atr_now * cfg.ZONE_BUFFER

        signal = s_level = None
        s_type = explanation = final_pattern = ""
        is_counter = False

        # ══════════════════════════════════════════
        # 1. ЛОНГИ
        # ══════════════════════════════════════════

        for sup in reversed(sup_zones):
            lvl = sup["price"]

            # SFP: ложный пробой поддержки снизу
            if (
                df["low"].iloc[-1] < lvl - zone_buf
                and df["close"].iloc[-1] > lvl
                and vol_ratio > 1.2
            ):
                signal, s_level, s_type = "LONG", lvl, "SFP (Ложный пробой)"
                explanation = (
                    f"Собрали стопы за поддержкой (касаний: {sup['hits']}) "
                    f"и вернулись на объёме ×{vol_ratio:.1f}."
                )
                final_pattern = bull_pat or "🟢 Пин-бар SFP"
                is_counter     = bear_local
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
                is_counter     = bear_local
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
                        f"на объёме ×{vol_ratio:.1f}."
                    )
                    final_pattern = bull_pat or "🟢 Импульсная свеча"
                    is_counter     = bear_local
                    break

                # Ретест пробитого сопротивления
                if (
                    (df["close"].iloc[-6:-1] > lvl).any()
                    and abs(df["low"].iloc[-1] - lvl) < zone_buf
                    and bull_pat
                ):
                    signal, s_level, s_type = "LONG", lvl, "Ретест сопротивления"
                    explanation = (
                        f"Возврат к пробитому сопротивлению. "
                        f"Подтверждение: {bull_pat}."
                    )
                    final_pattern = bull_pat
                    is_counter     = bear_local
                    break

        # ══════════════════════════════════════════
        # 2. ШОРТЫ
        # ══════════════════════════════════════════

        if not signal:
            for res in reversed(res_zones):
                lvl = res["price"]

                # SFP: ложный пробой сопротивления сверху
                if (
                    df["high"].iloc[-1] > lvl + zone_buf
                    and df["close"].iloc[-1] < lvl
                    and vol_ratio > 1.2
                ):
                    signal, s_level, s_type = "SHORT", lvl, "SFP (Ложный пробой)"
                    explanation = (
                        f"Ложный закол свинг-хая (касаний: {res['hits']}) "
                        f"на объёме ×{vol_ratio:.1f}."
                    )
                    final_pattern = bear_pat or "🔴 Пин-бар SFP"
                    is_counter     = bull_local
                    break

                # Отбой от сопротивления
                if (
                    abs(c_now - lvl) < zone_buf * 1.5
                    and bear_pat
                    and vol_ratio >= cfg.VOL_MULT
                ):
                    signal, s_level, s_type = "SHORT", lvl, "Отбой от сопротивления"
                    explanation = (
                        f"Остановка у зоны сопротивления (касаний: {res['hits']}). "
                        f"Паттерн: {bear_pat}."
                    )
                    final_pattern = bear_pat
                    is_counter     = bull_local
                    break

        if not signal:
            for sup in reversed(sup_zones):
                lvl = sup["price"]

                # Пробой поддержки вниз
                if (
                    df["close"].iloc[-2] > lvl
                    and c_now < lvl - zone_buf
                    and vol_ratio > 1.5
                ):
                    signal, s_level, s_type = "SHORT", lvl, "Пробой поддержки"
                    explanation = (
                        f"Пробой сильной поддержки вниз "
                        f"на объёме ×{vol_ratio:.1f}."
                    )
                    final_pattern = bear_pat or "🔴 Импульсная свеча"
                    is_counter     = bull_local
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
                    is_counter     = bull_local
                    break

        if not signal:
            return None

        # ══════════════════════════════════════════
        # 3. ЖЁСТКИЕ ФИЛЬТРЫ (оригинал)
        # ══════════════════════════════════════════

        if cfg.USE_HTF_FILTER:
            if signal == "LONG"  and not htf_bull: return None
            if signal == "SHORT" and not htf_bear: return None

        if cfg.USE_RSI_FILTER:
            if signal == "LONG"  and rsi_now > cfg.RSI_OB: return None
            if signal == "SHORT" and rsi_now < cfg.RSI_OS: return None

        # ══════════════════════════════════════════
        # 3б. ФИЛЬТРЫ КАЧЕСТВА УРОВНЯ  ← НОВОЕ v4.2.1
        # ══════════════════════════════════════════

        # Чистота уровня: шумная зона → пропускаем
        zone_q = self._zone_quality(df, s_level, atr_now)
        if zone_q < 0.05:
            log.debug(
                f"{symbol}: уровень шумный "
                f"(quality={zone_q:.2f}), пропуск"
            )
            return None

        # Живая сила уровня: слишком много пробоев → уровень мёртвый
        lvl_strength = self._level_strength(df, s_level, atr_now)
        if lvl_strength < 1:
            log.debug(
                f"{symbol}: уровень ослаблен "
                f"(strength={lvl_strength}), пропуск"
            )
            return None

        # Подтверждение удержания после пробоя
        # (только для пробойных сигналов, не для ретестов и отскоков)
        if "Пробой" in s_type and "Ретест" not in s_type:
            brk_dir = "up" if signal == "LONG" else "down"
            if not self._breakout_confirmed(df, s_level, brk_dir, atr_now):
                log.debug(
                    f"{symbol}: пробой не подтверждён "
                    f"(цена вернулась), пропуск"
                )
                return None

        # ══════════════════════════════════════════
        # 4. РАСЧЁТ ВХОДА / SL / TP (оригинал)
        # ══════════════════════════════════════════

        entry = c_now

        if signal == "LONG":
            sl = (
                min(df["low"].iloc[-3:].min(), s_level - zone_buf)
                - atr_now * cfg.ATR_MULT * 0.5
            )
            sl   = min(sl, entry * (1 - cfg.MAX_RISK_PCT / 100))
            risk = entry - sl
        else:
            sl = (
                max(df["high"].iloc[-3:].max(), s_level + zone_buf)
                + atr_now * cfg.ATR_MULT * 0.5
            )
            sl   = max(sl, entry * (1 + cfg.MAX_RISK_PCT / 100))
            risk = sl - entry

        sign = 1 if signal == "LONG" else -1
        tp1  = entry + sign * risk * cfg.TP1_RR
        tp2  = entry + sign * risk * cfg.TP2_RR
        tp3  = entry + sign * risk * cfg.TP3_RR

        risk_pct = abs((sl - entry) / entry * 100)

        # ══════════════════════════════════════════
        # 4б. ФИЛЬТР R:R               ← НОВОЕ v4.2.1
        # ══════════════════════════════════════════

        min_rr = getattr(cfg, "MIN_RR", 1.3)
        if not self._check_rr(entry, sl, tp2, min_rr):   # ← tp2 вместо tp1
            log.debug(
                f"{symbol}: R:R слабый "
                f"(risk={abs(entry - sl):.5f} "
                f"reward={abs(tp2 - entry):.5f}), пропуск"
            )
            return None


        # ══════════════════════════════════════════
        # 5. ОЦЕНКА КАЧЕСТВА (оригинал + новые бонусы)
        # ══════════════════════════════════════════

        quality = 1
        reasons = [f"✅ {s_type}"]

        if vol_ratio >= cfg.VOL_MULT:
            quality += 1
            reasons.append(f"✅ Объём ×{vol_ratio:.1f}")

        if not is_counter:
            quality += 1
            reasons.append("✅ По локальному тренду")

        if (signal == "LONG" and htf_bull) or (signal == "SHORT" and htf_bear):
            quality += 1
            reasons.append("✅ HTF тренд подтверждает")

        if (signal == "LONG" and rsi_now < 50) or (signal == "SHORT" and rsi_now > 50):
            quality += 1
            reasons.append(f"✅ RSI {rsi_now:.1f}")

        # ── Бонусы за качество уровня ← НОВОЕ v4.2.1
        if zone_q > 0.6:
            quality += 1
            reasons.append(f"✅ Чёткий уровень ({zone_q:.0%})")

        if lvl_strength >= 3:
            quality += 1
            reasons.append(f"✅ Уровень тестировался {lvl_strength}× без пробоя")

        # ──────────────────────────────────────────
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
            quality           = min(quality, 5),
            reasons           = reasons,
            rsi               = rsi_now,
            volume_ratio      = vol_ratio,
            trend_local       = trend_local,
            trend_htf         = trend_htf,
            pattern           = final_pattern,
            breakout_type     = s_type,
            is_counter_trend  = is_counter,
            human_explanation = explanation,
        )
