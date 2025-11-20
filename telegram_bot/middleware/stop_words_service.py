from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from typing import Callable, Dict, Any, Awaitable
from telegram_bot.services.stop_words_service import StopWordsService


class StopWordsServiceMiddleware(BaseMiddleware):
    """Middleware для добавления StopWordsService в контекст обработчиков."""
    
    def __init__(self, stop_words_service: StopWordsService):
        self.stop_words_service = stop_words_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        data['stop_words_service'] = self.stop_words_service
        return await handler(event, data)
