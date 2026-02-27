"""
keyboards.py — клавиатуры бота v4 (мультисканнинг)
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from user_manager import UserSettings, TradeCfg


def _btn(text: str, cb: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data=cb)]

def _back(cb: str = "back_main") -> list:
    return [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)]

def _noop(text: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data="noop")]

def _check(v: bool) -> str:
    return "✅" if v else "❌"

def _mark(current, val) -> str:
    return "◉ " if current == val else "○ "


# ── Тренд ────────────────────────────────────────────

def trend_text(trend: dict) -> str:
    if not trend: return "🌍 <b>Глобальный тренд:</b> загрузка...\n"
    btc = trend.get("BTC", {})
    eth = trend.get("ETH", {})
    return (
        "🌍 <b>Глобальный тренд (H1 | H4 | D1 | W1):</b>\n"
        "🪙 BTC: " + btc.get("trend_text", "—") + "\n"
        "🪙 ETH: " + eth.get("trend_text", "—") + "\n"
    )

# ── ГЛАВНОЕ МЕНЮ ─────────────────────────────────────

def kb_main(user: UserSettings) -> InlineKeyboardMarkup:
    long_s  = "🟢" if user.long_active  else "⚫"
    short_s = "🟢" if user.short_active else "⚫"
    both_s  = "🟢" if (user.active and user.scan_mode == "both") else "⚫"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(long_s  + " 📈 ЛОНГ сканер  — только сигналы в лонг",  "mode_long"),
        _btn(short_s + " 📉 ШОРТ сканер  — только сигналы в шорт",  "mode_short"),
        _btn(both_s  + " ⚡ ОБА — лонги и шорты одновременно",       "mode_both"),
        [
            InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"),
            InlineKeyboardButton(text="📈 График",          callback_data="my_chart"),
        ],
        _btn("❓ Справка — что делает каждая кнопка", "help_show"),
    ])


# ── МЕНЮ ЛОНГ ────────────────────────────────────────

def kb_mode_long(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_long_cfg()
    status = "🟢 ЛОНГ ВКЛЮЧЁН — нажми чтобы остановить" if user.long_active \
           else "🔴 ЛОНГ ВЫКЛЮЧЕН — нажми чтобы запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                           "toggle_long"),
        _btn("📊 Таймфрейм: " + cfg.timeframe,                "menu_long_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval//60) + " мин.", "menu_long_interval"),
        _btn("⚙️ Настройки ЛОНГ →",                           "menu_long_settings"),
        _btn("📐 Пивоты",    "menu_long_pivots"),
        _btn("📉 EMA тренд", "menu_long_ema"),
        _btn("🔬 Фильтры",   "menu_long_filters"),
        _btn("⭐ Качество",   "menu_long_quality"),
        _btn("🛡 Стоп-лосс", "menu_long_sl"),
        _btn("🎯 Цели (TP)", "menu_long_targets"),
        _btn("🔁 Сбросить настройки ЛОНГ к общим", "reset_long_cfg"),
        _back(),
    ])


# ── МЕНЮ ШОРТ ────────────────────────────────────────

def kb_mode_short(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_short_cfg()
    status = "🟢 ШОРТ ВКЛЮЧЁН — нажми чтобы остановить" if user.short_active \
           else "🔴 ШОРТ ВЫКЛЮЧЕН — нажми чтобы запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                            "toggle_short"),
        _btn("📊 Таймфрейм: " + cfg.timeframe,                 "menu_short_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval//60) + " мин.", "menu_short_interval"),
        _btn("⚙️ Настройки ШОРТ →",                            "menu_short_settings"),
        _btn("📐 Пивоты",    "menu_short_pivots"),
        _btn("📉 EMA тренд", "menu_short_ema"),
        _btn("🔬 Фильтры",   "menu_short_filters"),
        _btn("⭐ Качество",   "menu_short_quality"),
        _btn("🛡 Стоп-лосс", "menu_short_sl"),
        _btn("🎯 Цели (TP)", "menu_short_targets"),
        _btn("🔁 Сбросить настройки ШОРТ к общим", "reset_short_cfg"),
        _back(),
    ])


# ── МЕНЮ ОБА ─────────────────────────────────────────

def kb_mode_both(user: UserSettings) -> InlineKeyboardMarkup:
    active = user.active and user.scan_mode == "both"
    status = "🟢 Сканер ВКЛ — нажми чтобы остановить" if active \
           else "🔴 Сканер ВЫКЛ — нажми чтобы запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                   "toggle_both"),
        _btn("📊 Таймфрейм: " + user.timeframe,                       "menu_tf"),
        _btn("🔄 Интервал: " + str(user.scan_interval//60) + " мин.", "menu_interval"),
        _btn("⚙️ Все настройки сигнала →",                            "menu_settings"),
        _back(),
    ])


# ── ВСПОМОГАТЕЛЬНАЯ — TF / Интервал ─────────────────

def _tf_rows(current: str, prefix: str, back_cb: str) -> list:
    tfs = [
        ("1m","1 мин — скальпинг"), ("5m","5 мин — скальпинг"),
        ("15m","15 мин — интрадей"), ("30m","30 мин — интрадей"),
        ("1h","1 час — свинг ⭐"), ("4h","4 часа — свинг"),
        ("1d","1 день — позиционная"),
    ]
    rows = [_noop("── Выбери таймфрейм ──")]
    for tf, desc in tfs:
        rows.append(_btn(_mark(current, tf) + tf + " — " + desc, prefix + tf))
    rows.append(_back(back_cb))
    return rows


def _interval_rows(current: int, prefix: str, back_cb: str) -> list:
    opts = [
        (300,"5 мин"), (900,"15 мин"), (1800,"30 мин"),
        (3600,"1 час ⭐"), (7200,"2 часа"), (14400,"4 часа"), (86400,"1 день"),
    ]
    rows = [_noop("── Интервал сканирования ──")]
    for sec, desc in opts:
        rows.append(_btn(_mark(current, sec) + desc, prefix + str(sec)))
    rows.append(_back(back_cb))
    return rows


# TF
def kb_timeframes(cur: str, *a)   -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_tf_rows(cur, "set_tf_", "mode_both"))
def kb_long_timeframes(cur: str)  -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_tf_rows(cur, "set_long_tf_", "mode_long"))
def kb_short_timeframes(cur: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_tf_rows(cur, "set_short_tf_", "mode_short"))

# Интервал
def kb_intervals(cur: int)        -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_interval_rows(cur, "set_interval_", "mode_both"))
def kb_long_intervals(cur: int)   -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_interval_rows(cur, "set_long_interval_", "mode_long"))
def kb_short_intervals(cur: int)  -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_interval_rows(cur, "set_short_interval_", "mode_short"))


# ── НАСТРОЙКИ (принимают cfg + prefix для callback) ──

def _settings_menu(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    """Общий шаблон меню настроек — для shared/long/short."""
    p = prefix  # "" / "long_" / "short_"
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Сигналы ──────────────────"),
        _btn("📐 Пивоты и уровни S/R",          "menu_" + p + "pivots"),
        _btn("📉 EMA тренд",                     "menu_" + p + "ema"),
        _btn("🔬 Фильтры (RSI / Объём / HTF)",  "menu_" + p + "filters"),
        _btn("⭐ Качество сигнала",               "menu_" + p + "quality"),
        _btn("🔁 Cooldown между сигналами",       "menu_" + p + "cooldown"),
        _noop("── Риск-менеджмент ──────────"),
        _btn("🛡 Стоп-лосс (ATR)",               "menu_" + p + "sl"),
        _btn("🎯 Цели (Take Profit R:R)",         "menu_" + p + "targets"),
        _noop("── Монеты ──────────────────"),
        _btn("💰 Фильтр монет по объёму",         "menu_" + p + "volume"),
        _noop("── Уведомления ─────────────"),
        _btn("📱 Уведомления",                    "menu_notify"),
        _back(back_cb),
    ])

def kb_settings()       -> InlineKeyboardMarkup: return _settings_menu("",       "mode_both")
def kb_long_settings()  -> InlineKeyboardMarkup: return _settings_menu("long_",  "mode_long")
def kb_short_settings() -> InlineKeyboardMarkup: return _settings_menu("short_", "mode_short")


# ── ПИВОТЫ ───────────────────────────────────────────

def _pivots_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Чувствительность пивотов ──────────────────────")]
    for v, d in [(3,"3 — много"), (5,"5 — умеренно"), (7,"7 — стандарт ⭐"), (10,"10 — сильные"), (15,"15 — ключевые")]:
        rows.append(_btn(_mark(cfg.pivot_strength, v) + d, p + "set_pivot_" + str(v)))
    rows.append(_noop("── Макс. возраст уровня ──────────────────────────"))
    for v, d in [(50,"50 свечей — свежие"), (100,"100 — стандарт ⭐"), (150,"150"), (200,"200 — исторические")]:
        rows.append(_btn(_mark(cfg.max_level_age, v) + d, p + "set_age_" + str(v)))
    rows.append(_noop("── Макс. ожидание ретеста ────────────────────────"))
    for v, d in [(10,"10"), (20,"20"), (30,"30 ⭐"), (50,"50")]:
        rows.append(_btn(_mark(cfg.max_retest_bars, v) + str(v) + " свечей — " + d, p + "set_retest_" + str(v)))
    rows.append(_noop("── Буфер зоны (ATR) ──────────────────────────────"))
    for v, d in [(0.1,"x0.1"), (0.2,"x0.2"), (0.3,"x0.3 ⭐"), (0.5,"x0.5")]:
        rows.append(_btn(_mark(cfg.zone_buffer, v) + str(v) + " — " + d, p + "set_buffer_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pivots(user: UserSettings)       -> InlineKeyboardMarkup: return _pivots_kb(user.shared_cfg(), "",       "menu_settings")
def kb_long_pivots(user: UserSettings)  -> InlineKeyboardMarkup: return _pivots_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_pivots(user: UserSettings) -> InlineKeyboardMarkup: return _pivots_kb(user.get_short_cfg(), "short_", "mode_short")


# ── EMA ──────────────────────────────────────────────

def _ema_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Быстрая EMA ───────────────────────────────────")]
    for v, d in [(20,"EMA 20"), (50,"EMA 50 ⭐"), (100,"EMA 100")]:
        rows.append(_btn(_mark(cfg.ema_fast, v) + d, p + "set_ema_fast_" + str(v)))
    rows.append(_noop("── Медленная EMA ─────────────────────────────────"))
    for v, d in [(100,"EMA 100"), (200,"EMA 200 ⭐"), (500,"EMA 500")]:
        rows.append(_btn(_mark(cfg.ema_slow, v) + d, p + "set_ema_slow_" + str(v)))
    rows.append(_noop("── HTF EMA ───────────────────────────────────────"))
    for v, d in [(20,"20"), (50,"50 ⭐"), (100,"100"), (200,"200")]:
        rows.append(_btn(_mark(cfg.htf_ema_period, v) + "EMA " + str(v) + " — " + d, p + "set_htf_ema_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ema(user: UserSettings)       -> InlineKeyboardMarkup: return _ema_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_ema(user: UserSettings)  -> InlineKeyboardMarkup: return _ema_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_ema(user: UserSettings) -> InlineKeyboardMarkup: return _ema_kb(user.get_short_cfg(), "short_", "mode_short")


# ── ФИЛЬТРЫ ──────────────────────────────────────────

def _filters_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("── Вкл/выкл фильтры ─────────────────────────────"),
        _btn(_check(cfg.use_rsi)     + " RSI",      p + "toggle_rsi"),
        _btn(_check(cfg.use_volume)  + " Объём",    p + "toggle_volume"),
        _btn(_check(cfg.use_pattern) + " Паттерны", p + "toggle_pattern"),
        _btn(_check(cfg.use_htf)     + " HTF тренд (+⭐ качество)", p + "toggle_htf"),
        _btn(_check(cfg.trend_only)  + " 📊 Тренд-сигналы (только по тренду)", p + "toggle_trend_only"),
        _noop("── Период RSI ────────────────────────────────────"),
    ]
    for v, d in [(7,"RSI 7 — быстрый"), (14,"RSI 14 ⭐"), (21,"RSI 21 — медленный")]:
        rows.append(_btn(_mark(cfg.rsi_period, v) + d, p + "set_rsi_period_" + str(v)))
    rows.append(_noop("── Перекупленность RSI ───────────────────────────"))
    for v in [60, 65, 70, 75]:
        rows.append(_btn(_mark(cfg.rsi_ob, v) + str(v), p + "set_rsi_ob_" + str(v)))
    rows.append(_noop("── Перепроданность RSI ───────────────────────────"))
    for v in [25, 30, 35, 40]:
        rows.append(_btn(_mark(cfg.rsi_os, v) + str(v), p + "set_rsi_os_" + str(v)))
    rows.append(_noop("── Объём (множитель) ─────────────────────────────"))
    for v, d in [(1.0,"x1.0"), (1.2,"x1.2 ⭐"), (1.5,"x1.5"), (2.0,"x2.0")]:
        rows.append(_btn(_mark(cfg.vol_mult, v) + d, p + "set_vol_mult_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_filters(user: UserSettings)       -> InlineKeyboardMarkup: return _filters_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_filters(user: UserSettings)  -> InlineKeyboardMarkup: return _filters_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_filters(user: UserSettings) -> InlineKeyboardMarkup: return _filters_kb(user.get_short_cfg(), "short_", "mode_short")


# ── КАЧЕСТВО ─────────────────────────────────────────

def _quality_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Минимальное качество сигнала ──────────────────")]
    for q, d in [(1,"⭐"),(2,"⭐⭐ ⭐"),(3,"⭐⭐⭐ рекомендуем"),(4,"⭐⭐⭐⭐ строгий"),(5,"⭐⭐⭐⭐⭐ только идеальные")]:
        rows.append(_btn(_mark(cfg.min_quality, q) + d, p + "set_quality_" + str(q)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_quality(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(min_quality=cur); return _quality_kb(cfg, "", "menu_settings")
def kb_long_quality(user: UserSettings)  -> InlineKeyboardMarkup: return _quality_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_quality(user: UserSettings) -> InlineKeyboardMarkup: return _quality_kb(user.get_short_cfg(), "short_", "mode_short")


# ── COOLDOWN ─────────────────────────────────────────

def _cooldown_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Cooldown между сигналами ──────────────────────")]
    for v, d in [(3,"3 свечи"),(5,"5 свечей ⭐"),(10,"10 свечей"),(15,"15"),(20,"20 — очень редко")]:
        rows.append(_btn(_mark(cfg.cooldown_bars, v) + str(v) + " — " + d, p + "set_cooldown_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cooldown(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(cooldown_bars=cur); return _cooldown_kb(cfg, "", "menu_settings")
def kb_long_cooldown(user: UserSettings)  -> InlineKeyboardMarkup: return _cooldown_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_cooldown(user: UserSettings) -> InlineKeyboardMarkup: return _cooldown_kb(user.get_short_cfg(), "short_", "mode_short")


# ── СТОП-ЛОСС ────────────────────────────────────────

def _sl_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Период ATR ────────────────────────────────────")]
    for v, d in [(7,"ATR 7 — быстрый"), (14,"ATR 14 ⭐"), (21,"ATR 21 — медленный")]:
        rows.append(_btn(_mark(cfg.atr_period, v) + d, p + "set_atr_period_" + str(v)))
    rows.append(_noop("── ATR множитель ─────────────────────────────────"))
    for v, d in [(0.5,"x0.5 — близкий"),(1.0,"x1.0 ⭐"),(1.5,"x1.5 — широкий"),(2.0,"x2.0")]:
        rows.append(_btn(_mark(cfg.atr_mult, v) + d, p + "set_atr_mult_" + str(v)))
    rows.append(_noop("── Макс. риск % ──────────────────────────────────"))
    for v, d in [(0.5,"0.5%"),(1.0,"1.0%"),(1.5,"1.5% ⭐"),(2.0,"2.0%"),(3.0,"3.0%")]:
        rows.append(_btn(_mark(cfg.max_risk_pct, v) + d, p + "set_risk_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_sl(user: UserSettings)        -> InlineKeyboardMarkup: return _sl_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_sl(user: UserSettings)   -> InlineKeyboardMarkup: return _sl_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_sl(user: UserSettings)  -> InlineKeyboardMarkup: return _sl_kb(user.get_short_cfg(), "short_", "mode_short")


# ── ЦЕЛИ ─────────────────────────────────────────────

def kb_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.shared_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Цели Take Profit (общие) ───────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_tp3"),
        _back("menu_settings"),
    ])

def kb_long_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_long_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Цели Take Profit ЛОНГ ──────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_long_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_long_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_long_tp3"),
        _back("mode_long"),
    ])

def kb_short_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_short_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Цели Take Profit ШОРТ ──────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_short_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_short_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_short_tp3"),
        _back("mode_short"),
    ])


# ── ОБЪЁМ ────────────────────────────────────────────

def _volume_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    opts = [
        (100_000,"100К$"),(500_000,"500К$"),(1_000_000,"1М$ ⭐"),
        (5_000_000,"5М$"),(10_000_000,"10М$"),(50_000_000,"50М$"),
    ]
    rows = [_noop("── Мин. суточный объём монеты ───────────────────")]
    for v, d in opts:
        rows.append(_btn(_mark(cfg.min_volume_usdt, float(v)) + d, p + "set_volume_" + str(int(v))))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_volume(cur: float)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(min_volume_usdt=cur); return _volume_kb(cfg, "", "menu_settings")
def kb_long_volume(user: UserSettings)  -> InlineKeyboardMarkup: return _volume_kb(user.get_long_cfg(),  "long_",  "menu_long_settings")
def kb_short_volume(user: UserSettings) -> InlineKeyboardMarkup: return _volume_kb(user.get_short_cfg(), "short_", "menu_short_settings")


# ── УВЕДОМЛЕНИЯ ──────────────────────────────────────

def kb_notify(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Типы уведомлений ──────────────────────────────"),
        _btn(_check(user.notify_signal)   + " Сигнал входа",        "toggle_notify_signal"),
        _btn(_check(user.notify_breakout) + " Пробой уровня (ранний)","toggle_notify_breakout"),
        _back("menu_settings"),
    ])


# ── ВСПОМОГАТЕЛЬНЫЕ ──────────────────────────────────

def kb_back()          -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back()])
def kb_back_settings() -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back("menu_settings")])

def kb_subscribe(config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn("💳 Оформить — " + config.PRICE_30_DAYS + " / 30 дней", "noop"),
        _btn("📩 Написать администратору", "noop"),
        _btn("ℹ️ Узнать подробнее /subscribe", "noop"),
    ])


def kb_back_photo() -> InlineKeyboardMarkup:
    """Кнопка «Назад» для сообщений с фото (удаляет фото, открывает главное меню)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_photo_main")]
    ])


# ── СПРАВКА ───────────────────────────────────────────

def help_text() -> str:
    return (
        "❓ <b>СПРАВКА — CHM BREAKER BOT</b>\n\n"
        "── <b>Режимы сканера</b> ──\n"
        "📈 <b>ЛОНГ</b> — поиск только лонговых сигналов\n"
        "📉 <b>ШОРТ</b> — поиск только шортовых сигналов\n"
        "⚡ <b>ОБА</b> — лонги + шорты с общими настройками\n\n"
        "── <b>Основные настройки</b> ──\n"
        "📊 <b>Таймфрейм</b> — период свечей (1m/5m/15m/1h/4h/1d)\n"
        "🔄 <b>Интервал</b> — как часто проверять рынок\n\n"
        "── <b>Сигналы</b> ──\n"
        "📐 <b>Пивоты</b> — чувствительность поиска уровней S/R\n"
        "📉 <b>EMA тренд</b> — быстрая/медленная EMA для тренда\n"
        "🔬 <b>Фильтры</b> — RSI, объём, паттерны, HTF, тренд-сигналы\n"
        "⭐ <b>Качество</b> — минимальный порог звёзд (1–5) для сигнала\n"
        "🔁 <b>Cooldown</b> — пауза между сигналами по одной монете\n\n"
        "── <b>Риск-менеджмент</b> ──\n"
        "🛡 <b>Стоп-лосс</b> — множитель ATR для стопа\n"
        "🎯 <b>Цели (TP)</b> — R:R для TP1/TP2/TP3\n\n"
        "── <b>Монеты</b> ──\n"
        "💰 <b>Объём монет</b> — мин. суточный объём монеты в $\n\n"
        "── <b>Под сигналом</b> ──\n"
        "📈 <b>График</b> — открыть монету на TradingView\n"
        "📊 <b>Статистика</b> — показать кривую доходности\n"
        "📋 <b>Результат</b> — записать итог сделки (TP1/TP2/TP3/SL/Пропустил)\n\n"
        "── <b>Качество ⭐ (из чего складывается)</b> ──\n"
        "Базовые 2 звезды за любой сигнал\n"
        "+1 за объём выше нормы (VOL × множитель)\n"
        "+1 за сигнал ПО тренду EMA\n"
        "+1 за паттерн свечи (пин-бар / поглощение)\n"
        "+1 за подтверждение HTF тренда (если HTF включён)\n\n"
        "── <b>Фильтры подробнее</b> ──\n"
        "✅ <b>RSI</b> — блокирует вход при перекупленности/перепроданности\n"
        "✅ <b>Объём</b> — требует повышенного объёма для входа\n"
        "✅ <b>Паттерны</b> — пин-бары и поглощения как подтверждение\n"
        "✅ <b>HTF тренд</b> — старший ТФ (1D) должен совпадать по тренду, даёт +1 звезду\n"
        "✅ <b>Тренд-сигналы</b> — показывать только сигналы ПО тренду (без контр-трендов)\n"
    )


def kb_help() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back()])
