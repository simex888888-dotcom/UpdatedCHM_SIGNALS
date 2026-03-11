"""
pd_runner.py — главный цикл обработки Памп/Дамп.

Создаёт MarketMonitor, принимает события из очереди,
запускает все анализаторы, агрегирует результат,
отправляет алерты подписанным пользователям.
"""

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import database as db
from pump_dump import (
    anomaly_detector   as anomaly,
    orderbook_analyzer as orderbook,
    hidden_signals,
    indicators,
    signal_aggregator  as aggregator,
)
from pump_dump.market_monitor  import MarketMonitor, MarketEvent
from pump_dump.ml_model        import get_model, build_feature_vector
from pump_dump.pd_handlers     import set_current_scores
from pump_dump.signal_aggregator import format_alert
from watermark import wm_inject

log = logging.getLogger("CHM.PD.Runner")

# Очередь событий
_QUEUE_MAX = 500


class PDRunner:
    def __init__(self, bot: Bot, db_path: str):
        self.bot     = bot
        self.db_path = db_path
        self.queue   = asyncio.Queue(maxsize=_QUEUE_MAX)
        self.monitor = MarketMonitor(self.queue)
        self._running = False
        self._scores: dict[str, float] = {}

    def is_running(self) -> bool:
        return self._running

    async def run_forever(self):
        self._running = True
        log.info("🚀 PDRunner запускается…")
        await asyncio.gather(
            self.monitor.run_forever(),
            self._process_loop(),
            self._retrain_loop(),
            self._outcome_loop(),
        )

    # ── Основной цикл обработки событий ──────────────────────────────────────

    async def _process_loop(self):
        while True:
            try:
                event: MarketEvent = await asyncio.wait_for(
                    self.queue.get(), timeout=5.0
                )
                await self._process_event(event)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.warning(f"PDRunner process: {e}", exc_info=True)

    async def _process_event(self, event: MarketEvent):
        sym = event.symbol
        df  = event.candles

        if len(df) < 30:
            return

        # Параллельный анализ всех слоёв
        an   = anomaly.detect(df)
        ob   = orderbook.analyze(event.orderbook, an.price_change_1m)
        ind  = indicators.analyze(df)
        hs   = await hidden_signals.analyze(sym, df, self.monitor.get_symbols())

        # Обновляем score для /pd_top даже если нет сигнала
        features = build_feature_vector(an, ob, hs, ind)
        ml_res   = get_model().predict(features)
        raw_score = (
            (an.volume_double_cond * 0.15) +
            (an.price_spike        * 0.10) +
            (an.cvd_signal         * 0.15) +
            (ob.imbalance_signal   * 0.10) +
            (ob.spread_signal      * 0.10) +
            (hs.funding_signal     * 0.15) +
            (hs.oi_signal          * 0.10) +
            ((ml_res is not None and ml_res.predicted != "NEUTRAL") * 0.15)
        ) * 100
        self._scores[sym] = round(raw_score, 1)
        set_current_scores({sym: self._scores[sym]})

        # Агрегируем для принятия решения об отправке
        signal = aggregator.aggregate(sym, event.last_price, an, ob, hs, ind)
        if signal is None:
            return

        log.info(f"🎯 PD сигнал: {sym} {signal.direction} {signal.score:.0f}%")
        await self._broadcast_signal(signal)
        await self._save_signal(signal, features)

    # ── Рассылка сигнала ─────────────────────────────────────────────────────

    async def _broadcast_signal(self, signal):
        from pump_dump.signal_aggregator import format_alert
        users = await db.db_pd_subscribers(min_threshold=int(signal.score))
        if not users:
            return
        text = format_alert(signal)
        for uid in users:
            try:
                wm_text = wm_inject(text, uid)
                await self.bot.send_message(
                    uid, wm_text,
                    parse_mode="HTML",
                    protect_content=True,
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(0.05)
            except TelegramForbiddenError:
                await db.db_pd_upsert_user(uid, subscribed=False)
            except Exception as e:
                log.debug(f"PD broadcast {uid}: {e}")

    # ── Сохранение сигнала в БД ───────────────────────────────────────────────

    async def _save_signal(self, signal, features: list):
        import json
        sig_id = await db.db_pd_save_signal(
            symbol=signal.symbol,
            direction=signal.direction,
            score=signal.score,
            layers_json=json.dumps(signal.active_layers),
            features_json=json.dumps(features),
            price=signal.price,
        )
        # Через 15 мин бэкфиллим исход
        asyncio.get_event_loop().call_later(
            900, lambda: asyncio.create_task(
                self._fill_outcome(sig_id, signal.symbol, signal.price, signal.direction)
            )
        )

    async def _fill_outcome(self, sig_id: int, symbol: str, price_at: float, direction: str):
        """Через 15 минут проверяем, было ли движение >= 3%."""
        try:
            from pump_dump.pd_config import BINGX_REST_FUTURES
            import aiohttp
            url = f"{BINGX_REST_FUTURES}/quote/ticker"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params={"symbol": symbol},
                                 timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
            current = float(data.get("data", {}).get("lastPrice", price_at))
            change  = (current - price_at) / price_at if price_at > 0 else 0.0
            correct = (direction == "PUMP" and change >= 0.03) or \
                      (direction == "DUMP" and change <= -0.03)
            await db.db_pd_save_outcome(sig_id, price_at, current, change * 100, correct)
            # Сохраняем в обучающую выборку
            await db.db_pd_save_train(sig_id, (1 if direction == "PUMP" else 2) if correct else 0)
        except Exception as e:
            log.debug(f"PD outcome {symbol}: {e}")

    # ── Переобучение ML ───────────────────────────────────────────────────────

    async def _retrain_loop(self):
        while True:
            await asyncio.sleep(3600)  # каждый час проверяем
            await get_model().maybe_retrain(self.db_path)

    # ── Трекинг исходов ───────────────────────────────────────────────────────

    async def _outcome_loop(self):
        """Раз в 5 минут проверяем незакрытые сигналы (fallback)."""
        while True:
            await asyncio.sleep(300)
            try:
                pending = await db.db_pd_pending_outcomes()
                for row in pending:
                    asyncio.create_task(
                        self._fill_outcome(
                            row["id"], row["symbol"],
                            row["price_signal"], row["direction"]
                        )
                    )
            except Exception as e:
                log.debug(f"PD outcome loop: {e}")
