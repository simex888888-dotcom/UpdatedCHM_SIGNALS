"""
Обработчики команд и кнопок Telegram бота
"""

import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from user_manager import UserManager, UserSettings
from keyboards import (
    kb_main, kb_timeframes, kb_intervals, kb_filters,
    kb_quality, kb_targets, kb_volume, kb_back
)

log = logging.getLogger("CHM.Handlers")


class EditState(StatesGroup):
    waiting_tp1 = State()
    waiting_tp2 = State()
    waiting_tp3 = State()


def settings_text(user: UserSettings) -> str:
    status = "🟢 АКТИВЕН" if user.active else "🔴 ОСТАНОВЛЕН"
    filters = []
    if user.use_rsi:     filters.append("RSI")
    if user.use_volume:  filters.append("Объём")
    if user.use_pattern: filters.append("Паттерн")
    if user.use_htf:     filters.append("HTF")

    return (
        f"⚡ <b>CHM BREAKER — Твои настройки</b>\n"
        f"\n"
        f"Статус: <b>{status}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Таймфрейм:      <b>{user.timeframe}</b>\n"
        f"🔄 Интервал скана: <b>каждые {user.scan_interval // 60} мин.</b>\n"
        f"💰 Мин. объём:     <b>${user.min_volume_usdt:,.0f}</b>\n"
        f"⭐ Мин. качество:  <b>{'⭐' * user.min_quality}</b>\n"
        f"🎯 Цели:           <b>{user.tp1_rr}R / {user.tp2_rr}R / {user.tp3_rr}R</b>\n"
        f"🔬 Фильтры:        <b>{', '.join(filters) if filters else 'все выкл'}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Сигналов получено: <b>{user.signals_received}</b>\n"
    )


def register_handlers(dp: Dispatcher, bot: Bot, um: UserManager, scanner, config):

    # ── /start ───────────────────────────────────────────
    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        user = um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        await msg.answer(
            f"👋 Привет, <b>{msg.from_user.first_name}</b>!\n"
            f"\n"
            f"⚡ <b>CHM BREAKER BOT</b> — by CHM Laboratory\n"
            f"\n"
            f"Я сканирую 200+ монет на OKX и шлю тебе сигналы\n"
            f"прямо сюда когда индикатор CHM BREAKER даёт вход.\n"
            f"\n"
            f"Настрой бота под себя и включи сканер 👇",
            parse_mode="HTML",
            reply_markup=kb_main(user),
        )

    # ── /menu ─────────────────────────────────────────────
    @dp.message(Command("menu"))
    async def cmd_menu(msg: Message):
        user = um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        await msg.answer(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    # ── /stop ─────────────────────────────────────────────
    @dp.message(Command("stop"))
    async def cmd_stop(msg: Message):
        user = um.get_or_create(msg.from_user.id)
        user.active = False
        um.save_user(user)
        await msg.answer("🔴 Сканер остановлен. Сигналы больше не приходят.\n\nНажми /menu чтобы включить снова.")

    # ── Включить/выключить сканер ────────────────────────
    @dp.callback_query(F.data == "toggle_active")
    async def toggle_active(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.active = not user.active
        um.save_user(user)
        status = "🟢 Сканер включён! Сигналы будут приходить сюда." if user.active \
            else "🔴 Сканер выключен."
        await cb.answer(status)
        await cb.message.edit_text(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    # ── Меню таймфреймов ─────────────────────────────────
    @dp.callback_query(F.data == "menu_tf")
    async def menu_tf(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("📊 <b>Выбери таймфрейм свечей:</b>", parse_mode="HTML",
                                   reply_markup=kb_timeframes(user.timeframe))

    @dp.callback_query(F.data.startswith("set_tf_"))
    async def set_tf(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        tf = cb.data.replace("set_tf_", "")
        user.timeframe = tf
        um.save_user(user)
        await cb.answer(f"✅ Таймфрейм: {tf}")
        await cb.message.edit_text(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    # ── Меню интервала ───────────────────────────────────
    @dp.callback_query(F.data == "menu_interval")
    async def menu_interval(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("🔄 <b>Как часто сканировать все монеты?</b>", parse_mode="HTML",
                                   reply_markup=kb_intervals(user.scan_interval))

    @dp.callback_query(F.data.startswith("set_interval_"))
    async def set_interval(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        sec  = int(cb.data.replace("set_interval_", ""))
        user.scan_interval = sec
        um.save_user(user)
        await cb.answer(f"✅ Интервал: каждые {sec // 60} мин.")
        await cb.message.edit_text(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    # ── Меню фильтров ────────────────────────────────────
    @dp.callback_query(F.data == "menu_filters")
    async def menu_filters(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text(
            "🔬 <b>Условия подачи сигнала</b>\n\nНажми чтобы включить/выключить:",
            parse_mode="HTML", reply_markup=kb_filters(user))

    @dp.callback_query(F.data == "toggle_rsi")
    async def toggle_rsi(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.use_rsi = not user.use_rsi
        um.save_user(user)
        await cb.answer("RSI фильтр: " + ("✅ вкл" if user.use_rsi else "❌ выкл"))
        await cb.message.edit_reply_markup(reply_markup=kb_filters(user))

    @dp.callback_query(F.data == "toggle_volume")
    async def toggle_volume(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.use_volume = not user.use_volume
        um.save_user(user)
        await cb.answer("Объёмный фильтр: " + ("✅ вкл" if user.use_volume else "❌ выкл"))
        await cb.message.edit_reply_markup(reply_markup=kb_filters(user))

    @dp.callback_query(F.data == "toggle_pattern")
    async def toggle_pattern(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.use_pattern = not user.use_pattern
        um.save_user(user)
        await cb.answer("Паттерны: " + ("✅ вкл" if user.use_pattern else "❌ выкл"))
        await cb.message.edit_reply_markup(reply_markup=kb_filters(user))

    @dp.callback_query(F.data == "toggle_htf")
    async def toggle_htf(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.use_htf = not user.use_htf
        um.save_user(user)
        await cb.answer("HTF фильтр: " + ("✅ вкл" if user.use_htf else "❌ выкл"))
        await cb.message.edit_reply_markup(reply_markup=kb_filters(user))

    @dp.callback_query(F.data == "toggle_notify_signal")
    async def toggle_notify_signal(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.notify_signal = not user.notify_signal
        um.save_user(user)
        await cb.answer("Уведомление о сигнале: " + ("✅ вкл" if user.notify_signal else "❌ выкл"))
        await cb.message.edit_reply_markup(reply_markup=kb_filters(user))

    @dp.callback_query(F.data == "toggle_notify_breakout")
    async def toggle_notify_breakout(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        user.notify_breakout = not user.notify_breakout
        um.save_user(user)
        await cb.answer("Уведомление о пробое: " + ("✅ вкл" if user.notify_breakout else "❌ выкл"))
        await cb.message.edit_reply_markup(reply_markup=kb_filters(user))

    # ── Меню качества ────────────────────────────────────
    @dp.callback_query(F.data == "menu_quality")
    async def menu_quality(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text(
            "⭐ <b>Минимальное качество сигнала</b>\n\nЧем выше — тем меньше сигналов, но надёжнее:",
            parse_mode="HTML", reply_markup=kb_quality(user.min_quality))

    @dp.callback_query(F.data.startswith("set_quality_"))
    async def set_quality(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        q = int(cb.data.replace("set_quality_", ""))
        user.min_quality = q
        um.save_user(user)
        await cb.answer(f"✅ Минимальное качество: {'⭐' * q}")
        await cb.message.edit_text(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    # ── Меню целей ───────────────────────────────────────
    @dp.callback_query(F.data == "menu_targets")
    async def menu_targets(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text(
            "🎯 <b>Цели (соотношение риск/прибыль)</b>\n\n1R = расстояние от входа до стопа.\nНажми на цель чтобы изменить:",
            parse_mode="HTML", reply_markup=kb_targets(user))

    @dp.callback_query(F.data == "edit_tp1")
    async def edit_tp1(cb: CallbackQuery, state: FSMContext):
        await state.set_state(EditState.waiting_tp1)
        await cb.message.answer("Введи значение для Цели 1 (например: 0.8 или 1.0 или 1.5):")

    @dp.callback_query(F.data == "edit_tp2")
    async def edit_tp2(cb: CallbackQuery, state: FSMContext):
        await state.set_state(EditState.waiting_tp2)
        await cb.message.answer("Введи значение для Цели 2 (например: 1.5 или 2.0):")

    @dp.callback_query(F.data == "edit_tp3")
    async def edit_tp3(cb: CallbackQuery, state: FSMContext):
        await state.set_state(EditState.waiting_tp3)
        await cb.message.answer("Введи значение для Цели 3 (например: 2.5 или 3.0):")

    @dp.message(EditState.waiting_tp1)
    async def save_tp1(msg: Message, state: FSMContext):
        user = um.get_or_create(msg.from_user.id)
        try:
            val = float(msg.text.replace(",", "."))
            user.tp1_rr = round(val, 1)
            um.save_user(user)
            await state.clear()
            await msg.answer(f"✅ Цель 1 = {user.tp1_rr}R", reply_markup=kb_targets(user))
        except ValueError:
            await msg.answer("❌ Введи число, например: 0.8")

    @dp.message(EditState.waiting_tp2)
    async def save_tp2(msg: Message, state: FSMContext):
        user = um.get_or_create(msg.from_user.id)
        try:
            val = float(msg.text.replace(",", "."))
            user.tp2_rr = round(val, 1)
            um.save_user(user)
            await state.clear()
            await msg.answer(f"✅ Цель 2 = {user.tp2_rr}R", reply_markup=kb_targets(user))
        except ValueError:
            await msg.answer("❌ Введи число, например: 1.5")

    @dp.message(EditState.waiting_tp3)
    async def save_tp3(msg: Message, state: FSMContext):
        user = um.get_or_create(msg.from_user.id)
        try:
            val = float(msg.text.replace(",", "."))
            user.tp3_rr = round(val, 1)
            um.save_user(user)
            await state.clear()
            await msg.answer(f"✅ Цель 3 = {user.tp3_rr}R", reply_markup=kb_targets(user))
        except ValueError:
            await msg.answer("❌ Введи число, например: 2.5")

    # ── Меню объёма ──────────────────────────────────────
    @dp.callback_query(F.data == "menu_volume")
    async def menu_volume(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text(
            "💰 <b>Минимальный суточный объём монеты</b>\n\nМонеты ниже этого объёма пропускаются:",
            parse_mode="HTML", reply_markup=kb_volume(user.min_volume_usdt))

    @dp.callback_query(F.data.startswith("set_volume_"))
    async def set_volume(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        vol  = float(cb.data.replace("set_volume_", ""))
        user.min_volume_usdt = vol
        um.save_user(user)
        await cb.answer(f"✅ Мин. объём: ${vol:,.0f}")
        await cb.message.edit_text(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    # ── Статистика ───────────────────────────────────────
    @dp.callback_query(F.data == "my_stats")
    async def my_stats(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text(
            f"📈 <b>Твоя статистика</b>\n\n"
            f"Сигналов получено: <b>{user.signals_received}</b>\n"
            f"Сканер сейчас: <b>{'🟢 активен' if user.active else '🔴 выключен'}</b>\n",
            parse_mode="HTML", reply_markup=kb_back())

    # ── Назад ─────────────────────────────────────────────
    @dp.callback_query(F.data == "back_main")
    async def back_main(cb: CallbackQuery):
        user = um.get_or_create(cb.from_user.id)
        await cb.message.edit_text(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))
