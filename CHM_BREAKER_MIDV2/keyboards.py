"""
keyboards.py — клавиатуры бота v4.6
Каждая кнопка в отдельной строке. Описания убраны — см. PDF-гайд.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from user_manager import UserSettings, TradeCfg


def _btn(text: str, cb: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data=cb)]

def _back(cb: str = "back_main") -> list:
    return [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)]

def _noop(text: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data="noop")]

def _sep() -> list:
    return _noop("─────────────────────────────────────────")

def _check(v: bool) -> str:
    return "✅" if v else "❌"

def _mark(current, val) -> str:
    return "◉ " if current == val else "○ "


# ── Тренд ─────────────────────────────────────────────────────────────────

def trend_text(trend: dict) -> str:
    if not trend:
        return "🌍 <b>Глобальный тренд:</b> загрузка...\n"
    btc = trend.get("BTC", {})
    eth = trend.get("ETH", {})
    return (
        "🌍 <b>Глобальный тренд (1D):</b>\n"
        + btc.get("emoji", "❓") + " BTC: <b>" + btc.get("trend", "—") + "</b>"
        + "   " + eth.get("emoji", "❓") + " ETH: <b>" + eth.get("trend", "—") + "</b>\n"
    )


# ══════════════════════════════════════════════════════════════════════════
#  ГЛАВНОЕ МЕНЮ
# ══════════════════════════════════════════════════════════════════════════

def kb_main(user: UserSettings) -> InlineKeyboardMarkup:
    long_s  = "🟢" if user.long_active  else "⚫"
    short_s = "🟢" if user.short_active else "⚫"
    both_s  = "🟢" if (user.active and user.scan_mode == "both") else "⚫"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(long_s  + " 📈 ЛОНГ",  "mode_long"),
        _btn(short_s + " 📉 ШОРТ",  "mode_short"),
        _btn(both_s  + " ⚡ ОБА",   "mode_both"),
        _btn("📊 Статистика",        "my_stats"),
    ])


# ══════════════════════════════════════════════════════════════════════════
#  МЕНЮ ЛОНГ / ШОРТ / ОБА
# ══════════════════════════════════════════════════════════════════════════

def kb_mode_long(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_long_cfg()
    status = "🟢 ЛОНГ ВКЛ — остановить" if user.long_active else "🔴 ЛОНГ ВЫКЛ — запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                "toggle_long"),
        _sep(),
        _btn("📊 Таймфрейм: " + cfg.timeframe,                     "menu_long_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval // 60) + " мин", "menu_long_interval"),
        _sep(),
        _btn("⚡ SMC условия",        "menu_long_smc"),
        _btn("📐 Пивоты / S&R",      "menu_long_pivots"),
        _btn("📉 EMA тренд",          "menu_long_ema"),
        _btn("🔬 Фильтры",            "menu_long_filters"),
        _btn("⭐ Качество сигнала",   "menu_long_quality"),
        _btn("🔁 Cooldown",           "menu_long_cooldown"),
        _btn("🛡 Стоп-лосс (ATR)",    "menu_long_sl"),
        _btn("🎯 Take Profit",        "menu_long_targets"),
        _btn("💰 Фильтр монет",       "menu_long_volume"),
        _sep(),
        _btn("🔁 Сбросить к общим",  "reset_long_cfg"),
        _back(),
    ])


def kb_mode_short(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_short_cfg()
    status = "🟢 ШОРТ ВКЛ — остановить" if user.short_active else "🔴 ШОРТ ВЫКЛ — запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                 "toggle_short"),
        _sep(),
        _btn("📊 Таймфрейм: " + cfg.timeframe,                      "menu_short_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval // 60) + " мин", "menu_short_interval"),
        _sep(),
        _btn("⚡ SMC условия",        "menu_short_smc"),
        _btn("📐 Пивоты / S&R",      "menu_short_pivots"),
        _btn("📉 EMA тренд",          "menu_short_ema"),
        _btn("🔬 Фильтры",            "menu_short_filters"),
        _btn("⭐ Качество сигнала",   "menu_short_quality"),
        _btn("🔁 Cooldown",           "menu_short_cooldown"),
        _btn("🛡 Стоп-лосс (ATR)",    "menu_short_sl"),
        _btn("🎯 Take Profit",        "menu_short_targets"),
        _btn("💰 Фильтр монет",       "menu_short_volume"),
        _sep(),
        _btn("🔁 Сбросить к общим",  "reset_short_cfg"),
        _back(),
    ])


def kb_mode_both(user: UserSettings) -> InlineKeyboardMarkup:
    active = user.active and user.scan_mode == "both"
    status = "🟢 Сканер ВКЛ — остановить" if active else "🔴 Сканер ВЫКЛ — запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                    "toggle_both"),
        _sep(),
        _btn("📊 Таймфрейм: " + user.timeframe,                       "menu_tf"),
        _btn("🔄 Интервал: " + str(user.scan_interval // 60) + " мин", "menu_interval"),
        _sep(),
        _btn("⚙️ Все настройки →",  "menu_settings"),
        _back(),
    ])


# ══════════════════════════════════════════════════════════════════════════
#  ТАЙМФРЕЙМ / ИНТЕРВАЛ
# ══════════════════════════════════════════════════════════════════════════

_TF_OPTIONS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
_TF_LABELS  = {
    "1m": "1 мин", "5m": "5 мин", "15m": "15 мин", "30m": "30 мин",
    "1h": "1 час ⭐", "4h": "4 часа", "1d": "1 день",
}

def _tf_rows(current: str, prefix: str, back_cb: str) -> list:
    rows = [_noop("── Таймфрейм ─────────────────────────────────────────────")]
    for tf in _TF_OPTIONS:
        rows.append(_btn(_mark(current, tf) + _TF_LABELS[tf], prefix + tf))
    rows.append(_back(back_cb))
    return rows

_INTERVAL_OPTIONS = {
    300: "5 мин", 900: "15 мин", 1800: "30 мин",
    3600: "1 час ⭐", 7200: "2 часа", 14400: "4 часа", 86400: "1 день",
}

def _interval_rows(current: int, prefix: str, back_cb: str) -> list:
    rows = [_noop("── Интервал сканирования ────────────────────────────────")]
    for sec, label in _INTERVAL_OPTIONS.items():
        rows.append(_btn(_mark(current, sec) + label, prefix + str(sec)))
    rows.append(_back(back_cb))
    return rows


def kb_timeframes(cur: str, *a)   -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_tf_rows(cur, "set_tf_", "mode_both"))
def kb_long_timeframes(cur: str)  -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_tf_rows(cur, "set_long_tf_", "mode_long"))
def kb_short_timeframes(cur: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_tf_rows(cur, "set_short_tf_", "mode_short"))

def kb_intervals(cur: int)        -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_interval_rows(cur, "set_interval_", "mode_both"))
def kb_long_intervals(cur: int)   -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_interval_rows(cur, "set_long_interval_", "mode_long"))
def kb_short_intervals(cur: int)  -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_interval_rows(cur, "set_short_interval_", "mode_short"))


# ══════════════════════════════════════════════════════════════════════════
#  МЕНЮ НАСТРОЕК
# ══════════════════════════════════════════════════════════════════════════

def _settings_menu(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Анализ рынка ─────────────────────────────────────"),
        _btn("⚡ SMC условия",             "menu_" + p + "smc"),
        _btn("📐 Пивоты / S&R",           "menu_" + p + "pivots"),
        _btn("📉 EMA тренд",               "menu_" + p + "ema"),
        _btn("🔬 Фильтры",                 "menu_" + p + "filters"),
        _btn("⭐ Качество сигнала",         "menu_" + p + "quality"),
        _btn("🔁 Cooldown",                "menu_" + p + "cooldown"),
        _noop("── Риск-менеджмент ──────────────────────────────────"),
        _btn("🛡 Стоп-лосс (ATR)",         "menu_" + p + "sl"),
        _btn("🎯 Take Profit (R:R)",        "menu_" + p + "targets"),
        _noop("── Прочее ───────────────────────────────────────────"),
        _btn("💰 Фильтр монет",             "menu_" + p + "volume"),
        _btn("📱 Уведомления",              "menu_notify"),
        _back(back_cb),
    ])

def kb_settings()       -> InlineKeyboardMarkup: return _settings_menu("",       "mode_both")
def kb_long_settings()  -> InlineKeyboardMarkup: return _settings_menu("long_",  "mode_long")
def kb_short_settings() -> InlineKeyboardMarkup: return _settings_menu("short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  SMC
# ══════════════════════════════════════════════════════════════════════════

def _smc_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── SMC условия входа ──────────────────────────────"),
        _btn(_check(cfg.smc_use_bos)   + "  BOS — Break of Structure",  p + "smc_toggle_bos"),
        _btn(_check(cfg.smc_use_ob)    + "  Order Block (OB)",           p + "smc_toggle_ob"),
        _btn(_check(cfg.smc_use_fvg)   + "  FVG — Fair Value Gap",       p + "smc_toggle_fvg"),
        _btn(_check(cfg.smc_use_sweep) + "  Sweep — ложный пробой",      p + "smc_toggle_sweep"),
        _btn(_check(cfg.smc_use_choch) + "  CHOCH — смена структуры",    p + "smc_toggle_choch"),
        _btn(_check(cfg.smc_use_conf)  + "  Daily Confluence (D1)",      p + "smc_toggle_conf"),
        _back(back_cb),
    ])

def kb_smc(user: UserSettings)       -> InlineKeyboardMarkup: return _smc_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_smc(user: UserSettings)  -> InlineKeyboardMarkup: return _smc_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_smc(user: UserSettings) -> InlineKeyboardMarkup: return _smc_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  ПИВОТЫ
# ══════════════════════════════════════════════════════════════════════════

def _pivots_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Чувствительность пивотов ─────────────────────────")]
    for v, d in [(3,"3"), (5,"5"), (7,"7 ⭐"), (10,"10"), (15,"15")]:
        rows.append(_btn(_mark(cfg.pivot_strength, v) + d, p + "set_pivot_" + str(v)))

    rows.append(_noop("── Макс. возраст уровня (свечей) ────────────────────"))
    for v, d in [(50,"50"), (100,"100 ⭐"), (150,"150"), (200,"200")]:
        rows.append(_btn(_mark(cfg.max_level_age, v) + d, p + "set_age_" + str(v)))

    rows.append(_noop("── Макс. ожидание ретеста (свечей) ──────────────────"))
    for v, d in [(10,"10"), (20,"20"), (30,"30 ⭐"), (50,"50")]:
        rows.append(_btn(_mark(cfg.max_retest_bars, v) + d, p + "set_retest_" + str(v)))

    rows.append(_noop("── Буфер зоны (× ATR) ───────────────────────────────"))
    for v, d in [(0.1,"×0.1"), (0.2,"×0.2"), (0.3,"×0.3 ⭐"), (0.5,"×0.5")]:
        rows.append(_btn(_mark(cfg.zone_buffer, v) + d, p + "set_buffer_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pivots(user: UserSettings)       -> InlineKeyboardMarkup: return _pivots_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_pivots(user: UserSettings)  -> InlineKeyboardMarkup: return _pivots_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_pivots(user: UserSettings) -> InlineKeyboardMarkup: return _pivots_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  EMA
# ══════════════════════════════════════════════════════════════════════════

def _ema_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Быстрая EMA ──────────────────────────────────────────")]
    for v, d in [(20,"EMA 20"), (50,"EMA 50 ⭐"), (100,"EMA 100")]:
        rows.append(_btn(_mark(cfg.ema_fast, v) + d, p + "set_ema_fast_" + str(v)))

    rows.append(_noop("── Медленная EMA ────────────────────────────────────────"))
    for v, d in [(100,"EMA 100"), (200,"EMA 200 ⭐"), (500,"EMA 500")]:
        rows.append(_btn(_mark(cfg.ema_slow, v) + d, p + "set_ema_slow_" + str(v)))

    rows.append(_noop("── HTF EMA (старший таймфрейм) ──────────────────────────"))
    for v, d in [(20,"EMA 20"), (50,"EMA 50 ⭐"), (100,"EMA 100"), (200,"EMA 200")]:
        rows.append(_btn(_mark(cfg.htf_ema_period, v) + "HTF " + d, p + "set_htf_ema_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ema(user: UserSettings)       -> InlineKeyboardMarkup: return _ema_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_ema(user: UserSettings)  -> InlineKeyboardMarkup: return _ema_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_ema(user: UserSettings) -> InlineKeyboardMarkup: return _ema_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  ФИЛЬТРЫ
# ══════════════════════════════════════════════════════════════════════════

def _filters_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Фильтры сигнала ──────────────────────────────────────")]
    rows.append(_btn(_check(cfg.use_rsi)     + "  RSI фильтр",         p + "toggle_rsi"))
    rows.append(_btn(_check(cfg.use_volume)  + "  Фильтр объёма",      p + "toggle_volume"))
    rows.append(_btn(_check(cfg.use_pattern) + "  Паттерны свечей",    p + "toggle_pattern"))
    rows.append(_btn(_check(cfg.use_htf)     + "  HTF тренд (D1)",     p + "toggle_htf"))
    rows.append(_btn(_check(cfg.use_session) + "  Прайм-сессии",       p + "toggle_session"))

    rows.append(_noop("── Период RSI ───────────────────────────────────────────"))
    for v, d in [(7,"RSI 7"), (14,"RSI 14 ⭐"), (21,"RSI 21")]:
        rows.append(_btn(_mark(cfg.rsi_period, v) + d, p + "set_rsi_period_" + str(v)))

    rows.append(_noop("── Перекупленность RSI (для ШОРТ) ───────────────────────"))
    for v in [60, 65, 70, 75]:
        rows.append(_btn(_mark(cfg.rsi_ob, v) + str(v), p + "set_rsi_ob_" + str(v)))

    rows.append(_noop("── Перепроданность RSI (для ЛОНГ) ───────────────────────"))
    for v in [25, 30, 35, 40]:
        rows.append(_btn(_mark(cfg.rsi_os, v) + str(v), p + "set_rsi_os_" + str(v)))

    rows.append(_noop("── Множитель объёма ─────────────────────────────────────"))
    for v, d in [(1.0,"×1.0"), (1.2,"×1.2 ⭐"), (1.5,"×1.5"), (2.0,"×2.0")]:
        rows.append(_btn(_mark(cfg.vol_mult, v) + d, p + "set_vol_mult_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_filters(user: UserSettings)       -> InlineKeyboardMarkup: return _filters_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_filters(user: UserSettings)  -> InlineKeyboardMarkup: return _filters_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_filters(user: UserSettings) -> InlineKeyboardMarkup: return _filters_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  КАЧЕСТВО
# ══════════════════════════════════════════════════════════════════════════

def _quality_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Мин. качество сигнала ────────────────────────────────")]
    for q, d in [
        (1, "⭐          — все сигналы"),
        (2, "⭐⭐        — слабые"),
        (3, "⭐⭐⭐      — баланс ⭐"),
        (4, "⭐⭐⭐⭐    — строгий"),
        (5, "⭐⭐⭐⭐⭐  — идеальные"),
    ]:
        rows.append(_btn(_mark(cfg.min_quality, q) + d, p + "set_quality_" + str(q)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_quality(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(); cfg.min_quality = cur; return _quality_kb(cfg, "", "menu_settings")
def kb_long_quality(user: UserSettings)  -> InlineKeyboardMarkup: return _quality_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_quality(user: UserSettings) -> InlineKeyboardMarkup: return _quality_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  COOLDOWN
# ══════════════════════════════════════════════════════════════════════════

def _cooldown_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Cooldown (пауза между сигналами) ─────────────────────")]
    for v, d in [(3,"3 свечи"), (5,"5 свечей ⭐"), (10,"10 свечей"), (15,"15 свечей"), (20,"20 свечей")]:
        rows.append(_btn(_mark(cfg.cooldown_bars, v) + d, p + "set_cooldown_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cooldown(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(); cfg.cooldown_bars = cur; return _cooldown_kb(cfg, "", "menu_settings")
def kb_long_cooldown(user: UserSettings)  -> InlineKeyboardMarkup: return _cooldown_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_cooldown(user: UserSettings) -> InlineKeyboardMarkup: return _cooldown_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  СТОП-ЛОСС
# ══════════════════════════════════════════════════════════════════════════

def _sl_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Период ATR ───────────────────────────────────────────")]
    for v, d in [(7,"ATR 7"), (14,"ATR 14 ⭐"), (21,"ATR 21")]:
        rows.append(_btn(_mark(cfg.atr_period, v) + d, p + "set_atr_period_" + str(v)))

    rows.append(_noop("── Множитель ATR ────────────────────────────────────────"))
    for v, d in [(0.5,"×0.5"), (1.0,"×1.0 ⭐"), (1.5,"×1.5"), (2.0,"×2.0")]:
        rows.append(_btn(_mark(cfg.atr_mult, v) + d, p + "set_atr_mult_" + str(v)))

    rows.append(_noop("── Макс. риск на сделку (%) ─────────────────────────────"))
    for v, d in [(0.5,"0.5%"), (1.0,"1.0%"), (1.5,"1.5% ⭐"), (2.0,"2.0%"), (3.0,"3.0%")]:
        rows.append(_btn(_mark(cfg.max_risk_pct, v) + d, p + "set_risk_" + str(v)))

    rows.append(_noop("── Фильтр: пропустить сигнал если стоп > X% ─────────────"))
    for v, d in [(0.0,"Выкл ⭐"), (1.0,"≤ 1.0%"), (1.5,"≤ 1.5%"), (2.0,"≤ 2.0%"), (3.0,"≤ 3.0%"), (5.0,"≤ 5.0%")]:
        rows.append(_btn(_mark(cfg.max_signal_risk_pct, v) + d, p + "set_signal_risk_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_sl(user: UserSettings)       -> InlineKeyboardMarkup: return _sl_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_sl(user: UserSettings)  -> InlineKeyboardMarkup: return _sl_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_sl(user: UserSettings) -> InlineKeyboardMarkup: return _sl_kb(user.get_short_cfg(), "short_", "mode_short")


# ══════════════════════════════════════════════════════════════════════════
#  ЦЕЛИ (TP)
# ══════════════════════════════════════════════════════════════════════════

def kb_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.shared_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Take Profit (общие) ──────────────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_tp3"),
        _back("menu_settings"),
    ])

def kb_long_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_long_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Take Profit ЛОНГ ────────────────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_long_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_long_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_long_tp3"),
        _back("mode_long"),
    ])

def kb_short_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_short_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Take Profit ШОРТ ────────────────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_short_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_short_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_short_tp3"),
        _back("mode_short"),
    ])


# ══════════════════════════════════════════════════════════════════════════
#  ОБЪЁМ МОНЕТ
# ══════════════════════════════════════════════════════════════════════════

def _volume_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Мин. суточный объём монеты ───────────────────────────")]
    for v, d in [
        (100_000,    "100K$"),
        (500_000,    "500K$"),
        (1_000_000,  "1M$ ⭐"),
        (5_000_000,  "5M$"),
        (10_000_000, "10M$"),
        (50_000_000, "50M$"),
    ]:
        rows.append(_btn(_mark(cfg.min_volume_usdt, float(v)) + d, p + "set_volume_" + str(int(v))))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_volume(cur: float)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(); cfg.min_volume_usdt = cur; return _volume_kb(cfg, "", "menu_settings")
def kb_long_volume(user: UserSettings)  -> InlineKeyboardMarkup: return _volume_kb(user.get_long_cfg(),  "long_",  "menu_long_settings")
def kb_short_volume(user: UserSettings) -> InlineKeyboardMarkup: return _volume_kb(user.get_short_cfg(), "short_", "menu_short_settings")


# ══════════════════════════════════════════════════════════════════════════
#  УВЕДОМЛЕНИЯ
# ══════════════════════════════════════════════════════════════════════════

def kb_notify(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Уведомления ─────────────────────────────────────────"),
        _btn(_check(user.notify_signal)   + "  Сигнал входа (TP/SL)",    "toggle_notify_signal"),
        _btn(_check(user.notify_breakout) + "  Пробой уровня (ранний)",  "toggle_notify_breakout"),
        _back("menu_settings"),
    ])


# ══════════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ
# ══════════════════════════════════════════════════════════════════════════

def kb_back()          -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back()])
def kb_back_settings() -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back("menu_settings")])

def kb_subscribe(config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn("💳 30 дней — " + config.PRICE_30_DAYS,  "buy_30"),
        _btn("💳 90 дней — " + config.PRICE_90_DAYS,  "buy_90"),
        _btn("💳 365 дней — " + config.PRICE_365_DAYS, "buy_365"),
        _btn("📩 Написать администратору",              "contact_admin"),
    ])
