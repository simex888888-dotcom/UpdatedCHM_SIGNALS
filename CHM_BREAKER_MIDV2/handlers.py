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
    status  = "🟢 АКТИВЕН" if user.active else "🔴 ОСТАНОВЛЕН"
    sub_em  = {"active": "✅", "trial": "🆓", "expired": "❌", "banned": "🚫"}.get(user.sub_status, "❓")
    sub_str = f"{sub_em} {user.sub_status.upper()} — осталось {user.time_left_str()}"
    filters = ", ".join(f for f, v in [
        ("RSI", user.use_rsi), ("Объём", user.use_volume),
        ("Паттерн", user.use_pattern), ("HTF", user.use_htf)] if v) or "все выкл"
    return (
        f"⚡ <b>CHM BREAKER BOT</b>\n\n"
        f"Статус:    <b>{status}</b>\n"
        f"Подписка:  <b>{sub_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Таймфрейм:     <b>{user.timeframe}</b>\n"
        f"🔄 Интервал:      <b>каждые {user.scan_interval // 60} мин.</b>\n"
        f"💰 Мин. объём:    <b>${user.min_volume_usdt:,.0f}</b>\n"
        f"⭐ Мин. качество: <b>{'⭐' * user.min_quality}</b>\n"
        f"🎯 Цели:          <b>{user.tp1_rr}R / {user.tp2_rr}R / {user.tp3_rr}R</b>\n"
        f"🔬 Фильтры:       <b>{filters}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📐 Пивоты: сила <b>{user.pivot_strength}</b> | возраст <b>{user.max_level_age}</b>\n"
        f"📉 EMA <b>{user.ema_fast}/{user.ema_slow}</b>  ATR <b>{user.atr_period}п x{user.atr_mult}</b>\n"
        f"🔁 Cooldown: <b>{user.cooldown_bars} свечей</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Сигналов получено: <b>{user.signals_received}</b>\n"
    )


def stats_text(user: UserSettings, stats: dict) -> str:
    name = f"@{user.username}" if user.username else "Трейдер"
    if not stats:
        return (
            f"📊 <b>Статистика — {name}</b>\n\n"
            f"Закрытых сделок пока нет.\n\n"
            f"После сигнала нажми кнопку результата:\n"
            f"<b>TP1 / TP2 / TP3 / SL</b>"
        )
    wr    = stats["winrate"]
    rr    = stats["avg_rr"]
    tot   = stats["total_rr"]
    sign  = "+" if tot >= 0 else ""
    wr_em = "🔥" if wr >= 70 else "✅" if wr >= 50 else "⚠️"
    rr_em = "💰" if rr > 1.0 else "⚖️" if rr > 0 else "📉"
    lw, lt = stats["longs_wins"], stats["longs_total"]
    sw, st = stats["shorts_wins"], stats["shorts_total"]
    lwr    = f"{lw/lt*100:.0f}%" if lt else "—"
    swr    = f"{sw/st*100:.0f}%" if st else "—"
    best   = "".join(
        f"  • {s}: {d['wins']}/{d['total']} ({d['wins']/d['total']*100:.0f}%)\n"
        for s, d in stats.get("best_symbols", [])
    )
    return (
        f"📊 <b>Статистика — {name}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Сделок: <b>{stats['total']}</b>  ✅ <b>{stats['wins']}</b>  ❌ <b>{stats['losses']}</b>\n"
        f"{wr_em} Винрейт:    <b>{wr:.1f}%</b>\n"
        f"{rr_em} Средний R:  <b>{rr:+.2f}R</b>\n"
        f"💼 Итого R:  <b>{sign}{tot:.2f}R</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP1: <b>{stats['tp1_cnt']}</b>  TP2: <b>{stats['tp2_cnt']}</b>  TP3: <b>{stats['tp3_cnt']}</b>\n"
        f"📈 Лонги:  <b>{lw}/{lt}</b> ({lwr})\n"
        f"📉 Шорты:  <b>{sw}/{st}</b> ({swr})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 Лучшая серия: <b>{stats['streak_w']}</b>  💔 Худшая: <b>{stats['streak_l']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>Лучшие монеты:</b>\n"
        f"{best or '  Нужно 2+ сделки по монете\n'}"
    )


def access_denied_text(reason: str) -> str:
    if reason == "banned":
        return "🚫 <b>Доступ заблокирован.</b>\n\nОбратись к администратору."
    return (
        "⏰ <b>Доступ истёк</b>\n\n"
        "Для продолжения оформи подписку — нажми кнопку ниже.\n"
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
        trial_note = ""
        if user.sub_status == "trial":
            trial_note = NL + NL + "🆓 Пробный период: осталось <b>" + user.time_left_str() + "</b>"

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
        user  = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        stats = await db.db_get_user_stats(user.user_id)
        await msg.answer(stats_text(user, stats), parse_mode="HTML", reply_markup=kb_back())

    @dp.message(Command("subscribe"))
    async def cmd_subscribe(msg: Message):
        await msg.answer(
            f"💳 <b>Подписка CHM BREAKER BOT</b>\n\n"
            f"📅 30 дней  — <b>{config.PRICE_30_DAYS}</b>\n"
            f"📅 90 дней  — <b>{config.PRICE_90_DAYS}</b>\n"
            f"📅 365 дней — <b>{config.PRICE_365_DAYS}</b>\n\n"
            f"После оплаты напиши: <b>{config.PAYMENT_INFO}</b>\n"
            f"Укажи свой Telegram ID: <code>{msg.from_user.id}</code>",
            parse_mode="HTML",
        )

    # ════════════════════════════════════════════════
    # АДМИН-КОМАНДЫ
    # ════════════════════════════════════════════════

    @dp.message(Command("admin"))
    async def cmd_admin(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        s   = await um.stats_summary()
        prf = scanner.get_perf()
        cs  = prf.get("cache", {})
        await msg.answer(
            f"👑 <b>Панель администратора</b>\n\n"
            f"👥 Всего:    <b>{s['total']}</b>\n"
            f"🆓 Триал:   <b>{s['trial']}</b>  ✅ Активных: <b>{s['active']}</b>\n"
            f"❌ Истекших: <b>{s['expired']}</b>  🚫 Забан: <b>{s['banned']}</b>\n"
            f"🔄 Сканируют: <b>{s['scanning']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Производительность:</b>\n"
            f"Циклов: <b>{prf['cycles']}</b>  Юзеров: <b>{prf['users']}</b>\n"
            f"Сигналов: <b>{prf['signals']}</b>  API calls: <b>{prf['api_calls']}</b>\n"
            f"Кэш: <b>{cs.get('size',0)}</b> ключей | хит <b>{cs.get('ratio',0)}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Команды:</b>\n"
            f"/give [id] [days] — выдать доступ\n"
            f"/revoke [id]      — отозвать\n"
            f"/ban [id]         — забанить\n"
            f"/unban [id]       — разбанить\n"
            f"/userinfo [id]    — инфо о юзере\n"
            f"/broadcast [текст]— рассылка",
            parse_mode="HTML",
        )

    @dp.message(Command("give"))
    async def cmd_give(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        parts = msg.text.split()
        if len(parts) < 3:
            await msg.answer("Использование: /give [user_id] [дней]\nПример: /give 123456789 30")
            return
        try:
            tid  = int(parts[1])
            days = int(parts[2])
        except ValueError:
            await msg.answer("❌ Неверный формат. Пример: /give 123456789 30")
            return
        user = await um.get(tid)
        if not user:
            await msg.answer(f"❌ Пользователь {tid} не найден в базе")
            return
        user.grant_access(days)
        await um.save(user)
        await msg.answer(
            f"✅ Доступ выдан!\n"
            f"👤 @{user.username or tid}\n"
            f"📅 +{days} дней\n"
            f"⏰ Осталось: {user.time_left_str()}"
        )
        try:
            await bot.send_message(
                tid,
                f"🎉 <b>Доступ открыт!</b>\n\n"
                f"Подписка активирована на <b>{days} дней</b>.\n"
                f"Осталось: <b>{user.time_left_str()}</b>\n\n"
                f"Нажми /menu чтобы начать.",
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
        user.sub_status  = "expired"
        user.sub_expires = 0
        user.active      = False
        await um.save(user)
        await msg.answer(f"✅ Доступ отозван у @{user.username or tid}")

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
        user.active     = False
        await um.save(user)
        await msg.answer(f"🚫 @{user.username or tid} заблокирован")

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
        await msg.answer(f"✅ @{user.username or tid} разблокирован")

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
        await msg.answer(
            f"👤 <b>@{user.username or '—'}</b> (<code>{user.user_id}</code>)\n"
            f"Подписка: <b>{user.sub_status.upper()}</b> | Осталось: <b>{user.time_left_str()}</b>\n"
            f"Сканер: {'🟢 вкл' if user.active else '🔴 выкл'}  TF: <b>{user.timeframe}</b>\n"
            f"Сигналов: <b>{user.signals_received}</b>\n"
            f"Сделок в БД: <b>{stats.get('total', 0)}</b>  "
            f"Винрейт: <b>{stats.get('winrate', 0):.1f}%</b>  "
            f"R: <b>{stats.get('total_rr', 0):+.2f}R</b>",
            parse_mode="HTML",
        )

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(msg: Message):
        if not is_admin(msg.from_user.id):
            return
        text = msg.text.replace("/broadcast", "", 1).strip()
        if not text:
            await msg.answer("Использование: /broadcast [текст]")
            return
        users  = await um.all_users()
        sent   = failed = 0
        for u in users:
            if u.sub_status in ("trial", "active"):
                try:
                    await bot.send_message(u.user_id, f"📢 {text}")
                    sent += 1
                    await asyncio.sleep(0.04)
                except Exception:
                    failed += 1
        await msg.answer(f"📢 Рассылка: ✅ {sent}  ❌ {failed}")

    # ════════════════════════════════════════════════
    # РЕЗУЛЬТАТЫ СДЕЛОК
    # ════════════════════════════════════════════════

    @dp.callback_query(F.data.startswith("res_"))
    async def trade_result(cb: CallbackQuery):
        parts    = cb.data.split("_", 2)
        result   = parts[1]
        trade_id = parts[2]

        # ✅ cb.answer() ВСЕГДА ПЕРВЫМ — до любых await с БД
        # Telegram даёт 10 секунд. Если сначала идти в БД — алерт не успевает.
        labels = {
            "TP1": "🎯 TP1 зафиксирован!",
            "TP2": "🎯 TP2 зафиксирован!",
            "TP3": "🏆 TP3 зафиксирован!",
            "SL":  "❌ Стоп-лосс зафиксирован",
            "SKIP":"⏭ Пропущено",
        }
        await cb.answer(labels.get(result, "✅ Записано"), show_alert=True)

        # Теперь спокойно работаем с БД
        trade = await db.db_get_trade(trade_id)
        if not trade:
            await cb.message.answer("⚠️ Сделка не найдена в базе.")
            return

        # Защита от двойного нажатия
        if trade.get("result") and trade["result"] not in ("", "SKIP"):
            await cb.message.answer(
                f"ℹ️ Результат уже записан: <b>{trade['result']}</b>",
                parse_mode="HTML"
            )
            return

        rr_map = {
            "TP1": trade["tp1_rr"], "TP2": trade["tp2_rr"],
            "TP3": trade["tp3_rr"], "SL": -1.0, "SKIP": 0.0,
        }
        await db.db_set_trade_result(trade_id, result, rr_map.get(result, 0.0))

        # Добавляем результат в текст сообщения + убираем кнопки
        emojis = {"TP1":"🎯 TP1","TP2":"🎯 TP2","TP3":"🏆 TP3","SL":"❌ SL","SKIP":"⏭ Пропущено"}
        rr_str = {
            "TP1": f"+{trade['tp1_rr']}R", "TP2": f"+{trade['tp2_rr']}R",
            "TP3": f"+{trade['tp3_rr']}R", "SL": "-1R", "SKIP": "",
        }
        result_line = f"\n\n<b>Результат: {emojis.get(result)}  {rr_str.get(result, '')}</b>"
        try:
            await cb.message.edit_text(
                (cb.message.text or "") + result_line,
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass

        # Отправляем обновлённую сводку
        if result != "SKIP":
            user  = await um.get_or_create(cb.from_user.id)
            stats = await db.db_get_user_stats(user.user_id)
            if stats:
                wr   = stats["winrate"]
                tot  = stats["total_rr"]
                sign = "+" if tot >= 0 else ""
                wr_em = "🔥" if wr >= 70 else "✅" if wr >= 50 else "⚠️"
                await cb.message.answer(
                    f"📊 <b>Счёт обновлён</b>\n\n"
                    f"Сделок: <b>{stats['total']}</b>  "
                    f"{wr_em} Винрейт: <b>{wr:.1f}%</b>\n"
                    f"Итого R: <b>{sign}{tot:.2f}R</b>\n\n"
                    f"Полная статистика → /stats",
                    parse_mode="HTML",
                )

    # ════════════════════════════════════════════════
    # МЕНЮ И НАСТРОЙКИ
    # Правило: cb.answer() первым, потом save + edit
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
        await cb.answer(f"✅ Таймфрейм: {user.timeframe}")
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
        await cb.answer(f"✅ Каждые {user.scan_interval // 60} мин.")
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
        await cb.answer(f"✅ Пивоты: {user.pivot_strength}")
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_age_"))
    async def set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_level_age = int(cb.data.replace("set_age_", ""))
        await cb.answer(f"✅ Возраст уровня: {user.max_level_age}")
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_retest_"))
    async def set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_retest_bars = int(cb.data.replace("set_retest_", ""))
        await cb.answer(f"✅ Ретест: {user.max_retest_bars} свечей")
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты и уровни S/R</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_buffer_"))
    async def set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.zone_buffer = float(cb.data.replace("set_buffer_", ""))
        await cb.answer(f"✅ Буфер зоны: x{user.zone_buffer}")
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
        await cb.answer(f"✅ EMA Fast: {user.ema_fast}")
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA тренд</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_slow_"))
    async def set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.ema_slow = int(cb.data.replace("set_ema_slow_", ""))
        await cb.answer(f"✅ EMA Slow: {user.ema_slow}")
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA тренд</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_htf_ema_"))
    async def set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.htf_ema_period = int(cb.data.replace("set_htf_ema_", ""))
        await cb.answer(f"✅ HTF EMA: {user.htf_ema_period}")
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
        await cb.answer(f"✅ RSI период: {user.rsi_period}")
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_ob_"))
    async def set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_ob = int(cb.data.replace("set_rsi_ob_", ""))
        await cb.answer(f"✅ RSI Overbought: {user.rsi_ob}")
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_os_"))
    async def set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_os = int(cb.data.replace("set_rsi_os_", ""))
        await cb.answer(f"✅ RSI Oversold: {user.rsi_os}")
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_vol_mult_"))
    async def set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.vol_mult = float(cb.data.replace("set_vol_mult_", ""))
        await cb.answer(f"✅ Объём: x{user.vol_mult}")
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры сигнала</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_vol_len_"))
    async def set_vol_len(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.vol_len = int(cb.data.replace("set_vol_len_", ""))
        await cb.answer(f"✅ Период объёма: {user.vol_len}")
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
        await cb.answer(f"✅ Мин. качество: {'⭐' * user.min_quality}")
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
        await cb.answer(f"✅ Cooldown: {user.cooldown_bars} свечей")
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
        await cb.answer(f"✅ ATR период: {user.atr_period}")
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс (ATR)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_mult_"))
    async def set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.atr_mult = float(cb.data.replace("set_atr_mult_", ""))
        await cb.answer(f"✅ ATR множитель: x{user.atr_mult}")
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп-лосс (ATR)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_risk_"))
    async def set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_risk_pct = float(cb.data.replace("set_risk_", ""))
        await cb.answer(f"✅ Макс. риск: {user.max_risk_pct}%")
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
            user.tp1_rr = round(float(msg.text.replace(",",".")), 1)
            await um.save(user)
            await state.clear()
            await msg.answer(f"✅ Цель 1 = <b>{user.tp1_rr}R</b>", parse_mode="HTML", reply_markup=kb_targets(user))
        except ValueError:
            await msg.answer("❌ Введи число, например: 0.8")

    @dp.message(EditState.waiting_tp2)
    async def save_tp2(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp2_rr = round(float(msg.text.replace(",",".")), 1)
            await um.save(user)
            await state.clear()
            await msg.answer(f"✅ Цель 2 = <b>{user.tp2_rr}R</b>", parse_mode="HTML", reply_markup=kb_targets(user))
        except ValueError:
            await msg.answer("❌ Введи число, например: 1.5")

    @dp.message(EditState.waiting_tp3)
    async def save_tp3(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp3_rr = round(float(msg.text.replace(",",".")), 1)
            await um.save(user)
            await state.clear()
            await msg.answer(f"✅ Цель 3 = <b>{user.tp3_rr}R</b>", parse_mode="HTML", reply_markup=kb_targets(user))
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
        await cb.answer(f"✅ Мин. объём: ${user.min_volume_usdt:,.0f}")
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
        user  = await um.get_or_create(cb.from_user.id)
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
