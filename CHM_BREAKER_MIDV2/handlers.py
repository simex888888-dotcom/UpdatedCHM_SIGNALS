"""
handlers.py v4 — мультисканнинг ЛОНГ + ШОРТ + ОБА
Правило: cb.answer() ВСЕГДА первым, до любых await с БД.
"""

import asyncio
import logging
from dataclasses import fields
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
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
    kb_long_settings, kb_short_settings,
    kb_pivots, kb_long_pivots, kb_short_pivots,
    kb_ema, kb_long_ema, kb_short_ema,
    kb_filters, kb_long_filters, kb_short_filters,
    kb_quality, kb_long_quality, kb_short_quality,
    kb_cooldown, kb_long_cooldown, kb_short_cooldown,
    kb_sl, kb_long_sl, kb_short_sl,
    kb_targets, kb_long_targets, kb_short_targets,
    kb_volume, kb_long_volume, kb_short_volume,
    trend_text,
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
            if "not modified" in str(e): return
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
        return "🚫 <b>Доступ заблокирован.</b>" + NL + NL + "Обратись к администратору."
    return "⏰ <b>Доступ истёк</b>" + NL + NL + "Оформи подписку чтобы продолжить."


# ── Хелперы для обновления cfg направления ──────────

def _update_long_field(user: UserSettings, field: str, value):
    cfg = TradeCfg.from_json(user.long_cfg)
    setattr(cfg, field, value)
    user.long_cfg = cfg.to_json()

def _update_short_field(user: UserSettings, field: str, value):
    cfg = TradeCfg.from_json(user.short_cfg)
    setattr(cfg, field, value)
    user.short_cfg = cfg.to_json()


# ── Регистрация хендлеров ────────────────────────────

def register_handlers(dp: Dispatcher, bot: Bot, um: UserManager, scanner, config):

    is_admin = lambda uid: uid in config.ADMIN_IDS

    # ─── КОМАНДЫ ──────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(access_denied_text(reason), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return
        trend = scanner.get_trend()
        await msg.answer(main_text(user, trend), parse_mode="HTML", reply_markup=kb_main(user))

    @dp.message(Command("menu"))
    async def cmd_menu(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, reason = user.check_access()
        if not has:
            await msg.answer(access_denied_text(reason), parse_mode="HTML", reply_markup=kb_subscribe(config))
            return
        trend = scanner.get_trend()
        await msg.answer(main_text(user, trend), parse_mode="HTML", reply_markup=kb_main(user))

    @dp.message(Command("stop"))
    async def cmd_stop(msg: Message):
        user = await um.get_or_create(msg.from_user.id)
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
            "💳 <b>Подписка CHM BREAKER BOT</b>" + NL + NL +
            "📅 30 дней  — <b>" + config.PRICE_30_DAYS + "</b>" + NL +
            "📅 90 дней  — <b>" + config.PRICE_90_DAYS + "</b>" + NL +
            "📅 365 дней — <b>" + config.PRICE_365_DAYS + "</b>" + NL + NL +
            "После оплаты: <b>" + config.PAYMENT_INFO + "</b>" + NL +
            "Твой Telegram ID: <code>" + str(msg.from_user.id) + "</code>",
            parse_mode="HTML",
        )

    # ─── АДМИН ────────────────────────────────────────

    @dp.message(Command("admin"))
    async def cmd_admin(msg: Message):
        if not is_admin(msg.from_user.id): return
        s   = await um.stats_summary()
        prf = scanner.get_perf()
        cs  = prf.get("cache", {})
        NL  = "\n"
        await msg.answer(
            "👑 <b>Панель администратора</b>" + NL + NL +
            "👥 Всего: <b>" + str(s["total"]) + "</b>  🆓 Триал: <b>" + str(s["trial"]) + "</b>  ✅ Активных: <b>" + str(s["active"]) + "</b>" + NL +
            "🔄 Сканируют: <b>" + str(s["scanning"]) + "</b>" + NL +
            "━━━━━━━━━━━━━━━━━━━━" + NL +
            "Циклов: <b>" + str(prf["cycles"]) + "</b>  Сигналов: <b>" + str(prf["signals"]) + "</b>  API: <b>" + str(prf["api_calls"]) + "</b>" + NL +
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
        user  = await um.get(tid)
        if not user: await msg.answer("❌ Не найден"); return
        stats = await db.db_get_user_stats(tid)
        NL    = "\n"
        await msg.answer(
            "👤 <b>@" + str(user.username or "—") + "</b> (<code>" + str(user.user_id) + "</code>)" + NL +
            "Подписка: <b>" + user.sub_status.upper() + "</b> | Осталось: <b>" + user.time_left_str() + "</b>" + NL +
            "ЛОНГ: " + ("🟢" if user.long_active else "⚫") +
            "  ШОРТ: " + ("🟢" if user.short_active else "⚫") +
            "  ОБА: " + ("🟢" if user.active else "⚫") + NL +
            "Сигналов: <b>" + str(user.signals_received) + "</b>  Сделок: <b>" + str(stats.get("total",0)) + "</b>  R: <b>" + "{:+.2f}".format(stats.get("total_rr",0)) + "R</b>",
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

        emojis = {"TP1":"🎯 TP1","TP2":"🎯 TP2","TP3":"🏆 TP3","SL":"❌ SL","SKIP":"⏭ Пропущено"}
        rr_str = {"TP1":"+"+str(trade["tp1_rr"])+"R","TP2":"+"+str(trade["tp2_rr"])+"R","TP3":"+"+str(trade["tp3_rr"])+"R","SL":"-1R","SKIP":""}
        try:
            await cb.message.edit_text(
                (cb.message.text or "") + NL + NL + "<b>Результат: " + emojis.get(result,"") + "  " + rr_str.get(result,"") + "</b>",
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

    # ─── НАВИГАЦИЯ (главное меню) ─────────────────────

    @dp.callback_query(F.data == "back_main")
    async def back_main(cb: CallbackQuery):
        await cb.answer()
        user  = await um.get_or_create(cb.from_user.id)
        trend = scanner.get_trend()
        await safe_edit(cb, main_text(user, trend), kb_main(user))

    @dp.callback_query(F.data == "my_stats")
    async def my_stats(cb: CallbackQuery):
        await cb.answer()
        user  = await um.get_or_create(cb.from_user.id)
        stats = await db.db_get_user_stats(user.user_id)
        await safe_edit(cb, stats_text(user, stats), kb_back())

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
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config))
            return
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
        cfg = user.get_long_cfg()
        await safe_edit(cb, cfg_text(cfg, "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

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
        cfg = user.get_long_cfg()
        await safe_edit(cb, cfg_text(cfg, "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

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
        await safe_edit(cb, "💰 <b>Объём монет ЛОНГ</b>", kb_long_volume(user))

    @dp.callback_query(F.data == "reset_long_cfg")
    async def reset_long_cfg(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer("✅ Настройки ЛОНГ сброшены к общим")
        user.long_cfg = "{}"
        await um.save(user)
        cfg = user.get_long_cfg()
        await safe_edit(cb, cfg_text(cfg, "📈 <b>ЛОНГ сканер</b>"), kb_mode_long(user))

    # ЛОНГ — сеттеры (пивоты, EMA, фильтры, SL и т.д.)
    @dp.callback_query(F.data.startswith("long_set_pivot_"))
    async def long_set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_pivot_", ""))
        await cb.answer("✅ Пивоты ЛОНГ: " + str(v))
        _update_long_field(user, "pivot_strength", v)
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_age_"))
    async def long_set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_age_", ""))
        await cb.answer("✅ Возраст ЛОНГ: " + str(v))
        _update_long_field(user, "max_level_age", v)
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_retest_"))
    async def long_set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_retest_", ""))
        await cb.answer("✅ Ретест ЛОНГ: " + str(v))
        _update_long_field(user, "max_retest_bars", v)
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_buffer_"))
    async def long_set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_buffer_", ""))
        await cb.answer("✅ Буфер ЛОНГ: x" + str(v))
        _update_long_field(user, "zone_buffer", v)
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ЛОНГ</b>", kb_long_pivots(user))

    @dp.callback_query(F.data.startswith("long_set_ema_fast_"))
    async def long_set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_ema_fast_", ""))
        await cb.answer("✅ EMA Fast ЛОНГ: " + str(v))
        _update_long_field(user, "ema_fast", v)
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ЛОНГ</b>", kb_long_ema(user))

    @dp.callback_query(F.data.startswith("long_set_ema_slow_"))
    async def long_set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_ema_slow_", ""))
        await cb.answer("✅ EMA Slow ЛОНГ: " + str(v))
        _update_long_field(user, "ema_slow", v)
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ЛОНГ</b>", kb_long_ema(user))

    @dp.callback_query(F.data.startswith("long_set_htf_ema_"))
    async def long_set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_htf_ema_", ""))
        await cb.answer("✅ HTF EMA ЛОНГ: " + str(v))
        _update_long_field(user, "htf_ema_period", v)
        await um.save(user)
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
        await cb.answer("✅ RSI период ЛОНГ: " + str(v))
        _update_long_field(user, "rsi_period", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_rsi_ob_"))
    async def long_set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_rsi_ob_", ""))
        await cb.answer("✅ RSI OB ЛОНГ: " + str(v))
        _update_long_field(user, "rsi_ob", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_rsi_os_"))
    async def long_set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_rsi_os_", ""))
        await cb.answer("✅ RSI OS ЛОНГ: " + str(v))
        _update_long_field(user, "rsi_os", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_vol_mult_"))
    async def long_set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_vol_mult_", ""))
        await cb.answer("✅ Объём ЛОНГ: x" + str(v))
        _update_long_field(user, "vol_mult", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ЛОНГ</b>", kb_long_filters(user))

    @dp.callback_query(F.data.startswith("long_set_quality_"))
    async def long_set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_quality_", ""))
        await cb.answer("✅ Качество ЛОНГ: " + str(v))
        _update_long_field(user, "min_quality", v); await um.save(user)
        await safe_edit(cb, "⭐ <b>Качество ЛОНГ</b>", kb_long_quality(user))

    @dp.callback_query(F.data.startswith("long_set_cooldown_"))
    async def long_set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_cooldown_", ""))
        await cb.answer("✅ Cooldown ЛОНГ: " + str(v))
        _update_long_field(user, "cooldown_bars", v); await um.save(user)
        await safe_edit(cb, "🔁 <b>Cooldown ЛОНГ</b>", kb_long_cooldown(user))

    @dp.callback_query(F.data.startswith("long_set_atr_period_"))
    async def long_set_atr_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("long_set_atr_period_", ""))
        await cb.answer("✅ ATR ЛОНГ: " + str(v))
        _update_long_field(user, "atr_period", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data.startswith("long_set_atr_mult_"))
    async def long_set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_atr_mult_", ""))
        await cb.answer("✅ ATR mult ЛОНГ: x" + str(v))
        _update_long_field(user, "atr_mult", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data.startswith("long_set_risk_"))
    async def long_set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_risk_", ""))
        await cb.answer("✅ Риск ЛОНГ: " + str(v) + "%")
        _update_long_field(user, "max_risk_pct", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ЛОНГ</b>", kb_long_sl(user))

    @dp.callback_query(F.data.startswith("long_set_volume_"))
    async def long_set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("long_set_volume_", ""))
        await cb.answer("✅ Объём ЛОНГ: $" + str(int(v)))
        _update_long_field(user, "min_volume_usdt", v); await um.save(user)
        await safe_edit(cb, "💰 <b>Объём ЛОНГ</b>", kb_long_volume(user))

    # ─── РЕЖИМ ШОРТ ───────────────────────────────────

    @dp.callback_query(F.data == "mode_short")
    async def mode_short(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        cfg  = user.get_short_cfg()
        await safe_edit(cb, cfg_text(cfg, "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    @dp.callback_query(F.data == "toggle_short")
    async def toggle_short(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        has, reason = user.check_access()
        if not has:
            await cb.answer("Подписка истекла!", show_alert=True)
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config))
            return
        user.short_active = not user.short_active
        await cb.answer("🟢 ШОРТ включён!" if user.short_active else "🔴 ШОРТ выключен.")
        await um.save(user)
        cfg = user.get_short_cfg()
        await safe_edit(cb, cfg_text(cfg, "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    @dp.callback_query(F.data == "menu_short_tf")
    async def menu_short_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📊 <b>Таймфрейм ШОРТ</b>", kb_short_timeframes(user.short_tf))

    @dp.callback_query(F.data.startswith("set_short_tf_"))
    async def set_short_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.short_tf = cb.data.replace("set_short_tf_", "")
        await cb.answer("✅ ШОРТ ТФ: " + user.short_tf)
        await um.save(user)
        cfg = user.get_short_cfg()
        await safe_edit(cb, cfg_text(cfg, "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    @dp.callback_query(F.data == "menu_short_interval")
    async def menu_short_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔄 <b>Интервал ШОРТ</b>", kb_short_intervals(user.short_interval))

    @dp.callback_query(F.data.startswith("set_short_interval_"))
    async def set_short_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.short_interval = int(cb.data.replace("set_short_interval_", ""))
        await cb.answer("✅ Каждые " + str(user.short_interval//60) + " мин.")
        await um.save(user)
        cfg = user.get_short_cfg()
        await safe_edit(cb, cfg_text(cfg, "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    @dp.callback_query(F.data == "menu_short_settings")
    async def menu_short_settings(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, "⚙️ <b>Настройки ШОРТ</b>", kb_short_settings())

    @dp.callback_query(F.data == "menu_short_pivots")
    async def menu_short_pivots(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data == "menu_short_ema")
    async def menu_short_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data == "menu_short_filters")
    async def menu_short_filters(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "menu_short_quality")
    async def menu_short_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Качество ШОРТ</b>", kb_short_quality(user))

    @dp.callback_query(F.data == "menu_short_cooldown")
    async def menu_short_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔁 <b>Cooldown ШОРТ</b>", kb_short_cooldown(user))

    @dp.callback_query(F.data == "menu_short_sl")
    async def menu_short_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data == "menu_short_targets")
    async def menu_short_targets(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Цели ШОРТ</b>", kb_short_targets(user))

    @dp.callback_query(F.data == "menu_short_volume")
    async def menu_short_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Объём ШОРТ</b>", kb_short_volume(user))

    @dp.callback_query(F.data == "reset_short_cfg")
    async def reset_short_cfg(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer("✅ Настройки ШОРТ сброшены к общим")
        user.short_cfg = "{}"
        await um.save(user)
        cfg = user.get_short_cfg()
        await safe_edit(cb, cfg_text(cfg, "📉 <b>ШОРТ сканер</b>"), kb_mode_short(user))

    # ШОРТ — сеттеры (аналогично ЛОНГ)
    @dp.callback_query(F.data.startswith("short_set_pivot_"))
    async def short_set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_pivot_", ""))
        await cb.answer("✅ Пивоты ШОРТ: " + str(v))
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
        _update_short_field(user, "max_retest_bars", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_buffer_"))
    async def short_set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_buffer_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "zone_buffer", v); await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты ШОРТ</b>", kb_short_pivots(user))

    @dp.callback_query(F.data.startswith("short_set_ema_fast_"))
    async def short_set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_ema_fast_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "ema_fast", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data.startswith("short_set_ema_slow_"))
    async def short_set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_ema_slow_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "ema_slow", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data.startswith("short_set_htf_ema_"))
    async def short_set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_htf_ema_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "htf_ema_period", v); await um.save(user)
        await safe_edit(cb, "📉 <b>EMA ШОРТ</b>", kb_short_ema(user))

    @dp.callback_query(F.data == "short_toggle_rsi")
    async def short_toggle_rsi(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.short_cfg); cfg.use_rsi = not cfg.use_rsi
        await cb.answer("RSI ШОРТ " + ("✅" if cfg.use_rsi else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_volume")
    async def short_toggle_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.short_cfg); cfg.use_volume = not cfg.use_volume
        await cb.answer("Объём ШОРТ " + ("✅" if cfg.use_volume else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_pattern")
    async def short_toggle_pattern(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.short_cfg); cfg.use_pattern = not cfg.use_pattern
        await cb.answer("Паттерны ШОРТ " + ("✅" if cfg.use_pattern else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data == "short_toggle_htf")
    async def short_toggle_htf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        cfg  = TradeCfg.from_json(user.short_cfg); cfg.use_htf = not cfg.use_htf
        await cb.answer("HTF ШОРТ " + ("✅" if cfg.use_htf else "❌"))
        user.short_cfg = cfg.to_json(); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_rsi_period_"))
    async def short_set_rsi_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_rsi_period_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "rsi_period", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_rsi_ob_"))
    async def short_set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_rsi_ob_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "rsi_ob", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_rsi_os_"))
    async def short_set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_rsi_os_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "rsi_os", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_vol_mult_"))
    async def short_set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_vol_mult_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "vol_mult", v); await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры ШОРТ</b>", kb_short_filters(user))

    @dp.callback_query(F.data.startswith("short_set_quality_"))
    async def short_set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_quality_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "min_quality", v); await um.save(user)
        await safe_edit(cb, "⭐ <b>Качество ШОРТ</b>", kb_short_quality(user))

    @dp.callback_query(F.data.startswith("short_set_cooldown_"))
    async def short_set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = int(cb.data.replace("short_set_cooldown_", ""))
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "cooldown_bars", v); await um.save(user)
        await safe_edit(cb, "🔁 <b>Cooldown ШОРТ</b>", kb_short_cooldown(user))

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
        await cb.answer("✅ " + str(v))
        _update_short_field(user, "atr_mult", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data.startswith("short_set_risk_"))
    async def short_set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_risk_", ""))
        await cb.answer("✅ " + str(v) + "%")
        _update_short_field(user, "max_risk_pct", v); await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп ШОРТ</b>", kb_short_sl(user))

    @dp.callback_query(F.data.startswith("short_set_volume_"))
    async def short_set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        v = float(cb.data.replace("short_set_volume_", ""))
        await cb.answer("✅ $" + str(int(v)))
        _update_short_field(user, "min_volume_usdt", v); await um.save(user)
        await safe_edit(cb, "💰 <b>Объём ШОРТ</b>", kb_short_volume(user))

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
            await safe_edit(cb, access_denied_text(reason), kb_subscribe(config))
            return
        if user.scan_mode != "both":
            user.scan_mode = "both"
            user.active = True
        else:
            user.active = not user.active
        await cb.answer("🟢 Включён!" if user.active else "🔴 Выключен.")
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_tf")
    async def menu_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📊 <b>Таймфрейм ОБА</b>", kb_timeframes(user.timeframe))

    @dp.callback_query(F.data.startswith("set_tf_"))
    async def set_tf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.timeframe = cb.data.replace("set_tf_", "")
        await cb.answer("✅ ТФ: " + user.timeframe)
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_interval")
    async def menu_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔄 <b>Интервал ОБА</b>", kb_intervals(user.scan_interval))

    @dp.callback_query(F.data.startswith("set_interval_"))
    async def set_interval(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.scan_interval = int(cb.data.replace("set_interval_", ""))
        await cb.answer("✅ Каждые " + str(user.scan_interval//60) + " мин.")
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_settings")
    async def menu_settings(cb: CallbackQuery):
        await cb.answer()
        await safe_edit(cb, "⚙️ <b>Настройки ОБА</b>", kb_settings())

    # Общие настройки (ОБА) — пивоты, EMA, фильтры, и т.д.
    @dp.callback_query(F.data == "menu_pivots")
    async def menu_pivots(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📐 <b>Пивоты (общие)</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_pivot_"))
    async def set_pivot(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.pivot_strength = int(cb.data.replace("set_pivot_", ""))
        await cb.answer("✅ " + str(user.pivot_strength))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты (общие)</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_age_"))
    async def set_age(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_level_age = int(cb.data.replace("set_age_", ""))
        await cb.answer("✅ " + str(user.max_level_age))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты (общие)</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_retest_"))
    async def set_retest(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_retest_bars = int(cb.data.replace("set_retest_", ""))
        await cb.answer("✅ " + str(user.max_retest_bars))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты (общие)</b>", kb_pivots(user))

    @dp.callback_query(F.data.startswith("set_buffer_"))
    async def set_buffer(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.zone_buffer = float(cb.data.replace("set_buffer_", ""))
        await cb.answer("✅ x" + str(user.zone_buffer))
        await um.save(user)
        await safe_edit(cb, "📐 <b>Пивоты (общие)</b>", kb_pivots(user))

    @dp.callback_query(F.data == "menu_ema")
    async def menu_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📉 <b>EMA (общие)</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_fast_"))
    async def set_ema_fast(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.ema_fast = int(cb.data.replace("set_ema_fast_", ""))
        await cb.answer("✅ EMA Fast: " + str(user.ema_fast))
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA (общие)</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_ema_slow_"))
    async def set_ema_slow(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.ema_slow = int(cb.data.replace("set_ema_slow_", ""))
        await cb.answer("✅ EMA Slow: " + str(user.ema_slow))
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA (общие)</b>", kb_ema(user))

    @dp.callback_query(F.data.startswith("set_htf_ema_"))
    async def set_htf_ema(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.htf_ema_period = int(cb.data.replace("set_htf_ema_", ""))
        await cb.answer("✅ HTF: " + str(user.htf_ema_period))
        await um.save(user)
        await safe_edit(cb, "📉 <b>EMA (общие)</b>", kb_ema(user))

    @dp.callback_query(F.data == "menu_filters")
    async def menu_filters(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_rsi")
    async def toggle_rsi(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_rsi = not user.use_rsi
        await cb.answer("RSI " + ("✅" if user.use_rsi else "❌"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_volume")
    async def toggle_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_volume = not user.use_volume
        await cb.answer("Объём " + ("✅" if user.use_volume else "❌"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_pattern")
    async def toggle_pattern(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_pattern = not user.use_pattern
        await cb.answer("Паттерны " + ("✅" if user.use_pattern else "❌"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data == "toggle_htf")
    async def toggle_htf(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.use_htf = not user.use_htf
        await cb.answer("HTF " + ("✅" if user.use_htf else "❌"))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_period_"))
    async def set_rsi_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_period = int(cb.data.replace("set_rsi_period_", ""))
        await cb.answer("✅ RSI: " + str(user.rsi_period))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_ob_"))
    async def set_rsi_ob(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_ob = int(cb.data.replace("set_rsi_ob_", ""))
        await cb.answer("✅ " + str(user.rsi_ob))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_rsi_os_"))
    async def set_rsi_os(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.rsi_os = int(cb.data.replace("set_rsi_os_", ""))
        await cb.answer("✅ " + str(user.rsi_os))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data.startswith("set_vol_mult_"))
    async def set_vol_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.vol_mult = float(cb.data.replace("set_vol_mult_", ""))
        await cb.answer("✅ x" + str(user.vol_mult))
        await um.save(user)
        await safe_edit(cb, "🔬 <b>Фильтры (общие)</b>", kb_filters(user))

    @dp.callback_query(F.data == "menu_quality")
    async def menu_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "⭐ <b>Качество (общее)</b>", kb_quality(user.min_quality))

    @dp.callback_query(F.data.startswith("set_quality_"))
    async def set_quality(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.min_quality = int(cb.data.replace("set_quality_", ""))
        await cb.answer("✅ " + str(user.min_quality))
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_cooldown")
    async def menu_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🔁 <b>Cooldown (общий)</b>", kb_cooldown(user.cooldown_bars))

    @dp.callback_query(F.data.startswith("set_cooldown_"))
    async def set_cooldown(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.cooldown_bars = int(cb.data.replace("set_cooldown_", ""))
        await cb.answer("✅ " + str(user.cooldown_bars))
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_sl")
    async def menu_sl(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🛡 <b>Стоп-лосс (общий)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_period_"))
    async def set_atr_period(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.atr_period = int(cb.data.replace("set_atr_period_", ""))
        await cb.answer("✅ ATR: " + str(user.atr_period))
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп (общий)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_atr_mult_"))
    async def set_atr_mult(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.atr_mult = float(cb.data.replace("set_atr_mult_", ""))
        await cb.answer("✅ x" + str(user.atr_mult))
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп (общий)</b>", kb_sl(user))

    @dp.callback_query(F.data.startswith("set_risk_"))
    async def set_risk(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.max_risk_pct = float(cb.data.replace("set_risk_", ""))
        await cb.answer("✅ " + str(user.max_risk_pct) + "%")
        await um.save(user)
        await safe_edit(cb, "🛡 <b>Стоп (общий)</b>", kb_sl(user))

    @dp.callback_query(F.data == "menu_targets")
    async def menu_targets(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "🎯 <b>Цели (общие)</b>", kb_targets(user))

    @dp.callback_query(F.data == "menu_volume")
    async def menu_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "💰 <b>Объём (общий)</b>", kb_volume(user.min_volume_usdt))

    @dp.callback_query(F.data.startswith("set_volume_"))
    async def set_volume(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.min_volume_usdt = float(cb.data.replace("set_volume_", ""))
        await cb.answer("✅ $" + str(int(user.min_volume_usdt)))
        await um.save(user)
        await safe_edit(cb, settings_text(user), kb_mode_both(user))

    @dp.callback_query(F.data == "menu_notify")
    async def menu_notify(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        await cb.answer()
        await safe_edit(cb, "📱 <b>Уведомления</b>", kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify_signal")
    async def toggle_notify_signal(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.notify_signal = not user.notify_signal
        await cb.answer("Сигналы " + ("✅" if user.notify_signal else "❌"))
        await um.save(user)
        await safe_edit(cb, "📱 <b>Уведомления</b>", kb_notify(user))

    @dp.callback_query(F.data == "toggle_notify_breakout")
    async def toggle_notify_breakout(cb: CallbackQuery):
        user = await um.get_or_create(cb.from_user.id)
        user.notify_breakout = not user.notify_breakout
        await cb.answer("Пробои " + ("✅" if user.notify_breakout else "❌"))
        await um.save(user)
        await safe_edit(cb, "📱 <b>Уведомления</b>", kb_notify(user))

    # ─── TP редактирование (FSM) ──────────────────────

    @dp.callback_query(F.data == "edit_tp1")
    async def edit_tp1(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.tp1)
        await cb.message.answer("🎯 Введи Цель 1 (например: <b>0.8</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_tp2")
    async def edit_tp2(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.tp2)
        await cb.message.answer("🎯 Введи Цель 2 (например: <b>1.5</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_tp3")
    async def edit_tp3(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(EditState.tp3)
        await cb.message.answer("🏆 Введи Цель 3 (например: <b>2.5</b>):", parse_mode="HTML")

    @dp.message(EditState.tp1)
    async def save_tp1(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp1_rr = round(float(msg.text.replace(",",".")), 1)
            await um.save(user); await state.clear()
            await msg.answer("✅ Цель 1 = <b>" + str(user.tp1_rr) + "R</b>", parse_mode="HTML", reply_markup=kb_targets(user))
        except ValueError: await msg.answer("❌ Введи число, например: 0.8")

    @dp.message(EditState.tp2)
    async def save_tp2(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp2_rr = round(float(msg.text.replace(",",".")), 1)
            await um.save(user); await state.clear()
            await msg.answer("✅ Цель 2 = <b>" + str(user.tp2_rr) + "R</b>", parse_mode="HTML", reply_markup=kb_targets(user))
        except ValueError: await msg.answer("❌ Введи число, например: 1.5")

    @dp.message(EditState.tp3)
    async def save_tp3(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            user.tp3_rr = round(float(msg.text.replace(",",".")), 1)
            await um.save(user); await state.clear()
            await msg.answer("✅ Цель 3 = <b>" + str(user.tp3_rr) + "R</b>", parse_mode="HTML", reply_markup=kb_targets(user))
        except ValueError: await msg.answer("❌ Введи число, например: 2.5")

    # ЛОНГ TP
    @dp.callback_query(F.data == "edit_long_tp1")
    async def edit_long_tp1(cb: CallbackQuery, state: FSMContext):
        await cb.answer(); await state.set_state(EditState.long_tp1)
        await cb.message.answer("🎯 Цель 1 ЛОНГ (например: <b>0.8</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_long_tp2")
    async def edit_long_tp2(cb: CallbackQuery, state: FSMContext):
        await cb.answer(); await state.set_state(EditState.long_tp2)
        await cb.message.answer("🎯 Цель 2 ЛОНГ (например: <b>1.5</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_long_tp3")
    async def edit_long_tp3(cb: CallbackQuery, state: FSMContext):
        await cb.answer(); await state.set_state(EditState.long_tp3)
        await cb.message.answer("🏆 Цель 3 ЛОНГ (например: <b>2.5</b>):", parse_mode="HTML")

    @dp.message(EditState.long_tp1)
    async def save_long_tp1(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            v = round(float(msg.text.replace(",",".")), 1)
            _update_long_field(user, "tp1_rr", v); await um.save(user); await state.clear()
            await msg.answer("✅ Цель 1 ЛОНГ = <b>" + str(v) + "R</b>", parse_mode="HTML", reply_markup=kb_long_targets(user))
        except ValueError: await msg.answer("❌ Введи число")

    @dp.message(EditState.long_tp2)
    async def save_long_tp2(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            v = round(float(msg.text.replace(",",".")), 1)
            _update_long_field(user, "tp2_rr", v); await um.save(user); await state.clear()
            await msg.answer("✅ Цель 2 ЛОНГ = <b>" + str(v) + "R</b>", parse_mode="HTML", reply_markup=kb_long_targets(user))
        except ValueError: await msg.answer("❌ Введи число")

    @dp.message(EditState.long_tp3)
    async def save_long_tp3(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            v = round(float(msg.text.replace(",",".")), 1)
            _update_long_field(user, "tp3_rr", v); await um.save(user); await state.clear()
            await msg.answer("✅ Цель 3 ЛОНГ = <b>" + str(v) + "R</b>", parse_mode="HTML", reply_markup=kb_long_targets(user))
        except ValueError: await msg.answer("❌ Введи число")

    # ШОРТ TP
    @dp.callback_query(F.data == "edit_short_tp1")
    async def edit_short_tp1(cb: CallbackQuery, state: FSMContext):
        await cb.answer(); await state.set_state(EditState.short_tp1)
        await cb.message.answer("🎯 Цель 1 ШОРТ (например: <b>0.8</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_short_tp2")
    async def edit_short_tp2(cb: CallbackQuery, state: FSMContext):
        await cb.answer(); await state.set_state(EditState.short_tp2)
        await cb.message.answer("🎯 Цель 2 ШОРТ (например: <b>1.5</b>):", parse_mode="HTML")

    @dp.callback_query(F.data == "edit_short_tp3")
    async def edit_short_tp3(cb: CallbackQuery, state: FSMContext):
        await cb.answer(); await state.set_state(EditState.short_tp3)
        await cb.message.answer("🏆 Цель 3 ШОРТ (например: <b>2.5</b>):", parse_mode="HTML")

    @dp.message(EditState.short_tp1)
    async def save_short_tp1(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            v = round(float(msg.text.replace(",",".")), 1)
            _update_short_field(user, "tp1_rr", v); await um.save(user); await state.clear()
            await msg.answer("✅ Цель 1 ШОРТ = <b>" + str(v) + "R</b>", parse_mode="HTML", reply_markup=kb_short_targets(user))
        except ValueError: await msg.answer("❌ Введи число")

    @dp.message(EditState.short_tp2)
    async def save_short_tp2(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            v = round(float(msg.text.replace(",",".")), 1)
            _update_short_field(user, "tp2_rr", v); await um.save(user); await state.clear()
            await msg.answer("✅ Цель 2 ШОРТ = <b>" + str(v) + "R</b>", parse_mode="HTML", reply_markup=kb_short_targets(user))
        except ValueError: await msg.answer("❌ Введи число")

    @dp.message(EditState.short_tp3)
    async def save_short_tp3(msg: Message, state: FSMContext):
        user = await um.get_or_create(msg.from_user.id)
        try:
            v = round(float(msg.text.replace(",",".")), 1)
            _update_short_field(user, "tp3_rr", v); await um.save(user); await state.clear()
            await msg.answer("✅ Цель 3 ШОРТ = <b>" + str(v) + "R</b>", parse_mode="HTML", reply_markup=kb_short_targets(user))
        except ValueError: await msg.answer("❌ Введи число")

    # ─── ОБЩЕЕ ────────────────────────────────────────

    @dp.callback_query(F.data == "noop")
    async def noop(cb: CallbackQuery):
        await cb.answer()

    @dp.callback_query(F.data == "toggle_active")
    async def toggle_active_legacy(cb: CallbackQuery):
        """Совместимость со старыми сообщениями."""
        await toggle_both(cb)
