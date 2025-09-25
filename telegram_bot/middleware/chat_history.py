from aiogram import BaseMiddleware
from aiogram.types import Message
from typing import Callable, Dict, Any, Awaitable
from telegram_bot.services.chat_history_service import ChatHistoryService


class ChatHistoryMiddleware(BaseMiddleware):
    def __init__(self, chat_history_service: ChatHistoryService):
        self.chat_history_service = chat_history_service

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        data["chat_history_service"] = self.chat_history_service
        return await handler(event, data)
