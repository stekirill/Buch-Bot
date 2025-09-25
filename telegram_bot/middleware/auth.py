from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject
from typing import Callable, Dict, Any, Awaitable
from sqlalchemy.ext.asyncio import async_sessionmaker

from telegram_bot.services.client_service import ClientService
from telegram_bot.database.repository import ClientRepository


class ClientAuthMiddleware(BaseMiddleware):
    def __init__(self, session_pool: async_sessionmaker):
        self.session_pool = session_pool
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) or isinstance(event, CallbackQuery):
            async with self.session_pool() as session:
                client_repo = ClientRepository(session)
                client_service = ClientService(client_repo)

                user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
                if user is not None:
                    client = await client_service.get_or_create_client(
                        user_id=user.id,
                        username=user.username,
                        first_name=user.first_name
                    )
                    await session.commit()
                    data['client'] = client
                    data['session'] = session

                return await handler(event, data)

        return await handler(event, data)
