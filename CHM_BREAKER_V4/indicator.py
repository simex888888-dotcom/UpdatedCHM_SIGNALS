"""
CHM BREAKER — Price Action + Key Levels (Professional Trading Protocol)

Классификация уровней: Абсолютный (1) / Сильный (2) / Рабочий (3)
Протокол: Карта уровней → Качество подхода → Реакция → Зоны → R:R ≥ 2:1
Финальный чеклист: 5 условий, все должны быть выполнены.
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
    level_class:      int   = 3   # 1=Абсолютный, 2=Сильный, 3=Рабочий
    test_count:       int   = 0   # Кол-во тестов уровня за последние 30 свечей


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
        tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    # ──────────────────────────────────────────────────────────────────
    # КЛАССИФИКАЦИЯ УРОВНЕЙ
    # ──────────────────────────────────────────────────────────────────

    def _is_psychological_level(self, price: float) -> bool:
        """Психологически круглый уровень ($100, $1000, $10000 и т.д.)"""
        magnitudes = [1, 5, 10, 25, 50, 100, 500, 1000, 5000, 10000, 50000]
        for mag in magnitudes:
            if mag > price * 10:
                break
            if abs(price % mag) / price < 0.003:
                return True
        return False

    def _classify_level(self, price: float, hits: int, age_bars: int,
                        is_psychological: bool = False) -> int:
        """
        Классификация по силе:
          1 = Абсолютный — психологический уровень ИЛИ 3+ касания за ≤30 баров
          2 = Сильный    — 2+ касания любого возраста
          3 = Рабочий    — одиночный пивот (обычно игнорируется)
        """
        if is_psychological:
            return 1
        if hits >= 3 and age_bars <= 30:
            return 1
        if hits >= 2:
            return 2
        return 3

    # ──────────────────────────────────────────────────────────────────
    # ЗОНЫ (кластеризация пивотов)
    # ──────────────────────────────────────────────────────────────────

    def _get_zones(self, df: pd.DataFrame, strength: int, atr_now: float):
        """Кластеризация пивотов в зоны с классификацией уровней."""
        highs = df["high"].values
        lows  = df["low"].values

        res_points: list[tuple[float, int]] = []
        sup_points: list[tuple[float, int]] = []

        for i in range(strength, len(df) - strength):
            if highs[i] == max(highs[i - strength: i + strength + 1]):
                res_points.append((highs[i], len(df) - 1 - i))
            if lows[i] == min(lows[i - strength: i + strength + 1]):
                sup_points.append((lows[i], len(df) - 1 - i))

        # ATR-буфер для кластеризации близких точек
        buffer = atr_now * self.cfg.ZONE_BUFFER

        def cluster(points: list[tuple[float, int]]) -> list[dict]:
            if not points:
                return []
            points.sort(key=lambda x: x[0])
            clusters: list[dict] = []
            group = [points[0]]
            for p in points[1:]:
                if p[0] - group[-1][0] <= buffer:
                    group.append(p)
                else:
                    clusters.append(_make_zone(group))
                    group = [p]
            clusters.append(_make_zone(group))
            # Фильтр: только 2+ касания (Класс 1 или 2)
            return [c for c in clusters if c["hits"] >= 2]

        def _make_zone(group: list[tuple[float, int]]) -> dict:
            avg_price = sum(x[0] for x in group) / len(group)
            min_age   = min(x[1] for x in group)
            hits      = len(group)
            is_psych  = self._is_psychological_level(avg_price)
            lvl_class = self._classify_level(avg_price, hits, min_age, is_psych)
            return {
                "price":           avg_price,
                "hits":            hits,
                "age_bars":        min_age,
                "class":           lvl_class,
                "is_psychological": is_psych,
            }

        return cluster(sup_points), cluster(res_points)

    # ──────────────────────────────────────────────────────────────────
    # ПАТТЕРНЫ СВЕЧЕЙ
    # ──────────────────────────────────────────────────────────────────

    def _detect_pattern(self, df: pd.DataFrame) -> tuple[str, str]:
        """Pin Bar, Engulfing — основные паттерны подтверждения."""
        c = df.iloc[-1]
        p = df.iloc[-2]
        body  = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total < 1e-10:
            return "", ""

        uw = c["high"] - max(c["close"], c["open"])
        lw = min(c["close"], c["open"]) - c["low"]

        bull, bear = "", ""
        if lw >= body * 1.5 and uw < body and c["close"] >= c["open"]:
            bull = "Пин-бар покупок"
        elif uw >= body * 1.5 and lw < body and c["close"] <= c["open"]:
            bear = "Пин-бар продаж"
        elif (c["close"] > c["open"] and p["close"] < p["open"]
              and c["open"] <= p["close"] and c["close"] > p["open"]):
            bull = "Бычье поглощение"
        elif (c["close"] < c["open"] and p["close"] > p["open"]
              and c["open"] >= p["close"] and c["close"] < p["open"]):
            bear = "Медвежье поглощение"

        return bull, bear

    # ──────────────────────────────────────────────────────────────────
    # ЭТАП 2 — КАЧЕСТВО ПОДХОДА К УРОВНЮ
    # ──────────────────────────────────────────────────────────────────

    def _assess_approach_quality(self, df: pd.DataFrame, level: float,
                                 zone_buf: float,
                                 vol_ma: pd.Series) -> tuple[bool, str]:
        """
        ✅ ХОРОШИЙ подход: медленно, объём снижается, свечи уменьшаются.
        🚫 ПЛОХОЙ подход: импульсная свеча с объёмом, 4+ попыток подряд.
        """
        if len(df) < 5:
            return True, "Недостаточно данных"

        last      = df.iloc[-1]
        avg_vol   = vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1.0
        last_vol  = df["volume"].iloc[-1]
        last_body = abs(last["close"] - last["open"])
        last_rng  = last["high"] - last["low"]

        # 🚫 Импульсная свеча с большим объёмом влетела в уровень
        if last_vol > avg_vol * 1.8 and last_rng > 0 and last_body / last_rng > 0.7:
            return False, "Импульсный подход с высоким объёмом"

        # 🚫 4+ попыток пробить уровень в последних 10 свечах подряд
        near_count = 0
        for i in range(max(0, len(df) - 10), len(df) - 1):
            bar = df.iloc[i]
            if bar["low"] <= level + zone_buf and bar["high"] >= level - zone_buf:
                near_count += 1
        if near_count >= 4:
            return False, f"Уровень тестировался {near_count}x подряд — ожидается пробой"

        # ✅ Объём на подходе снижался
        vols = [df["volume"].iloc[i] for i in range(-4, -1)]
        vol_decreasing = vols[-1] < vols[0] if vols[0] > 0 else True

        # ✅ Последние 3 свечи уменьшаются по размеру
        bodies = [abs(df["close"].iloc[i] - df["open"].iloc[i]) for i in range(-3, 0)]
        size_decreasing = bodies[-1] < bodies[0] if bodies[0] > 0 else True

        if vol_decreasing and size_decreasing:
            return True, "Мягкий подход — объём и размер свечей снижаются"
        if vol_decreasing:
            return True, "Объём на подходе снижается"
        if size_decreasing:
            return True, "Свечи на подходе уменьшаются"

        return True, "Нейтральный подход"

    # ──────────────────────────────────────────────────────────────────
    # ЛОЖНЫЙ ПРОБОЙ (самый сильный сигнал)
    # ──────────────────────────────────────────────────────────────────

    def _check_fakeout(self, df: pd.DataFrame, level: float,
                       direction: str, zone_buf: float) -> bool:
        """
        Свеча пробила уровень, но ЗАКРЫЛАСЬ обратно — Fakeout.
        LONG: low < level, close > level
        SHORT: high > level, close < level
        """
        last = df.iloc[-1]
        if direction == "LONG":
            return last["low"] < level - zone_buf * 0.5 and last["close"] > level
        else:
            return last["high"] > level + zone_buf * 0.5 and last["close"] < level

    # ──────────────────────────────────────────────────────────────────
    # ПОДСЧЁТ ТЕСТОВ УРОВНЯ
    # ──────────────────────────────────────────────────────────────────

    def _count_recent_tests(self, df: pd.DataFrame, level: float,
                            zone_pct: float, lookback: int = 30) -> int:
        """Считает кол-во отдельных касаний зоны уровня за последние N свечей."""
        zone_range = level * zone_pct / 100
        count = 0
        prev_in_zone = False
        start = max(0, len(df) - lookback)
        for i in range(start, len(df)):
            bar = df.iloc[i]
            in_zone = (bar["low"] <= level + zone_range
                       and bar["high"] >= level - zone_range)
            if in_zone and not prev_in_zone:
                count += 1
            prev_in_zone = in_zone
        return count

    # ──────────────────────────────────────────────────────────────────
    # TP1 — БЛИЖАЙШИЙ ПРОТИВОПОЛОЖНЫЙ УРОВЕНЬ
    # ──────────────────────────────────────────────────────────────────

    def _find_tp1_level(self, direction: str, entry: float,
                        sup_zones: list, res_zones: list) -> Optional[float]:
        """TP1 = ближайший уровень на противоположной стороне от входа."""
        if direction == "LONG":
            candidates = [z["price"] for z in res_zones if z["price"] > entry * 1.002]
            return min(candidates) if candidates else None
        else:
            candidates = [z["price"] for z in sup_zones if z["price"] < entry * 0.998]
            return max(candidates) if candidates else None

    # ──────────────────────────────────────────────────────────────────
    # ГЛАВНЫЙ МЕТОД АНАЛИЗА
    # ──────────────────────────────────────────────────────────────────

    def analyze(self, symbol: str, df: pd.DataFrame,
                df_htf=None) -> Optional[SignalResult]:
        cfg = self.cfg
        if df is None or len(df) < max(cfg.EMA_SLOW, 100):
            return None
        bar_idx = len(df) - 1
        if bar_idx - self._last_signal.get(symbol, -999) < cfg.COOLDOWN_BARS:
            return None

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

        bull_local = c_now > ema50.iloc[-1] > ema200.iloc[-1]
        bear_local = c_now < ema50.iloc[-1] < ema200.iloc[-1]
        trend_local = ("📈 Бычий" if bull_local
                       else ("📉 Медвежий" if bear_local else "↔️ Боковик"))

        sup_zones, res_zones = self._get_zones(df, cfg.PIVOT_STRENGTH, atr_now)
        if not sup_zones and not res_zones:
            return None

        bull_pat, bear_pat = self._detect_pattern(df)

        # ══════════════════════════════════════════════════════════════
        # ЭТАП 1 — КАРТА УРОВНЕЙ: проверяем дистанцию до ближайшего
        # Если цена >1.5% от ближайшего уровня — сигнал не даём
        # ══════════════════════════════════════════════════════════════
        all_levels = [(z["price"], z) for z in sup_zones + res_zones]
        if not all_levels:
            return None

        nearest_price, _ = min(all_levels, key=lambda x: abs(x[0] - c_now))
        dist_pct = abs(c_now - nearest_price) / c_now * 100
        if dist_pct > 1.5:
            return None

        # Ширина зоны: ±0.7% от уровня (зона, не линия)
        ZONE_PCT = 0.7

        # ══════════════════════════════════════════════════════════════
        # ПОИСК СИГНАЛА — ЛОНГ
        # ══════════════════════════════════════════════════════════════
        signal, s_level, s_type, explanation = None, None, "", ""
        is_counter = False
        s_hits, s_class = 0, 3

        for sup in reversed(sup_zones):
            lvl      = sup["price"]
            hits     = sup["hits"]
            lvl_class = sup["class"]
            zone_buf = lvl * ZONE_PCT / 100

            if abs(c_now - lvl) > zone_buf * 3:
                continue

            # Ложный пробой (ПРИОРИТЕТ №1)
            if self._check_fakeout(df, lvl, "LONG", zone_buf):
                signal, s_level = "LONG", lvl
                s_type = "Ложный пробой (Fakeout)"
                explanation = (
                    f"Цена пробила поддержку, собрала стопы, но закрылась выше уровня. "
                    f"Класс уровня: {lvl_class}, касаний: {hits}. Самый сильный сигнал."
                )
                is_counter = bear_local; s_hits = hits; s_class = lvl_class
                break

            # Отскок с паттерном свечи
            if abs(c_now - lvl) < zone_buf * 2 and bull_pat:
                signal, s_level = "LONG", lvl
                s_type = "Отскок от поддержки"
                explanation = (
                    f"Цена у зоны поддержки класса {lvl_class} (касаний: {hits}). "
                    f"Подтверждение: {bull_pat}."
                )
                is_counter = bear_local; s_hits = hits; s_class = lvl_class
                break

            # SFP — пробой вниз с возвратом на объёме
            if (df["low"].iloc[-1] < lvl - zone_buf
                    and c_now > lvl and vol_ratio > 1.2):
                signal, s_level = "LONG", lvl
                s_type = "SFP (Захват ликвидности)"
                explanation = (
                    f"Поддержка класса {lvl_class} (касаний: {hits}). "
                    f"Прокол вниз — собраны стопы, возврат на объёме x{vol_ratio:.1f}."
                )
                is_counter = bear_local; s_hits = hits; s_class = lvl_class
                break

        # Ретест пробитого сопротивления → теперь поддержка
        if not signal:
            for res in reversed(res_zones):
                lvl       = res["price"]
                hits      = res["hits"]
                lvl_class = res["class"]
                zone_buf  = lvl * ZONE_PCT / 100

                if abs(c_now - lvl) > zone_buf * 3:
                    continue

                recent_closes = df["close"].iloc[-6:-1]
                if ((recent_closes > lvl).any()
                        and abs(df["low"].iloc[-1] - lvl) < zone_buf
                        and bull_pat):
                    signal, s_level = "LONG", lvl
                    s_type = "Ретест пробитого уровня"
                    explanation = (
                        f"Пробитое сопротивление класса {lvl_class} стало поддержкой. "
                        f"Мягкий возврат с подтверждением: {bull_pat}."
                    )
                    is_counter = bear_local; s_hits = hits; s_class = lvl_class
                    break

                # Честный пробой вверх
                if (df["close"].iloc[-2] < lvl
                        and c_now > lvl + zone_buf and vol_ratio > 1.5):
                    signal, s_level = "LONG", lvl
                    s_type = "Пробой уровня"
                    explanation = (
                        f"Пробой сопротивления класса {lvl_class} (касаний: {hits}). "
                        f"Свеча закрылась выше зоны. Объём x{vol_ratio:.1f}."
                    )
                    is_counter = bear_local; s_hits = hits; s_class = lvl_class
                    break

        # ══════════════════════════════════════════════════════════════
        # ПОИСК СИГНАЛА — ШОРТ
        # ══════════════════════════════════════════════════════════════
        if not signal:
            for res in reversed(res_zones):
                lvl       = res["price"]
                hits      = res["hits"]
                lvl_class = res["class"]
                zone_buf  = lvl * ZONE_PCT / 100

                if abs(c_now - lvl) > zone_buf * 3:
                    continue

                if self._check_fakeout(df, lvl, "SHORT", zone_buf):
                    signal, s_level = "SHORT", lvl
                    s_type = "Ложный пробой (Fakeout)"
                    explanation = (
                        f"Цена пробила сопротивление, собрала ликвидность, "
                        f"но закрылась ниже. Класс уровня: {lvl_class}, касаний: {hits}."
                    )
                    is_counter = bull_local; s_hits = hits; s_class = lvl_class
                    break

                if abs(c_now - lvl) < zone_buf * 2 and bear_pat:
                    signal, s_level = "SHORT", lvl
                    s_type = "Отскок от сопротивления"
                    explanation = (
                        f"Сопротивление класса {lvl_class} (касаний: {hits}). "
                        f"Подтверждение: {bear_pat}."
                    )
                    is_counter = bull_local; s_hits = hits; s_class = lvl_class
                    break

                if (df["high"].iloc[-1] > lvl + zone_buf
                        and c_now < lvl and vol_ratio > 1.2):
                    signal, s_level = "SHORT", lvl
                    s_type = "SFP (Ложный пробой вверх)"
                    explanation = (
                        f"Сопротивление класса {lvl_class} (касаний: {hits}). "
                        f"Прокол вверх — собрана ликвидность, возврат под уровень. "
                        f"Объём x{vol_ratio:.1f}."
                    )
                    is_counter = bull_local; s_hits = hits; s_class = lvl_class
                    break

        if not signal:
            for sup in reversed(sup_zones):
                lvl       = sup["price"]
                hits      = sup["hits"]
                lvl_class = sup["class"]
                zone_buf  = lvl * ZONE_PCT / 100

                if abs(c_now - lvl) > zone_buf * 3:
                    continue

                recent_closes = df["close"].iloc[-6:-1]
                if ((recent_closes < lvl).any()
                        and abs(df["high"].iloc[-1] - lvl) < zone_buf
                        and bear_pat):
                    signal, s_level = "SHORT", lvl
                    s_type = "Ретест пробитой поддержки"
                    explanation = (
                        f"Пробитая поддержка класса {lvl_class} стала сопротивлением. "
                        f"Откат снизу с подтверждением: {bear_pat}."
                    )
                    is_counter = bull_local; s_hits = hits; s_class = lvl_class
                    break

                if (df["close"].iloc[-2] > lvl
                        and c_now < lvl - zone_buf and vol_ratio > 1.5):
                    signal, s_level = "SHORT", lvl
                    s_type = "Пробой поддержки"
                    explanation = (
                        f"Пробой поддержки класса {lvl_class} вниз. "
                        f"Нет возврата. Объём x{vol_ratio:.1f}."
                    )
                    is_counter = bull_local; s_hits = hits; s_class = lvl_class
                    break

        if not signal:
            return None

        # Актуальный zone_buf для найденного уровня
        zone_buf = s_level * ZONE_PCT / 100

        # ══════════════════════════════════════════════════════════════
        # ЭТАП 2 — КАЧЕСТВО ПОДХОДА (применяем после нахождения уровня)
        # ══════════════════════════════════════════════════════════════
        approach_ok, approach_reason = self._assess_approach_quality(
            df, s_level, zone_buf, vol_ma
        )
        if not approach_ok:
            log.debug(f"{symbol}: Плохой подход — {approach_reason}")
            return None

        # ══════════════════════════════════════════════════════════════
        # ПОДСЧЁТ ТЕСТОВ УРОВНЯ
        # 🚫 4+ тестов без отката >2% — ждём пробоя, не отскока
        # ══════════════════════════════════════════════════════════════
        test_count = self._count_recent_tests(df, s_level, ZONE_PCT, lookback=30)
        if test_count >= 4:
            log.debug(
                f"{symbol}: Уровень {s_level:.4f} тестировался {test_count}x — "
                f"пропуск (ожидается пробой)"
            )
            return None

        # ══════════════════════════════════════════════════════════════
        # ФИЛЬТРЫ (RSI)
        # ══════════════════════════════════════════════════════════════
        if cfg.USE_RSI_FILTER:
            if signal == "LONG" and rsi_now > cfg.RSI_OB:
                return None
            if signal == "SHORT" and rsi_now < cfg.RSI_OS:
                return None

        # ══════════════════════════════════════════════════════════════
        # ЭТАП 4 — СТОП ЗА ЗОНУ + 0.5%
        # LONG:  стоп ниже нижней границы зоны - 0.5%
        # SHORT: стоп выше верхней границы зоны + 0.5%
        # ══════════════════════════════════════════════════════════════
        entry = c_now
        if signal == "LONG":
            zone_bottom = s_level - zone_buf
            sl = zone_bottom * (1.0 - 0.005)
            sl = min(sl, entry * (1.0 - cfg.MAX_RISK_PCT / 100))
        else:
            zone_top = s_level + zone_buf
            sl = zone_top * (1.0 + 0.005)
            sl = max(sl, entry * (1.0 + cfg.MAX_RISK_PCT / 100))

        risk = abs(entry - sl)
        if risk <= 0:
            return None

        # ══════════════════════════════════════════════════════════════
        # ЭТАП 5 — ЦЕЛИ: TP1 = ближайший противоположный уровень
        # Минимальный R:R = 2:1 (без исключений)
        # ══════════════════════════════════════════════════════════════
        tp1_from_level = self._find_tp1_level(signal, entry, sup_zones, res_zones)
        if tp1_from_level is not None:
            tp1 = tp1_from_level
        else:
            # Нет противоположного уровня — ставим 2R минимум
            tp1 = (entry + risk * 2.0 if signal == "LONG"
                   else entry - risk * 2.0)

        tp2 = (entry + risk * cfg.TP2_RR if signal == "LONG"
               else entry - risk * cfg.TP2_RR)
        tp3 = (entry + risk * cfg.TP3_RR if signal == "LONG"
               else entry - risk * cfg.TP3_RR)

        # Проверка R:R ≥ 2:1
        rr_actual = ((tp1 - entry) / risk if signal == "LONG"
                     else (entry - tp1) / risk)
        if rr_actual < 2.0:
            log.debug(f"{symbol}: R:R = {rr_actual:.2f} < 2.0 — сигнал пропущен")
            return None

        risk_pct = abs((sl - entry) / entry * 100)

        # ══════════════════════════════════════════════════════════════
        # КАЧЕСТВО СИГНАЛА (звёзды 1–5)
        # ══════════════════════════════════════════════════════════════
        quality = 2
        class_names = {1: "Абсолютный", 2: "Сильный", 3: "Рабочий"}
        reasons = [f"✅ {s_type}"]

        # Класс уровня
        reasons.append(f"✅ Уровень класса {s_class} ({class_names.get(s_class, '')})")
        if s_class == 1:
            quality += 1

        # Тест уровня
        if test_count <= 2:
            quality += 1
            reasons.append(f"✅ Тест #{test_count} уровня (оптимально)")
        else:
            reasons.append(f"⚠️ Тест #{test_count} — с осторожностью")

        # Объём
        if vol_ratio >= cfg.VOL_MULT:
            quality += 1
            reasons.append(f"✅ Объём x{vol_ratio:.1f}")

        # По тренду
        if not is_counter:
            quality += 1
            reasons.append("✅ По локальному тренду")

        # Паттерн свечи
        active_pat = bull_pat if signal == "LONG" else bear_pat
        if active_pat:
            quality += 1
            reasons.append(f"✅ Паттерн: {active_pat}")

        # Ложный пробой — высший приоритет
        if "Fakeout" in s_type or "SFP" in s_type:
            quality += 1
            reasons.append("✅ Ложный пробой / захват ликвидности")

        # RSI выходит из экстремальной зоны
        if (signal == "LONG" and 30 < rsi_now < 45):
            reasons.append("✅ RSI выходит из перепроданности")
        elif (signal == "SHORT" and 55 < rsi_now < 70):
            reasons.append("✅ RSI выходит из перекупленности")

        # HTF подтверждение
        if (cfg.USE_HTF_FILTER and df_htf is not None
                and len(df_htf) >= cfg.HTF_EMA_PERIOD):
            htf_ema_val = self._ema(df_htf["close"], cfg.HTF_EMA_PERIOD).iloc[-1]
            htf_price   = df_htf["close"].iloc[-1]
            if ((signal == "LONG" and htf_price > htf_ema_val)
                    or (signal == "SHORT" and htf_price < htf_ema_val)):
                quality += 1
                reasons.append("✅ HTF тренд подтверждает")

        # Снижаем качество при 3-м тесте
        if test_count == 3:
            quality = max(1, quality - 1)
            reasons.append("⚠️ Третий тест — уровень слабеет")

        # ══════════════════════════════════════════════════════════════
        # ФИНАЛЬНЫЙ ЧЕКЛИСТ — все 5 условий должны быть выполнены
        # ══════════════════════════════════════════════════════════════
        # 1. Уровень класса 1 или 2 (или класса 3 с 3+ подтвердителями)?
        checklist_1 = s_class <= 2 or (s_class == 3 and quality >= 4)

        # 2. Подход к уровню был мягким?
        checklist_2 = approach_ok

        # 3. Есть подтверждённая реакция (паттерн / fakeout / объём)?
        has_pattern = bool(bull_pat if signal == "LONG" else bear_pat)
        is_fakeout  = "Fakeout" in s_type or "SFP" in s_type
        checklist_3 = has_pattern or is_fakeout or vol_ratio > 1.5

        # 4. R:R ≥ 2:1?
        checklist_4 = rr_actual >= 2.0

        # 5. Первый или второй тест уровня?
        checklist_5 = test_count <= 2

        if not all([checklist_1, checklist_2, checklist_3, checklist_4, checklist_5]):
            log.debug(
                f"{symbol}: Чеклист НЕ пройден — "
                f"[Уровень:{checklist_1} Подход:{checklist_2} "
                f"Реакция:{checklist_3} R:R:{checklist_4} Тест:{checklist_5}]"
            )
            return None

        self._last_signal[symbol] = bar_idx

        return SignalResult(
            symbol=symbol, direction=signal, entry=entry, sl=sl,
            tp1=tp1, tp2=tp2, tp3=tp3, risk_pct=risk_pct,
            quality=min(quality, 5), reasons=reasons,
            rsi=rsi_now, volume_ratio=vol_ratio,
            trend_local=trend_local, breakout_type=s_type,
            is_counter_trend=is_counter, human_explanation=explanation,
            level_class=s_class, test_count=test_count,
        )
