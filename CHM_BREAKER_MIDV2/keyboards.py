"""
keyboards.py — клавиатуры бота v5 (с описаниями опций)
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


# ── Тренд ──
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


# ── ГЛАВНОЕ МЕНЮ ──
def kb_main(user: UserSettings) -> InlineKeyboardMarkup:
    long_s  = "🟢" if user.long_active  else "⚫"
    short_s = "🟢" if user.short_active else "⚫"
    both_s  = "🟢" if (user.active and user.scan_mode == "both") else "⚫"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(long_s  + " 📈 ЛОНГ сканер  — только сигналы в лонг",  "mode_long"),
        _btn(short_s + " 📉 ШОРТ сканер  — только сигналы в шорт",  "mode_short"),
        _btn(both_s  + " ⚡ ОБА — лонги и шорты одновременно",       "mode_both"),
        _btn("📊 Моя статистика",                                     "my_stats"),
    ])


# ── МЕНЮ ЛОНГ ──
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


# ── МЕНЮ ШОРТ ──
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


# ── МЕНЮ ОБА ──
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


# ── TF / Интервал ──
def _tf_rows(current: str, prefix: str, back_cb: str) -> list:
    tfs = [
        ("1m",  "1 мин — агрессивный скальп, очень много сигналов"),
        ("5m",  "5 мин — скальпинг, высокая частота"),
        ("15m", "15 мин — интрадей, баланс скальп/свинг"),
        ("30m", "30 мин — интрадей, меньше шума"),
        ("1h",  "1 час — свинг, оптимально ⭐ рекомендуем"),
        ("4h",  "4 часа — только сильные движения"),
        ("1d",  "1 день — позиционная торговля"),
    ]
    rows = [_noop("── Выбери таймфрейм ──")]
    for tf, desc in tfs:
        rows.append(_btn(_mark(current, tf) + tf + " — " + desc, prefix + tf))
    rows.append(_back(back_cb))
    return rows


def _interval_rows(current: int, prefix: str, back_cb: str) -> list:
    opts = [
        (300,   "5 мин — мгновенно, нагрузка на API"),
        (900,   "15 мин — быстро, скальпинг"),
        (1800,  "30 мин — стандарт для активной торговли"),
        (3600,  "1 час — оптимальный баланс ⭐ рекомендуем"),
        (7200,  "2 часа — меньше уведомлений"),
        (14400, "4 часа — редкие, качественные сигналы"),
        (86400, "1 день — один раз в сутки"),
    ]
    rows = [_noop("── Интервал сканирования ──")]
    for sec, desc in opts:
        rows.append(_btn(_mark(current, sec) + desc, prefix + str(sec)))
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


# ── НАСТРОЙКИ ──
def _settings_menu(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Сигналы ──────────────────"),
        _btn("📐 Пивоты и уровни S/R",         "menu_" + p + "pivots"),
        _btn("📉 EMA тренд",                    "menu_" + p + "ema"),
        _btn("🔬 Фильтры (RSI / Объём / HTF)", "menu_" + p + "filters"),
        _btn("⭐ Качество сигнала",              "menu_" + p + "quality"),
        _btn("🔁 Cooldown между сигналами",      "menu_" + p + "cooldown"),
        _noop("── Риск-менеджмент ──────────"),
        _btn("🛡 Стоп-лосс (ATR)",              "menu_" + p + "sl"),
        _btn("🎯 Цели (Take Profit R:R)",        "menu_" + p + "targets"),
        _noop("── Монеты ──────────────────"),
        _btn("💰 Фильтр монет по объёму",        "menu_" + p + "volume"),
        _noop("── Уведомления ─────────────"),
        _btn("📱 Уведомления",                   "menu_notify"),
        _back(back_cb),
    ])

def kb_settings()       -> InlineKeyboardMarkup: return _settings_menu("",       "mode_both")
def kb_long_settings()  -> InlineKeyboardMarkup: return _settings_menu("long_",  "mode_long")
def kb_short_settings() -> InlineKeyboardMarkup: return _settings_menu("short_", "mode_short")


# ── ПИВОТЫ ──
def _pivots_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("📐 ПИВОТЫ — уровни поддержки и сопротивления"),
        _noop("Сигнал возникает при отбое или пробое этих уровней"),
        _noop("── Чувствительность (свечей вокруг пика) ──"),
    ]
    for v, d in [
        (3,  "3 — много уровней, включая мелкие"),
        (5,  "5 — умеренно, подходит для скальпа"),
        (7,  "7 — стандарт, баланс точность/частота ⭐"),
        (10, "10 — только сильные структурные уровни"),
        (15, "15 — ключевые зоны, исторические экстремумы"),
    ]:
        rows.append(_btn(_mark(cfg.pivot_strength, v) + d, p + "set_pivot_" + str(v)))

    rows.append(_noop("── Макс. возраст уровня (свечей) ──"))
    rows.append(_noop("Старше этого лимита — уровень игнорируется"))
    for v, d in [
        (50,  "50 — только свежие уровни"),
        (100, "100 — стандарт ⭐"),
        (150, "150 — включает более старые"),
        (200, "200 — исторические зоны"),
    ]:
        rows.append(_btn(_mark(cfg.max_level_age, v) + d, p + "set_age_" + str(v)))

    rows.append(_noop("── Ожидание ретеста (свечей) ──"))
    rows.append(_noop("Как долго ждём возврат цены к уровню"))
    for v, d in [
        (10, "10 — только мгновенный отбой"),
        (20, "20 — быстрый ретест"),
        (30, "30 — стандарт ⭐"),
        (50, "50 — долгое ожидание"),
    ]:
        rows.append(_btn(_mark(cfg.max_retest_bars, v) + str(v) + " свечей — " + d, p + "set_retest_" + str(v)))

    rows.append(_noop("── Буфер зоны (× ATR) ──"))
    rows.append(_noop("Ширина зоны вокруг уровня для захода в позицию"))
    for v, d in [
        (0.1, "x0.1 — точный вход, риск ложного срабатывания"),
        (0.2, "x0.2 — умеренный"),
        (0.3, "x0.3 — стандарт ⭐"),
        (0.5, "x0.5 — широкий, для волатильного рынка"),
    ]:
        rows.append(_btn(_mark(cfg.zone_buffer, v) + str(v) + " — " + d, p + "set_buffer_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pivots(user: UserSettings)       -> InlineKeyboardMarkup: return _pivots_kb(user.shared_cfg(), "",       "menu_settings")
def kb_long_pivots(user: UserSettings)  -> InlineKeyboardMarkup: return _pivots_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_pivots(user: UserSettings) -> InlineKeyboardMarkup: return _pivots_kb(user.get_short_cfg(), "short_", "mode_short")


# ── EMA ──
def _ema_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("📉 EMA — скользящие средние для определения тренда"),
        _noop("Лонг: цена > EMA50 > EMA200. Шорт: цена < EMA50 < EMA200"),
        _noop("── Быстрая EMA (локальный тренд) ──"),
    ]
    for v, d in [
        (20,  "EMA 20 — быстрая реакция, больше сигналов"),
        (50,  "EMA 50 — оптимальный баланс ⭐"),
        (100, "EMA 100 — медленная, только сильный тренд"),
    ]:
        rows.append(_btn(_mark(cfg.ema_fast, v) + d, p + "set_ema_fast_" + str(v)))

    rows.append(_noop("── Медленная EMA (основной тренд) ──"))
    for v, d in [
        (100, "EMA 100 — среднесрочный тренд"),
        (200, "EMA 200 — главный тренд, «золотой крест» ⭐"),
        (500, "EMA 500 — только мощный долгосрочный тренд"),
    ]:
        rows.append(_btn(_mark(cfg.ema_slow, v) + d, p + "set_ema_slow_" + str(v)))

    rows.append(_noop("── HTF EMA (тренд старшего таймфрейма) ──"))
    rows.append(_noop("Если HTF фильтр ВКЛ — сигналы только по тренду 1D"))
    for v, d in [
        (20,  "20 — краткосрочный HTF тренд"),
        (50,  "50 — среднесрочный ⭐"),
        (100, "100 — долгосрочный"),
        (200, "200 — мегатренд, очень строгий фильтр"),
    ]:
        rows.append(_btn(_mark(cfg.htf_ema_period, v) + "EMA " + str(v) + " — " + d, p + "set_htf_ema_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ema(user: UserSettings)       -> InlineKeyboardMarkup: return _ema_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_ema(user: UserSettings)  -> InlineKeyboardMarkup: return _ema_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_ema(user: UserSettings) -> InlineKeyboardMarkup: return _ema_kb(user.get_short_cfg(), "short_", "mode_short")


# ── ФИЛЬТРЫ ──
def _filters_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("🔬 ФИЛЬТРЫ — условия подтверждения сигнала"),
        _noop("✅ = обязательное условие  |  ❌ = условие выключено"),
        _noop("── Включить / выключить ──"),
        _btn(_check(cfg.use_rsi)
             + " RSI — перекуп/перепродажа (лонг <" + str(cfg.rsi_os) + ", шорт >" + str(cfg.rsi_ob) + ")",
             p + "toggle_rsi"),
        _btn(_check(cfg.use_volume)
             + " Объём — повышенный объём подтверждает движение (×" + str(cfg.vol_mult) + " от ср.)",
             p + "toggle_volume"),
        _btn(_check(cfg.use_pattern)
             + " Паттерн — свечной разворот (пин-бар, поглощение, молот и др.)",
             p + "toggle_pattern"),
        _btn(_check(cfg.use_htf)
             + " HTF тренд — сигнал только по тренду 1D (убирает контртрендовые)",
             p + "toggle_htf"),
        _noop("── Период RSI ──"),
        _noop("Чем меньше — чувствительнее. Чем больше — надёжнее"),
    ]
    for v, d in [
        (7,  "RSI 7 — быстрый, много сигналов"),
        (14, "RSI 14 — стандарт Уайлдера ⭐"),
        (21, "RSI 21 — медленный, меньше ложных"),
    ]:
        rows.append(_btn(_mark(cfg.rsi_period, v) + d, p + "set_rsi_period_" + str(v)))

    rows.append(_noop("── Перекупленность RSI — порог шорта ──"))
    for v in [60, 65, 70, 75]:
        labels = {60: "мягко (больше шортов)", 65: "умеренно", 70: "классика ⭐", 75: "строго (мало шортов)"}
        rows.append(_btn(_mark(cfg.rsi_ob, v) + str(v) + " — " + labels[v], p + "set_rsi_ob_" + str(v)))

    rows.append(_noop("── Перепроданность RSI — порог лонга ──"))
    for v in [25, 30, 35, 40]:
        labels = {25: "строго (мало лонгов)", 30: "классика ⭐", 35: "умеренно", 40: "мягко (больше лонгов)"}
        rows.append(_btn(_mark(cfg.rsi_os, v) + str(v) + " — " + labels[v], p + "set_rsi_os_" + str(v)))

    rows.append(_noop("── Объём (множитель к среднему) ──"))
    rows.append(_noop("Объём свечи должен быть в N раз выше среднего"))
    for v, d in [
        (1.0, "x1.0 — любой объём"),
        (1.2, "x1.2 — чуть выше среднего ⭐"),
        (1.5, "x1.5 — заметное повышение"),
        (2.0, "x2.0 — только сильные всплески"),
    ]:
        rows.append(_btn(_mark(cfg.vol_mult, v) + d, p + "set_vol_mult_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_filters(user: UserSettings)       -> InlineKeyboardMarkup: return _filters_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_filters(user: UserSettings)  -> InlineKeyboardMarkup: return _filters_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_filters(user: UserSettings) -> InlineKeyboardMarkup: return _filters_kb(user.get_short_cfg(), "short_", "mode_short")


# ── КАЧЕСТВО ──
def _quality_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("⭐ КАЧЕСТВО — минимальный балл для отправки сигнала"),
        _noop("1 балл базово + по 1 за: объём, паттерн, RSI, тренд/HTF, BOS"),
        _noop("Чем выше порог — тем реже, но точнее сигналы"),
        _noop("── Минимальный балл ──"),
    ]
    descs = [
        (1, "⭐☆☆☆☆  — все сигналы (для изучения рынка)"),
        (2, "⭐⭐☆☆☆ — с одним подтверждением"),
        (3, "⭐⭐⭐☆☆ — рекомендуем, баланс ⭐"),
        (4, "⭐⭐⭐⭐☆ — строгий, 3+ подтверждения"),
        (5, "⭐⭐⭐⭐⭐ — только идеальные точки входа"),
    ]
    for q, d in descs:
        rows.append(_btn(_mark(cfg.min_quality, q) + d, p + "set_quality_" + str(q)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_quality(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(min_quality=cur); return _quality_kb(cfg, "", "menu_settings")
def kb_long_quality(user: UserSettings)  -> InlineKeyboardMarkup: return _quality_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_quality(user: UserSettings) -> InlineKeyboardMarkup: return _quality_kb(user.get_short_cfg(), "short_", "mode_short")


# ── COOLDOWN ──
def _cooldown_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("🔁 COOLDOWN — пауза между сигналами по одной монете"),
        _noop("Предотвращает дублирование сигналов в одном движении"),
        _noop("── Количество свечей паузы ──"),
    ]
    for v, d in [
        (3,  "3 — агрессивно, часто"),
        (5,  "5 — стандарт ⭐"),
        (10, "10 — умеренно"),
        (15, "15 — редко"),
        (20, "20 — только крупные отдельные движения"),
    ]:
        rows.append(_btn(_mark(cfg.cooldown_bars, v) + str(v) + " свечей — " + d, p + "set_cooldown_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cooldown(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(cooldown_bars=cur); return _cooldown_kb(cfg, "", "menu_settings")
def kb_long_cooldown(user: UserSettings)  -> InlineKeyboardMarkup: return _cooldown_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_cooldown(user: UserSettings) -> InlineKeyboardMarkup: return _cooldown_kb(user.get_short_cfg(), "short_", "mode_short")


# ── СТОП-ЛОСС ──
def _sl_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("🛡 СТОП-ЛОСС — рассчитывается через ATR (волатильность)"),
        _noop("SL = уровень ± (ATR × множитель). Чем шире — меньше случайных стопов"),
        _noop("── Период ATR (свечей) ──"),
    ]
    for v, d in [
        (7,  "ATR 7 — быстрый, реагирует на текущую волатильность"),
        (14, "ATR 14 — стандарт Уайлдера ⭐"),
        (21, "ATR 21 — медленный, сглаженный"),
    ]:
        rows.append(_btn(_mark(cfg.atr_period, v) + d, p + "set_atr_period_" + str(v)))

    rows.append(_noop("── ATR множитель (ширина стопа) ──"))
    for v, d in [
        (0.5, "x0.5 — близкий стоп, малый риск но много стопов"),
        (1.0, "x1.0 — стандарт ⭐"),
        (1.5, "x1.5 — широкий, меньше случайных стопов"),
        (2.0, "x2.0 — очень широкий, для трендового рынка"),
    ]:
        rows.append(_btn(_mark(cfg.atr_mult, v) + d, p + "set_atr_mult_" + str(v)))

    rows.append(_noop("── Макс. риск на сделку (% от депо) ──"))
    rows.append(_noop("Ограничивает стоп если ATR даёт слишком широкий риск"))
    for v, d in [
        (0.5, "0.5% — очень консервативно"),
        (1.0, "1.0% — консервативно"),
        (1.5, "1.5% — стандарт ⭐"),
        (2.0, "2.0% — агрессивно"),
        (3.0, "3.0% — высокий риск"),
    ]:
        rows.append(_btn(_mark(cfg.max_risk_pct, v) + d, p + "set_risk_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_sl(user: UserSettings)        -> InlineKeyboardMarkup: return _sl_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_sl(user: UserSettings)   -> InlineKeyboardMarkup: return _sl_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_sl(user: UserSettings)  -> InlineKeyboardMarkup: return _sl_kb(user.get_short_cfg(), "short_", "mode_short")


# ── ЦЕЛИ ──
def kb_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.shared_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("🎯 ЦЕЛИ — коэффициенты Risk:Reward для Take Profit"),
        _noop("1R = расстояние вход→стоп. Цель 2R = вдвое дальше стопа"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_tp3"),
        _back("menu_settings"),
    ])

def kb_long_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_long_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("🎯 ЦЕЛИ ЛОНГ — R:R коэффициенты"),
        _noop("Рекомендуем: 1R / 2R / 3R или 0.8R / 1.5R / 2.5R"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_long_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_long_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_long_tp3"),
        _back("mode_long"),
    ])

def kb_short_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_short_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("🎯 ЦЕЛИ ШОРТ — R:R коэффициенты"),
        _noop("Рекомендуем: 1R / 2R / 3R или 0.8R / 1.5R / 2.5R"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_short_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_short_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_short_tp3"),
        _back("mode_short"),
    ])


# ── ОБЪЁМ ──
def _volume_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    opts = [
        (100_000,    "100К$ — мелкие альткоины"),
        (500_000,    "500К$ — средние монеты"),
        (1_000_000,  "1М$ — ликвидные монеты ⭐"),
        (5_000_000,  "5М$ — топ монеты, меньше проскальзывания"),
        (10_000_000, "10М$ — только крупняк"),
        (50_000_000, "50М$ — BTC, ETH, топ-10"),
    ]
    rows = [
        _noop("💰 ОБЪЁМ — мин. суточный объём монеты в USDT"),
        _noop("Больше объём = выше ликвидность = меньше проскальзывание"),
        _noop("── Минимальный суточный объём ──"),
    ]
    for v, d in opts:
        rows.append(_btn(_mark(cfg.min_volume_usdt, float(v)) + d, p + "set_volume_" + str(int(v))))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_volume(cur: float)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(min_volume_usdt=cur); return _volume_kb(cfg, "", "menu_settings")
def kb_long_volume(user: UserSettings)  -> InlineKeyboardMarkup: return _volume_kb(user.get_long_cfg(),  "long_",  "menu_long_settings")
def kb_short_volume(user: UserSettings) -> InlineKeyboardMarkup: return _volume_kb(user.get_short_cfg(), "short_", "menu_short_settings")


# ── УВЕДОМЛЕНИЯ ──
def kb_notify(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("📱 УВЕДОМЛЕНИЯ — какие события получать"),
        _noop("── Типы уведомлений ──"),
        _btn(_check(user.notify_signal)
             + " Сигнал входа — полный сигнал с TP/SL и чеклистом подтверждений",
             "toggle_notify_signal"),
        _btn(_check(user.notify_breakout)
             + " Пробой уровня — ранний сигнал при пробое (без точных уровней)",
             "toggle_notify_breakout"),
        _back("menu_settings"),
    ])


# ── ВСПОМОГАТЕЛЬНЫЕ ──
def kb_back()          -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back()])
def kb_back_settings() -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back("menu_settings")])

def kb_subscribe(config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn("💳 30 дней — " + config.PRICE_30_DAYS, "buy_30"),
        _btn("💳 90 дней — " + config.PRICE_90_DAYS, "buy_90"),
        _btn("💳 365 дней — " + config.PRICE_365_DAYS, "buy_365"),
        _btn("📩 Написать администратору", "contact_admin"),
    ])
