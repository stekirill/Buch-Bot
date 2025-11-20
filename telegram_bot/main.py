import sys
print(f"--- Запуск бота... Версия Python: {sys.version} ---")

import asyncio
import logging
from telegram_bot.core.bot import create_bot
from telegram_bot.core.dispatcher import setup_dispatcher
from telegram_bot.core.scheduler import TaskScheduler
from telegram_bot.database.engine import async_session_factory, async_engine
from telegram_bot.middleware.auth import ClientAuthMiddleware
from telegram_bot.middleware.typing import AIServiceMiddleware
from telegram_bot.middleware.bitrix import BitrixServiceMiddleware
from telegram_bot.middleware.roster import RosterMiddleware
from telegram_bot.middleware.chat_history import ChatHistoryMiddleware
from telegram_bot.services.knowledge_base import KnowledgeBaseService
from telegram_bot.services.ai_service import AIService
from telegram_bot.services.bitrix_service import BitrixService
from telegram_bot.services.roster_service import RosterService
from telegram_bot.services.perplexity_service import PerplexityService
from telegram_bot.services.chat_history_service import ChatHistoryService
from telegram_bot.services.stop_words_service import StopWordsService
from telegram_bot.config.settings import BotSettings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from telegram_bot.database.models import Base
from telegram_bot.handlers import commands, messages
from telegram_bot.handlers import callbacks as callback_handlers

# Proactive fix for asyncio on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main():
    print("1. Настройка логирования...")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[
            logging.FileHandler("telegram_bot/bot.log"),
            logging.StreamHandler(),
        ],
    )
    
    print("2. Загрузка настроек...")
    settings = BotSettings()

    print("3. Инициализация БД...")
   

    # Инициализируем сервисы
    print("4. Инициализация Базы Знаний...")
    knowledge_base = KnowledgeBaseService(settings)
    await knowledge_base.initialize()
    print("База Знаний готова.")

    print("5. Инициализация сервисов...")
    perplexity_service = PerplexityService(settings)
    ai_service = AIService(settings, knowledge_base, perplexity_service)
    bitrix_service = BitrixService(settings)
    roster_service = RosterService(settings)
    chat_history_service = ChatHistoryService()
    stop_words_service = StopWordsService(settings)
    print("Сервисы созданы.")
    
    print("6. Загрузка таблицы чатов/ответственных...")
    await roster_service.initialize()
    roster_service.start_periodic_refresh()
    print("Таблица загружена.")
    
    print("6.1. Загрузка стоп-слов...")
    await stop_words_service.initialize()
    stop_words_service.start_periodic_refresh()
    print("Стоп-слова загружены.")
    
    print("7. Создание бота и диспетчера...")
    bot, dp = await create_bot()
    print("Бот и диспетчер созданы.")

    # Регистрация роутеров и middleware централизована в одной функции
    print("8. Настройка роутеров и middleware...")
    setup_dispatcher(
        dp,
        session_pool=async_session_factory,
        ai_service=ai_service,
        bitrix_service=bitrix_service,
        roster_service=roster_service,
        chat_history_service=chat_history_service,
        stop_words_service=stop_words_service,
    )
    print("Роутеры и middleware настроены.")
    
    print("9. Настройка планировщика...")
    scheduler = TaskScheduler(bot, bitrix_service)
    await scheduler.setup_jobs()
    scheduler.start()
    print("Планировщик запущен.")
    
    try:
        print("--- НАЧИНАЮ ПОЛЛИНГ ---")
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()
        scheduler.shutdown()
        await roster_service.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
