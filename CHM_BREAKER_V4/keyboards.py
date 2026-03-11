"""
keyboards.py — клавиатуры бота v5 (без триала, полные настройки)
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from user_manager import UserSettings, TradeCfg, SMCUserCfg


def _btn(text: str, cb: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data=cb)]

def _back(cb: str = "back_main") -> list:
    return [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)]

def _noop(text: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data="noop")]

def _check(v: bool) -> str:
    return "✅" if v else "❌"

def _mark(current, val) -> str:
    try:
        return "◉ " if round(float(current), 6) == round(float(val), 6) else "○ "
    except (TypeError, ValueError):
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

# ── Авто-трейдинг ────────────────────────────────────

def _auto_trade_label(user: UserSettings) -> str:
    at = getattr(user, "auto_trade", False)
    return "💹✅" if at else "💹"


def kb_auto_trade(user: UserSettings) -> InlineKeyboardMarkup:
    """Меню авто-трейдинга Bybit."""
    at        = getattr(user, "auto_trade",       False)
    mode      = getattr(user, "auto_trade_mode",  "confirm")
    risk      = getattr(user, "trade_risk_pct",   1.0)
    lev       = getattr(user, "trade_leverage",   10)
    max_tr    = getattr(user, "max_trades_limit",  5)
    has_key   = bool(getattr(user, "bybit_api_key", ""))

    status_label = "🟢 ВКЛ — нажать чтобы выключить" if at else "🔴 ВЫКЛ — нажать чтобы включить"
    mode_label   = ("🤖 Авто (открывать сразу)"       if mode == "auto"
                    else "👆 С подтверждением (кнопка)")
    key_label    = "🔑 Ключи: ✅ подключены" if has_key else "🔑 Ключи: ❌ не настроены"

    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── 💹 Авто-трейдинг Bybit ──────────────────────"),
        _btn(status_label,                                          "toggle_auto_trade"),
        _noop("── Режим входа ────────────────────────────────────"),
        _btn(("◉ " if mode == "confirm" else "○ ") + "👆 Кнопка подтверждения",  "set_at_mode_confirm"),
        _btn(("◉ " if mode == "auto"    else "○ ") + "🤖 Авто-вход (без кнопки)", "set_at_mode_auto"),
        _noop("── Риск на сделку (% от баланса) ─────────────────"),
        *[_btn(("◉ " if risk == r else "○ ") + f"{r}%", f"set_at_risk_{r}")
          for r in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]],
        _noop("── Плечо ───────────────────────────────────────────"),
        *[_btn(("◉ " if lev == l else "○ ") + f"x{l}", f"set_at_lev_{l}")
          for l in [3, 5, 10, 15, 20, 25, 50]],
        _noop("── Лимит открытых сделок ───────────────────────────"),
        *[_btn(("◉ " if max_tr == n else "○ ") + f"{n} сделок", f"set_at_maxtr_{n}")
          for n in [1, 2, 3, 5, 10, 15, 20, 30, 50]],
        _btn(f"✏️ Своё значение (сейчас: {max_tr})", "set_at_maxtr_custom"),
        _noop("── API ключи ────────────────────────────────────────"),
        _btn(key_label,              "setup_bybit_api"),
        _btn("🧪 Проверить соединение", "test_bybit_api") if has_key else _noop("── Введи ключи для проверки ──"),
        _btn("🗑 Удалить ключи",      "remove_bybit_api") if has_key else _noop("──────────────────────────────────"),
        _back(),
    ])


# ── ГЛАВНОЕ МЕНЮ ─────────────────────────────────────

def _watch_coin_label(user) -> str:
    wc = getattr(user, "watch_coin", "").strip()
    if wc:
        base = wc.replace("-USDT-SWAP", "").replace("-USDT", "")
        return f"🎯 Монета: {base} — сменить / сбросить"
    return "🎯 Мониторить одну монету — все / выбрать"


def _quick_start_label(user: UserSettings) -> str:
    strategy = getattr(user, "strategy", "LEVELS")
    if strategy == "SMC":
        both_on = getattr(user, "smc_long_active", False) and getattr(user, "smc_short_active", False)
    else:
        both_on = user.long_active and user.short_active
    return "🟢 Сканер работает — остановить" if both_on else "🚀 БЫСТРЫЙ СТАРТ — включить все сканеры"


def kb_main(user: UserSettings) -> InlineKeyboardMarkup:
    strategy = getattr(user, "strategy", "LEVELS")
    if strategy == "SMC":
        long_s  = "🟢" if getattr(user, "smc_long_active",  False) else "⚫"
        short_s = "🟢" if getattr(user, "smc_short_active", False) else "⚫"
        both_s  = "🟢" if (user.active and user.scan_mode == "smc_both") else "⚫"
        return InlineKeyboardMarkup(inline_keyboard=[
            _btn(_quick_start_label(user),                                         "quick_start"),
            _btn(long_s  + " 📈 SMC ЛОНГ — только лонговые сигналы",             "mode_smc_long"),
            _btn(short_s + " 📉 SMC ШОРТ — только шортовые сигналы",             "mode_smc_short"),
            _btn(both_s  + " ⚡ SMC ОБА — все SMC сигналы",                       "mode_smc_both"),
            _btn("🎯 Стратегия: 🧠 SMC — сменить",                                "show_strategy"),
            [
                InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"),
                InlineKeyboardButton(text="📈 График",          callback_data="my_chart"),
            ],
            _btn(_watch_coin_label(user),                                          "watch_coin_menu"),
            _btn("🔍 Анализ монеты — разовый сигнал по запросу",                  "analyze_coin"),
            _btn(_auto_trade_label(user) + " Авто-трейдинг Bybit",                "auto_trade_menu"),
            _btn("🎰 Памп/Дамп детектор (BingX)",                              "pd_menu"),
            _btn("👥 Реферальная программа — пригласить друга",                 "my_referral"),
            _btn("❓ Справка — что делает каждая кнопка",                          "help_show"),
        ])
    # ── LEVELS (default) ──
    long_s  = "🟢" if user.long_active  else "⚫"
    short_s = "🟢" if user.short_active else "⚫"
    both_s  = "🟢" if (user.active and user.scan_mode == "both") else "⚫"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(_quick_start_label(user),                                     "quick_start"),
        _btn(long_s  + " 📈 ЛОНГ сканер  — только сигналы в лонг",       "mode_long"),
        _btn(short_s + " 📉 ШОРТ сканер  — только сигналы в шорт",       "mode_short"),
        _btn(both_s  + " ⚡ ОБА — лонги и шорты одновременно",             "mode_both"),
        _btn("🎯 Стратегия: 📊 Уровни — сменить",                          "show_strategy"),
        [
            InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats"),
            InlineKeyboardButton(text="📈 График",          callback_data="my_chart"),
        ],
        _btn(_watch_coin_label(user),                                      "watch_coin_menu"),
        _btn("🔍 Анализ монеты — разовый сигнал по запросу",               "analyze_coin"),
        _btn(_auto_trade_label(user) + " Авто-трейдинг Bybit",             "auto_trade_menu"),
        _btn("🎰 Памп/Дамп детектор (BingX)",                              "pd_menu"),
        _btn("👥 Реферальная программа — пригласить друга",                "my_referral"),
        _btn("❓ Справка — что делает каждая кнопка",                       "help_show"),
    ])


# ── МЕНЮ ЛОНГ ────────────────────────────────────────

def kb_mode_long(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_long_cfg()
    status = "🟢 ЛОНГ ВКЛЮЧЁН — нажми чтобы остановить" if user.long_active \
           else "🔴 ЛОНГ ВЫКЛЮЧЕН — нажми чтобы запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                "toggle_long"),
        _btn("📊 Таймфрейм: " + cfg.timeframe,                     "menu_long_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval//60) + " мин.", "menu_long_interval"),
        _btn("⚙️ Все настройки ЛОНГ →",                            "menu_long_settings"),
        _btn("🔁 Сбросить настройки ЛОНГ к общим",                 "reset_long_cfg"),
        _back(),
    ])


# ── МЕНЮ ШОРТ ────────────────────────────────────────

def kb_mode_short(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_short_cfg()
    status = "🟢 ШОРТ ВКЛЮЧЁН — нажми чтобы остановить" if user.short_active \
           else "🔴 ШОРТ ВЫКЛЮЧЕН — нажми чтобы запустить"
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                 "toggle_short"),
        _btn("📊 Таймфрейм: " + cfg.timeframe,                      "menu_short_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval//60) + " мин.", "menu_short_interval"),
        _btn("⚙️ Все настройки ШОРТ →",                             "menu_short_settings"),
        _btn("🔁 Сбросить настройки ШОРТ к общим",                  "reset_short_cfg"),
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
    rows.append(_noop("── Буфер зоны (ATR кластеризация) ───────────────"))
    for v, d in [(0.1,"x0.1"), (0.2,"x0.2"), (0.3,"x0.3 ⭐"), (0.5,"x0.5")]:
        rows.append(_btn(_mark(cfg.zone_buffer, v) + str(v) + " — " + d, p + "set_buffer_" + str(v)))
    rows.append(_noop("── Ширина зоны уровня (% от цены) ───────────────"))
    for v, d in [(0.3,"0.3% — точно"), (0.5,"0.5% — умеренно"), (0.7,"0.7% — стандарт ⭐"), (1.0,"1.0% — широко"), (1.5,"1.5% — для альтов")]:
        rows.append(_btn(_mark(cfg.zone_pct, v) + str(v) + "% — " + d, p + "set_zone_pct_" + str(v)))
    rows.append(_noop("── Макс. дистанция до уровня (%) ────────────────"))
    for v, d in [(0.5,"0.5% — только у уровня"), (1.0,"1.0% — строго"), (1.5,"1.5% — рекомендуется ⭐"), (2.0,"2.0% — мягко"), (3.0,"3.0% — широко")]:
        rows.append(_btn(_mark(cfg.max_dist_pct, v) + str(v) + "% — " + d, p + "set_dist_pct_" + str(v)))
    rows.append(_noop("── Макс. тестов уровня (потом пробой) ───────────"))
    for v, d in [(2,"2 — очень строго"), (3,"3 — строго"), (4,"4 — рекомендуется ⭐"), (5,"5 — мягко"), (99,"без лимита")]:
        rows.append(_btn(_mark(cfg.max_level_tests, v) + str(v) + " — " + d, p + "set_max_tests_" + str(v)))
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

def _min_rr_rows(cfg: TradeCfg, prefix: str) -> list:
    rows = [_noop("── Минимальный R:R для входа ─────────────────────")]
    for v, d in [(1.5,"1.5R — мягко"), (2.0,"2.0R — рекомендуется ⭐"), (2.5,"2.5R — строго"), (3.0,"3.0R — только лучшие")]:
        rows.append(_btn(_mark(cfg.min_rr, v) + str(v) + "R — " + d, prefix + "set_min_rr_" + str(v)))
    return rows

def kb_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.shared_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Минимальный R:R для входа ─────────────────────"),
        *_min_rr_rows(cfg, ""),
        _noop("── Цели Take Profit (fallback если нет уровня) ───"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_tp3"),
        _back("menu_settings"),
    ])

def kb_long_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_long_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Минимальный R:R ЛОНГ ─────────────────────────"),
        *_min_rr_rows(cfg, "long_"),
        _noop("── Цели Take Profit ЛОНГ (fallback) ─────────────"),
        _btn("🎯 Цель 1: " + str(cfg.tp1_rr) + "R — изменить", "edit_long_tp1"),
        _btn("🎯 Цель 2: " + str(cfg.tp2_rr) + "R — изменить", "edit_long_tp2"),
        _btn("🏆 Цель 3: " + str(cfg.tp3_rr) + "R — изменить", "edit_long_tp3"),
        _back("mode_long"),
    ])

def kb_short_targets(user: UserSettings) -> InlineKeyboardMarkup:
    cfg = user.get_short_cfg()
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── Минимальный R:R ШОРТ ─────────────────────────"),
        *_min_rr_rows(cfg, "short_"),
        _noop("── Цели Take Profit ШОРТ (fallback) ─────────────"),
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


# ── ПОДПИСКА — ВЫБОР ТАРИФА ───────────────────────────

def kb_subscribe(config=None) -> InlineKeyboardMarkup:
    """Меню выбора тарифа при старте."""
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── 🤖 CHM BREAKER BOT ──────────────────────"),
        _btn("📅 3 месяца — 290$", "plan_bot_90"),
        _btn("📅 1 ГОД    — 990$", "plan_bot_365"),
        _noop("── 🎁 Специальные предложения ─────────────────"),
        [InlineKeyboardButton(
            text="💎 Бот + Лаба — написать @crypto_chm",
            url="https://t.me/crypto_chm"
        )],
        _btn("🎟 Ввести промокод (тестовый доступ)", "enter_promo"),
    ])


def kb_payment(plan_label: str, amount: str, address: str) -> InlineKeyboardMarkup:
    """Инструкция по оплате после выбора тарифа."""
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── После оплаты напиши администратору ──────────"),
        [InlineKeyboardButton(
            text="✍️ Написать @crypto_chm (после оплаты)",
            url="https://t.me/crypto_chm"
        )],
        _btn("◀️ Назад к тарифам", "show_plans"),
    ])


def kb_contact_admin() -> InlineKeyboardMarkup:
    """Кнопка «Написать администратору»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать администратору @crypto_chm", url="https://t.me/crypto_chm")],
    ])


def kb_back()          -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back()])
def kb_back_settings() -> InlineKeyboardMarkup: return InlineKeyboardMarkup(inline_keyboard=[_back("menu_settings")])

def kb_back_photo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_photo_main")]
    ])


# ── SMC НАСТРОЙКИ ─────────────────────────────────────

def kb_smc_main(user: UserSettings) -> InlineKeyboardMarkup:
    """Главное меню SMC — показывает текущие значения и ссылки на подменю."""
    cfg = user.get_smc_cfg()
    dir_label = {"LONG": "📈 Только ЛОНГ", "SHORT": "📉 Только ШОРТ", "BOTH": "⚡ ОБА"}.get(cfg.direction, "⚡ ОБА")
    tf_label  = {"15m": "15 мин", "1H": "1 час ⭐", "4H": "4 часа"}.get(cfg.tf_key, cfg.tf_key)
    interval_min = cfg.scan_interval // 60
    return InlineKeyboardMarkup(inline_keyboard=[
        _noop("── 🧠 Smart Money Concepts ─────────────────────"),
        _btn("📊 Таймфрейм: "        + tf_label,              "smc_menu_tf"),
        _btn("🔄 Интервал: "          + str(interval_min) + " мин.", "smc_menu_interval"),
        _btn("🎯 Направление: "       + dir_label,             "smc_menu_direction"),
        _noop("── Фильтры сигнала ──────────────────────────────"),
        _btn("⭐ Мин. подтверждений: " + str(cfg.min_confirmations) + "/5",  "smc_menu_confirmations"),
        _btn("📐 Мин. R:R: 1:"        + str(cfg.min_rr),       "smc_menu_rr"),
        _btn("🛡 Буфер SL: "          + str(cfg.sl_buffer_pct) + "%", "smc_menu_sl"),
        _btn("💰 Мин. объём: "        + _fmt_vol(cfg.min_volume_usdt), "smc_menu_volume"),
        _noop("── Включить / Выключить ─────────────────────────"),
        _btn(_check(cfg.fvg_enabled)      + " FVG / IFVG",           "smc_toggle_fvg"),
        _btn(_check(cfg.choch_enabled)    + " CHoCH (смена тренда)",  "smc_toggle_choch"),
        _btn(_check(cfg.ob_use_breaker)   + " Breaker Blocks",        "smc_toggle_breaker"),
        _btn(_check(cfg.sweep_close_req)  + " Sweep: закрытие за уровнем", "smc_toggle_sweep"),
        _noop("── OB ─────────────────────────────────────────────"),
        _btn("🕯 Макс. возраст OB: " + str(cfg.ob_max_age) + " свечей", "smc_menu_ob_age"),
        _back("back_main"),
    ])


def _fmt_vol(v: float) -> str:
    if v >= 1_000_000: return str(int(v // 1_000_000)) + "M$"
    if v >= 1_000:     return str(int(v // 1_000)) + "K$"
    return str(int(v)) + "$"


def kb_smc_tf(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    opts = [("15m", "15 мин — скальпинг"), ("1H", "1 час — свинг ⭐"), ("4H", "4 часа — позиционная")]
    rows = [_noop("── Основной таймфрейм SMC (MTF) ─────────────────")]
    for v, d in opts:
        rows.append(_btn(_mark(cfg.tf_key, v) + v + " — " + d, "smc_set_tf_" + v))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_interval(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    opts = [(300,"5 мин"),(600,"10 мин"),(900,"15 мин ⭐"),(1800,"30 мин"),(3600,"1 час")]
    rows = [_noop("── Интервал сканирования ─────────────────────────")]
    for sec, d in opts:
        rows.append(_btn(_mark(cfg.scan_interval, sec) + d, "smc_set_interval_" + str(sec)))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_direction(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    opts = [("BOTH","⚡ ОБА направления ⭐"),("LONG","📈 Только ЛОНГ"),("SHORT","📉 Только ШОРТ")]
    rows = [_noop("── Направление сигналов ──────────────────────────")]
    for v, d in opts:
        rows.append(_btn(_mark(cfg.direction, v) + d, "smc_set_dir_" + v))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_confirmations(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    rows = [_noop("── Мин. подтверждений из 5 ──────────────────────")]
    for v, d in [(2,"2 — мягко"),(3,"3 — рекомендуется ⭐"),(4,"4 — строго"),(5,"5 — только идеальные")]:
        rows.append(_btn(_mark(cfg.min_confirmations, v) + str(v) + " — " + d, "smc_set_conf_" + str(v)))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_rr(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    rows = [_noop("── Минимальный R:R ───────────────────────────────")]
    for v, d in [(1.5,"1.5R"),(2.0,"2.0R ⭐"),(2.5,"2.5R"),(3.0,"3.0R — строго")]:
        rows.append(_btn(_mark(cfg.min_rr, v) + str(v) + "R — " + d, "smc_set_rr_" + str(v)))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_sl(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    rows = [_noop("── Буфер SL от экстремума OB ────────────────────")]
    for v, d in [(0.1,"0.1% — очень плотный"),(0.15,"0.15% ⭐"),(0.25,"0.25% — мягкий"),(0.5,"0.5% — широкий")]:
        rows.append(_btn(_mark(cfg.sl_buffer_pct, v) + str(v) + "% — " + d, "smc_set_sl_" + str(v)))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_volume(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    opts = [(1_000_000,"1M$"),(5_000_000,"5M$ ⭐"),(10_000_000,"10M$"),(50_000_000,"50M$")]
    rows = [_noop("── Мин. суточный объём монеты ───────────────────")]
    for v, d in opts:
        rows.append(_btn(_mark(cfg.min_volume_usdt, float(v)) + d, "smc_set_vol_" + str(int(v))))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_smc_ob_age(cfg: SMCUserCfg) -> InlineKeyboardMarkup:
    rows = [_noop("── Макс. возраст Order Block (в свечах) ─────────")]
    for v, d in [(20,"20 — только свежие"),(30,"30 — актуальные"),(50,"50 ⭐"),(100,"100 — исторические")]:
        rows.append(_btn(_mark(cfg.ob_max_age, v) + str(v) + " свечей — " + d, "smc_set_ob_age_" + str(v)))
    rows.append(_back("smc_settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── SMC РЕЖИМ ЛОНГ / ШОРТ / ОБА ──────────────────────

def kb_smc_mode_long(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_smc_cfg()
    active = getattr(user, "smc_long_active", False)
    status = "🟢 SMC ЛОНГ ВКЛЮЧЁН — нажми чтобы остановить" if active \
           else "🔴 SMC ЛОНГ ВЫКЛЮЧЕН — нажми чтобы запустить"
    tf_label = {"15m":"15 мин","1H":"1 час ⭐","4H":"4 часа"}.get(cfg.tf_key, cfg.tf_key)
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                       "toggle_smc_long"),
        _btn("📊 Таймфрейм: " + tf_label,                                 "smc_menu_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval // 60) + " мин.",    "smc_menu_interval"),
        _btn("⚙️ Все настройки SMC →",                                    "smc_settings"),
        _back(),
    ])


def kb_smc_mode_short(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_smc_cfg()
    active = getattr(user, "smc_short_active", False)
    status = "🟢 SMC ШОРТ ВКЛЮЧЁН — нажми чтобы остановить" if active \
           else "🔴 SMC ШОРТ ВЫКЛЮЧЕН — нажми чтобы запустить"
    tf_label = {"15m":"15 мин","1H":"1 час ⭐","4H":"4 часа"}.get(cfg.tf_key, cfg.tf_key)
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                       "toggle_smc_short"),
        _btn("📊 Таймфрейм: " + tf_label,                                 "smc_menu_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval // 60) + " мин.",    "smc_menu_interval"),
        _btn("⚙️ Все настройки SMC →",                                    "smc_settings"),
        _back(),
    ])


def kb_smc_mode_both(user: UserSettings) -> InlineKeyboardMarkup:
    cfg    = user.get_smc_cfg()
    active = user.active and user.scan_mode == "smc_both"
    status = "🟢 SMC ОБА ВКЛЮЧЁН — нажми чтобы остановить" if active \
           else "🔴 SMC ОБА ВЫКЛЮЧЕН — нажми чтобы запустить"
    tf_label = {"15m":"15 мин","1H":"1 час ⭐","4H":"4 часа"}.get(cfg.tf_key, cfg.tf_key)
    return InlineKeyboardMarkup(inline_keyboard=[
        _btn(status,                                                       "toggle_smc_both"),
        _btn("📊 Таймфрейм: " + tf_label,                                 "smc_menu_tf"),
        _btn("🔄 Интервал: " + str(cfg.scan_interval // 60) + " мин.",    "smc_menu_interval"),
        _btn("⚙️ Все настройки SMC →",                                    "smc_settings"),
        _back(),
    ])


# ── СПРАВКА ───────────────────────────────────────────

def help_text() -> str:
    return (
        "❓ <b>СПРАВКА — CHM BREAKER BOT</b>\n\n"

        "━━ <b>ГЛАВНЫЕ КНОПКИ</b> ━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>БЫСТРЫЙ СТАРТ</b> — запускает ЛОНГ + ШОРТ сканеры одной кнопкой.\n"
        "   Повторное нажатие останавливает оба сканера.\n"
        "📈 <b>ЛОНГ / 📉 ШОРТ</b> — раздельные настройки для каждого направления.\n"
        "   Используй если нужны разные параметры на лонг и шорт.\n"
        "⚡ <b>ОБА</b> — единые настройки сразу для лонга и шорта (рекомендуется).\n"
        "🎯 <b>Стратегия</b> — LEVELS (уровни S/R) или SMC (Smart Money Concepts).\n\n"

        "━━ <b>НАСТРОЙКИ — РЕКОМЕНДАЦИИ</b> ━━━━━━\n"
        "📊 <b>Таймфрейм</b> — период свечей для поиска сигналов.\n"
        "   ⭐ Оптимально: <b>1H</b> — лучший баланс частоты и качества.\n"
        "   Скальп: 5m–15m. Позиционная торговля: 4H–1D.\n\n"
        "🔄 <b>Интервал</b> — частота проверки рынка ботом.\n"
        "   ⭐ Рекомендуется: <b>1 час</b> (соответствует таймфрейму 1H).\n\n"
        "📐 <b>Пивоты</b> — ширина «окна» для поиска уровней поддержки/сопротивления.\n"
        "   ⭐ Рекомендуется: <b>7</b>. Больше = сильнее уровни, меньше сигналов.\n\n"
        "📉 <b>EMA тренд</b> — пара EMA для определения направления тренда.\n"
        "   ⭐ Рекомендуется: быстрая <b>50</b>, медленная <b>200</b>.\n\n"

        "━━ <b>ФИЛЬТРЫ</b> ━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>RSI OB/OS</b> — блокирует вход при перекупе и перепроданности.\n"
        "   ⭐ Рекомендуется: OB = <b>65</b>, OS = <b>35</b>.\n"
        "   Строже: OB=60/OS=40 — меньше сигналов, но чище.\n\n"
        "📦 <b>Объём ×</b> — вход только при объёме выше среднего.\n"
        "   ⭐ Рекомендуется: <b>×1.2</b>. При ×1.5+ — только сильные движения.\n\n"
        "🕯 <b>Паттерны</b> — пин-бары и поглощения как доп. подтверждение (+1⭐).\n"
        "🌐 <b>HTF тренд</b> — 1D тренд должен совпадать с направлением (+1⭐).\n"
        "➡️ <b>Только по тренду</b> — убирает контртрендовые входы.\n\n"

        "━━ <b>КАЧЕСТВО СИГНАЛА ⭐</b> ━━━━━━━━━━━\n"
        "Минимальный порог для отправки сигнала (1–6):\n"
        "  +2 базовые ⭐ за любой сигнал\n"
        "  +1 ⭐ объём выше нормы\n"
        "  +1 ⭐ направление по тренду EMA\n"
        "  +1 ⭐ паттерн свечи (пин-бар / поглощение)\n"
        "  +1 ⭐ совпадение с HTF (1D) трендом\n"
        "   ⭐ Рекомендуется порог: <b>3⭐</b>.\n"
        "   4–5⭐ — только топовые сигналы (меньше, но надёжнее).\n\n"

        "━━ <b>СТОП-ЛОСС и ЦЕЛИ</b> ━━━━━━━━━━━━━\n"
        "🛡 <b>ATR множитель</b> — стоп ставится за уровень + N × ATR (волатильность).\n"
        "   ⭐ Рекомендуется: <b>×1.5</b> — стоп за структуру, вне рыночного шума.\n"
        "   ×1.0 — ближе к цене (риск выбивания шумом).\n"
        "   ×2.0 — очень широко (для высоковолатильных монет).\n\n"
        "🎯 <b>Цели R:R</b> — соотношение прибыль/риск для каждой цели.\n"
        "   ⭐ Рекомендуется: TP1 = <b>2R</b>, TP2 = <b>3R</b>, TP3 = <b>4.5R</b>.\n\n"

        "━━ <b>АВТО-ТРЕЙДИНГ (Bybit)</b> ━━━━━━━━━\n"
        "💹 <b>Вкл/Выкл</b> — включает автоматическое открытие сделок на Bybit.\n"
        "👆 <b>С подтверждением</b> — бот присылает сигнал с кнопкой «Открыть сделку».\n"
        "🤖 <b>Авто-вход</b> — открывает позицию сразу, без нажатий.\n"
        "💰 <b>Риск</b>: ⭐ <b>1–2%</b> от депозита — безопасно. 3–5% — агрессивно.\n"
        "📊 <b>Плечо</b>: ⭐ <b>5–10x</b> — оптимально. 20x+ — для опытных.\n"
        "🔢 <b>Лимит сделок</b>: ⭐ <b>3–5</b> одновременно.\n\n"
        "♻️ <b>3 тейка + Безубыток</b>:\n"
        "   Позиция делится на 3 части: TP1/TP2/TP3 по 1/3 каждый.\n"
        "   После достижения TP1 стоп <b>автоматически</b> переносится на вход.\n"
        "   Дальнейший риск по позиции = 0.\n\n"

        "━━ <b>МОНИТОРИНГ МОНЕТЫ</b> ━━━━━━━━━━━━\n"
        "🎯 <b>Мониторить монету</b> — сигналы только по одной выбранной паре.\n\n"

        "━━ <b>ПОД КАЖДЫМ СИГНАЛОМ</b> ━━━━━━━━━\n"
        "📈 <b>График</b> — открыть монету на TradingView\n"
        "📋 <b>Результат</b> — записать итог (TP1/TP2/TP3/SL/Пропустил)\n"
        "📊 <b>Статистика</b> — кривая доходности по всем записанным сделкам\n"
    )


def kb_help() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back()])
