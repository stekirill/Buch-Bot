from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, String, func, ForeignKey, Text, Boolean
from sqlalchemy.types import DateTime
from typing import Optional
import datetime


class Base(DeclarativeBase):
    """Базовая модель для всех таблиц."""
    pass


class Client(Base):
    """Модель для хранения информации о клиентах."""
    __tablename__ = 'clients'

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, 
        default=datetime.datetime.utcnow,
        server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Client(id={self.id}, user_id={self.user_id}, username='{self.username}')>"


class ChatMessage(Base):
    """История сообщений в чате."""
    __tablename__ = 'chat_messages'

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # Telegram chat ID
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.utcnow,
        server_default=func.now(),
        nullable=False,
    )


class BitrixTaskLink(Base):
    """Связка клиента и задачи в Битрикс."""
    __tablename__ = 'bitrix_task_links'

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(512))
    status: Mapped[Optional[str]] = mapped_column(String(64))
    last_comment_id: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    kind: Mapped[Optional[str]] = mapped_column(String(32))  # 'question' | 'docs'
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.utcnow,
        server_default=func.now(),
        nullable=False,
    )


class PendingAttachment(Base):
    """Буфер для вложений, пока нет активной задачи."""
    __tablename__ = 'pending_attachments'

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.utcnow,
        server_default=func.now(),
        nullable=False,
    )