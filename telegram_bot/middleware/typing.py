from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

from telegram_bot.services.ai_service import AIService


class AIServiceMiddleware(BaseMiddleware):
    def __init__(self, ai_service: AIService):
        self.ai_service = ai_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        data["ai_service"] = self.ai_service
        return await handler(event, data)
