from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import async_sessionmaker

from telegram_bot.handlers import commands, messages, callbacks
from telegram_bot.middleware.auth import ClientAuthMiddleware
from telegram_bot.middleware.typing import AIServiceMiddleware
from telegram_bot.middleware.bitrix import BitrixServiceMiddleware
from telegram_bot.middleware.roster import RosterMiddleware
from telegram_bot.middleware.chat_history import ChatHistoryMiddleware

from telegram_bot.services.ai_service import AIService
from telegram_bot.services.bitrix_service import BitrixService
from telegram_bot.services.roster_service import RosterService
from telegram_bot.services.chat_history_service import ChatHistoryService

def setup_dispatcher(
    dp: Dispatcher,
    session_pool: async_sessionmaker,
    ai_service: AIService,
    bitrix_service: BitrixService,
    roster_service: RosterService,
    chat_history_service: ChatHistoryService,
):
    """
    Настройка роутеров и middleware для диспетчера.
    """
    # Middleware регистрируются для каждого роутера индивидуально
    for router in [commands.router, messages.router, callbacks.router]:
        # DB session + client resolving
        client_auth_mw = ClientAuthMiddleware(session_pool=session_pool)
        router.message.middleware(client_auth_mw)
        router.callback_query.middleware(client_auth_mw)

        # Service middlewares
        ai_mw = AIServiceMiddleware(ai_service=ai_service)
        bitrix_mw = BitrixServiceMiddleware(bitrix_service=bitrix_service)
        roster_mw = RosterMiddleware(roster=roster_service)
        history_mw = ChatHistoryMiddleware(chat_history_service=chat_history_service)

        for mw in (ai_mw, bitrix_mw, roster_mw, history_mw):
            router.message.middleware(mw)
            router.callback_query.middleware(mw)

    dp.include_router(commands.router)
    dp.include_router(callbacks.router)
    dp.include_router(messages.router)
