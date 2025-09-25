from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional
from loguru import logger
from cachetools import TTLCache


@dataclass
class AppState:
    # key: (chat_id, user_id), value: task_id
    pending_clarify: Dict[Tuple[int, int], int] = field(default_factory=dict)
    # key: (chat_id, client_id), value: original_question text
    pending_pre_task_clarify: TTLCache = field(default_factory=lambda: TTLCache(maxsize=1024, ttl=900))

    def set_pending_clarify(self, chat_id: int, user_id: int, task_id: int) -> Optional[int]:
        """
        Устанавливает состояние ожидания для задачи.
        Если для этого чата/пользователя уже есть активное ожидание,
        возвращает ID "старой" задачи и не меняет состояние.
        В случае успеха возвращает None.
        """
        key = (chat_id, user_id)
        if key in self.pending_clarify:
            return self.pending_clarify[key]
        
        self.pending_clarify[key] = task_id
        return None

    def pop_pending_clarify(self, chat_id: int, user_id: int) -> Optional[int]:
        return self.pending_clarify.pop((chat_id, user_id), None)

    def get_pending_clarify(self, chat_id: int, user_id: int) -> Optional[int]:
        return self.pending_clarify.get((chat_id, user_id))

    def set_pending_pre_task_clarification(self, chat_id: int, client_id: int, original_question: str):
        """Запоминает, что бот задал уточняющий вопрос ПЕРЕД созданием задачи."""
        key = (chat_id, client_id)
        # Сохраняем исходный вопрос на 15 минут
        self.pending_pre_task_clarify[key] = original_question
        logger.info(f"Установлен pre-task clarify для {key}, вопрос: {original_question}")

    def pop_pending_pre_task_clarification(self, chat_id: int, client_id: int) -> Optional[str]:
        """Извлекает и удаляет исходный вопрос, если он ожидался."""
        key = (chat_id, client_id)
        return self.pending_pre_task_clarify.pop(key, None)


STATE = AppState()


