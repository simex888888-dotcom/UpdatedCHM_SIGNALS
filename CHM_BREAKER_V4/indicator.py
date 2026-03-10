"""
CHM BREAKER — Institutional-Grade Price Action Engine v2.0
==========================================================
Философия: не угадывать движение, а находить точки где рынок ОБЯЗАН реагировать.
Протокол: Карта уровней → Качество подхода → Паттерн → R:R → Финальный чеклист.

Архитектура уровней:
  Слой 1 — Фракталы (pivot highs/lows)
  Слой 2 — KDE кластеры (gaussian density)
  Слой 3 — Volume Profile (HVN/LVN)

Классификация: Абсолютный (1) / Сильный (2) / Рабочий (3)
Quality: 0–10 (фильтр по cfg.MIN_QUALITY)
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from config import Config

log = logging.getLogger("CHM.Indicator")

# Попытка импорта scipy для KDE — без него слой 2 молча пропускается
try:
    from scipy.stats import gaussian_kde
    from scipy.signal import argrelextrema as _arex
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False
    log.debug("scipy не установлен — KDE-слой уровней отключён")


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASS РЕЗУЛЬТАТА СИГНАЛА
# ══════════════════════════════════════════════════════════════════════════════

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
    level_class:       int   = 3    # 1=Абсолютный, 2=Сильный, 3=Рабочий
    test_count:        int   = 0    # Кол-во тестов уровня за последние 30 свечей
    rr_score:          float = 0.0  # Взвешенный R:R score
    corr_label:        str   = ""   # Корреляционная метка с BTC/ETH
    session:           str   = ""   # Торговая сессия по UTC
    btc_corr:          float = 0.0  # Корреляция с BTC (30 свечей)
    eth_corr:          float = 0.0  # Корреляция с ETH (30 свечей)


# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ КЛАСС
# ══════════════════════════════════════════════════════════════════════════════

class CHMIndicator:

    def __init__(self, config: Config):
        self.cfg = config
        self._last_signal: dict[str, int] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _ema(s: pd.Series, n: int) -> pd.Series:
        return s.ewm(span=n, adjust=False).mean()

    @staticmethod
    def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
        d  = s.diff()
        g  = d.clip(lower=0).ewm(span=n, adjust=False).mean()
        ls = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
        rs = g / ls.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
        h, l, pc = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.ewm(span=n, adjust=False).mean()

    # ─────────────────────────────────────────────────────────────────────────
    # ПСИХОЛОГИЧЕСКИЕ УРОВНИ
    # ─────────────────────────────────────────────────────────────────────────

    def _is_psychological_level(self, price: float) -> bool:
        """Круглые уровни: $1, $5, $10, $25, $50, $100, $500, $1000, $5000, $10000."""
        magnitudes = [1, 5, 10, 25, 50, 100, 500, 1_000, 5_000, 10_000, 50_000]
        for mag in magnitudes:
            if mag > price * 10:
                break
            if price > 0 and abs(price % mag) / price < 0.003:
                return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # КЛАССИФИКАЦИЯ УРОВНЯ
    # ─────────────────────────────────────────────────────────────────────────

    def _classify_level(self, price: float, hits: int, age_bars: int,
                        is_psychological: bool = False,
                        confirmed_layers: int = 1) -> int:
        """
        Правила (в порядке приоритета):
          1. Психологический → class=1 всегда
          2. 3+ касания за ≤30 баров → class=1
          3. Подтверждён 3 слоями → class=1
          4. 2+ касания, age ≤100 → class=2
          5. 2+ касания, age >100, НЕ psychological → class=3 (decay)
          6. 1 касание → class=3
        """
        if is_psychological:
            return 1
        if hits >= 3 and age_bars <= 30:
            return 1
        if confirmed_layers >= 3:
            return 1
        if hits >= 2 and age_bars <= 100:
            return 2
        if hits >= 2:
            return 3  # устаревший уровень
        return 3

    # ─────────────────────────────────────────────────────────────────────────
    # СЛОЙ 2 — KDE КЛАСТЕРЫ (gaussian density)
    # ─────────────────────────────────────────────────────────────────────────

    def _kde_levels(self, pivot_prices: list[float],
                    price_range: tuple[float, float],
                    n_points: int = 500) -> list[float]:
        """Возвращает цены пиков плотности KDE. Молча возвращает [] если scipy нет."""
        if not _SCIPY_OK or len(pivot_prices) < 5:
            return []
        try:
            arr = np.array(pivot_prices, dtype=float)
            kde = gaussian_kde(arr, bw_method="silverman")
            xs  = np.linspace(price_range[0], price_range[1], n_points)
            ys  = kde(xs)
            # Пики плотности — "народные уровни"
            peaks = _arex(ys.reshape(-1, 1), np.greater, order=10)[0]
            return [float(xs[i]) for i in peaks]
        except Exception as e:
            log.debug(f"KDE ошибка: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # СЛОЙ 3 — VOLUME PROFILE (HVN/LVN)
    # ─────────────────────────────────────────────────────────────────────────

    def _volume_profile(self, df: pd.DataFrame,
                        n_bins: int = 100) -> dict:
        """
        Возвращает {
          "hvn": [price, ...],   -- High Volume Nodes (>1.5× avg)
          "lvn": [price, ...],   -- Low Volume Nodes  (<0.5× avg)
          "bin_edges": np.array,
          "volumes":   np.array,
        }
        """
        result: dict = {"hvn": [], "lvn": [], "bin_edges": np.array([]), "volumes": np.array([])}
        if len(df) < 10:
            return result
        try:
            lo, hi = df["low"].min(), df["high"].max()
            if hi <= lo:
                return result
            bins = np.linspace(lo, hi, n_bins + 1)
            mid  = (bins[:-1] + bins[1:]) / 2
            vols = np.zeros(n_bins)
            for _, row in df.iterrows():
                # Распределяем объём по бинам, которые перекрывает свеча
                mask = (mid >= row["low"]) & (mid <= row["high"])
                span = max(mask.sum(), 1)
                vols[mask] += row["volume"] / span
            avg  = vols.mean()
            hvn  = [float(mid[i]) for i in range(n_bins) if avg > 0 and vols[i] > avg * 1.5]
            lvn  = [float(mid[i]) for i in range(n_bins) if avg > 0 and vols[i] < avg * 0.5]
            result["hvn"]       = hvn
            result["lvn"]       = lvn
            result["bin_edges"] = bins
            result["volumes"]   = vols
        except Exception as e:
            log.debug(f"VolumeProfile ошибка: {e}")
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # СЛОЙ 1+2+3 — МНОГОСЛОЙНАЯ КЛАСТЕРИЗАЦИЯ ЗОН
    # ─────────────────────────────────────────────────────────────────────────

    def _get_zones(self, df: pd.DataFrame, strength: int,
                   atr_now: float) -> tuple[list[dict], list[dict]]:
        """
        Три независимых слоя детекции уровней → объединение → классификация.
        Слой 1: фракталы (pivot highs/lows)
        Слой 2: KDE пики плотности
        Слой 3: HVN из Volume Profile
        """
        highs = df["high"].values
        lows  = df["low"].values
        n     = len(df)

        # ── Слой 1: Фракталы ──────────────────────────────────────────────
        res_pts: list[tuple[float, int]] = []  # (price, age_in_bars)
        sup_pts: list[tuple[float, int]] = []

        for i in range(strength, n - strength):
            if highs[i] == max(highs[i - strength: i + strength + 1]):
                res_pts.append((float(highs[i]), n - 1 - i))
            if lows[i] == min(lows[i - strength: i + strength + 1]):
                sup_pts.append((float(lows[i]), n - 1 - i))

        all_pivot_prices = [p for p, _ in res_pts + sup_pts]

        # ── Слой 2: KDE ───────────────────────────────────────────────────
        price_range = (df["low"].min(), df["high"].max())
        kde_prices  = self._kde_levels(all_pivot_prices, price_range)

        # ── Слой 3: Volume Profile ─────────────────────────────────────────
        vp = self._volume_profile(df)

        # ── ATR-буфер для кластеризации ───────────────────────────────────
        buffer = atr_now * self.cfg.ZONE_BUFFER

        def _find_layer_hits(price: float) -> int:
            """Сколько слоёв (KDE, HVN) подтверждают цену (±buffer*2)."""
            hits = 0
            tol  = buffer * 2
            if any(abs(price - k) <= tol for k in kde_prices):
                hits += 1
            if any(abs(price - h) <= tol for h in vp["hvn"]):
                hits += 1
            return hits

        def _check_lvn_nearby(price_a: float, price_b: float) -> bool:
            """Есть ли LVN между двумя ценами (чистый путь к цели)."""
            lo_p, hi_p = min(price_a, price_b), max(price_a, price_b)
            return any(lo_p < lv < hi_p for lv in vp["lvn"])

        def _make_zone(group: list[tuple[float, int]]) -> dict:
            avg_price = sum(x[0] for x in group) / len(group)
            min_age   = min(x[1] for x in group)
            hits      = len(group)
            is_psych  = self._is_psychological_level(avg_price)
            layers    = _find_layer_hits(avg_price)
            # Каждый дополнительный слой даёт +2 к hits для классификации
            eff_hits  = hits + layers * 2
            lvl_class = self._classify_level(
                avg_price, eff_hits, min_age, is_psych, layers + 1
            )
            return {
                "price":            avg_price,
                "hits":             hits,
                "eff_hits":         eff_hits,
                "age_bars":         min_age,
                "class":            lvl_class,
                "is_psychological": is_psych,
                "layers":           layers + 1,  # фракталы всегда 1
                "has_hvn":          any(abs(avg_price - h) <= buffer * 2 for h in vp["hvn"]),
                "has_lvn_to_tp":    False,        # проставляется позже в analyze()
                "lvn_checker":      _check_lvn_nearby,
            }

        def _cluster(points: list[tuple[float, int]]) -> list[dict]:
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
            # Минимум 2 касания (класс 1 может быть и с 1 касанием если психологический)
            return [c for c in clusters if c["hits"] >= 2 or c["is_psychological"]]

        sup_zones = _cluster(sup_pts)
        res_zones = _cluster(res_pts)

        log.debug(
            f"Zones: {len(sup_zones)} sup / {len(res_zones)} res | "
            f"KDE={len(kde_prices)} HVN={len(vp['hvn'])} LVN={len(vp['lvn'])}"
        )
        return sup_zones, res_zones

    # ─────────────────────────────────────────────────────────────────────────
    # МУЛЬТИ-ТАЙМФРЕЙМОВЫЕ УРОВНИ
    # ─────────────────────────────────────────────────────────────────────────

    def _multi_timeframe_levels(self, df_ltf: pd.DataFrame,
                                df_mtf: Optional[pd.DataFrame],
                                df_htf: Optional[pd.DataFrame],
                                atr_now: float) -> list[dict]:
        """
        Запускает _get_zones() на каждом ТФ.
        Совпадение на 2 ТФ → "MTF уровень" (class≤2).
        Совпадение на 3 ТФ → "Институциональный уровень" (class=1).
        Возвращает объединённый список с полем "timeframes".
        """
        tol = atr_now * 0.5
        tf_data = [("ltf", df_ltf)]
        if df_mtf is not None and len(df_mtf) > 20:
            tf_data.append(("mtf", df_mtf))
        if df_htf is not None and len(df_htf) > 20:
            tf_data.append(("htf", df_htf))

        all_zones_by_tf: list[tuple[str, list[dict]]] = []
        for tf_name, df_tf in tf_data:
            try:
                sup, res = self._get_zones(df_tf, self.cfg.PIVOT_STRENGTH, atr_now)
                for z in sup + res:
                    z["tf"] = tf_name
                all_zones_by_tf.append((tf_name, sup + res))
            except Exception as e:
                log.debug(f"MTF _get_zones ошибка на {tf_name}: {e}")

        if not all_zones_by_tf:
            return []

        # Собираем все зоны в плоский список
        all_zones: list[dict] = []
        for _, zones in all_zones_by_tf:
            all_zones.extend(zones)

        # Ищем совпадения между ТФ
        merged: list[dict] = []
        used = set()
        for i, zone in enumerate(all_zones):
            if i in used:
                continue
            tfs = [zone.get("tf", "ltf")]
            for j, other in enumerate(all_zones):
                if j <= i or j in used:
                    continue
                if abs(zone["price"] - other["price"]) <= tol:
                    tfs.append(other.get("tf", "ltf"))
                    used.add(j)
            used.add(i)
            zone = dict(zone)
            zone["timeframes"] = list(set(tfs))
            tf_count = len(zone["timeframes"])
            if tf_count >= 3:
                zone["class"] = 1
                zone["mtf_label"] = "Институциональный"
            elif tf_count == 2:
                zone["class"] = min(zone["class"], 2)
                zone["mtf_label"] = "MTF"
            else:
                zone["mtf_label"] = ""
            merged.append(zone)

        return merged

    # ─────────────────────────────────────────────────────────────────────────
    # РАСШИРЕННЫЕ ПАТТЕРНЫ СВЕЧЕЙ
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_pattern(self, df: pd.DataFrame) -> tuple[str, str]:
        """
        Паттерны: PinBar, Engulfing, Doji, Hammer, Morning/Evening Star,
                  Inside Bar, Liquidity Sweep, Institutional OB.
        Возвращает (bull_pattern, bear_pattern).
        """
        if len(df) < 3:
            return "", ""

        c  = df.iloc[-1]   # текущая свеча
        p  = df.iloc[-2]   # предыдущая
        pp = df.iloc[-3]   # позапрошлая

        body_c  = abs(c["close"] - c["open"])
        total_c = c["high"] - c["low"]
        body_p  = abs(p["close"] - p["open"])
        total_p = p["high"] - p["low"] if (p["high"] - p["low"]) > 1e-10 else 1e-10

        if total_c < 1e-10:
            return "", ""

        uw_c = c["high"] - max(c["close"], c["open"])  # upper wick
        lw_c = min(c["close"], c["open"]) - c["low"]   # lower wick

        bull, bear = "", ""

        # ── Бычий Пин-бар ──────────────────────────────────────────────────
        if (lw_c >= body_c * 1.5 and uw_c < body_c * 0.5
                and c["close"] >= c["open"]):
            bull = "Пин-бар покупок"

        # ── Медвежий Пин-бар ───────────────────────────────────────────────
        elif (uw_c >= body_c * 1.5 and lw_c < body_c * 0.5
              and c["close"] <= c["open"]):
            bear = "Пин-бар продаж"

        # ── Бычье поглощение ───────────────────────────────────────────────
        elif (c["close"] > c["open"] and p["close"] < p["open"]
              and c["open"] <= p["close"] and c["close"] > p["open"]):
            bull = "Бычье поглощение"

        # ── Медвежье поглощение ────────────────────────────────────────────
        elif (c["close"] < c["open"] and p["close"] > p["open"]
              and c["open"] >= p["close"] and c["close"] < p["open"]):
            bear = "Медвежье поглощение"

        # ── Doji у уровня ──────────────────────────────────────────────────
        elif body_c / total_c < 0.10:
            # Doji сам по себе нейтрален — дальше уточняем по позиции хвостов
            if lw_c > uw_c * 2:
                bull = "Бычий Doji (Dragonfly)"
            elif uw_c > lw_c * 2:
                bear = "Медвежий Doji (Gravestone)"
            # else: нейтральный Doji — пропускаем

        # ── Hammer / Inverted Hammer ───────────────────────────────────────
        elif (lw_c >= total_c * 0.6 and body_c <= total_c * 0.3):
            bull = "Молот (Hammer)"
        elif (uw_c >= total_c * 0.6 and body_c <= total_c * 0.3):
            bear = "Перевёрнутый молот"

        # ── Inside Bar (сжатие перед движением) ───────────────────────────
        elif (c["high"] <= p["high"] and c["low"] >= p["low"]):
            # Нейтральный паттерн, добавляем как слабый
            bull = "Inside Bar (сжатие)"
            bear = "Inside Bar (сжатие)"

        # ── Morning Star (3-свечной бычий разворот) ────────────────────────
        if not bull and not bear:
            if (pp["close"] < pp["open"]                   # медвежья
                    and body_p / total_p < 0.3             # маленькое тело
                    and c["close"] > c["open"]             # бычья
                    and c["close"] > (pp["open"] + pp["close"]) / 2):
                bull = "Утренняя звезда"

            # ── Evening Star ───────────────────────────────────────────────
            elif (pp["close"] > pp["open"]
                  and body_p / total_p < 0.3
                  and c["close"] < c["open"]
                  and c["close"] < (pp["open"] + pp["close"]) / 2):
                bear = "Вечерняя звезда"

        return bull, bear

    # ─────────────────────────────────────────────────────────────────────────
    # ПАТТЕРНЫ ИНСТИТУЦИОНАЛЬНОГО УРОВНЯ
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_institutional_pattern(self, df: pd.DataFrame,
                                      level: float, direction: str,
                                      vol_ratio: float,
                                      zone_buf: float) -> tuple[str, int]:
        """
        Возвращает (pattern_name, quality_bonus).
        Иерархия A > B > C > D.
        """
        if len(df) < 5:
            return "", 0

        c = df.iloc[-1]
        p = df.iloc[-2]

        body_c  = abs(c["close"] - c["open"])
        total_c = max(c["high"] - c["low"], 1e-10)
        body_p  = abs(p["close"] - p["open"])
        total_p = max(p["high"] - p["low"], 1e-10)

        # ── Уровень A ──────────────────────────────────────────────────────

        # LIQUIDITY_SWEEP: пробил уровень, закрылся обратно + объём >2×
        if direction == "LONG":
            ls_cond = (c["low"] < level - zone_buf * 0.5
                       and c["close"] > level
                       and vol_ratio > 2.0)
        else:
            ls_cond = (c["high"] > level + zone_buf * 0.5
                       and c["close"] < level
                       and vol_ratio > 2.0)
        if ls_cond:
            return "LIQUIDITY_SWEEP", 3

        # INSTITUTIONAL_ORDERBLOCK: последняя бычья/медвежья свеча перед
        # сильным импульсом (тело >70%, объём >2×)
        if direction == "LONG":
            ob_cond = (p["close"] < p["open"]      # медвежья перед импульсом
                       and body_p / total_p > 0.70
                       and vol_ratio > 2.0
                       and c["close"] > c["open"])
        else:
            ob_cond = (p["close"] > p["open"]
                       and body_p / total_p > 0.70
                       and vol_ratio > 2.0
                       and c["close"] < c["open"])
        if ob_cond:
            return "INSTITUTIONAL_ORDERBLOCK", 3

        # ── Уровень B ──────────────────────────────────────────────────────

        # FAKEOUT_PINBAR: ложный пробой + пин-бар на одной свече
        uw_c = c["high"] - max(c["close"], c["open"])
        lw_c = min(c["close"], c["open"]) - c["low"]
        body_ratio = body_c / total_c if total_c > 0 else 0
        if direction == "LONG":
            fp_cond = (c["low"] < level - zone_buf * 0.3
                       and c["close"] > level
                       and lw_c >= body_c * 1.5
                       and uw_c < body_c)
        else:
            fp_cond = (c["high"] > level + zone_buf * 0.3
                       and c["close"] < level
                       and uw_c >= body_c * 1.5
                       and lw_c < body_c)
        if fp_cond:
            return "FAKEOUT_PINBAR", 2

        # ENGULFING_AT_LEVEL: поглощение прямо у уровня (class 1/2)
        if direction == "LONG":
            eg_cond = (c["close"] > c["open"]
                       and p["close"] < p["open"]
                       and c["open"] <= p["close"]
                       and c["close"] > p["open"]
                       and abs(c["close"] - level) < zone_buf * 2)
        else:
            eg_cond = (c["close"] < c["open"]
                       and p["close"] > p["open"]
                       and c["open"] >= p["close"]
                       and c["close"] < p["open"]
                       and abs(c["close"] - level) < zone_buf * 2)
        if eg_cond:
            return "ENGULFING_AT_LEVEL", 2

        # ── Уровень C ──────────────────────────────────────────────────────

        # PINBAR_AT_LEVEL
        if direction == "LONG":
            pb_cond = (lw_c >= body_c * 1.5 and uw_c < body_c
                       and c["close"] >= c["open"])
        else:
            pb_cond = (uw_c >= body_c * 1.5 and lw_c < body_c
                       and c["close"] <= c["open"])
        if pb_cond:
            return "PINBAR_AT_LEVEL", 1

        # SFP: Swing Failure Pattern
        if direction == "LONG":
            sfp_cond = (c["low"] < level - zone_buf
                        and c["close"] > level
                        and vol_ratio > 1.2)
        else:
            sfp_cond = (c["high"] > level + zone_buf
                        and c["close"] < level
                        and vol_ratio > 1.2)
        if sfp_cond:
            return "SFP", 1

        # ── Уровень D ──────────────────────────────────────────────────────
        if vol_ratio > 1.5:
            return "BREAKOUT_RETEST", 0

        return "BOUNCE_PLAIN", 0

    # ─────────────────────────────────────────────────────────────────────────
    # КАЧЕСТВО ПОДХОДА К УРОВНЮ
    # ─────────────────────────────────────────────────────────────────────────

    def _assess_approach_quality(self, df: pd.DataFrame, level: float,
                                 zone_buf: float,
                                 vol_ma: pd.Series) -> tuple[bool, str]:
        """
        ✅ ХОРОШИЙ: объём снижается, свечи уменьшаются, нет вертикального полёта.
        🚫 ПЛОХОЙ: импульс с объёмом, 4+ попыток подряд, вертикальный подход.
        Возвращает (ok, reason).
        """
        if len(df) < 6:
            return True, "Недостаточно данных"

        last     = df.iloc[-1]
        avg_vol  = vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1.0
        last_vol = df["volume"].iloc[-1]
        last_body = abs(last["close"] - last["open"])
        last_rng  = max(last["high"] - last["low"], 1e-10)

        # 🚫 Импульсный подход с большим объёмом
        if last_vol > avg_vol * 1.8 and last_body / last_rng > 0.7:
            return False, "Импульсный подход с высоким объёмом"

        # 🚫 Вертикальный подход: 3+ свечи подряд одного цвета >1% каждая
        vertical_count = 0
        for i in range(-4, -1):
            bar = df.iloc[i]
            bar_pct = abs(bar["close"] - bar["open"]) / max(bar["open"], 1e-10) * 100
            if bar_pct > 1.0:
                if bar["close"] > bar["open"]:
                    vertical_count = vertical_count + 1 if vertical_count >= 0 else 1
                else:
                    vertical_count = vertical_count - 1 if vertical_count <= 0 else -1
            else:
                vertical_count = 0
        if abs(vertical_count) >= 3:
            return False, "Вертикальный подход — слишком быстро"

        # 🚫 4+ касаний уровня подряд
        near_count = 0
        for i in range(max(0, len(df) - 10), len(df) - 1):
            bar = df.iloc[i]
            if bar["low"] <= level + zone_buf and bar["high"] >= level - zone_buf:
                near_count += 1
        if near_count >= 4:
            return False, f"Уровень тестировался {near_count}× подряд — ожидается пробой"

        # ✅ Momentum decay: уменьшение тел свечей
        bodies = [abs(df["close"].iloc[i] - df["open"].iloc[i]) for i in range(-5, 0)]
        size_decaying = (bodies[-1] < bodies[-2] < bodies[-3]
                         and bodies[0] > 0)

        # ✅ Объём на подходе снижался (последние 3 свечи)
        vols = [df["volume"].iloc[i] for i in range(-4, -1)]
        vol_declining = vols[-1] < vols[-2] < vols[0] if vols[0] > 0 else True

        # ✅ Консолидация у уровня (3–7 свечей не пробивают)
        consol_count = 0
        for i in range(-7, -1):
            bar = df.iloc[i]
            if bar["low"] <= level + zone_buf and bar["high"] >= level - zone_buf:
                consol_count += 1
        has_consolidation = 3 <= consol_count <= 7

        reasons_ok = []
        if vol_declining:
            reasons_ok.append("✅ Снижение объёма на подходе")
        if size_decaying:
            reasons_ok.append("✅ Затухающий импульс")
        if has_consolidation:
            reasons_ok.append("✅ Консолидация у уровня")

        if reasons_ok:
            return True, " | ".join(reasons_ok)
        return True, "Нейтральный подход"

    # ─────────────────────────────────────────────────────────────────────────
    # ЛОЖНЫЙ ПРОБОЙ
    # ─────────────────────────────────────────────────────────────────────────

    def _check_fakeout(self, df: pd.DataFrame, level: float,
                       direction: str, zone_buf: float) -> bool:
        last = df.iloc[-1]
        if direction == "LONG":
            return last["low"] < level - zone_buf * 0.5 and last["close"] > level
        return last["high"] > level + zone_buf * 0.5 and last["close"] < level

    # ─────────────────────────────────────────────────────────────────────────
    # ПОДСЧЁТ ТЕСТОВ УРОВНЯ
    # ─────────────────────────────────────────────────────────────────────────

    def _count_recent_tests(self, df: pd.DataFrame, level: float,
                            zone_pct: float, lookback: int = 30) -> int:
        zone_range = level * zone_pct / 100
        count      = 0
        prev_in    = False
        start      = max(0, len(df) - lookback)
        for i in range(start, len(df)):
            bar    = df.iloc[i]
            in_zone = (bar["low"] <= level + zone_range
                       and bar["high"] >= level - zone_range)
            if in_zone and not prev_in:
                count += 1
            prev_in = in_zone
        return count

    # ─────────────────────────────────────────────────────────────────────────
    # TP1 — БЛИЖАЙШИЙ ПРОТИВОПОЛОЖНЫЙ УРОВЕНЬ
    # ─────────────────────────────────────────────────────────────────────────

    def _find_tp1_level(self, direction: str, entry: float,
                        sup_zones: list, res_zones: list) -> Optional[float]:
        if direction == "LONG":
            cands = [z["price"] for z in res_zones if z["price"] > entry * 1.002]
            return min(cands) if cands else None
        cands = [z["price"] for z in sup_zones if z["price"] < entry * 0.998]
        return max(cands) if cands else None

    # ─────────────────────────────────────────────────────────────────────────
    # R:R SCORE
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_rr_score(self, direction: str, entry: float, sl: float,
                            tp1: float, tp2: float, tp3: float) -> float:
        """Взвешенный R:R score: rr1*0.5 + rr2*0.3 + rr3*0.2."""
        risk = abs(entry - sl)
        if risk <= 0:
            return 0.0
        if direction == "LONG":
            rr1 = (tp1 - entry) / risk
            rr2 = (tp2 - entry) / risk
            rr3 = (tp3 - entry) / risk
        else:
            rr1 = (entry - tp1) / risk
            rr2 = (entry - tp2) / risk
            rr3 = (entry - tp3) / risk
        return rr1 * 0.5 + rr2 * 0.3 + rr3 * 0.2

    # ─────────────────────────────────────────────────────────────────────────
    # BTC/ETH КОРРЕЛЯЦИЯ
    # ─────────────────────────────────────────────────────────────────────────

    def _btc_eth_correlation(self, df: pd.DataFrame,
                             df_btc: Optional[pd.DataFrame],
                             df_eth: Optional[pd.DataFrame]) -> dict:
        """
        Rolling 50-bar correlation монеты с BTC и ETH.
        Возвращает {"btc_corr": float, "eth_corr": float, "label": str}.
        """
        result = {"btc_corr": 0.5, "eth_corr": 0.5, "label": "〰️ Слабая корреляция"}

        def _corr(df_a: pd.DataFrame, df_b: pd.DataFrame) -> float:
            try:
                b_close = df_b["close"].reindex(df_a.index, method="nearest")
                window  = min(50, len(df_a) - 1)
                if window < 10:
                    return 0.5
                return float(df_a["close"].rolling(window).corr(b_close).iloc[-1])
            except Exception:
                return 0.5

        if df_btc is not None and len(df_btc) > 10:
            result["btc_corr"] = _corr(df, df_btc)
        if df_eth is not None and len(df_eth) > 10:
            result["eth_corr"] = _corr(df, df_eth)

        bc, ec = result["btc_corr"], result["eth_corr"]
        if bc > 0.75 and ec > 0.75:
            result["label"] = "🔗 Ходит за BTC и ETH"
        elif bc > 0.75:
            result["label"] = "🔗 Ходит за BTC"
        elif ec > 0.75:
            result["label"] = "🔗 Ходит за ETH"
        elif bc < 0.4 and ec < 0.4:
            result["label"] = "🚀 Независимое движение"
        else:
            result["label"] = "〰️ Слабая корреляция с рынком"

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # ТОРГОВАЯ СЕССИЯ
    # ─────────────────────────────────────────────────────────────────────────

    def _market_session_filter(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        Определяет сессию по UTC-времени последней свечи.
        Возвращает (is_active_session, session_name).
        """
        try:
            last_ts = df.index[-1]
            if hasattr(last_ts, "hour"):
                h = last_ts.hour
            else:
                import datetime
                h = pd.Timestamp(last_ts).hour
        except Exception:
            return True, "⏰ Сессия неизвестна"

        if 0 <= h < 8:
            return True, "🌏 Азиатская сессия"
        if 8 <= h < 12:
            return True, "🇬🇧 Лондон открытие"
        if 12 <= h < 16:
            return True, "🌍 Европа + США пересечение"
        if 16 <= h < 21:
            return True, "🇺🇸 Нью-Йорк сессия"
        return True, "🌙 Мёртвая зона"

    # ─────────────────────────────────────────────────────────────────────────
    # RSI ДИВЕРГЕНЦИЯ
    # ─────────────────────────────────────────────────────────────────────────

    def _divergence_check(self, df: pd.DataFrame,
                          rsi_series: pd.Series,
                          direction: str) -> tuple[bool, str]:
        """
        Бычья: цена делает более низкий Low, RSI — более высокий Low.
        Медвежья: цена делает более высокий High, RSI — более низкий High.
        Lookback: 4 пивота.
        """
        if len(df) < 20:
            return False, ""
        try:
            lows_p  = df["low"].rolling(3).min()
            highs_p = df["high"].rolling(3).max()
            lows_r  = rsi_series.rolling(3).min()
            highs_r = rsi_series.rolling(3).max()

            # Берём последние 4 значения
            p_lo  = lows_p.iloc[-4:].values
            r_lo  = lows_r.iloc[-4:].values
            p_hi  = highs_p.iloc[-4:].values
            r_hi  = highs_r.iloc[-4:].values

            if direction == "LONG":
                # Бычья: цена ниже, RSI выше
                if p_lo[-1] < p_lo[-2] and r_lo[-1] > r_lo[-2]:
                    return True, "✅ Бычья дивергенция RSI"
            else:
                # Медвежья: цена выше, RSI ниже
                if p_hi[-1] > p_hi[-2] and r_hi[-1] < r_hi[-2]:
                    return True, "✅ Медвежья дивергенция RSI"
        except Exception as e:
            log.debug(f"Divergence check ошибка: {e}")
        return False, ""

    # ─────────────────────────────────────────────────────────────────────────
    # HTF CONFLUENCY (унаследованный)
    # ─────────────────────────────────────────────────────────────────────────

    def _htf_confluence(self, df_htf: Optional[pd.DataFrame],
                        direction: str) -> bool:
        """HTF тренд совпадает с направлением сигнала."""
        if df_htf is None or len(df_htf) < self.cfg.HTF_EMA_PERIOD:
            return False
        htf_ema = self._ema(df_htf["close"], self.cfg.HTF_EMA_PERIOD).iloc[-1]
        htf_price = df_htf["close"].iloc[-1]
        if direction == "LONG":
            return htf_price > htf_ema
        return htf_price < htf_ema

    # ─────────────────────────────────────────────────────────────────────────
    # ГЕНЕРАЦИЯ human_explanation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_p(v: float) -> str:
        """Форматирует цену без научной нотации."""
        if v >= 10_000: return f"{v:,.0f}"
        if v >= 100:    return f"{v:,.1f}"
        if v >= 1:      return f"{v:.4f}".rstrip("0").rstrip(".")
        return f"{v:.6f}".rstrip("0").rstrip(".")

    def _build_human_explanation(self, signal: str, s_level: float,
                                 s_class: int, s_hits: int, s_type: str,
                                 entry: float, sl: float,
                                 tp1: float, tp2: float,
                                 rr1: float, rr2: float,
                                 risk_pct: float, session: str,
                                 corr_label: str,
                                 diverg_label: str) -> str:
        fp = self._fmt_p
        is_long = signal == "LONG"
        cls_names = {1: "Абсолютный", 2: "Сильный", 3: "Рабочий"}
        lvl_label = cls_names.get(s_class, "Рабочий")
        side_lvl  = "поддержки" if is_long else "сопротивления"

        # 1. Описание уровня
        hits_str = (f"{s_hits} касания" if s_hits <= 4
                    else f"{s_hits} касаний")
        if s_hits >= 3:
            lvl_part = (f"{lvl_label} уровень {fp(s_level)} ({hits_str}) — "
                        f"каждый раз давал уверенную реакцию.")
        else:
            lvl_part = (f"{lvl_label} уровень {fp(s_level)} ({hits_str}).")

        # 2. Тип сетапа
        if "Ложный пробой" in s_type or "Fakeout" in s_type:
            if is_long:
                setup_part = ("Цена ушла ниже уровня, но быстро вернулась — "
                              "ложный пробой вниз, ловушка для продавцов.")
            else:
                setup_part = ("Цена пробила уровень вверх, но не закрепилась — "
                              "ложный пробой, ловушка для покупателей.")
        elif "SFP" in s_type:
            if is_long:
                setup_part = ("Захват ликвидности снизу (SFP): пробой ниже уровня "
                              "со быстрым возвратом — бычья ловушка медвежьих стопов.")
            else:
                setup_part = ("Захват ликвидности сверху (SFP): пробой выше уровня "
                              "со быстрым возвратом — медвежья ловушка бычьих стопов.")
        elif "Ретест" in s_type:
            if is_long:
                setup_part = ("Уровень пробит снизу вверх и сменил роль — "
                              "бывшее сопротивление стало поддержкой. "
                              "Ретест — классическая точка входа в лонг.")
            else:
                setup_part = ("Уровень пробит сверху вниз и сменил роль — "
                              "бывшая поддержка стала сопротивлением. "
                              "Ретест подтверждает смену тренда.")
        elif "Пробой" in s_type:
            if is_long:
                setup_part = ("Пробой ключевого уровня вверх с закреплением. "
                              "Вход на импульсе по тренду.")
            else:
                setup_part = ("Пробой ключевой поддержки вниз с закреплением. "
                              "Вход на импульсе по тренду.")
        else:
            if is_long:
                setup_part = (f"Цена подошла к уровню {side_lvl} и показывает "
                              "признаки разворота. Отскок от ключевой зоны.")
            else:
                setup_part = (f"Цена подошла к уровню {side_lvl} и теряет импульс. "
                              "Ожидается отказ и разворот вниз.")

        # 3. Риск и цели
        risk_part = (f"Стоп за структуру {fp(sl)} (риск {risk_pct:.1f}%). "
                     f"TP1: {fp(tp1)} (R:R 1:{rr1:.1f}), TP2: {fp(tp2)} (R:R 1:{rr2:.1f}).")

        parts = [lvl_part, setup_part]
        if session:
            parts.append(session)
        if corr_label:
            parts.append(corr_label)
        if diverg_label:
            parts.append(diverg_label)
        parts.append(risk_part)
        return " ".join(parts)

    # ─────────────────────────────────────────────────────────────────────────
    # ГЛАВНЫЙ МЕТОД АНАЛИЗА
    # ─────────────────────────────────────────────────────────────────────────

    def analyze(self, symbol: str, df: pd.DataFrame,
                df_htf: Optional[pd.DataFrame] = None,
                df_btc: Optional[pd.DataFrame] = None,
                df_eth: Optional[pd.DataFrame] = None) -> Optional["SignalResult"]:
        """
        Основной метод. Вызывается scanner_mid.py по имени analyze().
        НЕ переименовывать.
        """
        cfg = self.cfg
        if df is None or len(df) < max(cfg.EMA_SLOW, 100):
            return None

        bar_idx = len(df) - 1
        if bar_idx - self._last_signal.get(symbol, -9999) < cfg.COOLDOWN_BARS:
            return None

        result = self._do_analyze(symbol, df, df_htf, df_btc, df_eth,
                                  min_quality_override=None)
        if result is not None:
            self._last_signal[symbol] = bar_idx
        return result

    def analyze_on_demand(self, symbol: str, df: pd.DataFrame,
                          df_htf: Optional[pd.DataFrame] = None,
                          df_btc: Optional[pd.DataFrame] = None,
                          df_eth: Optional[pd.DataFrame] = None) -> Optional["SignalResult"]:
        """
        Ручной анализ монеты (/analyze команда).
        Не проверяет COOLDOWN_BARS, не записывает в _last_signal.
        MIN_QUALITY снижен до 1 — показываем всё что нашли.
        """
        if df is None or len(df) < 50:
            return None
        return self._do_analyze(symbol, df, df_htf, df_btc, df_eth,
                                min_quality_override=1)

    # ─────────────────────────────────────────────────────────────────────────
    # ВНУТРЕННИЙ МЕТОД АНАЛИЗА
    # ─────────────────────────────────────────────────────────────────────────

    def _do_analyze(self, symbol: str, df: pd.DataFrame,
                    df_htf: Optional[pd.DataFrame],
                    df_btc: Optional[pd.DataFrame],
                    df_eth: Optional[pd.DataFrame],
                    min_quality_override: Optional[int]) -> Optional["SignalResult"]:
        cfg = self.cfg

        # ── Базовые индикаторы ────────────────────────────────────────────
        close  = df["close"]
        atr    = self._atr(df, cfg.ATR_PERIOD)
        ema50  = self._ema(close, cfg.EMA_FAST)
        ema200 = self._ema(close, cfg.EMA_SLOW)
        rsi    = self._rsi(close, cfg.RSI_PERIOD)
        vol_ma = df["volume"].rolling(cfg.VOL_LEN).mean()

        c_now     = float(close.iloc[-1])
        atr_now   = float(atr.iloc[-1])
        rsi_now   = float(rsi.iloc[-1])
        vol_now   = float(df["volume"].iloc[-1])
        vol_avg   = float(vol_ma.iloc[-1]) if vol_ma.iloc[-1] > 0 else 1.0
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

        bull_local = c_now > ema50.iloc[-1] > ema200.iloc[-1]
        bear_local = c_now < ema50.iloc[-1] < ema200.iloc[-1]
        trend_local = ("📈 Бычий" if bull_local
                       else ("📉 Медвежий" if bear_local else "↔️ Боковик"))

        # ── Сессия ────────────────────────────────────────────────────────
        _, session = self._market_session_filter(df)

        # ── Сессионный штраф (определяем заранее для quality) ────────────
        is_dead_session = session in ("🌏 Азиатская сессия", "🌙 Мёртвая зона")

        # ── Зоны (многослойная кластеризация) ────────────────────────────
        sup_zones, res_zones = self._get_zones(df, cfg.PIVOT_STRENGTH, atr_now)
        if not sup_zones and not res_zones:
            log.debug(f"{symbol}: нет зон уровней")
            return None

        # ── Паттерны ─────────────────────────────────────────────────────
        bull_pat, bear_pat = self._detect_pattern(df)

        # ── Ближайший уровень ─────────────────────────────────────────────
        all_levels = [(z["price"], z) for z in sup_zones + res_zones]
        nearest_price, _ = min(all_levels, key=lambda x: abs(x[0] - c_now))
        dist_pct = abs(c_now - nearest_price) / c_now * 100
        if dist_pct > cfg.MAX_DIST_PCT:
            log.debug(f"{symbol}: дистанция до уровня {dist_pct:.2f}% > {cfg.MAX_DIST_PCT}%")
            return None

        ZONE_PCT = cfg.ZONE_PCT

        # ══════════════════════════════════════════════════════════════════
        # ПОИСК СИГНАЛА
        # ══════════════════════════════════════════════════════════════════

        signal: Optional[str]  = None
        s_level: Optional[float] = None
        s_type     = ""
        is_counter = False
        s_hits     = 0
        s_class    = 3
        s_zone: dict = {}

        # ── ЛОНГ от поддержки ─────────────────────────────────────────────
        for sup in reversed(sup_zones):
            lvl       = sup["price"]
            hits      = sup["hits"]
            lvl_class = sup["class"]
            zone_buf  = lvl * ZONE_PCT / 100

            if abs(c_now - lvl) > zone_buf * 3:
                continue

            # Fakeout (приоритет 1)
            if self._check_fakeout(df, lvl, "LONG", zone_buf):
                signal, s_level = "LONG", lvl
                s_type = "Ложный пробой (Fakeout)"
                is_counter = bear_local
                s_hits = hits; s_class = lvl_class; s_zone = sup
                break

            # Bounce с паттерном
            if abs(c_now - lvl) < zone_buf * 2 and bull_pat:
                signal, s_level = "LONG", lvl
                s_type = "Отскок от поддержки"
                is_counter = bear_local
                s_hits = hits; s_class = lvl_class; s_zone = sup
                break

            # SFP
            if (df["low"].iloc[-1] < lvl - zone_buf
                    and c_now > lvl and vol_ratio > 1.2):
                signal, s_level = "LONG", lvl
                s_type = "SFP (Захват ликвидности)"
                is_counter = bear_local
                s_hits = hits; s_class = lvl_class; s_zone = sup
                break

        # ── ЛОНГ: ретест пробитого сопротивления ─────────────────────────
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
                    is_counter = bear_local
                    s_hits = hits; s_class = lvl_class; s_zone = res
                    break

                # Честный пробой вверх
                if (df["close"].iloc[-2] < lvl
                        and c_now > lvl + zone_buf and vol_ratio > 1.5):
                    signal, s_level = "LONG", lvl
                    s_type = "Пробой уровня"
                    is_counter = bear_local
                    s_hits = hits; s_class = lvl_class; s_zone = res
                    break

        # ── ШОРТ от сопротивления ─────────────────────────────────────────
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
                    is_counter = bull_local
                    s_hits = hits; s_class = lvl_class; s_zone = res
                    break

                if abs(c_now - lvl) < zone_buf * 2 and bear_pat:
                    signal, s_level = "SHORT", lvl
                    s_type = "Отскок от сопротивления"
                    is_counter = bull_local
                    s_hits = hits; s_class = lvl_class; s_zone = res
                    break

                if (df["high"].iloc[-1] > lvl + zone_buf
                        and c_now < lvl and vol_ratio > 1.2):
                    signal, s_level = "SHORT", lvl
                    s_type = "SFP (Ложный пробой вверх)"
                    is_counter = bull_local
                    s_hits = hits; s_class = lvl_class; s_zone = res
                    break

        # ── ШОРТ: ретест пробитой поддержки ──────────────────────────────
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
                    is_counter = bull_local
                    s_hits = hits; s_class = lvl_class; s_zone = sup
                    break

                if (df["close"].iloc[-2] > lvl
                        and c_now < lvl - zone_buf and vol_ratio > 1.5):
                    signal, s_level = "SHORT", lvl
                    s_type = "Пробой поддержки"
                    is_counter = bull_local
                    s_hits = hits; s_class = lvl_class; s_zone = sup
                    break

        if not signal or s_level is None:
            return None

        zone_buf = s_level * ZONE_PCT / 100

        # ── Институциональный паттерн (иерархия A→D) ─────────────────────
        inst_pattern, pattern_bonus = self._detect_institutional_pattern(
            df, s_level, signal, vol_ratio, zone_buf
        )

        # ── Качество подхода ──────────────────────────────────────────────
        approach_ok, approach_reason = self._assess_approach_quality(
            df, s_level, zone_buf, vol_ma
        )
        if not approach_ok:
            log.debug(f"{symbol}: Плохой подход — {approach_reason}")
            return None

        # ── Тест-счётчик ──────────────────────────────────────────────────
        test_count = self._count_recent_tests(df, s_level, ZONE_PCT, lookback=30)
        if test_count >= cfg.MAX_LEVEL_TESTS:
            log.debug(
                f"{symbol}: Уровень {s_level:.4f} тестировался {test_count}× "
                f"— ожидается пробой"
            )
            return None

        # ── Фильтр RSI ────────────────────────────────────────────────────
        if cfg.USE_RSI_FILTER:
            if signal == "LONG" and rsi_now > cfg.RSI_OB:
                return None
            if signal == "SHORT" and rsi_now < cfg.RSI_OS:
                return None

        # ══════════════════════════════════════════════════════════════════
        # СТОП-ЛОСС (структурный)
        # ══════════════════════════════════════════════════════════════════
        entry = c_now
        if signal == "LONG":
            zone_bottom = s_level - zone_buf
            sl = zone_bottom - atr_now * 1.5   # 1.5 ATR ниже зоны — за структуру
            # Safeguard: не дальше cfg.MAX_RISK_PCT
            sl = max(sl, entry * (1.0 - cfg.MAX_RISK_PCT / 100))
            if sl >= entry:
                log.debug(f"{symbol}: LONG SL={sl:.4f} >= entry={entry:.4f} — отмена")
                return None
        else:
            zone_top = s_level + zone_buf
            sl = zone_top + atr_now * 1.5      # 1.5 ATR выше зоны — за структуру
            sl = min(sl, entry * (1.0 + cfg.MAX_RISK_PCT / 100))
            if sl <= entry:
                log.debug(f"{symbol}: SHORT SL={sl:.4f} <= entry={entry:.4f} — отмена")
                return None

        risk = abs(entry - sl)
        if risk <= 0:
            return None

        # ══════════════════════════════════════════════════════════════════
        # ФИЛЬТР СТОПА (ATR + минимум по типу монеты)
        # ══════════════════════════════════════════════════════════════════
        _MEMCOIN_KW = ("FLOKI","PEPE","SHIB","DOGE","WIF","BONK","NEIRO",
                       "MEME","SATS","TURBO","CATS","ACT","BOME","BOOK")
        _sym_up   = symbol.upper()
        is_memcoin = any(k in _sym_up for k in _MEMCOIN_KW)
        is_major   = "BTC" in _sym_up or "ETH" in _sym_up

        risk_pct_raw = risk / entry * 100
        if is_major and risk_pct_raw < 0.4:
            log.debug(f"{symbol}: stop {risk_pct_raw:.2f}% < 0.4% (BTC/ETH min) — пропуск")
            return None
        if is_memcoin and risk_pct_raw < 1.5:
            log.debug(f"{symbol}: stop {risk_pct_raw:.2f}% < 1.5% (мемкоин min) — пропуск")
            return None
        if not is_major and not is_memcoin and risk_pct_raw < 0.8:
            log.debug(f"{symbol}: stop {risk_pct_raw:.2f}% < 0.8% (альт min) — пропуск")
            return None
        if atr_now > 0 and risk < atr_now * 1.5:
            log.debug(f"{symbol}: stop {risk:.5f} < 1.5×ATR({atr_now:.5f}) — пропуск")
            return None

        # ══════════════════════════════════════════════════════════════════
        # ЦЕЛИ (строгий порядок TP)
        # ══════════════════════════════════════════════════════════════════
        tp1_from_level = self._find_tp1_level(signal, entry, sup_zones, res_zones)
        if tp1_from_level is not None:
            tp1 = tp1_from_level
        else:
            tp1 = (entry + risk * cfg.TP1_RR if signal == "LONG"
                   else entry - risk * cfg.TP1_RR)

        tp2 = (entry + risk * cfg.TP2_RR if signal == "LONG"
               else entry - risk * cfg.TP2_RR)
        tp3 = (entry + risk * cfg.TP3_RR if signal == "LONG"
               else entry - risk * cfg.TP3_RR)

        # Исправление порядка TP (критический баг — предотвращаем инверсию)
        if signal == "LONG":
            if tp1 >= tp2:
                tp2 = entry + risk * cfg.TP2_RR
            if tp2 >= tp3:
                tp3 = tp2 + risk * 1.5
            if not (entry < tp1 < tp2 < tp3):
                log.debug(f"{symbol}: LONG TP порядок нарушен — отмена")
                return None
        else:
            if tp1 <= tp2:
                tp2 = entry - risk * cfg.TP2_RR
            if tp2 <= tp3:
                tp3 = tp2 - risk * 1.5
            if not (entry > tp1 > tp2 > tp3):
                log.debug(f"{symbol}: SHORT TP порядок нарушен — отмена")
                return None

        # ── Проверка R:R ──────────────────────────────────────────────────
        rr_actual = ((tp1 - entry) / risk if signal == "LONG"
                     else (entry - tp1) / risk)

        if rr_actual < cfg.MIN_RR:
            log.debug(f"{symbol}: R:R={rr_actual:.2f} < {cfg.MIN_RR} — пропуск")
            return None

        risk_pct = abs((sl - entry) / entry * 100)

        # ── R:R score ─────────────────────────────────────────────────────
        rr_score = self._calculate_rr_score(signal, entry, sl, tp1, tp2, tp3)
        if rr_score < cfg.MIN_RR * 0.5:
            log.debug(f"{symbol}: rr_score={rr_score:.2f} — пропуск")
            return None

        # ── Корреляция ────────────────────────────────────────────────────
        corr_data = self._btc_eth_correlation(df, df_btc, df_eth)

        # ── RSI дивергенция ───────────────────────────────────────────────
        diverg_ok, diverg_label = self._divergence_check(df, rsi, signal)

        # ── LVN на пути к TP ─────────────────────────────────────────────
        has_lvn_path = False
        if s_zone and "lvn_checker" in s_zone:
            try:
                has_lvn_path = s_zone["lvn_checker"](entry, tp1)
            except Exception:
                pass

        # ══════════════════════════════════════════════════════════════════
        # СИСТЕМА QUALITY (0–10)
        # ══════════════════════════════════════════════════════════════════
        quality = 0
        reasons: list[str] = []
        class_names = {1: "Абсолютный", 2: "Сильный", 3: "Рабочий"}

        # Базовый тип сигнала
        reasons.append(f"✅ {s_type}")

        # Паттерн (бонус по иерархии A/B/C/D)
        if pattern_bonus == 3:
            quality += 3
            reasons.append(f"✅ [{inst_pattern}] — паттерн класса A")
        elif pattern_bonus == 2:
            quality += 2
            reasons.append(f"✅ [{inst_pattern}] — паттерн класса B")
        elif pattern_bonus == 1:
            quality += 1
            reasons.append(f"✅ [{inst_pattern}] — паттерн класса C")
        elif inst_pattern:
            reasons.append(f"⚠️ [{inst_pattern}] — паттерн класса D")

        # Класс уровня
        reasons.append(f"✅ Уровень класса {s_class} ({class_names.get(s_class, '')})")
        if s_class == 1:
            quality += 2
        elif s_class == 2:
            quality += 1

        # HTF подтверждение
        if cfg.USE_HTF_FILTER:
            htf_ok = self._htf_confluence(df_htf, signal)
            if htf_ok:
                quality += 1
                reasons.append("✅ HTF тренд подтверждает")

        # MTF уровень
        mtf_label = s_zone.get("mtf_label", "")
        tf_count  = len(s_zone.get("timeframes", []))
        if mtf_label == "Институциональный" or tf_count >= 3:
            quality += 2
            reasons.append("✅ Институциональный MTF уровень (3 ТФ)")
        elif mtf_label == "MTF" or tf_count == 2:
            quality += 1
            reasons.append("✅ MTF уровень (2 ТФ)")

        # По тренду
        if not is_counter:
            quality += 1
            reasons.append("✅ По локальному тренду")
        else:
            quality -= 1
            reasons.append("⚠️ Контртренд")

        # Объём
        if vol_ratio >= cfg.VOL_MULT:
            quality += 1
            reasons.append(f"✅ Объём x{vol_ratio:.1f}")

        # HVN у уровня
        if s_zone.get("has_hvn"):
            quality += 1
            reasons.append("✅ Volume Profile узел (HVN)")

        # LVN на пути к TP
        if has_lvn_path:
            quality += 1
            reasons.append("✅ Чистый путь к цели (LVN)")

        # RSI дивергенция
        if diverg_ok:
            quality += 2
            reasons.append(diverg_label)

        # Подход
        if approach_ok and approach_reason not in ("Нейтральный подход", "Недостаточно данных"):
            quality += 1
            reasons.append(f"✅ {approach_reason}")

        # Консолидация у уровня
        if "Консолидация" in approach_reason:
            quality += 1  # дополнительный бонус

        # RSI экстремумы
        if signal == "LONG" and 30 < rsi_now < 45:
            reasons.append("✅ RSI выходит из перепроданности")
        elif signal == "SHORT" and 55 < rsi_now < 70:
            reasons.append("✅ RSI выходит из перекупленности")

        # Независимая монета
        if corr_data["btc_corr"] < 0.4 and corr_data["eth_corr"] < 0.4:
            quality += 1
            reasons.append("✅ Независимое движение (corr < 0.4)")

        # Тест-счётчик
        if test_count <= 2:
            reasons.append(f"✅ Тест #{test_count} уровня (оптимально)")
        else:
            quality -= 1
            reasons.append(f"⚠️ Тест #{test_count} — уровень слабеет")

        # Штраф за мёртвую сессию
        if is_dead_session:
            quality -= 2
            reasons.append(f"⚠️ {session} — низкая ликвидность")

        # Взвешенный R:R фильтр
        if rr_score < 1.2:
            log.debug(f"{symbol}: rr_score={rr_score:.2f} < 1.2 — блок")
            return None
        if rr_score < 1.8:
            quality -= 1
            reasons.append(f"⚠️ Взвешенный R:R: {rr_score:.2f} (ниже 1.8)")

        # Зажим в [0, 10]
        quality = max(0, min(10, quality))

        # Мемкоин: максимум ⭐⭐⭐
        if is_memcoin and quality > 3:
            quality = 3
            reasons.append("⚠️ Мемкоин: ограничение ⭐⭐⭐ (повышенный риск)")

        # ── Финальный чеклист ─────────────────────────────────────────────
        has_pattern  = bool(bull_pat if signal == "LONG" else bear_pat)
        is_fakeout   = "Fakeout" in s_type or "SFP" in s_type
        checklist = [
            s_class <= 2 or (s_class == 3 and quality >= 4),  # уровень
            approach_ok,                                         # подход
            has_pattern or is_fakeout or vol_ratio > 1.5,       # реакция
            rr_actual >= cfg.MIN_RR,                             # R:R
            test_count <= cfg.MAX_LEVEL_TESTS - 1,              # тесты
        ]
        if not all(checklist):
            log.debug(
                f"{symbol}: Чеклист НЕ пройден — "
                f"[Уровень:{checklist[0]} Подход:{checklist[1]} "
                f"Реакция:{checklist[2]} R:R:{checklist[3]} Тест:{checklist[4]}]"
            )
            return None

        # ── Quality фильтр ────────────────────────────────────────────────
        # analyze_on_demand() может передать min_quality_override=1.
        # Обычный analyze() не фильтрует по quality — это делает scanner_mid.py.
        if min_quality_override is not None and quality < min_quality_override:
            log.debug(f"{symbol}: quality={quality} < min={min_quality_override} — пропуск")
            return None

        # ── human_explanation ─────────────────────────────────────────────
        if signal == "LONG":
            rr1_val = (tp1 - entry) / risk
            rr2_val = (tp2 - entry) / risk
        else:
            rr1_val = (entry - tp1) / risk
            rr2_val = (entry - tp2) / risk

        explanation = self._build_human_explanation(
            signal, s_level, s_class, s_hits, s_type,
            entry, sl, tp1, tp2, rr1_val, rr2_val, risk_pct,
            session, corr_data["label"], diverg_label
        )

        log.debug(
            f"{symbol}: {signal} @ {entry:.4g} | class={s_class} "
            f"quality={quality} | {s_type}"
        )

        return SignalResult(
            symbol=symbol,
            direction=signal,
            entry=entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_pct=risk_pct,
            quality=quality,
            reasons=reasons,
            rsi=rsi_now,
            volume_ratio=vol_ratio,
            trend_local=trend_local,
            trend_htf="",
            pattern=inst_pattern,
            breakout_type=s_type,
            is_counter_trend=is_counter,
            human_explanation=explanation,
            level_class=s_class,
            test_count=test_count,
            rr_score=rr_score,
            corr_label=corr_data["label"],
            session=session,
        )
