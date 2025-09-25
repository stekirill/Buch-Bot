from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram_bot.services.bitrix_service import BitrixService
from aiogram import Bot
from datetime import datetime, timedelta, timezone
from loguru import logger
import asyncio
from telegram_bot.database.engine import async_session_factory
from sqlalchemy import select, update
from telegram_bot.database.models import BitrixTaskLink, Client
from aiogram.types import BufferedInputFile
import aiohttp
from telegram_bot.utils.keyboards import BotKeyboards
from telegram_bot.services.state import STATE
from telegram_bot.config.constants import CLARIFY_INSTRUCTION_HTML

# Временное хранилище времени последней проверки (МСК)
MSK = timezone(timedelta(hours=3))
LAST_CHECKED_TIME = datetime.now(tz=MSK) - timedelta(hours=1)


def is_clarification_needed(text: str) -> bool:
    """Проверяет, является ли текст запросом на уточнение."""
    if not text:
        return False
    text = text.lower().strip()
    if text.endswith("?"):
        return True
    
    keywords = [
        "уточните", "пришлите", "подскажите", "направьте", "сообщите", 
        "опишите", "расскажите", "нужн", "требуется" # 'нужен', 'нужна', 'нужно'
    ]
    
    return any(keyword in text for keyword in keywords)


class TaskScheduler:
    def __init__(self, bot: Bot, bitrix_service: BitrixService):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self.bot = bot
        self.bitrix = bitrix_service
        
    async def setup_jobs(self):
        # Проверка обновлений каждые 3 минуты
        self.scheduler.add_job(
            self.check_task_updates,
            CronTrigger(minute="*/1"),
            id="check_updates"
        )
        
        # Ежедневная отчетность в 21:00
        self.scheduler.add_job(
            self.send_daily_report,
            CronTrigger(hour=21, minute=0),
            id="daily_report"
        )

    def start(self):
        self.scheduler.start()

    def shutdown(self):
        self.scheduler.shutdown()
    
    async def check_task_updates(self):
        global LAST_CHECKED_TIME
        logger.info(f"Проверка обновлений в Битрикс (медленный процесс)...")
        
        try:
            updates = await self.bitrix.get_task_updates(since=LAST_CHECKED_TIME)
            if not updates:
                logger.info("Нет активных задач для проверки.")
                return

            logger.info(f"Найдено {len(updates)} активных задач с пользователями TG. Проверяем новые комментарии...")

            async with async_session_factory() as session:
                tasks_processed_with_new_comments = set()
                
                for update_item in updates:
                    task_id = update_item.get("task_id")
                    client_id = update_item.get("client_user_id")
                    target_chat_id = update_item.get("chat_id")
                    status_code = update_item.get("status")
                    all_comments = update_item.get("all_comments", [])

                    if not (task_id and client_id):
                        continue
                        
                    # 1. Получаем из БД ID последнего отправленного комментария
                    link_row = await session.execute(
                        select(BitrixTaskLink).where(BitrixTaskLink.task_id == task_id)
                    )
                    link = link_row.scalars().one_or_none()
                    last_known_comment_id = link.last_comment_id if link else None

                    # 2. Фильтруем неотправленные комментарии
                    new_comments = []
                    found_last = not last_known_comment_id
                    for c in all_comments:
                        if found_last:
                            new_comments.append(c)
                        if c['id'] == last_known_comment_id:
                            found_last = True

                    if not new_comments:
                        continue

                    logger.info(f"Найдено {len(new_comments)} новых комментариев для задачи #{task_id}.")
                    
                    # 3. Получаем детали задачи (один раз) и отправляем комментарии по одному
                    brief = await self.bitrix.get_task_brief(task_id)

                    for comment in new_comments:
                        response = comment.get("text")
                        comment_id = comment.get("id")
                        response_files = comment.get("files", [])
                        
                        if not (response or response_files):
                            continue

                        question = None
                        if brief:
                            title = (brief.get("title") or "").strip()
                            descr = (brief.get("description") or "").strip()
                            if title:
                                question = title.replace("Вопрос:", "", 1).strip()
                            elif descr:
                                question = descr.splitlines()[0]

                        header = f"Ответ по вопросу: {question}" if question else "Ответ по вашей задаче"
                        text = f"{header}\n\n<b>{response}</b>" if response else header

                        try:
                            # Отправляем сообщение и файлы
                            reply_markup = None
                            final_text = text
                            parse_mode = "HTML"

                            if response and is_clarification_needed(response):
                                reply_markup = BotKeyboards.get_task_actions(task_id=task_id)
                                # Запоминаем, что мы ждем уточнения по этой задаче
                                chat_for_state = target_chat_id or client_id
                                # STATE.set_pending_clarify(chat_for_state, client_id, task_id) # Убрано - будет в коллбэке
                                final_text += CLARIFY_INSTRUCTION_HTML
                                

                            if response:
                                await self.bot.send_message(
                                    chat_id=target_chat_id or client_id, 
                                    text=final_text,
                                    reply_markup=reply_markup,
                                    parse_mode=parse_mode
                                )
                            if response_files:
                                for file_info in response_files:
                                    # ... (логика отправки файлов осталась та же)
                                    file_url, file_name = file_info.get("url"), file_info.get("name")
                                    if not file_url or not file_name: continue
                                    try:
                                        async with aiohttp.ClientSession() as http_session:
                                            async with http_session.get(file_url) as file_resp:
                                                if file_resp.status == 200:
                                                    file_content = await file_resp.read()
                                                    input_file = BufferedInputFile(file_content, filename=file_name)
                                                    await self.bot.send_document(chat_id=target_chat_id or client_id, document=input_file)
                                                else:
                                                    await self.bot.send_message(chat_id=target_chat_id or client_id, text=f"Не удалось скачать файл из Битрикс: {file_name}")
                                    except Exception as e:
                                        logger.error(f"Ошибка при отправке файла: {e}")

                            logger.success(f"Комментарий #{comment_id} к задаче #{task_id} отправлен в чат {target_chat_id or client_id}")
                            
                            tasks_processed_with_new_comments.add(task_id)
                            
                            # 4. Атомарно обновляем/создаем ссылку с ID *этого* комментария
                            if link:
                                link.last_comment_id = comment_id
                                if int(status_code) == 5: link.is_active = False
                            else:
                                client_row = await session.execute(select(Client).where(Client.user_id == client_id))
                                client_obj = client_row.scalars().one_or_none()
                                if client_obj:
                                    link = BitrixTaskLink(
                                        client_id=client_obj.id,
                                        chat_id=target_chat_id,
                                        task_id=task_id,
                                        title=brief.get('title') if brief else None,
                                        status=str(status_code),
                                        last_comment_id=comment_id,
                                        is_active=(str(status_code) != '5'),
                                        kind='question'
                                    )
                                    session.add(link)
                            
                            await session.commit()

                        except Exception as e:
                            logger.error(f"Не удалось отправить комментарий #{comment_id} для задачи #{task_id}: {e}")
                            await session.rollback()
                            break # Прерываем обработку комм-ев для ЭТОЙ задачи, попробуем в след. раз
                
                # Обновляем статус задачи в ссылке, если новых комментариев не было, но статус изменился
                if not new_comments and link and str(link.status) != str(status_code):
                    try:
                        link.status = str(status_code)
                        if int(status_code) == 5:
                            link.is_active = False
                        await session.commit()
                        logger.info(f"Обновлен статус задачи #{task_id} на {status_code}.")
                    except Exception as e:
                        logger.error(f"Ошибка обновления статуса для задачи #{task_id}: {e}")
                        await session.rollback()

            if tasks_processed_with_new_comments:
                LAST_CHECKED_TIME = datetime.now(tz=MSK)
                logger.info(f"Обработаны комментарии для {len(tasks_processed_with_new_comments)} задач. Время обновлено до {LAST_CHECKED_TIME}")

        except Exception as e:
            logger.error(f"Критическая ошибка при проверке обновлений задач: {e}")
        
    async def send_daily_report(self):
        pass
