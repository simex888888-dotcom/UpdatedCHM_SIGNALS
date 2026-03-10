"""
handlers.py v5 — CHM BREAKER BOT
Правило: cb.answer() ВСЕГДА первым, до любых await с БД.
Улучшения: /analyze команда, rate-limit, dedup keyboard helper, corr_label в ответе.
"""
import html as _html
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
from user_manager import UserManager, UserSettings, TradeCfg, SMCUserCfg
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
    kb_smc_main, kb_smc_tf, kb_smc_interval, kb_smc_direction,
    kb_smc_confirmations, kb_smc_rr, kb_smc_sl, kb_smc_volume, kb_smc_ob_age,
    kb_smc_mode_long, kb_smc_mode_short, kb_smc_mode_both,
    kb_auto_trade,
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
    Кнопки с url, без callback_data и с callback_data="noop" (заголовки разделов) не трогаются.
    """
    seen_callbacks: set = set()
    new_rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            cb = btn.callback_data
            if cb is None or cb == "noop":
                # url-кнопки и разделители (noop) всегда оставляем
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


class SetupBybitState(StatesGroup):
    api_key    = State()
    api_secret = State()


# ── Тексты ───────────────────────────────────────────

def main_text(user: UserSettings, trend: dict) -> str:
    NL = "\n"
    sub_em  = {"active":"✅","trial":"🆓","expired":"❌","banned":"🚫"}.get(user.sub_status,"❓")
    sub_line = "Подписка: " + sub_em + " " + user.sub_status.upper() + " — " + user.time_left_str()
    strategy = getattr(user, "strategy", "LEVELS")
    if strategy == "SMC":
        long_s  = "🟢 ЛОНГ" if getattr(user, "smc_long_active",  False) else "⚫ лонг"
        short_s = "🟢 ШОРТ" if getattr(user, "smc_short_active", False) else "⚫ шорт"
        both_s  = "🟢 ОБА"  if (user.active and user.scan_mode == "smc_both") else "⚫ оба"
        cfg     = user.get_smc_cfg()
        return (
            "⚡ <b>CHM BREAKER BOT — 🧠 Smart Money</b>" + NL + NL +
            trend_text(trend) + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            long_s + "  |  " + short_s + "  |  " + both_s + NL +
            "Таймфрейм: <b>" + cfg.tf_key + "</b>  Интервал: <b>" + str(cfg.scan_interval // 60) + " мин.</b>" + NL +
            sub_line + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "Выбери режим SMC сканера 👇"
        )
    # ── LEVELS ──
    long_s  = "🟢 ЛОНГ" if user.long_active  else "⚫ лонг выкл"
    short_s = "🟢 ШОРТ" if user.short_active else "⚫ шорт выкл"
    both_s  = "🟢 ОБА"  if (user.active and user.scan_mode == "both") else "⚫ оба выкл"
    return (
        "⚡ <b>CHM BREAKER BOT</b>" + NL + NL +
        trend_text(trend) + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        long_s + "  |  " + short_s + "  |  " + both_s + NL +
        sub_line + NL +
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
        "🤖 <b>CHM BREAKER BOT:</b>" + NL +
        "  📅 3 месяца — <b>" + config.BOT_PRICE_90 + "</b>" + NL +
        "  📅 1 ГОД    — <b>" + config.BOT_PRICE_365 + "</b>" + NL + NL +
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
        "💰 Вход:    <code>" + _fmt_price(sig.entry) + "</code>" + NL +
        "🛑 Стоп:    <code>" + _fmt_price(sig.sl) + "</code>  <i>(-" + "{:.2f}".format(sig.risk_pct) + "%)</i>" + NL + NL +
        "🎯 Цель 1: <code>" + _fmt_price(sig.tp1) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp1)) + "%)</i>" + NL +
        "🎯 Цель 2: <code>" + _fmt_price(sig.tp2) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp2)) + "%)</i>" + NL +
        "🏆 Цель 3: <code>" + _fmt_price(sig.tp3) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp3)) + "%)</i>" + NL +
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
# ── Клавиатуры выбора стратегии ───────────────────────

def _kb_strategy_select() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Уровни (Price Action)", callback_data="strategy_levels"),
            InlineKeyboardButton(text="🧠 Smart Money (SMC)",     callback_data="strategy_smc"),
        ],
    ])

def _kb_strategy_change(current: str = "") -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📊 Уровни", callback_data="strategy_levels"),
            InlineKeyboardButton(text="🧠 SMC",    callback_data="strategy_smc"),
        ],
    ]
    if current == "SMC":
        rows.append([InlineKeyboardButton(text="⚙️ Настройки SMC →", callback_data="smc_settings")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _strategy_text(strategy: str) -> str:
    chosen = ("📊 Уровни (Price Action)" if strategy == "LEVELS"
              else "🧠 Smart Money (SMC)" if strategy == "SMC"
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
        "💰 Вход: <code>" + _fmt_price(sig.entry_low) + " – " + _fmt_price(sig.entry_high) + "</code>" + NL +
        "🛑 Стоп: <code>" + _fmt_price(sig.sl) + "</code>  <i>(-" + "{:.2f}".format(sig.risk_pct) + "%)</i>" + NL + NL +
        "🎯 TP1: <code>" + _fmt_price(sig.tp1) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp1)) + "%)</i>" + NL +
        "🎯 TP2: <code>" + _fmt_price(sig.tp2) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp2)) + "%)</i>" + NL +
        "🏆 TP3: <code>" + _fmt_price(sig.tp3) + "</code>  <i>(+" + "{:.2f}".format(pct(sig.tp3)) + "%)</i>" + NL +
        "📐 R:R = 1:" + str(sig.rr) + NL + NL +
        "⚡ <i>CHM Laboratory — SMC Strategy</i>"
    )


# ── Мультитаймфреймный /analyze (человекоподобный) ───

async def _do_analyze_multitf(symbol: str, fetcher, indicator,
                               strategy: str, bot) -> str:
    """
    Загружает 1H, 4H, 1D свечи. Анализирует уровни на каждом ТФ.
    Возвращает человекоподобный текст с рекомендацией.
    """
    NL = "\n"

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

        df_htf = dfs.get("1D") if dfs.get("1D") is not None else dfs.get("4H")
        df_mtf = dfs.get("4H") if dfs.get("4H") is not None else dfs.get("1H")
        df_ltf = dfs.get("1H")

        if df_htf is None or df_mtf is None:
            return f"⚠️ Недостаточно данных для SMC анализа <b>{symbol}</b>."

        analysis = smc_analyzer.analyze(symbol, df_htf, df_mtf, df_ltf)
        sig = build_smc_signal(symbol, analysis, smc_cfg,
                               tf_htf="1D", tf_mtf="4H", tf_ltf="1H")
        if sig:
            return _analyze_smc_text(symbol, sig)

        # Нет готового сигнала — показываем полный разбор зон по SMC
        return _format_smc_deep_analysis(symbol, analysis, dfs)

    else:
        # LEVELS анализ (Price Action)
        best_sig = None
        tf_analyses = {}
        zones_data = {}

        for tf, df in dfs.items():
            try:
                df_htf_l = dfs.get("1D") if tf != "1D" else None
                sig = indicator.analyze_on_demand(symbol, df, df_htf_l, df_btc, df_eth)
                tf_analyses[tf] = sig
                if sig and (best_sig is None or sig.quality > best_sig.quality):
                    best_sig = sig
                # Собираем зоны для dual-scenario независимо от наличия сигнала
                try:
                    atr_s  = indicator._atr(df, indicator.cfg.ATR_PERIOD)
                    atr_now = float(atr_s.iloc[-1])
                    ema50  = indicator._ema(df["close"], indicator.cfg.EMA_FAST)
                    ema200 = indicator._ema(df["close"], indicator.cfg.EMA_SLOW)
                    sup, res = indicator._get_zones(df, indicator.cfg.PIVOT_STRENGTH, atr_now)
                    c_now  = float(df["close"].iloc[-1])
                    rsi_s  = indicator._rsi(df["close"], indicator.cfg.RSI_PERIOD)
                    rsi_now = float(rsi_s.iloc[-1])
                    vol_ma  = df["volume"].rolling(indicator.cfg.VOL_LEN).mean()
                    vol_ratio = (float(df["volume"].iloc[-1]) /
                                 float(vol_ma.iloc[-1]) if vol_ma.iloc[-1] > 0 else 1.0)
                    zones_data[tf] = {
                        "sup": sup, "res": res,
                        "price": c_now, "atr": atr_now,
                        "rsi": rsi_now, "vol_ratio": vol_ratio,
                        "bull_local": c_now > ema50.iloc[-1] > ema200.iloc[-1],
                        "bear_local": c_now < ema50.iloc[-1] < ema200.iloc[-1],
                    }
                except Exception:
                    pass
            except Exception:
                tf_analyses[tf] = None

        return _format_multitf_levels_text(symbol, tf_analyses, tf_labels, best_sig, zones_data, dfs)


def _format_multitf_levels_text(symbol: str, tf_analyses: dict,
                                  tf_labels: dict, best_sig,
                                  zones_data: dict = None, dfs: dict = None) -> str:
    """Детальный анализ уровней: всегда два сценария LONG и SHORT."""
    NL = "\n"
    cls_map = {1: "Абсолютный", 2: "Сильный", 3: "Рабочий"}
    clean_sym = symbol.replace("-USDT-SWAP", "").replace("-USDT", "")
    zones_data = zones_data or {}

    # Берём данные лучшего TF (4H приоритет)
    z4h = zones_data.get("4H") or zones_data.get("1H") or zones_data.get("1D") or {}
    z1d = zones_data.get("1D") or {}
    current_price = z4h.get("price", 0)
    rsi_now       = z4h.get("rsi", 50)
    vol_ratio     = z4h.get("vol_ratio", 1.0)
    bull_local    = z4h.get("bull_local", False)
    bear_local    = z4h.get("bear_local", False)

    # Все зоны по всем ТФ (объединяем, приоритет HTF)
    all_sup: list = []
    all_res: list = []
    for tf in ["1D", "4H", "1H"]:
        zd = zones_data.get(tf, {})
        for z in zd.get("sup", []):
            z2 = dict(z); z2["_tf"] = tf
            all_sup.append(z2)
        for z in zd.get("res", []):
            z2 = dict(z); z2["_tf"] = tf
            all_res.append(z2)

    # Тренд из 1D (HTF)
    z1d_data = zones_data.get("1D", {})
    htf_bull = z1d_data.get("bull_local", False)
    htf_bear = z1d_data.get("bear_local", False)
    if htf_bull:
        trend_str = "📈 Бычий"
    elif htf_bear:
        trend_str = "📉 Медвежий"
    else:
        trend_str = "↔️ Боковик"

    # ── Вспомогательная: построить план от уровня ───────────────────────────
    def _plan_from_level(is_long: bool, entry_zone: dict,
                         all_opposite: list, current_p: float) -> dict:
        """Строим план входа от уровня поддержки (LONG) или сопротивления (SHORT)."""
        lvl       = entry_zone["price"]
        lvl_class = entry_zone.get("class", 3)
        hits      = entry_zone.get("hits", 1)
        tf_src    = entry_zone.get("_tf", "4H")
        zone_buf  = lvl * 0.007   # 0.7% зона

        if is_long:
            entry_lo = lvl - zone_buf
            entry_hi = lvl + zone_buf
            sl       = lvl - zone_buf * 2.5  # стоп за структуру уровня
            # TP1: ближайшее сопротивление выше
            cands = sorted([z["price"] for z in all_opposite if z["price"] > lvl * 1.005])
            tp1   = cands[0] if cands else lvl + abs(lvl - sl) * 2.5
            tp2   = cands[1] if len(cands) > 1 else (lvl + abs(lvl - sl) * 4)
            tp3   = cands[2] if len(cands) > 2 else (lvl + abs(lvl - sl) * 6)
        else:
            entry_lo = lvl - zone_buf
            entry_hi = lvl + zone_buf
            sl       = lvl + zone_buf * 2.5
            # TP1: ближайшая поддержка ниже
            cands = sorted([z["price"] for z in all_opposite if z["price"] < lvl * 0.995], reverse=True)
            tp1   = cands[0] if cands else lvl - abs(sl - lvl) * 2.5
            tp2   = cands[1] if len(cands) > 1 else (lvl - abs(sl - lvl) * 4)
            tp3   = cands[2] if len(cands) > 2 else (lvl - abs(sl - lvl) * 6)

        return dict(
            lvl=lvl, lvl_class=lvl_class, hits=hits, tf_src=tf_src,
            entry_lo=entry_lo, entry_hi=entry_hi,
            sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        )

    # ── Ищем лучшие зоны для каждого сценария ─────────────────────────────
    def _best_zone(zones: list, is_long: bool, current_p: float):
        """Ближайшая значимая зона для входа."""
        if not zones or not current_p:
            return None
        # Сортируем по близости к текущей цене, с бонусом за класс
        def score(z):
            dist = abs(z["price"] - current_p) / current_p
            cls_bonus = (4 - z.get("class", 3)) * 0.005  # class 1 = +0.015 бонус
            return dist - cls_bonus
        candidates = sorted(zones, key=score)
        return candidates[0] if candidates else None

    long_zone  = _best_zone(all_sup, True,  current_price)
    short_zone = _best_zone(all_res, False, current_price)

    long_plan  = _plan_from_level(True,  long_zone,  all_res, current_price) if long_zone  else None
    short_plan = _plan_from_level(False, short_zone, all_sup, current_price) if short_zone else None

    # ── Приоритет сценариев ────────────────────────────────────────────────
    long_score  = (htf_bull * 3 + bull_local * 2 +
                   (rsi_now < 40) * 2 + (vol_ratio > 1.5) * 1 +
                   (long_zone and long_zone.get("class", 3) == 1) * 2)
    short_score = (htf_bear * 3 + bear_local * 2 +
                   (rsi_now > 60) * 2 + (vol_ratio > 1.5) * 1 +
                   (short_zone and short_zone.get("class", 3) == 1) * 2)
    long_is_priority = long_score >= short_score

    # ── Рендер одного сценария ────────────────────────────────────────────
    def _render_plan(is_long: bool, plan: dict, is_priority: bool,
                     sig: "SignalResult | None") -> list:
        lines = []
        tag   = "🔥 ПРИОРИТЕТНЫЙ" if is_priority else "⚠️ КОНТР-ТРЕНДОВЫЙ"
        dir_em = "🟢 LONG" if is_long else "🔴 SHORT"
        lines.append(f"{tag} СЦЕНАРИЙ: {dir_em}" + NL)

        lvl_name = cls_map.get(plan["lvl_class"], "Рабочий")
        q_stars  = "⭐" * (4 - plan["lvl_class"]) + "☆" * (plan["lvl_class"] - 1)
        hits = plan["hits"]
        hits_str = f"{hits} касания" if hits <= 4 else f"{hits} касаний"
        lines.append(f"Уровень {plan['tf_src']}: <code>{_fmt_price(plan['lvl'])}</code> — "
                     f"{lvl_name} ({hits_str})  {q_stars}" + NL + NL)

        # ── Логика входа как трейдер ──────────────────────────────────────
        setup_type = (sig.breakout_type if sig and sig.direction == ("LONG" if is_long else "SHORT")
                      else ("Отскок от поддержки" if is_long else "Отскок от сопротивления"))
        if "Ложный пробой" in setup_type or "Fakeout" in setup_type:
            setup_label = "Ложный пробой"
            if is_long:
                setup_desc = ("Цена ушла ниже уровня, но быстро вернулась — "
                              "ловушка для продавцов. Вход после возврата за уровень.")
            else:
                setup_desc = ("Цена пробила уровень вверх, но не закрепилась — "
                              "ловушка для покупателей. Вход после возврата под уровень.")
        elif "SFP" in setup_type:
            setup_label = "Захват ликвидности (SFP)"
            if is_long:
                setup_desc = ("Пробой ниже ликвидности со быстрым возвратом — "
                              "бычья ловушка. Вход при закрытии свечи выше уровня.")
            else:
                setup_desc = ("Пробой выше ликвидности со быстрым возвратом — "
                              "медвежья ловушка. Вход при закрытии свечи ниже уровня.")
        elif "Ретест" in setup_type:
            setup_label = "Ретест пробитого уровня"
            if is_long:
                setup_desc = ("Бывшее сопротивление стало поддержкой — "
                              "уровень сменил роль. Ретест подтверждает смену и даёт точку входа.")
            else:
                setup_desc = ("Бывшая поддержка стала сопротивлением — "
                              "уровень сменил роль. Ретест = точка входа в шорт.")
        elif "Пробой" in setup_type:
            setup_label = "Пробой уровня"
            if is_long:
                setup_desc = "Пробой сопротивления вверх с закреплением. Вход на импульсе."
            else:
                setup_desc = "Пробой поддержки вниз с закреплением. Вход на импульсе."
        else:
            setup_label = "Отбой от уровня"
            if is_long:
                setup_desc = ("Цена приближается к поддержке. "
                              "Ждать замедления и бычьей свечи-подтверждения.")
            else:
                setup_desc = ("Цена приближается к сопротивлению. "
                              "Ждать отказа и медвежьей свечи-подтверждения.")

        htf_ctx = ("✅ Старший ТФ бычий — сделка по тренду." if (is_long and htf_bull)
                   else "✅ Старший ТФ медвежий — сделка по тренду." if (not is_long and htf_bear)
                   else "⚠️ Старший ТФ нейтрален — повышенная осторожность.")
        lines.append(f"🧠 ЛОГИКА ВХОДА: <i>{setup_label}</i>" + NL)
        lines.append(f"   {setup_desc}" + NL)
        lines.append(f"   {htf_ctx}" + NL + NL)
        # ─────────────────────────────────────────────────────────────────

        lines.append(f"🎯 ЗОНА ВХОДА:" + NL)
        lines.append(f"   <code>{_fmt_price(plan['entry_lo'])} – {_fmt_price(plan['entry_hi'])}</code>" + NL + NL)

        sl_pct = _pct_diff(plan["sl"], plan["lvl"])
        lines.append(f"🛑 СТОП-ЛОСС: <code>{_fmt_price(plan['sl'])}</code>  (-{sl_pct})" + NL)
        lines.append(f"   {'Ниже' if is_long else 'Выше'} зоны с буфером 2.5×" + NL + NL)

        entry_mid = (plan["entry_lo"] + plan["entry_hi"]) / 2
        tp1_pct = _pct_diff(plan["tp1"], entry_mid)
        tp2_pct = _pct_diff(plan["tp2"], entry_mid)
        tp3_pct = _pct_diff(plan["tp3"], entry_mid)
        lines.append("🎯 ЦЕЛИ:" + NL)
        lines.append(f"   TP1: <code>{_fmt_price(plan['tp1'])}</code>  (+{tp1_pct})  "
                     f"{_rr(entry_mid, plan['sl'], plan['tp1'])}  ← ближайший уровень" + NL)
        lines.append(f"   TP2: <code>{_fmt_price(plan['tp2'])}</code>  (+{tp2_pct})  "
                     f"{_rr(entry_mid, plan['sl'], plan['tp2'])}" + NL)
        lines.append(f"   TP3: <code>{_fmt_price(plan['tp3'])}</code>  (+{tp3_pct})  "
                     f"{_rr(entry_mid, plan['sl'], plan['tp3'])}" + NL + NL)

        # Факторы
        lines.append("📋 ФАКТОРЫ:" + NL)
        if is_long:
            lines.append(f"{'✅' if htf_bull else '⚠️'} HTF тренд: {trend_str}" + NL)
            lines.append(f"{'✅' if bull_local else '⚠️'} 4H тренд: {'📈 Бычий' if bull_local else '📉 Медвежий/Боковик'}" + NL)
            lines.append(f"{'✅' if rsi_now < 45 else '⚠️'} RSI: <code>{rsi_now:.0f}</code> "
                         f"{'— перепродан ✅' if rsi_now < 35 else '— нейтральный' if rsi_now < 55 else '— высокий ⚠️'}" + NL)
            lines.append(f"{'✅' if vol_ratio > 1.3 else '—'} Объём: x{vol_ratio:.1f} {'— подтверждён' if vol_ratio > 1.3 else ''}" + NL)
            lines.append(f"{'✅' if plan['lvl_class'] == 1 else '⭐' if plan['lvl_class'] == 2 else '—'} "
                         f"Уровень: {lvl_name}" + NL)
        else:
            lines.append(f"{'✅' if htf_bear else '⚠️'} HTF тренд: {trend_str}" + NL)
            lines.append(f"{'✅' if bear_local else '⚠️'} 4H тренд: {'📉 Медвежий' if bear_local else '📈 Бычий/Боковик'}" + NL)
            lines.append(f"{'✅' if rsi_now > 55 else '⚠️'} RSI: <code>{rsi_now:.0f}</code> "
                         f"{'— перекуплен ✅' if rsi_now > 65 else '— нейтральный' if rsi_now > 45 else '— низкий ⚠️'}" + NL)
            lines.append(f"{'✅' if vol_ratio > 1.3 else '—'} Объём: x{vol_ratio:.1f} {'— подтверждён' if vol_ratio > 1.3 else ''}" + NL)
            lines.append(f"{'✅' if plan['lvl_class'] == 1 else '⭐' if plan['lvl_class'] == 2 else '—'} "
                         f"Уровень: {lvl_name}" + NL)

        # Если есть реальный сигнал — добавляем
        if sig and sig.direction == ("LONG" if is_long else "SHORT"):
            lines.append(NL + f"⚡ <b>АКТИВНЫЙ СИГНАЛ</b>: {_html.escape(sig.pattern or sig.breakout_type or '')}" + NL)
            lines.append(f"   Вход сейчас: <code>{_fmt_price(sig.entry)}</code>  "
                         f"SL: <code>{_fmt_price(sig.sl)}</code>" + NL)
            lines.append(f"   Качество: {'⭐' * sig.quality}  RSI: {sig.rsi:.0f}" + NL)
            if sig.corr_label:
                lines.append(f"   {_html.escape(sig.corr_label)}" + NL)

        lines.append(NL + "⏳ УСЛОВИЕ ВХОДА:" + NL)
        lines.append(f"   Возврат в зону <code>{_fmt_price(plan['entry_lo'])}–{_fmt_price(plan['entry_hi'])}</code>" + NL)
        lines.append(f"   + {'бычья' if is_long else 'медвежья'} свеча-подтверждение" + NL)
        if is_long and rsi_now > 60:
            lines.append("   ⚠️ RSI высокий — ждать снижения RSI перед входом" + NL)
        if not is_long and rsi_now < 40:
            lines.append("   ⚠️ RSI низкий — ждать роста RSI перед входом" + NL)
        return lines

    # ── Заголовок ─────────────────────────────────────────────────────────
    p = []
    p.append(f"🔍 <b>АНАЛИЗ: {clean_sym} — Уровни (Price Action)</b>" + NL + NL)

    # Мультитаймфреймный обзор
    p.append("🌍 <b>РЫНОЧНЫЙ КОНТЕКСТ</b>" + NL)
    p.append(f"Тренд HTF (1D): <b>{trend_str}</b>" + NL)
    p.append(f"Тренд MTF (4H): <b>{'📈 Бычий' if bull_local else '📉 Медвежий' if bear_local else '↔️ Боковик'}</b>" + NL)
    if current_price:
        p.append(f"Текущая цена: <code>{_fmt_price(current_price)}</code>" + NL)
    p.append(f"RSI (4H): <code>{rsi_now:.0f}</code>  |  Объём: x{vol_ratio:.1f}" + NL)

    p.append(NL + "📊 <b>ОБЗОР ПО ТАЙМФРЕЙМАМ:</b>" + NL)
    tf_order = ["1H", "4H", "1D"]
    for tf in tf_order:
        sig   = tf_analyses.get(tf)
        label = tf_labels.get(tf, tf)
        zd    = zones_data.get(tf, {})
        n_sup = len(zd.get("sup", []))
        n_res = len(zd.get("res", []))
        if sig:
            dir_em   = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"
            stars    = "⭐" * sig.quality
            cls_name = cls_map.get(sig.level_class, "")
            p.append(f"  {label} ({tf}): {dir_em}  {stars}  —  {_html.escape(sig.pattern or sig.breakout_type or '')}" + NL)
            p.append(f"    Уровень: <code>{_fmt_price(sig.entry)}</code>  ({cls_name})" + NL)
        else:
            p.append(f"  {label} ({tf}): нет сигнала у уровня  [{n_sup} sup / {n_res} res]" + NL)

    p.append(NL)

    # Находим лучший сигнал для каждого направления
    best_long  = next((tf_analyses[tf] for tf in ["4H","1D","1H"]
                       if tf_analyses.get(tf) and tf_analyses[tf].direction == "LONG"), None)
    best_short = next((tf_analyses[tf] for tf in ["4H","1D","1H"]
                       if tf_analyses.get(tf) and tf_analyses[tf].direction == "SHORT"), None)

    # ── Рендеринг обоих сценариев ──────────────────────────────────────────
    if long_is_priority:
        if long_plan:
            p.extend(_render_plan(True,  long_plan,  True,  best_long))
        p.append(NL)
        if short_plan:
            p.extend(_render_plan(False, short_plan, False, best_short))
    else:
        if short_plan:
            p.extend(_render_plan(False, short_plan, True,  best_short))
        p.append(NL)
        if long_plan:
            p.extend(_render_plan(True,  long_plan,  False, best_long))

    if not long_plan and not short_plan:
        p.append("⚠️ Ключевых уровней не найдено — недостаточно данных." + NL)
        p.append("Попробуйте более ликвидную монету или другой таймфрейм." + NL)

    p.append(NL)

    # ── Итог ──────────────────────────────────────────────────────────────
    p.append("🏆 <b>ЛУЧШИЙ СЦЕНАРИЙ:</b>" + NL)
    best_plan = (long_plan if long_is_priority else short_plan) or long_plan or short_plan
    best_dir  = "🟢 LONG" if long_is_priority else "🔴 SHORT"
    if best_plan:
        conf = "🔥 Высокая" if max(long_score, short_score) >= 6 else \
               "✅ Средняя" if max(long_score, short_score) >= 3 else "⚠️ Низкая"
        p.append(f"{best_dir} от зоны <code>{_fmt_price(best_plan['entry_lo'])}–{_fmt_price(best_plan['entry_hi'])}</code>" + NL)
        p.append(f"SL: <code>{_fmt_price(best_plan['sl'])}</code>  →  TP1: <code>{_fmt_price(best_plan['tp1'])}</code>  "
                 f"TP3: <code>{_fmt_price(best_plan['tp3'])}</code>" + NL)
        p.append(f"Уверенность: {conf}" + NL)

    p.append(NL + "⚡ <i>CHM Laboratory — Price Action Strategy</i>")
    return "".join(p)




def _fmt_price(v) -> str:
    """Форматирует цену без научной нотации."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v >= 10_000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:,.1f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _pct_diff(a, b) -> str:
    """Процентная разница от b до a."""
    if not b:
        return "?"
    return f"{abs((a - b) / b * 100):.2f}%"


def _rr(entry, sl, tp) -> str:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if not risk:
        return "?"
    return f"1:{reward / risk:.1f}"


def _format_smc_deep_analysis(symbol: str, analysis: dict, dfs: dict) -> str:
    """Детальный SMC анализ: всегда оба сценария LONG и SHORT."""
    NL = "\n"
    struct = analysis.get("structure", {})
    pd_z   = analysis.get("pd_zone",   {})
    ob     = analysis.get("ob",         {})
    liq    = analysis.get("liquidity",  {})
    fvg    = analysis.get("fvg",        {})

    trend   = struct.get("trend",        "RANGING")
    choch   = struct.get("choch",        False)
    bos_d   = struct.get("bos",          {}).get("detected", False)
    zone    = pd_z.get("zone",           "NEUTRAL")
    pos     = pd_z.get("position_pct",   50.0)
    hi      = pd_z.get("range_high",     0)
    lo      = pd_z.get("range_low",      0)
    eq50    = pd_z.get("eq50",           0)

    ob_b    = ob.get("bull_ob",  {}) or {}
    ob_s    = ob.get("bear_ob",  {}) or {}
    sweep_u = liq.get("sweep_up",   {}) or {}
    sweep_d = liq.get("sweep_down", {}) or {}
    swing_h = liq.get("swing_high", 0)
    swing_l = liq.get("swing_low",  0)
    fvg_b   = (fvg or {}).get("bull_fvg", {}) or {}
    fvg_s   = (fvg or {}).get("bear_fvg", {}) or {}

    df_mtf = dfs.get("4H") if dfs.get("4H") is not None else dfs.get("1H")
    current_price = float(df_mtf["close"].iloc[-1]) if df_mtf is not None else 0

    clean_sym = symbol.replace("-USDT-SWAP", "").replace("-USDT", "")

    has_long_ob   = ob_b.get("found", False)
    has_short_ob  = ob_s.get("found", False)
    has_long_fvg  = fvg_b.get("found", False)
    has_short_fvg = fvg_s.get("found", False)

    # ── Вспомогательная функция: построить сценарий ─────────────────────
    def _build_scenario(is_long: bool) -> dict:
        """Возвращает dict с entry_lo/hi, sl, tp1/2/3, zone_src, quality."""
        rng = (hi - lo) if (hi and lo) else (current_price * 0.1 if current_price else 1)

        if is_long:
            # Зона входа: OB → FVG → swing_l → discount зона
            if has_long_ob:
                entry_lo = ob_b["ob_low"]
                entry_hi = ob_b["ob_high"]
                zone_src = "Бычий Order Block"
                quality  = 4
            elif has_long_fvg:
                entry_lo = fvg_b["low"]
                entry_hi = fvg_b["high"]
                zone_src = "Бычий FVG (имбаланс)"
                quality  = 3
            elif swing_l and lo and swing_l > lo:
                entry_hi = swing_l
                entry_lo = swing_l * 0.995
                zone_src = "Зона у Swing Low"
                quality  = 2
            else:
                base     = lo if lo else (current_price * 0.95 if current_price else 0)
                entry_lo = base
                entry_hi = base * 1.005 if base else 0
                zone_src = "Дискаунт-зона диапазона"
                quality  = 1

            if not entry_lo:
                return {}
            entry_mid = (entry_lo + entry_hi) / 2
            sl = entry_lo * (1 - 0.0015)
            risk = entry_mid - sl
            tp1 = eq50 if (eq50 and eq50 > entry_mid) else (entry_mid + risk * 2)
            tp2 = swing_h if (swing_h and swing_h > tp1) else (entry_mid + risk * 3.5)
            tp3 = hi if (hi and hi > tp2) else (entry_mid + risk * 6)
            liq_status = "✅ снята" if sweep_u.get("swept") else "⏳ не снята"
            liq_label  = f"Ликвидность снизу (buy-side): {liq_status}"
        else:
            # Зона входа: OB → FVG → swing_h → premium зона
            if has_short_ob:
                entry_lo = ob_s["ob_low"]
                entry_hi = ob_s["ob_high"]
                zone_src = "Медвежий Order Block"
                quality  = 4
            elif has_short_fvg:
                entry_lo = fvg_s["low"]
                entry_hi = fvg_s["high"]
                zone_src = "Медвежий FVG (имбаланс)"
                quality  = 3
            elif swing_h and hi and swing_h < hi:
                entry_lo = swing_h
                entry_hi = swing_h * 1.005
                zone_src = "Зона у Swing High"
                quality  = 2
            else:
                base     = hi if hi else (current_price * 1.05 if current_price else 0)
                entry_lo = base * 0.995 if base else 0
                entry_hi = base
                zone_src = "Премиум-зона диапазона"
                quality  = 1

            if not entry_hi:
                return {}
            entry_mid = (entry_lo + entry_hi) / 2
            sl = entry_hi * (1 + 0.0015)
            risk = sl - entry_mid
            tp1 = eq50 if (eq50 and eq50 < entry_mid) else (entry_mid - risk * 2)
            tp2 = swing_l if (swing_l and swing_l < tp1) else (entry_mid - risk * 3.5)
            tp3 = lo if (lo and lo < tp2) else (entry_mid - risk * 6)
            liq_status = "✅ снята" if sweep_d.get("swept") else "⏳ не снята"
            liq_label  = f"Ликвидность сверху (sell-side): {liq_status}"

        return dict(
            entry_lo=entry_lo, entry_hi=entry_hi, entry_mid=entry_mid,
            sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, zone_src=zone_src,
            quality=quality, liq_label=liq_label
        )

    # ── Определяем приоритет сценариев ──────────────────────────────────
    long_priority = (
        (trend == "BULLISH") * 3 +
        (zone == "DISCOUNT") * 2 +
        has_long_ob * 2 +
        has_long_fvg * 1 +
        sweep_u.get("swept", False) * 1
    )
    short_priority = (
        (trend == "BEARISH") * 3 +
        (zone == "PREMIUM") * 2 +
        has_short_ob * 2 +
        has_short_fvg * 1 +
        sweep_d.get("swept", False) * 1
    )
    long_is_priority = long_priority >= short_priority

    # ── Строим оба сценария ──────────────────────────────────────────────
    long_sc  = _build_scenario(True)
    short_sc = _build_scenario(False)

    def _render_scenario(is_long: bool, sc: dict, is_priority: bool) -> list:
        if not sc:
            return []
        lines = []
        tag   = "🔥 ПРИОРИТЕТНЫЙ" if is_priority else "⚠️ КОНТР-ТРЕНДОВЫЙ"
        dir_em = "🟢 LONG" if is_long else "🔴 SHORT"
        lines.append(f"{tag} СЦЕНАРИЙ: {dir_em}" + NL)

        # Качество сигнала
        q_stars = "⭐" * sc["quality"] + "☆" * (4 - sc["quality"])
        lines.append(f"Качество зоны: {q_stars}  ({sc['zone_src']})" + NL + NL)

        # Зона входа
        lines.append(f"🎯 ЗОНА ВХОДА:" + NL)
        lines.append(f"   <code>{_fmt_price(sc['entry_lo'])} – {_fmt_price(sc['entry_hi'])}</code>" + NL)

        # SL
        sl_pct = _pct_diff(sc["sl"], sc["entry_mid"])
        lines.append(f"🛑 СТОП-ЛОСС: <code>{_fmt_price(sc['sl'])}</code>  (-{sl_pct})" + NL)
        lines.append(f"   {'Ниже' if is_long else 'Выше'} зоны с буфером 0.15%" + NL + NL)

        # TPs
        lines.append("🎯 ЦЕЛИ:" + NL)
        tp1_pct = _pct_diff(sc["tp1"], sc["entry_mid"])
        tp2_pct = _pct_diff(sc["tp2"], sc["entry_mid"])
        tp3_pct = _pct_diff(sc["tp3"], sc["entry_mid"])
        eq_lbl  = f"← Равновесие 50% <code>{_fmt_price(eq50)}</code>" if eq50 else ""
        sh_lbl  = f"← Swing High <code>{_fmt_price(swing_h)}</code>" if (swing_h and is_long) else ""
        sl_lbl  = f"← Swing Low <code>{_fmt_price(swing_l)}</code>" if (swing_l and not is_long) else ""
        hi_lbl  = f"← Максимум диапазона" if is_long else ""
        lo_lbl  = f"← Минимум диапазона" if not is_long else ""
        sign = "+" if is_long else "-"
        lines.append(f"   TP1: <code>{_fmt_price(sc['tp1'])}</code>  ({sign}{tp1_pct})  {_rr(sc['entry_mid'], sc['sl'], sc['tp1'])}  {eq_lbl}" + NL)
        lines.append(f"   TP2: <code>{_fmt_price(sc['tp2'])}</code>  ({sign}{tp2_pct})  {_rr(sc['entry_mid'], sc['sl'], sc['tp2'])}  {sh_lbl}{sl_lbl}" + NL)
        lines.append(f"   TP3: <code>{_fmt_price(sc['tp3'])}</code>  ({sign}{tp3_pct})  {_rr(sc['entry_mid'], sc['sl'], sc['tp3'])}  {hi_lbl}{lo_lbl}" + NL + NL)

        # Факторы
        lines.append("📋 ФАКТОРЫ:" + NL)
        if is_long:
            lines.append(f"{'✅' if trend == 'BULLISH' else '⚠️'} HTF тренд: {'📈 Бычий' if trend == 'BULLISH' else '📉 Медвежий' if trend == 'BEARISH' else '↔️ Боковик'}" + NL)
            lines.append(f"{'✅' if zone == 'DISCOUNT' else '⚠️'} Позиция: {zone} ({pos:.0f}%) {'— оптимально для LONG' if zone == 'DISCOUNT' else '— против тренда' if zone == 'PREMIUM' else ''}" + NL)
            lines.append(f"{'✅' if has_long_ob else '—'} Бычий OB: {'найден' if has_long_ob else 'не найден'}" + NL)
            lines.append(f"{'✅' if has_long_fvg else '—'} Бычий FVG: {'найден' if has_long_fvg else 'не найден'}" + NL)
            lines.append(f"{'✅' if bos_d else '—'} BOS: {'подтверждён' if bos_d else 'нет'}" + NL)
            lines.append(f"{'✅' if sweep_u.get('swept') else '⏳'} {sc['liq_label']}" + NL)
        else:
            lines.append(f"{'✅' if trend == 'BEARISH' else '⚠️'} HTF тренд: {'📉 Медвежий' if trend == 'BEARISH' else '📈 Бычий' if trend == 'BULLISH' else '↔️ Боковик'}" + NL)
            lines.append(f"{'✅' if zone == 'PREMIUM' else '⚠️'} Позиция: {zone} ({pos:.0f}%) {'— оптимально для SHORT' if zone == 'PREMIUM' else '— против тренда' if zone == 'DISCOUNT' else ''}" + NL)
            lines.append(f"{'✅' if has_short_ob else '—'} Медвежий OB: {'найден' if has_short_ob else 'не найден'}" + NL)
            lines.append(f"{'✅' if has_short_fvg else '—'} Медвежий FVG: {'найден' if has_short_fvg else 'не найден'}" + NL)
            lines.append(f"{'✅' if bos_d else '—'} BOS: {'подтверждён' if bos_d else 'нет'}" + NL)
            lines.append(f"{'✅' if sweep_d.get('swept') else '⏳'} {sc['liq_label']}" + NL)
        if choch:
            lines.append("⚠️ CHoCH: смена структуры — дождись подтверждения" + NL)

        # Условие входа
        lines.append(NL + "⏳ УСЛОВИЕ ВХОДА:" + NL)
        lines.append(f"   Ждать возврата в зону <code>{_fmt_price(sc['entry_lo'])}–{_fmt_price(sc['entry_hi'])}</code>." + NL)
        lines.append(f"   Вход после закрытой <b>{'бычьей' if is_long else 'медвежьей'} свечи</b> в зоне." + NL)
        if eq50:
            if is_long:
                lines.append(f"   ❌ Не входить выше EQ <code>{_fmt_price(eq50)}</code>." + NL)
            else:
                lines.append(f"   ❌ Не входить ниже EQ <code>{_fmt_price(eq50)}</code>." + NL)
        return lines

    # ── Заголовок и контекст ─────────────────────────────────────────────
    p = []
    p.append(f"🔍 <b>АНАЛИЗ: {clean_sym} — Smart Money</b>" + NL)
    p.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━" + NL)

    trend_txt = {"BULLISH": "📈 Бычий (HH/HL)",
                 "BEARISH": "📉 Медвежий (LH/LL)",
                 "RANGING": "↔️ Боковик (нет структуры)"}
    zone_txt  = {"PREMIUM":     "🔴 Премиум &gt;50%",
                 "DISCOUNT":    "🟢 Дискаунт &lt;50%",
                 "EQUILIBRIUM": "⚖️ Равновесие ~50%",
                 "NEUTRAL":     "—"}

    p.append("🌍 <b>РЫНОЧНЫЙ КОНТЕКСТ</b>" + NL)
    p.append(f"Тренд (HTF): <b>{trend_txt.get(trend, trend)}</b>" + NL)
    if current_price:
        p.append(f"Текущая цена: <code>{_fmt_price(current_price)}</code>" + NL)
    p.append(f"Позиция: <b>{zone_txt.get(zone, zone)}</b> — {pos:.0f}% диапазона" + NL)
    if hi and lo:
        p.append(f"Диапазон HTF: <code>{_fmt_price(lo)}</code> — <code>{_fmt_price(hi)}</code>" + NL)
    if eq50:
        p.append(f"Равновесие 50%: <code>{_fmt_price(eq50)}</code>" + NL)
    if swing_h:
        p.append(f"Swing High: <code>{_fmt_price(swing_h)}</code>  {'✅ swept' if sweep_d.get('swept') else '⏳ не снят'}" + NL)
    if swing_l:
        p.append(f"Swing Low:  <code>{_fmt_price(swing_l)}</code>  {'✅ swept' if sweep_u.get('swept') else '⏳ не снят'}" + NL)
    if bos_d:
        p.append("✅ <b>BOS:</b> пробой структуры — тренд подтверждён" + NL)
    if choch:
        p.append("⚠️ <b>CHoCH:</b> смена структуры — возможный разворот" + NL)

    p.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━" + NL)

    # ── Приоритетный сценарий ────────────────────────────────────────────
    if long_is_priority:
        first_sc, first_long = long_sc,  True
        second_sc, second_long = short_sc, False
    else:
        first_sc, first_long = short_sc, False
        second_sc, second_long = long_sc,  True

    p.extend(_render_scenario(first_long,  first_sc,  True))
    p.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━" + NL)
    p.extend(_render_scenario(second_long, second_sc, False))
    p.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━" + NL)

    # ── Итоговый вывод ──────────────────────────────────────────────────
    p.append("🏆 <b>ЛУЧШИЙ СЦЕНАРИЙ:</b>" + NL)
    best_dir = "📈 LONG" if long_is_priority else "📉 SHORT"
    best_sc  = first_sc
    if best_sc:
        p.append(f"{best_dir} от зоны <code>{_fmt_price(best_sc['entry_lo'])}–{_fmt_price(best_sc['entry_hi'])}</code>" + NL)
        p.append(f"SL: <code>{_fmt_price(best_sc['sl'])}</code>  →  TP1: <code>{_fmt_price(best_sc['tp1'])}</code>  TP3: <code>{_fmt_price(best_sc['tp3'])}</code>" + NL)
        strength = long_priority if long_is_priority else short_priority
        conf = "🔥 Высокая" if strength >= 6 else "✅ Средняя" if strength >= 3 else "⚠️ Низкая (торговать осторожно)"
        p.append(f"Уверенность: {conf}" + NL)

    p.append(NL + "⚡ <i>CHM Laboratory — SMC Strategy</i>")
    return "".join(p)


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
            f"{_fmt_price(ob_b['ob_low'])}–{_fmt_price(ob_b['ob_high'])}. "
            "Ждать митигации (возврата в зону) для входа."
        )
    if trend == "BEARISH" and zone == "PREMIUM" and ob_s.get("found"):
        return (
            "\n💡 <b>Рекомендация:</b> Тренд медвежий, цена в премиуме. "
            f"Лучшая точка входа в SHORT — медвежий OB "
            f"{_fmt_price(ob_s['ob_low'])}–{_fmt_price(ob_s['ob_high'])}. "
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
            run_smc_scanner(bot, um, _fetcher_for_smc)
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
        if not user.strategy:
            await msg.answer(_strategy_text(user.strategy), parse_mode="HTML",
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
        await msg.answer(_strategy_text(user.strategy), parse_mode="HTML",
                         reply_markup=_kb_strategy_change(user.strategy))

    # ─── ПОДПИСКА — ВЫБОР ТАРИФА (callback) ─────────────

    PLANS = {
        "plan_bot_90":   ("🤖 CHM BOT — 3 месяца",  "290$"),
        "plan_bot_365":  ("🤖 CHM BOT — 1 ГОД",     "990$"),
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

    # ─── ВЫБОР СТРАТЕГИИ ──────────────────────────────

    @dp.callback_query(F.data.startswith("strategy_"))
    async def cb_strategy(cb: CallbackQuery):
        choice = cb.data.replace("strategy_", "")   # "levels" | "smc"
        strategy_map = {"levels": "LEVELS", "smc": "SMC"}
        strategy = strategy_map.get(choice)
        if not strategy:
            await cb.answer("Неизвестная стратегия", show_alert=True); return

        user = await um.get_or_create(cb.from_user.id)
        user.strategy = strategy
        await um.save(user)
        label = "📊 Уровни (Price Action)" if strategy == "LEVELS" else "🧠 Smart Money (SMC)"
        await cb.answer("✅ Стратегия: " + label, show_alert=False)

        trend = scanner.get_trend() if hasattr(scanner, "get_trend") else {}
        await safe_edit(cb, main_text(user, trend), kb_main(user))

    # ─── АНАЛИЗ МОНЕТЫ ПО ЗАПРОСУ ────────────────────

    async def _do_analyze(msg_or_cb, user: UserSettings, symbol: str):
        """Анализ любой монеты — всегда возвращает детальный разбор зон."""
        is_cb = isinstance(msg_or_cb, CallbackQuery)
        send  = (msg_or_cb.message.answer if is_cb else msg_or_cb.answer)

        if not symbol:
            await send("⚠️ Укажите тикер монеты. Пример: /analyze BTC")
            return

        symbol = _normalize_symbol(symbol)

        # Rate-limit
        uid = user.user_id
        now = time.time()
        if now - _analyze_cooldown.get(uid, 0) < _ANALYZE_COOLDOWN_SEC:
            await send(f"⏳ Подожди {_ANALYZE_COOLDOWN_SEC} секунд между запросами.", parse_mode="HTML")
            return
        _analyze_cooldown[uid] = now

        wait_msg = await send(
            "⏳ <b>Глубокий анализ " + symbol + "...</b>\n\nЗагружаю свечи 1H/4H/1D...",
            parse_mode="HTML",
        )
        try:
            from indicator import CHMIndicator
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
                symbol, _fetcher_for_smc, indicator_obj, user.strategy, bot
            )
        except Exception as e:
            log.error(f"_do_analyze error {symbol}: {e}")
            result_text = f"❌ Ошибка анализа <b>{symbol}</b>: {e}"
        try:
            await wait_msg.delete()
        except Exception:
            pass
        # Telegram limit: 4096 chars. Split if needed.
        MAX = 4000
        if len(result_text) <= MAX:
            try:
                await send(result_text, parse_mode="HTML")
            except Exception as e:
                log.error(f"_do_analyze send error: {e}")
                await send(f"❌ Не удалось отправить анализ: {e}")
        else:
            parts = []
            current = ""
            for line in result_text.split("\n"):
                if len(current) + len(line) + 1 > MAX:
                    parts.append(current)
                    current = line
                else:
                    current = (current + "\n" + line) if current else line
            if current:
                parts.append(current)
            for part in parts:
                try:
                    await send(part, parse_mode="HTML")
                except Exception:
                    await send(part)  # fallback без HTML если тег сломан

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
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, _strategy_text(user.strategy), _kb_strategy_change(user.strategy))

    # ─── SMC РЕЖИМЫ (ЛОНГ / ШОРТ / ОБА) ──────────────

    @dp.callback_query(F.data == "mode_smc_long")
    async def mode_smc_long(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📈 <b>SMC ЛОНГ сканер</b>", kb_smc_mode_long(user))

    @dp.callback_query(F.data == "mode_smc_short")
    async def mode_smc_short(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>SMC ШОРТ сканер</b>", kb_smc_mode_short(user))

    @dp.callback_query(F.data == "mode_smc_both")
    async def mode_smc_both(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⚡ <b>SMC ОБА</b>", kb_smc_mode_both(user))

    @dp.callback_query(F.data == "toggle_smc_long")
    async def toggle_smc_long(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config)); return
        user.smc_long_active = not user.smc_long_active
        await um.save(user)
        await cb.answer("🟢 SMC ЛОНГ включён!" if user.smc_long_active else "🔴 SMC ЛОНГ выключен.")
        await safe_edit(cb, "📈 <b>SMC ЛОНГ сканер</b>", kb_smc_mode_long(user))

    @dp.callback_query(F.data == "toggle_smc_short")
    async def toggle_smc_short(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config)); return
        user.smc_short_active = not user.smc_short_active
        await um.save(user)
        await cb.answer("🟢 SMC ШОРТ включён!" if user.smc_short_active else "🔴 SMC ШОРТ выключен.")
        await safe_edit(cb, "📉 <b>SMC ШОРТ сканер</b>", kb_smc_mode_short(user))

    @dp.callback_query(F.data == "toggle_smc_both")
    async def toggle_smc_both(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config)); return
        is_active = user.active and user.scan_mode == "smc_both"
        if is_active:
            user.active = False
        else:
            user.active    = True
            user.scan_mode = "smc_both"
        await um.save(user)
        await cb.answer("🟢 SMC ОБА включён!" if user.active else "🔴 SMC ОБА выключен.")
        await safe_edit(cb, "⚡ <b>SMC ОБА</b>", kb_smc_mode_both(user))

    # ─── SMC НАСТРОЙКИ ────────────────────────────────

    @dp.callback_query(F.data == "smc_settings")
    async def smc_settings(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    # Подменю
    @dp.callback_query(F.data == "smc_menu_tf")
    async def smc_menu_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📊 <b>Таймфрейм SMC</b>", kb_smc_tf(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_interval")
    async def smc_menu_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔄 <b>Интервал сканирования SMC</b>", kb_smc_interval(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_direction")
    async def smc_menu_direction(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Направление сигналов SMC</b>", kb_smc_direction(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_confirmations")
    async def smc_menu_confirmations(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Мин. подтверждений SMC</b>", kb_smc_confirmations(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_rr")
    async def smc_menu_rr(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Минимальный R:R</b>", kb_smc_rr(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_sl")
    async def smc_menu_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Буфер стоп-лосса</b>", kb_smc_sl(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_volume")
    async def smc_menu_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Мин. объём монеты</b>", kb_smc_volume(user.get_smc_cfg()))

    @dp.callback_query(F.data == "smc_menu_ob_age")
    async def smc_menu_ob_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🕯 <b>Макс. возраст Order Block</b>", kb_smc_ob_age(user.get_smc_cfg()))

    # Сохранение значений
    @dp.callback_query(F.data.startswith("smc_set_tf_"))
    async def smc_set_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.tf_key = cb.data.replace("smc_set_tf_", "")
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Таймфрейм: " + cfg.tf_key)
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_interval_"))
    async def smc_set_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.scan_interval = int(cb.data.replace("smc_set_interval_", ""))
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Интервал: " + str(cfg.scan_interval // 60) + " мин.")
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_dir_"))
    async def smc_set_dir(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.direction = cb.data.replace("smc_set_dir_", "")
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Направление: " + cfg.direction)
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_conf_"))
    async def smc_set_conf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.min_confirmations = int(cb.data.replace("smc_set_conf_", ""))
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Мин. подтверждений: " + str(cfg.min_confirmations))
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_rr_"))
    async def smc_set_rr(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.min_rr = float(cb.data.replace("smc_set_rr_", ""))
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Мин. R:R: 1:" + str(cfg.min_rr))
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_sl_"))
    async def smc_set_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.sl_buffer_pct = float(cb.data.replace("smc_set_sl_", ""))
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Буфер SL: " + str(cfg.sl_buffer_pct) + "%")
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_vol_"))
    async def smc_set_vol(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.min_volume_usdt = float(cb.data.replace("smc_set_vol_", ""))
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Мин. объём обновлён")
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data.startswith("smc_set_ob_age_"))
    async def smc_set_ob_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.ob_max_age = int(cb.data.replace("smc_set_ob_age_", ""))
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("✅ Возраст OB: " + str(cfg.ob_max_age) + " свечей")
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    # Тогглы (вкл/выкл)
    @dp.callback_query(F.data == "smc_toggle_fvg")
    async def smc_toggle_fvg(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.fvg_enabled = not cfg.fvg_enabled
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("FVG: " + ("✅ вкл" if cfg.fvg_enabled else "❌ выкл"))
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data == "smc_toggle_choch")
    async def smc_toggle_choch(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.choch_enabled = not cfg.choch_enabled
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("CHoCH: " + ("✅ вкл" if cfg.choch_enabled else "❌ выкл"))
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data == "smc_toggle_breaker")
    async def smc_toggle_breaker(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.ob_use_breaker = not cfg.ob_use_breaker
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("Breaker Blocks: " + ("✅ вкл" if cfg.ob_use_breaker else "❌ выкл"))
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

    @dp.callback_query(F.data == "smc_toggle_sweep")
    async def smc_toggle_sweep(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = user.get_smc_cfg()
        cfg.sweep_close_req = not cfg.sweep_close_req
        user.set_smc_cfg(cfg)
        await um.save(user)
        await cb.answer("Sweep закрытие: " + ("✅ вкл" if cfg.sweep_close_req else "❌ выкл"))
        await safe_edit(cb, "🧠 <b>Настройки SMC сканера</b>", kb_smc_main(user))

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
        if not user.long_active and not user.strategy:
            await cb.answer()
            await safe_edit(cb, _strategy_text(user.strategy), _kb_strategy_select()); return
        user.long_active = not user.long_active
        if user.long_active and user.active and user.scan_mode == "both":
            # Отключаем BOTH режим чтобы не было дублей сигналов
            user.active = False
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
        if not user.short_active and not user.strategy:
            await cb.answer()
            await safe_edit(cb, _strategy_text(user.strategy), _kb_strategy_select()); return
        user.short_active = not user.short_active
        if user.short_active:
            # Отключаем BOTH режим чтобы не было дублей сигналов
            if user.active and user.scan_mode == "both":
                user.active = False
            user.scan_mode = "short"
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
        user.short_tf = v  # критично: TF хранится в user.short_tf, не в short_cfg
        await um.save(user)
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
        user.short_interval = v  # критично: интервал хранится в user.short_interval
        await um.save(user)
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
        if not is_active and not user.strategy:
            await cb.answer()
            await safe_edit(cb, _strategy_text(user.strategy), _kb_strategy_select()); return
        user.active = not is_active
        user.scan_mode = "both" if user.active else user.scan_mode
        if user.active:
            # Отключаем индивидуальные сканеры — они дублируют BOTH
            user.long_active  = False
            user.short_active = False
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

    # ─── АВТО-ТРЕЙДИНГ BYBIT ──────────────────────────

    def _auto_trade_text(user: UserSettings) -> str:
        at       = getattr(user, "auto_trade",        False)
        mode     = getattr(user, "auto_trade_mode",   "confirm")
        risk     = getattr(user, "trade_risk_pct",    1.0)
        lev      = getattr(user, "trade_leverage",    10)
        max_tr   = getattr(user, "max_trades_limit",  5)
        has_key  = bool(getattr(user, "bybit_api_key", ""))
        NL = "\n"
        mode_label = "👆 С подтверждением (кнопка)" if mode == "confirm" else "🤖 Авто (без кнопки)"
        return (
            "💹 <b>Авто-трейдинг Bybit</b>" + NL + NL +
            "Статус: " + ("🟢 <b>Включён</b>" if at else "🔴 <b>Выключен</b>") + NL +
            "Режим: <b>" + mode_label + "</b>" + NL +
            "Риск: <b>" + str(risk) + "% от баланса</b>" + NL +
            "Плечо: <b>x" + str(lev) + "</b>" + NL +
            "Лимит сделок: <b>" + str(max_tr) + " за 24ч</b>" + NL +
            "API: " + ("✅ <b>Подключено</b>" if has_key else "❌ <b>Не настроено</b>") + NL + NL +
            "⚠️ <i>Используй только собственные API-ключи.\n"
            "Разрешение: только Contract Trade. Без Withdraw!</i>"
        )

    @dp.callback_query(F.data == "auto_trade_menu")
    async def auto_trade_menu(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("❌ Нет доступа", show_alert=True); return
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data == "toggle_auto_trade")
    async def toggle_auto_trade(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, _ = user.check_access()
        if not has:
            await cb.answer("❌ Нет доступа", show_alert=True); return
        if not getattr(user, "bybit_api_key", ""):
            await cb.answer("⚠️ Сначала настрой API ключи!", show_alert=True); return
        user.auto_trade = not user.auto_trade
        await um.save(user)
        await cb.answer("💹 Авто-трейд: " + ("✅ вкл" if user.auto_trade else "❌ выкл"))
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data.in_({"set_at_mode_confirm", "set_at_mode_auto"}))
    async def set_at_mode(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.auto_trade_mode = "confirm" if cb.data == "set_at_mode_confirm" else "auto"
        await um.save(user)
        await cb.answer("Режим: " + user.auto_trade_mode)
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data.startswith("set_at_risk_"))
    async def set_at_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        try:
            user.trade_risk_pct = float(cb.data.split("_")[-1])
        except ValueError:
            await cb.answer("Ошибка"); return
        await um.save(user)
        await cb.answer(f"Риск: {user.trade_risk_pct}%")
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data.startswith("set_at_lev_"))
    async def set_at_lev(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        try:
            user.trade_leverage = int(cb.data.split("_")[-1])
        except ValueError:
            await cb.answer("Ошибка"); return
        await um.save(user)
        await cb.answer(f"Плечо: x{user.trade_leverage}")
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data.startswith("set_at_maxtr_"))
    async def set_at_maxtr(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        try:
            user.max_trades_limit = int(cb.data.split("_")[-1])
        except ValueError:
            await cb.answer("Ошибка"); return
        await um.save(user)
        await cb.answer(f"Лимит сделок: {user.max_trades_limit}")
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data == "setup_bybit_api")
    async def setup_bybit_api(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        has, _ = user.check_access()
        if not has:
            await cb.answer("❌ Нет доступа", show_alert=True); return
        await state.set_state(SetupBybitState.api_key)
        await cb.message.answer(
            "🔑 <b>Настройка Bybit API</b>\n\n"
            "Введи свой <b>API Key</b> (публичный ключ):\n\n"
            "💡 Создать ключ: Bybit → Аккаунт → API Management\n"
            "⚠️ Права: только <b>Contract - Trade</b> (без Withdraw!)",
            parse_mode="HTML",
        )

    @dp.message(SetupBybitState.api_key)
    async def bybit_api_key_input(msg: Message, state: FSMContext):
        key = (msg.text or "").strip()
        if len(key) < 10:
            await msg.answer("❌ Слишком короткий ключ. Попробуй снова:")
            return
        await state.update_data(api_key=key)
        await state.set_state(SetupBybitState.api_secret)
        await msg.answer(
            "✅ API Key получен.\n\n"
            "Теперь введи <b>API Secret</b> (секретный ключ):",
            parse_mode="HTML",
        )

    @dp.message(SetupBybitState.api_secret)
    async def bybit_api_secret_input(msg: Message, state: FSMContext):
        secret = (msg.text or "").strip()
        if len(secret) < 10:
            await msg.answer("❌ Слишком короткий секрет. Попробуй снова:")
            return
        data = await state.get_data()
        api_key = data.get("api_key", "")
        await state.clear()

        wait = await msg.answer("⏳ Проверяю соединение с Bybit...")
        try:
            import bybit_trader
            result = await bybit_trader.test_connection(api_key, secret)
        except Exception as e:
            await wait.delete()
            await msg.answer(f"❌ Ошибка: {e}")
            return

        await wait.delete()
        if result["ok"]:
            user = await um.get_or_create(msg.from_user.id)
            user.bybit_api_key    = api_key
            user.bybit_api_secret = secret
            await um.save(user)
            balance = result["balance"]
            await msg.answer(
                f"✅ <b>Bybit подключён!</b>\n\n"
                f"💰 Баланс USDT: <code>${balance:.2f}</code>\n\n"
                f"Теперь включи авто-трейд в меню 💹",
                parse_mode="HTML",
            )
        else:
            await msg.answer(
                f"❌ <b>Не удалось подключиться</b>\n\n"
                f"Ошибка: {result.get('error', '?')}\n\n"
                f"Проверь ключи и попробуй снова через кнопку <b>🔑 Настроить Bybit API</b>.",
                parse_mode="HTML",
            )

    @dp.callback_query(F.data == "test_bybit_api")
    async def test_bybit_api(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        api_key    = getattr(user, "bybit_api_key",    "")
        api_secret = getattr(user, "bybit_api_secret", "")
        if not api_key:
            await cb.answer("❌ Ключи не настроены", show_alert=True); return
        wait = await cb.message.answer("⏳ Проверяю соединение...")
        try:
            import bybit_trader
            result = await bybit_trader.test_connection(api_key, api_secret)
        except Exception as e:
            await wait.delete()
            await cb.message.answer(f"❌ Ошибка: {e}")
            return
        await wait.delete()
        if result["ok"]:
            await cb.message.answer(
                f"✅ <b>Bybit — соединение OK</b>\n"
                f"💰 Баланс: <code>${result['balance']:.2f} USDT</code>",
                parse_mode="HTML",
            )
        else:
            await cb.message.answer(
                f"❌ <b>Ошибка соединения</b>\n{result.get('error', '?')}",
                parse_mode="HTML",
            )

    @dp.callback_query(F.data == "remove_bybit_api")
    async def remove_bybit_api(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        user.bybit_api_key    = ""
        user.bybit_api_secret = ""
        user.auto_trade       = False
        await um.save(user)
        await safe_edit(cb, _auto_trade_text(user), kb_auto_trade(user))

    @dp.callback_query(F.data.startswith("exec_trade_"))
    async def exec_trade(cb: CallbackQuery):
        """Пользователь нажал '✅ Открыть сделку на Bybit'."""
        await cb.answer()
        trade_id = cb.data[len("exec_trade_"):]
        user = await um.get_or_create(cb.from_user.id)
        has, _ = user.check_access()
        if not has:
            await cb.answer("❌ Нет доступа", show_alert=True); return

        api_key    = getattr(user, "bybit_api_key",    "")
        api_secret = getattr(user, "bybit_api_secret", "")
        if not api_key or not api_secret:
            await cb.message.answer(
                "❌ <b>Bybit API не настроен</b>\n\n"
                "Перейди в 💹 Авто-трейдинг → 🔑 Настроить Bybit API",
                parse_mode="HTML",
            )
            return

        trade = await db.db_get_trade(trade_id)
        if not trade:
            await cb.answer("❌ Сделка не найдена", show_alert=True); return

        max_trades = getattr(user, "max_trades_limit", 5)
        open_count = await db.db_count_open_trades(user.user_id)
        if open_count >= max_trades:
            await cb.answer(
                f"⛔ Лимит сделок достигнут ({open_count}/{max_trades}). "
                f"Дождись закрытия открытых позиций.",
                show_alert=True,
            )
            return

        wait = await cb.message.answer("⏳ Открываю позицию на Bybit...")
        try:
            import bybit_trader
            result = await bybit_trader.place_trade(
                api_key, api_secret,
                trade["symbol"],
                trade["direction"],
                float(trade["entry"]),
                float(trade["sl"]),
                float(trade["tp1"]),
                getattr(user, "trade_risk_pct",  1.0),
                getattr(user, "trade_leverage",  10),
            )
            text = bybit_trader.format_trade_result(
                result,
                trade["direction"],
                trade["symbol"],
                float(trade["entry"]),
                float(trade["sl"]),
                float(trade["tp1"]),
                getattr(user, "trade_risk_pct", 1.0),
                getattr(user, "trade_leverage", 10),
            )
        except Exception as e:
            log.error(f"exec_trade {trade_id}: {e}")
            text = f"❌ Ошибка открытия сделки:\n{e}"

        await wait.delete()
        await cb.message.answer(text, parse_mode="HTML")

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
        await _save_subs_backup()   # автосохранение на диск
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
            "Стратегия: <b>" + (user.strategy or "не выбрана") + "</b>",
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

    # ─── ЭКСПОРТ / ИМПОРТ ПОДПИСОК ────────────────────
    # После редеплоя БД может быть пуста. /export_subs сохраняет
    # список активных подписок в файл и в Telegram.
    # /import_subs восстанавливает их из файла.

    def _subs_backup_path() -> str:
        import os
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "subs_backup.txt"
        )

    async def _save_subs_backup():
        """Сохраняет активных пользователей в файл рядом со скриптом."""
        import os, datetime
        users = await um.all_users()
        lines = [f"# Backup: {datetime.datetime.utcnow().isoformat()}"]
        for u in users:
            if u.sub_status in ("active", "trial") and u.sub_expires > time.time():
                lines.append(
                    f"{u.user_id}\t{u.username or ''}\t{u.sub_status}\t{u.sub_expires:.0f}"
                )
        path = _subs_backup_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            log.warning(f"subs backup write error: {e}")

    @dp.message(Command("export_subs"))
    async def cmd_export_subs(msg: Message):
        if not is_admin(msg.from_user.id): return
        await _save_subs_backup()
        users = await um.all_users()
        active = [u for u in users if u.sub_status in ("active", "trial") and u.sub_expires > time.time()]
        if not active:
            await msg.answer("📋 Нет активных подписок."); return
        lines = ["📋 <b>Активные подписки:</b>", ""]
        for u in active:
            left = u.time_left_str()
            lines.append(f"<code>/give {u.user_id} ___</code>  @{u.username or u.user_id}  осталось: {left}")
        lines.append("")
        lines.append(f"<i>Файл: subs_backup.txt — используй /import_subs после редеплоя</i>")
        await msg.answer("\n".join(lines), parse_mode="HTML")

    @dp.message(Command("import_subs"))
    async def cmd_import_subs(msg: Message):
        if not is_admin(msg.from_user.id): return
        path = _subs_backup_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            await msg.answer("❌ Файл subs_backup.txt не найден. Сначала сделай /export_subs."); return
        restored = 0
        errors = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            try:
                uid = int(parts[0])
                username = parts[1]
                sub_status = parts[2]
                sub_expires = float(parts[3])
                if sub_expires <= time.time():
                    continue  # уже истекла
                user = await um.get_or_create(uid, username)
                user.sub_status  = sub_status
                user.sub_expires = sub_expires
                user.username    = username or user.username
                await um.save(user)
                restored += 1
            except Exception as e:
                log.warning(f"import_subs line error: {e}")
                errors += 1
        await msg.answer(f"✅ Восстановлено: {restored}  ❌ Ошибок: {errors}")

    # ─── Закрываем register_handlers ──────────────────
    # (конец функции)
