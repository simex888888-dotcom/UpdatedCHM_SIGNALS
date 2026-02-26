"""
keyboards.py — Унифицированный интерфейс CHM BREAKER v5
Оптимизировано: исправлены опечатки, добавлены недостающие функции меню.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from user_manager import UserSettings, TradeCfg

# ── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────

def _btn(text: str, cb: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data=cb)]

def _back(cb: str = "back_main") -> list:
    return [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)]

def _noop(text: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data="noop")]

def _check(v: bool) -> str:
    return "✅" if v else "❌"

# ── ТРЕНД И ГЛАВНОЕ МЕНЮ ─────────────────────────────

def trend_text(trend: dict) -> str:
    if not trend:
        return "🌍 <b>Глобальный тренд:</b> загрузка...\n"
    
    res = "📊 <b>Глобальный анализ рынка:</b>\n"
    for coin in ["BTC", "ETH"]:
        data = trend.get(coin, {})
        res += f"<b>{coin}:</b> "
        res += f"1H {data.get('h1_emoji', '⚪')} | "
        res += f"4H {data.get('h4_emoji', '⚪')} | "
        res += f"1D {data.get('d1_emoji', '⚪')}\n"
    return res

def kb_main(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── МОНИТОРИНГ ──────────────────────────────"),
        [
            InlineKeyboardButton(text=_check(user.long_active) + " LONG", callback_data="toggle_long"),
            InlineKeyboardButton(text=_check(user.short_active) + " SHORT", callback_data="toggle_short")
        ],
        _btn("📈 Статистика профита", "my_stats"),
        _noop("── НАСТРОЙКИ РЕЖИМОВ ───────────────────────"),
        [
            InlineKeyboardButton(text="🔵 LONG", callback_data="menu_settings_long"),
            InlineKeyboardButton(text="🔴 SHORT", callback_data="menu_settings_short"),
            InlineKeyboardButton(text="🟣 ОБА", callback_data="menu_settings_shared")
        ],
        _noop("── ДОПОЛНИТЕЛЬНО ───────────────────────────"),
        [
            InlineKeyboardButton(text="🔔 Уведомления", callback_data="menu_notify"),
            InlineKeyboardButton(text="💎 Подписка", callback_data="menu_sub")
        ]
    ])

# ── УНИФИЦИРОВАННОЕ МЕНЮ НАСТРОЕК ────────────────────

def kb_settings_unified(user: UserSettings, direction: str) -> InlineKeyboardMarkup:
    # Определяем, какой конфиг показывать
    if direction == "long":
        cfg = user.get_long_cfg()
    elif direction == "short":
        cfg = user.get_short_cfg()
    else:
        cfg = user # shared 
        
    pfx = f"set_{direction}_" # Префикс для колбэков

    return InlineKeyboardMarkup(inline_keyboard=[
        _noop(f"⚙️ {direction.upper()} ПАРАМЕТРЫ"),
        [
            InlineKeyboardButton(text=f"⏳ ТФ: {cfg.timeframe}", callback_data=f"{pfx}tf"),
            InlineKeyboardButton(text=f"⏲ Инт: {cfg.scan_interval//60}м", callback_data=f"{pfx}int")
        ],
        [
            InlineKeyboardButton(text=f"📏 Pivot: {cfg.pivot_strength}", callback_data=f"{pfx}pivot"),
            InlineKeyboardButton(text=f"🌐 Зона: {cfg.zone_buffer}%", callback_data=f"{pfx}zone")
        ],
        [
            InlineKeyboardButton(text=f"📉 EMA {cfg.ema_slow}", callback_data=f"{pfx}ema"),
            InlineKeyboardButton(text=f"📊 RSI {cfg.rsi_period}", callback_data=f"{pfx}rsi")
        ],
        [
            InlineKeyboardButton(text=f"🛡 SL: {cfg.sl_atr_mult}x", callback_data=f"{pfx}sl"), # Исправлено ffx на pfx
            InlineKeyboardButton(text=f"🎯 TP (RR)", callback_data=f"{pfx}tp")
        ],
        _btn("❓ Справка по настройкам", "help_settings"),
        _back("back_main")
    ])

# ── ВСПОМОГАТЕЛЬНЫЕ МЕНЮ ВЫБОРА ──────────────────────

def kb_timeframes(direction: str, current: str) -> InlineKeyboardMarkup:
    tfs = ["5m", "15m", "30m", "1h", "4h"]
    rows = []
    for tf in tfs:
        mark = "◉ " if tf == current else "○ "
        rows.append(InlineKeyboardButton(text=f"{mark}{tf}", callback_data=f"save_{direction}_tf_{tf}"))
    
    keyboard = [rows[i:i + 3] for i in range(0, len(rows), 3)]
    keyboard.append(_back(f"menu_settings_{direction}"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def kb_intervals(direction: str, current: int) -> InlineKeyboardMarkup:
    ints = [("1м", 60), ("5м", 300), ("15м", 900), ("1ч", 3600)]
    rows = []
    for label, val in ints:
        mark = "◉ " if val == current else "○ "
        rows.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"save_{direction}_int_{val}"))
    
    keyboard = [rows[i:i + 2] for i in range(0, len(rows), 2)]
    keyboard.append(_back(f"menu_settings_{direction}"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def kb_pivots(direction: str, current: int) -> InlineKeyboardMarkup:
    opts = [3, 4, 5, 7, 10, 15]
    rows = []
    for v in opts:
        mark = "◉ " if v == current else "○ "
        rows.append(InlineKeyboardButton(text=f"{mark}{v}", callback_data=f"save_{direction}_pivot_{v}"))
    
    keyboard = [rows[i:i + 3] for i in range(0, len(rows), 3)]
    keyboard.append(_back(f"menu_settings_{direction}"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ── СЕРВИСНЫЕ МЕНЮ ───────────────────────────────────

def kb_notify(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── УВЕДОМЛЕНИЯ ──"),
        _btn(_check(user.notify_signal)   + " Сигналы входа", "toggle_notify_signal"),
        _btn(_check(user.notify_breakout) + " Пробои уровней", "toggle_notify_breakout"),
        _back()
    ])

def kb_subscribe(config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(f"💳 30 дней — {config.PRICE_30_DAYS}", "sub_30"),
        _btn(f"💳 90 дней — {config.PRICE_90_DAYS}", "sub_90"),
        _back()
    ])

def kb_back_to_settings(direction: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back(f"menu_settings_{direction}")])
