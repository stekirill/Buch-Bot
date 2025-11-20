from typing import Dict, List, Optional
from datetime import datetime
import aiohttp
from telegram_bot.config.settings import BotSettings
from telegram_bot.utils.transliteration import get_russian_name
import re
from loguru import logger
from urllib.parse import urlparse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc


class BitrixService:
    """
    Минимальная обёртка над Bitrix REST. Создание задачи через webhook.
    """
    def __init__(self, settings: BotSettings):
        if settings.bitrix_webhook:
            self.webhook_base = settings.bitrix_webhook.rstrip('/')
            parsed_url = urlparse(self.webhook_base)
            self.portal_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        else:
            self.webhook_base = None
            self.portal_base_url = None
        
        # Фолбэк может быть задан через .env (DEFAULT_RESPONSIBLE_ID)
        self.default_responsible_id = settings.default_responsible_id
        self.timeout = aiohttp.ClientTimeout(total=45)

    async def find_similar_active_task(self, title_fragment: str, client_user_id: int, chat_id: int) -> Optional[str]:
        """Ищет активную задачу по фрагменту заголовка для конкретного пользователя В КОНКРЕТНОМ ЧАТЕ."""
        if not self.webhook_base:
            return None
        url = f"{self.webhook_base}/tasks.task.list.json"
        payload = {
            "order": {"ID": "DESC"},
            "filter": {
                "%TITLE": title_fragment,
                "%DESCRIPTION": f"[TG_USER_ID={client_user_id}]",
                "!STATUS": 5,  # 5 = Завершена
            },
            "select": ["ID", "TITLE", "DESCRIPTION"]
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    tasks = data.get("result", {}).get("tasks", [])
                    if not tasks:
                        return None
                    chat_tag = f"[TG_CHAT_ID={chat_id}]"
                    for t in tasks:
                        descr = t.get("description") or t.get("DESCRIPTION") or ""
                        if isinstance(descr, str) and chat_tag in descr:
                            return str(t.get("id") or t.get("ID"))
        except Exception as e:
            logger.error(f"Ошибка поиска похожей задачи в Битрикс: {e}")
        return None

    async def create_task(self, title: str, description: str, client_user_id: int, responsible_id: Optional[int] = None, chat_id: Optional[int] = None, chat_title: Optional[str] = None, accomplices: Optional[List[int]] = None) -> Optional[str]:
        """
        Создаёт новую задачу в Битрикс24.
        Возвращает ID созданной задачи или None.
        """
        # Отключаем автоматическое переиспользование задач по похожим заголовкам
        # Это может приводить к нежелательному объединению разных вопросов
        # title_fragment = title.replace("Вопрос:", "").strip()[:50]
        # existing_task_id = await self.find_similar_active_task(title_fragment, client_user_id, chat_id)
        # if existing_task_id:
        #     logger.info(f"Найдена похожая активная задача #{existing_task_id} в этом же чате. Добавляем комментарий.")
        #     comment_text = f"Пользователь задал похожий вопрос:\n{description}"
        #     await self.add_comment(existing_task_id, comment_text)
        #     return existing_task_id

        if not self.webhook_base:
            return None
        responsible = responsible_id if responsible_id is not None else self.default_responsible_id
        
        # Проверяем, что исполнитель указан (обязательное поле для Bitrix)
        if responsible is None:
            logger.error(
                f"Не указан исполнитель для задачи. "
                f"responsible_id={responsible_id}, default_responsible_id={self.default_responsible_id}, "
                f"chat_id={chat_id}, client_user_id={client_user_id}"
            )
            return None
        
        url = f"{self.webhook_base}/tasks.task.add.json"
        
        # Добавляем ID пользователя и чата как надежные метки
        tags = [f"[TG_USER_ID={client_user_id}]"]
        if chat_id is not None:
            tags.append(f"[TG_CHAT_ID={chat_id}]")
        
        # Используем chat_title если он есть, иначе chat_id
        chat_identifier = chat_title if chat_title else f"Чат {chat_id}"
        final_description = f"{description}\n\nЧат: {chat_identifier}\n" + " ".join(tags)
        
        fields: Dict[str, object] = {
            "TITLE": title,
            "DESCRIPTION": final_description,
            "RESPONSIBLE_ID": responsible,  # Теперь всегда добавляем, т.к. проверили выше
        }
        if accomplices:
            fields["ACCOMPLICES"] = accomplices

        payload = {"fields": fields}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            f"Ошибка при создании задачи в Битрикс: {resp.status}, "
                            f"message='{resp.reason}', url='{url}', "
                            f"response_body='{error_text}', payload={payload}"
                        )
                        resp.raise_for_status()
                    data = await resp.json()
                    result = data.get("result")
                    
                    if isinstance(result, dict):
                        task_id = result.get("task", {}).get("id")
                        if task_id: return str(task_id)
                    
                    logger.warning(f"Не удалось создать задачу в Битрикс. Ответ: {data}")
                    return None
        except aiohttp.ClientResponseError as e:
            logger.error(
                f"Ошибка HTTP при создании задачи в Битрикс: {e.status}, "
                f"message='{e.message}', url='{url}', payload={payload}"
            )
            return None
        except Exception as e:
            logger.error(f"Ошибка при создании задачи в Битрикс: {e}, payload={payload}")
            return None

    async def get_task_updates(self, since: datetime) -> List[Dict]:
        """
        Получает обновленные задачи и все комментарии к ним для последующей фильтрации.
        """
        if not self.webhook_base:
            return []
            
        url = f"{self.webhook_base}/tasks.task.list.json"
        payload = {
            "order": {"ID": "DESC"},
            "filter": {
                "%DESCRIPTION": "[TG_USER_ID=",
                "!STATUS": 5, # Только активные
            },
            "select": ["ID", "TITLE", "STATUS", "DESCRIPTION"]
        }
        
        updated_tasks = []
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    tasks = data.get("result", {}).get("tasks", [])
                    if not tasks: return []

                    for task in tasks:
                        description_text = task.get("description", "")
                        client_id_match = re.search(r'\[TG_USER_ID=(\d+)\]', description_text)
                        chat_id_match = re.search(r'\[TG_CHAT_ID=(-?\d+)\]', description_text)
                        if not client_id_match:
                            continue
                        client_user_id = int(client_id_match.group(1))
                        chat_id_val = int(chat_id_match.group(1)) if chat_id_match else None

                        all_comments = await self._get_all_user_comments(task["id"])
                        if not all_comments:
                            continue
                        
                        updated_tasks.append({
                            "task_id": task["id"],
                            "client_user_id": client_user_id,
                            "chat_id": chat_id_val,
                            "status": task["status"],
                            "all_comments": all_comments,
                        })
            return updated_tasks
        except Exception as e:
            logger.error(f"Ошибка при получении обновлений задач из Битрикс: {e}")
            return []

    async def _get_all_user_comments(self, task_id: str) -> List[Dict]:
        """Возвращает список всех пользовательских комментариев, отсортированных по дате."""
        url = f"{self.webhook_base}/task.commentitem.getlist.json"
        payload = { "taskId": task_id }
        
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    comments_data = await resp.json()
                    comments = comments_data.get("result", [])
                    if not comments:
                        return []

                    user_comments = [
                        c for c in comments
                        if int(c.get("AUTHOR_ID", 0)) > 0 and str(c.get("AUX", "")).upper() != "Y"
                    ]
                    if not user_comments:
                        return []
                        
                    user_comments.sort(key=lambda c: datetime.fromisoformat(c['POST_DATE']))

                    cleaned_comments = []
                    for comment in user_comments:
                        raw = comment.get("POST_MESSAGE") or ""
                        
                        # Фильтруем системные/ботовские комментарии по префиксам в сыром тексте
                        bot_prefixes = (
                            "Уточнение от клиента:",
                            "[URGENT]",
                            "Пользователь задал похожий вопрос:",
                            "[TG_ATTACH]",
                        )
                        if raw.strip().startswith(bot_prefixes):
                            continue

                        cleaned = re.sub(r"\[/?(USER|URL|B|I|U|CODE|QUOTE)(?:=[^\]]+)?\]", "", raw).strip()
                        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
                        if lines and re.match(r"^[\wА-Яа-яЁё]+\d{1,2}:\d{2}$", lines[0] or ""):
                            lines = lines[1:]
                        cleaned_text = "\n".join(lines).strip()
                        
                        # Дополнительная фильтрация служебных авто-комментариев
                        lowered_text = cleaned_text.lower()
                        if not (comment.get("ATTACHED_OBJECTS")): # не фильтруем комменты с файлами
                            if "назначен исполнителем" in lowered_text or \
                               "назначена исполнителем" in lowered_text or \
                                "назначены исполнителем" in lowered_text or \
                               "необходимо указать крайний срок" in lowered_text or \
                               "изменил крайний срок" in lowered_text or \
                               "изменила крайний срок" in lowered_text or \
                                "завершите задачу или передвиньте срок" in lowered_text:
                                continue

                        files = []
                        attached_objects = comment.get("ATTACHED_OBJECTS")
                        if attached_objects and isinstance(attached_objects, dict) and self.portal_base_url:
                            for att in attached_objects.values():
                                download_url = att.get("DOWNLOAD_URL")
                                name = att.get("NAME")
                                if download_url and name:
                                    full_url = self.portal_base_url + download_url if download_url.startswith("/") else download_url
                                    files.append({"name": name, "url": full_url})
                        
                        if cleaned_text or files:
                             cleaned_comments.append({
                                 "id": str(comment.get("ID")), 
                                 "text": cleaned_text, 
                                 "files": files
                            })
                    return cleaned_comments

        except Exception as e:
            logger.error(f"Ошибка получения комментариев для задачи {task_id}: {e}")
        return []

    async def get_files_by_attached_ids(self, attached_ids: List[str]) -> List[Dict]:
        """Возвращает [{name, url}] по списку ID прикреплений (ATTACHMENT_ID).
        Делает disk.attachedobject.get -> disk.file.get для каждого ID."""
        if not self.webhook_base or not attached_ids:
            return []

        results: List[Dict] = []
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                for att_id in attached_ids:
                    # 1) Получаем объект диска (file id)
                    url_att = f"{self.webhook_base}/disk.attachedobject.get.json"
                    payload_att = {"id": att_id}
                    async with session.post(url_att, json=payload_att) as resp_att:
                        resp_att.raise_for_status()
                        data_att = await resp_att.json()
                        att_obj = data_att.get("result") or {}
                        file_id = att_obj.get("OBJECT_ID")
                        if not file_id:
                            continue
                    # 2) Получаем файл и рабочую ссылку на скачивание
                    url_file = f"{self.webhook_base}/disk.file.get.json"
                    payload_file = {"id": file_id}
                    async with session.post(url_file, json=payload_file) as resp_file:
                        resp_file.raise_for_status()
                        data_file = await resp_file.json()
                        file_obj = data_file.get("result") or {}
                        name = file_obj.get("NAME")
                        dl_url = file_obj.get("DOWNLOAD_URL")
                        if not name or not dl_url:
                            continue
                        # DOWNLOAD_URL может быть относительным
                        if dl_url.startswith("/") and self.portal_base_url:
                            dl_url = self.portal_base_url + dl_url
                        results.append({"name": name, "url": dl_url})
        except Exception as e:
            logger.error(f"Ошибка при получении ссылок для вложений: {e}")
        return results

    async def update_task_status(self, task_id: str, status: str) -> bool:
        return False

    async def get_client_company(self, client_telegram_id: int) -> Optional[Dict]:
        return None

    async def get_task_brief(self, task_id: str) -> Optional[Dict]:
        """Возвращает краткую информацию по задаче (id, title, status, description, deadline)."""
        if not self.webhook_base:
            return None
        url = f"{self.webhook_base}/tasks.task.get.json"
        payload = {"taskId": task_id}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    result = data.get("result")
                    task: Optional[Dict] = None
                    # Typical shape: {"result": {"task": {...}}}
                    if isinstance(result, dict):
                        if isinstance(result.get("task"), dict):
                            task = result.get("task")
                        # Sometimes API may return the task fields directly under result
                        elif any(k in result for k in ("id", "title", "status")):
                            task = result  # type: ignore[assignment]
                    # Rare shape: {"result": [{"task": {...}}]} or list of dicts
                    if task is None and isinstance(result, list):
                        for item in result:
                            if isinstance(item, dict):
                                if isinstance(item.get("task"), dict):
                                    task = item.get("task")
                                    break
                                if any(k in item for k in ("id", "title", "status")):
                                    task = item
                                    break
                    if not isinstance(task, dict):
                        return None
                    # Обрезаем длинное описание для компактного статуса
                    raw_desc = (task.get("description") or "").strip()
                    clean_desc = re.sub(r"\[/?(USER|URL|B|I|U|CODE|QUOTE)(?:=[^\]]+)?\]", "", raw_desc)
                    if len(clean_desc) > 180:
                        clean_desc = clean_desc[:177] + "..."
                    return {
                        "id": task.get("id"),
                        "title": task.get("title"),
                        "status": task.get("status"),
                        "deadline": task.get("deadline"),
                        "description": clean_desc,
                    }
        except Exception as e:
            logger.error(f"Ошибка получения задачи {task_id}: {e}")
            return None

    async def list_active_tasks_for_chat(self, chat_id: int, session: AsyncSession) -> List[Dict]:
        """Возвращает активные задачи для чата, используя локальную БД и получая актуальные статусы из Битрикс.
        
        Shape: [{"id": str, "title": str, "status": Any, "description": str, "deadline": str}]
        """
        # Сначала получаем задачи из локальной БД
        from telegram_bot.database.models import BitrixTaskLink
        
        links_row = await session.execute(
            select(BitrixTaskLink)
            .where(BitrixTaskLink.chat_id == chat_id, BitrixTaskLink.is_active == True)
            .order_by(desc(BitrixTaskLink.created_at))
            .limit(10)
        )
        links = list(links_row.scalars())
        
        if not links:
            return []
        
        # Получаем актуальные статусы из Битрикс для каждой задачи
        results: List[Dict] = []
        for link in links:
            brief = await self.get_task_brief(link.task_id)
            if brief:
                results.append({
                    "id": brief.get("id"),
                    "title": brief.get("title") or "",
                    "status": brief.get("status"),
                    "description": brief.get("description") or "",
                    "deadline": brief.get("deadline") or "",
                })
        
        return results

    async def find_or_create_docs_task(self, chat_id: int, client_user_id: int, client_name: str, responsible_id: Optional[int] = None) -> Optional[str]:
        """Находит открытую задачу 'Документы из чата <chat_id>' для пользователя или создаёт новую."""
        if not self.webhook_base:
            return None
        title = f"Документы из чата {chat_id}"
        # Ищем по заголовку и метке TG_USER_ID
        url = f"{self.webhook_base}/tasks.task.list.json"
        payload = {
            "arOrder": {"ID": "DESC"},
            "arFilter": {
                "TITLE": title,
                "%DESCRIPTION": f"[TG_USER_ID={client_user_id}]",
                "!STATUS": 5  # исключим завершённые
            },
            "arSelect": ["ID", "TITLE", "STATUS"]
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    tasks = data.get("result", {}).get("tasks", [])
                    if tasks:
                        return str(tasks[0].get("id"))
        except Exception as e:
            logger.error(f"Ошибка поиска задачи 'Документы из чата {chat_id}': {e}")
        # Создаём новую
        # Используем транслитерированное имя в описании задачи
        russian_name = get_russian_name(client_name)
        desc = f"Имя: {russian_name}\nЧат: {chat_id}\nТип: Документы из чата"
        return await self.create_task(title=title, description=desc, client_user_id=client_user_id, responsible_id=responsible_id, chat_id=chat_id)

    async def add_comment(self, task_id: str, text: str) -> bool:
        """Добавляет комментарий к задаче."""
        if not self.webhook_base:
            return False
        url = f"{self.webhook_base}/task.commentitem.add.json"
        payload = {
            "taskId": task_id,
            "fields": {"POST_MESSAGE": text}
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return bool(data.get("result"))
        except Exception as e:
            logger.error(f"Ошибка добавления комментария в задачу {task_id}: {e}")
            return False

    async def find_active_question_task(self, client_user_id: int) -> Optional[str]:
        """Ищет последнюю активную задачу-вопрос для пользователя по метке TG_USER_ID и статусу."""
        if not self.webhook_base:
            return None
        url = f"{self.webhook_base}/tasks.task.list.json"
        payload = {
            "arOrder": {"ID": "DESC"},
            "arFilter": {
                "%DESCRIPTION": f"[TG_USER_ID={client_user_id}]",
                "!STATUS": 5
            },
            "arSelect": ["ID", "TITLE", "STATUS"]
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    tasks = data.get("result", {}).get("tasks", [])
                    if tasks:
                        return str(tasks[0].get("id"))
        except Exception as e:
            logger.error(f"Ошибка поиска активной задачи для пользователя {client_user_id}: {e}")
        return None
