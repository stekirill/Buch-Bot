from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Dict, Any, Awaitable

from telegram_bot.services.roster_service import RosterService


class RosterMiddleware(BaseMiddleware):
    def __init__(self, roster: RosterService):
        self.roster = roster

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        data["roster_service"] = self.roster
        return await handler(event, data)


