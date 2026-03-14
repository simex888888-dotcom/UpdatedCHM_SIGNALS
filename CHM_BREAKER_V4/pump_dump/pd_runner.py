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
)
from pump_dump.market_monitor    import MarketMonitor, MarketEvent
from pump_dump.ml_model          import get_model, build_feature_vector
from pump_dump.pd_handlers       import set_current_scores
from pump_dump.signal_aggregator import format_alert, analyze_levels
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
        self._bg_tasks: set = set()   # держим ссылки на фоновые задачи

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
            self._hidden_data_loop(),
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
        an   = anomaly.detect(df, event.trades_buy_vol, event.trades_sell_vol)
        ob   = orderbook.analyze(event.orderbook, an.price_change_1m)
        ind  = indicators.analyze(df)
        hs   = await hidden_signals.analyze(sym, df, self.monitor.get_symbols())

        # Обновляем score для /pd_top даже если нет сигнала.
        # Для отображения используем ненаправленный score; ML-бонус — только при совпадении направления.
        features = build_feature_vector(an, ob, hs, ind)
        ml_res   = get_model().predict(features)
        direction_hint = an.spike_dir or ("PUMP" if (event.trades_buy_vol or 0) >= (event.trades_sell_vol or 0) else "DUMP")
        raw_score = (
            (an.volume_double_cond * 0.15) +
            (an.price_spike        * 0.10) +
            (an.cvd_signal         * 0.15) +
            (ob.imbalance_signal   * 0.10) +
            (ob.spread_signal      * 0.10) +
            (hs.funding_signal     * 0.15) +
            (hs.oi_signal          * 0.10) +
            ((ml_res is not None and ml_res.predicted == direction_hint) * 0.15)
        ) * 100
        self._scores[sym] = round(raw_score, 1)
        set_current_scores({sym: self._scores[sym]})

        # ── Многоуровневые предупреждения (1, 2, 3) ──────────────────────────
        total_vol = (event.trades_buy_vol or 0) + (event.trades_sell_vol or 0)
        buy_ratio = (event.trades_buy_vol / total_vol) if total_vol > 0 else 0.5

        level_alerts = analyze_levels(
            sym, event.last_price, an, ob, hs, ind, buy_ratio
        )
        for level, text, payload in level_alerts:
            # Используем score из payload (взвешенный, с учётом направления),
            # а не raw_score (ненаправленный, для /pd_top)
            actual_score = payload.score if payload is not None else raw_score
            if level == 3:
                log.info(f"🎯 PD уровень 3 (ФИНАЛЬНЫЙ): {sym} {actual_score:.0f}%")
                if payload is not None:
                    await self._save_signal(payload, features)
            elif level == 2:
                log.info(f"⚠️ PD уровень 2 (НАБЛЮДЕНИЕ): {sym}")
            else:
                log.debug(f"👀 PD уровень 1 (ВНИМАНИЕ): {sym}")
            await self._broadcast_level(level, text, actual_score)

    # ── Рассылка по уровню ───────────────────────────────────────────────────

    async def _broadcast_level(self, level: int, text: str, score: float):
        """Рассылает предупреждение заданного уровня подписанным пользователям.

        Уровень 1 — ранние предупреждения, получают все подписчики (threshold<=100).
        Уровень 2 — наблюдение, получают подписчики с threshold<=60.
        Уровень 3 — финальный сигнал, только пользователи у которых threshold<=score.

        db_pd_subscribers(min_threshold=X) → WHERE pd_threshold <= X
        """
        if level == 3:
            threshold = int(score)
        elif level == 2:
            threshold = 60   # пользователи с порогом <= 60% получают уровень 2
        else:
            threshold = 100  # все подписчики получают уровень 1
        users = await db.db_pd_subscribers(min_threshold=threshold)
        if not users:
            return
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
                log.debug(f"PD broadcast level{level} {uid}: {e}")

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
        def _schedule_outcome():
            task = asyncio.create_task(
                self._fill_outcome(sig_id, signal.symbol, signal.price, signal.direction)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        asyncio.get_running_loop().call_later(900, _schedule_outcome)

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

    # ── Независимый цикл обновления Funding/OI/L&S ───────────────────────────

    async def _hidden_data_loop(self):
        """Обновляет funding/OI/L&S независимо от обработки событий.

        Без этого данные funding появлялись только после обработки первого
        события из очереди, что могло занять несколько секунд. Кнопка
        «Аномальный Funding Rate» показывала «загружается» при любом клике.
        """
        from pump_dump.hidden_signals import _cache
        from pump_dump.pd_config import FUNDING_FETCH_EVERY
        # Ждём пока монитор загрузит список монет (обычно 5-15с)
        while not self.monitor.get_symbols():
            await asyncio.sleep(1)
        log.info("💸 PD: первичная загрузка Funding/OI/L&S…")
        symbols = self.monitor.get_symbols()
        await _cache.refresh_if_needed(symbols)
        log.info("💸 PD: Funding/OI/L&S загружены")
        while True:
            await asyncio.sleep(FUNDING_FETCH_EVERY)
            symbols = self.monitor.get_symbols()
            if symbols:
                _cache.reset_fetch_timestamps()
                await _cache.refresh_if_needed(symbols)

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
                    task = asyncio.create_task(
                        self._fill_outcome(
                            row["id"], row["symbol"],
                            row["price_signal"], row["direction"]
                        )
                    )
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)
            except Exception as e:
                log.debug(f"PD outcome loop: {e}")
