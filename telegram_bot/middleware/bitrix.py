from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

from telegram_bot.services.bitrix_service import BitrixService


class BitrixServiceMiddleware(BaseMiddleware):
    def __init__(self, bitrix_service: BitrixService):
        self.bitrix_service = bitrix_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        data["bitrix_service"] = self.bitrix_service
        return await handler(event, data)


