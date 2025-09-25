from abc import ABC, abstractmethod
from typing import TypeVar, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram_bot.database.models import Base, Client

T = TypeVar("T", bound=Base)


class AbstractRepository(ABC):
    @abstractmethod
    async def get_one_or_none(self, **filter_by):
        raise NotImplementedError

    @abstractmethod
    async def add_one(self, data: dict):
        raise NotImplementedError


class SQLAlchemyRepository(AbstractRepository):
    model: Type[T] = None

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_one_or_none(self, **filter_by) -> T | None:
        stmt = select(self.model).filter_by(**filter_by)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def add_one(self, data: dict) -> T:
        instance = self.model(**data)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance


class ClientRepository(SQLAlchemyRepository):
    model = Client
