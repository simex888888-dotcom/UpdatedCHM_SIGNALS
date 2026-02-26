"""
handlers.py — все хэндлеры бота v4.6
Структура:
  • /start, /help, /admin
  • Главное меню и навигация
  • Все настройки (SMC / Пивоты / EMA / Фильтры / Качество / Cooldown / SL / TP / Объём / Уведомления)
  • Кнопки под сигналом: Подтверждения / График / Скрыть / Статистика
  • Запись результата сделки (FSM через inline-кнопки)
  • История сделок и общая статистика
"""

import logging
import time
from typing import Optional

from aiogram import Dispatcher, Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from user_manager import UserManager, UserSettings, TradeCfg
from scanner_multi import MultiScanner, make_signal_text, make_checklist_text
import keyboards as kb

log = logging.getLogger("CHM.Handlers")


# ══════════════════════════════════════════════════════════════════════════
#  FSM — ввод кастомных TP значений
# ══════════════════════════════════════════════════════════════════════════

class TPInput(StatesGroup):
    waiting_tp1 = State()
    waiting_tp2 = State()
    waiting_tp3 = State()
    # prefix: "", "long_", "short_"
    _prefix = State()


# ══════════════════════════════════════════════════════════════════════════
#  Хелперы
# ══════════════════════════════════════════════════════════════════════════

def _b(text: str, cb: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data=cb)]


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


async def _answer(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    """Редактирует сообщение или отвечает новым если не удалось."""
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await call.message.answer(text, parse_mode="HTML", reply_markup=markup)
    await call.answer()


async def _get_user(call: CallbackQuery, um: UserManager) -> Optional[UserSettings]:
    user = await um.get_or_create(call.from_user.id, call.from_user.username or "")
    ok, status = user.check_access()
    if not ok:
        await call.answer("❌ Подписка истекла. Напиши /start", show_alert=True)
        return None
    return user


def _main_text(user: UserSettings, scanner: MultiScanner) -> str:
    trend = scanner.get_trend()
    trend_line = kb.trend_text(trend)
    left = user.time_left_str()
    status_icon = "✅" if user.sub_status == "active" else "⏳"
    return (
        trend_line +
        f"\n{status_icon} Подписка: <b>{user.sub_status}</b> · осталось <b>{left}</b>\n"
        f"📨 Сигналов получено: <b>{user.signals_received}</b>\n\n"
        f"Выбери режим сканирования:"
    )


# ══════════════════════════════════════════════════════════════════════════
#  Статистика — вспомогательные
# ══════════════════════════════════════════════════════════════════════════

def _stats_text(stats: dict) -> str:
    if not stats:
        return "📊 <b>Статистика пуста</b>\n\nЗапиши результаты первых сделок нажав <b>📈 Статистика</b> под сигналом."

    total = stats["total"]
    wins  = stats["wins"]
    losses = stats["losses"]
    be    = total - wins - losses
    wr    = stats["winrate"]
    avg   = stats["avg_rr"]
    tot   = stats["total_rr"]

    last5_line = ""
    # streak
    sw = stats.get("streak_w", 0)
    sl = stats.get("streak_l", 0)

    tp1 = stats.get("tp1_cnt", 0)
    tp2 = stats.get("tp2_cnt", 0)
    tp3 = stats.get("tp3_cnt", 0)

    lt = stats.get("longs_total", 0)
    lw = stats.get("longs_wins", 0)
    st = stats.get("shorts_total", 0)
    sw2 = stats.get("shorts_wins", 0)

    pf_num = sum([1.0] * wins)
    pf_den = sum([1.0] * losses) if losses else 1
    pf = pf_num / pf_den if pf_den else 0

    lines = [
        "📊 <b>Моя статистика</b>",
        "",
        f"📈 Всего сделок:    <b>{total}</b>",
        f"✅ Побед:           <b>{wins}</b>  ({wr:.1f}%)",
        f"❌ Убытков:         <b>{losses}</b>",
        f"➖ Безубытков:      <b>{be}</b>",
        "",
        f"💰 Средний R:R:     <b>{avg:+.2f}R</b>",
        f"💰 Итого R:R:       <b>{tot:+.2f}R</b>",
        f"📐 Профит-фактор:   <b>{pf:.2f}</b>",
        "",
        f"🎯 По целям:  TP1 {tp1}  TP2 {tp2}  TP3 {tp3}",
        "",
        f"📈 ЛОНГ:  {lt} сделок  {lw} побед",
        f"📉 ШОРТ:  {st} сделок  {sw2} побед",
        "",
        f"🔥 Серии:  макс. побед {sw}  ·  макс. убытков {sl}",
    ]

    best = stats.get("best_symbols", [])
    if best:
        lines += ["", "🏆 Лучшие монеты:"]
        for sym, d in best[:5]:
            wr2 = d["wins"] / d["total"] * 100 if d["total"] else 0
            lines.append(f"   {sym}  {wr2:.0f}%  ({d['wins']}/{d['total']})")

    return "\n".join(lines)


def _stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _b("📋 История сделок",  "trade_history_0"),
        _b("🗑 Сбросить статистику", "trade_reset_confirm"),
        _b("◀️ Назад",           "back_main"),
    ])


def _record_kb(trade_id: str) -> InlineKeyboardMarkup:
    """Клавиатура записи результата сделки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        _b("📋 Записать результат сделки", f"trade_pick_result_{trade_id}"),
        _b("✕ Закрыть", f"trade_close_stat_{trade_id}"),
    ])


def _pick_result_kb(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _b("✅ Победа",     f"trade_result_win_{trade_id}"),
        _b("❌ Убыток",     f"trade_result_loss_{trade_id}"),
        _b("➖ Безубыток",  f"trade_result_be_{trade_id}"),
        _b("◀️ Назад",     f"sig_stats_{trade_id}"),
    ])


def _pick_tp_kb(trade_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        _b("🎯 Цель 1  (TP1)",   f"trade_tp_1_{trade_id}"),
        _b("🎯 Цель 2  (TP2)",   f"trade_tp_2_{trade_id}"),
        _b("🏆 Цель 3  (TP3)",   f"trade_tp_3_{trade_id}"),
        _b("◀️ Назад",           f"trade_pick_result_{trade_id}"),
    ])


async def _history_text_and_kb(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    trades = await db.db_get_user_trades(user_id)
    if not trades:
        return "📋 <b>История пуста</b>\n\nЗапиши первые сделки.", InlineKeyboardMarkup(
            inline_keyboard=[_b("◀️ Назад", "my_stats")]
        )

    trades = list(reversed(trades))  # новые сначала
    per_page = 5
    total_pages = max(1, (len(trades) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = trades[page * per_page: (page + 1) * per_page]

    icons = {"TP1": "🎯", "TP2": "🎯", "TP3": "🏆", "SL": "❌", "BE": "➖"}
    result_names = {"TP1": "TP1 ✅", "TP2": "TP2 ✅", "TP3": "TP3 ✅", "SL": "SL ❌", "BE": "Безубыток"}

    lines = [f"📋 <b>История сделок</b>  (стр. {page+1}/{total_pages})", ""]
    for t in chunk:
        icon = icons.get(t["result"], "•")
        res  = result_names.get(t["result"], t["result"])
        dt   = time.strftime("%d.%m %H:%M", time.localtime(t["created_at"]))
        rr   = f"  {t['result_rr']:+.2f}R" if t.get("result_rr") else ""
        lines.append(f"{icon} <b>{t['symbol']}</b> {t['direction']}  {res}{rr}  <i>{dt}</i>")

    rows = [_b("📊 К статистике", "my_stats")]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"trade_history_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"trade_history_{page+1}"))
    if nav:
        rows.insert(0, nav)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ══════════════════════════════════════════════════════════════════════════
#  РЕГИСТРАЦИЯ ВСЕХ ХЭНДЛЕРОВ
# ══════════════════════════════════════════════════════════════════════════

def register_handlers(dp: Dispatcher, bot: Bot, um: UserManager, scanner: MultiScanner, config):

    # ─────────────────────────────────────────────────────────────────────
    #  /start
    # ─────────────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        ok, status = user.check_access()
        if not ok:
            await msg.answer(
                "⚡ <b>CHM BREAKER BOT</b>\n\n"
                "🔒 Пробный период истёк.\n\n"
                "Для продолжения оформи подписку:",
                parse_mode="HTML",
                reply_markup=kb.kb_subscribe(config),
            )
            return
        text = _main_text(user, scanner)
        await msg.answer(text, parse_mode="HTML", reply_markup=kb.kb_main(user))

    # ─────────────────────────────────────────────────────────────────────
    #  /admin
    # ─────────────────────────────────────────────────────────────────────

    @dp.message(Command("admin"))
    async def cmd_admin(msg: Message):
        if msg.from_user.id not in config.ADMIN_IDS:
            return
        s = await um.stats_summary()
        perf = scanner.get_perf()
        text = (
            "🔑 <b>Админ-панель</b>\n\n"
            f"👥 Всего юзеров:  <b>{s.get('total', 0)}</b>\n"
            f"   Пробный:       <b>{s.get('trial', 0)}</b>\n"
            f"   Активных:      <b>{s.get('active', 0)}</b>\n"
            f"   Истекших:      <b>{s.get('expired', 0)}</b>\n"
            f"   Заблокированных: <b>{s.get('banned', 0)}</b>\n\n"
            f"🔍 Сканирует: <b>{s.get('scanning', 0)}</b> пользователей\n"
            f"📨 Сигналов отправлено: <b>{perf.get('signals', 0)}</b>\n"
            f"🔄 Циклов сканирования: <b>{perf.get('cycles', 0)}</b>\n"
        )
        await msg.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            _b("👤 Выдать подписку — /grant USER_ID DAYS", "noop"),
        ]))

    @dp.message(Command("grant"))
    async def cmd_grant(msg: Message):
        if msg.from_user.id not in config.ADMIN_IDS:
            return
        parts = msg.text.strip().split()
        if len(parts) != 3:
            await msg.answer("Использование: /grant USER_ID DAYS"); return
        try:
            uid = int(parts[1]); days = int(parts[2])
        except ValueError:
            await msg.answer("Неверный формат."); return
        target = await um.get(uid)
        if not target:
            await msg.answer(f"Пользователь {uid} не найден."); return
        target.grant_access(days)
        await um.save(target)
        await msg.answer(f"✅ Пользователю {uid} выдано {days} дней.\nСтатус: {target.sub_status}\nИстекает: {target.time_left_str()}")
        try:
            await bot.send_message(uid, f"✅ Ваша подписка активирована на {days} дней!\nИстекает: {target.time_left_str()}")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    #  noop (серые строки)
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "noop")
    async def cb_noop(call: CallbackQuery):
        await call.answer()

    # ─────────────────────────────────────────────────────────────────────
    #  Назад → главное меню
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "back_main")
    async def cb_back_main(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, _main_text(user, scanner), kb.kb_main(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Режимы
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "mode_long")
    async def cb_mode_long(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "📈 <b>ЛОНГ сканер</b>", kb.kb_mode_long(user))

    @dp.callback_query(F.data == "mode_short")
    async def cb_mode_short(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "📉 <b>ШОРТ сканер</b>", kb.kb_mode_short(user))

    @dp.callback_query(F.data == "mode_both")
    async def cb_mode_both(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚡ <b>Сканер ОБА</b>", kb.kb_mode_both(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Переключатели вкл/выкл
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "toggle_long")
    async def cb_toggle_long(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.long_active = not user.long_active
        if user.long_active:
            user.short_active = False
            user.active = False
        await um.save(user)
        await _answer(call, "📈 <b>ЛОНГ сканер</b>", kb.kb_mode_long(user))

    @dp.callback_query(F.data == "toggle_short")
    async def cb_toggle_short(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.short_active = not user.short_active
        if user.short_active:
            user.long_active = False
            user.active = False
        await um.save(user)
        await _answer(call, "📉 <b>ШОРТ сканер</b>", kb.kb_mode_short(user))

    @dp.callback_query(F.data == "toggle_both")
    async def cb_toggle_both(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        was_active = user.active and user.scan_mode == "both"
        user.active     = not was_active
        user.scan_mode  = "both"
        user.long_active  = False
        user.short_active = False
        await um.save(user)
        await _answer(call, "⚡ <b>Сканер ОБА</b>", kb.kb_mode_both(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Сброс настроек
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "reset_long_cfg")
    async def cb_reset_long(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.long_cfg = "{}"
        await um.save(user)
        await call.answer("✅ Настройки ЛОНГ сброшены к общим", show_alert=True)
        await _answer(call, "📈 <b>ЛОНГ сканер</b>", kb.kb_mode_long(user))

    @dp.callback_query(F.data == "reset_short_cfg")
    async def cb_reset_short(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.short_cfg = "{}"
        await um.save(user)
        await call.answer("✅ Настройки ШОРТ сброшены к общим", show_alert=True)
        await _answer(call, "📉 <b>ШОРТ сканер</b>", kb.kb_mode_short(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Таймфреймы
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "menu_tf")
    async def cb_menu_tf(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "📊 <b>Таймфрейм</b>", kb.kb_timeframes(user.timeframe))

    @dp.callback_query(F.data == "menu_long_tf")
    async def cb_menu_long_tf(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "📊 <b>Таймфрейм ЛОНГ</b>", kb.kb_long_timeframes(user.long_tf))

    @dp.callback_query(F.data == "menu_short_tf")
    async def cb_menu_short_tf(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "📊 <b>Таймфрейм ШОРТ</b>", kb.kb_short_timeframes(user.short_tf))

    @dp.callback_query(F.data.startswith("set_tf_"))
    async def cb_set_tf(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        tf = call.data.replace("set_tf_", "")
        user.timeframe = tf
        await um.save(user)
        await _answer(call, "⚡ <b>Сканер ОБА</b>", kb.kb_mode_both(user))

    @dp.callback_query(F.data.startswith("set_long_tf_"))
    async def cb_set_long_tf(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.long_tf = call.data.replace("set_long_tf_", "")
        await um.save(user)
        await _answer(call, "📈 <b>ЛОНГ сканер</b>", kb.kb_mode_long(user))

    @dp.callback_query(F.data.startswith("set_short_tf_"))
    async def cb_set_short_tf(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.short_tf = call.data.replace("set_short_tf_", "")
        await um.save(user)
        await _answer(call, "📉 <b>ШОРТ сканер</b>", kb.kb_mode_short(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Интервалы
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "menu_interval")
    async def cb_menu_interval(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "🔄 <b>Интервал сканирования</b>", kb.kb_intervals(user.scan_interval))

    @dp.callback_query(F.data == "menu_long_interval")
    async def cb_menu_long_interval(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "🔄 <b>Интервал ЛОНГ</b>", kb.kb_long_intervals(user.long_interval))

    @dp.callback_query(F.data == "menu_short_interval")
    async def cb_menu_short_interval(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "🔄 <b>Интервал ШОРТ</b>", kb.kb_short_intervals(user.short_interval))

    @dp.callback_query(F.data.startswith("set_interval_"))
    async def cb_set_interval(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.scan_interval = int(call.data.replace("set_interval_", ""))
        await um.save(user)
        await _answer(call, "⚡ <b>Сканер ОБА</b>", kb.kb_mode_both(user))

    @dp.callback_query(F.data.startswith("set_long_interval_"))
    async def cb_set_long_interval(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.long_interval = int(call.data.replace("set_long_interval_", ""))
        await um.save(user)
        await _answer(call, "📈 <b>ЛОНГ сканер</b>", kb.kb_mode_long(user))

    @dp.callback_query(F.data.startswith("set_short_interval_"))
    async def cb_set_short_interval(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.short_interval = int(call.data.replace("set_short_interval_", ""))
        await um.save(user)
        await _answer(call, "📉 <b>ШОРТ сканер</b>", kb.kb_mode_short(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Меню настроек (оглавление)
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "menu_settings")
    async def cb_menu_settings(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚙️ <b>Настройки сигнала</b>", kb.kb_settings())

    @dp.callback_query(F.data == "menu_long_settings")
    async def cb_menu_long_settings(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚙️ <b>Настройки ЛОНГ</b>", kb.kb_long_settings())

    @dp.callback_query(F.data == "menu_short_settings")
    async def cb_menu_short_settings(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚙️ <b>Настройки ШОРТ</b>", kb.kb_short_settings())

    # ─────────────────────────────────────────────────────────────────────
    #  SMC
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "menu_smc")
    async def cb_menu_smc(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚡ <b>SMC условия входа</b>", kb.kb_smc(user))

    @dp.callback_query(F.data == "menu_long_smc")
    async def cb_menu_long_smc(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚡ <b>SMC условия — ЛОНГ</b>", kb.kb_long_smc(user))

    @dp.callback_query(F.data == "menu_short_smc")
    async def cb_menu_short_smc(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "⚡ <b>SMC условия — ШОРТ</b>", kb.kb_short_smc(user))

    def _smc_toggle(field: str, user: UserSettings) -> bool:
        cur = getattr(user, field)
        setattr(user, field, not cur)
        return not cur

    @dp.callback_query(F.data.startswith("smc_toggle_"))
    async def cb_smc_toggle(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        raw = call.data  # e.g. "smc_toggle_bos" / "long_smc_toggle_ob"

        # определяем prefix
        if raw.startswith("long_smc_toggle_"):
            prefix = "long_"
            key    = raw.replace("long_smc_toggle_", "smc_use_")
            back   = "mode_long"
            mkb    = kb.kb_long_smc
        elif raw.startswith("short_smc_toggle_"):
            prefix = "short_"
            key    = raw.replace("short_smc_toggle_", "smc_use_")
            back   = "mode_short"
            mkb    = kb.kb_short_smc
        else:
            prefix = ""
            key    = raw.replace("smc_toggle_", "smc_use_")
            back   = "menu_settings"
            mkb    = kb.kb_smc

        # Переключаем на объекте пользователя (shared для "" и long/short через cfg)
        if prefix == "":
            _smc_toggle(key, user)
            await um.save(user)
            await _answer(call, "⚡ <b>SMC условия входа</b>", mkb(user))
        else:
            cfg = user.get_long_cfg() if prefix == "long_" else user.get_short_cfg()
            cur = getattr(cfg, key)
            setattr(cfg, key, not cur)
            if prefix == "long_":
                user.set_long_cfg(cfg)
            else:
                user.set_short_cfg(cfg)
            await um.save(user)
            dir_name = "ЛОНГ" if prefix == "long_" else "ШОРТ"
            await _answer(call, f"⚡ <b>SMC условия — {dir_name}</b>", mkb(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Пивоты
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_pivots", "menu_long_pivots", "menu_short_pivots"}))
    async def cb_menu_pivots(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_pivots":
            await _answer(call, "📐 <b>Пивоты — ЛОНГ</b>", kb.kb_long_pivots(user))
        elif call.data == "menu_short_pivots":
            await _answer(call, "📐 <b>Пивоты — ШОРТ</b>", kb.kb_short_pivots(user))
        else:
            await _answer(call, "📐 <b>Пивоты и уровни S/R</b>", kb.kb_pivots(user))

    def _apply_pivot(data: str, user: UserSettings):
        """Применяет одно пивот-значение к нужному cfg."""
        if data.startswith("long_set_"):
            cfg = user.get_long_cfg()
            key = data[len("long_"):]
            is_long = True
        elif data.startswith("short_set_"):
            cfg = user.get_short_cfg()
            key = data[len("short_"):]
            is_short = True
            is_long = False
        else:
            cfg = None
            key = data

        if key.startswith("set_pivot_"):
            v = int(key.split("_")[-1])
            if cfg:
                cfg.pivot_strength = v
            else:
                user.pivot_strength = v
        elif key.startswith("set_age_"):
            v = int(key.split("_")[-1])
            if cfg:
                cfg.max_level_age = v
            else:
                user.max_level_age = v
        elif key.startswith("set_retest_"):
            v = int(key.split("_")[-1])
            if cfg:
                cfg.max_retest_bars = v
            else:
                user.max_retest_bars = v
        elif key.startswith("set_buffer_"):
            v = float(key.split("_")[-1])
            if cfg:
                cfg.zone_buffer = v
            else:
                user.zone_buffer = v

        if data.startswith("long_") and cfg:
            user.set_long_cfg(cfg)
        elif data.startswith("short_") and cfg:
            user.set_short_cfg(cfg)

    @dp.callback_query(F.data.startswith("set_pivot_") | F.data.startswith("set_age_") |
                       F.data.startswith("set_retest_") | F.data.startswith("set_buffer_") |
                       F.data.startswith("long_set_pivot_") | F.data.startswith("long_set_age_") |
                       F.data.startswith("long_set_retest_") | F.data.startswith("long_set_buffer_") |
                       F.data.startswith("short_set_pivot_") | F.data.startswith("short_set_age_") |
                       F.data.startswith("short_set_retest_") | F.data.startswith("short_set_buffer_"))
    async def cb_set_pivot_val(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        _apply_pivot(call.data, user)
        await um.save(user)
        if call.data.startswith("long_"):
            await _answer(call, "📐 <b>Пивоты — ЛОНГ</b>", kb.kb_long_pivots(user))
        elif call.data.startswith("short_"):
            await _answer(call, "📐 <b>Пивоты — ШОРТ</b>", kb.kb_short_pivots(user))
        else:
            await _answer(call, "📐 <b>Пивоты и уровни S/R</b>", kb.kb_pivots(user))

    # ─────────────────────────────────────────────────────────────────────
    #  EMA
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_ema", "menu_long_ema", "menu_short_ema"}))
    async def cb_menu_ema(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_ema":
            await _answer(call, "📉 <b>EMA тренд — ЛОНГ</b>", kb.kb_long_ema(user))
        elif call.data == "menu_short_ema":
            await _answer(call, "📉 <b>EMA тренд — ШОРТ</b>", kb.kb_short_ema(user))
        else:
            await _answer(call, "📉 <b>EMA тренд</b>", kb.kb_ema(user))

    def _apply_ema(data: str, user: UserSettings):
        if data.startswith("long_"):
            cfg = user.get_long_cfg(); key = data[len("long_"):]; prefix = "long_"
        elif data.startswith("short_"):
            cfg = user.get_short_cfg(); key = data[len("short_"):]; prefix = "short_"
        else:
            cfg = None; key = data; prefix = ""

        if key.startswith("set_ema_fast_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.ema_fast = v
            else:   user.ema_fast = v
        elif key.startswith("set_ema_slow_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.ema_slow = v
            else:   user.ema_slow = v
        elif key.startswith("set_htf_ema_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.htf_ema_period = v
            else:   user.htf_ema_period = v

        if prefix == "long_" and cfg:   user.set_long_cfg(cfg)
        elif prefix == "short_" and cfg: user.set_short_cfg(cfg)

    @dp.callback_query(F.data.startswith("set_ema_fast_") | F.data.startswith("set_ema_slow_") |
                       F.data.startswith("set_htf_ema_") |
                       F.data.startswith("long_set_ema_") | F.data.startswith("short_set_ema_") |
                       F.data.startswith("long_set_htf_ema_") | F.data.startswith("short_set_htf_ema_"))
    async def cb_set_ema(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        _apply_ema(call.data, user)
        await um.save(user)
        if call.data.startswith("long_"):
            await _answer(call, "📉 <b>EMA тренд — ЛОНГ</b>", kb.kb_long_ema(user))
        elif call.data.startswith("short_"):
            await _answer(call, "📉 <b>EMA тренд — ШОРТ</b>", kb.kb_short_ema(user))
        else:
            await _answer(call, "📉 <b>EMA тренд</b>", kb.kb_ema(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Фильтры
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_filters", "menu_long_filters", "menu_short_filters"}))
    async def cb_menu_filters(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_filters":
            await _answer(call, "🔬 <b>Фильтры — ЛОНГ</b>", kb.kb_long_filters(user))
        elif call.data == "menu_short_filters":
            await _answer(call, "🔬 <b>Фильтры — ШОРТ</b>", kb.kb_short_filters(user))
        else:
            await _answer(call, "🔬 <b>Фильтры сигнала</b>", kb.kb_filters(user))

    def _apply_filter(data: str, user: UserSettings):
        if data.startswith("long_"):
            cfg = user.get_long_cfg(); key = data[len("long_"):]; prefix = "long_"
        elif data.startswith("short_"):
            cfg = user.get_short_cfg(); key = data[len("short_"):]; prefix = "short_"
        else:
            cfg = None; key = data; prefix = ""

        toggles = {
            "toggle_rsi":     "use_rsi",
            "toggle_volume":  "use_volume",
            "toggle_pattern": "use_pattern",
            "toggle_htf":     "use_htf",
            "toggle_session": "use_session",
        }
        if key in toggles:
            field = toggles[key]
            if cfg:
                setattr(cfg, field, not getattr(cfg, field))
            else:
                setattr(user, field, not getattr(user, field))
        elif key.startswith("set_rsi_period_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.rsi_period = v
            else:   user.rsi_period = v
        elif key.startswith("set_rsi_ob_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.rsi_ob = v
            else:   user.rsi_ob = v
        elif key.startswith("set_rsi_os_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.rsi_os = v
            else:   user.rsi_os = v
        elif key.startswith("set_vol_mult_"):
            v = float(key.split("_")[-1])
            if cfg: cfg.vol_mult = v
            else:   user.vol_mult = v

        if prefix == "long_" and cfg:    user.set_long_cfg(cfg)
        elif prefix == "short_" and cfg:  user.set_short_cfg(cfg)

    @dp.callback_query(F.data.startswith("toggle_rsi") | F.data.startswith("toggle_volume") |
                       F.data.startswith("toggle_pattern") | F.data.startswith("toggle_htf") |
                       F.data.startswith("toggle_session") |
                       F.data.startswith("set_rsi_") | F.data.startswith("set_vol_mult_") |
                       F.data.startswith("long_toggle_") | F.data.startswith("short_toggle_") |
                       F.data.startswith("long_set_rsi_") | F.data.startswith("short_set_rsi_") |
                       F.data.startswith("long_set_vol_mult_") | F.data.startswith("short_set_vol_mult_"))
    async def cb_filter_val(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        _apply_filter(call.data, user)
        await um.save(user)
        if call.data.startswith("long_"):
            await _answer(call, "🔬 <b>Фильтры — ЛОНГ</b>", kb.kb_long_filters(user))
        elif call.data.startswith("short_"):
            await _answer(call, "🔬 <b>Фильтры — ШОРТ</b>", kb.kb_short_filters(user))
        else:
            await _answer(call, "🔬 <b>Фильтры сигнала</b>", kb.kb_filters(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Качество
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_quality", "menu_long_quality", "menu_short_quality"}))
    async def cb_menu_quality(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_quality":
            await _answer(call, "⭐ <b>Качество — ЛОНГ</b>", kb.kb_long_quality(user))
        elif call.data == "menu_short_quality":
            await _answer(call, "⭐ <b>Качество — ШОРТ</b>", kb.kb_short_quality(user))
        else:
            await _answer(call, "⭐ <b>Качество сигнала</b>", kb.kb_quality(user.min_quality))

    def _apply_quality(data: str, user: UserSettings):
        if data.startswith("long_set_quality_"):
            cfg = user.get_long_cfg()
            cfg.min_quality = int(data.split("_")[-1])
            user.set_long_cfg(cfg)
        elif data.startswith("short_set_quality_"):
            cfg = user.get_short_cfg()
            cfg.min_quality = int(data.split("_")[-1])
            user.set_short_cfg(cfg)
        else:
            user.min_quality = int(data.replace("set_quality_", ""))

    @dp.callback_query(F.data.startswith("set_quality_") | F.data.startswith("long_set_quality_") |
                       F.data.startswith("short_set_quality_"))
    async def cb_set_quality(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        _apply_quality(call.data, user)
        await um.save(user)
        if call.data.startswith("long_"):
            await _answer(call, "⭐ <b>Качество — ЛОНГ</b>", kb.kb_long_quality(user))
        elif call.data.startswith("short_"):
            await _answer(call, "⭐ <b>Качество — ШОРТ</b>", kb.kb_short_quality(user))
        else:
            await _answer(call, "⭐ <b>Качество сигнала</b>", kb.kb_quality(user.min_quality))

    # ─────────────────────────────────────────────────────────────────────
    #  Cooldown
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_cooldown", "menu_long_cooldown", "menu_short_cooldown"}))
    async def cb_menu_cooldown(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_cooldown":
            await _answer(call, "🔁 <b>Cooldown — ЛОНГ</b>", kb.kb_long_cooldown(user))
        elif call.data == "menu_short_cooldown":
            await _answer(call, "🔁 <b>Cooldown — ШОРТ</b>", kb.kb_short_cooldown(user))
        else:
            await _answer(call, "🔁 <b>Cooldown</b>", kb.kb_cooldown(user.cooldown_bars))

    @dp.callback_query(F.data.startswith("set_cooldown_") | F.data.startswith("long_set_cooldown_") |
                       F.data.startswith("short_set_cooldown_"))
    async def cb_set_cooldown(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        raw = call.data
        if raw.startswith("long_set_cooldown_"):
            cfg = user.get_long_cfg()
            cfg.cooldown_bars = int(raw.split("_")[-1])
            user.set_long_cfg(cfg)
            await um.save(user)
            await _answer(call, "🔁 <b>Cooldown — ЛОНГ</b>", kb.kb_long_cooldown(user))
        elif raw.startswith("short_set_cooldown_"):
            cfg = user.get_short_cfg()
            cfg.cooldown_bars = int(raw.split("_")[-1])
            user.set_short_cfg(cfg)
            await um.save(user)
            await _answer(call, "🔁 <b>Cooldown — ШОРТ</b>", kb.kb_short_cooldown(user))
        else:
            user.cooldown_bars = int(raw.replace("set_cooldown_", ""))
            await um.save(user)
            await _answer(call, "🔁 <b>Cooldown</b>", kb.kb_cooldown(user.cooldown_bars))

    # ─────────────────────────────────────────────────────────────────────
    #  Стоп-лосс (ATR)
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_sl", "menu_long_sl", "menu_short_sl"}))
    async def cb_menu_sl(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_sl":
            await _answer(call, "🛡 <b>Стоп-лосс — ЛОНГ</b>", kb.kb_long_sl(user))
        elif call.data == "menu_short_sl":
            await _answer(call, "🛡 <b>Стоп-лосс — ШОРТ</b>", kb.kb_short_sl(user))
        else:
            await _answer(call, "🛡 <b>Стоп-лосс (ATR)</b>", kb.kb_sl(user))

    def _apply_sl(data: str, user: UserSettings):
        if data.startswith("long_"):
            cfg = user.get_long_cfg(); key = data[len("long_"):]; prefix = "long_"
        elif data.startswith("short_"):
            cfg = user.get_short_cfg(); key = data[len("short_"):]; prefix = "short_"
        else:
            cfg = None; key = data; prefix = ""

        if key.startswith("set_atr_period_"):
            v = int(key.split("_")[-1])
            if cfg: cfg.atr_period = v
            else:   user.atr_period = v
        elif key.startswith("set_atr_mult_"):
            v = float(key.split("_")[-1])
            if cfg: cfg.atr_mult = v
            else:   user.atr_mult = v
        elif key.startswith("set_risk_"):
            v = float(key.split("_")[-1])
            if cfg: cfg.max_risk_pct = v
            else:   user.max_risk_pct = v

        if prefix == "long_" and cfg:    user.set_long_cfg(cfg)
        elif prefix == "short_" and cfg:  user.set_short_cfg(cfg)

    @dp.callback_query(F.data.startswith("set_atr_") | F.data.startswith("set_risk_") |
                       F.data.startswith("long_set_atr_") | F.data.startswith("short_set_atr_") |
                       F.data.startswith("long_set_risk_") | F.data.startswith("short_set_risk_"))
    async def cb_set_sl(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        _apply_sl(call.data, user)
        await um.save(user)
        if call.data.startswith("long_"):
            await _answer(call, "🛡 <b>Стоп-лосс — ЛОНГ</b>", kb.kb_long_sl(user))
        elif call.data.startswith("short_"):
            await _answer(call, "🛡 <b>Стоп-лосс — ШОРТ</b>", kb.kb_short_sl(user))
        else:
            await _answer(call, "🛡 <b>Стоп-лосс (ATR)</b>", kb.kb_sl(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Take Profit
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_targets", "menu_long_targets", "menu_short_targets"}))
    async def cb_menu_targets(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_targets":
            await _answer(call, "🎯 <b>Take Profit — ЛОНГ</b>", kb.kb_long_targets(user))
        elif call.data == "menu_short_targets":
            await _answer(call, "🎯 <b>Take Profit — ШОРТ</b>", kb.kb_short_targets(user))
        else:
            await _answer(call, "🎯 <b>Take Profit</b>", kb.kb_targets(user))

    @dp.callback_query(F.data.startswith("edit_tp") | F.data.startswith("edit_long_tp") |
                       F.data.startswith("edit_short_tp"))
    async def cb_edit_tp(call: CallbackQuery, state: FSMContext):
        user = await _get_user(call, um)
        if not user: return

        d = call.data
        if d.startswith("edit_long_tp"):
            prefix = "long_"; tp_n = d.replace("edit_long_tp", "")
        elif d.startswith("edit_short_tp"):
            prefix = "short_"; tp_n = d.replace("edit_short_tp", "")
        else:
            prefix = ""; tp_n = d.replace("edit_tp", "")

        await state.update_data(tp_n=tp_n, prefix=prefix, user_id=user.user_id)
        state_map = {"1": TPInput.waiting_tp1, "2": TPInput.waiting_tp2, "3": TPInput.waiting_tp3}
        await state.set_state(state_map.get(tp_n, TPInput.waiting_tp1))

        cfg = user.get_long_cfg() if prefix == "long_" else (
              user.get_short_cfg() if prefix == "short_" else user.shared_cfg())
        cur = getattr(cfg, f"tp{tp_n}_rr")
        await call.message.answer(
            f"🎯 <b>Цель {tp_n}</b> — текущее значение: <b>{cur}R</b>\n\n"
            f"Введи новое значение (например: <code>1.5</code>):",
            parse_mode="HTML",
        )
        await call.answer()

    @dp.message(TPInput.waiting_tp1)
    @dp.message(TPInput.waiting_tp2)
    @dp.message(TPInput.waiting_tp3)
    async def input_tp(msg: Message, state: FSMContext):
        data = await state.get_data()
        try:
            val = float(msg.text.strip().replace(",", "."))
            if val <= 0 or val > 50:
                raise ValueError
        except ValueError:
            await msg.answer("❌ Неверный формат. Введи число, например <code>2.0</code>", parse_mode="HTML")
            return

        user = await um.get(data["user_id"])
        if not user:
            await state.clear(); return

        prefix = data.get("prefix", "")
        tp_n   = data.get("tp_n", "1")

        if prefix == "long_":
            cfg = user.get_long_cfg()
            setattr(cfg, f"tp{tp_n}_rr", val)
            user.set_long_cfg(cfg)
        elif prefix == "short_":
            cfg = user.get_short_cfg()
            setattr(cfg, f"tp{tp_n}_rr", val)
            user.set_short_cfg(cfg)
        else:
            setattr(user, f"tp{tp_n}_rr", val)

        await um.save(user)
        await state.clear()

        back_map = {"long_": kb.kb_long_targets, "short_": kb.kb_short_targets, "": kb.kb_targets}
        mk = back_map[prefix](user)
        label = {"long_": "ЛОНГ", "short_": "ШОРТ", "": "Общие"}[prefix]
        await msg.answer(
            f"✅ Цель {tp_n} установлена: <b>{val}R</b>",
            parse_mode="HTML",
            reply_markup=mk,
        )

    # ─────────────────────────────────────────────────────────────────────
    #  Объём монет
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.in_({"menu_volume", "menu_long_volume", "menu_short_volume"}))
    async def cb_menu_volume(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        if call.data == "menu_long_volume":
            await _answer(call, "💰 <b>Фильтр монет — ЛОНГ</b>", kb.kb_long_volume(user))
        elif call.data == "menu_short_volume":
            await _answer(call, "💰 <b>Фильтр монет — ШОРТ</b>", kb.kb_short_volume(user))
        else:
            await _answer(call, "💰 <b>Фильтр монет по объёму</b>", kb.kb_volume(user.min_volume_usdt))

    @dp.callback_query(F.data.startswith("set_volume_") | F.data.startswith("long_set_volume_") |
                       F.data.startswith("short_set_volume_"))
    async def cb_set_volume(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        raw = call.data
        if raw.startswith("long_set_volume_"):
            cfg = user.get_long_cfg()
            cfg.min_volume_usdt = float(raw.split("_")[-1])
            user.set_long_cfg(cfg)
            await um.save(user)
            await _answer(call, "💰 <b>Фильтр монет — ЛОНГ</b>", kb.kb_long_volume(user))
        elif raw.startswith("short_set_volume_"):
            cfg = user.get_short_cfg()
            cfg.min_volume_usdt = float(raw.split("_")[-1])
            user.set_short_cfg(cfg)
            await um.save(user)
            await _answer(call, "💰 <b>Фильтр монет — ШОРТ</b>", kb.kb_short_volume(user))
        else:
            user.min_volume_usdt = float(raw.replace("set_volume_", ""))
            await um.save(user)
            await _answer(call, "💰 <b>Фильтр монет по объёму</b>", kb.kb_volume(user.min_volume_usdt))

    # ─────────────────────────────────────────────────────────────────────
    #  Уведомления
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "menu_notify")
    async def cb_menu_notify(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await _answer(call, "📱 <b>Уведомления</b>", kb.kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify_signal")
    async def cb_toggle_notify_signal(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.notify_signal = not user.notify_signal
        await um.save(user)
        await _answer(call, "📱 <b>Уведомления</b>", kb.kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify_breakout")
    async def cb_toggle_notify_breakout(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        user.notify_breakout = not user.notify_breakout
        await um.save(user)
        await _answer(call, "📱 <b>Уведомления</b>", kb.kb_notify(user))

    # ─────────────────────────────────────────────────────────────────────
    #  Статистика (главный экран)
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "my_stats")
    async def cb_my_stats(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        stats = await db.db_get_user_stats(user.user_id)
        await _answer(call, _stats_text(stats), _stats_kb())

    @dp.callback_query(F.data.startswith("trade_history_"))
    async def cb_trade_history(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        page = int(call.data.replace("trade_history_", ""))
        text, mkb = await _history_text_and_kb(user.user_id, page)
        await _answer(call, text, mkb)

    @dp.callback_query(F.data == "trade_reset_confirm")
    async def cb_trade_reset_confirm(call: CallbackQuery):
        await _answer(
            call,
            "⚠️ <b>Сбросить статистику?</b>\n\nВсе записанные сделки будут удалены без возможности восстановления.",
            InlineKeyboardMarkup(inline_keyboard=[
                _b("🗑 Да, удалить всё", "trade_reset_do"),
                _b("◀️ Отмена",          "my_stats"),
            ])
        )

    @dp.callback_query(F.data == "trade_reset_do")
    async def cb_trade_reset_do(call: CallbackQuery):
        user = await _get_user(call, um)
        if not user: return
        await db.db_reset_user_trades(user.user_id)
        await call.answer("✅ Статистика сброшена", show_alert=True)
        await _answer(call, _stats_text({}), _stats_kb())

    # ─────────────────────────────────────────────────────────────────────
    #  Кнопки под сигналом
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("sig_checks_"))
    async def cb_sig_checks(call: CallbackQuery):
        trade_id = call.data.replace("sig_checks_", "")
        sig_cache = scanner.get_sig_cache()
        if trade_id not in sig_cache:
            await call.answer("⏳ Данные устарели. Подтверждения недоступны.", show_alert=True)
            return
        sig, user = sig_cache[trade_id]
        text = make_checklist_text(sig, user)
        await call.message.answer(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                _b("✕ Закрыть", f"close_msg"),
            ])
        )
        await call.answer()

    @dp.callback_query(F.data == "close_msg")
    async def cb_close_msg(call: CallbackQuery):
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer()

    @dp.callback_query(F.data.startswith("sig_chart_"))
    async def cb_sig_chart(call: CallbackQuery):
        trade_id = call.data.replace("sig_chart_", "")
        sig_cache = scanner.get_sig_cache()
        if trade_id not in sig_cache:
            await call.answer("⏳ Данные устарели.", show_alert=True)
            return
        sig, user = sig_cache[trade_id]
        # Генерируем ссылку TradingView
        symbol_clean = sig.symbol.replace("-USDT-SWAP", "").replace("-USDT", "").replace("USDT", "")
        tf_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
        tf = tf_map.get(user.timeframe, "60")
        url = f"https://ru.tradingview.com/chart/?symbol=OKX:{symbol_clean}USDT.P&interval={tf}"
        await call.message.answer(
            f"📊 <b>График</b> · {sig.symbol}\n\n"
            f"<a href=\"{url}\">🔗 Открыть в TradingView</a>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_b("✕ Закрыть", "close_msg")])
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("sig_hide_"))
    async def cb_sig_hide(call: CallbackQuery):
        trade_id = call.data.replace("sig_hide_", "")
        sig_cache = scanner.get_sig_cache()
        symbol = "сигнал"
        if trade_id in sig_cache:
            sig, _ = sig_cache[trade_id]
            symbol = sig.symbol

        await _answer(
            call,
            f"🔕 <b>Скрыть {symbol}?</b>",
            InlineKeyboardMarkup(inline_keyboard=[
                _b(f"⏸ Скрыть этот сигнал",       f"hide_once_{trade_id}"),
                _b(f"⏹ Скрыть {symbol} на 1 час",  f"hide_1h_{trade_id}"),
                _b(f"🚫 Скрыть {symbol} навсегда",  f"hide_perm_{trade_id}"),
                _b("◀️ Отмена",                     "close_msg"),
            ])
        )

    @dp.callback_query(F.data.startswith("hide_once_") | F.data.startswith("hide_1h_") |
                       F.data.startswith("hide_perm_"))
    async def cb_hide_action(call: CallbackQuery):
        await call.answer("✅ Скрыто", show_alert=False)
        try:
            await call.message.delete()
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    #  📈 Статистика (кнопка под сигналом) → запись результата
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("sig_stats_"))
    async def cb_sig_stats(call: CallbackQuery):
        trade_id = call.data.replace("sig_stats_", "")
        sig_cache = scanner.get_sig_cache()
        trade = await db.db_get_trade(trade_id)

        if trade and trade.get("result"):
            # Уже записана
            result_labels = {"TP1": "🎯 TP1 ✅", "TP2": "🎯 TP2 ✅", "TP3": "🏆 TP3 ✅",
                             "SL": "❌ Убыток", "BE": "➖ Безубыток"}
            res_label = result_labels.get(trade["result"], trade["result"])
            await call.message.answer(
                f"📈 <b>Результат уже записан</b>\n\n"
                f"<b>{trade['symbol']}</b> {trade['direction']}\n"
                f"Результат: <b>{res_label}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[_b("✕ Закрыть", "close_msg")])
            )
            await call.answer()
            return

        sym = trade["symbol"] if trade else "Сделка"
        direction = trade["direction"] if trade else ""
        entry = trade["entry"] if trade else 0

        await call.message.answer(
            f"📈 <b>Записать результат</b>\n\n"
            f"<b>{sym}</b> {direction}  <code>{entry}</code>\n\n"
            f"Выбери результат сделки:",
            parse_mode="HTML",
            reply_markup=_pick_result_kb(trade_id),
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("trade_pick_result_"))
    async def cb_trade_pick_result(call: CallbackQuery):
        trade_id = call.data.replace("trade_pick_result_", "")
        trade = await db.db_get_trade(trade_id)
        sym = trade["symbol"] if trade else "Сделка"
        await _answer(
            call,
            f"📈 <b>Результат сделки</b>\n<b>{sym}</b>\n\nВыбери:",
            _pick_result_kb(trade_id)
        )

    @dp.callback_query(F.data.startswith("trade_result_win_"))
    async def cb_result_win(call: CallbackQuery):
        trade_id = call.data.replace("trade_result_win_", "")
        trade = await db.db_get_trade(trade_id)
        sym = trade["symbol"] if trade else "Сделка"
        await _answer(
            call,
            f"✅ <b>Победа!</b>  <b>{sym}</b>\n\nНа какой цели вышел?",
            _pick_tp_kb(trade_id)
        )

    @dp.callback_query(F.data.startswith("trade_result_loss_"))
    async def cb_result_loss(call: CallbackQuery):
        trade_id = call.data.replace("trade_result_loss_", "")
        trade = await db.db_get_trade(trade_id)
        rr = -abs(trade.get("tp1_rr", 1.0)) if trade else -1.0
        await db.db_set_trade_result(trade_id, "SL", rr)
        await _answer(
            call,
            f"❌ <b>Убыток записан</b>\n\n<b>{trade['symbol'] if trade else ''}</b>  {rr:.1f}R",
            InlineKeyboardMarkup(inline_keyboard=[
                _b("📊 К статистике", "my_stats"),
                _b("✕ Закрыть",       "close_msg"),
            ])
        )

    @dp.callback_query(F.data.startswith("trade_result_be_"))
    async def cb_result_be(call: CallbackQuery):
        trade_id = call.data.replace("trade_result_be_", "")
        trade = await db.db_get_trade(trade_id)
        await db.db_set_trade_result(trade_id, "BE", 0.0)
        await _answer(
            call,
            f"➖ <b>Безубыток записан</b>\n\n<b>{trade['symbol'] if trade else ''}</b>  0.0R",
            InlineKeyboardMarkup(inline_keyboard=[
                _b("📊 К статистике", "my_stats"),
                _b("✕ Закрыть",       "close_msg"),
            ])
        )

    @dp.callback_query(F.data.startswith("trade_tp_"))
    async def cb_trade_tp(call: CallbackQuery):
        # trade_tp_{1|2|3}_{trade_id}
        parts    = call.data.replace("trade_tp_", "").split("_", 1)
        tp_n     = parts[0]
        trade_id = parts[1] if len(parts) > 1 else ""
        trade    = await db.db_get_trade(trade_id)
        if not trade:
            await call.answer("❌ Сделка не найдена", show_alert=True); return

        rr_key = f"tp{tp_n}_rr"
        rr = trade.get(rr_key, 1.0)
        result_label = f"TP{tp_n}"
        await db.db_set_trade_result(trade_id, result_label, float(rr))

        tp_icons = {"1": "🎯", "2": "🎯", "3": "🏆"}
        await _answer(
            call,
            f"{tp_icons.get(tp_n, '🎯')} <b>TP{tp_n} записан!</b>\n\n"
            f"<b>{trade['symbol']}</b> {trade['direction']}  <b>+{rr:.2f}R</b>",
            InlineKeyboardMarkup(inline_keyboard=[
                _b("📊 К статистике", "my_stats"),
                _b("✕ Закрыть",       "close_msg"),
            ])
        )

    @dp.callback_query(F.data.startswith("trade_close_stat_"))
    async def cb_trade_close_stat(call: CallbackQuery):
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer()

    # ─────────────────────────────────────────────────────────────────────
    #  Подписка
    # ─────────────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("buy_"))
    async def cb_buy(call: CallbackQuery):
        days_map = {"buy_30": 30, "buy_90": 90, "buy_365": 365}
        days     = days_map.get(call.data, 30)
        price_map = {
            "buy_30":  config.PRICE_30_DAYS,
            "buy_90":  config.PRICE_90_DAYS,
            "buy_365": config.PRICE_365_DAYS,
        }
        price = price_map.get(call.data, "")
        admin_id = config.ADMIN_IDS[0] if config.ADMIN_IDS else None
        admin_link = f"<a href=\"tg://user?id={admin_id}\">написать администратору</a>" if admin_id else "написать администратору"
        await call.message.answer(
            f"💳 <b>Оплата подписки</b>\n\n"
            f"Тариф: <b>{days} дней</b>\nСтоимость: <b>{price}</b>\n\n"
            f"Для оплаты {admin_link} и укажи свой Telegram ID: <code>{call.from_user.id}</code>",
            parse_mode="HTML",
        )
        await call.answer()

    @dp.callback_query(F.data == "contact_admin")
    async def cb_contact_admin(call: CallbackQuery):
        admin_id = config.ADMIN_IDS[0] if config.ADMIN_IDS else None
        if admin_id:
            await call.message.answer(
                f"📩 Напиши администратору и укажи свой ID: <code>{call.from_user.id}</code>",
                parse_mode="HTML",
            )
        await call.answer()

    log.info("✅ Все хэндлеры зарегистрированы (v4.6)")
