"""
Клавиатуры для Telegram бота
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from user_manager import UserSettings


def kb_main(user: UserSettings) -> InlineKeyboardMarkup:
    status = "🟢 Сканер ВКЛ" if user.active else "🔴 Сканер ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=status, callback_data="toggle_active")],
        [InlineKeyboardButton(text="📊 Таймфрейм",       callback_data="menu_tf")],
        [InlineKeyboardButton(text="🔄 Интервал скана",  callback_data="menu_interval")],
        [InlineKeyboardButton(text="🔬 Фильтры",         callback_data="menu_filters")],
        [InlineKeyboardButton(text="⭐ Качество сигнала", callback_data="menu_quality")],
        [InlineKeyboardButton(text="🎯 Цели (R:R)",       callback_data="menu_targets")],
        [InlineKeyboardButton(text="💰 Мин. объём монеты",callback_data="menu_volume")],
        [InlineKeyboardButton(text="📈 Моя статистика",   callback_data="my_stats")],
    ])


def kb_timeframes(current: str) -> InlineKeyboardMarkup:
    options = [
        ("1m",  "1 минута — скальпинг"),
        ("5m",  "5 минут — скальпинг"),
        ("15m", "15 минут — интрадей"),
        ("30m", "30 минут — интрадей"),
        ("1h",  "1 час — свинг ⭐"),
        ("4h",  "4 часа — свинг"),
        ("1d",  "1 день — позиционная"),
    ]
    rows = []
    for tf, label in options:
        mark = "✅ " if tf == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{tf} — {label}", callback_data=f"set_tf_{tf}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_intervals(current: int) -> InlineKeyboardMarkup:
    options = [
        (300,   "5 минут"),
        (900,   "15 минут"),
        (1800,  "30 минут"),
        (3600,  "1 час ⭐"),
        (7200,  "2 часа"),
        (14400, "4 часа"),
        (86400, "1 день"),
    ]
    rows = []
    for sec, label in options:
        mark = "✅ " if sec == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_interval_{sec}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_filters(user: UserSettings) -> InlineKeyboardMarkup:
    def icon(val): return "✅" if val else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{icon(user.use_rsi)}     RSI фильтр",          callback_data="toggle_rsi")],
        [InlineKeyboardButton(text=f"{icon(user.use_volume)}  Объёмный фильтр",     callback_data="toggle_volume")],
        [InlineKeyboardButton(text=f"{icon(user.use_pattern)} Свечные паттерны",    callback_data="toggle_pattern")],
        [InlineKeyboardButton(text=f"{icon(user.use_htf)}     HTF тренд (дневной)", callback_data="toggle_htf")],
        [InlineKeyboardButton(text=f"{icon(user.notify_signal)}   Уведомление о сигнале",  callback_data="toggle_notify_signal")],
        [InlineKeyboardButton(text=f"{icon(user.notify_breakout)} Уведомление о пробое",   callback_data="toggle_notify_breakout")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])


def kb_quality(current: int) -> InlineKeyboardMarkup:
    options = [
        (1, "⭐          — любые (много шума)"),
        (2, "⭐⭐         — слабая фильтрация"),
        (3, "⭐⭐⭐        — баланс ⭐"),
        (4, "⭐⭐⭐⭐       — строгая"),
        (5, "⭐⭐⭐⭐⭐      — только идеальные"),
    ]
    rows = []
    for q, label in options:
        mark = "✅ " if q == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_quality_{q}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_targets(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎯 Цель 1: {user.tp1_rr}R  (нажми чтобы изменить)", callback_data="edit_tp1")],
        [InlineKeyboardButton(text=f"🎯 Цель 2: {user.tp2_rr}R  (нажми чтобы изменить)", callback_data="edit_tp2")],
        [InlineKeyboardButton(text=f"🏆 Цель 3: {user.tp3_rr}R  (нажми чтобы изменить)", callback_data="edit_tp3")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])


def kb_volume(current: float) -> InlineKeyboardMarkup:
    options = [
        (500_000,     "500К$ — много монет"),
        (1_000_000,   "1М$   — стандарт ⭐"),
        (5_000_000,   "5М$   — только ликвидные"),
        (10_000_000,  "10М$  — только топ"),
        (50_000_000,  "50М$  — топ-20 монет"),
    ]
    rows = []
    for vol, label in options:
        mark = "✅ " if vol == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_volume_{int(vol)}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")]
    ])
