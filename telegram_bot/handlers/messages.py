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
    Обработчик для всех текстовых сообщений, который сначала классифицирует запрос,
    а затем направляет его по соответствующей логике.
    """
    if ai_service is None or chat_history_service is None or bitrix_service is None:
        await message.answer("Один из сервисов временно недоступен. Попробуйте позже.")
        return

    # 1. Обеспечиваем наличие клиента и сессии
    temp_session: AsyncSession | None = None
    if session is None or client is None:
        temp_session, client = await _ensure_client(None, message)
        session = temp_session

    full_name = client.first_name or (message.from_user.first_name or "")
    user_id = client.user_id
    settings = BotSettings()

    # 2. Фильтр для сотрудников (не отвечаем, но сохраняем в историю)
    if message.from_user.username and message.from_user.username.lstrip('@') in settings.staff_usernames:
        await chat_history_service.add_message_to_history(
            session=session, client_id=client.id, chat_id=message.chat.id,
            role="user", content=message.text
        )
        if temp_session: await temp_session.close()
        return
        
    # 3. Сохраняем сообщение пользователя в историю (ВСЕГДА!)
    original_question_for_clarify = STATE.get_pending_clarify(message.chat.id, client.user_id)
    pre_task_original_question = STATE.pop_pending_pre_task_clarification(message.chat.id, client.id)

    await chat_history_service.add_message_to_history(
        session=session, client_id=client.id, chat_id=message.chat.id,
        role="user", content=message.text
    )

    # 4. Основной роутинг
    async with ChatActionSender.typing(chat_id=message.chat.id, bot=message.bot):
        # Вложенная функция для рефакторинга создания задач
        async def _create_or_update_task_flow(
            question: str, 
            history: list, 
            skip_clarification: bool = False,
            custom_reply_text: Optional[str] = None
        ) -> None:
            """
            Инкапсулирует логику: 
            1. ПРИОРИТЕТ: Поиск по БЗ (если есть ответ - используем его)
            2. Проверка на off-tariff (только если нет ответа в БЗ)
            3. Проверка на полноту -> Уточнение (только если нет ответа в БЗ)
            4. Проверка на переиспользование задачи
            5. Генерация саммари и создание задачи
            """
            # 1. ПРИОРИТЕТ: Поиск по БЗ. Если есть готовый ответ, используем его вместо всех остальных проверок.
            playbook = await ai_service.get_kb_playbook(
                question,
                session,
                message.chat.id,
                min_confidence=0.85,  # Высокий порог для приоритетного поиска
            )
            if playbook:
                reply_text = playbook.get("reply")
                creates_task = playbook.get("create_task") and bitrix_service
                if reply_text:
                    reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                    final_reply_text = await ai_service.format_response_with_name(
                        response=reply_text, client_name=full_name, is_first_today=is_first_today
                    )
                    if creates_task:
                        # Если БЗ говорит создать задачу, создаем новую задачу
                        title = f"Вопрос: {(question or '').strip().splitlines()[0][:140]}"
                        
                        summary = await ai_service.summarize_for_task(question, history)
                        summary_text = f"Контекст диалога:\n{summary}\n\n---\n" if summary else ""
                        # Используем транслитерированное имя в описании задачи
                        russian_name = get_russian_name(full_name)
                        desc = f"{summary_text}Имя: {russian_name}\nВопрос: {question}"
                        
                        responsible_id = roster_service.get_responsible_id(message.chat.id) if roster_service else None
                        roster_entry = roster_service.get_entry(message.chat.id) if roster_service else None
                        chat_title = roster_entry.chat_title if roster_entry else None
                        
                        task_id = await bitrix_service.create_task(
                            title=title,
                            description=desc,
                            client_user_id=user_id,
                            responsible_id=responsible_id,
                            chat_id=message.chat.id,
                            chat_title=chat_title
                        )
                        if task_id:
                            link = BitrixTaskLink(client_id=client.id, chat_id=message.chat.id, task_id=str(task_id), title=title, is_active=True, kind='question')
                            session.add(link)
                            await session.commit()
                            logger.info(f"Created new Bitrix task #{task_id} for user {client.user_id}")
                        
                        # Отправляем ответ с кнопками задачи (только если task_id валидный)
                        reply_markup = BotKeyboards.get_task_actions(task_id=task_id) if task_id else None
                        await message.answer(final_reply_text + CLARIFY_INSTRUCTION_HTML, reply_markup=reply_markup, parse_mode="HTML")
                        await chat_history_service.add_message_to_history(
                            session=session, client_id=client.id, chat_id=message.chat.id,
                            role="assistant", content=final_reply_text
                        )
                    else:
                        # Если БЗ говорит не создавать задачу, просто отвечаем
                        await message.answer(final_reply_text, parse_mode="HTML")
                        await chat_history_service.add_message_to_history(
                            session=session, client_id=client.id, chat_id=message.chat.id,
                            role="assistant", content=final_reply_text
                        )
                return

            # 2. Проверка на off-tariff. Если платная услуга И нет ответа в БЗ, создаем задачу на продажников и выходим.
            is_off_tariff = await ai_service.check_if_off_tariff(question, history)
            if is_off_tariff:
                # Используем транслитерированное имя и проверяем первое обращение сегодня
                russian_name = get_russian_name(full_name)
                is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                response_text = "Принято, подключаю вашего менеджера к диалогу. @EK_ak1 @SK_AK3"
                formatted_response = await ai_service.format_response_with_name(response_text, russian_name, is_first_today)
                await message.answer(formatted_response)
                
                # Сохраняем ответ в историю
                await chat_history_service.add_message_to_history(
                    session=session, client_id=client.id, chat_id=message.chat.id,
                    role="assistant", content=formatted_response
                )
                
                sales_ids = settings.sales_responsible_ids
                if sales_ids:
                    responsible_id = sales_ids[0]
                    accomplices = sales_ids[1:] if len(sales_ids) > 1 else None
                    task_title = f"Продажа/доп.услуга: {question[:100]}"
                    task_description = f"Клиент '{russian_name}' (@{message.from_user.username or 'N/A'}) запросил услугу вне тарифа.\n\n" \
                                       f"Текст запроса:\n{question}"
                    await bitrix_service.create_task(
                        title=task_title,
                        description=task_description,
                        client_user_id=message.from_user.id,
                        responsible_id=responsible_id,
                        accomplices=accomplices,
                        chat_id=message.chat.id,
                        chat_title=message.chat.title
                    )
                return

            # 3. Проверка на полноту. Если вопрос неполный И нет ответа в БЗ, задаем уточнение.
            if not skip_clarification:
                is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                clarifying_question = await ai_service.check_request_completeness(
                    question, history, client_name=full_name, is_first_today=is_first_today
                )
                if clarifying_question:
                    await message.answer(clarifying_question)
                    STATE.set_pending_pre_task_clarification(message.chat.id, client.id, question)
                    await chat_history_service.add_message_to_history(
                        session=session, client_id=client.id, chat_id=message.chat.id,
                        role="assistant", content=clarifying_question
                    )
                    return
                
            # 4. Создаем новую задачу
            title = f"Вопрос: {(question or '').strip().splitlines()[0][:140]}"
            
            summary = await ai_service.summarize_for_task(question, history)
            summary_text = f"Контекст диалога:\n{summary}\n\n---\n" if summary else ""
            # Используем транслитерированное имя в описании задачи
            russian_name = get_russian_name(full_name)
            desc = f"{summary_text}Имя: {russian_name}\nВопрос: {question}"
            
            responsible_id = roster_service.get_responsible_id(message.chat.id) if roster_service else None
            roster_entry = roster_service.get_entry(message.chat.id) if roster_service else None
            chat_title = roster_entry.chat_title if roster_entry else None

            task_id = await bitrix_service.create_task(
                title=title,
                description=desc,
                client_user_id=user_id,
                responsible_id=responsible_id,
                chat_id=message.chat.id,
                chat_title=chat_title
            )
            if task_id:
                link = BitrixTaskLink(client_id=client.id, chat_id=message.chat.id, task_id=str(task_id), title=title, is_active=True, kind='question')
                session.add(link)
                await session.commit()
                logger.info(f"Created new Bitrix task #{task_id} for user {client.user_id}")
            
            # 6. Отправляем ответ пользователю
            if task_id:
                # Используем кастомный ответ если он есть, иначе — стандартное уведомление
                if custom_reply_text:
                    notice = custom_reply_text
                else:
                    in_window = is_processing_window_now(settings.processing_schedule)
                    notice_template = NOTICES.get("task_accepted_worktime" if in_window else "task_accepted_off_hours", "")
                    # Используем транслитерированное имя для уведомлений
                    russian_name = get_russian_name(full_name)
                    notice = notice_template.format(name=russian_name)
                
                # Отправляем ответ с кнопками задачи (только если task_id валидный)
                reply_markup = BotKeyboards.get_task_actions(task_id=task_id) if task_id else None
                await message.answer(notice + CLARIFY_INSTRUCTION_HTML, reply_markup=reply_markup, parse_mode="HTML")
                await chat_history_service.add_message_to_history(
                    session=session, client_id=client.id, chat_id=message.chat.id,
                    role="assistant", content=notice
                )
            else:
                await message.answer("Не удалось создать или обновить задачу. Пожалуйста, попробуйте снова.")

        # Шаг 4.0: Приоритетная проверка на ожидаемое уточнение
        
        # Ответ на уточнение перед созданием задачи
        if pre_task_original_question:
            # Объединяем вопрос + уточнение и повторно маршрутизируем, включая экспертный путь
            combined_question = f"{pre_task_original_question}\n\nУточнение: {message.text}"
            history = await chat_history_service.get_recent_messages(session, chat_id=message.chat.id, limit=100, exclude_staff=False)
            from telegram_bot.utils.schedule import now_msk
            msk_now = now_msk()
            category = await ai_service.classify_question(
                combined_question,
                history,
                msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'),
                first_name=message.from_user.first_name,
                username=message.from_user.username,
            )

            # CHITCHAT маловероятен после уточнения, но обработаем как обычный фолбэк
            if category == QuestionCategory.CHITCHAT:
                await _create_or_update_task_flow(combined_question, history, skip_clarification=True)
                if temp_session: await temp_session.close()
                return

            if category == QuestionCategory.EXPERT_QUESTION:
                playbook = await ai_service.get_kb_playbook(
                    combined_question,
                    session,
                    message.chat.id,
                    min_confidence=0.92,
                )
                if playbook:
                    reply_text = playbook.get("reply")
                    creates_task = playbook.get("create_task") and bitrix_service
                    if reply_text:
                        reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                        is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                        final_reply_text = await ai_service.format_response_with_name(
                            response=reply_text, client_name=full_name, is_first_today=is_first_today
                        )
                        if creates_task:
                            await _create_or_update_task_flow(combined_question, history, skip_clarification=True, custom_reply_text=final_reply_text)
                        else:
                            await message.answer(final_reply_text, parse_mode="HTML")
                            await chat_history_service.add_message_to_history(
                                session=session, client_id=client.id, chat_id=message.chat.id,
                                role="assistant", content=final_reply_text
                            )
                    if temp_session: await temp_session.close()
                    return

                response_text, success = await ai_service.get_expert_answer(combined_question)
                if success and response_text:
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                    final_text = await ai_service.format_response_with_name(
                        response=response_text, client_name=full_name, is_first_today=is_first_today,
                    )
                    await chat_history_service.add_message_to_history(
                        session=session, client_id=client.id, chat_id=message.chat.id,
                        role="assistant", content=response_text
                    )
                    await message.answer(final_text, parse_mode="HTML", disable_web_page_preview=True)
                    if temp_session: await temp_session.close()
                    return
                else:
                    await _create_or_update_task_flow(combined_question, history, skip_clarification=True)
                    if temp_session: await temp_session.close()
                    return

            if category == QuestionCategory.NON_STANDARD_FAQ:
                playbook = await ai_service.get_kb_playbook(
                    combined_question,
                    session,
                    message.chat.id,
                )
                if playbook:
                    reply_text = playbook.get("reply")
                    creates_task = playbook.get("create_task") and bitrix_service
                    if reply_text:
                        reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                        is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                        final_reply_text = await ai_service.format_response_with_name(
                            response=reply_text, client_name=full_name, is_first_today=is_first_today
                        )
                        if creates_task:
                            await _create_or_update_task_flow(
                                question=combined_question,
                                history=history,
                                skip_clarification=True,
                                custom_reply_text=final_reply_text
                            )
                        else:
                            await message.answer(final_reply_text, parse_mode="HTML")
                            await chat_history_service.add_message_to_history(
                                session=session, client_id=client.id, chat_id=message.chat.id,
                                role="assistant", content=final_reply_text
                            )
                    if temp_session: await temp_session.close()
                    return

                # Падение в обычный локальный ответ из БЗ (без Perplexity)
                response_text, is_first_today, success, _, _ = await ai_service.generate_response(
                    question=combined_question,
                    user_id=user_id,
                    session=session,
                    client_db_id=client.id,
                    chat_id=message.chat.id,
                    msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'),
                    first_name=message.from_user.first_name,
                    username=message.from_user.username,
                )
                if success:
                    await chat_history_service.add_message_to_history(
                        session=session, client_id=client.id, chat_id=message.chat.id,
                        role="assistant", content=response_text
                    )
                    await message.answer(response_text)
                    if temp_session: await temp_session.close()
                    return
                else:
                    await _create_or_update_task_flow(combined_question, history, skip_clarification=True)
                    if temp_session: await temp_session.close()
                    return

            # По умолчанию — стандартный поток без повторных уточнений
            await _create_or_update_task_flow(combined_question, history, skip_clarification=True)
            if temp_session: await temp_session.close()
            return

        # Ответ на уточнение для уже созданной задачи
        pending_task_id = STATE.pop_pending_clarify(message.chat.id, client.user_id)
        if pending_task_id:
            comment_text = f"Уточнение от клиента:\n{message.text}"
            success = await bitrix_service.add_comment(str(pending_task_id), comment_text)
                
            if success:
                reply = "Принято, добавила ваше уточнение к задаче."
            else:
                reply = "Не удалось добавить уточнение. Пожалуйста, попробуйте снова."

            await chat_history_service.add_message_to_history(
                session=session, client_id=client.id, chat_id=message.chat.id,
                role="assistant", content=reply
            )
            await message.answer(reply)
            if temp_session: await temp_session.close()
            return

        # Шаг 4.1: Сначала классифицируем, чтобы отсечь chitchat
        # Расширяем контекст до 100 последних сообщений и включаем всех участников чата
        history = await chat_history_service.get_recent_messages(session, chat_id=message.chat.id, limit=100, exclude_staff=False)
        from telegram_bot.utils.schedule import now_msk
        msk_now = now_msk()
        category = await ai_service.classify_question(
            message.text,
            history,
            msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'),
            first_name=message.from_user.first_name,
            username=message.from_user.username,
        )

        # Шаг 4.1.5: Проверка на off-tariff для релевантных категорий - ЭТА ЛОГИКА ПЕРЕМЕЩЕНА ВНУТРЬ _create_or_update_task_flow
        if category in (QuestionCategory.BITRIX_TASK, QuestionCategory.GENERAL_QUESTION):
            # The logic is now inside the helper function
            pass

        # Шаг 4.2: Немедленная обработка простого диалога
        if category == QuestionCategory.CHITCHAT:
            is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
            response_text = await ai_service.generate_chitchat_response(
                user_message=message.text,
                history=history,
                msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'),
                first_name=message.from_user.first_name,
                username=message.from_user.username,
                is_first_today=is_first_today
            )
            await message.answer(response_text)
            await chat_history_service.add_message_to_history(
                session=session, client_id=client.id, chat_id=message.chat.id,
                role="assistant", content=response_text
            )
            if temp_session: await temp_session.close()
            return
            
        # Логика переиспользования задач отключена - всегда создаем новые задачи
        
        # Шаг 4.4: Проверка по Базе Знаний
        # KB lookup будет запускаться позже, в ветках NON_STANDARD_FAQ/GENERAL

        # A.2) Основной флоу создания задачи
        if category in (QuestionCategory.BITRIX_TASK, QuestionCategory.GENERAL_QUESTION):
            # Теперь off-tariff проверка будет внутри _create_or_update_task_flow
            await _create_or_update_task_flow(message.text, history)

        # B) Вопросы для Perplexity (внешняя БЗ)
        elif category == QuestionCategory.EXPERT_QUESTION:
            # Prefer локальную БЗ, если попадание очень уверенное
            playbook = await ai_service.get_kb_playbook(
                message.text,
                session,
                message.chat.id,
                min_confidence=0.92,
            )
            if playbook:
                reply_text = playbook.get("reply")
                creates_task = playbook.get("create_task") and bitrix_service
                if reply_text:
                    reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                    final_reply_text = await ai_service.format_response_with_name(
                        response=reply_text, client_name=full_name, is_first_today=is_first_today
                    )
                    if creates_task:
                        await _create_or_update_task_flow(message.text, history, skip_clarification=True, custom_reply_text=final_reply_text)
                    else:
                        await message.answer(final_reply_text, parse_mode="HTML")
                        await chat_history_service.add_message_to_history(
                            session=session, client_id=client.id, chat_id=message.chat.id,
                            role="assistant", content=final_reply_text
                        )
                if temp_session: await temp_session.close()
                return

            response_text, success = await ai_service.get_expert_answer(message.text)
            if success and response_text:
                is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                final_text = await ai_service.format_response_with_name(
                    response=response_text, client_name=full_name, is_first_today=is_first_today,
                )
                await chat_history_service.add_message_to_history(
                    session=session, client_id=client.id, chat_id=message.chat.id,
                    role="assistant", content=response_text
                )
                await message.answer(final_text, parse_mode="HTML", disable_web_page_preview=True)
            elif bitrix_service:
                await _create_or_update_task_flow(message.text, history)

        # C) Вопросы для внутренней БЗ (нестандартные)
        elif category == QuestionCategory.NON_STANDARD_FAQ:
            # Сначала пробуем локальную БЗ (повышенный порог)
            playbook = await ai_service.get_kb_playbook(
                message.text,
                session,
                message.chat.id,
            )
            if playbook:
                reply_text = playbook.get("reply")
                creates_task = playbook.get("create_task") and bitrix_service

                if reply_text:
                    reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                    final_reply_text = await ai_service.format_response_with_name(
                        response=reply_text, client_name=full_name, is_first_today=is_first_today
                    )

                    if creates_task:
                        history = await chat_history_service.get_recent_messages(session, chat_id=message.chat.id, limit=100, exclude_staff=False)
                        await _create_or_update_task_flow(
                            question=message.text, 
                            history=history, 
                            skip_clarification=True,
                            custom_reply_text=final_reply_text
                        )
                    else:
                        await message.answer(final_reply_text, parse_mode="HTML")
                        await chat_history_service.add_message_to_history(
                            session=session, client_id=client.id, chat_id=message.chat.id,
                            role="assistant", content=final_reply_text
                        )
                if temp_session: await temp_session.close()
                return

            response_text, is_first_today, success, _, _ = await ai_service.generate_response(
                question=message.text,
                user_id=user_id,
                session=session,
                client_db_id=client.id,
                chat_id=message.chat.id,
                msk_now=msk_now.strftime('%Y-%m-%d %H:%M:%S'),
                first_name=message.from_user.first_name,
                username=message.from_user.username,
            )
            
            if success:
                # Ответ из generate_response уже содержит приветствие и имя, доп. форматирование не нужно
                await chat_history_service.add_message_to_history(
                    session=session, client_id=client.id, chat_id=message.chat.id,
                    role="assistant", content=response_text
                )
                await message.answer(response_text)
            elif bitrix_service:
                await _create_or_update_task_flow(message.text, history)

        # D) Общие вопросы (внутренняя логика, без теории)
        elif category == QuestionCategory.GENERAL_QUESTION:
            # Попытка KB с повышенным порогом
            playbook = await ai_service.get_kb_playbook(
                message.text,
                session,
                message.chat.id,
            )
            if playbook:
                reply_text = playbook.get("reply")
                creates_task = playbook.get("create_task") and bitrix_service
                if reply_text:
                    reply_text = await ai_service.strip_kb_header_with_llm(reply_text)
                    is_first_today = await chat_history_service.is_first_assistant_reply_today(session, chat_id=message.chat.id)
                    final_reply_text = await ai_service.format_response_with_name(
                        response=reply_text, client_name=full_name, is_first_today=is_first_today
                    )
                    if creates_task:
                        await _create_or_update_task_flow(message.text, history, skip_clarification=True, custom_reply_text=final_reply_text)
                    else:
                        await message.answer(final_reply_text, parse_mode="HTML")
                        await chat_history_service.add_message_to_history(
                            session=session, client_id=client.id, chat_id=message.chat.id,
                            role="assistant", content=final_reply_text
                        )
                if temp_session: await temp_session.close()
                return
            # иначе — обычный поток создания задачи
            await _create_or_update_task_flow(message.text, history)
    
    # Закрываем временную сессию
    if temp_session:
        await temp_session.close()
