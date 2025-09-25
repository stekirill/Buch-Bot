import openai
from typing import Optional, Dict, Any, List
from telegram_bot.config.settings import BotSettings
from telegram_bot.services.knowledge_base import KnowledgeBaseService
from telegram_bot.services.chat_history_service import ChatHistoryService
from sqlalchemy.ext.asyncio import AsyncSession
import random
import re
import html as _html
from enum import Enum
from telegram_bot.services.perplexity_service import PerplexityService
from telegram_bot.utils.schedule import now_msk
from telegram_bot.utils.transliteration import get_russian_name
import json
from loguru import logger


class QuestionCategory(str, Enum):
    """Категории запросов для классификации с помощью LLM."""
    CHITCHAT = "chitchat"  # Приветствия, благодарности, прощания
    NON_STANDARD_FAQ = "non_standard_faq"  # Вопрос из локальной БЗ (нестандартные кейсы)
    EXPERT_QUESTION = "expert_question"   # Требует ссылок на законы, не требует контекста клиента
    BITRIX_TASK = "bitrix_task"           # Требует действия от человека или данных о клиенте
    GENERAL_QUESTION = "general_question" # Общий вопрос, который не попал в другие категории


class AIService:
    def __init__(self, settings: BotSettings, knowledge_base: KnowledgeBaseService, perplexity: Optional[PerplexityService] = None):
        self.settings = settings
        self.knowledge_base = knowledge_base
        self.client = openai.AsyncOpenAI(api_key=self.settings.openai_api_key)
        self.history = ChatHistoryService()
        self.perplexity = perplexity


    async def check_if_off_tariff(self, question: str, history: List[Dict[str, str]]) -> bool:
        """
        Uses LLM to decide if a request is a new paid service outside of the standard subscription.
        """
        history_slice = history[-100:] if len(history) > 100 else history
        history_str = "\n".join(f"{m['role']}: {m['content']}" for m in history_slice)

        system_prompt = """
        Ты — ИИ-ассистент, который помогает определить, является ли запрос клиента запросом на новую платную услугу, выходящую за рамки стандартного бухгалтерского обслуживания.

        ПРАВИЛА АНАЛИЗА:

        1.  **Стандартное обслуживание (is_off_tariff: false):**
            - Вопросы по текущим налогам, отчетам, зарплатам.
            - Запросы на получение существующих документов (акты, счета).
            - Простые консультации по бухгалтерии ("Как рассчитать НДФЛ?").
            - Уточнения или дополнения к уже существующей, активной задаче (которую видно из истории).
            - Инциденты и срочные ситуации из плейбуков БЗ: требования ФНС, блокировка счёта банком по запросу ФНС, уведомления ИФНС/СФР, документы/пояснения по требованиям. Эти сценарии обрабатываются по внутренним регламентам и НЕ считаются off-tariff.
            - Стандартные запросы, связанные с текущим обслуживанием: выставление счёта за услуги, отправка акта сверки, запрос на выгрузку документов.

        2.  **Платная услуга (is_off_tariff: true):**
            - Явный запрос на создание НОВОГО документа, который не является стандартным (например, "составить договор купли-продажи", "подготовить документы для тендера").
            - Запросы на регистрационные действия ("зарегистрировать онлайн-кассу", "снять кассу с учета").
            - Запросы на сложные аналитические отчеты, не входящие в стандартный пакет ("сравните мою выручку с прошлым годом", "покажите структуру расходов").
            - Просьбы о помощи в нестандартных ситуациях, требующих активного вмешательства ("помогите разблокировать счет в банке").

        ПРИМЕРЫ:
        - "В банке заблокировался счёт. Говорят, по запросу ФНС" -> {"is_off_tariff": false}
        - "Мне пришло требование из ИФНС, что делать?" -> {"is_off_tariff": false}
        - "Мне нужен счёт на оплату услуг бухгалтерии" -> {"is_off_tariff": false}
        - "Составьте договор купли-продажи" -> {"is_off_tariff": true}

        ВАЖНО: Не считай off-tariff простые вопросы или продолжение уже начатого обсуждения. Только если это явный запрос на НОВУЮ РАБОТУ.

        Твой ответ должен быть ТОЛЬКО `true` или `false` в формате JSON: {"is_off_tariff": boolean}.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"ИСТОРИЯ ДИАЛОГА:\n{history_str}\n---\nПОСЛЕДНИЙ ВОПРОС КЛИЕНТА: \"{question}\""}
        ]

        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            result_str = completion.choices[0].message.content
            result = json.loads(result_str or "{}")
            decision = bool(result.get("is_off_tariff", False))
            logger.info(f"off-tariff decision: question='{question[:80]}...' -> {decision}")
            return decision
        except Exception as e:
            logger.error(f"Ошибка при проверке off-tariff: {e}")
            return False

    async def check_request_completeness(self, question: str, history: List[Dict[str, str]], client_name: str = None, is_first_today: bool = False) -> Optional[str]:
        """
        Проверяет, достаточно ли в запросе информации для создания задачи.
        Если нет — возвращает уточняющий вопрос с именем и приветствием.
        """
        history_str = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        system_prompt = """
        Ты — внимательный ИИ-ассистент бухгалтера. Твоя задача — проанализировать последний запрос клиента на предмет полноты информации. Учитывай контекст диалога. 

        ПРАВИЛА:
        1.  **Если запрос ПОЛНЫЙ** и содержит всю необходимую информацию для выполнения (например, конкретные даты, суммы, названия документов), верни ТОЛЬКО слово `null`.
            - "Пришлите акт сверки с ООО 'Ромашка' за 3 квартал 2023 года" -> `null`
            - "Рассчитайте зарплату Иванову И.И. за первую половину сентября" -> `null`

        2.  **Если в запросе НЕ ХВАТАЕТ** критически важной информации для начала работы, сформулируй ОДИН короткий и вежливый уточняющий вопрос.
            - "Подготовьте справку" -> "Конечно. Уточните, пожалуйста, какую именно справку и за какой период нужно подготовить?"
            - "Нужны документы для банка" -> "Разумеется. Подскажите, пожалуйста, для какого банка и какой комплект документов требуется?"
            - "Оплатите счет" -> "Хорошо. Пришлите, пожалуйста, сам счет на оплату."
            - "Подготовьте справку для банка" -> "Подскажите, для какого банка и какую именно справку подготовить (например, об отсутствии задолженности, о состоянии расчётов) и за какой период?"

        3.  ЧЕК-ЛИСТ ПОЛНОТЫ (если упомянуто, но не указано явно — спроси):
            - Для **справки**: вид справки (какая именно), период/дата, на кого/о чём.
            - Для **банка**: название банка и формат/требования банка, если известны.
            - Для **отчёта/выгрузки**: период, форма, адресат.

        4.  Твой ответ должен быть либо `null` (текстом), либо текстом вопроса. Не добавляй ничего лишнего.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"ИСТОРИЯ ДИАЛОГА:\n{history_str}\n---\nПОСЛЕДНИЙ ЗАПРОС КЛИЕНТА: \"{question}\""}
        ]
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.4,
                max_tokens=200
            )
            content = (completion.choices[0].message.content or "").strip()
            if content.lower() == 'null' or not content:
                return None
            
            # Форматируем уточняющий вопрос с именем и приветствием
            if client_name:
                return await self.format_response_with_name(content, client_name, is_first_today)
            return content
        except Exception as e:
            logger.error(f"Ошибка при проверке полноты запроса: {e}")
            return None

    async def summarize_for_task(self, question: str, history: List[Dict[str, str]]) -> str:
        """
        Генерирует краткое саммари диалога для описания задачи в Битрикс.
        """
        history_str = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        system_prompt = """
        Ты — ИИ-ассистент, который готовит задачи для бухгалтера.
        Тебе даны история диалога и последний запрос клиента.
        Твоя задача — написать очень короткое (1-2 предложения) саммари ПРЕДЫСТОРИИ вопроса, чтобы специалист быстро понял контекст.

        ПРАВИЛА:
        - Не повторяй сам вопрос. Сконцентрируйся на том, что к нему привело.
        - Если предыстории нет или она нерелевантна, верни пустую строку.
        - Будь кратким и по делу.

        Пример:
        - История: ...обсуждение нового договора...
        - Вопрос: "Когда будут готовы закрывающие документы?"
        - Результат: "Ранее в чате обсуждался новый договор с контрагентом."

        Пример 2:
        - История: ...
        - Вопрос: "Пришлите акт сверки с ООО 'Ромашка'."
        - Результат: "" (предыстория не нужна)

        Верни ТОЛЬКО текст саммари или пустую строку.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"ИСТОРИЯ ДИАЛОГА:\n{history_str}\n---\nПОСЛЕДНИЙ ЗАПРОС КЛИЕНТА: \"{question}\""}
        ]
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.0,
                max_tokens=250
            )
            return (completion.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error(f"Ошибка при создании саммари для задачи: {e}")
            return ""

    def _cleanup_latex(self, text: str) -> str:
        """Преобразует базовый синтаксис LaTeX в читаемый текст."""
        text = text.replace("\\[", "").replace("\\]", "")
        text = text.replace("\\(", "").replace("\\)", "")
        text = re.sub(r'\\text\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'(\1) / (\2)', text)
        text = text.replace("\\times", "*")
        text = text.replace("\\cdot", "*")
        text = text.replace("\\%", "%")
        return text.strip()

    def _format_perplexity_response(self, text: str, sources: List[object]) -> str:
        """Форматирует ответ от Perplexity в HTML с очисткой и ссылками.
        sources: может быть списком URL-строк или списком словарей {"url": str, "title": Optional[str]}.
        """
        if not text:
            return ""

        cleaned_text = self._cleanup_latex(text)

        # Normalize sources -> lists of (url, display)
        urls: List[str] = []
        displays: List[str] = []
        for s in sources or []:
            if isinstance(s, str):
                url = s
                title = None
            elif isinstance(s, dict):
                url = s.get("url") or s.get("link") or s.get("source") or ""
                title = s.get("title")
            else:
                url = ""
                title = None
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            urls.append(url)
            if isinstance(title, str) and title.strip():
                displays.append(title.strip())
            else:
                # fallback: truncated URL as display
                disp = url
                if len(disp) > 70:
                    disp = disp[:67] + "…"
                displays.append(disp)

        # Build sources list HTML
        source_list_html = ""
        if urls:
            lines = [f"{i+1}. <a href=\"{_html.escape(urls[i])}\">{_html.escape(displays[i])}</a>" for i in range(len(urls))]
            source_list_html = "\n\n<b>Источники:</b>\n" + "\n".join(lines)

        # Map [n] to links
        source_map = {i + 1: _html.escape(urls[i]) for i in range(len(urls))}

        def reformat_citations(match):
            numbers = re.findall(r'\[(\d+)\]', match.group(0))
            links = []
            for n_str in numbers:
                try:
                    n = int(n_str)
                except Exception:
                    continue
                if n in source_map:
                    links.append(f'<a href="{source_map[n]}">[{n}]</a>')
            return ", ".join(links) if links else match.group(0)

        processed_text = _html.escape(cleaned_text.strip())
        processed_text = re.sub(r'(\[\d+\])+', reformat_citations, processed_text)

        return f"{processed_text}{source_list_html}"

    async def get_expert_answer(self, question: str) -> tuple[str | None, bool]:
        """
        Получает и форматирует ответ от Perplexity.
        Возвращает (formatted_answer|None, success).
        """
        if not self.perplexity:
            return None, False
        
        text, sources_info = await self.perplexity.search_its_glavbukh(question)
        if not text:
            return None, False
        
        formatted_html = self._format_perplexity_response(text, sources_info or [])
        return formatted_html, True

    async def expand_user_query(self, query: str) -> str:
        """
        Использует LLM для преобразования короткого запроса пользователя в более полный
        поисковый запрос для семантического поиска по базе знаний.
        """
        if len(query) > 50 or query.strip().endswith('?'):
            logger.info(f"Query is long enough, skipping expansion: '{query[:80]}...'")
            return query

        try:
            system_prompt = (
                "Ты — ассистент, который улучшает поисковые запросы. "
                "Твоя задача — превратить короткую фразу пользователя в полноценный, развернутый вопрос, "
                "подходящий для семантического поиска по базе знаний бухгалтерии. "
                "Не отвечай на вопрос, а только переформулируй его. "
                "Сохраняй ключевые детали. Ответ должен быть в виде одного предложения."
            )
            user_prompt = f"Пользовательская фраза: \"{query}\"\n\nУлучшенный поисковый запрос:"
            
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=100,
            )
            expanded_query = (completion.choices[0].message.content or "").strip().replace('"', '')
            if expanded_query and expanded_query != query:
                logger.info(f"Query expanded: '{query}' -> '{expanded_query}'")
                return expanded_query
            else:
                logger.warning(f"Query expansion failed or returned same query for: '{query}'")
                return query
        except Exception as e:
            logger.error(f"Ошибка при расширении запроса: {e}")
            return query

    async def strip_kb_header_with_llm(self, raw_text: str) -> str:
        """
        Удаляет чужое имя/приветствие/кавычки в начале ответа из БЗ с помощью LLM.
        Возвращает очищенный текст. При ошибке использует regex-фолбэк.
        """
        try:
            system_prompt = (
                "Ты помогаешь очистить ответ из базы знаний. Удали ТОЛЬКО начальные приветствия и обращения по имени, "
                "а также начальные кавычки и лишние пробелы. Ничего не добавляй и не меняй в основной части текста. "
                "Верни чистый текст без лишних префиксов. Примеры приветствий: 'Здравствуйте', 'Добрый день', 'Добрый вечер', "
                "'Доброе утро', 'Привет' и любые варианты с именем и запятой."
            )
            user_prompt = f"Текст:\n{raw_text}\n---\nВерни текст без начального приветствия/имени:"
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=300,
            )
            cleaned = (completion.choices[0].message.content or "").strip()
            if cleaned and cleaned != raw_text.strip():
                logger.info("KB header sanitized by LLM")
            # sanity: if LLM returned empty, fallback to regex version
            return cleaned or self._strip_foreign_name_or_greeting(raw_text)
        except Exception as e:
            logger.error(f"Ошибка LLM-санации KB ответа: {e}")
            return self._strip_foreign_name_or_greeting(raw_text)

    async def get_kb_playbook(
        self,
        question: str,
        session: AsyncSession,
        chat_id: int,
        min_confidence: float = 0.85,
    ) -> Optional[dict]:
        """
        Ищет ответ в локальной БЗ. Если находит, использует LLM, чтобы
        извлечь из текста ответа план действий (playbook).
        Возвращает dict {reply: str, create_task: bool} или None.
        """
        # 1. Сначала ищем точное совпадение по заголовку
        exact_match_content = self.knowledge_base.find_exact_match_in_kb(question)
        if exact_match_content:
            context_parts = [exact_match_content]
            confidence = 1.0
            logger.info(f"KB exact match found for query: '{question[:80]}...' -> confidence={confidence}")
        else:
            # 2. Если точного нет, расширяем короткие запросы для улучшения семантического поиска
            expanded_question = await self.expand_user_query(question)
            
            # 3. Ищем релевантный фрагмент в БЗ по расширенному вопросу
            context_parts, confidence = await self.knowledge_base.search_with_confidence(expanded_question, top_k=1)
            logger.info(f"KB semantic search: confidence={confidence:.3f} (min={min_confidence:.2f}) for question='{expanded_question[:80]}...'")
            if confidence < min_confidence:
                return None
        
        context = "\n---\n".join(context_parts)

        # 2. Используем LLM для извлечения плана действий из текста
        system_prompt = f"""
        Ты — ИИ-анализатор. Тебе дан текст ответа из Базы Знаний в формате "Вопрос: ... Ответ: ...".
        Твоя задача — проанализировать текст и вернуть JSON с двумя полями:
        1. `reply`: Текст, который нужно отправить клиенту. Это ДОЛЖЕН БЫТЬ ВЕСЬ ТЕКСТ из поля "Ответ:", без изменений и сокращений.
        2. `create_task`: boolean (true/false), нужно ли создавать задачу в Битрикс.

        ПРАВИЛА для `create_task`:
        - `true`, если в тексте ответа упоминается действие, которое выполнит сотрудник (бухгалтер, менеджер и т.д.).
        - `true`, если есть фразы: "ставлю задачу", "передам специалисту", "подготовит отчет", "направим вам", "проверяю", "пришлите" и т.п.
        - `false`, если ответ чисто информационный или заканчивается вопросом к клиенту ("Нужна помощь?", "Прислать копию сюда?").

        Пример 1:
        Текст: "Вопрос: ... Ответ: Алексей, принято, ставлю задачу на бухгалтера..."
        Результат: {{"reply": "Алексей, принято, ставлю задачу на бухгалтера...", "create_task": true}}

        Пример 2:
        Текст: "Вопрос: ... Ответ: Анна, ... Бухгалтер подготовит для вас отчет... Направим его вам завтра..."
        Результат: {{"reply": "Анна, ... Бухгалтер подготовит для вас отчет... Направим его вам завтра...", "create_task": true}}

        Пример 3:
        Текст: "Вопрос: ... Ответ: Дмитрий, ... Нужна помощь?"
        Результат: {{"reply": "Дмитрий, ... Нужна помощь?", "create_task": false}}
        
        Верни ТОЛЬКО валидный JSON.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Текст из Базы Знаний:\n{context}"}
        ]
        
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            playbook_str = completion.choices[0].message.content
            playbook = json.loads(playbook_str)
            logger.info(f"KB playbook returned: create_task={playbook.get('create_task')} for question='{question[:60]}...'")
            return playbook
        except Exception as e:
            logger.error(f"Ошибка при создании плейбука из БЗ: {e}")
            return None

    async def classify_question(
        self,
        question: str,
        history: List[Dict[str, str]],
        msk_now: Optional[str] = None,
        first_name: Optional[str] = None,
        username: Optional[str] = None,
    ) -> QuestionCategory:
        """
        Классифицирует вопрос с помощью LLM.
        """
        # Используем более широкий контекст (до 100 последних сообщений)
        history_slice = history[-100:] if len(history) > 100 else history
        history_str = "\n".join(f"{m['role']}: {m['content']}" for m in history_slice)

        # Метаданные окружения помогают модели учитывать время суток и имя, даже если это не критично для категории
        meta_time = msk_now or ""
        # Транслитерируем имя для использования в промпте
        meta_name = get_russian_name(first_name or "")
        meta_username = (username or "")

        system_prompt = f"""
        Ты — ИИ-классификатор для бота-бухгалтера. Твоя задача — отнести ПОСЛЕДНИЙ вопрос пользователя к одной из категорий.
        В ответе должно быть ТОЛЬКО название категории и ничего больше.

        МЕТАДАННЫЕ (для общего контекста, не цитируй их в ответе):
        - Время (МСК): {meta_time}
        - Имя Telegram: {meta_name}
        - Username: @{meta_username}

        ИСТОРИЯ ДИАЛОГА (для контекста):
        {history_str}
        ---

        КАТЕГОРИИ И ПРАВИЛА:

        1. `chitchat`:
           - Сообщение не является вопросом, а выражает вежливость.
           - Примеры: "Привет", "Спасибо", "Хорошо, понял", "Добрый день".

        2. `bitrix_task`:
           - ЛЮБОЙ вопрос, который требует ответа на основе данных конкретного клиента (выручка, расходы, налоги, документы, сотрудники).
           - ЛЮБОЙ вопрос про стоимость услуг, тарифы.
           - ЛЮБОЙ запрос на выполнение действия (выставить счет, подготовить отчет, прислать документ).
           - Примеры: "Какой у меня финансовый результат?", "Пришлите мою выручку", "Когда мне сдавать 6-НДФЛ?", "Сколько стоят ваши услуги?".

        3. `expert_question`:
           - Вопрос требует экспертного заключения, ссылок на законы, но НЕ требует данных о клиенте. Это общий теоретический вопрос.
           - Примеры: "Какие обязательные реквизиты должны быть в кассовом чеке?", "Кто платит транспортный налог при лизинге?".

        4. `non_standard_faq`:
           - Вопрос касается специфической, нестандартной ситуации, которая может быть описана во внутренней базе знаний.
           - Примеры: "В банке заблокировали счет по запросу ФНС, что делать?", "Касса сломалась, день не пробивали чеки, будет ли штраф?", "Мне звонил инспектор из налоговой".

        5. `general_question`:
           - Общий вопрос, который не подходит ни под одну из категорий выше.
           - Примеры: "Как зарегистрировать онлайн-кассу?", "Что такое УСН?".

        Проанализируй последний вопрос пользователя и верни ОДНУ категорию.
        """

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": f"Последний вопрос: \"{question}\""},
        ]

        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.0,
                max_tokens=1000, # Увеличим на всякий случай
            )
            raw_category = completion.choices[0].message.content.strip().lower()

            try:
                return QuestionCategory(raw_category)
            except ValueError:
                print(f"Warning: LLM returned an unknown category '{raw_category}'. Defaulting to bitrix_task.")
                return QuestionCategory.BITRIX_TASK

        except Exception as e:
            print(f"Ошибка при классификации вопроса: {e}")
            return QuestionCategory.BITRIX_TASK

    async def generate_response(
        self,
        question: str,
        user_id: int,
        session: AsyncSession,
        client_db_id: int,
        chat_id: int,
        msk_now: Optional[str] = None,
        first_name: Optional[str] = None,
        username: Optional[str] = None,
    ) -> tuple[str, bool, bool, float, bool]:
        """
        Генерирует ответ ТОЛЬКО на основе локальной knowledge_base (RAG).
        Возвращает (response_text, is_first_today, success, confidence, used_perplexity=False).
        `success` - флаг, удалось ли найти релевантный ответ.
        """
        # RAG контекст + confidence
        context_parts, confidence = await self.knowledge_base.search_with_confidence(question, top_k=1)
        context = "\n---\n".join(context_parts) if context_parts else ""

        # Если уверенность низкая, считаем, что ответа в БЗ нет
        if confidence < 0.82: # Порог для специфичных вопросов должен быть выше
            return "Не удалось найти точный ответ в базе знаний.", False, False, confidence, False

        is_first_today = await self.history.is_first_message_today(session, chat_id=chat_id)
        meta_time = msk_now or ""
        # Транслитерируем имя для использования в промпте
        meta_name = get_russian_name(first_name or "")
        meta_username = (username or "")

        system_prompt = (
            "Ты — ИИ-ассистент в чате бухгалтерской компании. Отвечай вежливо и профессионально. "
            "Твоя задача — дать ответ на вопрос клиента, используя ТОЛЬКО предоставленный контекст. "
            "Не придумывай ничего от себя. Обращайся к клиенту по имени.\n\n"
            f"МЕТАДАННЫЕ (для стиля, не включай их в ответ напрямую):\n"
            f"- Время (МСК): {meta_time}\n- Имя Telegram: {meta_name}\n- Username: @{meta_username}\n"
            f"- Первый ответ за сегодня: {str(is_first_today).lower()}\n\n"
            "ТРЕБОВАНИЯ К ОТВЕТУ: Если первый ответ за сегодня — начни с приветствия по МСК и обратись по имени на русском. Иначе — только имя, без приветствия. Для нетеоретических ответов не ставь точку в конце последнего предложения. Не повторяй дословно прошлые сообщения. Пиши без Markdown."
        )

        user_prompt = (
            f"Контекст из базы знаний:\n---\n{context}\n---\n"
            f"Имя клиента: {meta_name}\n"
            f"Вопрос клиента: \"{question}\"\n\nОтвет (без лишних префиксов):"
        )

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response_text = ""
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1",
                messages=messages,
                temperature=0.1,
                max_tokens=2000,
            )
            response_text = completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"Ошибка при генерации ответа по БЗ: {e}")
            return "Возникла проблема при генерации ответа.", False, False, 0.0, False

        return response_text, is_first_today, True, confidence, False

    async def generate_chitchat_response(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        msk_now: Optional[str] = None,
        first_name: Optional[str] = None,
        username: Optional[str] = None,
        is_first_today: bool = False,
    ) -> str:
        """
        Генерирует контекстуальный ответ для chitchat с помощью LLM.
        Использует последние 5-10 сообщений для лучшего понимания контекста.
        """
        meta_time = msk_now or ""
        # Транслитерируем имя для использования в промпте
        meta_name = get_russian_name(first_name or "")
        meta_username = (username or "")

        # Берем последние 10 сообщений для контекста (5-10 сообщений)
        recent_history = history[-10:] if len(history) > 10 else history
        history_str = "\n".join(f"{m['role']}: {m['content']}" for m in recent_history)

        system_prompt = f"""
        Ты — умный ИИ-ассистент в чате бухгалтерского бота. Твоя задача — дать естественный, контекстуальный ответ на короткое сообщение пользователя.
        Проанализируй контекст диалога и текущее сообщение пользователя.

        МЕТАДАННЫЕ (для более корректного тона, не цитируй их и не пересказывай):
        - Время (МСК): {meta_time}
        - Имя Telegram: {meta_name}
        - Username: @{meta_username}
        - Первый ответ за сегодня: {is_first_today}

        КОНТЕКСТ ДИАЛОГА:
        {history_str}

        ПРАВИЛА:
        1. Если сообщение пользователя — это приветствие (например, "Привет", "Добрый день", "Добрый вечер", "Доброй ночи", "Добрых снов", "Здравствуйте") и это первый ответ за сегодня (`is_first_today: true`), ответь развернуто: поприветствуй по времени суток (используя `meta_time`), обратись по имени на русском (`meta_name`) и спроси, чем можешь помочь. Учитывай разные варианты приветствий, в том числе пожелания ("Добрых снов", "Хорошего дня" и т.п.), и отвечай соответствующе — например, если пожелали доброй ночи, можешь пожелать спокойной ночи в ответ.
        2. Если сообщение пользователя — это простое подтверждение, благодарность или знак того, что информация принята (например, "ок", "хорошо", "понял", "спасибо", "буду ждать", "благодарю", "спасибо большое", "спс", "ага", "ясно"), ответь коротко и уместно, учитывая контекст предыдущих сообщений.
           - Если бот что-то сообщил или создал задачу: "Хорошо", "Принято"
           - Если бот помог или ответил на вопрос: "Пожалуйста", "Рад помочь"
        3. Если сообщение пользователя содержит пожелание (например, "хорошего дня", "удачи", "добрых снов", "спокойной ночи", "приятного вечера"), ответь вежливо и по ситуации, можешь ответить взаимным пожеланием или поблагодарить.
        4. Если пользователь отвечает на вопрос бота или развивает тему из контекста, дай соответствующий ответ, учитывая предыдущие сообщения.
        5. Во всех остальных случаях (например, если это новый вопрос или не первое приветствие за день), ответь нейтрально: "Слушаю вас".
        6. Твой ответ должен быть коротким (кроме правила 1).
        7. Не ставь точку в конце последнего предложения.

        Верни ТОЛЬКО текст ответа.
        """

        prompt = f"""
        Сообщение пользователя: "{user_message}"

        Твой контекстуальный ответ:
        """

        messages = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": prompt.strip()}
        ]

        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.3,
                max_tokens=100,
            )
            response = completion.choices[0].message.content.strip().replace('"', '')
            # Подстраховка, если модель вернет что-то длинное
            return response
        except Exception as e:
            logger.error(f"Ошибка при генерации chitchat ответа: {e}")
            # Фолбэк на простую логику при ошибке LLM
            if "спасибо" in user_message.lower():
                return "Пожалуйста! Обращайтесь."
            return "Слушаю вас."

    def _strip_foreign_name_or_greeting(self, text: str) -> str:
        """Удаляет чужое имя/приветствие в начале ответа, оставляя фактический текст."""
        if not text:
            return text
        original = text
        s = text.strip()
        # Удаляем ведущие кавычки и пробелы
        s = re.sub(r'^[\s\"\'""«»„”“]+', '', s)
        # Удаляем приветствия вида: "Здравствуйте, Имя, ..." или без имени
        greet_pattern = r'^(?:Здравствуйте|Добрый\s+день|Добрый\s+вечер|Доброе\s+утро|Привет)[,!]?\s*(?:[A-Za-zА-Яа-яЁё\-]{2,30})?[,!]?\s+'
        s = re.sub(greet_pattern, '', s, flags=re.IGNORECASE)
        # Удаляем первое слово-имя вида "Имя, " даже если оно было в кавычках
        name_pattern = r'^(?:[A-Za-zА-Яа-яЁё\-]{2,30})[,\:]\s+'
        s = re.sub(name_pattern, '', s)
        # Повторно убираем оставшиеся начальные кавычки/пробелы
        s = re.sub(r'^[\s\"\'""«»„”“]+', '', s)
        cleaned = s.strip()
        if cleaned != original.strip():
            logger.info("Sanitized KB reply header (removed foreign greeting/name)")
        return cleaned

    async def format_response_with_name(self, response: str, client_name: str, is_first_today: bool = False) -> str:
        core = self._strip_foreign_name_or_greeting(response)
        # Транслитерируем имя для использования в ответах
        russian_name = get_russian_name(client_name) if client_name else ""

        def _greeting_by_msk_time() -> str:
            try:
                current = now_msk()
                hour = current.hour
                if 5 <= hour < 11:
                    return "Доброе утро"
                if 11 <= hour < 17:
                    return "Добрый день"
                if 17 <= hour < 23:
                    return "Добрый вечер"
                return "Доброй ночи"
            except Exception:
                return "Здравствуйте"

        if is_first_today and russian_name:
            greeting = _greeting_by_msk_time()
            return f"{greeting}, {russian_name}! {core}"
        if russian_name:
            # Короткое обращение без приветствия
            return f"{russian_name}, {core}"
        return core
