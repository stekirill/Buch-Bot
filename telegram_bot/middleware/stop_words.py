from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from typing import Callable, Dict, Any, Awaitable
from telegram_bot.services.stop_words_service import StopWordsService
from loguru import logger


class StopWordsMiddleware(BaseMiddleware):
    """Middleware для фильтрации сообщений со стоп-словами."""
    
    def __init__(self, stop_words_service: StopWordsService):
        self.stop_words_service = stop_words_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Проверяем только текстовые сообщения
        if isinstance(event, Message) and event.text:
            # Проверяем наличие стоп-слов
            if self.stop_words_service.contains_stop_word(event.text):
                logger.info(f"Сообщение от {event.from_user.id} заблокировано стоп-словом: {event.text[:50]}...")
                # Не вызываем handler - просто игнорируем сообщение
                return
        
        # Если стоп-слов нет или это не текстовое сообщение - продолжаем обработку
        return await handler(event, data)
