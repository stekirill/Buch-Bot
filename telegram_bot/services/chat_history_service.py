from typing import List, Dict, Optional
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession
from telegram_bot.database.models import ChatMessage
import datetime


class ChatHistoryService:
    async def get_recent_messages(self, session: AsyncSession, chat_id: int, limit: int = 50, exclude_staff: bool = True, staff_usernames: Optional[List[str]] = None) -> List[Dict[str, str]]:
        """Получить последние сообщения из чата.
        
        Args:
            session: Database session
            chat_id: Telegram chat ID
            limit: Maximum number of messages to return
            exclude_staff: Whether to exclude staff messages from context
            staff_usernames: List of staff usernames to exclude (if exclude_staff=True)
        """
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(desc(ChatMessage.created_at))
            .limit(limit)
        )
        
        result = await session.execute(stmt)
        rows = list(result.scalars())
        rows.reverse()  # в хронологическом порядке (старые -> новые)
        
        # Фильтруем сообщения сотрудников, если нужно
        if exclude_staff and staff_usernames:
            # Здесь нужно будет добавить логику фильтрации по username
            # Пока возвращаем все сообщения
            pass
            
        return [{"role": row.role, "content": row.content} for row in rows]

    async def add_message_to_history(self, session: AsyncSession, client_id: int, chat_id: int, role: str, content: str) -> None:
        """Добавить одно сообщение в историю чата."""
        msg = ChatMessage(client_id=client_id, chat_id=chat_id, role=role, content=content)
        session.add(msg)
        await session.commit()

    async def is_first_message_today(self, session: AsyncSession, chat_id: int) -> bool:
        stmt = (
            select(func.max(ChatMessage.created_at))
            .where(ChatMessage.chat_id == chat_id)
        )
        result = await session.execute(stmt)
        last_dt = result.scalar_one_or_none()
        if not last_dt:
            return True
        return last_dt.date() != datetime.date.today()

    async def is_first_assistant_reply_today(self, session: AsyncSession, chat_id: int) -> bool:
        """Возвращает True, если сегодня еще не было сообщений роли 'assistant' в этом чате.
        Это нужно для корректного управления приветствием: пользовательские/сервисные записи
        не должны подавлять приветствие первого содержательного ответа дня.
        """
        stmt = (
            select(func.max(ChatMessage.created_at))
            .where(and_(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant"))
        )
        result = await session.execute(stmt)
        last_dt = result.scalar_one_or_none()
        if not last_dt:
            return True
        return last_dt.date() != datetime.date.today()

    async def get_last_assistant_message(self, session: AsyncSession, chat_id: int) -> Optional[str]:
        """Получает последнее сообщение ассистента из истории чата."""
        stmt = (
            select(ChatMessage.content)
            .where(and_(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant"))
            .order_by(desc(ChatMessage.created_at))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()