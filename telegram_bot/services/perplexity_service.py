from typing import Dict, List, Optional, Tuple
from openai import AsyncOpenAI
from telegram_bot.config.settings import BotSettings


class PerplexityService:
    def __init__(self, settings: BotSettings):
        self.enabled = bool(settings.pplx_api_key)
        if self.enabled:
            self.client = AsyncOpenAI(base_url="https://api.perplexity.ai", api_key=settings.pplx_api_key)
        else:
            self.client = None

    async def search_its_glavbukh(self, query: str, history: Optional[List[Dict[str, str]]] = None) -> Tuple[str, List[Dict[str, str]]]:
        """
        Returns (answer_text, sources_info[{url, title}]) or ("", []) if disabled/error.
        
        Args:
            query: Текущий вопрос пользователя
            history: История чата (последние сообщения). Если передана, используются последние 4 сообщения для контекста.
        """
        if not self.enabled or self.client is None:
            return "", []
        try:
            # Формируем системный промпт с акцентом на ответ на неотвеченный вопрос
            system_content = """Отвечай на русском, вежливо, без канцелярита, ЛАКОНИЧНО и по делу. Избегай лишних деталей и воды. Пользователь должен получить удовольствие от ознакомления с информацией. Никакой Markdown-разметки типа ** или ##.

ВАЖНО: Если в контексте диалога есть неотвеченный вопрос пользователя, дай ответ именно на него, используя контекст предыдущих сообщений для лучшего понимания. Будь кратким, но информативным."""
            
            messages = [{"role": "system", "content": system_content}]
            
            # Собираем историю в один user-промпт, чтобы не нарушать формат чередования ролей
            if history:
                recent_history = history[-4:] if len(history) > 4 else history
                history_lines = []
                for msg in recent_history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if not content:
                        continue
                    history_lines.append(f"{role}: {content}")
                history_str = "\n".join(history_lines)
                user_content = (
                    f"История диалога (для контекста):\n{history_str}\n\n---\n"
                    f"Текущий вопрос пользователя:\n{query}"
                )
            else:
                user_content = query

            messages.append({"role": "user", "content": user_content})
            
            base_args = dict(
                model="sonar",
                messages=messages,
            )
            try:
                # Perplexity-specific params should go via extra_body in OpenAI-compatible SDK
                resp = await self.client.chat.completions.create(
                    **base_args,
                    extra_body={
                        "search_domain_filter": ["its.1c.ru", "glavbukh.ru"],
                        "return_citations": True,
                    },
                )
            except TypeError as e:
                # Older SDKs may not support extra_body; retry without filter
                print("Perplexity: extra_body unsupported, retrying without domain filter:", repr(e))
                resp = await self.client.chat.completions.create(**base_args)

            text = ""
            message = resp.choices[0].message if getattr(resp, "choices", None) else None
            if message is not None:
                text = message.content or ""

            # Extract structured source info from either citations or search_results
            sources_info: List[Dict[str, str]] = []
            citations = getattr(resp, "citations", [])
            search_results = getattr(resp, "search_results", [])
            
            # Map URLs to titles from search_results for better display
            url_to_title = {item['url']: item['title'] for item in search_results if isinstance(item, dict) and item.get('url') and item.get('title')}

            seen_urls = set()
            if citations:
                for cit in citations:
                    url = None
                    if isinstance(cit, dict):
                        url = cit.get("url")
                    elif isinstance(cit, str):
                        url = cit.strip()

                    if url and url not in seen_urls:
                        title = url_to_title.get(url) # Get title if available
                        sources_info.append({"url": url, "title": title})
                        seen_urls.add(url)
            
            # Fallback for older formats or if citations are missing
            if not sources_info and search_results:
                for res in search_results:
                    url = None
                    if isinstance(res, dict):
                        url = res.get("url")
                    
                    if url and url not in seen_urls:
                        title = res.get("title") if isinstance(res, dict) else None
                        sources_info.append({"url": url, "title": title})
                        seen_urls.add(url)
                        
            return text or "", sources_info
        except Exception as e:
            print("Perplexity error:", repr(e))
            return "", []


