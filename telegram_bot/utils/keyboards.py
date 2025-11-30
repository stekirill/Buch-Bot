from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from typing import Optional

class BotKeyboards:
    @staticmethod
    def get_main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="my_tasks")],
            [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="ask_question")],
            [InlineKeyboardButton(text="📞 Связаться с бухгалтером", callback_data="call_accountant")]
        ])
    
    @staticmethod 
    def get_task_actions(task_id: int = None) -> Optional[InlineKeyboardMarkup]:
        if task_id is None or task_id == "" or str(task_id).strip() == "":
            return None
        buttons = [
            [InlineKeyboardButton(text="✏️ Уточнить", callback_data=f"clarify:{task_id}")],
            [InlineKeyboardButton(text="👤 Позвать бухгалтера", callback_data=f"call_expert:{task_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    
    @staticmethod
    def get_cancel_clarify_keyboard(task_id: int) -> InlineKeyboardMarkup:
        """Клавиатура с кнопкой отмены уточнения."""
        buttons = [
            [InlineKeyboardButton(text="❌ Отменить уточнение", callback_data=f"cancel_clarify:{task_id}")]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)