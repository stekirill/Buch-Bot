import asyncio
from typing import Callable, Dict, Any
from loguru import logger

class DebounceManager:
    def __init__(self):
        self.tasks: Dict[int, asyncio.Task] = {}

    async def schedule(self, chat_id: int, callback: Callable, delay: float = 15.0, **kwargs):
        """
        Планирует выполнение callback через delay секунд.
        Если для этого chat_id уже есть задача, она отменяется и создается новая.
        """
        # Если таймер уже тикает — отменяем его (пришло новое сообщение)
        if chat_id in self.tasks:
            task = self.tasks[chat_id]
            if not task.done():
                task.cancel()

        # Запускаем новый таймер
        self.tasks[chat_id] = asyncio.create_task(
            self._wait_and_execute(chat_id, callback, delay, **kwargs)
        )

    async def _wait_and_execute(self, chat_id: int, callback: Callable, delay: float, **kwargs):
        try:
            await asyncio.sleep(delay)
            # Если мы здесь, значит новых сообщений не было delay секунд
            if chat_id in self.tasks:
                del self.tasks[chat_id]
            await callback(chat_id, **kwargs)
        except asyncio.CancelledError:
            # Задача была отменена (пришло новое сообщение), ничего не делаем
            pass
        except Exception as e:
            logger.error(f"Error in debounce task for chat {chat_id}: {e}")
            if chat_id in self.tasks:
                del self.tasks[chat_id]

# Глобальный экземпляр
debounce_manager = DebounceManager()

