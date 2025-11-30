from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.utils.chat_action import ChatActionSender
from aiogram.enums import ContentType
from typing import Optional
from loguru import logger

from telegram_bot.services.ai_service import AIService, QuestionCategory
from telegram_bot.services.bitrix_service import BitrixService
from telegram_bot.services.roster_service import RosterService
from telegram_bot.services.chat_history_service import ChatHistoryService
from telegram_bot.database.models import Client, PendingAttachment, BitrixTaskLink
from sqlalchemy import select, desc, update
from telegram_bot.database.engine import async_session_factory
from telegram_bot.services.client_service import ClientService
from telegram_bot.database.repository import ClientRepository
from telegram_bot.utils.keyboards import BotKeyboards
from telegram_bot.utils.schedule import is_processing_window_now
from telegram_bot.config.settings import BotSettings
from telegram_bot.services.state import STATE
from telegram_bot.config.constants import CLARIFY_INSTRUCTION_HTML, NOTICES
from telegram_bot.utils.transliteration import get_russian_name
from telegram_bot.utils.debounce import debounce_manager
from typing import Any

router = Router()


def _tg_file_link(bot_token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path}"


async def _ensure_client(session: AsyncSession | None, message: Message) -> tuple[AsyncSession, Client]:
    if session is not None and hasattr(message, "client"):
        return session, message.client  # type: ignore[attr-defined]
    async with async_session_factory() as local_session:
        client_repo = ClientRepository(local_session)
        client_service = ClientService(client_repo)
        client = await client_service.get_or_create_client(
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name or "",
        )
        await local_session.commit()
        return local_session, client


async def _append_files_comment(bitrix: BitrixService, task_id: str, caption: str, entries: list[str]) -> None:
    text = "[TG_ATTACH] " + (caption or "Файлы из Telegram") + "\n\n" + "\n".join(f"• {u}" for u in entries)
    await bitrix.add_comment(task_id, text)


@router.message(F.content_type.in_({ContentType.DOCUMENT, ContentType.PHOTO, ContentType.VIDEO, ContentType.AUDIO, ContentType.VOICE}))
async def handle_attachments(
    message: Message,
    client: Client | None = None,
    session: AsyncSession | None = None,
    bitrix_service: BitrixService | None = None,
    roster_service: RosterService | None = None,
    ai_service: AIService | None = None,
    chat_history_service: ChatHistoryService | None = None,
):
    if bitrix_service is None:
        await message.answer("Сервис задач временно недоступен. Попробуйте позже.")
        return

    settings = BotSettings()

    # Фильтр для сотрудников (не обрабатываем файлы от сотрудников, но сохраняем в историю)
    if message.from_user.username and message.from_user.username.lstrip('@') in settings.staff_usernames:
        # Получаем клиента и сессию для сохранения в историю
        local_session, ensured_client = await _ensure_client(session, message)
        caption = message.caption or f"Файл: {message.document.file_name if message.document else 'вложение'}"
        await chat_history_service.add_message_to_history(
            session=local_session, client_id=ensured_client.id, chat_id=message.chat.id,
            role="user", content=caption
        )
        if session is None: await local_session.close()
        return

    # Готовим ссылки на файлы Telegram
    bot = message.bot
    file_links: list[str] = []

    # Документ
    if message.document:
        f = await bot.get_file(message.document.file_id)
        file_links.append(_tg_file_link(bot.token, f.file_path))

    # Фото: берём largest
    if message.photo:
        photo = message.photo[-1]
        f = await bot.get_file(photo.file_id)
        file_links.append(_tg_file_link(bot.token, f.file_path))

    # Видео
    if message.video:
        f = await bot.get_file(message.video.file_id)
        file_links.append(_tg_file_link(bot.token, f.file_path))

    # Аудио/Voice
    if message.audio:
        f = await bot.get_file(message.audio.file_id)
        file_links.append(_tg_file_link(bot.token, f.file_path))
    if message.voice:
        f = await bot.get_file(message.voice.file_id)
        file_links.append(_tg_file_link(bot.token, f.file_path))

    if not file_links:
        await message.answer("Не удалось получить файл. Попробуйте ещё раз.")
        return

    # Получаем клиента/сессию
    local_session, ensured_client = await _ensure_client(session, message)

    # 1) ПРИОРИТЕТ: Проверяем, есть ли ожидаемое уточнение для уже созданной задачи
    pending_task_id = STATE.pop_pending_clarify(message.chat.id, ensured_client.user_id)
    if pending_task_id:
        # Привязываем файлы к задаче, для которой ожидается уточнение
        caption = message.caption or "Файлы из Telegram"
        await _append_files_comment(bitrix_service, str(pending_task_id), caption, file_links)
        
        # Используем транслитерированное имя для ответа
        russian_name = get_russian_name(ensured_client.first_name or message.from_user.first_name)
        await message.answer(
            f"{russian_name}, документы получила. Прикрепила к задаче #{pending_task_id}"
        )
        
        return

    # 2) Если нет ожидаемого уточнения — пытаемся привязать к последней активной question-задаче из локальной БД
    task_id: Optional[str] = None
    active_link_row = await local_session.execute(
        select(BitrixTaskLink)
        .where(BitrixTaskLink.client_id == ensured_client.id, BitrixTaskLink.kind == 'question', BitrixTaskLink.is_active == True)
        .order_by(desc(BitrixTaskLink.created_at))
        .limit(1)
    )
    active_link = active_link_row.scalars().one_or_none()
    task_id = active_link.task_id if active_link else None
    if task_id:
        logger.info(f"Attachments: bind to last active local question task #{task_id}; files={len(file_links)}")

    # Если нет активной вопрос-задачи — в задачу Документы из чата (будет создана при отсутствии)
    if not task_id:
        try:
            # Пытаемся создать УМНУЮ вопрос-задачу с саммари контекста
            history = []
            if chat_history_service is not None:
                history = await chat_history_service.get_recent_messages(local_session, chat_id=message.chat.id, limit=100, exclude_staff=False)
            question_hint = message.caption or "Вложения из чата"
            summary = await ai_service.summarize_for_task(question_hint, history) if ai_service else ""
            summary_text = f"Контекст диалога:\n{summary}\n\n---\n" if summary else ""
            title = f"Вложения по вопросу: {question_hint.strip().splitlines()[0][:120]}"
            
            responsible_id = roster_service.get_responsible_id(message.chat.id) if roster_service else None
            chat_title = (roster_service.get_entry(message.chat.id).chat_title if roster_service and roster_service.get_entry(message.chat.id) else None)

            # Используем транслитерированное имя в описании задачи
            russian_name = get_russian_name(ensured_client.first_name or message.from_user.first_name)
            task_created = await bitrix_service.create_task(
                title=title,
                description=f"{summary_text}Имя: {russian_name}\nЗаголовок вложений: {question_hint}",
                client_user_id=ensured_client.user_id,
                responsible_id=responsible_id,
                chat_id=message.chat.id,
                chat_title=chat_title
            )
            if task_created:
                task_id = str(task_created)
                # Линкуем в БД
                link = BitrixTaskLink(client_id=ensured_client.id, chat_id=message.chat.id, task_id=str(task_id), title=title, is_active=True, kind='question')
                local_session.add(link)
                await local_session.commit()
                logger.info(f"Attachments: created new question task #{task_id} with AI summary; files={len(file_links)}")
        except Exception as e:
            logger.error(f"Attachments: failed to create smart task: {e}")

    # Если всё ещё нет — используем/создаём 'Документы из чата'
    if not task_id:
        responsible_id = roster_service.get_responsible_id(message.chat.id) if roster_service else None
        # Используем транслитерированное имя для создания задачи
        russian_name = get_russian_name(ensured_client.first_name or (message.from_user.first_name or ""))
        task_id = await bitrix_service.find_or_create_docs_task(
            chat_id=message.chat.id,
            client_user_id=ensured_client.user_id,
            client_name=russian_name,
            responsible_id=responsible_id,
        )
        if task_id:
            logger.info(f"Attachments: created/used docs task #{task_id} for chat {message.chat.id}; files={len(file_links)}")

    if not task_id:
        return

    caption = message.caption or "Файлы из Telegram"
    await _append_files_comment(bitrix_service, task_id, caption, file_links)

    # Используем транслитерированное имя для ответа
    russian_name = get_russian_name(ensured_client.first_name or message.from_user.first_name)
    await message.answer(
        f"{russian_name}, документы получила. Прикрепила к задаче #{task_id}"
    )


async def process_accumulated_messages(
    chat_id: int,
    bot_instance: Any,
    user_id: int,
    first_name: str,
    username: Optional[str],
    ai_service: AIService,
    bitrix_service: BitrixService,
    roster_service: RosterService,
    chat_history_service: ChatHistoryService,
):
    async with async_session_factory() as session:
        # 1. Получаем клиента
        client_repo = ClientRepository(session)
        client_service = ClientService(client_repo)
        client = await client_service.get_or_create_client(user_id=user_id, username=username, first_name=first_name)
        
        # 2. Собираем ВСЕ сообщения пользователя, на которые бот еще не ответил
        unanswered_msgs = await chat_history_service.get_unanswered_user_messages(session, chat_id)
        if not unanswered_msgs:
            return
            
        # Склеиваем их в один текст
        combined_text = "\n".join(unanswered_msgs)
        logger.info(f"Processing accumulated messages for chat {chat_id} (count={len(unanswered_msgs)}): {combined_text[:50]}...")

        full_name = client.first_name or ""
        settings = BotSettings()

        # Вспомогательная функция отправки (т.к. message недоступен)
        async def send_answer(text: str, reply_markup=None, parse_mode=None, disable_web_page_preview=None):
            try:
                await bot_instance.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview
                )
            except Exception as e:
                logger.error(f"Failed to send message to {chat_id}: {e}")

        original_question_for_clarify = STATE.get_pending_clarify(chat_id, client.user_id)
        pre_task_original_question = STATE.pop_pending_pre_task_clarification(chat_id, client.id)

        # Статус "печатает" перед ответом
        await bot_instance.send_chat_action(chat_id=chat_id, action="typing")

        # Внутренняя функция логики (адаптированная)
        async def _create_or_update_task_flow(
            question: str, 
            history: list, 
            skip_clarification: bool = False,
            custom_reply_text: Optional[str] = None
        ) -> None:
            # 1. Поиск по БЗ
            playbook = await ai_service.get_kb_playbook(question, session, chat_id, min_confidence=0.85)
            if playbook:
                reply_text = playbook.get("reply")
                creates_task = playbook.get("create_task") and bitrix_service
                if reply_text:
                    reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
                    final_reply_text = await ai_service.format_response_with_name(reply_text, full_name, is_first_today)
                    
                    if creates_task:
                        title = f"Вопрос: {(question or '').strip().splitlines()[0][:140]}"
                        summary = await ai_service.summarize_for_task(question, history)
                        summary_text = f"Контекст диалога:\n{summary}\n\n---\n" if summary else ""
                        russian_name = get_russian_name(full_name)
                        desc = f"{summary_text}Имя: {russian_name}\nВопрос: {question}"
                        
                        responsible_id = roster_service.get_responsible_id(chat_id) if roster_service else None
                        roster_entry = roster_service.get_entry(chat_id) if roster_service else None
                        chat_title = roster_entry.chat_title if roster_entry else None
                        
                        task_id = await bitrix_service.create_task(
                            title=title, description=desc, client_user_id=user_id,
                            responsible_id=responsible_id, chat_id=chat_id, chat_title=chat_title
                        )
                        if task_id:
                            link = BitrixTaskLink(client_id=client.id, chat_id=chat_id, task_id=str(task_id), title=title, is_active=True, kind='question')
                            session.add(link)
                            await session.commit()
                        
                        reply_markup = BotKeyboards.get_task_actions(task_id=task_id) if task_id else None
                        await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=final_reply_text)
                        await session.commit()
                        await send_answer(final_reply_text + CLARIFY_INSTRUCTION_HTML, reply_markup=reply_markup, parse_mode="HTML")
                    else:
                        await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=final_reply_text)
                        await session.commit()
                        await send_answer(final_reply_text, parse_mode="HTML")
                return

            # 2. Off-tariff
            is_off_tariff = await ai_service.check_if_off_tariff(question, history)
            if is_off_tariff:
                russian_name = get_russian_name(full_name)
                is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
                response_text = "Принято, подключаю вашего менеджера к диалогу. @EK_ak1 @SK_AK3"
                formatted_response = await ai_service.format_response_with_name(response_text, russian_name, is_first_today)
                await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=formatted_response)
                await session.commit()
                await send_answer(formatted_response)
                
                sales_ids = settings.sales_responsible_ids
                if sales_ids:
                    responsible_id = sales_ids[0]
                    accomplices = sales_ids[1:] if len(sales_ids) > 1 else None
                    task_title = f"Продажа/доп.услуга: {question[:100]}"
                    task_description = f"Клиент '{russian_name}' (@{username or 'N/A'}) запросил услугу вне тарифа.\n\nТекст запроса:\n{question}"
                    await bitrix_service.create_task(
                        title=task_title, description=task_description, client_user_id=user_id,
                        responsible_id=responsible_id, accomplices=accomplices, chat_id=chat_id, chat_title=""
                    )
                return

            # 3. Completeness
            if not skip_clarification:
                is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
                clarifying_question = await ai_service.check_request_completeness(question, history, client_name=full_name, is_first_today=is_first_today)
                if clarifying_question:
                    STATE.set_pending_pre_task_clarification(chat_id, client.id, question)
                    await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=clarifying_question)
                    await session.commit()
                    await send_answer(clarifying_question)
                    return

            # 4. Create Task
            title = f"Вопрос: {(question or '').strip().splitlines()[0][:140]}"
            summary = await ai_service.summarize_for_task(question, history)
            summary_text = f"Контекст диалога:\n{summary}\n\n---\n" if summary else ""
            russian_name = get_russian_name(full_name)
            desc = f"{summary_text}Имя: {russian_name}\nВопрос: {question}"
            
            responsible_id = roster_service.get_responsible_id(chat_id) if roster_service else None
            roster_entry = roster_service.get_entry(chat_id) if roster_service else None
            chat_title = roster_entry.chat_title if roster_entry else None

            task_id = await bitrix_service.create_task(
                title=title, description=desc, client_user_id=user_id,
                responsible_id=responsible_id, chat_id=chat_id, chat_title=chat_title
            )
            if task_id:
                link = BitrixTaskLink(client_id=client.id, chat_id=chat_id, task_id=str(task_id), title=title, is_active=True, kind='question')
                session.add(link)
                await session.commit()
                
                if custom_reply_text:
                    notice = custom_reply_text
                else:
                    in_window = is_processing_window_now(settings.processing_schedule)
                    notice_template = NOTICES.get("task_accepted_worktime" if in_window else "task_accepted_off_hours", "")
                    notice = notice_template.format(name=russian_name)
                
                reply_markup = BotKeyboards.get_task_actions(task_id=task_id)
                await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=notice)
                await session.commit()
                await send_answer(notice + CLARIFY_INSTRUCTION_HTML, reply_markup=reply_markup, parse_mode="HTML")
            else:
                await send_answer("Не удалось создать задачу.")

        # --- ROUTING LOGIC ---
        if pre_task_original_question:
            combined_question = f"{pre_task_original_question}\n\nУточнение: {combined_text}"
            history = await chat_history_service.get_recent_messages(session, chat_id=chat_id, limit=100, exclude_staff=False)
            from telegram_bot.utils.schedule import now_msk
            msk_now = now_msk()
            category = await ai_service.classify_question(combined_question, history, msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'), first_name=first_name, username=username)
            
            if category == QuestionCategory.EXPERT_QUESTION:
                recent_history = history[-4:] if len(history) > 4 else history
                response_text, success = await ai_service.get_expert_answer(combined_question, recent_history)
                if success and response_text:
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
                    final_text = await ai_service.format_response_with_name(response_text, full_name, is_first_today)
                    await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=response_text)
                    await session.commit()
                    await send_answer(final_text, parse_mode="HTML", disable_web_page_preview=True)
                    return
            
            await _create_or_update_task_flow(combined_question, history, skip_clarification=True)
            return

        pending_task_id = STATE.pop_pending_clarify(chat_id, client.user_id)
        if pending_task_id:
            comment_text = f"Уточнение от клиента:\n{combined_text}"
            success = await bitrix_service.add_comment(str(pending_task_id), comment_text)
            reply = "Принято, добавила ваше уточнение к задаче." if success else "Не удалось добавить уточнение."
            await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=reply)
            await session.commit()
            await send_answer(reply)
            return

        history = await chat_history_service.get_recent_messages(session, chat_id=chat_id, limit=100, exclude_staff=False)
        from telegram_bot.utils.schedule import now_msk
        msk_now = now_msk()
        category = await ai_service.classify_question(combined_text, history, msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'), first_name=first_name, username=username)
        logger.info(f"Chat {chat_id}: classified as {category}")

        if category == QuestionCategory.CHITCHAT:
            logger.info(f"Chat {chat_id}: handling as CHITCHAT")
            is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
            response_text = await ai_service.generate_chitchat_response(combined_text, history, msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'), first_name=first_name, username=username, is_first_today=is_first_today)
            await send_answer(response_text)
            await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=response_text)
            await session.commit()
            logger.info(f"Chat {chat_id}: CHITCHAT response sent")
            return

        if category in (QuestionCategory.BITRIX_TASK, QuestionCategory.GENERAL_QUESTION):
            logger.info(f"Chat {chat_id}: handling as BITRIX_TASK/GENERAL_QUESTION")
            await _create_or_update_task_flow(combined_text, history)
            logger.info(f"Chat {chat_id}: task flow completed")
            
        elif category == QuestionCategory.MIXED_QUESTION_AND_TASK:
            logger.info(f"Chat {chat_id}: handling as MIXED_QUESTION_AND_TASK")
            
            # 1. Сначала пробуем локальную БЗ с высоким порогом (0.85)
            playbook = await ai_service.get_kb_playbook(combined_text, session, chat_id, min_confidence=0.85)
            
            if playbook and playbook.get("reply"):
                # БЗ нашла с достаточной уверенностью - используем стандартный flow
                logger.info(f"Chat {chat_id}: MIXED - found in KB (confidence >= 0.85)")
                await _create_or_update_task_flow(combined_text, history)
            else:
                # БЗ не нашла (confidence < 0.85) - генерируем ответ на основе общих знаний GPT
                logger.info(f"Chat {chat_id}: MIXED - KB not found (confidence < 0.85), generating general answer")
                
                msk_now_str = msk_now.strftime('%Y-%m-%d %H:%M:%S')
                general_answer = await ai_service.generate_general_answer(
                    question=combined_text,
                    msk_now=msk_now_str,
                    first_name=first_name
                )
                
                if general_answer:
                    # GPT сгенерировал ответ - отправляем
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
                    final_text = await ai_service.format_response_with_name(general_answer, full_name, is_first_today)
                    await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=general_answer)
                    await session.commit()
                    await send_answer(final_text, parse_mode="HTML", disable_web_page_preview=True)
                    logger.info(f"Chat {chat_id}: MIXED - general GPT answer sent")
                else:
                    logger.info(f"Chat {chat_id}: MIXED - GPT generation failed")
                
                # В любом случае создаём задачу (т.к. есть просьба о действии)
                await _create_or_update_task_flow(combined_text, history, skip_clarification=True)
                
            logger.info(f"Chat {chat_id}: MIXED_QUESTION_AND_TASK completed")
            
        elif category == QuestionCategory.EXPERT_QUESTION:
             logger.info(f"Chat {chat_id}: handling as EXPERT_QUESTION")
             recent_history = history[-4:] if len(history) > 4 else history
             response_text, success = await ai_service.get_expert_answer(combined_text, recent_history)
             if success and response_text:
                 is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=chat_id)
                 final_text = await ai_service.format_response_with_name(response_text, full_name, is_first_today)
                 await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=response_text)
                 await session.commit()
                 await send_answer(final_text, parse_mode="HTML", disable_web_page_preview=True)
                 logger.info(f"Chat {chat_id}: EXPERT_QUESTION response sent")
             else:
                 logger.info(f"Chat {chat_id}: EXPERT_QUESTION failed, creating task")
                 await _create_or_update_task_flow(combined_text, history)

        elif category == QuestionCategory.NON_STANDARD_FAQ:
             logger.info(f"Chat {chat_id}: handling as NON_STANDARD_FAQ")
             response_text, is_first_today, success, _, _ = await ai_service.generate_response(
                 question=combined_text, user_id=user_id, session=session, client_db_id=client.id,
                 chat_id=chat_id, msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'), first_name=first_name, username=username
             )
             if success:
                 await chat_history_service.add_message_to_history(session=session, client_id=client.id, chat_id=chat_id, role="assistant", content=response_text)
                 await session.commit()
                 await send_answer(response_text)
                 logger.info(f"Chat {chat_id}: NON_STANDARD_FAQ response sent")
             else:
                 logger.info(f"Chat {chat_id}: NON_STANDARD_FAQ failed, creating task")
                 await _create_or_update_task_flow(combined_text, history)


@router.message(F.text & ~F.text.startswith('/'))
async def handle_text_message(
    message: Message,
    client: Client | None = None,
    session: AsyncSession | None = None,
    ai_service: AIService | None = None,
    bitrix_service: BitrixService | None = None,
    roster_service: RosterService | None = None,
    chat_history_service: ChatHistoryService | None = None,
):
    """
    Легковесный хендлер: только сохраняет и запускает таймер debounce.
    """
    if ai_service is None or chat_history_service is None or bitrix_service is None:
        await message.answer("Сервисы временно недоступны.")
        return

    # 1. Обеспечиваем клиента
    temp_session: AsyncSession | None = None
    if session is None or client is None:
        temp_session, client = await _ensure_client(None, message)
        session = temp_session

    settings = BotSettings()

    # 2. Фильтр для сотрудников (не отвечаем, но сохраняем в историю)
    if message.from_user.username and message.from_user.username.lstrip('@') in settings.staff_usernames:
        await chat_history_service.add_message_to_history(
            session=session, client_id=client.id, chat_id=message.chat.id,
            role="user", content=message.text
        )
        if temp_session: await temp_session.close()
        return

    # 3. Сохраняем сообщение СРАЗУ
    await chat_history_service.add_message_to_history(
        session=session, client_id=client.id, chat_id=message.chat.id,
        role="user", content=message.text
    )
    
    if temp_session:
        await temp_session.commit()
    
    # 4. Показываем статус "печатает"
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass

    user_id = message.from_user.id
    first_name = message.from_user.first_name or ""
    username = message.from_user.username
    
    if temp_session: await temp_session.close()

    # 5. Запускаем Debounce на 15 секунд
    await debounce_manager.schedule(
        chat_id=message.chat.id,
        callback=process_accumulated_messages,
        delay=15.0,
        # Контекст
        bot_instance=message.bot,
        user_id=user_id,
        first_name=first_name,
        username=username,
        ai_service=ai_service,
        bitrix_service=bitrix_service,
        roster_service=roster_service,
        chat_history_service=chat_history_service
    )
