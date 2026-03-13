"""
gerchik_strategy.py — Стратегия уровневой торговли по методу Александра Герчика.

КОНЦЕПЦИЯ:
  Цена движется от одного сильного уровня к другому.
  Торговля ведётся ТОЛЬКО от уровней поддержки и сопротивления.
  Никаких индикаторов — только Price Action + объём.

КЛЮЧЕВЫЕ ТЕРМИНЫ:
  БСУ   — бар, сформировавший уровень
  БПУ-1 — первый бар, подтвердивший уровень (касается и отскакивает)
  БПУ-2 — второй подтверждающий бар (сразу за БПУ-1, без пропусков)
  ТВХ   — точка входа (лимитный ордер у уровня + люфт 20% от стопа)

Зависимости:
  pip install pandas numpy ccxt aiogram

Запуск:
  python gerchik_strategy.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("Gerchik")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

# ══════════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GerchikConfig:
    """Все параметры стратегии вынесены в один dataclass."""

    # ── ATR ───────────────────────────────────────────────────────────────────
    atr_period:          int   = 14    # период ATR
    atr_entry_threshold: float = 0.75  # макс. доля дневного ATR для входа (75%)

    # ── Уровни ────────────────────────────────────────────────────────────────
    level_lookback:        int   = 50    # баров назад для поиска уровней
    pivot_strength:        int   = 5     # N баров слева и справа для пивота
    level_touch_threshold: float = 0.002 # 0.2% — допуск «касания» уровня
    cluster_tolerance:     float = 0.003 # 0.3% — объединяем уровни ближе этого

    # ── Риск-менеджмент ───────────────────────────────────────────────────────
    min_rr_ratio:    float = 3.0   # минимальный R:R (запас хода до следующего уровня)
    risk_per_trade:  float = 0.01  # риск на сделку — 1% от капитала
    lft_pct:         float = 0.20  # люфт = 20% от размера стопа

    # ── Take Profit ───────────────────────────────────────────────────────────
    tp1_rr:          float = 3.0   # TP1 = 3R от стопа
    tp2_rr:          float = 4.0   # TP2 = 4R от стопа
    tp1_close_pct:   float = 0.55  # 55% позиции закрываем на TP1
    tp2_close_pct:   float = 0.45  # 45% позиции закрываем на TP2

    # ── Дневные лимиты ────────────────────────────────────────────────────────
    max_daily_losses:     int   = 3    # максимум убыточных сделок в день
    max_dd_pct_of_daily:  float = 0.30 # убыток ≥ 30% дневного профита → стоп


# ══════════════════════════════════════════════════════════════════════════════
# ТИПЫ
# ══════════════════════════════════════════════════════════════════════════════

Direction = Literal["LONG", "SHORT"]
LevelType = Literal["support", "resistance"]


@dataclass
class Level:
    """Торговый уровень поддержки или сопротивления."""
    price:       float
    level_type:  LevelType   # "support" | "resistance"
    strength:    int   = 1   # 1–6 баллов
    touch_count: int   = 1   # сколько касаний было
    bar_index:   int   = 0   # индекс бара-формирователя (БСУ)
    is_mirror:   bool  = False


@dataclass
class TradeSignal:
    """Сигнал на вход, сформированный стратегией."""
    direction:     Direction
    symbol:        str
    entry:         float
    sl:            float
    tp1:           float
    tp2:           float
    level:         Level
    rr:            float
    position_size: float = 0.0
    bar_index:     int   = 0
    timestamp:     str   = ""


# ══════════════════════════════════════════════════════════════════════════════
# ЗАГРУЗКА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════════════

def load_ohlcv(
    symbol:        str = "BTC/USDT",
    timeframe:     str = "1d",
    limit:         int = 500,
    exchange_name: str = "bybit",
) -> pd.DataFrame:
    """
    Загружает OHLCV-данные с биржи через ccxt.

    Поддерживаемые биржи: bybit, bingx, okx.
    Возвращает DataFrame с колонками open, high, low, close, volume.

    Args:
        symbol:        торговая пара, например "BTC/USDT".
        timeframe:     таймфрейм ccxt ("1d", "4h", "1h", "5m" и т.д.).
        limit:         количество баров.
        exchange_name: название биржи.

    Returns:
        pd.DataFrame с индексом timestamp и колонками OHLCV.
    """
    try:
        import ccxt
    except ImportError:
        raise ImportError("Установи ccxt: pip install ccxt")

    supported = {"bybit": ccxt.bybit, "bingx": ccxt.bingx, "okx": ccxt.okx}
    if exchange_name not in supported:
        raise ValueError(
            f"Биржа '{exchange_name}' не поддерживается. "
            f"Доступны: {list(supported)}"
        )

    exchange = supported[exchange_name]({"enableRateLimit": True})

    log.info(f"Загружаю {symbol} {timeframe} ×{limit} с {exchange_name}...")
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as exc:
        raise RuntimeError(
            f"Ошибка загрузки данных с {exchange_name}: {exc}"
        ) from exc

    df = pd.DataFrame(
        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df.astype(float)

    log.info(f"Загружено {len(df)} баров: {df.index[0]} — {df.index[-1]}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM АЛЁРТЕР
# ══════════════════════════════════════════════════════════════════════════════

class TelegramAlerter:
    """
    Отправляет торговые сигналы в Telegram-бот.

    Параметры берёт из переменных окружения:
      GERCHIK_BOT_TOKEN — токен Telegram-бота (BotFather)
      GERCHIK_CHAT_ID   — ID чата/канала для отправки сигналов
    """

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or os.environ.get("GERCHIK_BOT_TOKEN", "")
        self.chat_id   = chat_id   or os.environ.get("GERCHIK_CHAT_ID",   "")

        if not self.bot_token or not self.chat_id:
            log.warning(
                "TelegramAlerter: GERCHIK_BOT_TOKEN / GERCHIK_CHAT_ID не заданы "
                "— отправка отключена."
            )
            self._enabled = False
            return

        try:
            from aiogram import Bot
            self._bot    = Bot(token=self.bot_token)
            self._enabled = True
        except ImportError:
            raise ImportError("Установи aiogram: pip install aiogram")

    # ── Форматирование сообщения ──────────────────────────────────────────────

    @staticmethod
    def _fp(v: float) -> str:
        """Форматирует цену: убирает лишние нули, добавляет разделители."""
        if v >= 10_000:
            return f"{v:,.0f}"
        if v >= 100:
            return f"{v:.2f}"
        return f"{v:.4f}".rstrip("0").rstrip(".")

    def _format_message(self, signal: dict) -> str:
        """
        Форматирует сигнал в HTML-сообщение для Telegram.

        Ожидаемые ключи signal:
          direction, symbol, entry, sl, tp1, tp2,
          level_strength, rr, level_type, timestamp.
        """
        direction  = signal.get("direction",     "LONG")
        symbol     = signal.get("symbol",         "—")
        entry      = float(signal.get("entry",    0))
        sl         = float(signal.get("sl",        0))
        tp1        = float(signal.get("tp1",       0))
        tp2        = float(signal.get("tp2",       0))
        strength   = int(signal.get("level_strength", 1))
        rr         = float(signal.get("rr",        3.0))
        level_type = signal.get("level_type",     "support")
        ts         = signal.get("timestamp",       "")

        dir_emoji  = "🟢" if direction == "LONG" else "🔴"
        dir_text   = "ЛОНГ" if direction == "LONG" else "ШОРТ"
        lvl_text   = "поддержка" if level_type == "support" else "сопротивление"
        stars      = "⭐" * min(strength, 6)

        fp = self._fp
        risk_pct   = abs(entry - sl) / entry * 100 if entry else 0

        return (
            f"{dir_emoji} <b>СИГНАЛ ГЕРЧИКА — {dir_text}</b>\n\n"
            f"💎 <b>{symbol}</b>  |  Уровень: {lvl_text}\n"
            f"💪 Сила уровня: {stars} ({strength}/6)\n\n"
            f"💰 Вход:        <code>{fp(entry)}</code>\n"
            f"🛑 Стоп:        <code>{fp(sl)}</code>"
            f"  <i>(-{risk_pct:.2f}%)</i>\n"
            f"🎯 TP1 (3R):   <code>{fp(tp1)}</code>"
            f"  <i>(55% позиции)</i>\n"
            f"🏆 TP2 (4R):   <code>{fp(tp2)}</code>"
            f"  <i>(45% позиции)</i>\n\n"
            f"📐 R:R = <b>1:{rr:.1f}</b>\n"
            f"⏰ <i>{ts}</i>\n\n"
            f"⚡ <i>CHM Laboratory — Стратегия Герчика</i>"
        )

    # ── Отправка ──────────────────────────────────────────────────────────────

    async def send_signal(self, signal: dict) -> bool:
        """
        Асинхронно отправляет торговый сигнал в Telegram.

        Args:
            signal: словарь с полями сигнала (см. _format_message).

        Returns:
            True если сообщение отправлено успешно.
        """
        if not self._enabled:
            return False

        text = self._format_message(signal)
        try:
            await self._bot.send_message(
                chat_id    = self.chat_id,
                text       = text,
                parse_mode = "HTML",
            )
            log.info(
                f"Telegram: сигнал отправлен → "
                f"{signal.get('symbol')} {signal.get('direction')}"
            )
            return True
        except Exception as exc:
            log.error(f"Telegram ошибка: {exc}")
            return False

    def send_signal_sync(self, signal: dict) -> bool:
        """
        Синхронная обёртка для send_signal.
        Используется в бэктесте и синхронном коде.
        """
        if not self._enabled:
            return False
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send_signal(signal))
                return True
            return loop.run_until_complete(self.send_signal(signal))
        except Exception as exc:
            log.error(f"send_signal_sync: {exc}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ КЛАСС СТРАТЕГИИ
# ══════════════════════════════════════════════════════════════════════════════

class GerchikStrategy:
    """
    Реализация уровневой торговой стратегии Александра Герчика.

    Цена движется от сильного уровня к сильному уровню.
    Торговля только от уровней поддержки / сопротивления
    на основе паттерна: БСУ → БПУ-1 → БПУ-2.
    """

    def __init__(
        self,
        config: Optional[GerchikConfig] = None,
        symbol: str = "BTC/USDT",
    ):
        self.cfg    = config or GerchikConfig()
        self.symbol = symbol

    # ─── 1. ATR ──────────────────────────────────────────────────────────────

    def compute_atr(self, df: pd.DataFrame) -> pd.Series:
        """
        Вычисляет Average True Range по Уайлдеру.

        True Range = max(high−low, |high−prev_close|, |low−prev_close|)
        ATR = скользящее среднее TR за atr_period баров.

        Returns:
            pd.Series с ATR на каждый бар.
        """
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        prev  = close.shift(1)

        tr = pd.concat(
            [(high - low), (high - prev).abs(), (low - prev).abs()],
            axis=1,
        ).max(axis=1)

        return tr.rolling(window=self.cfg.atr_period, min_periods=1).mean()

    # ─── 2. Поиск уровней ────────────────────────────────────────────────────

    def find_levels(self, df: pd.DataFrame) -> list[Level]:
        """
        Находит уровни поддержки и сопротивления как локальные экстремумы (пивоты).

        Сопротивление: high[i] — максимум в окне ±pivot_strength баров.
        Поддержка:     low[i]  — минимум в окне ±pivot_strength баров.

        Args:
            df: OHLCV DataFrame.

        Returns:
            Список объектов Level.
        """
        ps     = self.cfg.pivot_strength
        levels: list[Level] = []
        highs  = df["high"].values
        lows   = df["low"].values

        for i in range(ps, len(df) - ps):
            lo = i - ps
            hi = i + ps + 1

            # Сопротивление: локальный максимум по high
            if highs[i] >= highs[lo:hi].max():
                levels.append(Level(
                    price      = float(highs[i]),
                    level_type = "resistance",
                    bar_index  = i,
                ))

            # Поддержка: локальный минимум по low
            if lows[i] <= lows[lo:hi].min():
                levels.append(Level(
                    price      = float(lows[i]),
                    level_type = "support",
                    bar_index  = i,
                ))

        return levels

    # ─── 3. Кластеризация ────────────────────────────────────────────────────

    def cluster_levels(
        self, levels: list[Level], price: float
    ) -> list[Level]:
        """
        Объединяет близкие уровни одного типа в один кластер.

        Уровни в пределах cluster_tolerance% от ценового диапазона считаются
        одним уровнем. Берётся средняя цена и максимальная сила.

        Args:
            levels: список найденных уровней.
            price:  текущая цена для расчёта допуска.

        Returns:
            Список кластеризованных уровней.
        """
        if not levels:
            return []

        tol  = price * self.cfg.cluster_tolerance
        used = set()
        result: list[Level] = []

        for i, lvl in enumerate(levels):
            if i in used:
                continue

            cluster = [lvl]
            used.add(i)

            for j in range(i + 1, len(levels)):
                if j in used:
                    continue
                other = levels[j]
                # Объединяем только уровни одного типа
                if (other.level_type == lvl.level_type and
                        abs(other.price - lvl.price) <= tol):
                    cluster.append(other)
                    used.add(j)

            # Кластер → один уровень: средняя цена, макс. сила
            avg_price   = float(np.mean([c.price for c in cluster]))
            best        = max(cluster, key=lambda c: c.strength)
            merged      = Level(
                price       = avg_price,
                level_type  = best.level_type,
                strength    = best.strength,
                touch_count = len(cluster),
                bar_index   = best.bar_index,
                is_mirror   = any(c.is_mirror for c in cluster),
            )
            result.append(merged)

        return result

    # ─── 4. Сила уровня ──────────────────────────────────────────────────────

    def level_strength(
        self,
        level:   Level,
        df:      pd.DataFrame,
        bar_idx: int,
    ) -> int:
        """
        Оценивает силу уровня по шкале 1–6 баллов.

        Система начисления баллов:
          +1 = базовый (воздушный уровень: БСУ + БПУ-1 + БПУ-2)
          +2 = исторический (≥2 касаний до текущего бара)
          +1 = многократный (≥4 касаний — очень сильный)
          +1 = круглое число (кратно 500 или 50 в зависимости от цены)
          +1 = длинный хвост на БСУ (wick > 2× тела бара)

          Зеркальный уровень (+5) — если level.is_mirror=True (устанавливается
          при обнаружении смены роли поддержка ↔ сопротивление).

        Args:
            level:   объект Level.
            df:      исторический DataFrame (до текущего бара).
            bar_idx: индекс последнего бара в df.

        Returns:
            Сила уровня от 1 до 6.
        """
        score = 1  # базовый балл — воздушный уровень

        # ── Исторические касания ─────────────────────────────────────────────
        history  = df.iloc[:bar_idx]
        touches  = self._count_touches(history, level.price, level.level_type)

        if touches >= 2:
            score += 2   # исторический уровень
        if touches >= 4:
            score += 1   # очень часто тестировался

        # ── Зеркальный уровень (поддержка стала сопротивлением или наоборот) ─
        if level.is_mirror:
            score += 5

        # ── Круглое число ────────────────────────────────────────────────────
        p = level.price
        if p >= 1_000:
            # Кратно 500 или 1000 с допуском 0.05%
            if (p % 500 < p * 0.0005) or (p % 1_000 < p * 0.0005):
                score += 1
        elif p >= 100:
            if (p % 50 < p * 0.001) or (p % 100 < p * 0.001):
                score += 1
        elif p >= 10:
            if p % 5 < p * 0.01:
                score += 1

        # ── Длинный хвост на БСУ ─────────────────────────────────────────────
        bsu_idx = level.bar_index
        if 0 <= bsu_idx < len(df):
            bar  = df.iloc[bsu_idx]
            body = abs(float(bar["close"]) - float(bar["open"]))
            wick = float(bar["high"]) - float(bar["low"])
            if body > 0 and wick / body >= 2.5:
                score += 1

        return min(score, 6)

    def _count_touches(
        self,
        df:         pd.DataFrame,
        price:      float,
        level_type: LevelType,
    ) -> int:
        """
        Считает количество касаний уровня в истории.

        Касание = бар, у которого high (для сопротивления) или
        low (для поддержки) попадает в допуск level_touch_threshold.
        """
        thr = price * self.cfg.level_touch_threshold
        if level_type == "resistance":
            mask = (df["high"] - price).abs() <= thr
        else:
            mask = (df["low"]  - price).abs() <= thr
        return int(mask.sum())

    def _detect_mirror_levels(self, levels: list[Level]) -> None:
        """
        Помечает зеркальные уровни: поддержка и сопротивление совпадают
        по цене в пределах cluster_tolerance.

        Зеркальный уровень — где роль (поддержка/сопротивление) поменялась.
        Изменяет is_mirror=True у найденных уровней.
        """
        supports   = [l for l in levels if l.level_type == "support"]
        resistances = [l for l in levels if l.level_type == "resistance"]
        ref_price  = levels[0].price if levels else 1.0
        tol        = ref_price * self.cfg.cluster_tolerance * 3  # чуть шире

        for sup in supports:
            for res in resistances:
                if abs(sup.price - res.price) <= tol:
                    sup.is_mirror = True
                    res.is_mirror = True

    # ─── 5. Определение тренда ───────────────────────────────────────────────

    def determine_trend(
        self,
        close:  float,
        levels: list[Level],
    ) -> Optional[Direction]:
        """
        Определяет зону торговли по Герчику.

        Правило:
          - Ближайший значимый уровень ниже цены — поддержка → LONG-зона.
          - Ближайший значимый уровень выше цены — сопротивление → SHORT-зона.

        При неоднозначности побеждает уровень с более высокой силой.

        Args:
            close:  текущая цена закрытия.
            levels: список активных уровней.

        Returns:
            "LONG" | "SHORT" | None если уровней нет.
        """
        if not levels:
            return None

        # Уровни выше и ниже текущей цены
        above = [l for l in levels if l.price > close]
        below = [l for l in levels if l.price < close]

        if not above and not below:
            return None

        # Ближайший снизу и сверху
        nearest_below = max(below, key=lambda l: l.price) if below else None
        nearest_above = min(above, key=lambda l: l.price) if above else None

        dist_below = (close - nearest_below.price) if nearest_below else float("inf")
        dist_above = (nearest_above.price - close) if nearest_above else float("inf")

        # Торгуем в направлении более близкого значимого уровня:
        # если поддержка ближе снизу → LONG-зона
        # если сопротивление ближе сверху → SHORT-зона
        if dist_below <= dist_above:
            return "LONG"
        else:
            return "SHORT"

    # ─── 6. Паттерн БСУ → БПУ-1 → БПУ-2 ────────────────────────────────────

    def find_bsu_bpu_pattern(
        self,
        df:          pd.DataFrame,
        level_price: float,
        level_type:  LevelType,
        bar_idx:     int,
    ) -> bool:
        """
        Ищет паттерн входа БСУ → БПУ-1 → БПУ-2 у уровня.

        Логика:
          bar_idx     → предполагаемый БПУ-2 (текущий бар)
          bar_idx - 1 → БПУ-1

        Для уровня поддержки (LONG):
          БПУ-1: low касается уровня (±thr), бар бычий (close > open),
                 закрывается выше уровня.
          БПУ-2: сразу за БПУ-1, low не пробивает уровень (не ниже),
                 бар бычий, close ≥ close БПУ-1.

        Для уровня сопротивления (SHORT):
          БПУ-1: high касается уровня (±thr), бар медвежий (close < open),
                 закрывается ниже уровня.
          БПУ-2: сразу за БПУ-1, high не пробивает уровень (не выше),
                 бар медвежий, close ≤ close БПУ-1.

        Args:
            df:          полный OHLCV DataFrame.
            level_price: цена уровня.
            level_type:  тип уровня.
            bar_idx:     индекс текущего бара (кандидат на БПУ-2).

        Returns:
            True если паттерн подтверждён.
        """
        if bar_idx < 2:
            return False

        thr  = level_price * self.cfg.level_touch_threshold
        bpu2 = df.iloc[bar_idx]
        bpu1 = df.iloc[bar_idx - 1]

        if level_type == "support":
            # БПУ-1: касается уровня снизу и отскакивает вверх
            bpu1_touches  = bpu1["low"] <= level_price + thr
            bpu1_bullish  = bpu1["close"] > bpu1["open"]
            bpu1_rebounds = bpu1["close"] > level_price

            # БПУ-2: подтверждает отскок, не пробивает уровень
            bpu2_no_break = bpu2["low"] >= level_price - thr
            bpu2_bullish  = bpu2["close"] > bpu2["open"]
            bpu2_higher   = bpu2["close"] >= bpu1["close"]

            return (
                bpu1_touches  and
                bpu1_bullish  and
                bpu1_rebounds and
                bpu2_no_break and
                bpu2_bullish  and
                bpu2_higher
            )

        else:  # resistance
            # БПУ-1: касается уровня сверху и отскакивает вниз
            bpu1_touches  = bpu1["high"] >= level_price - thr
            bpu1_bearish  = bpu1["close"] < bpu1["open"]
            bpu1_rebounds = bpu1["close"] < level_price

            # БПУ-2: подтверждает падение, не пробивает уровень
            bpu2_no_break = bpu2["high"] <= level_price + thr
            bpu2_bearish  = bpu2["close"] < bpu2["open"]
            bpu2_lower    = bpu2["close"] <= bpu1["close"]

            return (
                bpu1_touches  and
                bpu1_bearish  and
                bpu1_rebounds and
                bpu2_no_break and
                bpu2_bearish  and
                bpu2_lower
            )

    # ─── 7. Фильтр ATR ───────────────────────────────────────────────────────

    def check_atr_filter(
        self,
        current_price: float,
        open_price:    float,
        atr:           float,
        direction:     Direction,
    ) -> bool:
        """
        Фильтр Герчика: не входим если цена уже прошла ≥ 75% дневного ATR.

        Смысл: если рынок уже «выдохся» (прошёл большой диапазон), риск
        разворота высок — лучше пропустить сигнал.

        Args:
            current_price: текущая цена.
            open_price:    цена открытия периода (дня/бара).
            atr:           значение ATR.
            direction:     направление сигнала.

        Returns:
            True если фильтр пройден (цена прошла < 75% ATR → можно входить).
        """
        if atr <= 0:
            return True  # нет данных → не фильтруем

        move      = abs(current_price - open_price)
        threshold = atr * self.cfg.atr_entry_threshold

        return move < threshold

    # ─── 8. Размер позиции ───────────────────────────────────────────────────

    def position_size(
        self,
        capital:   float,
        entry:     float,
        stop_loss: float,
    ) -> float:
        """
        Рассчитывает размер позиции через фиксированный % риска.

        Формула:
            size = (capital × risk_per_trade) / |entry − stop_loss|

        Пример:
            capital=1000, risk=1%, entry=50000, sl=49000
            → size = 10 / 1000 = 0.01 BTC

        Args:
            capital:   текущий капитал в USDT.
            entry:     цена входа (ТВХ).
            stop_loss: цена стоп-лосса.

        Returns:
            Размер позиции в единицах базовой валюты.
        """
        stop_dist = abs(entry - stop_loss)
        if stop_dist <= 0:
            return 0.0
        risk_usdt = capital * self.cfg.risk_per_trade
        return round(risk_usdt / stop_dist, 8)

    # ─── 9. Симуляция исхода сделки ──────────────────────────────────────────

    def _simulate_trade(
        self,
        future_bars: pd.DataFrame,
        signal:      TradeSignal,
        size:        float,
    ) -> dict:
        """
        Симулирует исход сделки на следующих барах.

        Логика Герчика (выход по частям):
          1. Если цена сначала бьёт SL → убыток 100% позиции.
          2. Если цена достигает TP1 → закрываем 55%, SL переносим в БУ (entry).
          3. После TP1:
             а. Достигнут TP2 → профит оставшихся 45%.
             б. Бьёт обновлённый SL (entry = БУ) → закрываем 45% на безубытке (PnL ≈ 0).
          4. Истёк лимит баров (TIMEOUT) → закрываем по текущей цене.

        Args:
            future_bars: DataFrame будущих баров для симуляции.
            signal:      объект TradeSignal.
            size:        размер позиции.

        Returns:
            dict: {result, pnl, closed_bars}
        """
        direction = signal.direction
        entry     = signal.entry
        sl        = signal.sl
        tp1       = signal.tp1
        tp2       = signal.tp2

        risk_usdt = abs(entry - sl) * size  # базовый риск

        tp1_hit = False
        pnl     = 0.0
        # После TP1 стоп переносится в безубыток (entry)
        active_sl = sl

        for bar_num, (_, bar) in enumerate(future_bars.iterrows()):
            h = float(bar["high"])
            l = float(bar["low"])

            if direction == "LONG":
                if not tp1_hit:
                    # Проверяем SL раньше TP (консервативно)
                    if l <= active_sl:
                        return {
                            "result": "SL",
                            "pnl": round(-risk_usdt, 2),
                            "closed_bars": bar_num + 1,
                        }
                    if h >= tp1:
                        # TP1 достигнут: закрываем 55%, остаток на БУ
                        pnl      += (tp1 - entry) * size * self.cfg.tp1_close_pct
                        tp1_hit   = True
                        active_sl = entry  # безубыток
                else:
                    if l <= active_sl:
                        # Остаток закрываем по безубытку → +0 от оставшихся
                        pnl += (active_sl - entry) * size * self.cfg.tp2_close_pct
                        return {
                            "result": "TP1+BE",
                            "pnl": round(pnl, 2),
                            "closed_bars": bar_num + 1,
                        }
                    if h >= tp2:
                        pnl += (tp2 - entry) * size * self.cfg.tp2_close_pct
                        return {
                            "result": "TP2",
                            "pnl": round(pnl, 2),
                            "closed_bars": bar_num + 1,
                        }

            else:  # SHORT
                if not tp1_hit:
                    if h >= active_sl:
                        return {
                            "result": "SL",
                            "pnl": round(-risk_usdt, 2),
                            "closed_bars": bar_num + 1,
                        }
                    if l <= tp1:
                        pnl      += (entry - tp1) * size * self.cfg.tp1_close_pct
                        tp1_hit   = True
                        active_sl = entry
                else:
                    if h >= active_sl:
                        pnl += (entry - active_sl) * size * self.cfg.tp2_close_pct
                        return {
                            "result": "TP1+BE",
                            "pnl": round(pnl, 2),
                            "closed_bars": bar_num + 1,
                        }
                    if l <= tp2:
                        pnl += (entry - tp2) * size * self.cfg.tp2_close_pct
                        return {
                            "result": "TP2",
                            "pnl": round(pnl, 2),
                            "closed_bars": bar_num + 1,
                        }

        # Таймаут — закрываем по последней цене
        last = float(future_bars["close"].iloc[-1]) if len(future_bars) > 0 else entry
        if direction == "LONG":
            close_pnl = (last - entry) * size
        else:
            close_pnl = (entry - last) * size

        if tp1_hit:
            pnl += close_pnl * self.cfg.tp2_close_pct
        else:
            pnl += close_pnl

        return {
            "result": "TIMEOUT",
            "pnl": round(pnl, 2),
            "closed_bars": len(future_bars),
        }

    # ─── 10. Полный бэктест ──────────────────────────────────────────────────

    def backtest(
        self,
        df:              pd.DataFrame,
        initial_capital: float = 1000.0,
        alerter:         Optional[TelegramAlerter] = None,
    ) -> pd.DataFrame:
        """
        Полный бэктест стратегии Герчика на исторических данных.

        Алгоритм на каждом баре:
          1. Вычисляем ATR текущего периода.
          2. Находим и кластеризуем уровни в lookback-окне.
          3. Помечаем зеркальные уровни.
          4. Обновляем силу каждого уровня.
          5. Определяем тренд (LONG/SHORT зону).
          6. Для ближайшего кандидатного уровня:
             а. Проверяем паттерн БПУ-1 → БПУ-2.
             б. Применяем ATR-фильтр (≤75% пройдено).
             в. Проверяем запас хода до следующего уровня (≥3R).
             г. Рассчитываем ТВХ, стоп, TP1, TP2.
             д. Определяем размер позиции (1% риска).
             е. Симулируем исход на следующих 50 барах.
          7. Применяем дневные лимиты.
          8. Записываем сделку в лог.

        Args:
            df:               OHLCV DataFrame.
            initial_capital:  начальный капитал в USDT.
            alerter:          TelegramAlerter (опционально).

        Returns:
            pd.DataFrame со всеми сделками.
        """
        cfg          = self.cfg
        atr_series   = self.compute_atr(df)
        capital      = initial_capital
        peak_capital = initial_capital
        max_drawdown = 0.0
        trades: list[dict] = []

        # Дневные счётчики
        daily_losses    = 0
        daily_profit    = 0.0
        current_day_str = ""

        log.info(
            f"Бэктест старт: {len(df)} баров | "
            f"Капитал: ${initial_capital:,.2f} | "
            f"Символ: {self.symbol}"
        )

        min_bar = cfg.level_lookback + cfg.pivot_strength + 2
        max_bar = len(df) - 52   # оставляем 52 бара на симуляцию

        for i in range(min_bar, max_bar):
            bar   = df.iloc[i]
            close = float(bar["close"])
            atr   = float(atr_series.iloc[i])
            ts    = str(df.index[i])[:10]

            # ── Сброс дневного счётчика ─────────────────────────────────
            if ts != current_day_str:
                current_day_str = ts
                daily_losses    = 0
                daily_profit    = 0.0

            # ── Дневной лимит убытков ────────────────────────────────────
            if daily_losses >= cfg.max_daily_losses:
                continue

            # ── Поиск и кластеризация уровней ───────────────────────────
            window    = df.iloc[i - cfg.level_lookback: i]
            raw_lvls  = self.find_levels(window)
            if not raw_lvls:
                continue

            levels = self.cluster_levels(raw_lvls, close)
            if not levels:
                continue

            # Помечаем зеркальные уровни
            self._detect_mirror_levels(levels)

            # Обновляем силу каждого уровня
            for lvl in levels:
                lvl.strength = self.level_strength(lvl, window, len(window) - 1)

            # ── Тренд ───────────────────────────────────────────────────
            trend = self.determine_trend(close, levels)
            if trend is None:
                continue

            # ── Отбор кандидатных уровней по тренду ────────────────────
            # Торгуем только от уровня, соответствующего направлению
            prox_thr = cfg.level_touch_threshold * 10   # 2% — рядом с уровнем
            if trend == "LONG":
                candidates = [
                    l for l in levels
                    if l.level_type == "support"
                    and 0 <= (close - l.price) / max(l.price, 1e-9) <= prox_thr
                ]
            else:
                candidates = [
                    l for l in levels
                    if l.level_type == "resistance"
                    and 0 <= (l.price - close) / max(l.price, 1e-9) <= prox_thr
                ]

            if not candidates:
                continue

            # Берём наиболее сильный ближайший уровень
            best = max(
                candidates,
                key=lambda l: (l.strength, -abs(close - l.price)),
            )

            # ── Паттерн БПУ-1 → БПУ-2 ───────────────────────────────────
            if not self.find_bsu_bpu_pattern(df, best.price, best.level_type, i):
                continue

            # ── ATR-фильтр ───────────────────────────────────────────────
            open_price = float(bar["open"])
            if not self.check_atr_filter(close, open_price, atr, trend):
                continue

            # ── Расчёт ТВХ, стопа, TP ────────────────────────────────────
            level_price = best.price
            if trend == "LONG":
                stop_dist = max(close - level_price, atr * 0.05)
                lft       = stop_dist * cfg.lft_pct
                entry     = level_price + lft
                sl        = level_price - lft
                tp1       = entry + stop_dist * cfg.tp1_rr
                tp2       = entry + stop_dist * cfg.tp2_rr
            else:
                stop_dist = max(level_price - close, atr * 0.05)
                lft       = stop_dist * cfg.lft_pct
                entry     = level_price - lft
                sl        = level_price + lft
                tp1       = entry - stop_dist * cfg.tp1_rr
                tp2       = entry - stop_dist * cfg.tp2_rr

            # ── Запас хода до противоположного уровня (≥ 3R) ─────────────
            opp_type = "resistance" if trend == "LONG" else "support"
            opp_lvls = [
                l for l in levels
                if l.level_type == opp_type and (
                    (trend == "LONG"  and l.price > entry) or
                    (trend == "SHORT" and l.price < entry)
                )
            ]
            if opp_lvls:
                if trend == "LONG":
                    nearest_opp = min(opp_lvls, key=lambda l: l.price)
                    room        = nearest_opp.price - entry
                else:
                    nearest_opp = max(opp_lvls, key=lambda l: l.price)
                    room        = entry - nearest_opp.price

                risk = abs(entry - sl)
                if risk > 0 and room / risk < cfg.min_rr_ratio:
                    continue   # недостаточно места

            # ── Размер позиции ────────────────────────────────────────────
            size = self.position_size(capital, entry, sl)
            if size <= 0:
                continue

            rr_actual = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0.0

            # ── Симуляция сделки ─────────────────────────────────────────
            future = df.iloc[i + 1: i + 51]
            if len(future) == 0:
                continue

            signal = TradeSignal(
                direction     = trend,
                symbol        = self.symbol,
                entry         = round(entry, 6),
                sl            = round(sl,    6),
                tp1           = round(tp1,   6),
                tp2           = round(tp2,   6),
                level         = best,
                rr            = round(rr_actual, 2),
                position_size = size,
                bar_index     = i,
                timestamp     = ts,
            )

            sim    = self._simulate_trade(future, signal, size)
            pnl    = sim["pnl"]
            result = sim["result"]
            capital += pnl

            # ── Дневные лимиты ────────────────────────────────────────────
            if pnl < 0:
                daily_losses += 1
            else:
                # Если убыток ≥ 30% дневного профита → стоп
                if daily_profit > 0 and abs(pnl) >= daily_profit * cfg.max_dd_pct_of_daily:
                    daily_losses = cfg.max_daily_losses
            daily_profit += max(pnl, 0)

            # ── Просадка ─────────────────────────────────────────────────
            if capital > peak_capital:
                peak_capital = capital
            dd = (peak_capital - capital) / peak_capital * 100 if peak_capital > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd

            trades.append({
                "timestamp":      ts,
                "direction":      trend,
                "entry":          round(entry,    4),
                "sl":             round(sl,       4),
                "tp1":            round(tp1,      4),
                "tp2":            round(tp2,      4),
                "rr":             round(rr_actual, 2),
                "level_strength": best.strength,
                "level_type":     best.level_type,
                "size":           round(size, 6),
                "pnl":            round(pnl,  2),
                "result":         result,
                "capital":        round(capital, 2),
                "drawdown_pct":   round(dd, 2),
            })

            # ── Telegram-алёрт ────────────────────────────────────────────
            if alerter:
                alerter.send_signal_sync({
                    "direction":      trend,
                    "symbol":         self.symbol,
                    "entry":          signal.entry,
                    "sl":             signal.sl,
                    "tp1":            signal.tp1,
                    "tp2":            signal.tp2,
                    "level_strength": best.strength,
                    "rr":             rr_actual,
                    "level_type":     best.level_type,
                    "timestamp":      ts,
                })

        result_df = pd.DataFrame(trades)
        log.info(
            f"Бэктест завершён: {len(trades)} сделок | "
            f"Итог: ${capital:,.2f} | "
            f"Макс. просадка: {max_drawdown:.1f}%"
        )
        return result_df


# ══════════════════════════════════════════════════════════════════════════════
# СТАТИСТИКА
# ══════════════════════════════════════════════════════════════════════════════

def print_stats(trades: pd.DataFrame, initial_capital: float) -> None:
    """
    Выводит сводную статистику бэктеста в консоль.

    Args:
        trades:          DataFrame со всеми сделками.
        initial_capital: начальный капитал.
    """
    if trades.empty:
        print("Сделок не найдено. Попробуй уменьшить pivot_strength или увеличить limit.")
        return

    total   = len(trades)
    wins    = trades[trades["pnl"] > 0]
    losses  = trades[trades["pnl"] <= 0]
    wr      = len(wins) / total * 100 if total else 0
    tot_pnl = trades["pnl"].sum()
    final   = trades["capital"].iloc[-1]
    max_dd  = trades["drawdown_pct"].max()
    avg_win = wins["pnl"].mean()   if len(wins)   > 0 else 0.0
    avg_los = losses["pnl"].mean() if len(losses) > 0 else 0.0

    pf_num  = wins["pnl"].sum()
    pf_den  = abs(losses["pnl"].sum())
    pf      = pf_num / pf_den if pf_den > 0 else float("inf")

    by_result = trades["result"].value_counts()

    line = "═" * 62
    print(f"\n{line}")
    print("   РЕЗУЛЬТАТЫ БЭКТЕСТА — СТРАТЕГИЯ ГЕРЧИКА")
    print(line)
    print(f"   Символ:               {trades.get('symbol', ['—'])[0] if 'symbol' in trades else '—'}")
    print(f"   Всего сделок:         {total}")
    print(f"   Win Rate:             {wr:.1f}%")
    print(f"   Profit Factor:        {pf:.2f}")
    print(f"   Начальный капитал:    ${initial_capital:,.2f}")
    print(f"   Итоговый капитал:     ${final:,.2f}")
    print(f"   Итого PnL:            ${tot_pnl:+,.2f}  ({tot_pnl / initial_capital * 100:+.1f}%)")
    print(f"   Макс. просадка:       {max_dd:.1f}%")
    print(f"   Средний выигрыш:      ${avg_win:.2f}")
    print(f"   Средний проигрыш:     ${avg_los:.2f}")
    print(line)
    print("   Распределение результатов:")
    for res, cnt in by_result.items():
        bar_w = int(cnt / total * 30)
        print(f"     {res:<12} {cnt:>4}  {'█' * bar_w}")
    print(line)
    print()

    # Таблица сделок
    cols = [
        "timestamp", "direction", "entry", "sl",
        "tp1", "tp2", "pnl", "result", "capital", "level_strength",
    ]
    print(trades[cols].to_string(index=False))
    print()


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ── Параметры ────────────────────────────────────────────────────────────
    SYMBOL          = "BTC/USDT"
    TIMEFRAME       = "1d"       # D1 для поиска уровней
    LIMIT           = 500        # баров истории
    EXCHANGE        = "bybit"    # bybit | bingx | okx
    INITIAL_CAPITAL = 1000.0     # стартовый капитал в USDT

    # ── Загрузка данных ───────────────────────────────────────────────────────
    try:
        df = load_ohlcv(
            symbol        = SYMBOL,
            timeframe     = TIMEFRAME,
            limit         = LIMIT,
            exchange_name = EXCHANGE,
        )
    except Exception as e:
        log.error(f"Не удалось загрузить данные: {e}")
        sys.exit(1)

    # ── Конфиг стратегии ─────────────────────────────────────────────────────
    config = GerchikConfig(
        atr_period           = 14,
        atr_entry_threshold  = 0.75,
        level_lookback       = 50,
        pivot_strength       = 5,
        level_touch_threshold= 0.002,
        cluster_tolerance    = 0.003,
        min_rr_ratio         = 3.0,
        risk_per_trade       = 0.01,
        lft_pct              = 0.20,
        tp1_rr               = 3.0,
        tp2_rr               = 4.0,
        tp1_close_pct        = 0.55,
        tp2_close_pct        = 0.45,
        max_daily_losses     = 3,
        max_dd_pct_of_daily  = 0.30,
    )

    strategy = GerchikStrategy(config=config, symbol=SYMBOL)

    # ── Telegram-алёртер (опционально) ───────────────────────────────────────
    alerter: Optional[TelegramAlerter] = None
    if os.environ.get("GERCHIK_BOT_TOKEN") and os.environ.get("GERCHIK_CHAT_ID"):
        try:
            alerter = TelegramAlerter()
            log.info("Telegram-алёртер подключён.")
        except Exception as e:
            log.warning(f"Telegram не инициализирован: {e}")

    # ── Бэктест ───────────────────────────────────────────────────────────────
    trades = strategy.backtest(
        df              = df,
        initial_capital = INITIAL_CAPITAL,
        alerter         = alerter,
    )

    # ── Результаты ───────────────────────────────────────────────────────────
    print_stats(trades, INITIAL_CAPITAL)

    # Сохраняем в CSV
    if not trades.empty:
        csv_path = "gerchik_backtest_results.csv"
        trades.to_csv(csv_path, index=False)
        log.info(f"Результаты сохранены: {csv_path}")
