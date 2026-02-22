"""
handlers.py — все обработчики команд и кнопок
ВАЖНО: во ВСЕХ callback-обработчиках cb.answer() вызывается ПЕРВЫМ,
до любых await с БД или сетью. Telegram даёт только 10 сек на ответ.
"""

import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

import database as db
from user_manager import UserManager, UserSettings
from keyboards import (
    kb_main, kb_settings, kb_timeframes, kb_intervals,
    kb_pivots, kb_ema, kb_filters, kb_quality, kb_cooldown,
    kb_sl, kb_targets, kb_volume, kb_notify, kb_back,
    kb_subscribe,
)

log = logging.getLogger("CHM.Handlers")


async def safe_edit(cb: CallbackQuery, text: str = None, reply_markup=None):
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
    waiting_tp1 = State()
    waiting_tp2 = State()
    waiting_tp3 = State()


def settings_text(user: UserSettings) -> str:
    NL = "\n"
    status = "🟢 АКТИВЕН" if user.active else "🔴 ОСТАНОВЛЕН"
    sub_em = {"active": "✅", "trial": "🆓", "expired": "❌", "banned": "🚫"}.get(user.sub_status, "❓")
    sub_str = sub_em + " " + user.sub_status.upper() + " — осталось " + user.time_left_str()
    filters_list = ", ".join(
        f for f, v in [
            ("RSI", user.use_rsi),
            ("Объём", user.use_volume),
            ("Паттерн", user.use_pattern),
            ("HTF", user.use_htf),
        ] if v
    ) or "все выкл"
    quality_stars = "⭐" * user.min_quality
    interval_min = user.scan_interval // 60
    vol_fmt = "{:,.0f}".format(user.min_volume_usdt)

    lines = [
        "⚡ <b>CHM BREAKER BOT</b>",
        "",
        "Статус:    <b>" + status + "</b>",
        "Подписка:  <b>" + sub_str + "</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 Таймфрейм:     <b>" + user.timeframe + "</b>",
        "🔄 Интервал:      <b>каждые " + str(interval_min) + " мин.</b>",
        "💰 Мин. объём:    <b>$" + vol_fmt + "</b>",
        "⭐ Мин. качество: <b>" + quality_stars + "</b>",
        "🎯 Цели:          <b>" + str(user.tp1_rr) + "R / " + str(user.tp2_rr) + "R / " + str(user.tp3_rr) + "R</b>",
        "🔬 Фильтры:       <b>" + filters_list + "</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "📐 Пивоты: сила <b>" + str(user.pivot_strength) + "</b> | возраст <b>" + str(user.max_level_age) + "</b>",
        "📉 EMA <b>" + str(user.ema_fast) + "/" + str(user.ema_slow) + "</b>  ATR <b>" +
        str(user.atr_period) + "п x" + str(user.atr_mult) + "</b>",
        "🔁 Cooldown: <b>" + str(user.cooldown_bars) + " свечей</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "📈 Сигналов получено: <b>" + str(user.signals_received) + "</b>",
    ]
    return NL.join(lines)


def stats_text(user: UserSettings, stats: dict) -> str:
    NL = "\n"
    name = "@" + user.username if user.username else "Трейдер"
    if not stats:
        return (
            "📊 <b>Статистика — " + name + "</b>" + NL + NL +
            "Закрытых сделок пока нет." + NL + NL +
            "После сигнала нажми кнопку результата:" + NL +
            "<b>TP1 / TP2 / TP3 / SL</b>"
        )

    wr = stats["winrate"]
    rr = stats["avg_rr"]
    tot = stats["total_rr"]
    sign = "+" if tot >= 0 else ""
    wr_em = "🔥" if wr >= 70 else "✅" if wr >= 50 else "⚠️"
    rr_em = "💰" if rr > 1.0 else "⚖️" if rr > 0 else "📉"
    lw, lt = stats["longs_wins"], stats["longs_total"]
    sw, st = stats["shorts_wins"], stats["shorts_total"]
    lwr = (str(round(lw / lt * 100)) + "%") if lt else "—"
    swr = (str(round(sw / st * 100)) + "%") if st else "—"

"

    best = ""
    for s, d in stats.get("best_symbols", []):
        pct = round(d["wins"] / d["total"] * 100)
        best += "  • " + s + ": " + str(d["wins"]) + "/" + str(d["total"]) + " (" + str(pct) + "%)" + NL
    if not best:
        best = "  Нужно 2+ сделки по монете" + NL

    lines = [
        "📊 <b>Статистика — " + name + "</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📋 Сделок: <b>" + str(stats["total"]) + "</b>  ✅ <b>" + str(stats["wins"]) +
        "</b>  ❌ <b>" + str(stats["losses"]) + "</b>",
        wr_em + " Винрейт:    <b>" + "{:.1f}".format(wr) + "%</b>",
        rr_em + " Средний R:  <b>" + "{:+.2f}".format(rr) + "R</b>",
        "💼 Итого R:  <b>" + sign + "{:.2f}".format(tot) + "R</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "🎯 TP1: <b>" + str(stats["tp1_cnt"]) + "</b>  TP2: <b>" + str(stats["tp2_cnt"]) +
        "</b>  TP3: <b>" + str(stats["tp3_cnt"]) + "</b>",
        "📈 Лонги:  <b>" + str(lw) + "/" + str(lt) + "</b> (" + lwr + ")",
        "📉 Шорты:  <b>" + str(sw) + "/" + str(st) + "</b> (" + swr + ")",
        "━━━━━━━━━━━━━━━━━━━━",
        "🔥 Лучшая серия: <b>" + str(stats["streak_w"]) + "</b>  💔 Худшая: <b>" +
        str(stats["streak_l"]) + "</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "🏆 <b>Лучшие монеты:</b>",
        best,
    ]
    return NL.join(lines)


def access_denied_text(reason: str) -> str:
    NL = "\n"
    if reason == "banned":
        return "🚫 <b>Доступ заблокирован.</b>" + NL + NL + "Обратись к администратору."
    return (
        "⏰ <b>Доступ истёк</b>" + NL + NL +
        "Для продолжения оформи подписку — нажми кнопку ниже." + NL +
        "После оплаты напиши администратору — доступ откроют в течение нескольких минут."
    )


def register_handlers(dp: Dispatcher, bot: Bot, um: UserManager, scanner, config):

    is_admin = lambda uid: uid in config.ADMIN_IDS

    # ════════════════════════════════════════════════
    # КОМАНДЫ ПОЛЬЗОВАТЕЛЯ
    # ════════════════════════════════════════════════

    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(access_denied_text(reason), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return

        NL = "\n"
        if user.sub_status == "trial":
            trial_note = NL + NL + "🆓 Пробный период: осталось <b>" + user.time_left_str() + "</b>"
        else:
            trial_note = ""

        text = (
            "👋 Привет, <b>" + msg.from_user.first_name + "</b>!" + NL + NL +
            "⚡ <b>CHM BREAKER BOT</b> — by CHM Laboratory" + NL + NL +
            "Сканирую 200+ монет на OKX и шлю сигналы" + NL +
            "когда индикатор CHM BREAKER даёт вход." +
            trial_note + NL + NL +
            "Настрой и включи сканер 👇"
        )

        await msg.answer(text, parse_mode="HTML", reply_markup=kb_main(user))

    @dp.message(Command("menu"))
    async def cmd_menu(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(access_denied_text(reason), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return
        await msg.answer(settings_text(user), parse_mode="HTML", reply_markup=kb_main(user))

    @dp.message(Command("stop"))
    async def cmd_stop(msg: Message):
        user = await um.get_or_create(msg.from_user.id)
        user.active = False
        await um.save(user)
        await msg.answer("🔴 Сканер остановлен. /menu чтобы снова включить.")

    @dp.message(Command("stats"))
    async def cmd_stats(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        stats = await db.db_get_user_stats(user.user_id)
        await msg.answer(stats_text(user, stats), parse_mode="HTML", reply_markup=kb_back())

    @dp.message(Command("subscribe"))
    async def cmd_subscribe(msg: Message):
        NL = "\n"
        text = (
            "💳 <b>Подписка CHM BREAKER BOT</b>" + NL + NL +
            "📅 30 дней  — <b>" + str(config.PRICE_30_DAYS) + "</b>" + NL +
            "📅 90 дней  — <b>" + str(config.PRICE_90_DAYS) + "</b>" + NL +
            "📅 365 дней — <b>" + str(config.PRICE_365_DAYS) + "</b>" + NL + NL +
            "После оплаты напиши: <b>" + str(config.PAYMENT_INFO) + "</b>" + NL +
            "Укажи свой Telegram ID: <code>" + str(msg.from_user.id) + "</code>"
        )
        await msg.answer(text, parse_mode="HTML")

    # ════════════════════════════════════════════════
    # АДМИН-КОМАНДЫ
    # ════════════════════════════════════════════════

    @dp.message(Command("admin"))
    async def cmd_admin(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        s = await um.stats_summary()
        prf = scanner.get_perf()
        cs = prf.get("cache", {})
        NL = "\n"
        text = (
            "👑 <b>Панель администратора</b>" + NL + NL +
            "👥 Всего:    <b>" + str(s["total"]) + "</b>" + NL +
            "🆓 Триал:   <b>" + str(s["trial"]) + "</b>  ✅ Активных: <b>" + str(s["active"]) + "</b>" + NL +
            "❌ Истекших: <b>" + str(s["expired"]) + "</b>  🚫 Забан: <b>" + str(s["banned"]) + "</b>" + NL +
            "🔄 Сканируют: <b>" + str(s["scanning"]) + "</b>" + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "⚙️ <b>Производительность:</b>" + NL +
            "Циклов: <b>" + str(prf["cycles"]) + "</b>  Юзеров: <b>" + str(prf["users"]) + "</b>" + NL +
            "Сигналов: <b>" + str(prf["signals"]) + "</b>  API calls: <b>" + str(prf["api_calls"]) + "</b>" + NL +
            "Кэш: <b>" + str(cs.get("size", 0)) + "</b> ключей | хит <b>" + str(cs.get("ratio", 0)) + "%</b>" + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "<b>Команды:</b>" + NL +
            "/give [id] [days] — выдать доступ" + NL +
            "/revoke [id]      — отозвать" + NL +
            "/ban [id]         — забанить" + NL +
            "/unban [id]       — разбанить" + NL +
            "/userinfo [id]    — инфо о юзере" + NL +
            "/broadcast [текст]— рассылка"
        )
        await msg.answer(text, parse_mode="HTML")

    @dp.message(Command("give"))
    async def cmd_give(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        parts = msg.text.split()
        if len(parts) < 3:
            await msg.answer("Использование: /give [user_id] [дней]\nПример: /give 123456789 30")
            return
        try:
            tid = int(parts[1])
            days = int(parts[2])
        except ValueError:
            await msg.answer("❌ Неверный формат. Пример: /give 123456789 30")
            return
        user = await um.get(tid)
        if not user:
            await msg.answer("❌ Пользователь " + str(tid) + " не найден в базе")
            return
        user.grant_access(days)
        await um.save(user)
        NL = "\n"
        time_left = user.time_left_str()
        uname = user.username or str(tid)
        await msg.answer(
            "✅ Доступ выдан!" + NL +
            "👤 @" + uname + NL +
            "📅 +" + str(days) + " дней" + NL +
            "⏰ Осталось: " + time_left
        )
        try:
            await bot.send_message(
                tid,
                "🎉 <b>Доступ открыт!</b>" + NL + NL +
                "Подписка активирована на <b>" + str(days) + " дней</b>." + NL +
                "Осталось: <b>" + time_left + "</b>" + NL + NL +
                "Нажми /menu чтобы начать.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    @dp.message(Command("revoke"))
    async def cmd_revoke(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        parts = msg.text.split()
        if len(parts) < 2:
            await msg.answer("Использование: /revoke [user_id]")
            return
        try:
            tid = int(parts[1])
        except ValueError:
            return
        user = await um.get(tid)
        if not user:
            await msg.answer("❌ Не найден")
            return
        user.sub_status = "expired"
        user.sub_expires = 0
        user.active = False
        await um.save(user)
        uname = user.username or str(tid)
        await msg.answer("✅ Доступ отозван у @" + uname)

    @dp.message(Command("ban"))
    async def cmd_ban(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        parts = msg.text.split()
        if len(parts) < 2:
            await msg.answer("Использование: /ban [user_id]")
            return
        try:
            tid = int(parts[1])
        except ValueError:
            return
        user = await um.get(tid)
        if not user:
            await msg.answer("❌ Не найден")
            return
        user.sub_status = "banned"
        user.active = False
        await um.save(user)
        uname = user.username or str(tid)
        await msg.answer("🚫 @" + uname + " заблокирован")

    @dp.message(Command("unban"))
    async def cmd_unban(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        parts = msg.text.split()
        if len(parts) < 2:
            await msg.answer("Использование: /unban [user_id]")
            return
        try:
            tid = int(parts[1])
        except ValueError:
            return
        user = await um.get(tid)
        if not user:
            await msg.answer("❌ Не найден")
            return
        user.sub_status = "expired"
        await um.save(user)
        uname = user.username or str(tid)
        await msg.answer("✅ @" + uname + " разблокирован")

    @dp.message(Command("userinfo"))
    async def cmd_userinfo(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        parts = msg.text.split()
        if len(parts) < 2:
            await msg.answer("Использование: /userinfo [user_id]")
            return
        try:
            tid = int(parts[1])
        except ValueError:
            return
        user = await um.get(tid)
        if not user:
            await msg.answer("❌ Не найден")
            return
        stats = await db.db_get_user_stats(tid)
        NL = "\n"
        uname = user.username or "—"
        winrate = stats.get("winrate", 0)
        total_rr = stats.get("total_rr", 0)
        text = (
            "👤 <b>@" + uname + "</b> (<code>" + str(user.user_id) + "</code>)" + NL +
            "Подписка: <b>" + user.sub_status.upper() + "</b> | Осталось: <b>" + user.time_left_str() + "</b>" + NL +
            "Сканер: " + ("🟢 вкл" if user.active else "🔴 выкл") + "  TF: <b>" + user.timeframe + "</b>" + NL +
            "Сигналов: <b>" + str(user.signals_received) + "</b>" + NL +
            "Сделок в БД: <b>" + str(stats.get("total", 0)) + "</b>  " +
            "Винрейт: <b>" + "{:.1f}".format(winrate) + "%</b>  " +
            "R: <b>" + "{:+.2f}".format(total_rr) + "R</b>"
        )
        await msg.answer(text, parse_mode="HTML")

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        text = msg.text.replace("/broadcast", "", 1).strip()
        if not text:
            await msg.answer("Использование: /broadcast [текст]")
            return
        users = await um.all_users()
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

    # ════════════════════════════════════════════════
    # РЕЗУЛЬТАТЫ СДЕЛОК
    # ════════════════════════════════════════════════

    @dp.callback_query(F.data.startswith("res_"))
    async def trade_result(cb: CallbackQuery):
        parts = cb.data.split("_", 2)
        result = parts[1]
        trade_id = parts[2]

        labels = {
            "TP1": "🎯 TP1 зафиксирован!",
            "TP2": "🎯 TP2 зафиксирован!",
            "TP3": "🏆 TP3 зафиксирован!",
            "SL": "❌ Стоп-лосс зафиксирован",
            "SKIP": "⏭ Пропущено",
        }
        await cb.answer(labels.get(result, "✅ Записано"), show_alert=True)

        trade = await db.db_get_trade(trade_id)
        if not trade:
            await cb.message.answer("⚠️ Сделка не найдена в базе.")
            return

        if trade.get("result") and trade["result"] not in ("", "SKIP"):
            await cb.message.answer(
                "ℹ️ Результат уже записан: <b>" + trade["result"] + "</b>",
                parse_mode="HTML",
            )
            return

        rr_map = {
            "TP1": trade["tp1_rr"],
            "TP2": trade["tp2_rr"],
            "TP3": trade["tp3_rr"],
            "SL": -1.0,
            "SKIP": 0.0,
        }
        await db.db_set_trade_result(trade_id, result, rr_map.get(result, 0.0))

        emojis = {"TP1": "🎯 TP1", "TP2": "🎯 TP2", "TP3": "🏆 TP3", "SL": "❌ SL", "SKIP": "⏭ Пропущено"}
        rr_str = {
            "TP1": "+" + str(trade["tp1_rr"]) + "R",
            "TP2": "+" + str(trade["tp2_rr"]) + "R",
            "TP3": "+" + str(trade["tp3_rr"]) + "R",
            "SL": "-1R",
            "SKIP": "",
        }
        NL = "\n"
        result_line = NL + NL + "<b>Результат: " + emojis.get(result, "") + "  " + rr_str.get(result, "") + "</b>"
        try:
            await cb.message.edit_text(
                (cb.message.text or "") + result_line,
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass

        if result != "SKIP":
            user = await um.get_or_create(cb.from_user.id)
            stats = await db.db_get_user_stats(user.user_id)
            if stats:
                wr = stats["winrate"]
                tot = stats["total_rr"]
                sign = "+" if tot >= 0 else ""
                wr_em = "🔥" if wr >= 70 else "✅" if wr >= 50 else "⚠️"
                text = (
                    "📊 <b>Счёт обновлён</b>" + NL + NL +
                    "Сделок: <b>" + str(stats["total"]) + "</b>  " +
                    wr_em + " Винрейт: <b>" + "{:.1f}".format(wr) + "%</b>" + NL +
                    "Итого R: <b>" + sign + "{:.2f}".format(tot) + "R</b>" + NL + NL +
                    "Полная статистика → /stats"
                )
                await cb.message.answer(text, parse_mode="HTML")

    # ════════════════════════════════════════════════
    # МЕНЮ И НАСТРОЙКИ
    # ════════════════════════════════════════════════

    @dp.callback_query(F.data == "toggle_active")
    async def toggle_active(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config))
            return
        user.active = not user.active
        await cb.answer("🟢 Сканер включён!" if user.active else "🔴 Сканер выключен.")
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "menu_tf")
    async def menu_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📊 <b>Таймфрейм свечей</b>\n\nЧем меньше — тем больше сигналов.", kb_timeframes(user.timeframe))

    @dp.callback_query(F.data.startswith("set_tf_"))
    async def set_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.timeframe = cb.data.replace("set_tf_", "")
        await cb.answer("✅ Таймфрейм: " + user.timeframe)
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "menu_interval")
    async def menu_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔄 <b>Интервал сканирования</b>", kb_intervals(user.scan_interval))

    @dp.callback_query(F.data.startswith("set_interval_"))
    async def set_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.scan_interval = int(cb.data.replace("set_interval_", ""))
        await cb.answer("✅ Каждые " + str(user.scan_interval // 60) + " мин.")
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "menu_settings")
    async def menu_settings(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, "⚙️ <b>Все настройки сигнала</b>", kb_settings())

    @dp.callback_query(F.data == "menu_pivots")
    async def menu_pivots(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_pivot_"))
    async def set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.pivot_strength = int(cb.data.replace("set_pivot_", ""))
        await cb.answer("✅ Пивоты: " + str(user.pivot_strength))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_age_"))
    async def set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_level_age = int(cb.data.replace("set_age_", ""))
        await cb.answer("✅ Возраст уровня: " + str(user.max_level_age))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_retest_"))
    async def set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_retest_bars = int(cb.data.replace("set_retest_", ""))
        await cb.answer("✅ Ретест: " + str(user.max_retest_bars) + " свечей")
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_buffer_"))
    async def set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.zone_buffer = float(cb.data.replace("set_buffer_", ""))
        await cb.answer("✅ Буфер зоны: x" + str(user.zone_buffer))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data == "menu_ema")
    async def menu_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>EMA тренд</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_fast_"))
    async def set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.ema_fast = int(cb.data.replace("set_ema_fast_", ""))
        await cb.answer("✅ EMA Fast: " + str(user.ema_fast))
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA тренд</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_slow_"))
    async def set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.ema_slow = int(cb.data.replace("set_ema_slow_", ""))
        await cb.answer("✅ EMA Slow: " + str(user.ema_slow))
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA тренд</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_htf_ema_"))
    async def set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.htf_ema_period = int(cb.data.replace("set_htf_ema_", ""))
        await cb.answer("✅ HTF EMA: " + str(user.htf_ema_period))
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA тренд</b>", kb_ema(user))

    @dp.callback_query(F.data == "menu_filters")
    async def menu_filters(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_rsi")
    async def toggle_rsi(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_rsi = not user.use_rsi
        await cb.answer("RSI " + ("✅ включён" if user.use_rsi else "❌ выключен"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_volume")
    async def toggle_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_volume = not user.use_volume
        await cb.answer("Объём " + ("✅ включён" if user.use_volume else "❌ выключен"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_pattern")
    async def toggle_pattern(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_pattern = not user.use_pattern
        await cb.answer("Паттерны " + ("✅ включены" if user.use_pattern else "❌ выключены"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_htf")
    async def toggle_htf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_htf = not user.use_htf
        await cb.answer("HTF " + ("✅ включён" if user.use_htf else "❌ выключен"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_period_"))
    async def set_rsi_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_period = int(cb.data.replace("set_rsi_period_", ""))
        await cb.answer("✅ RSI период: " + str(user.rsi_period))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_ob_"))
    async def set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_ob = int(cb.data.replace("set_rsi_ob_", ""))
        await cb.answer("✅ RSI Overbought: " + str(user.rsi_ob))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_os_"))
    async def set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_os = int(cb.data.replace("set_rsi_os_", ""))
        await cb.answer("✅ RSI Oversold: " + str(user.rsi_os))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_vol_mult_"))
    async def set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.vol_mult = float(cb.data.replace("set_vol_mult_", ""))
        await cb.answer("✅ Объём: x" + str(user.vol_mult))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_vol_len_"))
    async def set_vol_len(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.vol_len = int(cb.data.replace("set_vol_len_", ""))
        await cb.answer("✅ Период объёма: " + str(user.vol_len))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data == "menu_quality")
    async def menu_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Качество сигнала</b>", kb_quality(user.min_quality))

    @dp.callback_query(F.data.startswith("set_quality_"))
    async def set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.min_quality = int(cb.data.replace("set_quality_", ""))
        await cb.answer("✅ Мин. качество: " + ("⭐" * user.min_quality))
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "menu_cooldown")
    async def menu_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔁 <b>Cooldown между сигналами</b>", kb_cooldown(user.cooldown_bars))

    @dp.callback_query(F.data.startswith("set_cooldown_"))
    async def set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.cooldown_bars = int(cb.data.replace("set_cooldown_", ""))
        await cb.answer("✅ Cooldown: " + str(user.cooldown_bars) + " свечей")
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "menu_sl")
    async def menu_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Стоп-лосс (ATR)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_period_"))
    async def set_atr_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.atr_period = int(cb.data.replace("set_atr_period_", ""))
        await cb.answer("✅ ATR период: " + str(user.atr_period))
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс (ATR)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_mult_"))
    async def set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.atr_mult = float(cb.data.replace("set_atr_mult_", ""))
        await cb.answer("✅ ATR множитель: x" + str(user.atr_mult))
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс (ATR)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_risk_"))
    async def set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_risk_pct = float(cb.data.replace("set_risk_", ""))
        await cb.answer("✅ Макс. риск: " + str(user.max_risk_pct) + "%")
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс (ATR)</b>", kb_sl(user))

    @dp.callback_query(F.data == "menu_targets")
    async def menu_targets(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Цели Take Profit</b>\n\n1R = расстояние от входа до стопа.", kb_targets(user))

    @dp.callback_query(F.data == "edit_tp1")
    async def edit_tp1(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.waiting_tp1)
        await cb.message.answer("Введи Цель 1 (например: <b>0.8</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_tp2")
    async def edit_tp2(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.waiting_tp2)
        await cb.message.answer("Введи Цель 2 (например: <b>1.5</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_tp3")
    async def edit_tp3(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.waiting_tp3)
        await cb.message.answer("Введи Цель 3 (например: <b>2.5</b>):", parse_mode="HTML")

    @dp.message(EditState.waiting_tp1)
    async def save_tp1(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp1_rr = round(float(msg.text.replace(",", ".")), 1)
            await um.save(user)
            await state.clear()
            await msg.answer(
                "✅ Цель 1 = <b>" + str(user.tp1_rr) + "R</b>",
                parse_mode="HTML",
                reply_markup=kb_targets(user),
            )
        except ValueError:
            await msg.answer("❌ Введи число, например: 0.8")

    @dp.message(EditState.waiting_tp2)
    async def save_tp2(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp2_rr = round(float(msg.text.replace(",", ".")), 1)
            await um.save(user)
            await state.clear()
            await msg.answer(
                "✅ Цель 2 = <b>" + str(user.tp2_rr) + "R</b>",
                parse_mode="HTML",
                reply_markup=kb_targets(user),
            )
        except ValueError:
            await msg.answer("❌ Введи число, например: 1.5")

    @dp.message(EditState.waiting_tp3)
    async def save_tp3(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp3_rr = round(float(msg.text.replace(",", ".")), 1)
            await um.save(user)
            await state.clear()
            await msg.answer(
                "✅ Цель 3 = <b>" + str(user.tp3_rr) + "R</b>",
                parse_mode="HTML",
                reply_markup=kb_targets(user),
            )
        except ValueError:
            await msg.answer("❌ Введи число, например: 2.5")

    @dp.callback_query(F.data == "menu_volume")
    async def menu_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Фильтр монет по суточному объёму</b>", kb_volume(user.min_volume_usdt))

    @dp.callback_query(F.data.startswith("set_volume_"))
    async def set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.min_volume_usdt = float(cb.data.replace("set_volume_", ""))
        vol_fmt = "{:,.0f}".format(user.min_volume_usdt)
        await cb.answer("✅ Мин. объём: $" + vol_fmt)
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "menu_notify")
    async def menu_notify(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📱 <b>Уведомления</b>", kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify_signal")
    async def toggle_notify_signal(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.notify_signal = not user.notify_signal
        await cb.answer("Сигналы " + ("✅ включены" if user.notify_signal else "❌ выключены"))
        await um.save(user)
        await safe_edit(cb, "📱 <b>Уведомления</b>", kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify_breakout")
    async def toggle_notify_breakout(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.notify_breakout = not user.notify_breakout
        await cb.answer("Пробои " + ("✅ включены" if user.notify_breakout else "❌ выключены"))
        await um.save(user)
        await safe_edit(cb, "📱 <b>Уведомления</b>", kb_notify(user))

    @dp.callback_query(F.data == "my_stats")
    async def my_stats(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        stats = await db.db_get_user_stats(user.user_id)
        await safe_edit(cb, stats_text(user, stats), kb_back())

    @dp.callback_query(F.data == "back_main")
    async def back_main(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        await safe_edit(cb, settings_text(user), kb_main(user))

    @dp.callback_query(F.data == "noop")
    async def noop(cb: CallbackQuery):
        await cb.answer()
