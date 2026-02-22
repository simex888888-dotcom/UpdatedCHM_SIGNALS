"""
╔══════════════════════════════════════════════════════════════╗
║                  НАСТРОЙКИ CHM BREAKER BOT                  ║
║              Редактируй только этот файл!                   ║
╚══════════════════════════════════════════════════════════════╝
"""


class Config:

    # ═══════════════════════════════════════════════════════
    #  🔑 TELEGRAM — ЗАПОЛНИ ЭТИ ДВЕ СТРОКИ!
    # ═══════════════════════════════════════════════════════

    # Токен от @BotFather (выглядит так: 7412589631:AAHxyz...)
    TELEGRAM_TOKEN = "ВАШ_ТОКЕН_СЮДА"

    # Твой личный Chat ID — узнай через браузер:
    # https://api.telegram.org/botВАШ_ТОКЕН/getUpdates
    # Найди "chat":{"id": ЧИСЛО} — вот это число и вставь
    TELEGRAM_CHAT_ID = "ВАШ_CHAT_ID_СЮДА"


    # ═══════════════════════════════════════════════════════
    #  📊 РЕЖИМ ВЫБОРА МОНЕТ
    # ═══════════════════════════════════════════════════════

    COIN_MODE            = "auto"
    AUTO_MIN_VOLUME_USDT = 1_000_000
    AUTO_MAX_COINS       = 0
    AUTO_BLACKLIST       = [
        "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT",
        "EURUSDT",  "GBPUSDT",  "RUBUSDT",  "FDUSDUSDT",
    ]
    COINS = [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    ]


    # ═══════════════════════════════════════════════════════
    #  ⏱  ТАЙМФРЕЙМ И ИНТЕРВАЛ
    # ═══════════════════════════════════════════════════════

    TIMEFRAME     = "1h"
    SCAN_INTERVAL = 3600
    CHUNK_SIZE    = 8


    # ═══════════════════════════════════════════════════════
    #  ⚙️  ПАРАМЕТРЫ ИНДИКАТОРА
    # ═══════════════════════════════════════════════════════

    PIVOT_STRENGTH  = 7
    ATR_PERIOD      = 14
    ATR_MULT        = 1.0
    MAX_RISK_PCT    = 1.5
    EMA_FAST        = 50
    EMA_SLOW        = 200
    RSI_PERIOD      = 14
    RSI_OB          = 65
    RSI_OS          = 35
    VOL_MULT        = 1.2
    VOL_LEN         = 20
    MAX_LEVEL_AGE   = 100
    MAX_RETEST_BARS = 30
    COOLDOWN_BARS   = 10
    ZONE_BUFFER     = 0.3
    TP1_RR          = 0.8
    TP2_RR          = 1.5
    TP3_RR          = 2.5
    HTF_TIMEFRAME   = "1d"
    HTF_EMA_PERIOD  = 50


    # ═══════════════════════════════════════════════════════
    #  🔔 ФИЛЬТРЫ И УВЕДОМЛЕНИЯ
    # ═══════════════════════════════════════════════════════

    USE_RSI_FILTER             = True
    USE_VOLUME_FILTER          = True
    USE_PATTERN_FILTER         = True
    USE_HTF_FILTER             = True
    MIN_SIGNAL_QUALITY         = 3
    NOTIFY_ON_BREAKOUT         = True
    NOTIFY_ON_SIGNAL           = True
    MAX_NOTIFICATIONS_PER_HOUR = 30
