"""
╔══════════════════════════════════════════════════════════════╗
║        CHM BREAKER BOT MID — 50-500 пользователей           ║
║                      config.py                               ║
╚══════════════════════════════════════════════════════════════╝

  СТЕК:
    SQLite   — встроен в Python, не требует установки
    RAM кэш  — свечи в памяти с TTL, не нужен Redis
    asyncio  — 6 воркеров параллельного анализа

  ЗАПУСК:
    pip install aiogram aiohttp pandas numpy certifi
    python3 bot.py
"""

import os


class Config:

    # ════════════════════════════════════════════════
    #  🔑 TELEGRAM
    # ════════════════════════════════════════════════

    TELEGRAM_TOKEN = os.getenv("BOT_TOKEN_CHM")


    # Твой Telegram ID — станешь администратором
    # Узнать: написать @userinfobot
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "445677777,705020259,7107654772").split(",")]

    # ════════════════════════════════════════════════
    #  🗄  SQLITE — путь к БД (persistent volume)
    # ════════════════════════════════════════════════

    # Для сохранения данных после редеплоя:
    # Docker: монтировать /data как volume, задать DB_PATH=/data/chm_bot.db
    # По умолчанию — рядом со скриптом (не зависит от рабочей директории)
    DB_PATH = os.getenv(
        "DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "chm_bot.db"),
    )


    # ════════════════════════════════════════════════
    #  ⚙️  ПРОИЗВОДИТЕЛЬНОСТЬ
    # ════════════════════════════════════════════════

    # Параллельных запросов к OKX API
    # OKX лимит ~20 req/sec → ставим 12 для запаса
    API_CONCURRENCY = 12

    # Воркеров анализа (для 50-500 юзеров хватает 6)
    SCAN_WORKERS    = 6

    # Монет за один батч запросов
    CHUNK_SIZE      = 8

    # Пауза между батчами (защита от rate limit)
    CHUNK_SLEEP     = 0.07

    # Пауза главного цикла после каждого прохода
    SCAN_LOOP_SLEEP = 20

    # ════════════════════════════════════════════════
    #  🕐 КЭШИ СВЕЧЕЙ (in-memory, секунды)
    # ════════════════════════════════════════════════

    CACHE_TTL = {
        "1m":  55,    "5m":  270,  "15m": 870,
        "30m": 1770,  "1h":  3570, "4h":  14370,
        "1d":  85000, "1D":  85000,"1H":  3570,
        "4H":  14370,
    }

    # Максимум монет в кэше (защита от утечки памяти)
    CACHE_MAX_SYMBOLS = 300


    # ════════════════════════════════════════════════
    #  💳 ПОДПИСКА — ЦЕНЫ И ОПЛАТА
    # ════════════════════════════════════════════════

    # Адрес для оплаты (BEP20 / BSC)
    PAYMENT_ADDRESS = "0xb5116aa7d7a20d7c45a8a5ff10bc1d86437df985"
    PAYMENT_NETWORK = "BEP20 (BSC)"

    # Только БОТ
    BOT_PRICE_30    = "70$"
    BOT_PRICE_90    = "150$"
    BOT_PRICE_365   = "330$"

    # БОТ + ИНДИКАТОР на TradingView
    FULL_PRICE_30   = "90$"
    FULL_PRICE_90   = "230$"
    FULL_PRICE_365  = "630$"

    # Контакт администратора
    ADMIN_CONTACT   = "@crypto_chm"
    PAYMENT_INFO    = "@crypto_chm"

    # ════════════════════════════════════════════════
    #  📊 МОНЕТЫ
    # ════════════════════════════════════════════════

    AUTO_MIN_VOLUME_USDT = 1_000_000
    AUTO_BLACKLIST = [
        "USDC-USDT-SWAP", "BUSD-USDT-SWAP", "TUSD-USDT-SWAP",
        "FDUSD-USDT-SWAP", "DAI-USDT-SWAP",
    ]
    COINS_CACHE_HOURS = 6


    # ════════════════════════════════════════════════
    #  📋 ДЕФОЛТНЫЕ НАСТРОЙКИ НОВОГО ПОЛЬЗОВАТЕЛЯ
    # ════════════════════════════════════════════════

    D_TIMEFRAME    = "1h"
    D_INTERVAL     = 3600
    D_PIVOT        = 7
    D_ATR_PERIOD   = 14
    D_ATR_MULT     = 1.0
    D_MAX_RISK     = 1.5
    D_EMA_FAST     = 50
    D_EMA_SLOW     = 200
    D_RSI_PERIOD   = 14
    D_RSI_OB       = 65
    D_RSI_OS       = 35
    D_VOL_MULT     = 1.0
    D_VOL_LEN      = 20
    D_LEVEL_AGE    = 100
    D_RETEST_BARS  = 30
    D_COOLDOWN     = 5
    D_ZONE_BUF     = 0.3
    D_ZONE_PCT     = 0.7    # Ширина зоны уровня в % от цены (рекомендуется 0.7%)
    D_MAX_DIST_PCT = 1.5    # Макс. дистанция до уровня в % (если дальше — не торгуем)
    D_MIN_RR       = 2.0    # Минимальный R:R (строгое требование протокола)
    D_MAX_TESTS    = 4      # Макс. тестов уровня до пропуска сигнала (ожидается пробой)
    D_TP1          = 2.0    # TP1 fallback R:R (если нет противоположного уровня)
    D_TP2          = 3.0
    D_TP3          = 4.5
    D_HTF_EMA      = 50
    D_MIN_QUALITY  = 2
    D_MIN_VOL_USDT = 1_000_000
    D_USE_RSI      = True
    D_USE_VOLUME   = True
    D_USE_PATTERN  = False
    D_USE_HTF      = False
    D_NOTIFY_SIG   = True
    D_NOTIFY_BRK   = False
