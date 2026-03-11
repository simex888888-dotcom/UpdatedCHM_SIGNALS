"""
pd_handlers.py — Telegram-хендлеры для вкладки Памп/Дамп.

Команды:
  /pd или кнопка "🎰 Памп/Дамп" — главное меню модуля
  Подписка / отписка на алерты
  Настройка порога уверенности (70–95%)
  Статистика сигналов (/pd_stats)
  Топ-5 монет по текущему score (/pd_top)
  Статус системы
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from pump_dump.pd_config import DEFAULT_USER_THRESHOLD

if TYPE_CHECKING:
    from pump_dump.pd_runner import PDRunner

log = logging.getLogger("CHM.PD.Handlers")

# Временное хранилище текущих scores (заполняется PDRunner)
_current_scores: dict[str, float] = {}


def set_current_scores(scores: dict[str, float]):
    """Вызывается из PDRunner после каждого цикла анализа."""
    _current_scores.update(scores)


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _kb_pd_main(subscribed: bool, threshold: int) -> InlineKeyboardMarkup:
    sub_btn = "🔕 Отписаться от алертов" if subscribed else "🔔 Подписаться на алерты"
    sub_cb  = "pd_unsubscribe" if subscribed else "pd_subscribe"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sub_btn, callback_data=sub_cb)],
        [InlineKeyboardButton(text=f"🎚 Порог уверенности: {threshold}%",
                              callback_data="pd_threshold_menu")],
        [InlineKeyboardButton(text="📊 Моя статистика сигналов",   callback_data="pd_stats")],
        [InlineKeyboardButton(text="🏆 Топ-5 монет сейчас",        callback_data="pd_top")],
        [InlineKeyboardButton(text="💸 Аномальный Funding Rate",   callback_data="pd_funding")],
        [InlineKeyboardButton(text="🔄 Статус системы",            callback_data="pd_status")],
        [InlineKeyboardButton(text="◀️ Главное меню",              callback_data="back_main")],
    ])


def _kb_threshold() -> InlineKeyboardMarkup:
    thresholds = [70, 75, 80, 85, 90, 95]
    rows = []
    for i in range(0, len(thresholds), 3):
        rows.append([
            InlineKeyboardButton(text=f"{t}%", callback_data=f"pd_thr_{t}")
            for t in thresholds[i:i+3]
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="pd_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_back_pd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Памп/Дамп меню", callback_data="pd_menu"),
    ]])


# ── Тексты ────────────────────────────────────────────────────────────────────

def _pd_main_text(subscribed: bool, threshold: int) -> str:
    status = "🟢 Вы подписаны" if subscribed else "⚫ Вы не подписаны"
    return (
        "🎰 <b>Памп/Дамп детектор</b> (BingX Futures)\n\n"
        f"Статус: {status}\n"
        f"Порог уверенности: <b>{threshold}%</b>\n\n"
        "Система мониторит топ-50 монет на BingX и предупреждает\n"
        "о вероятном памп/дамп событии <b>ДО его начала</b>.\n\n"
        "Каждый сигнал подтверждается минимум 3 независимыми слоями:\n"
        "📦 Объём  •  🌊 CVD  •  📖 Стакан  •  💸 Funding\n"
        "📉 OI  •  🤖 ML модель  •  📊 Цена  •  ↔️ Спред"
    )


# ── Регистрация хендлеров ────────────────────────────────────────────────────

def register_pd_handlers(dp: Dispatcher, bot: Bot, runner_getter):
    """
    runner_getter() → PDRunner (ленивый геттер, т.к. runner создаётся после хендлеров)
    """

    async def _get_sub(user_id: int) -> tuple[bool, int]:
        """Возвращает (subscribed, threshold) для пользователя."""
        row = await db.db_pd_get_user(user_id)
        if row is None:
            return False, DEFAULT_USER_THRESHOLD
        return bool(row["pd_subscribed"]), int(row["pd_threshold"])

    # ── /pd command + кнопка ─────────────────────────────────────────────────
    @dp.message(Command("pd"))
    async def cmd_pd(msg: Message):
        subscribed, threshold = await _get_sub(msg.from_user.id)
        await msg.answer(
            _pd_main_text(subscribed, threshold),
            parse_mode="HTML",
            reply_markup=_kb_pd_main(subscribed, threshold),
        )

    @dp.callback_query(F.data == "pd_menu")
    async def cb_pd_menu(cb: CallbackQuery):
        subscribed, threshold = await _get_sub(cb.from_user.id)
        try:
            await cb.message.edit_text(
                _pd_main_text(subscribed, threshold),
                parse_mode="HTML",
                reply_markup=_kb_pd_main(subscribed, threshold),
            )
        except Exception:
            pass
        await cb.answer()

    # ── Подписка / отписка ────────────────────────────────────────────────────
    @dp.callback_query(F.data == "pd_subscribe")
    async def cb_pd_subscribe(cb: CallbackQuery):
        await db.db_pd_upsert_user(cb.from_user.id, subscribed=True)
        subscribed, threshold = await _get_sub(cb.from_user.id)
        try:
            await cb.message.edit_text(
                _pd_main_text(True, threshold),
                parse_mode="HTML",
                reply_markup=_kb_pd_main(True, threshold),
            )
        except Exception:
            pass
        await cb.answer("✅ Вы подписаны на памп/дамп алерты!")

    @dp.callback_query(F.data == "pd_unsubscribe")
    async def cb_pd_unsubscribe(cb: CallbackQuery):
        await db.db_pd_upsert_user(cb.from_user.id, subscribed=False)
        subscribed, threshold = await _get_sub(cb.from_user.id)
        try:
            await cb.message.edit_text(
                _pd_main_text(False, threshold),
                parse_mode="HTML",
                reply_markup=_kb_pd_main(False, threshold),
            )
        except Exception:
            pass
        await cb.answer("🔕 Вы отписаны от памп/дамп алертов")

    # ── Настройка порога ──────────────────────────────────────────────────────
    @dp.callback_query(F.data == "pd_threshold_menu")
    async def cb_pd_threshold_menu(cb: CallbackQuery):
        _, threshold = await _get_sub(cb.from_user.id)
        try:
            await cb.message.edit_text(
                f"🎚 <b>Порог уверенности</b>\n\n"
                f"Текущий: <b>{threshold}%</b>\n\n"
                f"Чем выше порог — тем меньше сигналов, но точнее.\n"
                f"Рекомендуем: 75–80%",
                parse_mode="HTML",
                reply_markup=_kb_threshold(),
            )
        except Exception:
            pass
        await cb.answer()

    @dp.callback_query(F.data.startswith("pd_thr_"))
    async def cb_pd_threshold_set(cb: CallbackQuery):
        try:
            thr = int(cb.data.split("_")[-1])
        except ValueError:
            await cb.answer(); return
        await db.db_pd_upsert_user(cb.from_user.id, threshold=thr)
        await cb.answer(f"✅ Порог установлен: {thr}%")
        subscribed, _ = await _get_sub(cb.from_user.id)
        try:
            await cb.message.edit_text(
                _pd_main_text(subscribed, thr),
                parse_mode="HTML",
                reply_markup=_kb_pd_main(subscribed, thr),
            )
        except Exception:
            pass

    # ── Статистика ────────────────────────────────────────────────────────────
    @dp.callback_query(F.data == "pd_stats")
    async def cb_pd_stats(cb: CallbackQuery):
        stats = await db.db_pd_stats()
        NL = "\n"
        text = (
            "📊 <b>Статистика сигналов Памп/Дамп</b>" + NL + NL +
            f"За 24 часа:   <b>{stats['day_total']}</b> сигналов, "
            f"<b>{stats['day_correct']}</b> подтверждено "
            f"({stats['day_prec']:.0f}%)" + NL +
            f"За 7 дней:    <b>{stats['week_total']}</b> сигналов, "
            f"<b>{stats['week_correct']}</b> подтверждено "
            f"({stats['week_prec']:.0f}%)" + NL + NL +
            f"Сигнал считается верным если цена двигалась >= 3% за 15 мин."
        )
        try:
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_kb_back_pd())
        except Exception:
            pass
        await cb.answer()

    # ── Топ монет ─────────────────────────────────────────────────────────────
    @dp.callback_query(F.data == "pd_top")
    async def cb_pd_top(cb: CallbackQuery):
        if not _current_scores:
            await cb.answer("⏳ Анализ ещё не завершён, подождите немного")
            return
        top5 = sorted(_current_scores.items(), key=lambda x: x[1], reverse=True)[:5]
        NL   = "\n"
        lines = ["🏆 <b>Топ-5 монет по текущему score</b>" + NL]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, (sym, score) in enumerate(top5):
            lines.append(f"{medals[i]} <b>{sym}</b> — {score:.0f}%")
        try:
            await cb.message.edit_text(NL.join(lines), parse_mode="HTML",
                                       reply_markup=_kb_back_pd())
        except Exception:
            pass
        await cb.answer()

    # ── Funding Rate аномалии ─────────────────────────────────────────────────
    @dp.callback_query(F.data == "pd_funding")
    async def cb_pd_funding(cb: CallbackQuery):
        from pump_dump.hidden_signals import _cache
        NL   = "\n"
        rows = sorted(
            [(sym, v[-1][0]) for sym, v in _cache._funding.items() if v],
            key=lambda x: abs(x[1]), reverse=True
        )[:10]
        if not rows:
            await cb.answer("⏳ Данные ещё загружаются, подождите")
            return
        lines = ["💸 <b>Топ-10 по аномальному Funding Rate</b>" + NL]
        for sym, rate in rows:
            emoji = "🔴" if rate > 0.0005 else ("🟢" if rate < -0.0005 else "⚪")
            lines.append(f"{emoji} <b>{sym}</b>  {rate*100:+.4f}%")
        try:
            await cb.message.edit_text(NL.join(lines), parse_mode="HTML",
                                       reply_markup=_kb_back_pd())
        except Exception:
            pass
        await cb.answer()

    # ── Статус системы ────────────────────────────────────────────────────────
    @dp.callback_query(F.data == "pd_status")
    async def cb_pd_status(cb: CallbackQuery):
        runner = runner_getter()
        from pump_dump.ml_model import get_model
        ml     = get_model()
        NL     = "\n"
        if runner:
            n_syms  = len(runner.monitor.get_symbols())
            q_size  = runner.queue.qsize()
            running = runner.is_running()
        else:
            n_syms = q_size = 0
            running = False

        text = (
            "🔄 <b>Статус системы Памп/Дамп</b>" + NL + NL +
            f"WS монитор:   {'🟢 активен' if running else '🔴 остановлен'}" + NL +
            f"Монет:        <b>{n_syms}</b>" + NL +
            f"Очередь:      <b>{q_size}</b> событий" + NL +
            f"ML модель:    {'✅ готова, precision=' + f'{ml._precision:.2f}' if ml.is_ready() else '⏳ обучается (нет данных)'}" + NL
        )
        try:
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_kb_back_pd())
        except Exception:
            pass
        await cb.answer()
