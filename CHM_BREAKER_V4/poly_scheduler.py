"""
poly_scheduler.py — фоновые задачи Polymarket.

Два asyncio-цикла (без внешних зависимостей типа APScheduler):
  digest_loop()  — ежедневный AI-дайджест топ-3 маркетов в 09:00 UTC
  alerts_loop()  — проверка ценовых алертов каждые 15 мин

Подключается в bot.py через asyncio.gather().
"""

import asyncio
import datetime
import logging
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from polymarket_service import PolymarketService, _parse_prices, _get_short_key

log = logging.getLogger("CHM.PolyScheduler")

_DIGEST_HOUR   = 9      # UTC
_DIGEST_TOP_N  = 20     # маркетов для анализа
_DIGEST_SEND_N = 3      # итого в дайджесте
_ALERTS_EVERY  = 900    # секунд (15 мин)


# ─── Digest helpers ───────────────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _conf_emoji(c: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(c, "⚪")


def _rec_emoji(r: str) -> str:
    return "✅" if r != "SKIP" else "⏭️"


async def _build_digest(poly: PolymarketService) -> list[dict]:
    """Возвращает топ-N маркетов: confidence != LOW, recommendation != SKIP."""
    try:
        markets = await poly.get_trending_markets(limit=_DIGEST_TOP_N)
    except Exception as e:
        log.warning(f"digest build: get_trending_markets: {e}")
        return []

    results = []
    for m in markets:
        try:
            analysis = await poly.analyze_market(m)
            if analysis.get("recommendation") == "SKIP":
                continue
            if analysis.get("confidence") == "LOW":
                continue
            results.append({
                "market_id":      m.get("id", ""),
                "question":       m.get("question", ""),
                "yes_price":      analysis["yes_price"],
                "recommendation": analysis["recommendation"],
                "confidence":     analysis["confidence"],
                "reasoning":      analysis.get("reasoning", ""),
            })
        except Exception:
            continue

    # HIGH первыми, затем по убыванию объёма (yes_price как прокси)
    results.sort(key=lambda x: (x["confidence"] != "HIGH",))
    return results[:_DIGEST_SEND_N]


async def send_daily_digest(bot: Bot, poly: PolymarketService, um) -> int:
    """Формирует и рассылает дайджест. Возвращает число получателей."""
    today = _today_utc()
    top = await _build_digest(poly)
    if not top:
        log.info("digest: нет подходящих маркетов")
        return 0

    lines = ["🌅 <b>Доброе утро! Топ маркеты на сегодня:</b>\n"]
    for i, m in enumerate(top, 1):
        q = m["question"]
        q_short = q[:65] + ("…" if len(q) > 65 else "")
        rec  = m["recommendation"]
        conf = m["confidence"]
        yes_p = m["yes_price"]
        reason = m["reasoning"]
        lines.append(
            f"{i}. 📊 <b>{q_short}</b>\n"
            f"   YES {yes_p:.0%} · <b>{rec}</b> {_rec_emoji(rec)} · {conf} {_conf_emoji(conf)}\n"
            f"   <i>«{reason}»</i>\n"
        )
    text = "\n".join(lines)

    users = await um.all_users()
    now   = time.time()
    sent  = 0
    for user in users:
        if user.sub_status not in ("trial", "active"):
            continue
        if user.sub_expires < now:
            continue
        wallet = await db.poly_wallet_get(user.user_id)
        if not wallet:
            continue
        if await db.poly_digest_sent_today(user.user_id, today):
            continue
        try:
            await bot.send_message(user.user_id, text, parse_mode="HTML")
            await db.poly_digest_mark_sent(user.user_id, today)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    log.info(f"digest: отправлено {sent} пользователям")
    return sent


async def digest_loop(bot: Bot, poly: PolymarketService, um):
    """Фоновый цикл: проверяет каждые 30 мин, отправляет дайджест в 09:00 UTC."""
    log.info("PolyScheduler digest_loop запущен")
    await asyncio.sleep(120)   # дать боту время запуститься
    while True:
        try:
            now = datetime.datetime.utcnow()
            if now.hour == _DIGEST_HOUR and now.minute < 30:
                await send_daily_digest(bot, poly, um)
        except Exception as e:
            log.warning(f"digest_loop error: {e}")
        await asyncio.sleep(1800)   # 30 минут


# ─── Alerts ───────────────────────────────────────────────────────────────────

async def check_price_alerts(bot: Bot, poly: PolymarketService):
    """Проверяет все активные алерты и шлёт уведомления при срабатывании."""
    try:
        alerts = await db.poly_alerts_all_active()
    except Exception as e:
        log.warning(f"alerts: DB error: {e}")
        return

    if not alerts:
        return

    # Группируем по market_id чтобы запрашивать каждый маркет один раз
    by_market: dict[str, list] = {}
    for a in alerts:
        by_market.setdefault(a["market_id"], []).append(a)

    notified = 0
    for market_id, market_alerts in by_market.items():
        try:
            market = await poly.get_market_by_id(market_id)
            if not market:
                continue
            yes_now, _ = _parse_prices(market)

            for a in market_alerts:
                base  = a["yes_price"]
                thr   = a["threshold"]          # процент, напр. 5.0
                if base <= 0:
                    continue
                change_pct = abs(yes_now - base) / base * 100
                if change_pct < thr:
                    continue

                direction = "📈" if yes_now > base else "📉"
                delta_str = f"{'+' if yes_now > base else ''}{yes_now - base:.0%}"
                q_short   = a["question"][:60] + ("…" if len(a["question"]) > 60 else "")
                text = (
                    f"⚡ <b>Алерт сработал!</b>\n\n"
                    f"📊 {q_short}\n"
                    f"YES: было {base:.0%} → стало {yes_now:.0%} ({delta_str}) {direction}\n\n"
                    f"<i>Порог ±{thr:.0f}% достигнут</i>"
                )
                sk = _get_short_key(market_id)
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📊 Открыть маркет", callback_data=f"pm:view:{sk}"),
                    InlineKeyboardButton(text="🔕 Удалить алерт", callback_data=f"pm:alert_del:{a['id']}"),
                ]])
                try:
                    await bot.send_message(
                        a["user_id"], text, parse_mode="HTML", reply_markup=kb
                    )
                    await db.poly_alert_delete(a["id"])
                    notified += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"alerts: market {market_id}: {e}")

    if notified:
        log.info(f"alerts: сработало {notified} алертов")


async def alerts_loop(bot: Bot, poly: PolymarketService):
    """Фоновый цикл алертов: каждые 15 минут."""
    log.info("PolyScheduler alerts_loop запущен")
    await asyncio.sleep(180)
    while True:
        try:
            await check_price_alerts(bot, poly)
        except Exception as e:
            log.warning(f"alerts_loop error: {e}")
        await asyncio.sleep(_ALERTS_EVERY)
