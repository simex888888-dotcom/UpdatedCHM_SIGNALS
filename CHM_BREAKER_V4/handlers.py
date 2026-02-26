"""
handlers.py v4 — мультисканнинг ЛОНГ + ШОРТ + ОБА
Исправленная версия для копирования.
"""
import io
import asyncio
import logging
from dataclasses import fields, asdict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

import database as db
from user_manager import UserManager, UserSettings, TradeCfg
from keyboards import (
    kb_main, kb_back, kb_settings, kb_notify, kb_subscribe,
    kb_mode_long, kb_mode_short, kb_mode_both,
    kb_long_timeframes, kb_short_timeframes, kb_timeframes,
    kb_long_intervals, kb_short_intervals, kb_intervals,
    kb_pivots, kb_long_pivots, kb_short_pivots,
    kb_ema, kb_long_ema, kb_short_ema,
    kb_filters, kb_long_filters, kb_short_filters,
    kb_quality, kb_long_quality, kb_short_quality,
    kb_cooldown, kb_long_cooldown, kb_short_cooldown,
    kb_sl, kb_long_sl, kb_short_sl,
    kb_targets, kb_long_targets, kb_short_targets,
    kb_long_volume, kb_short_volume, kb_volume,
    trend_text
)

log = logging.getLogger("CHM.Handlers")

class EditState(StatesGroup):
    long_tp1 = State()
    long_tp2 = State()
    long_tp3 = State()
    long_sl  = State()
    short_tp1 = State()
    short_tp2 = State()
    short_tp3 = State()
    short_sl  = State()

# --- Вспомогательные функции обновления ---

def _update_long_field(user: UserSettings, field_name: str, value):
    cfg = user.get_long_cfg()
    setattr(cfg, field_name, value)
    user.long_cfg = asdict(cfg)

def _update_short_field(user: UserSettings, field_name: str, value):
    cfg = user.get_short_cfg()
    setattr(cfg, field_name, value)
    user.short_cfg = asdict(cfg)

# --- РЕГИСТРАЦИЯ ХЕНДЛЕРОВ ---

def register_handlers(dp: Dispatcher, bot: Bot, um: UserManager, scanner, config):

    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        trend = getattr(scanner, "last_trend", {})
        text = "<b>Привет! Я ProScanner v4</b>\n\n" + trend_text(trend)
        await msg.answer(text, reply_markup=kb_main(user), parse_mode="HTML")

    @dp.callback_query(F.data == "back_main")
    async def back_main(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        trend = getattr(scanner, "last_trend", {})
        await cb.message.edit_text("Главное меню\n\n" + trend_text(trend), 
                                 reply_markup=kb_main(user), parse_mode="HTML")

    # --- Навигация по режимам ---

    @dp.callback_query(F.data == "menu_settings")
    async def menu_settings_main(cb: CallbackQuery):
        await cb.answer()
        await cb.message.edit_text("⚙️ <b>Настройки сканирования</b>\nВыбери направление для индивидуальной настройки:", 
                                 reply_markup=kb_settings(), parse_mode="HTML")

    @dp.callback_query(F.data == "mode_long")
    async def menu_long(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("📈 <b>Настройки LONG</b>", reply_markup=kb_mode_long(user), parse_mode="HTML")

    @dp.callback_query(F.data == "mode_short")
    async def menu_short(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("📉 <b>Настройки SHORT</b>", reply_markup=kb_mode_short(user), parse_mode="HTML")

    @dp.callback_query(F.data == "mode_both")
    async def menu_both(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("🔄 <b>Режим ОБА (Совместный)</b>", reply_markup=kb_mode_both(user), parse_mode="HTML")

    # --- Переключатели (Toggle) ---

    @dp.callback_query(F.data.startswith("toggle_"))
    async def toggle_handler(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        act = cb.data.replace("toggle_", "")
        
        if act == "long_active": user.long_active = not user.long_active
        elif act == "short_active": user.short_active = not user.short_active
        elif act == "active": user.active = not user.active
        elif act == "notify_signal": user.notify_signal = not user.notify_signal
        elif act == "notify_breakout": user.notify_breakout = not user.notify_breakout
        
        await um.save(user)
        await cb.answer("Изменено")
        
        # Обновляем ту же клавиатуру
        if "long" in cb.data: await cb.message.edit_reply_markup(reply_markup=kb_mode_long(user))
        elif "short" in cb.data: await cb.message.edit_reply_markup(reply_markup=kb_mode_short(user))
        elif "notify" in cb.data: await cb.message.edit_reply_markup(reply_markup=kb_notify(user))
        else: await cb.message.edit_reply_markup(reply_markup=kb_mode_both(user))

    # --- LONG Настройки (Таймфреймы, Интервалы и т.д.) ---

    @dp.callback_query(F.data == "menu_long_tf")
    async def menu_long_tf(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("Выбери TF для LONG:", reply_markup=kb_long_timeframes(user.get_long_cfg().timeframe))

    @dp.callback_query(F.data.startswith("set_long_tf_"))
    async def set_long_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        val = cb.data.replace("set_long_tf_", "")
        _update_long_field(user, "timeframe", val)
        await um.save(user)
        await cb.answer(f"LONG TF: {val}")
        await cb.message.edit_reply_markup(reply_markup=kb_long_timeframes(val))

    # --- SHORT Настройки ---

    @dp.callback_query(F.data == "menu_short_tf")
    async def menu_short_tf(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("Выбери TF для SHORT:", reply_markup=kb_short_timeframes(user.get_short_cfg().timeframe))

    @dp.callback_query(F.data.startswith("set_short_tf_"))
    async def set_short_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        val = cb.data.replace("set_short_tf_", "")
        _update_short_field(user, "timeframe", val)
        await um.save(user)
        await cb.answer(f"SHORT TF: {val}")
        await cb.message.edit_reply_markup(reply_markup=kb_short_timeframes(val))

    # --- Тейк-профиты и Стоп-лосс (Ввод текста) ---

    @dp.callback_query(F.data == "menu_long_targets")
    async def menu_long_targets(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("🎯 <b>Цели LONG (R:R)</b>", reply_markup=kb_long_targets(user), parse_mode="HTML")

    @dp.callback_query(F.data == "edit_long_tp1")
    async def edit_long_tp1(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await cb.message.answer("Введите значение TP1 для LONG (например 1.5):")
        await state.set_state(EditState.long_tp1)

    @dp.message(EditState.long_tp1)
    async def save_long_tp1(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            val = float(msg.text.replace(",", "."))
            _update_long_field(user, "tp1_rr", val)
            await um.save(user)
            await state.clear()
            await msg.answer(f"✅ TP1 LONG установлен: {val}", reply_markup=kb_long_targets(user))
        except:
            await msg.answer("❌ Ошибка. Введите число.")

    @dp.callback_query(F.data == "menu_short_targets")
    async def menu_short_targets(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await cb.message.edit_text("🎯 <b>Цели SHORT (R:R)</b>", reply_markup=kb_short_targets(user), parse_mode="HTML")

    @dp.callback_query(F.data == "edit_short_tp1")
    async def edit_short_tp1(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await cb.message.answer("Введите значение TP1 для SHORT:")
        await state.set_state(EditState.short_tp1)

    @dp.message(EditState.short_tp1)
    async def save_short_tp1(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            val = float(msg.text.replace(",", "."))
            _update_short_field(user, "tp1_rr", val)
            await um.save(user)
            await state.clear()
            await msg.answer(f"✅ TP1 SHORT установлен: {val}", reply_markup=kb_short_targets(user))
        except:
            await msg.answer("❌ Ошибка. Введите число.")

    # --- Прочие хендлеры (Статистика, Подписка) ---

    @dp.callback_query(F.data == "menu_stats")
    async def menu_stats(cb: CallbackQuery):
        await cb.answer("Загрузка статистики...")
        stats = await db.db_get_stats()
        text = (f"📊 <b>Статистика системы:</b>\n\n"
                f"Всего сигналов: {stats.get('total', 0)}\n"
                f"Винрейт: {stats.get('winrate', 0)}%\n"
                f"Профит (RR): {stats.get('total_rr', 0):.1f}")
        await cb.message.edit_text(text, reply_markup=kb_back(), parse_mode="HTML")

    @dp.callback_query(F.data == "menu_sub")
    async def menu_sub(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        status = "Активна" if user.is_sub_active() else "Истекла"
        text = f"💳 <b>Подписка</b>\nСтатус: {status}\nДо: {user.sub_expires_str()}"
        await cb.message.edit_text(text, reply_markup=kb_subscribe(config), parse_mode="HTML")

    @dp.callback_query(F.data == "noop")
    async def noop(cb: CallbackQuery):
        await cb.answer()
