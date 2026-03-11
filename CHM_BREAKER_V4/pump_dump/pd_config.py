"""Константы модуля Памп/Дамп."""

import os

# ── BingX API ────────────────────────────────────────────────────────────────
BINGX_WS_URL       = "wss://open-api-ws.bingx.com/market"
BINGX_REST_FUTURES = "https://open-api.bingx.com/openApi/swap/v2"
BINGX_API_KEY      = os.getenv("BINGX_API_KEY",    "p1hIr0pmP9gVqO3rHeWVPjIjcdkeRlHAuFTjob5kkV9bc5ZkXxS20a0OrnPHpMkgXQoCETk49IpAfrfK52JA")
BINGX_SECRET_KEY   = os.getenv("BINGX_SECRET_KEY", "cw7ZMgUeAKiKXjOH5Dl862AnjdXTCXYKnTh3zJxSLXA1DZOKSKtvATzK2OIHF3fxEEuulNtk27cv2KRreg")

# ── Мониторинг ────────────────────────────────────────────────────────────────
TOP_COINS_COUNT     = 50          # топ монет по объёму (50 для стабильности WS)
MIN_VOLUME_24H_USDT = 500_000     # минимальный 24h объём для мониторинга
CANDLE_BUFFER       = 200         # свечей в буфере на монету
WS_RECONNECT_MAX    = 60          # максимальная задержка реконнекта (сек)
HEARTBEAT_INTERVAL  = 5           # интервал pong (сек)
FUNDING_FETCH_EVERY = 300         # интервал опроса funding rate (сек)
OI_FETCH_EVERY      = 60          # интервал опроса Open Interest (сек)

# ── Детектор аномалий (двойное кондиционирование) ────────────────────────────
EWMA_SPAN           = 50          # span для EWMA baseline
ZSCORE_THRESHOLD    = 2.0         # Z-score порог аномалии объёма (было 3.5 — слишком редко)
DOUBLE_COND_MEAN_M  = 1.15        # объём > mean_ewma * 1.15 (было 1.30)
DOUBLE_COND_MAX_M   = 0.45        # объём > rolling_max * 0.45 (было 0.60)

# ── Ценовой спайк (оба условия обязательны) ──────────────────────────────────
PRICE_SPIKE_1M      = 0.02        # >= 2% за 1 свечу
PRICE_SPIKE_3M      = 0.035       # >= 3.5% за 3 свечи

# ── Стакан ────────────────────────────────────────────────────────────────────
IMBALANCE_PUMP      = 0.68        # bid_vol / total > 68% → памп
IMBALANCE_DUMP      = 0.32        # bid_vol / total < 32% → дамп
SPREAD_WIDENING_M   = 2.5         # спред > baseline * 2.5
WALL_PCT            = 0.07        # заявка > 7% суммарного объёма = стена
SPOOF_SECS          = 3           # заявка < 3 сек = спуфинг

# ── Скрытые сигналы ───────────────────────────────────────────────────────────
FUNDING_PUMP_THR    = -0.0005     # funding < -0.05% → шорты перегружены
FUNDING_DUMP_THR    = +0.0010     # funding > +0.10% → лонги перегружены
FUNDING_DELTA_THR   = 0.0003      # резкое изменение funding (+0.10 к score)
OI_PUMP_PCT         = 0.03        # OI вырос > 3% за 10 мин + цена боковик
OI_DUMP_PCT         = 0.05        # OI упал > 5% за 5 мин + цена вниз
LONG_SHORT_PUMP     = 0.40        # L/S ratio < 0.4 → short squeeze
LONG_SHORT_DUMP     = 3.50        # L/S ratio > 3.5 → long squeeze

# ── Агрегатор сигналов ────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE    = 50          # минимальный % для отправки алерта (было 70 — с 4 слоями max=60%, недостижимо)
MIN_ACTIVE_LAYERS   = 3           # минимум 3 из 8 слоёв активны (было 4)
ANTI_SPAM_MINUTES   = 15          # не повторять сигнал по монете N минут (было 20)
MAX_ATR_PCT         = 5.0         # не сигналить если памп уже идёт

# ── Веса слоёв ────────────────────────────────────────────────────────────────
LAYER_WEIGHTS = {
    "volume":    0.15,
    "price":     0.10,
    "cvd":       0.15,
    "orderbook": 0.10,
    "spread":    0.10,
    "funding":   0.15,
    "oi":        0.10,
    "ml":        0.15,
}

# ── ML модель ─────────────────────────────────────────────────────────────────
ML_MODEL_PATH        = "pump_dump/model.pkl"
ML_RETRAIN_DAYS      = 7
ML_MIN_SAMPLES       = 500        # минимум примеров для обучения
ML_PUMP_THRESHOLD    = 0.70       # порог вероятности памп класса
ML_PRECISION_MIN     = 0.80       # минимальная precision для использования модели

# ── Дефолтный порог пользователя ─────────────────────────────────────────────
DEFAULT_USER_THRESHOLD = 70       # %
