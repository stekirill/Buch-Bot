import os
import pickle
from pathlib import Path
from typing import List, Optional, Dict, Any

import numpy as np
import tiktoken
from openai import AsyncOpenAI
from sklearn.metrics.pairwise import cosine_similarity


def num_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    """
    Возвращает количество токенов в строке.
    """
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens


class KnowledgeBaseService:
    def __init__(self, settings):
        self.settings = settings
        self.client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        self.knowledge_base_path = Path(__file__).resolve().parent.parent / "knowledge_base"
        # bump cache to v2 due to chunking strategy change (whole-file chunks)
        self.embeddings_cache_path = self.knowledge_base_path / "embeddings_cache_v2.pkl"

        self.chunks: List[str] = []
        self.embeddings: Optional[np.ndarray] = None
        # Инициализацию переносим в отдельный async-метод initialize()

    async def initialize(self):
        """
        Асинхронная инициализация хранилища.
        Если есть кэш — загружаем его, иначе создаём и кэшируем эмбеддинги.
        """
        if self.embeddings_cache_path.exists():
            print("Загрузка эмбеддингов из кэша...")
            with open(self.embeddings_cache_path, "rb") as f:
                cached_data = pickle.load(f)
                self.chunks = cached_data["chunks"]
                self.embeddings = cached_data["embeddings"]
            print("Эмбеддинги успешно загружены.")
            return

        print("Создание эмбеддингов для базы знаний...")
        if not self._load_and_chunk_documents():
            return

        # Получаем эмбеддинги от OpenAI и сохраняем
        await self._create_and_cache_embeddings()
        print("База знаний успешно загружена и векторизована.")

    def _load_and_chunk_documents(self) -> bool:
        """
        Загружает документы из папки knowledge_base как цельные чанки (без разбиения по параграфам).
        Это гарантирует, что ответ после "Ответ:" берётся целиком, включая переносы строк.
        """
        txt_files = list(self.knowledge_base_path.glob("**/*.txt"))
        if not txt_files:
            print("Папка knowledge_base пуста. Поиск по базе знаний не будет работать.")
            return False

        chunks: List[str] = []
        for file_path in txt_files:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
                # Normalize line endings and strip trailing spaces
                text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
                if text:
                    chunks.append(text)
        self.chunks = chunks
        return True

    async def _create_and_cache_embeddings(self):
        """
        Создает эмбеддинги для чанков и кэширует их.
        """
        response = await self.client.embeddings.create(
            input=self.chunks,
            model="text-embedding-3-small"
        )
        self.embeddings = np.array([item.embedding for item in response.data])
        
        with open(self.embeddings_cache_path, "wb") as f:
            pickle.dump({"chunks": self.chunks, "embeddings": self.embeddings}, f)

    async def search_knowledge(self, query: str, top_k: int = 3) -> List[str]:
        """
        Выполняет семантический поиск.
        """
        if self.embeddings is None or not self.chunks:
            return []

        # 1. Создаем эмбеддинг для запроса
        response = await self.client.embeddings.create(
            input=[query],
            model="text-embedding-3-small"
        )
        query_embedding = np.array([response.data[0].embedding])

        # 2. Считаем косинусное сходство
        similarities = cosine_similarity(query_embedding, self.embeddings).flatten()

        # 3. Находим индексы top_k самых похожих чанков
        top_k_indices = similarities.argsort()[-top_k:][::-1]

        return [self.chunks[i] for i in top_k_indices]

    async def search_with_confidence(self, query: str, top_k: int = 3) -> tuple[List[str], float]:
        """
        Возвращает (список топ фрагментов, confidence от 0.0 до 1.0) на основе косинусного сходства.
        """
        if self.embeddings is None or not self.chunks:
            return [], 0.0

        resp = await self.client.embeddings.create(input=[query], model="text-embedding-3-small")
        query_embedding = np.array([resp.data[0].embedding])
        similarities = cosine_similarity(query_embedding, self.embeddings).flatten()

        top_k_indices = similarities.argsort()[-top_k:][::-1]
        top_chunks = [self.chunks[i] for i in top_k_indices]

        max_sim = float(similarities[top_k_indices[0]]) if len(top_k_indices) > 0 else -1.0
        confidence = max(0.0, min(1.0, (max_sim + 1.0) / 2.0))
        return top_chunks, confidence

    async def get_standard_answers(self) -> Dict[str, str]:
        """Получение стандартных ответов на типовые вопросы"""
        pass

    def find_exact_match_in_kb(self, query: str) -> Optional[str]:
        """
        Ищет точное совпадение вопроса в базе знаний (по полю "Вопрос:").
        Возвращает полное содержимое файла, если найдено.
        """
        normalized_query = query.strip().lower()
        if not normalized_query:
            return None

        for chunk in self.chunks:
            lines = chunk.split('\n')
            if not lines:
                continue
            
            # Ищем строку, начинающуюся с "Вопрос:"
            question_line = ""
            for line in lines:
                if line.lower().strip().startswith("вопрос:"):
                    question_line = line.strip()
                    break
            
            if question_line:
                # Извлекаем текст вопроса после "Вопрос:"
                kb_question = question_line[len("Вопрос:"):].strip().lower()
                # Удаляем знаки препинания в конце для более мягкого сравнения
                kb_question = kb_question.rstrip('.?!')
                normalized_query = normalized_query.rstrip('.?!')

                if kb_question == normalized_query:
                    # logger.info(f"KB exact match found for query: '{query}'") # This line was not in the original file, so it's not added.
                    return chunk

        return None
