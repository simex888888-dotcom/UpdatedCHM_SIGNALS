"""
handlers.py v5 — CHM BREAKER BOT
Правило: cb.answer() ВСЕГДА первым, до любых await с БД.
Улучшения: /analyze команда, rate-limit, dedup keyboard helper, corr_label в ответе.
"""
import io
import time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from aiogram.types import BufferedInputFile
import asyncio
import logging
import time
from dataclasses import fields
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

import database as db
from user_manager import UserManager, UserSettings, TradeCfg
from keyboards import (
    kb_main, kb_back, kb_back_photo, kb_settings, kb_notify, kb_subscribe,
    kb_contact_admin,
    kb_mode_long, kb_mode_short, kb_mode_both,
    kb_long_timeframes, kb_short_timeframes, kb_timeframes,
    kb_long_intervals, kb_short_intervals, kb_intervals,
    kb_long_settings, kb_short_settings,
    kb_pivots, kb_long_pivots, kb_short_pivots,
    kb_ema, kb_long_ema, kb_short_ema,
    kb_filters, kb_long_filters, kb_short_filters,
    kb_quality, kb_long_quality, kb_short_quality,
    kb_cooldown, kb_long_cooldown, kb_short_cooldown,
    kb_sl, kb_long_sl, kb_short_sl,
    kb_targets, kb_long_targets, kb_short_targets,
    kb_volume, kb_long_volume, kb_short_volume,
    trend_text, help_text, kb_help,
)
from scanner_mid import signal_compact_keyboard, trade_records_keyboard

log = logging.getLogger("CHM.Handlers")

# ─── Antispam: user_id → last_analyze_ts ──────────────
_analyze_cooldown: dict[int, float] = {}
_ANALYZE_COOLDOWN_SEC = 10  # секунд между /analyze запросами


# ─── Dedup keyboard helper ────────────────────────────

def _dedup_keyboard(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """
    Удаляет кнопки с дублирующимся callback_data из InlineKeyboardMarkup.
    Кнопки с url (не callback_data) не трогаются.
    Если дубликат callback_data — удаляется вторая кнопка.
    Если дубликат текста, но разный callback_data — оба остаются.
    """
    seen_callbacks: set = set()
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            cb = btn.callback_data
            if cb is None:
                # кнопка с url или без callback — оставляем
                new_row.append(btn)
            elif cb not in seen_callbacks:
                seen_callbacks.add(cb)
                new_row.append(btn)
            # дубликат callback_data — пропускаем кнопку
        if new_row:
            new_rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


# ─── Утилита: безопасное редактирование ──────────────

async def safe_edit(cb: CallbackQuery, text: str = None, reply_markup=None):
    if reply_markup is not None:
        reply_markup = _dedup_keyboard(reply_markup)
    for _ in range(3):
        try:
            if text:
                await cb.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
            else:
                await cb.message.edit_reply_markup(reply_markup=reply_markup)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            return
        except Exception:
            return


class EditState(StatesGroup):
    # Общие TP
    tp1 = State(); tp2 = State(); tp3 = State()
    # ЛОНГ TP
    long_tp1 = State(); long_tp2 = State(); long_tp3 = State()
    # ШОРТ TP
    short_tp1 = State(); short_tp2 = State(); short_tp3 = State()


class AnalyzeState(StatesGroup):
    waiting_symbol = State()


# ── Тексты ───────────────────────────────────────────

def main_text(user: UserSettings, trend: dict) -> str:
    NL = "\n"
    long_s  = "🟢 ЛОНГ" if user.long_active  else "⚫ лонг выкл"
    short_s = "🟢 ШОРТ" if user.short_active else "⚫ шорт выкл"
    both_s  = "🟢 ОБА"  if (user.active and user.scan_mode == "both") else "⚫ оба выкл"
    sub_em  = {"active":"✅","trial":"🆓","expired":"❌","banned":"🚫"}.get(user.sub_status,"❓")
    return (
        "⚡ <b>CHM BREAKER BOT</b>" + NL + NL +
        trend_text(trend) + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        long_s + "  |  " + short_s + "  |  " + both_s + NL +
        "Подписка: " + sub_em + " " + user.sub_status.upper() +
        " — " + user.time_left_str() + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "Выбери режим сканера 👇"
    )


def settings_text(user: UserSettings) -> str:
    """Текст для режима ОБА (legacy)."""
    NL = "\n"
    status  = "🟢 АКТИВЕН" if (user.active and user.scan_mode=="both") else "🔴 ОСТАНОВЛЕН"
    sub_em  = {"active":"✅","trial":"🆓","expired":"❌","banned":"🚫"}.get(user.sub_status,"❓")
    cfg     = user.shared_cfg()
    filters = ", ".join(f for f,v in [
        ("RSI",cfg.use_rsi),("Объём",cfg.use_volume),
        ("Паттерн",cfg.use_pattern),("HTF",cfg.use_htf)] if v) or "все выкл"
    return (
        "⚡ <b>CHM BREAKER BOT — режим ОБА</b>" + NL + NL +
        "Статус:    <b>" + status + "</b>" + NL +
        "Подписка:  <b>" + sub_em + " " + user.sub_status.upper() +
        " — " + user.time_left_str() + "</b>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "📊 Таймфрейм:     <b>" + user.timeframe + "</b>" + NL +
        "🔄 Интервал:      <b>каждые " + str(user.scan_interval//60) + " мин.</b>" + NL +
        "🎯 Цели:          <b>" + str(cfg.tp1_rr) + "R / " + str(cfg.tp2_rr) + "R / " + str(cfg.tp3_rr) + "R</b>" + NL +
        "🔬 Фильтры:       <b>" + filters + "</b>" + NL +
        "📈 Сигналов: <b>" + str(user.signals_received) + "</b>"
    )


def cfg_text(cfg: TradeCfg, title: str) -> str:
    NL = "\n"
    filters = ", ".join(f for f,v in [
        ("RSI",cfg.use_rsi),("Объём",cfg.use_volume),
        ("Паттерн",cfg.use_pattern),("HTF",cfg.use_htf)] if v) or "все выкл"
    return (
        title + NL + NL +
        "📊 Таймфрейм: <b>" + cfg.timeframe + "</b>" + NL +
        "🔄 Интервал:  <b>" + str(cfg.scan_interval//60) + " мин.</b>" + NL +
        "🎯 Цели:      <b>" + str(cfg.tp1_rr) + "R / " + str(cfg.tp2_rr) + "R / " + str(cfg.tp3_rr) + "R</b>" + NL +
        "⭐ Качество:   <b>" + str(cfg.min_quality) + "</b>  Cooldown: <b>" + str(cfg.cooldown_bars) + "</b>" + NL +
        "🔬 Фильтры:   <b>" + filters + "</b>" + NL +
        "📐 Пивоты: <b>" + str(cfg.pivot_strength) + "</b>  ATR: <b>" + str(cfg.atr_mult) + "x</b>" + NL +
        "📉 EMA <b>" + str(cfg.ema_fast) + "/" + str(cfg.ema_slow) + "</b>"
    )


def stats_text(user: UserSettings, stats: dict) -> str:
    NL = "\n"
    name = "@" + user.username if user.username else "Трейдер"
    if not stats:
        return "📊 <b>Статистика — " + name + "</b>" + NL + NL + "Сделок пока нет."
    wr   = stats["winrate"]
    rr   = stats["avg_rr"]
    tot  = stats["total_rr"]
    sign = "+" if tot >= 0 else ""
    wr_em = "🔥" if wr >= 70 else "✅" if wr >= 50 else "⚠️"
    rr_em = "💰" if rr > 1.0 else "⚖️" if rr > 0 else "📉"
    lw,lt = stats["longs_wins"],stats["longs_total"]
    sw,st = stats["shorts_wins"],stats["shorts_total"]
    lwr = str(round(lw/lt*100))+"%" if lt else "—"
    swr = str(round(sw/st*100))+"%" if st else "—"
    best = ""
    for s, d in stats.get("best_symbols", []):
        pct  = round(d["wins"]/d["total"]*100)
        best += "  • " + s + ": " + str(d["wins"]) + "/" + str(d["total"]) + " (" + str(pct) + "%)" + NL
    if not best:
        best = "  Нужно 2+ сделки по монете" + NL
    return (
        "📊 <b>Статистика — " + name + "</b>" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "📋 Сделок: <b>" + str(stats["total"]) + "</b>  ✅ <b>" + str(stats["wins"]) + "</b>  ❌ <b>" + str(stats["losses"]) + "</b>" + NL +
        wr_em + " Винрейт: <b>" + "{:.1f}".format(wr) + "%</b>" + NL +
        rr_em + " Средний R: <b>" + "{:+.2f}".format(rr) + "R</b>" + NL +
        "💼 Итого R: <b>" + sign + "{:.2f}".format(tot) + "R</b>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "📈 Лонги: <b>" + str(lw) + "/" + str(lt) + "</b> (" + lwr + ")" + NL +
        "📉 Шорты: <b>" + str(sw) + "/" + str(st) + "</b> (" + swr + ")" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "🏆 <b>Лучшие монеты:</b>" + NL + best
    )


def access_denied_text(reason: str) -> str:
    NL = "\n"
    if reason == "banned":
        return "🚫 <b>Доступ заблокирован.</b>" + NL + NL + "Обратись к администратору @crypto_chm."
    return (
        "🤖 <b>CHM BOT — автоматический сканер твоей прибыли. Бот, который не даст проспать профит.</b>" + NL + NL +
        "Выберите тариф подписки 👇" + NL + NL +
        "Оплата: <b>BEP20 (BSC)</b>" + NL +
        "💎 Для лабы — специальные цены, пишите @crypto_chm"
    )


def pricing_text(config) -> str:
    NL = "\n"
    return (
        "🤖 <b>CHM BOT — автоматический сканер твоей прибыли. Бот, который не даст проспать профит.</b>" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "🤖 <b>Только БОТ:</b>" + NL +
        "  📅 1 месяц  — <b>" + config.BOT_PRICE_30 + "</b>" + NL +
        "  📅 3 месяца — <b>" + config.BOT_PRICE_90 + "</b>" + NL +
        "  📅 1 ГОД    — <b>" + config.BOT_PRICE_365 + "</b>" + NL + NL +
        "🤖📊 <b>БОТ + ИНДИКАТОР на TradingView:</b>" + NL +
        "  📅 1 месяц  — <b>" + config.FULL_PRICE_30 + "</b>" + NL +
        "  📅 3 месяца — <b>" + config.FULL_PRICE_90 + "</b>" + NL +
        "  📅 1 ГОД    — <b>" + config.FULL_PRICE_365 + "</b>" + NL + NL +
        "💎 <b>Для лабы — дешевле.</b> Пишите @crypto_chm" + NL +
        "🎁 <b>Супер предложение</b> (бот + лаба) — @crypto_chm" + NL + NL +
        "Выберите тариф 👇"
    )


def payment_instruction_text(plan_name: str, amount: str, config) -> str:
    NL = "\n"
    return (
        "💳 <b>Оплата подписки</b>" + NL + NL +
        "📦 Тариф: <b>" + plan_name + " — " + amount + "</b>" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "🔗 Сеть: <b>" + config.PAYMENT_NETWORK + "</b>" + NL + NL +
        "📋 Адрес для оплаты:" + NL +
        "<code>" + config.PAYMENT_ADDRESS + "</code>" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "✅ После оплаты отправь скриншот и свой Telegram ID:" + NL +
        "🆔 Твой ID: ниже" + NL + NL +
        "✍️ Написать администратору: @crypto_chm"
    )


# ── Нормализация символа для /analyze ────────────────

def _normalize_symbol(raw: str) -> str:
    """
    "SOL" → "SOL-USDT-SWAP"
    "SOLUSDT" → "SOL-USDT-SWAP"
    "SOL-USDT" → "SOL-USDT-SWAP"
    "SOL-USDT-SWAP" → "SOL-USDT-SWAP"
    """
    s = raw.strip().upper()
    # Уже в правильном формате
    if s.endswith("-USDT-SWAP"):
        return s
    # Убрать -SWAP если есть без USDT
    s = s.replace("-SWAP", "")
    # Убрать -USDT суффикс чтобы получить чистый тикер
    if s.endswith("-USDT"):
        base = s[:-5]
    elif s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    return base + "-USDT-SWAP"


def _analyze_result_text(symbol: str, sig) -> str:
    """Форматирует результат analyze_on_demand в читаемый вид."""
    NL = "\n"
    arrow = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"
    stars = "⭐" * sig.quality + "☆" * max(0, 5 - sig.quality)

    def pct(t): return abs((t - sig.entry) / sig.entry * 100)

    reasons_block = ""
    if sig.reasons:
        reasons_block = "📋 <b>Факторы качества:</b>" + NL + NL.join(sig.reasons) + NL + NL

    corr_block = ("🔗 <b>Корреляция:</b> " + sig.corr_label + NL) if sig.corr_label else ""
    session_block = ("🕐 <b>Сессия:</b> " + sig.session + NL) if sig.session else ""

    return (
        "🔍 <b>Анализ по запросу: " + symbol + "</b>" + NL + NL +
        arrow + "  ⭐ Качество: " + stars + NL +
        session_block + corr_block + NL +
        reasons_block +
        "🧠 <b>Анализ:</b> <i>" + sig.human_explanation + "</i>" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "💰 Вход:    <code>" + "{:.6g}".format(sig.entry) + "</code>" + NL +
        "🛑 Стоп:    <code>" + "{:.6g}".format(sig.sl) + "</code>  <i>(-" + "{:.2f}".format(sig.risk_pct) + "%)</i>" + NL + NL +
        "🎯 Цель 1: <code>" + "{:.6g}".format(sig.tp1) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp1)) + "%)</i>" + NL +
        "🎯 Цель 2: <code>" + "{:.6g}".format(sig.tp2) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp2)) + "%)</i>" + NL +
        "🏆 Цель 3: <code>" + "{:.6g}".format(sig.tp3) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp3)) + "%)</i>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "📊 RSI: <code>" + "{:.1f}".format(sig.rsi) + "</code>  |  Vol: <code>x" + "{:.1f}".format(sig.volume_ratio) + "</code>" + NL +
        "⚡ <i>CHM Laboratory — ручной анализ</i>"
    )


# ── Хелперы для обновления cfg направления ──────────

def _update_long_field(user: UserSettings, field: str, value):
    cfg = TradeCfg.from_json(user.long_cfg)
    setattr(cfg, field, value)
    user.long_cfg = cfg.to_json()

def _update_short_field(user: UserSettings, field: str, value):
    cfg = TradeCfg.from_json(user.short_cfg)
    setattr(cfg, field, value)
    user.short_cfg = cfg.to_json()

def _update_shared_field(user: UserSettings, field: str, value):
    """Обновляет поле в обеих конфигах (long + short)."""
    _update_long_field(user, field, value)
    _update_short_field(user, field, value)

def _apply_shared_cfg(user: UserSettings, cfg: TradeCfg):
    """Применяет TradeCfg к обеим конфигам."""
    user.long_cfg = cfg.to_json()
    user.short_cfg = cfg.to_json()


# ── Хранилище стратегий пользователей ────────────────
import json, os as _os

_STRATEGY_FILE = "strategy_prefs.json"

def _load_strategies() -> dict:
    try:
        if _os.path.exists(_STRATEGY_FILE):
            return json.loads(open(_STRATEGY_FILE).read())
    except Exception:
        pass
    return {}

def _save_strategies(d: dict):
    try:
        with open(_STRATEGY_FILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass

_user_strategy: dict = _load_strategies()

def _get_user_strategy(uid: int) -> str:
    """Возвращает "LEVELS" | "SMC" | "" (не выбрана)."""
    return _user_strategy.get(str(uid), "")

def _set_user_strategy(uid: int, strategy: str):
    _user_strategy[str(uid)] = strategy
    _save_strategies(_user_strategy)


# ── Клавиатуры выбора стратегии ───────────────────────

def _kb_strategy_select() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Уровни (Price Action)", callback_data="strategy_levels"),
            InlineKeyboardButton(text="🧠 Smart Money (SMC)",     callback_data="strategy_smc"),
        ],
    ])

def _kb_strategy_change() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Уровни", callback_data="strategy_levels"),
            InlineKeyboardButton(text="🧠 SMC",    callback_data="strategy_smc"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
    ])

def _strategy_text(uid: int) -> str:
    s = _get_user_strategy(uid)
    chosen = ("📊 Уровни (Price Action)" if s == "LEVELS"
              else "🧠 Smart Money (SMC)" if s == "SMC"
              else "не выбрана")
    return (
        "🎯 <b>ВЫБОР СТРАТЕГИИ</b>\n\n"
        "Бот поддерживает две стратегии анализа:\n\n"
        "📊 <b>Уровни (Price Action)</b>\n"
        "  Классические уровни поддержки/сопротивления, фракталы, "
        "паттерны свечей, Volume Profile, мультитаймфреймный анализ.\n\n"
        "🧠 <b>Smart Money Concepts (SMC)</b>\n"
        "  Институциональный анализ: Market Structure (BOS/CHoCH), "
        "Liquidity Sweeps, Order Blocks, Fair Value Gaps, Premium/Discount Zones.\n\n"
        "Текущая стратегия: <b>" + chosen + "</b>\n\n"
        "Выбери стратегию 👇"
    )


# ── Форматирование SMC-сигнала для /analyze ───────────

def _analyze_smc_text(symbol: str, sig) -> str:
    NL = "\n"
    dir_em = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"

    def pct(t): return abs((t - sig.entry) / sig.entry * 100)

    confs = ""
    for label, passed in sig.confirmations:
        confs += ("✅ " if passed else "⬜ ") + label + NL

    return (
        "🔍 <b>SMC Анализ: " + symbol + "</b>" + NL + NL +
        sig.grade + "  " + dir_em + NL +
        "📊 Таймфреймы: " + sig.tf_ltf + " → " + sig.tf_mtf + " → " + sig.tf_htf + NL + NL +
        "📋 <b>Подтверждения (" + str(sig.score) + "/5):</b>" + NL +
        confs + NL +
        "🧠 <b>Логика входа:</b>" + NL +
        "<i>" + sig.narrative + "</i>" + NL + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "💰 Вход: <code>" + "{:.4g}".format(sig.entry_low) + " – " + "{:.4g}".format(sig.entry_high) + "</code>" + NL +
        "🛑 Стоп: <code>" + "{:.4g}".format(sig.sl) + "</code>  <i>(-" + "{:.2f}".format(sig.risk_pct) + "%)</i>" + NL + NL +
        "🎯 TP1: <code>" + "{:.4g}".format(sig.tp1) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp1)) + "%)</i>" + NL +
        "🎯 TP2: <code>" + "{:.4g}".format(sig.tp2) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp2)) + "%)</i>" + NL +
        "🏆 TP3: <code>" + "{:.4g}".format(sig.tp3) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp3)) + "%)</i>" + NL +
        "📐 R:R = 1:" + str(sig.rr) + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        "⚡ <i>CHM Laboratory — SMC Strategy</i>"
    )


# ── Мультитаймфреймный /analyze (человекоподобный) ───

async def _do_analyze_multitf(symbol: str, fetcher, indicator,
                               uid: int, bot) -> str:
    """
    Загружает 1H, 4H, 1D свечи. Анализирует уровни на каждом ТФ.
    Возвращает человекоподобный текст с рекомендацией.
    """
    NL = "\n"
    strategy = _get_user_strategy(uid)

    # Загрузка свечей BTC/ETH для корреляции
    try:
        df_btc = await fetcher.get_candles("BTC-USDT-SWAP", "1H", limit=100)
        df_eth = await fetcher.get_candles("ETH-USDT-SWAP", "1H", limit=100)
    except Exception:
        df_btc = df_eth = None

    # Попытка анализа на 3 ТФ
    tf_list = ["1H", "4H", "1D"]
    tf_labels = {"1H": "1 час", "4H": "4 часа", "1D": "1 день"}
    results = {}
    dfs = {}

    for tf in tf_list:
        try:
            df = await fetcher.get_candles(symbol, tf, limit=300)
            if df is not None and len(df) >= 50:
                dfs[tf] = df
        except Exception:
            pass

    if not dfs:
        return f"⚠️ Не удалось загрузить данные по <b>{symbol}</b>."

    if strategy == "SMC":
        # SMC анализ
        from smc.analyzer      import SMCAnalyzer, SMCConfig
        from smc.signal_builder import build_smc_signal
        smc_cfg      = SMCConfig()
        smc_analyzer = SMCAnalyzer(smc_cfg)

        df_htf = dfs.get("1D") or dfs.get("4H")
        df_mtf = dfs.get("4H") or dfs.get("1H")
        df_ltf = dfs.get("1H")

        if df_htf is None or df_mtf is None:
            return f"⚠️ Недостаточно данных для SMC анализа <b>{symbol}</b>."

        analysis = smc_analyzer.analyze(symbol, df_htf, df_mtf, df_ltf)
        sig = build_smc_signal(symbol, analysis, smc_cfg,
                               tf_htf="1D", tf_mtf="4H", tf_ltf="1H")
        if sig:
            return _analyze_smc_text(symbol, sig)

        # Если сигнала нет — показываем частичный анализ
        trend = analysis.get("structure", {}).get("trend", "RANGING")
        pd_z  = analysis.get("pd_zone", {}).get("zone", "NEUTRAL")
        pos   = analysis.get("pd_zone", {}).get("position_pct", 50.0)
        ob_b  = analysis.get("ob", {}).get("bull_ob", {})
        ob_s  = analysis.get("ob", {}).get("bear_ob", {})
        sweep_up   = analysis.get("liquidity", {}).get("sweep_up", {})
        sweep_down = analysis.get("liquidity", {}).get("sweep_down", {})
        trend_map  = {"BULLISH": "📈 Восходящий (HH/HL)",
                      "BEARISH": "📉 Нисходящий (LH/LL)",
                      "RANGING": "↔️ Боковик"}
        zone_map   = {"PREMIUM": "📈 Премиум (>50%)", "DISCOUNT": "📉 Дискаунт (<50%)",
                      "EQUILIBRIUM": "↔️ Равновесие"}

        parts = [f"🔍 <b>SMC Анализ: {symbol}</b>" + NL + NL]
        parts.append(f"📊 <b>Рыночная структура (1D):</b> {trend_map.get(trend, trend)}" + NL)
        parts.append(f"📍 <b>Позиция цены:</b> {zone_map.get(pd_z, pd_z)} ({pos:.0f}% диапазона)" + NL)
        if ob_b.get("found"):
            parts.append(f"🟢 <b>Ближайший бычий OB:</b> {ob_b['ob_low']:.4g}–{ob_b['ob_high']:.4g}" + NL)
        if ob_s.get("found"):
            parts.append(f"🔴 <b>Ближайший медвежий OB:</b> {ob_s['ob_low']:.4g}–{ob_s['ob_high']:.4g}" + NL)
        if sweep_up.get("swept"):
            parts.append(f"✅ <b>Снята ликвидность снизу</b> ({sweep_up['level']:.4g})" + NL)
        if sweep_down.get("swept"):
            parts.append(f"✅ <b>Снята ликвидность сверху</b> ({sweep_down['level']:.4g})" + NL)

        parts.append(NL + "⚠️ <b>Сигнала недостаточно подтверждений.</b>" + NL)
        parts.append(_smc_recommendation(analysis, dfs.get("1H")))
        return "".join(parts)

    else:
        # LEVELS анализ (Price Action)
        best_sig = None
        tf_analyses = {}

        for tf, df in dfs.items():
            try:
                df_htf_l = dfs.get("1D") if tf != "1D" else None
                sig = indicator.analyze_on_demand(symbol, df, df_htf_l, df_btc, df_eth)
                tf_analyses[tf] = sig
                if sig and (best_sig is None or sig.quality > best_sig.quality):
                    best_sig = sig
            except Exception:
                tf_analyses[tf] = None

        return _format_multitf_levels_text(symbol, tf_analyses, tf_labels, best_sig)


def _format_multitf_levels_text(symbol: str, tf_analyses: dict,
                                  tf_labels: dict, best_sig) -> str:
    NL = "\n"
    lines = [f"🔍 <b>Анализ: {symbol}</b>" + NL + NL,
             "📊 <b>Мультитаймфреймный обзор:</b>" + NL]

    tf_order = ["1H", "4H", "1D"]
    has_any = False

    for tf in tf_order:
        sig = tf_analyses.get(tf)
        label = tf_labels.get(tf, tf)
        if sig:
            has_any = True
            dir_em = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"
            cls_map = {1: "Абсолютный", 2: "Сильный", 3: "Рабочий"}
            cls_name = cls_map.get(sig.level_class, "")
            stars = "⭐" * sig.quality
            lines.append(
                NL + f"🕐 <b>{label} ({tf}):</b> {dir_em}  {stars}" + NL +
                f"  Уровень: <code>{sig.entry:.4g}</code> ({cls_name}, {sig.test_count} касания)" + NL +
                f"  Паттерн: {sig.pattern or sig.breakout_type}" + NL +
                (f"  Сессия: {sig.session}" + NL if sig.session else "") +
                (f"  Корреляция: {sig.corr_label}" + NL if sig.corr_label else "")
            )
        else:
            lines.append(NL + f"🕐 <b>{label} ({tf}):</b> нет сигнала" + NL)

    if best_sig:
        risk = abs(best_sig.entry - best_sig.sl)
        rr1  = abs(best_sig.tp1 - best_sig.entry) / risk if risk > 0 else 0

        dir_txt = "LONG" if best_sig.direction == "LONG" else "SHORT"
        best_tf = next((tf for tf, s in tf_analyses.items() if s is best_sig), "?")

        lines.append(
            NL + "━━━━━━━━━━━━━━━━━━━━" + NL +
            "🎯 <b>РЕКОМЕНДАЦИЯ:</b>" + NL + NL +
            f"<b>{dir_txt}</b> от <code>{best_sig.entry:.4g}</code> — "
            f"лучший сетап на {best_tf}." + NL +
            f"🛑 SL: <code>{best_sig.sl:.4g}</code> (-{best_sig.risk_pct:.2f}%)" + NL +
            f"🎯 TP1: <code>{best_sig.tp1:.4g}</code>  (R:R {rr1:.1f})" + NL +
            f"🎯 TP2: <code>{best_sig.tp2:.4g}</code>" + NL +
            f"🏆 TP3: <code>{best_sig.tp3:.4g}</code>" + NL + NL +
            f"💡 <i>{best_sig.human_explanation}</i>"
        )
    elif not has_any:
        lines.append(
            NL + "━━━━━━━━━━━━━━━━━━━━" + NL +
            "⚠️ <b>Сигналов не найдено</b>" + NL + NL +
            "Монета не в зоне ни на одном таймфрейме. " + NL +
            "Рекомендуется дождаться приближения к ключевым уровням."
        )
    else:
        lines.append(
            NL + "━━━━━━━━━━━━━━━━━━━━" + NL +
            "ℹ️ Уровни найдены, но качество входа пока недостаточно." + NL +
            "Рекомендуется ждать подтверждения паттерна."
        )
    return "".join(lines)


def _smc_recommendation(analysis: dict, df_1h=None) -> str:
    """Рекомендация даже без сигнала на основе SMC данных."""
    s     = analysis.get("structure", {})
    pd_z  = analysis.get("pd_zone", {})
    ob_b  = analysis.get("ob", {}).get("bull_ob", {})
    ob_s  = analysis.get("ob", {}).get("bear_ob", {})
    trend = s.get("trend", "RANGING")
    zone  = pd_z.get("zone", "NEUTRAL")

    if trend == "BULLISH" and zone == "DISCOUNT" and ob_b.get("found"):
        return (
            "\n💡 <b>Рекомендация:</b> Тренд бычий, цена в дискаунте. "
            f"Лучшая точка входа в LONG — бычий OB "
            f"{ob_b['ob_low']:.4g}–{ob_b['ob_high']:.4g}. "
            "Ждать митигации (возврата в зону) для входа."
        )
    if trend == "BEARISH" and zone == "PREMIUM" and ob_s.get("found"):
        return (
            "\n💡 <b>Рекомендация:</b> Тренд медвежий, цена в премиуме. "
            f"Лучшая точка входа в SHORT — медвежий OB "
            f"{ob_s['ob_low']:.4g}–{ob_s['ob_high']:.4g}. "
            "Ждать тест зоны для входа."
        )
    if trend == "BULLISH":
        return "\n💡 <b>Рекомендация:</b> Тренд бычий. Ищи покупки от дискаунта (ниже 50% диапазона)."
    if trend == "BEARISH":
        return "\n💡 <b>Рекомендация:</b> Тренд медвежий. Ищи шорты от премиума (выше 50% диапазона)."
    return "\n💡 <b>Рекомендация:</b> Рынок в боковике. Воздержись от торговли — жди формирования структуры."



# ─── Основная функция регистрации хендлеров ──────────

def register_handlers(dp: Dispatcher, bot: Bot, um: UserManager, scanner, config):

    is_admin = lambda uid: uid in config.ADMIN_IDS

    # ── Запуск SMC сканера (background task) ───────────────────────────────
    from fetcher import OKXFetcher as _OKXFetcher
    _fetcher_for_smc = _OKXFetcher()

    async def _on_startup_inner():
        from smc.scanner import run_smc_scanner
        asyncio.create_task(
            run_smc_scanner(bot, um, _fetcher_for_smc, _get_user_strategy)
        )

    dp.startup.register(_on_startup_inner)

    # ─── КОМАНДЫ ──────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(
                pricing_text(config),
                parse_mode="HTML",
                reply_markup=kb_subscribe(config),
            )
            return
        # Если стратегия не выбрана — предлагаем выбрать
        if not _get_user_strategy(user.user_id):
            await msg.answer(_strategy_text(user.user_id), parse_mode="HTML",
                             reply_markup=_kb_strategy_select())
            return
        trend = scanner.get_trend()
        await msg.answer(main_text(user, trend), parse_mode="HTML", reply_markup=kb_main(user))

    @dp.message(Command("menu"))
    async def cmd_menu(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(pricing_text(config), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return
        trend = scanner.get_trend()
        await msg.answer(main_text(user, trend), parse_mode="HTML", reply_markup=kb_main(user))

    @dp.message(Command("stop"))
    async def cmd_stop(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        user.active = False
        user.long_active = False
        user.short_active = False
        await um.save(user)
        await msg.answer("🔴 Все сканеры остановлены. /menu чтобы снова включить.")

    @dp.message(Command("stats"))
    async def cmd_stats(msg: Message):
        user  = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        stats = await db.db_get_user_stats(user.user_id)
        await msg.answer(stats_text(user, stats), parse_mode="HTML", reply_markup=kb_back())

    @dp.message(Command("subscribe"))
    async def cmd_subscribe(msg: Message):
        NL = "\n"
        await msg.answer(
            pricing_text(config) + NL + NL +
            "🆔 Твой Telegram ID: <code>" + str(msg.from_user.id) + "</code>",
            parse_mode="HTML",
            reply_markup=kb_subscribe(config),
        )

    @dp.message(Command("strategy"))
    async def cmd_strategy(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(access_denied_text(reason), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return
        await msg.answer(_strategy_text(user.user_id), parse_mode="HTML",
                         reply_markup=_kb_strategy_change())

    # ─── ПОДПИСКА — ВЫБОР ТАРИФА (callback) ─────────────

    PLANS = {
        "plan_bot_30":   ("🤖 Только БОТ — 1 месяц",   "70$"),
        "plan_bot_90":   ("🤖 Только БОТ — 3 месяца",  "150$"),
        "plan_bot_365":  ("🤖 Только БОТ — 1 ГОД",    "330$"),
        "plan_full_30":  ("🤖📊 БОТ + ИНДИКАТОР — 1 месяц",  "90$"),
        "plan_full_90":  ("🤖📊 БОТ + ИНДИКАТОР — 3 месяца", "230$"),
        "plan_full_365": ("🤖📊 БОТ + ИНДИКАТОР — 1 ГОД",   "630$"),
    }

    @dp.callback_query(F.data.startswith("plan_"))
    async def plan_selected(cb: CallbackQuery):
        await cb.answer()
        plan_key = cb.data
        if plan_key not in PLANS:
            return
        plan_name, amount = PLANS[plan_key]
        NL = "\n"
        text = (
            "💳 <b>Оплата подписки</b>" + NL + NL +
            "📦 Тариф: <b>" + plan_name + " — " + amount + "</b>" + NL + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "🔗 Сеть: <b>" + config.PAYMENT_NETWORK + "</b>" + NL + NL +
            "📋 Адрес для перевода:" + NL +
            "<code>" + config.PAYMENT_ADDRESS + "</code>" + NL + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "✅ После оплаты отправь скриншот + свой Telegram ID администратору:" + NL + NL +
            "🆔 Твой ID: <code>" + str(cb.from_user.id) + "</code>"
        )
        from keyboards import kb_payment
        await safe_edit(cb, text, kb_payment(plan_name, amount, config.PAYMENT_ADDRESS))

    @dp.callback_query(F.data == "show_plans")
    async def show_plans(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, pricing_text(config), kb_subscribe(config))

    # ── /analyze [SYMBOL] ─────────────────────────────────────────────────
    @dp.message(Command("analyze"))
    async def cmd_analyze(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(access_denied_text(reason), parse_mode="HTML"); return

        # Rate-limit: не чаще 1 раза в 10 секунд
        uid = msg.from_user.id
        now = time.time()
        if now - _analyze_cooldown.get(uid, 0) < _ANALYZE_COOLDOWN_SEC:
            await msg.answer(
                f"⏳ Подожди {_ANALYZE_COOLDOWN_SEC} секунд между запросами.",
                parse_mode="HTML"
            )
            return
        _analyze_cooldown[uid] = now

        # Парсим символ
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await msg.answer(
                "Использование: <code>/analyze BTCUSDT</code> или <code>/analyze BTC</code>",
                parse_mode="HTML"
            )
            return

        raw_sym = parts[1].strip()
        symbol  = _normalize_symbol(raw_sym)

        wait_msg = await msg.answer(f"🔍 Анализирую <b>{symbol}</b>...", parse_mode="HTML")

        try:
            from indicator import CHMIndicator
            from scanner_mid import _cfg_to_ind, IndConfig

            # Используем дефолтный индикатор для анализа
            from user_manager import TradeCfg as _TradeCfg
            _ind_cfg = _TradeCfg()
            from scanner_mid import IndConfig
            ind_config_obj = IndConfig(
                TIMEFRAME="1H", PIVOT_STRENGTH=7, ATR_PERIOD=14, ATR_MULT=1.0,
                MAX_RISK_PCT=1.5, EMA_FAST=50, EMA_SLOW=200,
                RSI_PERIOD=14, RSI_OB=65, RSI_OS=35,
                VOL_MULT=1.0, VOL_LEN=20, MAX_LEVEL_AGE=100,
                MAX_RETEST_BARS=30, COOLDOWN_BARS=0, ZONE_BUFFER=0.3,
                TP1_RR=2.0, TP2_RR=3.0, TP3_RR=4.5,
            )
            indicator_obj = CHMIndicator(ind_config_obj)

            result_text = await _do_analyze_multitf(
                symbol, _fetcher_for_smc, indicator_obj, uid, bot
            )

            try:
                await wait_msg.delete()
            except Exception:
                pass

            await msg.answer(result_text, parse_mode="HTML")

        except Exception as e:
            log.error(f"cmd_analyze error: {e}")
            try:
                await wait_msg.delete()
            except Exception:
                pass
            await msg.answer(f"❌ Ошибка анализа <b>{symbol}</b>: {e}", parse_mode="HTML")

    # Альтернативный формат: "/analyze BTCUSDT" как обычный текст
    @dp.message(F.text.regexp(r'^/analyze\s+\S+'))
    async def cmd_analyze_inline(msg: Message):
        """Дублёр для обеспечения работы без слэша."""
        await cmd_analyze(msg)

    # ─── ВЫБОР СТРАТЕГИИ ──────────────────────────────

    @dp.callback_query(F.data.startswith("strategy_"))
    async def cb_strategy(cb: CallbackQuery):
        choice = cb.data.replace("strategy_", "")   # "levels" | "smc"
        uid    = cb.from_user.id
        strategy_map = {"levels": "LEVELS", "smc": "SMC"}
        strategy = strategy_map.get(choice)
        if not strategy:
            await cb.answer("Неизвестная стратегия", show_alert=True); return

        _set_user_strategy(uid, strategy)
        label = "📊 Уровни (Price Action)" if strategy == "LEVELS" else "🧠 Smart Money (SMC)"
        await cb.answer(f"✅ Стратегия выбрана: {label}", show_alert=False)

        user  = await um.get_or_create(uid)
        trend = scanner.get_trend()
        await safe_edit(cb, main_text(user, trend), kb_main(user))

    # ─── АНАЛИЗ МОНЕТЫ ПО ЗАПРОСУ ────────────────────

    async def _do_analyze(msg_or_cb, user: UserSettings, symbol: str):
        """Общая функция анализа монеты и отправки результата."""
        is_cb = isinstance(msg_or_cb, CallbackQuery)
        send  = (msg_or_cb.message.answer if is_cb else msg_or_cb.answer)

        if not symbol:
            await send("⚠️ Укажите тикер монеты. Пример: /analyze BTC")
            return

        cfg  = user.shared_cfg()
        wait_msg = await send(
            "⏳ <b>Анализирую " + symbol.upper() + "...</b>\n\nПодождите несколько секунд.",
            parse_mode="HTML",
        )
        result = await scanner.analyze_on_demand(symbol, cfg)
        try:
            await wait_msg.delete()
        except Exception:
            pass

        if result is None:
            await send(
                "🔍 <b>Анализ " + symbol.upper() + "</b>\n\n"
                "Сигнала нет — цена вдали от ключевых уровней или сигнал не прошёл фильтры.\n\n"
                "<i>Попробуйте другой таймфрейм или проверьте позже.</i>",
                parse_mode="HTML",
            )
            return

        sig, text = result
        trade_id  = str(user.user_id) + "_ondemand_" + str(int(time.time() * 1000))
        risk      = abs(sig.entry - sig.sl)
        sign      = 1 if sig.direction == "LONG" else -1
        await db.db_add_trade({
            "trade_id":      trade_id,
            "user_id":       user.user_id,
            "symbol":        sig.symbol,
            "direction":     sig.direction,
            "entry":         sig.entry,
            "sl":            sig.sl,
            "tp1":           sig.entry + sign * risk * cfg.tp1_rr,
            "tp2":           sig.entry + sign * risk * cfg.tp2_rr,
            "tp3":           sig.entry + sign * risk * cfg.tp3_rr,
            "tp1_rr":        cfg.tp1_rr,
            "tp2_rr":        cfg.tp2_rr,
            "tp3_rr":        cfg.tp3_rr,
            "quality":       sig.quality,
            "timeframe":     cfg.timeframe,
            "breakout_type": sig.breakout_type,
            "created_at":    time.time(),
        })
        await send(text, parse_mode="HTML", reply_markup=signal_compact_keyboard(trade_id, sig.symbol))

    @dp.message(Command("analyze"))
    async def cmd_analyze(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(pricing_text(config), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return
        parts  = msg.text.split(maxsplit=1)
        symbol = parts[1].strip() if len(parts) > 1 else ""
        if symbol:
            await _do_analyze(msg, user, symbol)
        else:
            await state.set_state(AnalyzeState.waiting_symbol)
            await msg.answer(
                "🔍 <b>Анализ монеты</b>\n\nВведите тикер монеты (например: BTC, ETH, SOL, PEPE):",
                parse_mode="HTML",
            )

    @dp.message(AnalyzeState.waiting_symbol)
    async def analyze_symbol_input(msg: Message, state: FSMContext):
        await state.clear()
        user   = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        symbol = (msg.text or "").strip()
        await _do_analyze(msg, user, symbol)

    @dp.callback_query(F.data == "analyze_coin")
    async def analyze_coin_cb(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id, cb.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await safe_edit(cb, pricing_text(config), kb_subscribe(config))
            return
        await state.set_state(AnalyzeState.waiting_symbol)
        await safe_edit(
            cb,
            "🔍 <b>Анализ монеты по запросу</b>\n\n"
            "Введите тикер монеты (например: <code>BTC</code>, <code>SOL</code>, <code>PEPE</code>)\n\n"
            "<i>Анализ выполняется по вашим текущим настройкам (режим ОБА).</i>",
        )

    # ─── РЕЗУЛЬТАТЫ СДЕЛОК ────────────────────────────

    @dp.callback_query(F.data.startswith("res_"))
    async def trade_result(cb: CallbackQuery):
        NL       = "\n"
        parts    = cb.data.split("_", 2)
        result   = parts[1]
        trade_id = parts[2]
        labels   = {
            "TP1":"🎯 TP1 зафиксирован!","TP2":"🎯 TP2 зафиксирован!",
            "TP3":"🏆 TP3 зафиксирован!","SL":"❌ Стоп-лосс","SKIP":"⏭ Пропущено",
        }
        await cb.answer(labels.get(result, "✅ Записано"), show_alert=True)

        trade = await db.db_get_trade(trade_id)
        if not trade:
            await cb.message.answer("⚠️ Сделка не найдена."); return
        if trade.get("result") and trade["result"] not in ("", "SKIP"):
            await cb.message.answer("ℹ️ Результат уже записан: <b>" + trade["result"] + "</b>", parse_mode="HTML"); return

        rr_map = {"TP1":trade["tp1_rr"],"TP2":trade["tp2_rr"],"TP3":trade["tp3_rr"],"SL":-1.0,"SKIP":0.0}
        await db.db_set_trade_result(trade_id, result, rr_map.get(result, 0.0))

        emojis  = {"TP1":"🎯 TP1","TP2":"🎯 TP2","TP3":"🏆 TP3","SL":"❌ SL","SKIP":"⏭ Пропущено"}
        rr_strs = {"TP1":"+"+str(trade["tp1_rr"])+"R","TP2":"+"+str(trade["tp2_rr"])+"R",
                   "TP3":"+"+str(trade["tp3_rr"])+"R","SL":"-1R","SKIP":""}
        try:
            await cb.message.edit_text(
                (cb.message.text or "") + NL + NL +
                "<b>Результат: " + emojis.get(result,"") + "  " + rr_strs.get(result,"") + "</b>",
                parse_mode="HTML", reply_markup=None,
            )
        except Exception: pass

        if result != "SKIP":
            user  = await um.get_or_create(cb.from_user.id)
            stats = await db.db_get_user_stats(user.user_id)
            if stats:
                wr   = stats["winrate"]
                tot  = stats["total_rr"]
                sign = "+" if tot >= 0 else ""
                wr_em = "🔥" if wr >= 70 else "✅" if wr >= 50 else "⚠️"
                await cb.message.answer(
                    "📊 <b>Счёт обновлён</b>" + NL + NL +
                    "Сделок: <b>" + str(stats["total"]) + "</b>  " +
                    wr_em + " Винрейт: <b>" + "{:.1f}".format(wr) + "%</b>" + NL +
                    "Итого R: <b>" + sign + "{:.2f}".format(tot) + "R</b>" + NL + NL +
                    "Полная статистика → /stats",
                    parse_mode="HTML",
                )


    # ─── НАВИГАЦИЯ ────────────────────────────────────

    @dp.callback_query(F.data == "back_main")
    async def back_main(cb: CallbackQuery):
        await cb.answer()
        user  = await um.get_or_create(cb.from_user.id)
        trend = scanner.get_trend()
        await safe_edit(cb, main_text(user, trend), kb_main(user))

    @dp.callback_query(F.data == "show_strategy")
    async def show_strategy(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, _strategy_text(cb.from_user.id), _kb_strategy_change())

    @dp.callback_query(F.data == "my_stats")
    async def my_stats(cb: CallbackQuery):
        await cb.answer()
        user   = await um.get_or_create(cb.from_user.id)
        stats  = await db.db_get_user_stats(user.user_id)
        trades = await db.db_get_user_trades(user.user_id)
        text   = stats_text(user, stats)
        if not trades or len(trades) < 2:
            await safe_edit(cb, text, kb_back())
            return
        equity = [0.0]
        for t in trades:
            if t["result"] in ("TP1","TP2","TP3","SL"):
                equity.append(equity[-1] + t["result_rr"])
        plt.figure(figsize=(8, 4))
        color = '#00d26a' if equity[-1] >= 0 else '#f6465d'
        plt.plot(equity, color=color, linewidth=2)
        plt.fill_between(range(len(equity)), equity, alpha=0.1, color=color)
        plt.title("Кривая доходности (Risk/Reward) — @" + (user.username or "Trader"), color='white')
        plt.grid(True, linestyle='--', alpha=0.3)
        plt.gca().set_facecolor('#1e1e2d')
        plt.gcf().patch.set_facecolor('#1e1e2d')
        plt.gca().tick_params(colors='white')
        plt.axhline(0, color='white', linewidth=0.5, alpha=0.5)
        plt.ylabel("Профит (в R)", color='white')
        plt.xlabel("Количество сделок", color='white')
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0); plt.close()
        photo = BufferedInputFile(buf.getvalue(), filename="equity.png")
        await cb.message.delete()
        await bot.send_photo(chat_id=cb.message.chat.id, photo=photo,
                             caption=text, parse_mode="HTML", reply_markup=kb_back_photo())

    @dp.callback_query(F.data == "my_chart")
    async def my_chart(cb: CallbackQuery):
        await cb.answer()
        user   = await um.get_or_create(cb.from_user.id)
        trades = await db.db_get_user_trades(user.user_id)
        closed = [t for t in (trades or []) if t.get("result") in ("TP1","TP2","TP3","SL")]
        if len(closed) < 2:
            await safe_edit(cb, "📈 <b>График доходности</b>\n\nНужно минимум 2 закрытых сделки.", kb_back())
            return
        equity = [0.0]
        for t in closed:
            equity.append(equity[-1] + t["result_rr"])
        color = '#00d26a' if equity[-1] >= 0 else '#f6465d'
        plt.figure(figsize=(8, 4))
        plt.plot(equity, color=color, linewidth=2)
        plt.fill_between(range(len(equity)), equity, alpha=0.1, color=color)
        plt.title("Кривая доходности (R) — @" + (user.username or "Trader"), color='white')
        plt.grid(True, linestyle='--', alpha=0.3)
        plt.gca().set_facecolor('#1e1e2d')
        plt.gcf().patch.set_facecolor('#1e1e2d')
        plt.gca().tick_params(colors='white')
        plt.axhline(0, color='white', linewidth=0.5, alpha=0.5)
        plt.ylabel("Профит (в R)", color='white'); plt.xlabel("Количество сделок", color='white')
        buf = io.BytesIO(); plt.savefig(buf, format='png', bbox_inches='tight'); buf.seek(0); plt.close()
        sign = "+" if equity[-1] >= 0 else ""
        caption = ("📈 <b>График — @" + (user.username or "Trader") + "</b>\n\n" +
                   "Итого: <b>" + sign + "{:.2f}".format(equity[-1]) + "R</b> за " + str(len(closed)) + " сделок")
        photo = BufferedInputFile(buf.getvalue(), filename="chart.png")
        await cb.message.delete()
        await bot.send_photo(chat_id=cb.message.chat.id, photo=photo,
                             caption=caption, parse_mode="HTML", reply_markup=kb_back_photo())

    @dp.callback_query(F.data == "back_photo_main")
    async def back_photo_main(cb: CallbackQuery):
        await cb.answer()
        user  = await um.get_or_create(cb.from_user.id)
        trend = scanner.get_trend()
        try: await cb.message.delete()
        except Exception: pass
        await bot.send_message(cb.message.chat.id, main_text(user, trend),
                               parse_mode="HTML", reply_markup=kb_main(user))

    # ─── РЕЖИМ ЛОНГ ───────────────────────────────────

    @dp.callback_query(F.data == "mode_long")
    async def mode_long(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        cfg  = user.get_long_cfg()
        await safe_edit(cb, cfg_text(cfg, "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

    @dp.callback_query(F.data == "toggle_long")
    async def toggle_long(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config)); return
        # Проверяем стратегию перед включением
        if not user.long_active and not _get_user_strategy(user.user_id):
            await cb.answer()
            await safe_edit(cb, _strategy_text(user.user_id), _kb_strategy_select()); return
        user.long_active = not user.long_active
        await cb.answer("🟢 ЛОНГ включён!" if user.long_active else "🔴 ЛОНГ выключен.")
        await um.save(user)
        cfg = user.get_long_cfg()
        await safe_edit(cb, cfg_text(cfg, "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

    @dp.callback_query(F.data == "menu_long_tf")
    async def menu_long_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📊 <b>Таймфрейм ЛОНГ</b>", kb_long_timeframes(user.long_tf))

    @dp.callback_query(F.data.startswith("set_long_tf_"))
    async def set_long_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.long_tf = cb.data.replace("set_long_tf_", "")
        await cb.answer("✅ ЛОНГ ТФ: " + user.long_tf)
        await um.save(user)
        await safe_edit(cb, cfg_text(user.get_long_cfg(), "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

    @dp.callback_query(F.data == "menu_long_interval")
    async def menu_long_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔄 <b>Интервал ЛОНГ</b>", kb_long_intervals(user.long_interval))

    @dp.callback_query(F.data.startswith("set_long_interval_"))
    async def set_long_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.long_interval = int(cb.data.replace("set_long_interval_", ""))
        await cb.answer("✅ Каждые " + str(user.long_interval//60) + " мин.")
        await um.save(user)
        await safe_edit(cb, cfg_text(user.get_long_cfg(), "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

    @dp.callback_query(F.data == "menu_long_settings")
    async def menu_long_settings(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, "⚙️ <b>Настройки ЛОНГ</b>", kb_long_settings())

    @dp.callback_query(F.data == "menu_long_pivots")
    async def menu_long_pivots(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data == "menu_long_ema")
    async def menu_long_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>EMA ЛОНГ</b>", kb_long_ema(user))

    @dp.callback_query(F.data == "menu_long_filters")
    async def menu_long_filters(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data == "menu_long_quality")
    async def menu_long_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Качество ЛОНГ</b>", kb_long_quality(user))

    @dp.callback_query(F.data == "menu_long_cooldown")
    async def menu_long_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔁 <b>Cooldown ЛОНГ</b>", kb_long_cooldown(user))

    @dp.callback_query(F.data == "menu_long_sl")
    async def menu_long_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Стоп-лосс ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data == "menu_long_targets")
    async def menu_long_targets(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Цели ЛОНГ</b>", kb_long_targets(user))

    @dp.callback_query(F.data == "menu_long_volume")
    async def menu_long_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Объём ЛОНГ</b>", kb_long_volume(user))

    @dp.callback_query(F.data == "reset_long_cfg")
    async def reset_long_cfg(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer("✅ Настройки ЛОНГ сброшены к общим")
        user.long_cfg = "{}"
        await um.save(user)
        await safe_edit(cb, cfg_text(user.get_long_cfg(), "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

    # ЛОНГ — сеттеры
    @dp.callback_query(F.data.startswith("long_set_pivot_"))
    async def long_set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_pivot_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "pivot_strength", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_age_"))
    async def long_set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_age_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "max_level_age", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_retest_"))
    async def long_set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_retest_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "max_retest_bars", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_buffer_"))
    async def long_set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_buffer_", ""))
        await cb.answer("✅ x" + str(v))
        _update_long_field(user, "zone_buffer", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_zone_pct_"))
    async def long_set_zone_pct(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_zone_pct_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_long_field(user, "zone_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_dist_pct_"))
    async def long_set_dist_pct(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_dist_pct_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_long_field(user, "max_dist_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_max_tests_"))
    async def long_set_max_tests(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_max_tests_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "max_level_tests", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_min_rr_"))
    async def long_set_min_rr(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_min_rr_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "min_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели ЛОНГ</b>", kb_long_targets(user))

    @dp.callback_query(F.data.startswith("long_set_ema_fast_"))
    async def long_set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_ema_fast_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "ema_fast", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ЛОНГ</b>", kb_long_ema(user))

    @dp.callback_query(F.data.startswith("long_set_ema_slow_"))
    async def long_set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_ema_slow_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "ema_slow", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ЛОНГ</b>", kb_long_ema(user))

    @dp.callback_query(F.data.startswith("long_set_htf_ema_"))
    async def long_set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_htf_ema_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "htf_ema_period", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ЛОНГ</b>", kb_long_ema(user))

    @dp.callback_query(F.data == "long_toggle_rsi")
    async def long_toggle_rsi(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.long_cfg); cfg.use_rsi = not cfg.use_rsi
        await cb.answer("RSI ЛОНГ " + ("✅" if cfg.use_rsi else "❌"))
        user.long_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data == "long_toggle_volume")
    async def long_toggle_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.long_cfg); cfg.use_volume = not cfg.use_volume
        await cb.answer("Объём ЛОНГ " + ("✅" if cfg.use_volume else "❌"))
        user.long_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data == "long_toggle_pattern")
    async def long_toggle_pattern(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.long_cfg); cfg.use_pattern = not cfg.use_pattern
        await cb.answer("Паттерны ЛОНГ " + ("✅" if cfg.use_pattern else "❌"))
        user.long_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data == "long_toggle_htf")
    async def long_toggle_htf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.long_cfg); cfg.use_htf = not cfg.use_htf
        await cb.answer("HTF ЛОНГ " + ("✅" if cfg.use_htf else "❌"))
        user.long_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_rsi_period_"))
    async def long_set_rsi_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_rsi_period_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "rsi_period", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_rsi_ob_"))
    async def long_set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_rsi_ob_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "rsi_ob", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_rsi_os_"))
    async def long_set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_rsi_os_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "rsi_os", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_vol_mult_"))
    async def long_set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_vol_mult_", ""))
        await cb.answer("✅ x" + str(v))
        _update_long_field(user, "vol_mult", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_quality_"))
    async def long_set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_quality_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "min_quality", v); await um.save(user)
        await safe_edit(cb, "⭐ <b>Качество ЛОНГ</b>", kb_long_quality(user))

    @dp.callback_query(F.data.startswith("long_set_cooldown_"))
    async def long_set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_cooldown_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "cooldown_bars", v); await um.save(user)
        await safe_edit(cb, "🔁 <b>Cooldown ЛОНГ</b>", kb_long_cooldown(user))

    @dp.callback_query(F.data.startswith("long_set_atr_period_"))
    async def long_set_atr_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_atr_period_", ""))
        await cb.answer("✅ " + str(v))
        _update_long_field(user, "atr_period", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data.startswith("long_set_atr_mult_"))
    async def long_set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_atr_mult_", ""))
        await cb.answer("✅ x" + str(v))
        _update_long_field(user, "atr_mult", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data.startswith("long_set_risk_"))
    async def long_set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_risk_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_long_field(user, "max_risk_pct", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data.startswith("long_set_volume_"))
    async def long_set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_volume_", ""))
        await cb.answer("✅ $" + str(int(v)))
        _update_long_field(user, "min_volume_usdt", v); await um.save(user)
        await safe_edit(cb, "💰 <b>Объём ЛОНГ</b>", kb_long_volume(user))

    @dp.callback_query(F.data == "long_toggle_trend_only")
    async def long_toggle_trend_only(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.long_cfg)
        cfg.trend_only = not cfg.trend_only
        await cb.answer("📊 Тренд ЛОНГ " + ("✅" if cfg.trend_only else "❌"))
        user.long_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))


    # ─── РЕЖИМ ШОРТ ───────────────────────────────────

    @dp.callback_query(F.data == "mode_short")
    async def mode_short(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        cfg = user.get_short_cfg()
        await safe_edit(cb, cfg_text(cfg, "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    @dp.callback_query(F.data == "toggle_short")
    async def toggle_short(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config)); return
        # Проверяем стратегию перед включением
        if not user.short_active and not _get_user_strategy(user.user_id):
            await cb.answer()
            await safe_edit(cb, _strategy_text(user.user_id), _kb_strategy_select()); return
        user.short_active = not user.short_active
        if user.short_active:
            user.scan_mode = "short"
            user.active = True
        else:
            if not user.long_active:
                user.active = False
        await cb.answer("📉 ШОРТ " + ("✅ включён" if user.short_active else "❌ выключен"))
        await um.save(user)
        await safe_edit(cb, cfg_text(user.get_short_cfg(), "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    @dp.callback_query(F.data == "menu_short_tf")
    async def menu_short_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        cfg = user.get_short_cfg()
        await safe_edit(cb, "📊 <b>Таймфрейм ШОРТ</b>\n\nТекущий: <b>" + cfg.timeframe + "</b>", kb_short_timeframes(cfg.timeframe))

    @dp.callback_query(F.data.startswith("short_set_tf_"))
    async def short_set_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = cb.data.replace("short_set_tf_", "")
        await cb.answer("✅ " + v)
        _update_short_field(user, "timeframe", v); await um.save(user)
        await safe_edit(cb, "📊 <b>Таймфрейм ШОРТ</b>\n\nТекущий: <b>" + v + "</b>", kb_short_timeframes(v))

    @dp.callback_query(F.data == "menu_short_interval")
    async def menu_short_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        cfg = user.get_short_cfg()
        await safe_edit(cb, "🔄 <b>Интервал ШОРТ</b>\n\nТекущий: <b>" + str(cfg.scan_interval//60) + " мин.</b>", kb_short_intervals(cfg.scan_interval))

    @dp.callback_query(F.data.startswith("short_set_interval_"))
    async def short_set_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_interval_", ""))
        await cb.answer("✅ " + str(v//60) + " мин.")
        _update_short_field(user, "scan_interval", v); await um.save(user)
        await safe_edit(cb, "🔄 <b>Интервал ШОРТ</b>\n\nТекущий: <b>" + str(v//60) + " мин.</b>", kb_short_intervals(v))

    @dp.callback_query(F.data == "menu_short_settings")
    async def menu_short_settings(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, cfg_text(user.get_short_cfg(), "📉 <b>Настройки ШОРТ</b>"), kb_short_settings())

    @dp.callback_query(F.data == "menu_short_pivots")
    async def menu_short_pivots(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_pivot_"))
    async def short_set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_pivot_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "pivot_strength", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_age_"))
    async def short_set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_age_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "max_level_age", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_retest_"))
    async def short_set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_retest_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "min_retests", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_buffer_"))
    async def short_set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_buffer_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_short_field(user, "buffer_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_zone_pct_"))
    async def short_set_zone_pct(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_zone_pct_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_short_field(user, "zone_width_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_dist_pct_"))
    async def short_set_dist_pct(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_dist_pct_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_short_field(user, "max_dist_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_max_tests_"))
    async def short_set_max_tests(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_max_tests_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "max_tests", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_min_rr_"))
    async def short_set_min_rr(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_min_rr_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "min_rr", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data == "menu_short_ema")
    async def menu_short_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data.startswith("short_set_ema_fast_"))
    async def short_set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_ema_fast_", ""))
        await cb.answer("✅ EMA " + str(v))
        _update_short_field(user, "ema_fast", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data.startswith("short_set_ema_slow_"))
    async def short_set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_ema_slow_", ""))
        await cb.answer("✅ EMA " + str(v))
        _update_short_field(user, "ema_slow", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data.startswith("short_set_htf_ema_"))
    async def short_set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_htf_ema_", ""))
        await cb.answer("✅ HTF EMA " + str(v))
        _update_short_field(user, "htf_ema", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data == "menu_short_filters")
    async def menu_short_filters(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_rsi")
    async def short_toggle_rsi(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = TradeCfg.from_json(user.short_cfg)
        cfg.use_rsi = not cfg.use_rsi
        await cb.answer("RSI ШОРТ " + ("✅" if cfg.use_rsi else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_volume")
    async def short_toggle_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = TradeCfg.from_json(user.short_cfg)
        cfg.use_volume = not cfg.use_volume
        await cb.answer("Объём ШОРТ " + ("✅" if cfg.use_volume else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_pattern")
    async def short_toggle_pattern(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = TradeCfg.from_json(user.short_cfg)
        cfg.use_pattern = not cfg.use_pattern
        await cb.answer("Паттерн ШОРТ " + ("✅" if cfg.use_pattern else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_htf")
    async def short_toggle_htf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = TradeCfg.from_json(user.short_cfg)
        cfg.use_htf = not cfg.use_htf
        await cb.answer("HTF ШОРТ " + ("✅" if cfg.use_htf else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_trend_only")
    async def short_toggle_trend_only(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = TradeCfg.from_json(user.short_cfg)
        cfg.trend_only = not cfg.trend_only
        await cb.answer("📊 Тренд ШОРТ " + ("✅" if cfg.trend_only else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_rsi_period_"))
    async def short_set_rsi_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_rsi_period_", ""))
        await cb.answer("✅ RSI " + str(v))
        _update_short_field(user, "rsi_period", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_rsi_ob_"))
    async def short_set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_rsi_ob_", ""))
        await cb.answer("✅ RSI OB " + str(v))
        _update_short_field(user, "rsi_overbought", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_rsi_os_"))
    async def short_set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_rsi_os_", ""))
        await cb.answer("✅ RSI OS " + str(v))
        _update_short_field(user, "rsi_oversold", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_vol_mult_"))
    async def short_set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_vol_mult_", ""))
        await cb.answer("✅ x" + str(v))
        _update_short_field(user, "volume_mult", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "menu_short_quality")
    async def menu_short_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Качество ШОРТ</b>", kb_short_quality(user))

    @dp.callback_query(F.data.startswith("short_set_quality_"))
    async def short_set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_quality_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "min_quality", v); await um.save(user)
        await safe_edit(cb, "⭐ <b>Качество ШОРТ</b>", kb_short_quality(user))

    @dp.callback_query(F.data == "menu_short_cooldown")
    async def menu_short_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔁 <b>Cooldown ШОРТ</b>", kb_short_cooldown(user))

    @dp.callback_query(F.data.startswith("short_set_cooldown_"))
    async def short_set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_cooldown_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "cooldown_bars", v); await um.save(user)
        await safe_edit(cb, "🔁 <b>Cooldown ШОРТ</b>", kb_short_cooldown(user))

    @dp.callback_query(F.data == "menu_short_sl")
    async def menu_short_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data.startswith("short_set_atr_period_"))
    async def short_set_atr_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_atr_period_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "atr_period", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data.startswith("short_set_atr_mult_"))
    async def short_set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_atr_mult_", ""))
        await cb.answer("✅ x" + str(v))
        _update_short_field(user, "atr_mult", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data.startswith("short_set_risk_"))
    async def short_set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_risk_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_short_field(user, "max_risk_pct", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data == "menu_short_targets")
    async def menu_short_targets(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Цели ШОРТ</b>", kb_short_targets(user))

    @dp.callback_query(F.data.startswith("short_set_tp1_"))
    async def short_set_tp1(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_tp1_", ""))
        await cb.answer("✅ TP1 " + str(v) + "R")
        _update_short_field(user, "tp1_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели ШОРТ</b>", kb_short_targets(user))

    @dp.callback_query(F.data.startswith("short_set_tp2_"))
    async def short_set_tp2(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_tp2_", ""))
        await cb.answer("✅ TP2 " + str(v) + "R")
        _update_short_field(user, "tp2_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели ШОРТ</b>", kb_short_targets(user))

    @dp.callback_query(F.data.startswith("short_set_tp3_"))
    async def short_set_tp3(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_tp3_", ""))
        await cb.answer("✅ TP3 " + str(v) + "R")
        _update_short_field(user, "tp3_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели ШОРТ</b>", kb_short_targets(user))

    @dp.callback_query(F.data == "menu_short_volume")
    async def menu_short_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Объём ШОРТ</b>", kb_short_volume(user))

    @dp.callback_query(F.data.startswith("short_set_volume_"))
    async def short_set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_volume_", ""))
        await cb.answer("✅ $" + str(int(v)))
        _update_short_field(user, "min_volume_usdt", v); await um.save(user)
        await safe_edit(cb, "💰 <b>Объём ШОРТ</b>", kb_short_volume(user))

    @dp.callback_query(F.data == "reset_short_cfg")
    async def reset_short_cfg(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer("🔄 Сброс ШОРТ настроек")
        user.short_cfg = TradeCfg().to_json(); await um.save(user)
        await safe_edit(cb, cfg_text(user.get_short_cfg(), "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    # ─── РЕЖИМ ОБА ────────────────────────────────────

    @dp.callback_query(F.data == "mode_both")
    async def mode_both(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "toggle_both")
    async def toggle_both(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config)); return
        is_active = user.active and user.scan_mode == "both"
        if not is_active and not _get_user_strategy(user.user_id):
            await cb.answer()
            await safe_edit(cb, _strategy_text(user.user_id), _kb_strategy_select()); return
        user.active = not is_active
        user.scan_mode = "both" if user.active else user.scan_mode
        user.long_active = user.active
        user.short_active = user.active
        await cb.answer("⚡ ОБА " + ("✅ включены" if user.active else "❌ выключены"))
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_tf")
    async def menu_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📊 <b>Таймфрейм</b>\n\nТекущий: <b>" + user.timeframe + "</b>", kb_timeframes(user.timeframe))

    @dp.callback_query(F.data.startswith("set_tf_"))
    async def set_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = cb.data.replace("set_tf_", "")
        await cb.answer("✅ " + v)
        user.timeframe = v; await um.save(user)
        await safe_edit(cb, "📊 <b>Таймфрейм</b>\n\nТекущий: <b>" + v + "</b>", kb_timeframes(v))

    @dp.callback_query(F.data == "menu_interval")
    async def menu_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔄 <b>Интервал сканирования</b>\n\nТекущий: <b>" + str(user.scan_interval//60) + " мин.</b>", kb_intervals(user.scan_interval))

    @dp.callback_query(F.data.startswith("set_interval_"))
    async def set_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_interval_", ""))
        await cb.answer("✅ " + str(v//60) + " мин.")
        user.scan_interval = v; await um.save(user)
        await safe_edit(cb, "🔄 <b>Интервал</b>\n\nТекущий: <b>" + str(v//60) + " мин.</b>", kb_intervals(v))

    # ─── ОБЩИЕ НАСТРОЙКИ (ОБА / legacy) ──────────────

    @dp.callback_query(F.data == "menu_settings")
    async def menu_settings(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, cfg_text(user.shared_cfg(), "⚙️ <b>Настройки</b>"), kb_settings())

    @dp.callback_query(F.data == "menu_pivots")
    async def menu_pivots(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_pivot_"))
    async def set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_pivot_", ""))
        await cb.answer("✅ " + str(v))
        user.pivot_strength = v; await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_age_"))
    async def set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_age_", ""))
        await cb.answer("✅ " + str(v))
        _update_shared_field(user, "max_level_age", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_retest_"))
    async def set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_retest_", ""))
        await cb.answer("✅ " + str(v))
        _update_shared_field(user, "min_retests", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_buffer_"))
    async def set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_buffer_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_shared_field(user, "buffer_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_zone_pct_"))
    async def set_zone_pct(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_zone_pct_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_shared_field(user, "zone_width_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_dist_pct_"))
    async def set_dist_pct(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_dist_pct_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_shared_field(user, "max_dist_pct", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_max_tests_"))
    async def set_max_tests(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_max_tests_", ""))
        await cb.answer("✅ " + str(v))
        _update_shared_field(user, "max_tests", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_min_rr_"))
    async def set_min_rr(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_min_rr_", ""))
        await cb.answer("✅ " + str(v))
        _update_shared_field(user, "min_rr", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты</b>", kb_pivots(user))

    @dp.callback_query(F.data == "menu_ema")
    async def menu_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>EMA настройки</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_fast_"))
    async def set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_ema_fast_", ""))
        await cb.answer("✅ EMA fast " + str(v))
        _update_shared_field(user, "ema_fast", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA настройки</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_slow_"))
    async def set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_ema_slow_", ""))
        await cb.answer("✅ EMA slow " + str(v))
        _update_shared_field(user, "ema_slow", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA настройки</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_htf_ema_"))
    async def set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_htf_ema_", ""))
        await cb.answer("✅ HTF EMA " + str(v))
        _update_shared_field(user, "htf_ema", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA настройки</b>", kb_ema(user))

    @dp.callback_query(F.data == "menu_filters")
    async def menu_filters(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_rsi")
    async def toggle_rsi(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = user.shared_cfg()
        cfg.use_rsi = not cfg.use_rsi
        await cb.answer("RSI " + ("✅" if cfg.use_rsi else "❌"))
        _apply_shared_cfg(user, cfg); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_volume")
    async def toggle_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = user.shared_cfg()
        cfg.use_volume = not cfg.use_volume
        await cb.answer("Объём " + ("✅" if cfg.use_volume else "❌"))
        _apply_shared_cfg(user, cfg); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_pattern")
    async def toggle_pattern(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = user.shared_cfg()
        cfg.use_pattern = not cfg.use_pattern
        await cb.answer("Паттерн " + ("✅" if cfg.use_pattern else "❌"))
        _apply_shared_cfg(user, cfg); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_htf")
    async def toggle_htf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = user.shared_cfg()
        cfg.use_htf = not cfg.use_htf
        await cb.answer("HTF " + ("✅" if cfg.use_htf else "❌"))
        _apply_shared_cfg(user, cfg); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_trend_only")
    async def toggle_trend_only(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg = user.shared_cfg()
        cfg.trend_only = not cfg.trend_only
        await cb.answer("📊 Тренд " + ("✅" if cfg.trend_only else "❌"))
        _apply_shared_cfg(user, cfg); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_period_"))
    async def set_rsi_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_rsi_period_", ""))
        await cb.answer("✅ RSI " + str(v))
        _update_shared_field(user, "rsi_period", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_ob_"))
    async def set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_rsi_ob_", ""))
        await cb.answer("✅ RSI OB " + str(v))
        _update_shared_field(user, "rsi_overbought", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_os_"))
    async def set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_rsi_os_", ""))
        await cb.answer("✅ RSI OS " + str(v))
        _update_shared_field(user, "rsi_oversold", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_vol_mult_"))
    async def set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_vol_mult_", ""))
        await cb.answer("✅ x" + str(v))
        _update_shared_field(user, "volume_mult", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры</b>", kb_filters(user))

    @dp.callback_query(F.data == "menu_quality")
    async def menu_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Мин. качество</b>", kb_quality(user.shared_cfg().min_quality))

    @dp.callback_query(F.data.startswith("set_quality_"))
    async def set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_quality_", ""))
        await cb.answer("✅ " + str(v))
        _update_shared_field(user, "min_quality", v); await um.save(user)
        await safe_edit(cb, "⭐ <b>Мин. качество</b>", kb_quality(v))

    @dp.callback_query(F.data == "menu_cooldown")
    async def menu_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔁 <b>Cooldown</b>", kb_cooldown(user.shared_cfg().cooldown_bars))

    @dp.callback_query(F.data.startswith("set_cooldown_"))
    async def set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_cooldown_", ""))
        await cb.answer("✅ " + str(v))
        _update_shared_field(user, "cooldown_bars", v); await um.save(user)
        await safe_edit(cb, "🔁 <b>Cooldown</b>", kb_cooldown(v))

    @dp.callback_query(F.data == "menu_sl")
    async def menu_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Стоп-лосс</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_period_"))
    async def set_atr_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("set_atr_period_", ""))
        await cb.answer("✅ ATR " + str(v))
        _update_shared_field(user, "atr_period", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_mult_"))
    async def set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_atr_mult_", ""))
        await cb.answer("✅ x" + str(v))
        _update_shared_field(user, "atr_mult", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_risk_"))
    async def set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_risk_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_shared_field(user, "max_risk_pct", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс</b>", kb_sl(user))

    @dp.callback_query(F.data == "menu_targets")
    async def menu_targets(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Цели (TP)</b>", kb_targets(user))

    @dp.callback_query(F.data.startswith("set_tp1_"))
    async def set_tp1(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_tp1_", ""))
        await cb.answer("✅ TP1 " + str(v) + "R")
        _update_shared_field(user, "tp1_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели (TP)</b>", kb_targets(user))

    @dp.callback_query(F.data.startswith("set_tp2_"))
    async def set_tp2(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_tp2_", ""))
        await cb.answer("✅ TP2 " + str(v) + "R")
        _update_shared_field(user, "tp2_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели (TP)</b>", kb_targets(user))

    @dp.callback_query(F.data.startswith("set_tp3_"))
    async def set_tp3(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_tp3_", ""))
        await cb.answer("✅ TP3 " + str(v) + "R")
        _update_shared_field(user, "tp3_rr", v); await um.save(user)
        await safe_edit(cb, "🎯 <b>Цели (TP)</b>", kb_targets(user))

    @dp.callback_query(F.data == "menu_volume")
    async def menu_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Мин. объём</b>", kb_volume(user.shared_cfg().min_volume_usdt))

    @dp.callback_query(F.data.startswith("set_volume_"))
    async def set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("set_volume_", ""))
        await cb.answer("✅ $" + str(int(v)))
        _update_shared_field(user, "min_volume_usdt", v); await um.save(user)
        await safe_edit(cb, "💰 <b>Мин. объём</b>", kb_volume(v))

    @dp.callback_query(F.data == "menu_notify")
    async def menu_notify(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔔 <b>Уведомления</b>", kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify")
    async def toggle_notify(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.notifications_enabled = not user.notifications_enabled
        await cb.answer("🔔 Уведомления " + ("✅" if user.notifications_enabled else "❌"))
        await um.save(user)
        await safe_edit(cb, "🔔 <b>Уведомления</b>", kb_notify(user))

    # ─── FSM: РЕДАКТИРОВАНИЕ TP ───────────────────────

    @dp.callback_query(F.data == "edit_tp1")
    async def edit_tp1_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.tp1)
        await cb.message.answer("✏️ Введи цену TP1 (число):")

    @dp.callback_query(F.data == "edit_tp2")
    async def edit_tp2_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.tp2)
        await cb.message.answer("✏️ Введи цену TP2 (число):")

    @dp.callback_query(F.data == "edit_tp3")
    async def edit_tp3_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.tp3)
        await cb.message.answer("✏️ Введи цену TP3 (число):")

    @dp.message(EditState.tp1)
    async def save_tp1(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число, например 0.452"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp1=v)
        await state.clear()
        await msg.answer("✅ TP1 сохранён: " + str(v))

    @dp.message(EditState.tp2)
    async def save_tp2(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число, например 0.452"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp2=v)
        await state.clear()
        await msg.answer("✅ TP2 сохранён: " + str(v))

    @dp.message(EditState.tp3)
    async def save_tp3(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число, например 0.452"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp3=v)
        await state.clear()
        await msg.answer("✅ TP3 сохранён: " + str(v))

    # LONG TP FSM
    @dp.callback_query(F.data == "edit_long_tp1")
    async def edit_long_tp1_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.long_tp1)
        await cb.message.answer("✏️ TP1 ЛОНГ (число):")

    @dp.callback_query(F.data == "edit_long_tp2")
    async def edit_long_tp2_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.long_tp2)
        await cb.message.answer("✏️ TP2 ЛОНГ (число):")

    @dp.callback_query(F.data == "edit_long_tp3")
    async def edit_long_tp3_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.long_tp3)
        await cb.message.answer("✏️ TP3 ЛОНГ (число):")

    @dp.message(EditState.long_tp1)
    async def save_long_tp1(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp1=v)
        await state.clear()
        await msg.answer("✅ TP1 ЛОНГ: " + str(v))

    @dp.message(EditState.long_tp2)
    async def save_long_tp2(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp2=v)
        await state.clear()
        await msg.answer("✅ TP2 ЛОНГ: " + str(v))

    @dp.message(EditState.long_tp3)
    async def save_long_tp3(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp3=v)
        await state.clear()
        await msg.answer("✅ TP3 ЛОНГ: " + str(v))

    # SHORT TP FSM
    @dp.callback_query(F.data == "edit_short_tp1")
    async def edit_short_tp1_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.short_tp1)
        await cb.message.answer("✏️ TP1 ШОРТ (число):")

    @dp.callback_query(F.data == "edit_short_tp2")
    async def edit_short_tp2_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.short_tp2)
        await cb.message.answer("✏️ TP2 ШОРТ (число):")

    @dp.callback_query(F.data == "edit_short_tp3")
    async def edit_short_tp3_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.short_tp3)
        await cb.message.answer("✏️ TP3 ШОРТ (число):")

    @dp.message(EditState.short_tp1)
    async def save_short_tp1(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp1=v)
        await state.clear()
        await msg.answer("✅ TP1 ШОРТ: " + str(v))

    @dp.message(EditState.short_tp2)
    async def save_short_tp2(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp2=v)
        await state.clear()
        await msg.answer("✅ TP2 ШОРТ: " + str(v))

    @dp.message(EditState.short_tp3)
    async def save_short_tp3(msg: Message, state: FSMContext):
        try:
            v = float(msg.text.strip().replace(",", "."))
        except ValueError:
            await msg.answer("❌ Введи число"); return
        data = await state.get_data()
        signal_id = data.get("signal_id")
        if signal_id:
            await db.update_signal_tp(signal_id, tp3=v)
        await state.clear()
        await msg.answer("✅ TP3 ШОРТ: " + str(v))

    # ─── ЗАПИСИ СИГНАЛОВ ──────────────────────────────

    @dp.callback_query(F.data.startswith("sig_records_"))
    async def sig_records(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        parts = cb.data.split("_")
        signal_id = int(parts[2]) if len(parts) > 2 else 0
        records = await db.get_signal_records(signal_id)
        kb = trade_records_keyboard(signal_id, records)
        lines = ["📋 <b>Результаты сигнала</b>"]
        for r in records:
            em = "✅" if r.get("result") == "win" else "❌" if r.get("result") == "loss" else "⏳"
            lines.append(em + " " + str(r.get("result", "—")) + " " + str(r.get("rr", "")))
        await safe_edit(cb, "\n".join(lines) or "Нет записей", kb)

    @dp.callback_query(F.data.startswith("sig_back_"))
    async def sig_back(cb: CallbackQuery):
        await cb.answer()
        signal_id = int(cb.data.replace("sig_back_", ""))
        signal = await db.get_signal(signal_id)
        if not signal:
            await cb.message.answer("Сигнал не найден"); return
        kb = signal_compact_keyboard(signal)
        txt = "📊 <b>Сигнал #" + str(signal_id) + "</b>"
        await safe_edit(cb, txt, kb)

    # ─── РЕЗУЛЬТАТЫ СДЕЛОК ────────────────────────────

    @dp.callback_query(F.data.startswith("res_"))
    async def res_handler(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        parts = cb.data.split("_")
        # res_{result}_{signal_id}   e.g. res_win_123, res_loss_123, res_be_123
        if len(parts) < 3:
            await cb.answer(); return
        result = parts[1]   # win / loss / be / tp1 / tp2 / tp3
        signal_id = int(parts[2])
        await cb.answer("✅ Записано: " + result)
        rr_map = {"tp1": 1.0, "tp2": 2.0, "tp3": 3.0, "win": 2.0, "loss": -1.0, "be": 0.0}
        rr = rr_map.get(result, 0.0)
        await db.add_trade_record(user.user_id, signal_id, result, rr)
        signal = await db.get_signal(signal_id)
        kb = signal_compact_keyboard(signal) if signal else None
        txt = "✅ <b>Результат записан:</b> " + result.upper() + " (" + str(rr) + "R)"
        if signal:
            txt = "📊 " + str(signal.get("symbol", "")) + "\n" + txt
        await safe_edit(cb, txt, kb)

    # ─── НАВИГАЦИЯ ────────────────────────────────────

    @dp.callback_query(F.data == "back_main")
    async def back_main(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        trend = scanner.get_trend() if hasattr(scanner, "get_trend") else {}
        await safe_edit(cb, main_text(user, trend), kb_main(user))

    @dp.callback_query(F.data == "my_stats")
    async def my_stats(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        stats = await db.db_get_user_stats(user.user_id)
        await safe_edit(cb, stats_text(user, stats), kb_back())

    @dp.callback_query(F.data == "my_chart")
    async def my_chart(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        stats = await db.db_get_user_stats(user.user_id)
        if not stats or stats.get("total", 0) < 2:
            await cb.message.answer("📊 Нужно минимум 2 сделки для графика."); return
        # Build equity curve
        records = await db.get_user_records(user.user_id, limit=50)
        equity = [0.0]
        for r in records:
            equity.append(equity[-1] + float(r.get("rr", 0)))
        fig, ax = plt.subplots(figsize=(8, 4))
        color = "green" if equity[-1] >= 0 else "red"
        ax.plot(equity, color=color, linewidth=2)
        ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
        ax.fill_between(range(len(equity)), equity, 0, alpha=0.15, color=color)
        ax.set_title("Equity curve — " + str(len(records)) + " сделок", fontsize=13)
        ax.set_xlabel("Сделки"); ax.set_ylabel("R")
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=120); buf.seek(0); plt.close(fig)
        photo = BufferedInputFile(buf.read(), filename="equity.png")
        await cb.message.answer_photo(photo, caption="📈 Equity curve", reply_markup=kb_back_photo())

    @dp.callback_query(F.data == "back_photo_main")
    async def back_photo_main(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        trend = scanner.get_trend() if hasattr(scanner, "get_trend") else {}
        await cb.message.answer(main_text(user, trend), parse_mode="HTML", reply_markup=kb_main(user))

    # ─── ПОМОЩЬ ───────────────────────────────────────

    @dp.callback_query(F.data == "help_show")
    async def help_show(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, help_text(), kb_help())

    # ─── АДМИН-КОМАНДЫ ────────────────────────────────

    @dp.message(Command("admin"))
    async def cmd_admin(msg: Message):
        if not is_admin(msg.from_user.id): return
        s   = await um.stats_summary()
        prf = scanner.get_perf() if hasattr(scanner, "get_perf") else {}
        cs  = prf.get("cache", {})
        NL  = "\n"
        await msg.answer(
            "👑 <b>Панель администратора</b>" + NL + NL +
            "👥 Всего: <b>" + str(s["total"]) + "</b>  🆓 Триал: <b>" + str(s["trial"]) + "</b>  ✅ Активных: <b>" + str(s["active"]) + "</b>" + NL +
            "🔄 Сканируют: <b>" + str(s["scanning"]) + "</b>" + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "Циклов: <b>" + str(prf.get("cycles",0)) + "</b>  Сигналов: <b>" + str(prf.get("signals",0)) + "</b>  API: <b>" + str(prf.get("api_calls",0)) + "</b>" + NL +
            "Кэш: <b>" + str(cs.get("size",0)) + "</b> ключей | хит <b>" + str(cs.get("ratio",0)) + "%</b>" + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "/give [id] [дней]  /revoke [id]  /ban [id]" + NL +
            "/unban [id]  /userinfo [id]  /broadcast [текст]",
            parse_mode="HTML",
        )

    @dp.message(Command("give"))
    async def cmd_give(msg: Message):
        if not is_admin(msg.from_user.id): return
        parts = msg.text.split()
        if len(parts) < 3:
            await msg.answer("Использование: /give [user_id] [дней]"); return
        try:
            tid = int(parts[1]); days = int(parts[2])
        except ValueError:
            await msg.answer("❌ Неверный формат"); return
        user = await um.get(tid)
        if not user:
            await msg.answer("❌ Пользователь " + str(tid) + " не найден"); return
        user.grant_access(days)
        await um.save(user)
        await msg.answer("✅ Доступ выдан @" + str(user.username or tid) + " на " + str(days) + " дней")
        try:
            await bot.send_message(
                tid,
                "🎉 <b>Доступ открыт!</b>\n\nПодписка на <b>" + str(days) + " дней</b>.\nОсталось: <b>" + user.time_left_str() + "</b>\n\nНажми /menu",
                parse_mode="HTML",
            )
        except Exception: pass

    @dp.message(Command("revoke"))
    async def cmd_revoke(msg: Message):
        if not is_admin(msg.from_user.id): return
        parts = msg.text.split()
        if len(parts) < 2: await msg.answer("Использование: /revoke [id]"); return
        try: tid = int(parts[1])
        except ValueError: return
        user = await um.get(tid)
        if not user: await msg.answer("❌ Не найден"); return
        user.sub_status = "expired"; user.sub_expires = 0
        user.active = False; user.long_active = False; user.short_active = False
        await um.save(user)
        await msg.answer("✅ Доступ отозван у @" + str(user.username or tid))

    @dp.message(Command("ban"))
    async def cmd_ban(msg: Message):
        if not is_admin(msg.from_user.id): return
        parts = msg.text.split()
        if len(parts) < 2: await msg.answer("Использование: /ban [id]"); return
        try: tid = int(parts[1])
        except ValueError: return
        user = await um.get(tid)
        if not user: await msg.answer("❌ Не найден"); return
        user.sub_status = "banned"; user.active = False
        user.long_active = False; user.short_active = False
        await um.save(user)
        await msg.answer("🚫 @" + str(user.username or tid) + " заблокирован")

    @dp.message(Command("unban"))
    async def cmd_unban(msg: Message):
        if not is_admin(msg.from_user.id): return
        parts = msg.text.split()
        if len(parts) < 2: await msg.answer("Использование: /unban [id]"); return
        try: tid = int(parts[1])
        except ValueError: return
        user = await um.get(tid)
        if not user: await msg.answer("❌ Не найден"); return
        user.sub_status = "expired"
        await um.save(user)
        await msg.answer("✅ @" + str(user.username or tid) + " разблокирован")

    @dp.message(Command("userinfo"))
    async def cmd_userinfo(msg: Message):
        if not is_admin(msg.from_user.id): return
        parts = msg.text.split()
        if len(parts) < 2: await msg.answer("Использование: /userinfo [id]"); return
        try: tid = int(parts[1])
        except ValueError: return
        user = await um.get(tid)
        if not user: await msg.answer("❌ Не найден"); return
        stats = await db.db_get_user_stats(tid)
        NL    = "\n"
        await msg.answer(
            "👤 <b>@" + str(user.username or "—") + "</b> (<code>" + str(user.user_id) + "</code>)" + NL +
            "Подписка: <b>" + user.sub_status.upper() + "</b> | Осталось: <b>" + user.time_left_str() + "</b>" + NL +
            "ЛОНГ: " + ("🟢" if user.long_active else "⚫") +
            "  ШОРТ: " + ("🟢" if user.short_active else "⚫") +
            "  ОБА: " + ("🟢" if user.active else "⚫") + NL +
            "Сигналов: <b>" + str(user.signals_received) + "</b>  Сделок: <b>" + str(stats.get("total",0)) + "</b>  R: <b>" + "{:+.2f}".format(stats.get("total_rr",0)) + "R</b>" + NL +
            "Стратегия: <b>" + (_get_user_strategy(tid) or "не выбрана") + "</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(msg: Message):
        if not is_admin(msg.from_user.id): return
        text = msg.text.replace("/broadcast", "", 1).strip()
        if not text: await msg.answer("Использование: /broadcast [текст]"); return
        users  = await um.all_users()
        sent = failed = 0
        for u in users:
            if u.sub_status in ("trial", "active"):
                try:
                    await bot.send_message(u.user_id, "📢 " + text)
                    sent += 1
                    await asyncio.sleep(0.04)
                except Exception:
                    failed += 1
        await msg.answer("📢 Рассылка: ✅ " + str(sent) + "  ❌ " + str(failed))

    # ─── ПРОЧЕЕ ───────────────────────────────────────

    @dp.callback_query(F.data == "noop")
    async def noop(cb: CallbackQuery):
        await cb.answer()

    @dp.callback_query(F.data == "toggle_active_legacy")
    async def toggle_active_legacy(cb: CallbackQuery):
        """Legacy toggle для старых кнопок без режима."""
        user = await um.get_or_create(cb.from_user.id)
        if not user.has_access():
            await cb.answer("❌ Нет доступа", show_alert=True); return
        user.active = not user.active
        await cb.answer("⚡ " + ("✅ Включён" if user.active else "❌ Выключен"))
        await um.save(user)
        trend = scanner.get_trend() if hasattr(scanner, "get_trend") else {}
        await safe_edit(cb, main_text(user, trend), kb_main(user))

    @dp.message(Command("help"))
    async def cmd_help(msg: Message):
        await msg.answer(help_text(), parse_mode="HTML", reply_markup=kb_help())

    # ─── Закрываем register_handlers ──────────────────
    # (конец функции)
