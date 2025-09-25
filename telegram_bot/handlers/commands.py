from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from telegram_bot.services.bitrix_service import BitrixService
from telegram_bot.database.engine import async_session_factory
from telegram_bot.database.models import Client, BitrixTaskLink
from telegram_bot.utils.transliteration import get_russian_name
from telegram_bot.utils.schedule import now_msk, is_processing_window_now
from telegram_bot.config.settings import BotSettings
from sqlalchemy import select, desc

router = Router()


STATUS_MAP = {
    1: "Новая",
    2: "В ожидании",
    3: "Выполняется",
    4: "Ожидает контроля",
    5: "Завершена",
    6: "Отложена",
    7: "Отклонена",
}


def human_status(code: str | int | None) -> str:
    try:
        icode = int(code) if code is not None else None
    except Exception:
        icode = None
    return STATUS_MAP.get(icode, str(code) if code is not None else "-")


@router.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    Этот обработчик будет получать сообщения с командой `/start`
    """
    # Используем транслитерированное имя для приветствия
    russian_name = get_russian_name(message.from_user.full_name or message.from_user.first_name or "")
    await message.answer(f"Здравствуйте, {russian_name}!")


@router.message(Command("help"))
async def command_help_handler(message: Message) -> None:
    """
    Этот обработчик будет получать сообщения с командой `/help`
    """
    await message.answer("Это бот для бухгалтерских услуг. Чем я могу помочь?")


@router.message(Command("status"))
async def command_status_handler(message: Message, bitrix_service: BitrixService | None = None) -> None:
    if not bitrix_service:
        await message.answer("Сервис задач временно недоступен. Попробуйте позже.")
        return

    async with ChatActionSender.typing(chat_id=message.chat.id, bot=message.bot):
        # Ищем клиента по Telegram user_id
        async with async_session_factory() as session:
            client_row = await session.execute(select(Client).where(Client.user_id == message.from_user.id))
            client: Client | None = client_row.scalars().one_or_none()
            if not client:
                await message.answer("Пока нет задач.")
                return

            # Берем последние 10 связок по этому клиенту из текущего чата
            links_row = await session.execute(
                select(BitrixTaskLink)
                .where(BitrixTaskLink.client_id == client.id, BitrixTaskLink.chat_id == message.chat.id)
                .order_by(desc(BitrixTaskLink.created_at)).limit(10)
            )
            links = list(links_row.scalars())
            if not links:
                await message.answer("Открытых задач в этом чате не найдено.")
                return

        # Получаем актуальные статусы из Битрикс по taskId
        lines = ["Ваши задачи:"]
        any_found = False
        for link in links:
            brief = await bitrix_service.get_task_brief(link.task_id)
            if not brief:
                continue
            any_found = True
            title = brief.get('title') or ''
            # Убираем лишние детали из описания для компактности
            descr_text = (brief.get('description') or "").split("Чат:")[0].strip()
            main_line = title.strip() or descr_text.splitlines()[0] if descr_text else '(без названия)'
            status_label = human_status(brief.get('status'))
            deadline = brief.get('deadline')
            dl_part = f" | Дедлайн: {deadline[:10]}" if deadline else ""
            lines.append(f"• [{status_label}] {main_line}{dl_part} (ID: {brief.get('id')})")

        if not any_found:
            await message.answer("Открытых задач в этом чате не найдено.")
            return

        await message.answer("\n".join(lines))


@router.message(Command("time"))
async def command_time_handler(message: Message) -> None:
    """
    Команда для проверки времени по Москве и статуса рабочего времени
    """
    current_time = now_msk()
    settings = BotSettings()
    is_working = is_processing_window_now(settings.processing_schedule)
    
    weekday_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    weekday = weekday_names[current_time.weekday()]
    
    working_status = "🟢 Рабочее время" if is_working else "🔴 Нерабочее время"
    
    response = f"""🕐 **Время по Москве:**
{current_time.strftime('%d.%m.%Y %H:%M:%S')} ({weekday})

{working_status}
Рабочие часы: {settings.processing_schedule['weekdays']} (пн-пт)"""
    
    await message.answer(response)
