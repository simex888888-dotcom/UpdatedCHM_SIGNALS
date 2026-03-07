"""
smc/scanner.py — Автономный SMC-сканер
Запускается как background asyncio task из register_handlers().
Сканирует пользователей с выбранной стратегией "SMC".
"""
import asyncio
import logging
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from .analyzer      import SMCAnalyzer, SMCConfig
from .signal_builder import build_smc_signal, SMCSignalResult

log = logging.getLogger("CHM.SMC.Scanner")

# Таймфреймы для SMC (HTF→MTF→LTF)
_SMC_TF_MAP = {
    "4H":  ("1D",  "4H",  "1H"),
    "1H":  ("4H",  "1H",  "15m"),
    "15m": ("1H",  "15m", "5m"),
}

# Антидубликат: hash → timestamp
_sent_signals: dict[str, float] = {}
_DEDUP_HOURS = 4  # часов между одинаковыми сигналами


def _fp(v: float) -> str:
    """Форматирует цену без научной нотации."""
    if v >= 10_000: return f"{v:,.0f}"
    if v >= 100:    return f"{v:,.1f}"
    if v >= 1:      return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _signal_text_smc(sig: SMCSignalResult) -> str:
    NL = "\n"
    is_long = sig.direction == "LONG"
    dir_line = ("🟢 <b>LONG — ПОКУПКА</b>" if is_long
                else "🔴 <b>SHORT — ПРОДАЖА</b>")
    stars = "⭐" * sig.score + "☆" * (5 - sig.score)

    confirmations_block = ""
    for label, passed in sig.confirmations:
        mark = "✅" if passed else "⬜"
        confirmations_block += mark + " " + label + NL

    def pct(t): return abs((t - sig.entry) / sig.entry * 100)

    return (
        dir_line + "  " + sig.grade + NL +
        "<b>" + sig.symbol + "</b>  " + stars + NL + NL +
        "📊 ТФ: " + sig.tf_ltf + " → " + sig.tf_mtf + " → " + sig.tf_htf + NL +
        "💰 Вход: <code>" + _fp(sig.entry_low) + " – " + _fp(sig.entry_high) + "</code>" + NL +
        "🛑 Стоп: <code>" + _fp(sig.sl) + "</code>  (-" + "{:.2f}".format(sig.risk_pct) + "%)" + NL + NL +
        "🎯 TP1: <code>" + _fp(sig.tp1) + "</code>  (+" + "{:.2f}".format(pct(sig.tp1)) + "%) — 33% позиции" + NL +
        "🎯 TP2: <code>" + _fp(sig.tp2) + "</code>  (+" + "{:.2f}".format(pct(sig.tp2)) + "%) — 50% позиции" + NL +
        "🏆 TP3: <code>" + _fp(sig.tp3) + "</code>  (+" + "{:.2f}".format(pct(sig.tp3)) + "%) — 17% позиции" + NL +
        "📐 R:R = 1:" + str(sig.rr) + NL + NL +
        "📋 ПОДТВЕРЖДЕНИЯ (" + str(sig.score) + "/5):" + NL +
        confirmations_block + NL +
        "🧠 ЛОГИКА ВХОДА:" + NL +
        sig.narrative + NL + NL +
        "⚡ <i>CHM Laboratory — SMC Strategy</i>"
    )


def _smc_keyboard(symbol: str) -> InlineKeyboardMarkup:
    from fetcher import OKXFetcher
    clean = symbol.replace("-SWAP", "").replace("-", "")
    tv_url = "https://www.tradingview.com/chart/?symbol=OKX:" + clean + ".P"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📈 График",      url=tv_url),
        InlineKeyboardButton(text="📊 Статистика",  callback_data="my_stats"),
    ]])


async def run_smc_scanner(
    bot:          "Bot",
    um,
    fetcher,
    interval_sec: int = 900,
    tf_key:       str = "1H",
) -> None:
    """
    Главный цикл SMC-сканера.
    Фильтрует пользователей у которых user.strategy == "SMC".
    """
    analyzer = SMCAnalyzer(SMCConfig())
    tf_htf, tf_mtf, tf_ltf = _SMC_TF_MAP.get(tf_key, ("4H", "1H", "15m"))
    log.info(f"SMC Scanner started: {tf_htf}/{tf_mtf}/{tf_ltf}, interval={interval_sec}s")

    while True:
        try:
            await _scan_cycle(bot, um, fetcher, analyzer,
                              tf_htf, tf_mtf, tf_ltf)
        except asyncio.CancelledError:
            log.info("SMC Scanner stopped.")
            return
        except Exception as e:
            log.error(f"SMC scan cycle error: {e}")
        await asyncio.sleep(interval_sec)


async def _scan_cycle(bot, um, fetcher, analyzer,
                      tf_htf, tf_mtf, tf_ltf) -> None:
    users = await um.get_active_users()
    # SMC users: strategy==SMC AND at least one scanner is on
    smc_users = [
        u for u in users
        if u.strategy == "SMC" and (
            u.active or
            getattr(u, "smc_long_active",  False) or
            getattr(u, "smc_short_active", False)
        )
    ]
    if not smc_users:
        return

    log.info(f"SMC scan: {len(smc_users)} SMC users, tf={tf_mtf}")
    base_cfg = SMCConfig()

    # Наименьший минимальный объём среди всех SMC-пользователей (чтобы покрыть всех)
    min_vol = min((u.get_smc_cfg().min_volume_usdt for u in smc_users), default=5_000_000)

    # Загружаем список монет
    try:
        import cache
        coins = await cache.get_coins()
        if not coins:
            coins = await fetcher.get_all_usdt_pairs(min_volume_usdt=min_vol)
    except Exception as e:
        log.warning(f"SMC: не удалось загрузить монеты: {e}")
        return

    coins = coins[:50]

    for symbol in coins:
        try:
            df_htf_data = await fetcher.get_candles(symbol, tf_htf, limit=200)
            df_mtf_data = await fetcher.get_candles(symbol, tf_mtf, limit=200)
            df_ltf_data = await fetcher.get_candles(symbol, tf_ltf, limit=200)
        except Exception:
            continue

        if df_htf_data is None or df_mtf_data is None:
            continue
        if len(df_htf_data) < 30 or len(df_mtf_data) < 30:
            continue

        try:
            analysis = analyzer.analyze(symbol, df_htf_data, df_mtf_data, df_ltf_data)
        except Exception as e:
            log.debug(f"SMC {symbol} analyze: {e}")
            continue

        # Отправляем каждому пользователю согласно его персональным фильтрам
        for user in smc_users:
            ucfg = user.get_smc_cfg()

            # Применяем пользовательский конфиг поверх базового
            cfg_obj = SMCConfig()
            cfg_obj.MIN_CONFIRMATIONS   = ucfg.min_confirmations
            cfg_obj.MIN_RR              = ucfg.min_rr
            cfg_obj.SL_BUFFER_PCT       = ucfg.sl_buffer_pct
            cfg_obj.FVG_ENABLED         = ucfg.fvg_enabled
            cfg_obj.CHOCH_ENABLED       = ucfg.choch_enabled
            cfg_obj.OB_USE_BREAKER      = ucfg.ob_use_breaker
            cfg_obj.OB_MAX_AGE_CANDLES  = ucfg.ob_max_age
            cfg_obj.SWEEP_CLOSE_REQUIRED = ucfg.sweep_close_req

            try:
                sig = build_smc_signal(symbol, analysis, cfg_obj,
                                       tf_htf=tf_htf, tf_mtf=tf_mtf, tf_ltf=tf_ltf)
            except Exception as e:
                log.debug(f"SMC {symbol} build {user.user_id}: {e}")
                continue

            if sig is None:
                continue

            # Определяем какие направления активны для этого пользователя
            both_on  = user.active and user.scan_mode == "smc_both"
            long_on  = getattr(user, "smc_long_active",  False)
            short_on = getattr(user, "smc_short_active", False)
            if both_on:
                pass   # принимаем любое направление
            elif long_on and not short_on:
                if sig.direction != "LONG":
                    continue
            elif short_on and not long_on:
                if sig.direction != "SHORT":
                    continue
            elif long_on and short_on:
                pass   # оба включены — принимаем всё
            else:
                continue  # ни один режим не включён

            # Антидубликат per-user
            sig_hash = f"{user.user_id}_{symbol}_{sig.direction}_{sig.score}"
            if time.time() - _sent_signals.get(sig_hash, 0) < _DEDUP_HOURS * 3600:
                continue
            _sent_signals[sig_hash] = time.time()

            text = _signal_text_smc(sig)
            try:
                await bot.send_message(
                    user.user_id, text,
                    parse_mode="HTML",
                    reply_markup=_smc_keyboard(sig.symbol),
                    protect_content=True,
                )
                log.info(f"SMC ✅ {symbol} {sig.direction} {sig.grade} → @{user.username or user.user_id}")
            except TelegramForbiddenError:
                user.long_active = user.short_active = user.active = False
                user.smc_long_active = user.smc_short_active = False
                await um.save(user)
            except Exception as e:
                log.error(f"SMC send {user.user_id}: {e}")

        await asyncio.sleep(0.1)
