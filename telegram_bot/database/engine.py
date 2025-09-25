from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from telegram_bot.config.settings import BotSettings

settings = BotSettings()

# Создаем асинхронный движок для подключения к БД
async_engine = create_async_engine(
    url=settings.database_url,
    echo=False,  # В продакшене лучше False
    connect_args={"timeout": 10}  # Добавляем таймаут 10 секунд
)

# Создаем фабрику сессий для асинхронной работы
async_session_factory = async_sessionmaker(
    async_engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)
