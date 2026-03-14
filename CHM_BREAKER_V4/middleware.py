"""
middleware.py — aiogram middlewares для CHM BREAKER BOT.

ThrottleMiddleware : ограничивает частоту сообщений/callback-ов от одного
                     пользователя, защищая от флуда и случайного спама.
"""
import time
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery

log = logging.getLogger("CHM.Middleware")


class ThrottleMiddleware(BaseMiddleware):
    """
    Пропускает не более одного обновления каждые `rate` секунд от одного uid.

    Для Message: молча игнорирует превышение (пользователь не получает ответа).
    Для CallbackQuery: вызывает cb.answer() перед игнорированием, чтобы убрать
    спиннер загрузки с кнопки. Без этого Telegram держит спиннер 2 секунды, потом
    помечает query как expired → следующий ответ на ту же кнопку падает с
    "query is too old and response timeout expired".

    Параметры
    ----------
    rate : float
        Минимальный интервал (в секундах) между обработанными апдейтами.
        По умолчанию 0.5 с (2 req/sec на пользователя).
    """

    def __init__(self, rate: float = 0.5) -> None:
        super().__init__()
        self.rate = rate
        self._last: dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            uid = user.id
            if now - self._last[uid] < self.rate:
                log.debug("Throttled uid=%s (%.2fs < %.2fs)", uid, now - self._last[uid], self.rate)
                # Для CallbackQuery обязательно отвечаем — иначе Telegram
                # держит спиннер 2с и помечает query как "too old".
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return  # апдейт не обрабатываем
            self._last[uid] = now
        return await handler(event, data)
