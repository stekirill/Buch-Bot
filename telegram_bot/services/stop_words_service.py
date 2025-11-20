import asyncio
import base64
import json
from typing import List, Optional

import gspread
from google.oauth2.service_account import Credentials
from loguru import logger

from telegram_bot.config.settings import BotSettings


class StopWordsService:
    """
    Загружает список стоп-слов из Google Sheets.
    Если сообщение содержит стоп-слово или фразу, бот не будет отвечать.
    """

    def __init__(self, settings: BotSettings):
        self.settings = settings
        self._stop_words: List[str] = []
        self._refresh_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        """Инициализация - загружаем стоп-слова один раз"""
        await asyncio.get_event_loop().run_in_executor(None, self._load_once)

    def start_periodic_refresh(self) -> None:
        """Запуск периодического обновления стоп-слов"""
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def shutdown(self) -> None:
        """Остановка периодического обновления"""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self) -> None:
        """Периодическое обновление стоп-слов"""
        while True:
            try:
                await asyncio.sleep(self.settings.roster_refresh_seconds)
                await asyncio.get_event_loop().run_in_executor(None, self._load_once)
                logger.info(f"Обновлены стоп-слова. Загружено {len(self._stop_words)} слов/фраз")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка при обновлении стоп-слов: {e}")

    def _load_once(self) -> None:
        """Загрузка стоп-слов из Google Sheets"""
        try:
            if not self.settings.google_sheets_id:
                logger.warning("Google Sheets ID не настроен для стоп-слов")
                return

            credentials = self._build_credentials()
            if not credentials:
                logger.error("Не удалось создать credentials для Google Sheets")
                return

            gc = gspread.authorize(credentials)
            
            # Используем ID таблицы со стоп-словами из настроек
            sheet_id = self.settings.stop_words_sheet_id
            sheet = gc.open_by_key(sheet_id)
            
            # Пытаемся получить первый лист
            worksheet = sheet.sheet1
            
            # Получаем все значения из первого столбца (стоп-слова)
            values = worksheet.col_values(1)[1:]
            
            # Фильтруем пустые значения и приводим к нижнему регистру
            self._stop_words = [word.strip().lower() for word in values if word.strip()]
            
            logger.info(f"Загружено {len(self._stop_words)} стоп-слов из Google Sheets")
            
        except Exception as e:
            logger.error(f"Ошибка при загрузке стоп-слов из Google Sheets: {e}")

    def _build_credentials(self) -> Optional[Credentials]:
        """Создание credentials для доступа к Google Sheets"""
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            
            # Prefer explicit path
            if self.settings.google_sa_json_path:
                return Credentials.from_service_account_file(self.settings.google_sa_json_path, scopes=scopes)
            
            # Then base64 JSON
            if self.settings.google_sa_b64:
                data = json.loads(base64.b64decode(self.settings.google_sa_b64).decode("utf-8"))
                return Credentials.from_service_account_info(data, scopes=scopes)
            
            # Fallback to repo-local default file if present
            try:
                return Credentials.from_service_account_file("telegram_bot/google_credentials.json", scopes=scopes)
            except Exception:
                logger.error("Не настроены credentials для Google Sheets")
                return None
            
        except Exception as e:
            logger.error(f"Ошибка создания credentials для Google Sheets: {e}")
            return None

    def contains_stop_word(self, message: str) -> bool:
        """
        Проверяет, содержит ли сообщение стоп-слово или фразу.
        
        Args:
            message: Текст сообщения для проверки
            
        Returns:
            True если сообщение содержит стоп-слово/фразу, False иначе
        """
        if not self._stop_words or not message:
            return False
            
        message_lower = message.lower().strip()
        
        # Проверяем каждое стоп-слово/фразу
        for stop_word in self._stop_words:
            if stop_word in message_lower:
                logger.info(f"Обнаружено стоп-слово '{stop_word}' в сообщении: {message[:50]}...")
                return True
                
        return False

    def get_stop_words_count(self) -> int:
        """Возвращает количество загруженных стоп-слов"""
        return len(self._stop_words)

    def get_stop_words(self) -> List[str]:
        """Возвращает список стоп-слов (для отладки)"""
        return self._stop_words.copy()
