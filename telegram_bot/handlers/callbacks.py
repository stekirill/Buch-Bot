from aiogram import Router, F
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from loguru import logger

from telegram_bot.services.bitrix_service import BitrixService
from telegram_bot.services.state import STATE
from telegram_bot.services.roster_service import RosterService
from telegram_bot.database.models import Client, BitrixTaskLink
from telegram_bot.utils.keyboards import BotKeyboards
from sqlalchemy import select

router = Router()


@router.callback_query(F.data.startswith("call_expert"))
async def on_call_expert(
    cb: CallbackQuery,
    *,
    session: AsyncSession,
    bitrix_service: Optional[BitrixService] = None,
    roster_service: Optional[RosterService] = None,
    client: Optional[Client] = None,
):
    """
    Обрабатывает нажатие кнопки "Позвать бухгалтера".
    Пингует ответственных в чате и добавляет [URGENT] коммент в задачу.
    """
    if client is None:
        row = await session.execute(select(Client).where(Client.user_id == cb.from_user.id))
        client = row.scalars().one_or_none()

    # Пингуем ответственных из таблицы
    usernames = roster_service.get_tg_responsibles(cb.message.chat.id) if roster_service else []
    mention = " ".join(f"@{u}" for u in usernames) if usernames else ""
    note = "Вопрос будет обработан в ближайшее время." if usernames else "Ответственные не настроены в таблице."
    await cb.message.answer((mention + " \n" if mention else "") + note)
    
    # Добавляем комментарий в задачу, если она есть
    try:
        _, task_id_str = cb.data.split(":", 1)
        task_id = int(task_id_str)
    except (ValueError, IndexError):
        task_id = None
        logger.warning(f"Не удалось извлечь task_id из callback_data: {cb.data}. Попробуем найти активную задачу.")

    if not task_id and client:
        # Ищем последнюю активную question-задачу
        link_row = await session.execute(
            select(BitrixTaskLink)
            .where(BitrixTaskLink.client_id == client.id, BitrixTaskLink.kind == 'question', BitrixTaskLink.is_active == True)
            .order_by(BitrixTaskLink.created_at.desc()).limit(1)
        )
        link = link_row.scalars().one_or_none()
        if link:
            task_id = int(link.task_id)

    if task_id and bitrix_service:
        await bitrix_service.add_comment(str(task_id), "[URGENT] Клиент просит приоритет. Просьба ответить как можно быстрее.")
        logger.info(f"Добавлен [URGENT] комментарий к задаче {task_id}")
    else:
        logger.warning(f"Не найдена активная задача для пользователя {cb.from_user.id}, чтобы пометить ее срочной.")
    
    await cb.answer()


@router.callback_query(F.data.startswith("clarify:"))
async def handle_clarify_callback(
    callback_query: CallbackQuery, 
    *,
    session: AsyncSession, 
    client: Optional[Client] = None
):
    """
    Обрабатывает нажатие инлайн-кнопки 'Уточнить'.
    Устанавливает состояние ожидания уточнения от пользователя.
    """
    try:
        _, task_id_str = callback_query.data.split(":", 1)
        # Проверяем, что task_id_str не пустой
        if not task_id_str or task_id_str.strip() == "":
            logger.error(f"Пустой task_id в callback_data: {callback_query.data}")
            await callback_query.answer("Произошла ошибка, не удалось определить задачу.", show_alert=True)
            return
        task_id = int(task_id_str)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка извлечения task_id из callback_data: {callback_query.data}. Ошибка: {e}")
        await callback_query.answer("Произошла ошибка, не удалось определить задачу.", show_alert=True)
        return

    if client is None:
        row = await session.execute(select(Client).where(Client.user_id == callback_query.from_user.id))
        client = row.scalars().one_or_none()
    if not client:
        logger.warning(f"Не удалось найти клиента с telegram_id={callback_query.from_user.id} для уточнения задачи.")
        await callback_query.answer("Не удалось определить ваш профиль.", show_alert=True)
        return

    # Устанавливаем состояние ожидания
    existing_task_id = STATE.set_pending_clarify(callback_query.message.chat.id, client.user_id, task_id)

    if existing_task_id is not None:
        if existing_task_id == task_id:
            # Пользователь нажал на ту же кнопку еще раз
            await callback_query.answer(
                "Я уже жду ваше уточнение по этой задаче. Просто отправьте текст или файлы.", 
                show_alert=True
            )
        else:
            # Пользователь пытается уточнить другую задачу, пока активна первая
            await callback_query.answer(
                f"Сначала завершите предыдущее уточнение (для задачи #{existing_task_id}).\n\n"
                f"Просто отправьте текст или файлы.",
                show_alert=True
            )
        return

    # Отвечаем пользователю и удаляем кнопки
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer() # Закрываем "часики"
    
    # Отправляем сообщение с кнопкой отмены
    cancel_keyboard = BotKeyboards.get_cancel_clarify_keyboard(task_id)
    await callback_query.message.answer(
        "Слушаю ваше уточнение. Присылайте текст, фото или документы.",
        reply_markup=cancel_keyboard
    )
    
    logger.info(f"Пользователь {callback_query.from_user.id} нажал 'Уточнить' для задачи {task_id} в чате {callback_query.message.chat.id}. Установлено состояние ожидания.")


@router.callback_query(F.data.startswith("cancel_clarify:"))
async def handle_cancel_clarify_callback(
    callback_query: CallbackQuery,
    *,
    session: AsyncSession,
    client: Optional[Client] = None
):
    """
    Обрабатывает нажатие кнопки 'Отменить уточнение'.
    Удаляет состояние ожидания уточнения.
    """
    try:
        _, task_id_str = callback_query.data.split(":", 1)
        if not task_id_str or task_id_str.strip() == "":
            logger.error(f"Пустой task_id в callback_data: {callback_query.data}")
            await callback_query.answer("Произошла ошибка, не удалось определить задачу.", show_alert=True)
            return
        task_id = int(task_id_str)
    except (ValueError, IndexError) as e:
        logger.error(f"Ошибка извлечения task_id из callback_data: {callback_query.data}. Ошибка: {e}")
        await callback_query.answer("Произошла ошибка, не удалось определить задачу.", show_alert=True)
        return

    if client is None:
        row = await session.execute(select(Client).where(Client.user_id == callback_query.from_user.id))
        client = row.scalars().one_or_none()
    
    if not client:
        logger.warning(f"Не удалось найти клиента с telegram_id={callback_query.from_user.id} для отмены уточнения.")
        await callback_query.answer("Не удалось определить ваш профиль.", show_alert=True)
        return

    # Проверяем, есть ли активное ожидание уточнения
    existing_task_id = STATE.get_pending_clarify(callback_query.message.chat.id, client.user_id)
    
    if existing_task_id is None:
        # Нет активного ожидания
        await callback_query.answer("Нет активного ожидания уточнения.", show_alert=True)
        # Удаляем кнопку, если она есть
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return
    
    if existing_task_id != task_id:
        # Пользователь пытается отменить уточнение для другой задачи
        await callback_query.answer(
            f"Активно ожидание уточнения для задачи #{existing_task_id}, а не для #{task_id}.",
            show_alert=True
        )
        return

    # Удаляем состояние ожидания
    STATE.remove_pending_clarify(callback_query.message.chat.id, client.user_id)
    
    # Удаляем кнопку отмены
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer("Уточнение отменено. Можете продолжать обычный диалог.")
    
    # Отправляем подтверждающее сообщение
    await callback_query.message.answer("✅ Уточнение отменено. Теперь ваши сообщения будут обрабатываться как обычно.")
    
    logger.info(f"Пользователь {callback_query.from_user.id} отменил уточнение для задачи {task_id} в чате {callback_query.message.chat.id}.")
