"""
poly_handlers.py — обработчики Telegram-бота для Polymarket.

Архитектура: кастодиальные кошельки — каждый подписчик получает свой
Polygon-адрес, пополняет USDC и торгует прямо из бота.

Доступ:
  Просмотр маркетов + AI-анализ — все подписчики
  Торговля — подписчики с пополненным кошельком (WALLET_ENCRYPTION_KEY задан)
  Прямой admin-кошелёк (POLY_PRIVATE_KEY) — только администраторы
"""

import asyncio
import html
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
import wallet_service
from polymarket_service import (
    PolymarketService, analyze_market, _get_short_key, get_condition_id,
    _parse_prices, translate_market, translate_question,
)

log = logging.getLogger("CHM.Poly")

# ─── Антиспам ─────────────────────────────────────────────────────────────────
_bet_ts: dict[int, float] = {}
_BET_COOLDOWN = 3.0


# ─── FSM ──────────────────────────────────────────────────────────────────────

class PolyState(StatesGroup):
    waiting_search   = State()
    waiting_amount   = State()
    waiting_bet_size = State()


# ─── Хелперы ──────────────────────────────────────────────────────────────────

def _ik(*rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _conf_emoji(c: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(c, "⚪")


def _risk_emoji(r: str) -> str:
    return {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(r, "⚪")


def _rec_emoji(r: str) -> str:
    return {"BUY YES": "✅", "BUY NO": "✅", "SKIP": "⏭️"}.get(r, "⏭️")


def _fmt_pct(v: float) -> str:
    return f"{v:.0%}"


def _fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def _market_short(q: str, n: int = 40) -> str:
    return q[:n] + "…" if len(q) > n else q


def _market_card(market: dict, analysis: dict) -> str:
    q        = market.get("question", "—")
    orig_q   = market.get("question_original", "")   # оригинал если был переведён
    end_date = market.get("endDate", "")[:10] or "—"
    yes_p    = analysis["yes_price"]
    no_p     = analysis["no_price"]
    vol      = analysis["volume_24h"]
    liq      = analysis["liquidity"]
    rec      = analysis["recommendation"]
    conf     = analysis["confidence"]
    risk     = analysis.get("risk", "MEDIUM")
    edge     = analysis.get("edge", "0%")

    # Поля глубокого анализа
    main_thesis         = analysis.get("main_thesis", "")
    probability_verdict = analysis.get("probability_verdict", "")
    yes_scenario        = analysis.get("yes_scenario", "")
    no_scenario         = analysis.get("no_scenario", "")
    key_risk            = analysis.get("key_risk", "")
    # Fallback: если старые поля
    if not main_thesis:
        main_thesis = analysis.get("reasoning", "")

    NL = "\n"

    # Строим рекомендационную строку
    rec_line = {
        "BUY YES": "🟢 <b>СТАВИТЬ YES</b>",
        "BUY NO":  "🔴 <b>СТАВИТЬ NO</b>",
        "SKIP":    "⏭️ <b>ПРОПУСТИТЬ</b>",
    }.get(rec, f"<b>{rec}</b>")

    conf_map = {"HIGH": "Высокая 🟢", "MEDIUM": "Средняя 🟡", "LOW": "Низкая 🔴"}
    risk_map = {"HIGH": "Высокий 🔴", "MEDIUM": "Средний 🟡", "LOW": "Низкий 🟢"}

    # Базовая часть (экранируем названия маркетов — могут содержать < > &)
    text = (
        f"📊 <b>{html.escape(q)}</b>" + NL
    )
    if orig_q and orig_q != q:
        text += f"<i>🔤 {html.escape(orig_q)}</i>" + NL
    text += (
        NL +
        f"💲 YES: <b>{_fmt_pct(yes_p)}</b>  |  NO: <b>{_fmt_pct(no_p)}</b>" + NL +
        f"💧 Ликвидность: <b>{_fmt_usd(liq)}</b>  |  📈 Объём 24ч: <b>{_fmt_usd(vol)}</b>" + NL +
        f"⏰ Закрытие: <b>{end_date}</b>" + NL +
        NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL +
        f"🎯 Рекомендация: {rec_line}" + NL +
        f"💡 Уверенность: {conf_map.get(conf, conf)}  |  "
        f"⚠️ Риск: {risk_map.get(risk, risk)}" + NL +
        f"📐 Edge: <code>{edge}</code>" + NL +
        "━━━━━━━━━━━━━━━━━━━━" + NL
    )

    # Глубокий анализ — экранируем AI-текст чтобы не сломать HTML-разметку
    def _e(s: str) -> str:
        return html.escape(s)

    if main_thesis:
        text += NL + "🧠 <b>Главный тезис:</b>" + NL + f"<i>{_e(main_thesis)}</i>" + NL

    if probability_verdict:
        text += NL + "⚖️ <b>Оценка рынка:</b>" + NL + f"<i>{_e(probability_verdict)}</i>" + NL

    if yes_scenario:
        text += NL + "✅ <b>YES победит если:</b>" + NL + f"<i>{_e(yes_scenario)}</i>" + NL

    if no_scenario:
        text += NL + "❌ <b>NO победит если:</b>" + NL + f"<i>{_e(no_scenario)}</i>" + NL

    if key_risk:
        text += NL + "🚨 <b>Ключевой риск:</b>" + NL + f"<i>{_e(key_risk)}</i>" + NL

    return text


def _get_token_ids(market: dict) -> dict:
    """{'yes': token_id, 'no': token_id}"""
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


def _market_kb(
    sk: int, market: dict, analysis: dict,
    default_bet: float, can_trade: bool,
) -> InlineKeyboardMarkup:
    tokens    = _get_token_ids(market)
    yes_tid   = tokens.get("yes", "")
    no_tid    = tokens.get("no", "")
    bet_str   = f"{default_bet:.0f}"
    yes_short = yes_tid[:20] if yes_tid else ""
    no_short  = no_tid[:20] if no_tid else ""

    rows = []
    if can_trade and yes_tid and no_tid:
        rows.append([
            _btn(f"BUY YES ${bet_str}", f"pm:buy:{sk}:yes:{yes_short}:{bet_str}"),
            _btn(f"BUY NO ${bet_str}",  f"pm:buy:{sk}:no:{no_short}:{bet_str}"),
        ])
        rows.append([_btn("💰 Своя сумма", f"pm:custom:{sk}:yes:{yes_short}")])
    elif not can_trade:
        rows.append([_btn("👛 Нужен кошелёк с USDC", "pm:wallet_info")])

    rows.append([_btn("🔔 Уведомить при изменении", f"pm:set_alert:{sk}")])
    rows.append([_btn("🔙 К списку", "pm:trending:0"), _btn("📊 Polymarket", "pm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_edit(cb: CallbackQuery, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e1:
        log.debug(f"_safe_edit edit_text failed: {e1}")
        try:
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception as e2:
            log.warning(f"_safe_edit answer also failed: {e2} | text[:100]={text[:100]}")


async def _get_user_wallet_balance(user_id: int) -> tuple[Optional[dict], float]:
    """Возвращает (wallet_row, balance). Wallet = None если не создан."""
    wallet = await db.poly_wallet_get(user_id)
    if not wallet:
        return None, 0.0
    balance = await wallet_service.get_usdc_balance(wallet["address"])
    return wallet, balance


# ─── Регистрация ──────────────────────────────────────────────────────────────

def register_poly_handlers(
    dp: Dispatcher,
    bot: Bot,
    um,
    config,
    poly: PolymarketService,
):
    def is_admin(uid: int) -> bool:
        return uid in config.ADMIN_IDS

    def _trading_enabled() -> bool:
        """Торговля доступна если задан WALLET_ENCRYPTION_KEY (кастодиальные)
        ИЛИ POLY_PRIVATE_KEY (admin-кошелёк)."""
        return wallet_service.is_configured() or poly.is_trading_enabled()

    # ─── Главное меню (команда + callback) ────────────────────────────────

    async def _show_menu(reply_fn, user_id: int):
        """Формирует главное меню в зависимости от состояния кошелька."""
        user = await um.get_or_create(user_id)
        has, _ = user.check_access()
        if not has:
            await reply_fn("❌ Нужна подписка для доступа к Polymarket.")
            return

        wallet, balance = await _get_user_wallet_balance(user_id)
        NL = "\n"

        if not wallet:
            # Онбординг: кошелька нет
            text = (
                "📊 <b>Polymarket — Prediction Market</b>" + NL + NL +
                "Здесь ты торгуешь на предсказаниях:\n"
                "выборы, крипта, спорт, геополитика." + NL + NL +
                "AI анализирует маркеты и подсказывает лучшие сделки.\n"
                "Ставки прямо здесь — без регистраций." + NL + NL +
                "👇 Создай кошелёк чтобы начать:"
            )
            kb = _ik(
                [_btn("🚀 Создать кошелёк", "pm:create_wallet")],
                [_btn("🔥 Смотреть маркеты", "pm:trending:0")],
                [_btn("🔙 Главное меню", "back_main")],
            )
        elif wallet and balance < 0.5 and wallet_service.is_configured():
            # Кошелёк есть, баланс нулевой → подсказка пополнить
            addr = wallet["address"]
            text = (
                "📊 <b>Polymarket</b>" + NL + NL +
                f"👛 Кошелёк: <code>{addr}</code>" + NL +
                f"💰 Баланс: <b>${balance:.2f} USDC</b>" + NL + NL +
                "⚠️ Пополни баланс чтобы делать ставки.\n"
                "<i>Отправь USDC (сеть Polygon) на адрес выше.</i>"
            )
            kb = _ik(
                [_btn("🔄 Проверить баланс", "pm:check_balance")],
                [_btn("❓ Как купить USDC?", "pm:howto_usdc")],
                [_btn("🔥 Смотреть маркеты", "pm:trending:0")],
                [_btn("🔙 Главное меню", "back_main")],
            )
        else:
            # Нормальное меню
            bal_str = f"${balance:.2f} USDC" if wallet else "—"
            trade_info = (
                f"\n💰 Баланс: <b>{bal_str}</b>"
                if wallet and wallet_service.is_configured()
                else "\n🔒 <i>Торговля: только просмотр (WALLET_ENCRYPTION_KEY не задан)</i>"
                if not wallet_service.is_configured() and not is_admin(user_id)
                else ""
            )
            text = "📊 <b>Polymarket — Prediction Market</b>" + trade_info
            rows = [
                [_btn("🔥 Трендовые маркеты", "pm:trending:0")],
                [_btn("🔍 Поиск маркета", "pm:search"), _btn("💼 Портфель", "pm:portfolio")],
                [_btn("🔔 Мои алерты", "pm:alerts_list"), _btn("⚙️ Настройки", "pm:settings")],
            ]
            if wallet and wallet_service.is_configured():
                rows.append([_btn("👛 Мой кошелёк", "pm:wallet_info")])
            rows.append([_btn("🔙 Главное меню", "back_main")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)

        await reply_fn(text, parse_mode="HTML", reply_markup=kb)

    @dp.message(Command("poly"))
    async def cmd_poly(msg: Message):
        await _show_menu(msg.answer, msg.from_user.id)

    @dp.callback_query(F.data == "pm:menu")
    async def cb_menu(cb: CallbackQuery):
        await cb.answer()
        await _show_menu(
            lambda text, **kw: cb.message.edit_text(text, **kw),
            cb.from_user.id,
        )

    # ─── Онбординг: создание кошелька ─────────────────────────────────────

    @dp.callback_query(F.data == "pm:create_wallet")
    async def cb_create_wallet(cb: CallbackQuery):
        await cb.answer()
        if not wallet_service.is_configured():
            await cb.answer("⚠️ WALLET_ENCRYPTION_KEY не задан (спросите администратора).", show_alert=True)
            return

        # Проверить — вдруг уже есть
        existing = await db.poly_wallet_get(cb.from_user.id)
        if existing:
            await _safe_edit(
                cb,
                f"👛 Кошелёк уже существует:\n<code>{existing['address']}</code>",
                _ik([_btn("🔙 Polymarket", "pm:menu")]),
            )
            return

        try:
            w = wallet_service.generate_wallet()
            enc = wallet_service.encrypt_key(w["private_key"])
            await db.poly_wallet_create(cb.from_user.id, w["address"], enc)
        except Exception as e:
            await _safe_edit(cb, f"❌ Ошибка создания кошелька: {e}")
            return

        NL = "\n"
        text = (
            "✅ <b>Кошелёк создан!</b>" + NL + NL +
            "Твой адрес (Polygon):" + NL +
            f"<code>{w['address']}</code>" + NL + NL +
            "Пополни его в USDC (сеть Polygon):" + NL +
            "1. Купи USDC на Bybit, OKX или Binance" + NL +
            "2. Выведи на этот адрес, сеть: <b>Polygon</b>" + NL +
            "3. Минимум $1 USDC" + NL + NL +
            "⏱ После пополнения нажми «Проверить баланс»"
        )
        kb = _ik(
            [_btn("🔄 Проверить баланс", "pm:check_balance")],
            [_btn("❓ Как купить USDC?", "pm:howto_usdc")],
        )
        await _safe_edit(cb, text, kb)

    @dp.callback_query(F.data == "pm:howto_usdc")
    async def cb_howto(cb: CallbackQuery):
        await cb.answer()
        wallet = await db.poly_wallet_get(cb.from_user.id)
        addr   = wallet["address"] if wallet else "создай кошелёк"
        NL = "\n"
        text = (
            "📖 <b>Как пополнить за 5 минут:</b>" + NL + NL +
            "1. Зайди на Bybit.com или OKX.com" + NL +
            "2. Купи USDC любым способом" + NL +
            "3. Нажми «Вывод» → сеть <b>Polygon</b>" + NL +
            f"4. Адрес получателя:" + NL +
            f"   <code>{addr}</code>" + NL +
            "5. Подтверди — придёт за 1-2 минуты" + NL + NL +
            "⚠️ Обязательно выбирай сеть <b>Polygon (MATIC)</b>,\n"
            "иначе деньги потеряются!"
        )
        kb = _ik(
            [_btn("✅ Понял, проверить баланс", "pm:check_balance")],
            [_btn("🔙 Polymarket", "pm:menu")],
        )
        await _safe_edit(cb, text, kb)

    @dp.callback_query(F.data == "pm:check_balance")
    async def cb_check_balance(cb: CallbackQuery):
        await cb.answer()
        wallet = await db.poly_wallet_get(cb.from_user.id)
        if not wallet:
            await cb.answer("⚠️ Сначала создай кошелёк.", show_alert=True)
            return

        await _safe_edit(cb, "⏳ Проверяем баланс...", None)
        balance = await wallet_service.get_usdc_balance(wallet["address"])
        NL = "\n"

        if balance >= 0.5:
            text = (
                f"💰 <b>Баланс: ${balance:.2f} USDC</b>" + NL + NL +
                "Всё готово! Можешь делать ставки."
            )
            kb = _ik(
                [_btn("🔥 Показать топ маркеты", "pm:trending:0")],
                [_btn("🔙 Polymarket", "pm:menu")],
            )
        else:
            text = (
                f"💰 Баланс: <b>${balance:.2f} USDC</b>" + NL + NL +
                f"👛 <code>{wallet['address']}</code>" + NL + NL +
                "Баланс ещё не пополнен.\n"
                "<i>Отправь USDC на адрес выше (сеть Polygon).</i>"
            )
            kb = _ik(
                [_btn("🔄 Обновить", "pm:check_balance")],
                [_btn("❓ Инструкция", "pm:howto_usdc")],
                [_btn("🔥 Смотреть маркеты", "pm:trending:0")],
            )
        await _safe_edit(cb, text, kb)

    @dp.callback_query(F.data == "pm:wallet_info")
    async def cb_wallet_info(cb: CallbackQuery):
        await cb.answer()
        wallet = await db.poly_wallet_get(cb.from_user.id)
        if not wallet:
            await cb.answer("⚠️ Кошелёк не создан.", show_alert=True)
            return
        balance = await wallet_service.get_usdc_balance(wallet["address"])
        NL = "\n"
        text = (
            "👛 <b>Твой кошелёк</b>" + NL + NL +
            f"Адрес: <code>{wallet['address']}</code>" + NL +
            f"Баланс: <b>${balance:.2f} USDC</b>" + NL + NL +
            "Для пополнения отправь USDC на этот адрес\n(сеть Polygon)"
        )
        kb = _ik(
            [_btn("🔄 Обновить баланс", "pm:check_balance")],
            [_btn("🔙 Polymarket", "pm:menu")],
        )
        await _safe_edit(cb, text, kb)

    # ─── Трендовые маркеты ────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:trending:"))
    async def cb_trending(cb: CallbackQuery):
        await cb.answer()
        user = await um.get_or_create(cb.from_user.id)
        has, _ = user.check_access()
        if not has:
            await cb.answer("❌ Нужна подписка.", show_alert=True)
            return

        offset = int(cb.data.split(":")[2])
        try:
            markets = await poly.get_trending_markets(limit=10, offset=offset)
        except Exception as e:
            await _safe_edit(cb, f"⚠️ Polymarket API недоступен: {e}")
            return

        if not markets:
            await _safe_edit(cb, "📭 Маркеты не найдены.",
                             _ik([_btn("🔙 Polymarket", "pm:menu")]))
            return

        rows = []
        for m in markets:
            q        = _market_short(m.get("question", "—"), 40)
            sk       = _get_short_key(m.get("id", ""))
            analysis = analyze_market(m)
            pct      = f"{analysis['yes_price']:.0%}"
            em       = "✅" if analysis["recommendation"] != "SKIP" else "📊"
            rows.append([_btn(f"{em} {q} | YES {pct}", f"pm:view:{sk}")])

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

    # ─── Поиск маркета ────────────────────────────────────────────────────

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
            await msg.answer(
                f"📭 По запросу «{query}» ничего не найдено.",
                reply_markup=_ik([_btn("🔙 Polymarket", "pm:menu")]),
            )
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

    # ─── Карточка маркета ─────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:view:"))
    async def cb_view_market(cb: CallbackQuery):
        # Не отвечаем сразу — сначала проверяем данные, потом отвечаем один раз
        try:
            sk = int(cb.data.split(":")[2])
        except (ValueError, IndexError):
            await cb.answer("⚠️ Некорректный запрос.", show_alert=True)
            return

        condition_id = get_condition_id(sk)
        if not condition_id:
            # Маркет не найден в памяти — бот был перезапущен, список устарел
            await cb.answer(
                "⚠️ Список маркетов устарел. Обновите список.",
                show_alert=True,
            )
            try:
                await cb.message.edit_reply_markup(
                    reply_markup=_ik([_btn("🔄 Обновить список", "pm:trending:0"),
                                      _btn("🔙 Polymarket", "pm:menu")])
                )
            except Exception:
                pass
            return

        await cb.answer()  # снимаем спиннер только после проверки

        try:
            market = await poly.get_market_by_id(condition_id)
        except Exception as e:
            log.warning(f"get_market_by_id {condition_id}: {e}")
            market = None

        if not market:
            await _safe_edit(
                cb,
                "⚠️ Не удалось загрузить маркет.\n<i>Polymarket API недоступен.</i>",
                _ik([_btn("🔄 Попробовать снова", f"pm:view:{sk}"),
                     _btn("🔙 К списку", "pm:trending:0")]),
            )
            return

        await _safe_edit(cb, "⏳ <b>AI анализирует маркет...</b>", None)

        try:
            # Запускаем перевод и анализ параллельно — оба идут в Groq независимо
            market, analysis = await asyncio.gather(
                translate_market(market),
                poly.analyze_market(market),
            )
        except Exception as e:
            log.error(f"analyze_market {condition_id}: {e}")
            await _safe_edit(
                cb,
                f"⚠️ Ошибка анализа маркета: <code>{str(e)[:200]}</code>",
                _ik([_btn("🔄 Попробовать снова", f"pm:view:{sk}"),
                     _btn("🔙 К списку", "pm:trending:0")]),
            )
            return

        settings = await db.poly_get_settings(cb.from_user.id)
        default_bet = settings.get("default_bet", 5.0)

        # Можно ли торговать: есть кошелёк с балансом ИЛИ admin с POLY_PRIVATE_KEY
        wallet, balance = await _get_user_wallet_balance(cb.from_user.id)
        can_trade = (
            (wallet_service.is_configured() and wallet is not None and balance >= 1.0)
            or (is_admin(cb.from_user.id) and poly.is_trading_enabled())
        )

        text = _market_card(market, analysis)
        # Telegram limit: 4096 chars. Trim if needed.
        if len(text) > 4000:
            text = text[:3990] + "\n<i>…</i>"
        kb   = _market_kb(sk, market, analysis, default_bet, can_trade)
        await _safe_edit(cb, text, kb)

    # ─── Подтверждение покупки ────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:buy:"))
    async def cb_buy(cb: CallbackQuery):
        await cb.answer()
        uid = cb.from_user.id

        # Проверяем доступ к торговле
        wallet, balance = await _get_user_wallet_balance(uid)
        admin_trade = is_admin(uid) and poly.is_trading_enabled()
        user_trade  = wallet_service.is_configured() and wallet is not None and balance >= 1.0
        if not admin_trade and not user_trade:
            await cb.answer(
                "👛 Пополни кошелёк USDC (минимум $1) чтобы торговать.",
                show_alert=True,
            )
            return

        parts     = cb.data.split(":")
        # pm:buy:{sk}:{side}:{token_short}:{amount}
        sk        = int(parts[2])
        side      = parts[3].upper()
        tok_short = parts[4]
        amount    = float(parts[5])

        condition_id = get_condition_id(sk)
        market = await poly.get_market_by_id(condition_id) if condition_id else None
        q = market.get("question", "—") if market else "—"

        token_id = ""
        if market:
            tids = _get_token_ids(market)
            token_id = tids.get(side.lower(), tok_short)

        yes_, no_ = (0.5, 0.5)
        if market:
            _a = analyze_market(market)
            yes_, no_ = _a["yes_price"], _a["no_price"]

        price   = yes_ if side == "YES" else no_
        shares  = round(amount / price, 2) if price > 0 else 0
        profit  = round(shares - amount, 2)
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

    # ─── Исполнение ставки ────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:confirm:"))
    async def cb_confirm(cb: CallbackQuery):
        await cb.answer()
        uid = cb.from_user.id

        # Антиспам
        now = time.time()
        if now - _bet_ts.get(uid, 0) < _BET_COOLDOWN:
            await cb.answer("⏳ Подождите несколько секунд.", show_alert=True)
            return
        _bet_ts[uid] = now

        parts     = cb.data.split(":")
        sk        = int(parts[2])
        side      = parts[3].upper()
        tok_short = parts[4]
        amount    = float(parts[5])

        condition_id = get_condition_id(sk)
        market = await poly.get_market_by_id(condition_id) if condition_id else None

        token_id = tok_short
        if market:
            tids = _get_token_ids(market)
            token_id = tids.get(side.lower(), tok_short)

        await _safe_edit(cb, "⏳ <b>Размещаем ставку...</b>", None)

        # Определяем источник торговли
        wallet, balance = await _get_user_wallet_balance(uid)
        admin_trade = is_admin(uid) and poly.is_trading_enabled()
        user_trade  = wallet_service.is_configured() and wallet is not None and balance >= amount

        try:
            if user_trade:
                # Кастодиальный кошелёк пользователя
                enc_key = wallet["encrypted_key"]
                private_key = wallet_service.decrypt_key(enc_key)
                result = await poly.place_bet_for_user(private_key, token_id, amount)
                del private_key   # немедленно стираем из памяти
            elif admin_trade:
                result = await poly.place_bet(token_id, amount)
            else:
                await _safe_edit(cb, "❌ Недостаточно средств или кошелёк не настроен.",
                                 _ik([_btn("🔙 Назад", f"pm:view:{sk}")]))
                return
        except RuntimeError as e:
            await _safe_edit(cb, f"❌ <b>Ошибка:</b> {e}",
                             _ik([_btn("🔙 Назад", f"pm:view:{sk}")]))
            return
        except Exception as e:
            err = str(e)
            if "insufficient" in err.lower() or "balance" in err.lower():
                msg_text = "❌ Недостаточно USDC на кошельке."
            elif "closed" in err.lower():
                msg_text = "⏰ Этот маркет уже закрыт."
            else:
                msg_text = f"❌ Ошибка API: {err[:200]}"
            await _safe_edit(cb, msg_text, _ik([_btn("🔙 Назад", f"pm:view:{sk}")]))
            return

        order_id = ""
        if isinstance(result, dict):
            order_id = result.get("orderId", result.get("id", ""))
        q = market.get("question", "—") if market else "—"

        # Сохраняем ставку
        yes_, no_ = _parse_prices(market) if market else (0.5, 0.5)
        entry_price = yes_ if side == "YES" else no_
        await db.poly_save_bet(
            user_id=uid, market_id=condition_id or "",
            question=q, side=side, amount=amount,
            shares=round(amount / entry_price, 3) if entry_price > 0 else 0,
            price=entry_price, order_id=order_id,
        )

        NL = "\n"
        await _safe_edit(cb,
            "✅ <b>Ставка размещена!</b>" + NL + NL +
            f"Маркет: <b>{_market_short(q, 50)}</b>" + NL +
            f"Сторона: <b>{side}</b>  Сумма: <b>${amount:.2f} USDC</b>" + NL +
            (f"Order ID: <code>{order_id[:40]}</code>" if order_id else ""),
            _ik(
                [_btn("💼 Портфель", "pm:portfolio")],
                [_btn("🔙 Polymarket", "pm:menu")],
            ),
        )

    # ─── Своя сумма ───────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:custom:"))
    async def cb_custom_amount(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        parts = cb.data.split(":")
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

    # ─── Портфель ─────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "pm:portfolio")
    async def cb_portfolio(cb: CallbackQuery):
        await cb.answer()
        uid = cb.from_user.id

        wallet, balance = await _get_user_wallet_balance(uid)
        bets = await db.poly_get_bets(uid, limit=10)

        NL = "\n"
        lines = ["💼 <b>Портфель Polymarket</b>" + NL]

        if wallet:
            lines.append(f"💰 Баланс USDC: <b>${balance:.2f}</b>" + NL)

        if not bets:
            lines.append("📭 Ставок пока нет.")
        else:
            total_in = sum(b.get("amount_usdc", 0) for b in bets)
            lines.append(f"📊 Последние {len(bets)} ставок (всего вложено: <b>{_fmt_usd(total_in)}</b>)" + NL)
            for i, b in enumerate(bets, 1):
                q    = _market_short(b.get("question", "—"), 38)
                side = b.get("side", "—")
                amt  = b.get("amount_usdc", 0)
                price = b.get("price", 0)
                dt   = str(b.get("created_at", ""))
                dt_str = __import__("datetime").datetime.fromtimestamp(float(dt)).strftime("%d.%m") if dt else "—"
                em   = "🟢" if side == "YES" else "🔴"
                price_str = f" @ {price:.0%}" if price > 0 else ""
                lines.append(f"{i}. {em} <b>{q}</b>" + NL +
                             f"   {side}{price_str} | ${amt:.2f} | {dt_str}")

        text = NL.join(lines)
        kb = _ik(
            [_btn("🔄 Обновить", "pm:portfolio"), _btn("🔥 Новые маркеты", "pm:trending:0")],
            [_btn("🔙 Polymarket", "pm:menu")],
        )
        await _safe_edit(cb, text, kb)

    # ─── Ценовые алерты ───────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("pm:set_alert:"))
    async def cb_set_alert(cb: CallbackQuery):
        await cb.answer()
        sk = int(cb.data.split(":")[2])
        condition_id = get_condition_id(sk)
        if not condition_id:
            await cb.answer("⚠️ Маркет не найден.", show_alert=True)
            return
        NL = "\n"
        text = (
            "🔔 <b>Уведомить при изменении цены</b>" + NL + NL +
            "Выбери порог срабатывания:"
        )
        kb = _ik(
            [_btn("±5%",  f"pm:alert_save:{sk}:5"),
             _btn("±10%", f"pm:alert_save:{sk}:10"),
             _btn("±20%", f"pm:alert_save:{sk}:20")],
            [_btn("❌ Отмена", f"pm:view:{sk}")],
        )
        await _safe_edit(cb, text, kb)

    @dp.callback_query(F.data.startswith("pm:alert_save:"))
    async def cb_alert_save(cb: CallbackQuery):
        await cb.answer()
        parts = cb.data.split(":")
        sk        = int(parts[2])
        threshold = float(parts[3])
        uid = cb.from_user.id

        condition_id = get_condition_id(sk)
        market = await poly.get_market_by_id(condition_id) if condition_id else None
        if not market:
            await cb.answer("⚠️ Маркет не найден.", show_alert=True)
            return

        yes_now, _ = _parse_prices(market)
        q = market.get("question", "—")

        await db.poly_alert_add(uid, condition_id, q, yes_now, threshold)

        await cb.answer(f"✅ Уведомление ±{threshold:.0f}% установлено!", show_alert=True)
        await _safe_edit(
            cb,
            f"🔔 <b>Алерт установлен</b>\n\n"
            f"Маркет: {_market_short(q, 50)}\n"
            f"Текущий YES: {yes_now:.0%}\n"
            f"Уведомлю когда изменится на ±{threshold:.0f}%",
            _ik([_btn("🔙 Polymarket", "pm:menu")]),
        )

    @dp.callback_query(F.data == "pm:alerts_list")
    async def cb_alerts_list(cb: CallbackQuery):
        await cb.answer()
        alerts = await db.poly_alert_get_user(cb.from_user.id)
        NL = "\n"
        if not alerts:
            await _safe_edit(
                cb, "🔔 Активных алертов нет.",
                _ik([_btn("🔥 Трендовые маркеты", "pm:trending:0"),
                     _btn("🔙 Polymarket", "pm:menu")]),
            )
            return

        lines = ["🔔 <b>Твои алерты</b>" + NL]
        rows  = []
        for a in alerts:
            q   = _market_short(a["question"], 35)
            thr = a["threshold"]
            yp  = a["yes_price"]
            lines.append(f"• {q}\n  YES был {yp:.0%}, порог ±{thr:.0f}%")
            rows.append([_btn(f"🗑 Удалить: {_market_short(q, 25)}", f"pm:alert_del:{a['id']}")])

        rows.append([_btn("🔙 Polymarket", "pm:menu")])
        await _safe_edit(cb, NL.join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    @dp.callback_query(F.data.startswith("pm:alert_del:"))
    async def cb_alert_del(cb: CallbackQuery):
        await cb.answer()
        alert_id = int(cb.data.split(":")[2])
        await db.poly_alert_delete(alert_id)
        await cb.answer("✅ Алерт удалён.", show_alert=True)
        # Обновляем список
        alerts = await db.poly_alert_get_user(cb.from_user.id)
        if not alerts:
            await _safe_edit(
                cb, "🔔 Активных алертов нет.",
                _ik([_btn("🔙 Polymarket", "pm:menu")]),
            )
        else:
            # Рекурсивно показываем обновлённый список через фейк-callback
            await cb_alerts_list(cb)

    # ─── Настройки ────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "pm:settings")
    async def cb_settings(cb: CallbackQuery):
        await cb.answer()
        uid = cb.from_user.id
        s = await db.poly_get_settings(uid)
        bet     = s.get("default_bet", 5.0)
        digest  = s.get("digest_on", 1)

        NL = "\n"
        text = (
            "⚙️ <b>Настройки Polymarket</b>" + NL + NL +
            f"💰 Сумма по умолчанию: <b>${bet:.1f} USDC</b>" + NL +
            f"🌅 Утренний дайджест: <b>{'вкл ✅' if digest else 'выкл ❌'}</b>"
        )
        kb = _ik(
            [_btn("$1", "pm:setbet:1"), _btn("$5", "pm:setbet:5"),
             _btn("$10", "pm:setbet:10"), _btn("$25", "pm:setbet:25")],
            [_btn("💬 Своя сумма", "pm:setbet:custom")],
            [_btn(f"🌅 Дайджест: {'выкл' if digest else 'вкл'}",
                  f"pm:digest:{'0' if digest else '1'}")],
            [_btn("👛 Мой кошелёк", "pm:wallet_info")],
            [_btn("🔙 Polymarket", "pm:menu")],
        )
        await _safe_edit(cb, text, kb)

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
            f"✅ Размер ставки: <b>${val:.1f} USDC</b>",
            parse_mode="HTML",
            reply_markup=_ik([_btn("🔙 Polymarket", "pm:menu")]),
        )

    @dp.callback_query(F.data.startswith("pm:digest:"))
    async def cb_digest_toggle(cb: CallbackQuery):
        await cb.answer()
        val = int(cb.data.split(":")[2])   # 0 или 1
        await db.poly_save_digest(cb.from_user.id, val)
        await cb.answer(f"✅ Дайджест {'включён' if val else 'выключён'}", show_alert=True)
        await cb_settings(cb)

    # ─── Вспомогательные ──────────────────────────────────────────────────

    @dp.callback_query(F.data == "pm:noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()
