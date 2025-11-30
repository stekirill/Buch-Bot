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
    MIXED_QUESTION_AND_TASK = "mixed_question_and_task" # Содержит и вопрос, и просьбу о помощи


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
            - Камеральные и выездные проверки ФНС, помощь в их прохождении, подготовка документов и пояснений для проверок — это СТАНДАРТНАЯ работа бухгалтера.
            - Стандартные запросы, связанные с текущим обслуживанием: выставление счёта за услуги, отправка акта сверки, запрос на выгрузку документов.
            - Аналитические вопросы по финансам клиента: "какая выручка?", "сравните расходы с прошлым годом", "структура доходов", "какой доход за последние годы". Это справочная информация, которую может дать бухгалтер на основе данных клиента.
            - **Запросы на обратную связь по работе, жалобы, предложения или вопросы по текущим задачам.** Это зона ответственности исполнителя (бухгалтера), а не менеджера.

        2.  **Платная услуга (is_off_tariff: true):**
            - Явный запрос на создание НОВОГО документа, который не является стандартным (например, "составить договор купли-продажи", "подготовить документы для тендера").
            - Запросы на регистрационные действия ("зарегистрировать онлайн-кассу", "снять кассу с учета").
            - Запросы на сложное финансовое моделирование, бизнес-планы или глубокий аудит, требующий отдельного проекта.
            - Просьбы о помощи в нестандартных ситуациях, требующих активного ВНЕШНЕГО вмешательства (выезд в банк, представление интересов в суде).
            - **ВАЖНО:** Менеджер должен подключаться ТОЛЬКО если клиент говорит о чем-то, связанном с НОВЫМИ ДОП. УСЛУГАМИ.

        ПРИМЕРЫ:
        - "В банке заблокировался счёт. Говорят, по запросу ФНС" -> {"is_off_tariff": false}
        - "Мне пришло требование из ИФНС, что делать?" -> {"is_off_tariff": false}
        - "Мне нужен счёт на оплату услуг бухгалтерии" -> {"is_off_tariff": false}
        - "Насколько у меня большие расходы в сравнении с прошлым годом?" -> {"is_off_tariff": false}
        - "Какой у меня доход, если сравнить его с последними тремя годами?" -> {"is_off_tariff": false}
        - "Дайте обратную связь по работе" -> {"is_off_tariff": false}
        - "Почему так долго делаете отчет?" -> {"is_off_tariff": false}
        - "Составьте договор купли-продажи" -> {"is_off_tariff": true}
        - "Подготовьте документы для участия в тендере" -> {"is_off_tariff": true}

        ВАЖНО: Не считай off-tariff простые вопросы, аналитику по данным клиента или продолжение уже начатого обсуждения. Только если это явный запрос на НОВУЮ ДОПОЛНИТЕЛЬНУЮ РАБОТУ.
        **При сомнениях всегда выбирай false (лучше создать задачу бухгалтеру, чем зря дергать менеджера).**

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
        Проверяет, является ли запрос клиента самодостаточным для создания задачи.
        Если нет — возвращает уточняющий вопрос.
        Если да (или если это не задача) — возвращает None.
        """
        # Имя для обращения в промпте
        full_name = client_name or "Клиент"
        
        history_str = "\n".join(f"- {m['role']}: {m['content']}" for m in history)
        
        system_prompt = f"""
        Ты — AI-ассистент-бухгалтер. Твоя задача — проанализировать ПОСЛЕДНЕЕ сообщение клиента и решить, является ли оно самодостаточным или требует уточнения ДЛЯ ПОСТАНОВКИ ЗАДАЧИ БУХГАЛТЕРУ.

        ВНИМАТЕЛЬНО СЛЕДУЙ ПРАВИЛАМ:

        ШАГ 1: Проверь, не является ли вопрос чисто информационным, основываясь на последнем сообщении и истории.
        - Если это общий вопрос о компании, ее услугах или режиме работы (например, "какие у вас часы работы?", "вы регистрируете ооо?", "когда сдавать декларацию?"), он НЕ требует уточнения. В этом случае ВСЕГДА отвечай "ПОЛНЫЙ".
        - Если это простое приветствие, благодарность или болтовня ("привет", "спасибо") — также отвечай "ПОЛНЫЙ".

        ШАГ 2: Если вопрос НЕ информационный, а подразумевает ЗАДАЧУ (например, "пришлите отчет", "посчитайте налоги"), проверь, хватает ли в нем деталей.
        - Если в запросе на задачу достаточно информации (например, "Пришлите акт сверки с ООО 'Ромашка' за 3 квартал 2025 года"), ответь: "ПОЛНЫЙ".
        - Если в запросе на задачу НЕ хватает ключевых деталей (период, контрагент, сумма), задай ОДИН короткий, вежливый уточняющий вопрос.

        ВАЖНО:
        - Не отвечай на вопрос по существу! Твоя задача — только валидация.
        - НЕ ДОБАВЛЯЙ имя клиента или приветствие в свой ответ. Только текст вопроса. Форматирование будет добавлено позже.

        Примеры:
        - Клиент: "Какие у тебя чсасы работы?" -> Ответ: "ПОЛНЫЙ" (Это информационный вопрос из ШАГА 1)
        - Клиент: "Спасибо!" -> Ответ: "ПОЛНЫЙ" (Это болтовня из ШАГА 1)
        - Клиент: "Пришлите акт сверки" -> Ответ: "За какой период и с каким контрагентом нужно сформировать акт?" (Это задача без деталей из ШАГА 2)
        - Клиент: "Сравните мою выручку с прошлым годом" -> Ответ: "ПОЛНЫЙ" (Это задача с достаточным количеством деталей из ШАГА 2)
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вот история диалога:\n{history_str}\n\n---\nПроанализируй только последнее сообщение клиента:\n\"{question}\""}
        ]
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=messages,
                temperature=0.1,
                max_tokens=200
            )
            content = (completion.choices[0].message.content or "").strip()
            if content.upper() == 'ПОЛНЫЙ':
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

    async def get_expert_answer(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> tuple[str | None, bool]:
        """
        Получает и форматирует ответ от Perplexity.
        Возвращает (formatted_answer|None, success).
        
        Args:
            question: Текущий вопрос пользователя
            history: История чата (последние сообщения). Если передана, используются последние 4 сообщения для контекста.
        """
        if not self.perplexity:
            return None, False
        
        text, sources_info = await self.perplexity.search_its_glavbukh(question, history)
        if not text:
            return None, False
        
        formatted_html = self._format_perplexity_response(text, sources_info or [])
        return formatted_html, True

    async def expand_user_query(self, query: str) -> str:
        """
        Убирает приветствия из запроса, минимально изменяя его.
        Например: "Здравствуйте, работаете сейчас?" -> "работаете сейчас?"
        """
        if len(query) > 80:
            logger.info(f"Query is long enough, skipping cleanup: '{query[:80]}...'")
            return query

        try:
            system_prompt = (
                "Ты — помощник для очистки поисковых запросов. "
                "Твоя задача — убрать ТОЛЬКО приветствия и вежливые обращения из текста, сохранив суть вопроса БЕЗ ИЗМЕНЕНИЙ. "
                "НЕ переформулируй, НЕ расширяй, НЕ улучшай вопрос. Просто убери лишнее.\n\n"
                "Примеры:\n"
                "- 'Здравствуйте, работаете сейчас?' -> 'работаете сейчас?'\n"
                "- 'Добрый день, какие у вас часы работы?' -> 'какие у вас часы работы?'\n"
                "- 'Привет! Нужна помощь с декларацией' -> 'нужна помощь с декларацией'\n"
                "- 'Пришлите акт сверки' -> 'Пришлите акт сверки' (без изменений)\n\n"
                "Если приветствия нет, верни текст БЕЗ ИЗМЕНЕНИЙ."
            )
            user_prompt = f"Текст: \"{query}\"\n\nОчищенный текст:"
            
            completion = await self.client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=100,
            )
            cleaned_query = (completion.choices[0].message.content or "").strip().replace('"', '')
            if cleaned_query and cleaned_query != query:
                logger.info(f"Query cleaned: '{query}' -> '{cleaned_query}'")
                return cleaned_query
            else:
                return query
        except Exception as e:
            logger.error(f"Ошибка при очистке запроса: {e}")
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

    async def validate_and_improve_kb_answer(
        self,
        question: str,
        kb_answer: str,
        msk_now: Optional[str] = None,
    ) -> tuple[Optional[str], bool]:
        """
        Проверяет логичность и релевантность ответа из БЗ, улучшает его (пересчитывает даты, проверяет расчёты).
        Возвращает (улучшенный ответ или None, is_relevant: bool).
        Если ответ не релевантен или нелогичен, возвращает (None, False).
        """
        if not msk_now:
            msk_now = now_msk().strftime('%Y-%m-%d %H:%M:%S')
        
        system_prompt = f"""
        Ты — ИИ-валидатор ответов из базы знаний. Твоя задача — проверить ответ на релевантность и логичность, 
        а затем улучшить его, пересчитав даты и проверив расчёты.
        
        ВАЖНО: Ответы будут использоваться от имени ассистента Алины (женщина). Используй женский род во всех глаголах (подготовила, получила, прикрепила и т.д.).

        ТЕКУЩАЯ ДАТА И ВРЕМЯ (МСК): {msk_now}

        ЭТАП 1: ПРОВЕРКА РЕЛЕВАНТНОСТИ
        Проверь, действительно ли ответ из БЗ релевантен вопросу пользователя. Ответ должен:
        - Отвечать на суть вопроса
        - Быть применим к ситуации пользователя
        - Не содержать устаревшей или неактуальной информации

        ЭТАП 2: ПРОВЕРКА ЛОГИЧНОСТИ
        Проверь логичность ответа:
        - Все даты должны быть актуальными (пересчитай относительно текущей даты)
        - Все расчёты должны быть правильными
        - Сроки и периоды должны быть логичными
        - Нет противоречий в тексте

        ЭТАП 3: УЛУЧШЕНИЕ ОТВЕТА
        Если ответ релевантен и логичен, улучши его:
        - Пересчитай все даты относительно текущей даты ({msk_now})
        - Проверь и исправь все расчёты (проценты, суммы, сроки)
        - Адаптируй ответ под текущий контекст (например, если в БЗ написано "завтра", укажи конкретную дату)
        - Сохрани структуру и стиль оригинального ответа
        - НЕ меняй суть ответа, только даты, расчёты и актуализируй информацию

        ВАЖНО:
        - Если ответ НЕ релевантен вопросу, верни {{"is_relevant": false, "improved_answer": null}}
        - Если ответ релевантен, но содержит нелогичные данные, которые нельзя исправить, верни {{"is_relevant": false, "improved_answer": null}}
        - Если ответ релевантен и логичен (или был улучшен), верни {{"is_relevant": true, "improved_answer": "улучшенный текст ответа"}}

        ПРИМЕРЫ:
        1. Вопрос: "Когда сдавать декларацию по УСН за 2024 год?"
           Ответ из БЗ: "Декларацию по УСН за 2024 год нужно сдать до 30 апреля 2025 года"
           Текущая дата: 2025-03-15
           Результат: {{"is_relevant": true, "improved_answer": "Декларацию по УСН за 2024 год нужно сдать до 30 апреля 2025 года (осталось 46 дней)"}}

        2. Вопрос: "Как рассчитать НДФЛ с зарплаты 100000 рублей?"
           Ответ из БЗ: "НДФЛ составляет 13% от суммы, то есть 13000 рублей"
           Результат: {{"is_relevant": true, "improved_answer": "НДФЛ составляет 13% от суммы, то есть 13000 рублей (100000 × 0.13 = 13000)"}}

        3. Вопрос: "Когда сдавать отчёт?"
           Ответ из БЗ: "Нужно сдать отчёт до 25 числа следующего месяца"
           Текущая дата: 2025-03-15
           Результат: {{"is_relevant": true, "improved_answer": "Нужно сдать отчёт до 25 апреля 2025 года (осталось 41 день)"}}

        Верни ТОЛЬКО валидный JSON.
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n{question}\n\n---\n\nОТВЕТ ИЗ БАЗЫ ЗНАНИЙ:\n{kb_answer}\n\n---\n\nПроверь релевантность и логичность, улучши ответ если нужно:"}
        ]
        
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1",
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            result_str = completion.choices[0].message.content
            result = json.loads(result_str or "{}")
            
            is_relevant = result.get("is_relevant", False)
            improved_answer = result.get("improved_answer")
            
            if not is_relevant:
                logger.info(f"KB answer rejected: not relevant or illogical for question='{question[:60]}...'")
                return None, False
            
            if improved_answer:
                logger.info(f"KB answer improved for question='{question[:60]}...'")
                return improved_answer, True
            else:
                # Если релевантен, но улучшений не требуется, возвращаем оригинал
                return kb_answer, True
                
        except Exception as e:
            logger.error(f"Ошибка при валидации ответа из БЗ: {e}")
            # При ошибке валидации возвращаем оригинальный ответ, но с предупреждением
            return kb_answer, True

    async def generate_relevant_answer_from_kb_context(
        self,
        question: str,
        kb_context: str,
        msk_now: Optional[str] = None,
    ) -> str:
        """
        Генерирует релевантный ответ на основе контекста из БЗ, если оригинальный ответ не был релевантен.
        Использует информацию из БЗ для создания нового ответа, который точно отвечает на вопрос пользователя.
        """
        if not msk_now:
            msk_now = now_msk().strftime('%Y-%m-%d %H:%M:%S')
        
        system_prompt = f"""
        Ты — ИИ-ассистент Алина, женщина, в чате бухгалтерской компании. Твоя задача — сгенерировать релевантный ответ на вопрос клиента,
        используя информацию из базы знаний. Используй женский род во всех глаголах (подготовила, получила, прикрепила и т.д.).

        ТЕКУЩАЯ ДАТА И ВРЕМЯ (МСК): {msk_now}

        ПРАВИЛА:
        1. Используй ТОЛЬКО информацию из предоставленного контекста базы знаний
        2. Ответ должен точно отвечать на вопрос пользователя
        3. Пересчитай все даты относительно текущей даты ({msk_now})
        4. Проверь и покажи все расчёты (проценты, суммы, сроки)
        5. Если в контексте есть примеры, адаптируй их под вопрос пользователя
        6. Будь точным и конкретным
        7. Не придумывай информацию, которой нет в контексте
        8. Если информации недостаточно, укажи это, но используй то, что есть

        СТИЛЬ:
        - Отвечай вежливо и профессионально
        - Будь конкретным и точным
        - Используй актуальные даты и расчёты
        - Не используй Markdown разметку

        Верни ТОЛЬКО текст ответа, без дополнительных пояснений.
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ:\n{kb_context}\n\n---\n\nВОПРОС КЛИЕНТА:\n{question}\n\n---\n\nСгенерируй релевантный ответ на основе контекста:"}
        ]
        
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1",
                messages=messages,
                temperature=0.1,
                max_tokens=2000,
            )
            generated_answer = completion.choices[0].message.content.strip()
            logger.info(f"Generated relevant answer from KB context for question='{question[:60]}...'")
            return generated_answer
        except Exception as e:
            logger.error(f"Ошибка при генерации релевантного ответа из контекста БЗ: {e}")
            return ""

    async def get_kb_playbook(
        self,
        question: str,
        session: AsyncSession,
        chat_id: int,
        min_confidence: float = 0.85,
    ) -> Optional[dict]:
        """
        Ищет ответ в локальной БЗ. Если находит, использует LLM, чтобы
        извлечь из текста ответа план действий (playbook), затем валидирует и улучшает ответ.
        Возвращает dict {reply: str, create_task: bool} или None.
        """
        # 1. Сначала очищаем запрос от приветствий для лучшего поиска
        cleaned_question = await self.expand_user_query(question)
        
        # 2. Ищем точное совпадение по очищенному заголовку
        exact_match_content = self.knowledge_base.find_exact_match_in_kb(cleaned_question)
        if exact_match_content:
            context_parts = [exact_match_content]
            confidence = 1.0
            logger.info(f"KB exact match found for cleaned query: '{cleaned_question[:80]}...' -> confidence={confidence}")
        else:
            # 3. Если точного нет, используем семантический поиск
            context_parts, confidence = await self.knowledge_base.search_with_confidence(cleaned_question, top_k=1)
            logger.info(f"KB semantic search: confidence={confidence:.3f} (min={min_confidence:.2f}) for question='{cleaned_question[:80]}...'")
            if confidence < min_confidence:
                return None
        
        context = "\n---\n".join(context_parts)

        # 4. Используем LLM для извлечения плана действий из текста
        system_prompt = f"""
        Ты — ИИ-анализатор. Тебе дан текст ответа из Базы Знаний в формате "Вопрос: ... Ответ: ...".
        Твоя задача — проанализировать текст и вернуть JSON с двумя полями:
        1. `reply`: Текст, который нужно отправить клиенту. Это ДОЛЖЕН БЫТЬ ВЕСЬ ТЕКСТ из поля "Ответ:", без изменений и сокращений.
        2. `create_task`: boolean (true/false), нужно ли создавать задачу в Битрикс.

        ПРАВИЛА для `create_task`:
        - `true`, если в тексте ответа подразумевается действие, которое выполнит сотрудник (бухгалтер, менеджер и т.д.).
        - `false`, если ответ чисто информационный и не подразумевает действия или обещания действия.

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
                model="gpt-4.1",
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            playbook_str = completion.choices[0].message.content
            playbook = json.loads(playbook_str)
            
            # 5. Валидируем и улучшаем ответ
            raw_reply = playbook.get("reply", "")
            if raw_reply:
                msk_now = now_msk().strftime('%Y-%m-%d %H:%M:%S')
                improved_reply, is_relevant = await self.validate_and_improve_kb_answer(
                    question=question,
                    kb_answer=raw_reply,
                    msk_now=msk_now
                )
                
                if not is_relevant or not improved_reply:
                    # Если оригинальный ответ не релевантен, генерируем новый релевантный ответ на основе контекста из БЗ
                    logger.info(f"KB answer rejected after validation, generating relevant answer from context for question='{question[:60]}...'")
                    generated_reply = await self.generate_relevant_answer_from_kb_context(
                        question=question,
                        kb_context=context,
                        msk_now=msk_now
                    )
                    
                    if generated_reply:
                        playbook["reply"] = generated_reply
                        # Если генерируем новый ответ, обычно это информационный ответ, но проверяем по контексту
                        # Оставляем create_task из оригинального playbook, так как он может быть валидным
                        logger.info(f"Generated relevant answer from KB context for question='{question[:60]}...'")
                    else:
                        logger.warning(f"Failed to generate relevant answer from KB context for question='{question[:60]}...'")
                        return None
                else:
                    playbook["reply"] = improved_reply
                    logger.info(f"KB playbook returned: create_task={playbook.get('create_task')} for question='{question[:60]}...'")
            else:
                logger.warning(f"KB playbook has no reply field for question='{question[:60]}...'")
                return None
            
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

        2. `mixed_question_and_task`:
           - Сообщение содержит ОДНОВРЕМЕННО:
             1. Теоретический или справочный вопрос (как в expert_question или non_standard_faq).
             2. Явный запрос на помощь, участие человека или создание задачи (как в bitrix_task).
           - Примеры: "Что делать если назначили камералку? Вы можете подключиться?", "Как заполнить декларацию? Сделайте это за меня, пожалуйста".
           - ВАЖНО: Если есть и вопрос по существу, и просьба помочь — выбирай ЭТУ категорию.

        3. `bitrix_task`:
           - ЛЮБОЙ вопрос, который требует ответа на основе данных конкретного клиента (выручка, расходы, налоги, документы, сотрудники).
           - ЛЮБОЙ вопрос про стоимость услуг, тарифы.
           - ЛЮБОЙ запрос на выполнение действия (выставить счет, подготовить отчет, прислать документ).
           - НЕ относись к этой категории общие теоретические вопросы без указания конкретных реквизитов клиента
             (например, "Что делать, если я допустил ошибку в платежке в налоговую?" — это не запрос по конкретным данным клиента).
           - Примеры: "Какой у меня финансовый результат?", "Пришлите мою выручку", "Когда мне сдавать 6-НДФЛ?", "Сколько стоят ваши услуги?".
           - Если сообщение содержит теоретический вопрос + просьбу помочь, используй `mixed_question_and_task`.
          
        4. `expert_question`:
           - Вопрос требует экспертного заключения, ссылок на законы, но НЕ требует данных о клиенте. Это общий теоретический вопрос.
           - Примеры: "Какие обязательные реквизиты должны быть в кассовом чеке?", "Кто платит транспортный налог при лизинге?",
             "Что делать, если я допустил ошибку в платежке в налоговую?".

        5. `non_standard_faq`:
           - Вопрос касается специфической, нестандартной ситуации, которая может быть описана во внутренней базе знаний.
           - Примеры: "В банке заблокировали счет по запросу ФНС, что делать?", "Касса сломалась, день не пробивали чеки, будет ли штраф?", "Мне звонил инспектор из налоговой".

        6. `general_question`:
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
            "Ты — ИИ-ассистент Алина, женщина, в чате бухгалтерской компании. Отвечай вежливо и профессионально. "
            "Используй женский род во всех глаголах (подготовила, получила, прикрепила и т.д.). "
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
            
            # Валидируем сгенерированный ответ на релевантность и логичность
            if response_text:
                msk_now_str = msk_now or now_msk().strftime('%Y-%m-%d %H:%M:%S')
                validated_response, is_relevant = await self.validate_and_improve_kb_answer(
                    question=question,
                    kb_answer=response_text,
                    msk_now=msk_now_str
                )
                
                if not is_relevant or not validated_response:
                    # Если сгенерированный ответ не релевантен, генерируем новый релевантный ответ на основе контекста из БЗ
                    logger.info(f"Generated response rejected after validation, generating relevant answer from context for question='{question[:60]}...'")
                    generated_reply = await self.generate_relevant_answer_from_kb_context(
                        question=question,
                        kb_context=context,
                        msk_now=msk_now_str
                    )
                    
                    if generated_reply:
                        response_text = generated_reply
                        logger.info(f"Generated relevant answer from KB context for question='{question[:60]}...'")
                    else:
                        logger.warning(f"Failed to generate relevant answer from KB context for question='{question[:60]}...'")
                        return "Не удалось найти точный ответ в базе знаний.", False, False, confidence, False
                else:
                    response_text = validated_response
        except Exception as e:
            print(f"Ошибка при генерации ответа по БЗ: {e}")
            return "Возникла проблема при генерации ответа.", False, False, 0.0, False

        return response_text, is_first_today, True, confidence, False

    async def generate_general_answer(
        self,
        question: str,
        msk_now: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> str:
        """
        Генерирует ответ на основе общих знаний GPT (без контекста БЗ).
        Используется для вопросов, которые не найдены в БЗ.
        """
        if not msk_now:
            from telegram_bot.utils.schedule import now_msk
            msk_now = now_msk().strftime('%Y-%m-%d %H:%M:%S')
        
        russian_name = get_russian_name(first_name or "")
        
        system_prompt = f"""
        Ты — ИИ-ассистент Алина, женщина, в чате бухгалтерской компании. Отвечай вежливо и профессионально.
        Используй женский род во всех глаголах (подготовила, получила, прикрепила и т.д.).
        
        ТЕКУЩАЯ ДАТА И ВРЕМЯ (МСК): {msk_now}
        
        Твоя задача — дать краткий, но информативный ответ на вопрос клиента о бухгалтерии, налогах, проверках ФНС и т.д.
        
        ПРАВИЛА:
        1. Отвечай ЛАКОНИЧНО и по делу, без лишних деталей
        2. Используй актуальную информацию о российском налоговом законодательстве
        3. Если вопрос про конкретные действия - дай краткую инструкцию
        4. Если вопрос про сроки - укажи актуальные даты относительно {msk_now}
        5. Будь точным, но не перегружай деталями
        6. Не используй Markdown разметку
        
        СТИЛЬ:
        - Обращайся к клиенту по имени: {russian_name}
        - Будь вежливой и профессиональной
        - Пиши без воды, только суть
        
        Верни ТОЛЬКО текст ответа, без дополнительных пояснений.
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вопрос клиента: {question}\n\nДай краткий информативный ответ:"}
        ]
        
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4.1",
                messages=messages,
                temperature=0.1,
                max_tokens=1000,
            )
            answer = completion.choices[0].message.content.strip()
            logger.info(f"Generated general answer (without KB) for question='{question[:60]}...'")
            return answer
        except Exception as e:
            logger.error(f"Ошибка при генерации общего ответа: {e}")
            return ""

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
        Ты — умный ИИ-ассистент Алина, женщина, в чате бухгалтерского бота Аудит Картель. Твоя задача — дать естественный, контекстуальный ответ на короткое сообщение пользователя.
        Используй женский род во всех глаголах (подготовила, получила, прикрепила и т.д.).
        Проанализируй контекст диалога и текущее сообщение пользователя.

        МЕТАДАННЫЕ (для более корректного тона, не цитируй их и не пересказывай):
        - Время (МСК): {meta_time}
        - Имя Telegram: {meta_name}
        - Username: @{meta_username}
        - Первый ответ за сегодня: {is_first_today}

        КОНТЕКСТ ДИАЛОГА:
        {history_str}

        ПРАВИЛА:
        1. ВАЖНО: Если это первый ответ за сегодня (`is_first_today: true`), ВСЕГДА начни с приветствия по времени суток и обращения по имени, независимо от типа сообщения пользователя. Затем ответь на само сообщение.
        2. Если сообщение пользователя — это приветствие (например, "Привет", "Добрый день", "Добрый вечер", "Доброй ночи", "Добрых снов", "Здравствуйте"), ответь соответствующим приветствием и спроси, чем можешь помочь. Учитывай разные варианты приветствий, в том числе пожелания ("Добрых снов", "Хорошего дня" и т.п.), и отвечай соответствующе — например, если пожелали доброй ночи, можешь пожелать спокойной ночи в ответ.
        3. Если сообщение пользователя — это простое подтверждение, благодарность или знак того, что информация принята (например, "ок", "хорошо", "понял", "спасибо", "буду ждать", "благодарю", "спасибо большое", "спс", "ага", "ясно"), ответь коротко и уместно, учитывая контекст предыдущих сообщений.
           - Если бот что-то сообщил или создал задачу: "Хорошо", "Принято"
           - Если бот помог или ответил на вопрос: "Пожалуйста", "Рада помочь"
        4. Если сообщение пользователя содержит пожелание (например, "хорошего дня", "удачи", "добрых снов", "спокойной ночи", "приятного вечера"), ответь вежливо и по ситуации, можешь ответить взаимным пожеланием или поблагодарить.
        5. Если пользователь отвечает на вопрос бота или развивает тему из контекста, дай соответствующий ответ, учитывая предыдущие сообщения.
        6. Во всех остальных случаях (например, если это новый вопрос или не первое приветствие за день), ответь нейтрально: "Слушаю вас".
        7. Твой ответ должен быть коротким (кроме правила 1).
        8. Не ставь точку в конце последнего предложения.
        9. Если вопрос про время работы - вежливо скажи про пн-пт с 9 до 18

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
            return "Слушаю вас"

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
