"""
keyboards.py — клавиатуры бота v4.1
Каждая опция снабжена описанием что она делает при включении.
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
    if not trend:
        return "🌍 <b>Глобальный тренд:</b> загрузка...\n"
    btc = trend.get("BTC", {})
    eth = trend.get("ETH", {})
    return (
        "🌍 <b>Глобальный тренд (1D):</b>\n"
        + btc.get("emoji", "❓") + " BTC: <b>" + btc.get("trend", "—") + "</b>"
        + "   " + eth.get("emoji", "❓") + " ETH: <b>" + eth.get("trend", "—") + "</b>\n"
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
        _btn("📊 Моя статистика",                                     "my_stats"),
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


# ── TF / Интервал ─────────────────────────────────────────────────────────

# Описания таймфреймов: что означает каждый
_TF_DESCS = {
    "1m":  "1 мин  — скальпинг, много сигналов",
    "5m":  "5 мин  — скальпинг, чуть надёжнее",
    "15m": "15 мин — интрадей ⭐ популярный выбор",
    "30m": "30 мин — интрадей, меньше шума",
    "1h":  "1 час  — свинг, хорошее соотношение R:R",
    "4h":  "4 часа — свинг, высокая надёжность",
    "1d":  "1 день — позиционная, редкие сигналы",
}

def _tf_rows(current: str, prefix: str, back_cb: str) -> list:
    rows = [_noop("── Выбери таймфрейм ──────────────────────────────────────────")]
    for tf, desc in _TF_DESCS.items():
        rows.append(_btn(_mark(current, tf) + desc, prefix + tf))
    rows.append(_back(back_cb))
    return rows


# Описания интервалов сканирования
_INTERVAL_DESCS = {
    300:   "5 мин  — проверяет рынок каждые 5 мин",
    900:   "15 мин — баланс скорости и нагрузки",
    1800:  "30 мин — умеренно, меньше дублей",
    3600:  "1 час  — рекомендуется ⭐",
    7200:  "2 часа — для неспешной торговли",
    14400: "4 часа — редкие, качественные сигналы",
    86400: "1 день — только ежедневные",
}

def _interval_rows(current: int, prefix: str, back_cb: str) -> list:
    rows = [_noop("── Как часто проверять рынок ──────────────────────────────────")]
    for sec, desc in _INTERVAL_DESCS.items():
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


# ── НАСТРОЙКИ ──────────────────────────────────────────────────────────────

def _settings_menu(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Сигналы ──────────────────────────────────────────────────"),
        _btn("📐 Пивоты и уровни S/R",          "menu_" + p + "pivots"),
        _btn("📉 EMA тренд",                     "menu_" + p + "ema"),
        _btn("🔬 Фильтры (RSI / Объём / HTF)",  "menu_" + p + "filters"),
        _btn("⭐ Качество сигнала",               "menu_" + p + "quality"),
        _btn("🔁 Cooldown между сигналами",       "menu_" + p + "cooldown"),
        _noop("── Риск-менеджмент ──────────────────────────────────────────"),
        _btn("🛡 Стоп-лосс (ATR)",               "menu_" + p + "sl"),
        _btn("🎯 Цели (Take Profit R:R)",         "menu_" + p + "targets"),
        _noop("── Монеты ──────────────────────────────────────────────────"),
        _btn("💰 Фильтр монет по объёму",         "menu_" + p + "volume"),
        _noop("── Уведомления ────────────────────────────────────────────"),
        _btn("📱 Уведомления",                    "menu_notify"),
        _back(back_cb),
    ])

def kb_settings()       -> InlineKeyboardMarkup: return _settings_menu("",       "mode_both")
def kb_long_settings()  -> InlineKeyboardMarkup: return _settings_menu("long_",  "mode_long")
def kb_short_settings() -> InlineKeyboardMarkup: return _settings_menu("short_", "mode_short")


# ── ПИВОТЫ ─────────────────────────────────────────────────────────────────

def _pivots_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = []

    rows.append(_noop("── Чувствительность пивотов ──── чем выше, тем меньше пивотов"))
    for v, d in [
        (3,  "3  — много уровней, подходит для скальпинга"),
        (5,  "5  — умеренно, баланс сигналов"),
        (7,  "7  — стандарт ⭐ надёжные пивоты"),
        (10, "10 — сильные уровни для свинга"),
        (15, "15 — только ключевые исторические"),
    ]:
        rows.append(_btn(_mark(cfg.pivot_strength, v) + d, p + "set_pivot_" + str(v)))

    rows.append(_noop("── Макс. возраст уровня ──────────── сколько свечей «живёт» уровень"))
    for v, d in [
        (50,  "50  свечей — только свежие зоны"),
        (100, "100 свечей — стандарт ⭐"),
        (150, "150 свечей"),
        (200, "200 свечей — исторические уровни"),
    ]:
        rows.append(_btn(_mark(cfg.max_level_age, v) + d, p + "set_age_" + str(v)))

    rows.append(_noop("── Макс. ожидание ретеста ─── за сколько свечей ждать возврат к зоне"))
    for v, d in [(10, "10"), (20, "20"), (30, "30 ⭐"), (50, "50")]:
        rows.append(_btn(_mark(cfg.max_retest_bars, v) + str(v) + " свечей — " + d, p + "set_retest_" + str(v)))

    rows.append(_noop("── Буфер зоны (×ATR) ─── расширение зоны для захвата ретеста"))
    for v, d in [
        (0.1, "×0.1  — очень точный вход"),
        (0.2, "×0.2  — тесный буфер"),
        (0.3, "×0.3  — стандарт ⭐"),
        (0.5, "×0.5  — широкий, меньше ложных входов"),
    ]:
        rows.append(_btn(_mark(cfg.zone_buffer, v) + str(v) + " — " + d, p + "set_buffer_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pivots(user: UserSettings)       -> InlineKeyboardMarkup: return _pivots_kb(user.shared_cfg(), "",       "menu_settings")
def kb_long_pivots(user: UserSettings)  -> InlineKeyboardMarkup: return _pivots_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_pivots(user: UserSettings) -> InlineKeyboardMarkup: return _pivots_kb(user.get_short_cfg(), "short_", "mode_short")


# ── EMA ────────────────────────────────────────────────────────────────────

def _ema_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = []

    rows.append(_noop("── Быстрая EMA ──── определяет краткосрочный тренд"))
    for v, d in [
        (20,  "EMA 20  — быстрая реакция, больше шума"),
        (50,  "EMA 50  — стандарт ⭐ хороший баланс"),
        (100, "EMA 100 — медленная, меньше ложных"),
    ]:
        rows.append(_btn(_mark(cfg.ema_fast, v) + d, p + "set_ema_fast_" + str(v)))

    rows.append(_noop("── Медленная EMA ──── определяет глобальный тренд (TF-фон)"))
    for v, d in [
        (100, "EMA 100 — среднесрочный тренд"),
        (200, "EMA 200 — стандарт ⭐ глобальный тренд"),
        (500, "EMA 500 — только мощный тренд"),
    ]:
        rows.append(_btn(_mark(cfg.ema_slow, v) + d, p + "set_ema_slow_" + str(v)))

    rows.append(_noop("── HTF EMA ──── EMA на старшем таймфрейме для фильтра тренда"))
    for v, d in [
        (20,  "20  — быстрый HTF"),
        (50,  "50  — стандарт ⭐"),
        (100, "100 — медленный HTF"),
        (200, "200 — только с трендом на 1D"),
    ]:
        rows.append(_btn(_mark(cfg.htf_ema_period, v) + "EMA " + str(v) + " — " + d, p + "set_htf_ema_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ema(user: UserSettings)       -> InlineKeyboardMarkup: return _ema_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_ema(user: UserSettings)  -> InlineKeyboardMarkup: return _ema_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_ema(user: UserSettings) -> InlineKeyboardMarkup: return _ema_kb(user.get_short_cfg(), "short_", "mode_short")


# ── ФИЛЬТРЫ ────────────────────────────────────────────────────────────────

def _filters_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [
        _noop("── Включи фильтры — каждый добавляет точность, но снижает кол-во"),
        _btn(
            _check(cfg.use_rsi)     + " RSI  — отсекает сигналы в нейтральной зоне",
            p + "toggle_rsi"
        ),
        _btn(
            _check(cfg.use_volume)  + " Объём  — только когда объём выше среднего",
            p + "toggle_volume"
        ),
        _btn(
            _check(cfg.use_pattern) + " Паттерны  — пин-бар / поглощение / молот",
            p + "toggle_pattern"
        ),
        _btn(
            _check(cfg.use_htf)     + " HTF тренд  — сигнал только по тренду 1D",
            p + "toggle_htf"
        ),
        _btn(
            _check(cfg.use_session) + " Прайм-сессии  — только Лондон (07-10 UTC) и NY (13-17 UTC)",
            p + "toggle_session"
        ),
    ]

    rows.append(_noop("── Период RSI ──────── меньше период = быстрее реакция RSI"))
    for v, d in [
        (7,  "RSI 7  — очень чувствительный, скальп"),
        (14, "RSI 14 — стандарт ⭐"),
        (21, "RSI 21 — сглаженный, для свинга"),
    ]:
        rows.append(_btn(_mark(cfg.rsi_period, v) + d, p + "set_rsi_period_" + str(v)))

    rows.append(_noop("── Перекупленность RSI ──── для ШОРТ: продаём когда RSI выше"))
    for v in [60, 65, 70, 75]:
        rows.append(_btn(_mark(cfg.rsi_ob, v) + str(v), p + "set_rsi_ob_" + str(v)))

    rows.append(_noop("── Перепроданность RSI ──── для ЛОНГ: покупаем когда RSI ниже"))
    for v in [25, 30, 35, 40]:
        rows.append(_btn(_mark(cfg.rsi_os, v) + str(v), p + "set_rsi_os_" + str(v)))

    rows.append(_noop("── Объём (множитель) ──── сигнал только если объём ≥ среднего × N"))
    for v, d in [
        (1.0, "×1.0 — любой объём"),
        (1.2, "×1.2 — немного выше среднего ⭐"),
        (1.5, "×1.5 — заметный всплеск"),
        (2.0, "×2.0 — сильный всплеск, меньше сигналов"),
    ]:
        rows.append(_btn(_mark(cfg.vol_mult, v) + d, p + "set_vol_mult_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_filters(user: UserSettings)       -> InlineKeyboardMarkup: return _filters_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_filters(user: UserSettings)  -> InlineKeyboardMarkup: return _filters_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_filters(user: UserSettings) -> InlineKeyboardMarkup: return _filters_kb(user.get_short_cfg(), "short_", "mode_short")


# ── КАЧЕСТВО ───────────────────────────────────────────────────────────────

def _quality_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Минимальный рейтинг сигнала для отправки ──────────────────")]
    for q, d in [
        (1, "⭐          — все сигналы, много шума"),
        (2, "⭐⭐        — слабые условия"),
        (3, "⭐⭐⭐      — рекомендуется ⭐ баланс"),
        (4, "⭐⭐⭐⭐    — строгий отбор, мало сигналов"),
        (5, "⭐⭐⭐⭐⭐  — только идеальные совпадения"),
    ]:
        rows.append(_btn(_mark(cfg.min_quality, q) + d, p + "set_quality_" + str(q)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_quality(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(min_quality=cur); return _quality_kb(cfg, "", "menu_settings")
def kb_long_quality(user: UserSettings)  -> InlineKeyboardMarkup: return _quality_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_quality(user: UserSettings) -> InlineKeyboardMarkup: return _quality_kb(user.get_short_cfg(), "short_", "mode_short")


# ── COOLDOWN ───────────────────────────────────────────────────────────────

def _cooldown_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Пауза после сигнала — не шлёт повторный пока не пройдёт N свечей")]
    for v, d in [
        (3,  "3  свечи  — почти без паузы, для скальпинга"),
        (5,  "5  свечей — стандарт ⭐"),
        (10, "10 свечей — умеренно"),
        (15, "15 свечей — строгий cooldown"),
        (20, "20 свечей — очень редкие сигналы"),
    ]:
        rows.append(_btn(_mark(cfg.cooldown_bars, v) + str(v) + " — " + d, p + "set_cooldown_" + str(v)))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cooldown(cur: int)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(cooldown_bars=cur); return _cooldown_kb(cfg, "", "menu_settings")
def kb_long_cooldown(user: UserSettings)  -> InlineKeyboardMarkup: return _cooldown_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_cooldown(user: UserSettings) -> InlineKeyboardMarkup: return _cooldown_kb(user.get_short_cfg(), "short_", "mode_short")


# ── СТОП-ЛОСС ──────────────────────────────────────────────────────────────

def _sl_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    rows = [_noop("── Период ATR ──── волатильность за N последних свечей")]
    for v, d in [
        (7,  "ATR 7  — быстрый, реагирует на скачки"),
        (14, "ATR 14 — стандарт ⭐"),
        (21, "ATR 21 — сглаженный, стабильнее"),
    ]:
        rows.append(_btn(_mark(cfg.atr_period, v) + d, p + "set_atr_period_" + str(v)))

    rows.append(_noop("── ATR множитель ──── стоп = ATR × N от уровня входа"))
    for v, d in [
        (0.5, "×0.5 — тесный стоп, высокий R:R, но больше выносов"),
        (1.0, "×1.0 — стандарт ⭐"),
        (1.5, "×1.5 — широкий стоп, меньше ложных выносов"),
        (2.0, "×2.0 — очень широкий, для волатильных монет"),
    ]:
        rows.append(_btn(_mark(cfg.atr_mult, v) + d, p + "set_atr_mult_" + str(v)))

    rows.append(_noop("── Макс. риск на сделку (% от депозита) ─────────────────────"))
    for v, d in [
        (0.5, "0.5%  — консервативно"),
        (1.0, "1.0%  — умеренно"),
        (1.5, "1.5%  — стандарт ⭐"),
        (2.0, "2.0%  — агрессивно"),
        (3.0, "3.0%  — высокий риск"),
    ]:
        rows.append(_btn(_mark(cfg.max_risk_pct, v) + d, p + "set_risk_" + str(v)))

    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_sl(user: UserSettings)        -> InlineKeyboardMarkup: return _sl_kb(user.shared_cfg(),    "",       "menu_settings")
def kb_long_sl(user: UserSettings)   -> InlineKeyboardMarkup: return _sl_kb(user.get_long_cfg(),  "long_",  "mode_long")
def kb_short_sl(user: UserSettings)  -> InlineKeyboardMarkup: return _sl_kb(user.get_short_cfg(), "short_", "mode_short")


# ── ЦЕЛИ ───────────────────────────────────────────────────────────────────

def kb_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.shared_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Take Profit цели (общие) — R = риск × множитель ────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить (рекомендуется 1R–1.5R)", "edit_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить (рекомендуется 2R–3R)",   "edit_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить (рекомендуется 3R–5R)",   "edit_tp3"),
        _back("menu_settings"),
    ])

def kb_long_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_long_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Take Profit ЛОНГ ─────────────────────────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_long_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_long_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_long_tp3"),
        _back("mode_long"),
    ])

def kb_short_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_short_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Take Profit ШОРТ ─────────────────────────────────────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_short_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_short_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_short_tp3"),
        _back("mode_short"),
    ])


# ── ОБЪЁМ МОНЕТ ────────────────────────────────────────────────────────────

def _volume_kb(cfg: TradeCfg, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    p = prefix
    opts = [
        (100_000,     "100K$   — альткоины, высокий риск"),
        (500_000,     "500K$   — малая ликвидность"),
        (1_000_000,   "1M$     — стандарт ⭐ хорошая ликвидность"),
        (5_000_000,   "5M$     — топовые альткоины"),
        (10_000_000,  "10M$    — только крупные монеты"),
        (50_000_000,  "50M$    — BTC, ETH, топ-10"),
    ]
    rows = [_noop("── Мин. суточный объём монеты — фильтрует неликвид ────────────")]
    for v, d in opts:
        rows.append(_btn(_mark(cfg.min_volume_usdt, float(v)) + d, p + "set_volume_" + str(int(v))))
    rows.append(_back(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_volume(cur: float)              -> InlineKeyboardMarkup:
    cfg = TradeCfg(min_volume_usdt=cur); return _volume_kb(cfg, "", "menu_settings")
def kb_long_volume(user: UserSettings)  -> InlineKeyboardMarkup: return _volume_kb(user.get_long_cfg(),  "long_",  "menu_long_settings")
def kb_short_volume(user: UserSettings) -> InlineKeyboardMarkup: return _volume_kb(user.get_short_cfg(), "short_", "menu_short_settings")


# ── УВЕДОМЛЕНИЯ ────────────────────────────────────────────────────────────

def kb_notify(user: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Типы уведомлений ─────────────────────────────────────────────"),
        _btn(
            _check(user.notify_signal)   + " Сигнал входа  — основной сигнал с TP/SL",
            "toggle_notify_signal"
        ),
        _btn(
            _check(user.notify_breakout) + " Пробой уровня  — ранний сигнал (без TP/SL)",
            "toggle_notify_breakout"
        ),
        _back("menu_settings"),
    ])


# ── ВСПОМОГАТЕЛЬНЫЕ ──────────────────────────────────────────────────────

def kb_back()          -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back()])
def kb_back_settings() -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back("menu_settings")])

def kb_subscribe(config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn("💳 30 дней — " + config.PRICE_30_DAYS,  "buy_30"),
        _btn("💳 90 дней — " + config.PRICE_90_DAYS,  "buy_90"),
        _btn("💳 365 дней — " + config.PRICE_365_DAYS, "buy_365"),
        _btn("📩 Написать администратору",              "contact_admin"),
    ])
