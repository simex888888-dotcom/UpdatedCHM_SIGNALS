"""
poly_handlers.py — обработчики Telegram-бота для Polymarket.

Регистрируется через register_poly_handlers(dp, bot, um, config, poly).

Доступ:
  Просмотр маркетов + AI-анализ — все подписчики
  Размещение ставок              — только администраторы (единый кошелёк бота)
"""

import logging
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import database as db
from polymarket_service import (
    PolymarketService, analyze_market, _get_short_key, get_condition_id,
)

log = logging.getLogger("CHM.Poly")

# ─── Антиспам ─────────────────────────────────────────────────────────────────
_bet_ts: dict[int, float] = {}
_BET_COOLDOWN = 3.0


# ─── FSM состояния ────────────────────────────────────────────────────────────

class PolyState(StatesGroup):
    waiting_search   = State()
    waiting_amount   = State()
    waiting_bet_size = State()


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _ik(*rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _conf_emoji(conf: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "⚪")


def _risk_emoji(risk: str) -> str:
    return {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(risk, "⚪")


def _rec_emoji(rec: str) -> str:
    return {"BUY YES": "✅", "BUY NO": "✅", "SKIP": "⏭️"}.get(rec, "⏭️")


def _fmt_pct(v: float) -> str:
    return f"{v:.0%}"


def _fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def _market_short(question: str, max_len: int = 38) -> str:
    return question[:max_len] + "…" if len(question) > max_len else question


def _market_card(market: dict, analysis: dict) -> str:
    q        = market.get("question", "—")
    end_date = market.get("endDate", "")[:10] or "—"
    yes_p    = analysis["yes_price"]
    no_p     = analysis["no_price"]
    vol      = analysis["volume_24h"]
    liq      = analysis["liquidity"]
    rec      = analysis["recommendation"]
    conf     = analysis["confidence"]
    reason   = analysis["reasoning"]
    risk     = analysis.get("risk", "MEDIUM")
    edge     = analysis.get("edge", "0%")
    ai_label = "🤖 <b>AI-анализ (Groq):</b>" if analysis.get("_source") != "rule" else "🤖 <b>AI-анализ:</b>"

    NL = "\n"
    return (
        f"📊 <b>{q}</b>" + NL + NL +
        f"YES: <b>{_fmt_pct(yes_p)}</b> (${yes_p:.2f})  |  NO: <b>{_fmt_pct(no_p)}</b> (${no_p:.2f})" + NL +
        f"💧 Ликвидность: <b>{_fmt_usd(liq)}</b>" + NL +
        f"📈 Объём 24ч: <b>{_fmt_usd(vol)}</b>" + NL +
        f"⏰ Закрытие: <b>{end_date}</b>" + NL + NL +
        ai_label + NL +
        f"Рекомендация: <b>{rec}</b> {_rec_emoji(rec)}" + NL +
        f"Уверенность: <b>{conf}</b> {_conf_emoji(conf)}" + NL +
        f"Риск: <b>{risk}</b> {_risk_emoji(risk)}" + NL + NL +
        f"<i>{reason}</i>" + NL +
        f"<code>Edge: {edge}</code>"
    )


def _market_kb(sk: int, market: dict, analysis: dict, default_bet: float) -> InlineKeyboardMarkup:
    """Клавиатура карточки маркета."""
    tokens    = _get_token_ids(market)
    yes_tid   = tokens.get("yes", "")
    no_tid    = tokens.get("no", "")
    bet_str   = f"{default_bet:.0f}"
    yes_short = yes_tid[:20] if yes_tid else ""
    no_short  = no_tid[:20] if no_tid else ""

    rows = []
    if yes_tid and no_tid:
        rows.append([
            _btn(f"BUY YES ${bet_str}", f"pm:buy:{sk}:yes:{yes_short}:{bet_str}"),
            _btn(f"BUY NO ${bet_str}",  f"pm:buy:{sk}:no:{no_short}:{bet_str}"),
        ])
        rows.append([_btn("💰 Своя сумма", f"pm:custom:{sk}:yes:{yes_short}")])
    rows.append([_btn("🔙 К списку", "pm:trending:0"), _btn("📊 Polymarket", "pm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_token_ids(market: dict) -> dict:
    """Возвращает {'yes': token_id, 'no': token_id}."""
    from polymarket_service import _get_tokens
    tokens = _get_tokens(market)
    result = {}
    for t in tokens:
        outcome = str(t.get("outcome", "")).lower()
        if "yes" in outcome:
            result["yes"] = t.get("token_id", "")
        elif "no" in outcome:
            result["no"] = t.get("token_id", "")
    return result


async def _safe_edit(cb: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


# ─── Регистрация обработчиков ─────────────────────────────────────────────────

def register_poly_handlers(
    dp: Dispatcher,
    bot: Bot,
    um,
    config,
    poly: PolymarketService,
):
    def is_admin(uid: int) -> bool:
        return uid in config.ADMIN_IDS

    # ── /poly — главное меню ───────────────────────────────────────────────

    @dp.message(Command("poly"))
    async def cmd_poly(msg: Message):
        user = await um.get_or_create(msg.from_user.id, msg.from_user.username or "")
        has, _ = user.check_access()
        if not has:
            await msg.answer("❌ Нужна подписка для доступа к Polymarket.")
            return

        trade_note = (
            "\n\n🔒 <i>Торговля доступна только администраторам.</i>"
            if not is_admin(msg.from_user.id) else
            "\n\n💼 <i>Торговля включена.</i>"
        )

        kb = _ik(
            [_btn("🔥 Трендовые маркеты", "pm:trending:0")],
            [_btn("🔍 Поиск маркета",      "pm:search")],
            [_btn("💼 Мои ставки",          "pm:mybets")],
            [_btn("💰 Баланс кошелька",      "pm:balance")],
            [_btn("⚙️ Размер ставки",        "pm:settings")],
            [_btn("🔙 Главное меню",          "back_main")],
        )
        await msg.answer(
            "📊 <b>Polymarket — Prediction Market</b>" + trade_note,
            parse_mode="HTML", reply_markup=kb,
        )

    # ── Трендовые маркеты ─────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:trending:"))
    async def cb_trending(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        has, _ = user.check_access()
        if not has:
            await cb.answer("❌ Нужна подписка.", show_alert=True); return

        offset = int(cb.data.split(":")[2])
        try:
            markets = await poly.get_trending_markets(limit=10, offset=offset)
        except Exception as e:
            await _safe_edit(cb, f"⚠️ Polymarket API недоступен: {e}")
            return

        if not markets:
            await _safe_edit(cb, "📭 Маркеты не найдены.", _ik([_btn("🔙 Polymarket", "pm:menu")]))
            return

        rows = []
        for m in markets:
            q    = _market_short(m.get("question", "—"), 40)
            sk   = _get_short_key(m.get("id", ""))
            analysis = analyze_market(m)
            pct  = f"{analysis['yes_price']:.0%}"
            rows.append([_btn(f"{'✅' if analysis['recommendation']!='SKIP' else '📊'} {q} | YES {pct}", f"pm:view:{sk}")])

        nav = []
        if offset > 0:
            nav.append(_btn("◀️ Назад", f"pm:trending:{max(0, offset-10)}"))
        if len(markets) == 10:
            nav.append(_btn("▶️ Ещё", f"pm:trending:{offset+10}"))
        if nav:
            rows.append(nav)
        rows.append([_btn("🔙 Polymarket", "pm:menu")])

        await _safe_edit(
            cb,
            f"🔥 <b>Трендовые маркеты</b> (#{offset+1}–#{offset+len(markets)})",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # ── Поиск маркета ─────────────────────────────────────────────────────

    @dp.callback_query(F.data == "pm:search")
    async def cb_search_start(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(PolyState.waiting_search)
        await cb.message.answer(
            "🔍 Введите ключевое слово для поиска маркета:\n"
            "<i>Например: bitcoin, trump, election</i>",
            parse_mode="HTML",
        )

    @dp.message(PolyState.waiting_search)
    async def msg_search(msg: Message, state: FSMContext):
        await state.clear()
        user = await um.get_or_create(msg.from_user.id)
        has, _ = user.check_access()
        if not has:
            return

        query = msg.text.strip()[:50]
        try:
            markets = await poly.search_markets(query, limit=10)
        except Exception as e:
            await msg.answer(f"⚠️ Polymarket API недоступен: {e}")
            return

        if not markets:
            await msg.answer(f"📭 По запросу «{query}» ничего не найдено.",
                             reply_markup=_ik([_btn("🔙 Polymarket", "pm:menu")]))
            return

        rows = []
        for m in markets:
            q  = _market_short(m.get("question", "—"), 40)
            sk = _get_short_key(m.get("id", ""))
            analysis = analyze_market(m)
            pct = f"{analysis['yes_price']:.0%}"
            rows.append([_btn(f"📊 {q} | YES {pct}", f"pm:view:{sk}")])
        rows.append([_btn("🔙 Polymarket", "pm:menu")])

        await msg.answer(
            f"🔍 <b>Результаты поиска «{query}»</b> ({len(markets)} маркетов)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # ── Карточка маркета ──────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:view:"))
    async def cb_view_market(cb: CallbackQuery):
        await cb.answer()
        sk = int(cb.data.split(":")[2])
        condition_id = get_condition_id(sk)
        if not condition_id:
            await cb.answer("⚠️ Маркет не найден.", show_alert=True); return

        market = await poly.get_market_by_id(condition_id)
        if not market:
            await cb.answer("⚠️ Не удалось загрузить маркет.", show_alert=True); return

        await _safe_edit(cb, "⏳ <b>AI анализирует маркет...</b>", None)
        analysis = await poly.analyze_market(market)
        settings = await db.poly_get_settings(cb.from_user.id)
        default_bet = settings.get("default_bet", 5.0)

        text = _market_card(market, analysis)
        kb   = _market_kb(sk, market, analysis, default_bet)

        # Если пользователь не admin — убираем кнопки покупки
        if not is_admin(cb.from_user.id):
            kb = _ik(
                [_btn("🔒 Торговля — только для admin", "pm:noop")],
                [_btn("🔙 Назад к списку", "pm:trending:0")],
            )

        await _safe_edit(cb, text, kb)

    # ── Подтверждение покупки ─────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:buy:"))
    async def cb_buy(cb: CallbackQuery):
        await cb.answer()
        if not is_admin(cb.from_user.id):
            await cb.answer("🔒 Торговля только для администраторов.", show_alert=True)
            return

        parts    = cb.data.split(":")
        # pm:buy:{sk}:{side}:{token_short}:{amount}
        sk       = int(parts[2])
        side     = parts[3].upper()
        tok_short = parts[4]
        amount   = float(parts[5])

        condition_id = get_condition_id(sk)
        market = await poly.get_market_by_id(condition_id) if condition_id else None
        q = market.get("question", "—") if market else "—"

        # Находим полный token_id из кэша маркета
        token_id = ""
        if market:
            tids = _get_token_ids(market)
            token_id = tids.get(side.lower(), tok_short)

        yes_, no_ = (0.5, 0.5)
        if market:
            _a = analyze_market(market)   # fast rule-based for price only
            yes_, no_ = _a["yes_price"], _a["no_price"]

        price    = yes_ if side == "YES" else no_
        shares   = round(amount / price, 2) if price > 0 else 0
        profit   = round(shares - amount, 2)
        profit_p = round(profit / amount * 100, 1) if amount > 0 else 0

        NL = "\n"
        text = (
            "⚡ <b>Подтверждение ставки</b>" + NL + NL +
            f"Маркет: <b>{_market_short(q, 50)}</b>" + NL +
            f"Сторона: <b>{side}</b>" + NL +
            f"Сумма: <b>${amount:.2f} USDC</b>" + NL +
            f"Ожидаемые доли: <b>~{shares}</b>" + NL +
            f"Потенциальная прибыль: <b>${profit:.2f} ({profit_p}%)</b>"
        )

        confirm_data = f"pm:confirm:{sk}:{side.lower()}:{token_id[:20]}:{amount}"
        kb = _ik(
            [_btn("✅ Подтвердить", confirm_data), _btn("❌ Отмена", f"pm:view:{sk}")],
        )
        await _safe_edit(cb, text, kb)

    # ── Исполнение ставки ─────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:confirm:"))
    async def cb_confirm(cb: CallbackQuery):
        await cb.answer()
        if not is_admin(cb.from_user.id):
            await cb.answer("🔒 Только для администраторов.", show_alert=True)
            return

        # Антиспам
        now = time.time()
        if now - _bet_ts.get(cb.from_user.id, 0) < _BET_COOLDOWN:
            await cb.answer("⏳ Подождите несколько секунд.", show_alert=True)
            return
        _bet_ts[cb.from_user.id] = now

        parts     = cb.data.split(":")
        sk        = int(parts[2])
        side      = parts[3].upper()
        tok_short = parts[4]
        amount    = float(parts[5])

        condition_id = get_condition_id(sk)
        market = await poly.get_market_by_id(condition_id) if condition_id else None

        # Восстановить полный token_id из маркета
        token_id = tok_short
        if market:
            tids = _get_token_ids(market)
            token_id = tids.get(side.lower(), tok_short)

        await _safe_edit(cb, "⏳ <b>Размещаем ставку...</b>", None)

        try:
            result = await poly.place_bet(token_id, amount)
        except RuntimeError as e:
            await _safe_edit(cb,
                f"❌ <b>Ошибка:</b> {e}",
                _ik([_btn("🔙 Назад", f"pm:view:{sk}")]),
            )
            return
        except Exception as e:
            err = str(e)
            if "insufficient" in err.lower() or "balance" in err.lower():
                msg = f"❌ Недостаточно USDC."
            elif "closed" in err.lower():
                msg = "⏰ Этот маркет уже закрыт."
            else:
                msg = f"❌ Ошибка API: {err[:200]}"
            await _safe_edit(cb, msg, _ik([_btn("🔙 Назад", f"pm:view:{sk}")]))
            return

        # Сохраняем ставку в БД
        order_id = ""
        if isinstance(result, dict):
            order_id = result.get("orderId", result.get("id", ""))
        q = market.get("question", "—") if market else "—"
        await db.poly_save_bet(
            user_id=cb.from_user.id,
            market_id=condition_id or "",
            question=q,
            side=side,
            amount=amount,
            shares=0.0,
            price=0.0,
            order_id=order_id,
        )

        NL = "\n"
        await _safe_edit(cb,
            "✅ <b>Ставка размещена!</b>" + NL + NL +
            f"Маркет: <b>{_market_short(q, 50)}</b>" + NL +
            f"Сторона: <b>{side}</b>  Сумма: <b>${amount:.2f} USDC</b>" + NL +
            (f"Order ID: <code>{order_id[:40]}</code>" if order_id else ""),
            _ik([_btn("💼 Мои ставки", "pm:mybets"), _btn("🔙 Polymarket", "pm:menu")]),
        )

    # ── Своя сумма ────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:custom:"))
    async def cb_custom_amount(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        if not is_admin(cb.from_user.id):
            await cb.answer("🔒 Только для администраторов.", show_alert=True)
            return
        parts = cb.data.split(":")
        # pm:custom:{sk}:{side}:{tok_short}
        sk   = parts[2]
        side = parts[3]
        tok  = parts[4]
        await state.update_data(custom_sk=sk, custom_side=side, custom_tok=tok)
        await state.set_state(PolyState.waiting_amount)
        await cb.message.answer("💰 Введите сумму в USDC (минимум $1):")

    @dp.message(PolyState.waiting_amount)
    async def msg_custom_amount(msg: Message, state: FSMContext):
        data = await state.get_data()
        await state.clear()

        try:
            amount = float(msg.text.strip().replace("$", "").replace(",", "."))
            if amount < 1:
                raise ValueError
        except ValueError:
            await msg.answer("❌ Некорректная сумма. Минимум $1.")
            return

        sk       = int(data.get("custom_sk", 0))
        side     = data.get("custom_side", "yes").upper()
        tok_short = data.get("custom_tok", "")

        condition_id = get_condition_id(sk)
        market = await poly.get_market_by_id(condition_id) if condition_id else None
        q = market.get("question", "—") if market else "—"

        token_id = tok_short
        if market:
            tids = _get_token_ids(market)
            token_id = tids.get(side.lower(), tok_short)

        _a    = analyze_market(market) if market else {"yes_price": 0.5, "no_price": 0.5}
        price = _a["yes_price"] if side == "YES" else _a["no_price"]
        shares   = round(amount / price, 2) if price > 0 else 0
        profit   = round(shares - amount, 2)

        NL = "\n"
        text = (
            "⚡ <b>Подтверждение ставки</b>" + NL + NL +
            f"Маркет: <b>{_market_short(q, 50)}</b>" + NL +
            f"Сторона: <b>{side}</b>" + NL +
            f"Сумма: <b>${amount:.2f} USDC</b>" + NL +
            f"Ожидаемые доли: <b>~{shares}</b>" + NL +
            f"Потенциальная прибыль: <b>${profit:.2f}</b>"
        )
        confirm_data = f"pm:confirm:{sk}:{side.lower()}:{token_id[:20]}:{amount}"
        kb = _ik(
            [_btn("✅ Подтвердить", confirm_data), _btn("❌ Отмена", f"pm:view:{sk}")],
        )
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)

    # ── Мои ставки ────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "pm:mybets")
    async def cb_mybets(cb: CallbackQuery):
        await cb.answer()
        bets = await db.poly_get_bets(cb.from_user.id, limit=10)
        if not bets:
            await _safe_edit(cb, "📭 Ставок пока нет.",
                             _ik([_btn("🔙 Polymarket", "pm:menu")]))
            return

        NL = "\n"
        lines = ["💼 <b>Ваши последние ставки</b>" + NL]
        for i, b in enumerate(bets, 1):
            q    = _market_short(b.get("question", "—"), 35)
            side = b.get("side", "—")
            amt  = b.get("amount_usdc", 0)
            dt   = str(b.get("created_at", ""))[:10]
            em   = "🟢" if side == "YES" else "🔴"
            lines.append(f"{i}. {em} <b>{q}</b>" + NL +
                         f"   {side} | ${amt:.2f} | {dt}")

        await _safe_edit(cb, NL.join(lines),
                         _ik([_btn("🔙 Polymarket", "pm:menu")]))

    # ── Баланс ───────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "pm:balance")
    async def cb_balance(cb: CallbackQuery):
        await cb.answer()
        if not is_admin(cb.from_user.id):
            await _safe_edit(cb,
                "🔒 Баланс кошелька доступен только администраторам.",
                _ik([_btn("🔙 Polymarket", "pm:menu")]),
            )
            return
        await _safe_edit(cb, "⏳ Запрашиваем баланс...", None)
        balance = await poly.get_balance()
        await _safe_edit(cb,
            f"💰 <b>Баланс кошелька:</b> <b>${balance:.2f} USDC</b>",
            _ik([_btn("🔙 Polymarket", "pm:menu")]),
        )

    # ── Настройки (размер ставки по умолчанию) ───────────────────────────

    @dp.callback_query(F.data == "pm:settings")
    async def cb_settings(cb: CallbackQuery):
        await cb.answer()
        s = await db.poly_get_settings(cb.from_user.id)
        bet = s.get("default_bet", 5.0)
        await _safe_edit(cb,
            f"⚙️ <b>Настройки Polymarket</b>\n\n"
            f"Размер ставки по умолчанию: <b>${bet:.1f} USDC</b>",
            _ik(
                [_btn("$1", "pm:setbet:1"), _btn("$5", "pm:setbet:5"),
                 _btn("$10", "pm:setbet:10"), _btn("$25", "pm:setbet:25")],
                [_btn("💬 Своё значение", "pm:setbet:custom")],
                [_btn("🔙 Polymarket", "pm:menu")],
            ),
        )

    @dp.callback_query(F.data.startswith("pm:setbet:"))
    async def cb_setbet(cb: CallbackQuery, state: FSMContext):
        val = cb.data.split(":")[2]
        if val == "custom":
            await cb.answer()
            await state.set_state(PolyState.waiting_bet_size)
            await cb.message.answer("💰 Введите желаемый размер ставки в USDC:")
            return
        await db.poly_save_settings(cb.from_user.id, float(val))
        await cb.answer(f"✅ Размер ставки: ${val} USDC", show_alert=True)
        await cb_settings.__wrapped__(cb) if hasattr(cb_settings, "__wrapped__") else None

    @dp.message(PolyState.waiting_bet_size)
    async def msg_bet_size(msg: Message, state: FSMContext):
        await state.clear()
        try:
            val = float(msg.text.strip().replace("$", "").replace(",", "."))
            if val < 1:
                raise ValueError
        except ValueError:
            await msg.answer("❌ Некорректная сумма. Минимум $1.")
            return
        await db.poly_save_settings(msg.from_user.id, val)
        await msg.answer(
            f"✅ Размер ставки по умолчанию: <b>${val:.1f} USDC</b>",
            parse_mode="HTML",
            reply_markup=_ik([_btn("🔙 Polymarket", "pm:menu")]),
        )

    # ── Вспомогательные callback'и ────────────────────────────────────────

    @dp.callback_query(F.data == "pm:menu")
    async def cb_menu(cb: CallbackQuery):
        await cb.answer()
        trade_note = (
            "\n\n🔒 <i>Торговля доступна только администраторам.</i>"
            if not is_admin(cb.from_user.id) else "\n\n💼 <i>Торговля включена.</i>"
        )
        kb = _ik(
            [_btn("🔥 Трендовые маркеты", "pm:trending:0")],
            [_btn("🔍 Поиск маркета",      "pm:search")],
            [_btn("💼 Мои ставки",          "pm:mybets")],
            [_btn("💰 Баланс кошелька",      "pm:balance")],
            [_btn("⚙️ Размер ставки",        "pm:settings")],
            [_btn("🔙 Главное меню",          "back_main")],
        )
        await _safe_edit(
            cb,
            "📊 <b>Polymarket — Prediction Market</b>" + trade_note,
            kb,
        )

    @dp.callback_query(F.data == "pm:noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()
