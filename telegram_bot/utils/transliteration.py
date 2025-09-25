"""
Утилиты для транслитерации имен с латиницы на кириллицу.
"""

import re
from typing import Optional

# Словарь транслитерации латиница -> кириллица
TRANSLITERATION_MAP = {
    # Основные буквы
    'a': 'а', 'b': 'б', 'c': 'ц', 'd': 'д', 'e': 'е', 'f': 'ф', 'g': 'г',
    'h': 'х', 'i': 'и', 'j': 'й', 'k': 'к', 'l': 'л', 'm': 'м', 'n': 'н',
    'o': 'о', 'p': 'п', 'q': 'к', 'r': 'р', 's': 'с', 't': 'т', 'u': 'у',
    'v': 'в', 'w': 'в', 'x': 'кс', 'y': 'й', 'z': 'з',
    
    # Заглавные буквы
    'A': 'А', 'B': 'Б', 'C': 'Ц', 'D': 'Д', 'E': 'Е', 'F': 'Ф', 'G': 'Г',
    'H': 'Х', 'I': 'И', 'J': 'Й', 'K': 'К', 'L': 'Л', 'M': 'М', 'N': 'Н',
    'O': 'О', 'P': 'П', 'Q': 'К', 'R': 'Р', 'S': 'С', 'T': 'Т', 'U': 'У',
    'V': 'В', 'W': 'В', 'X': 'Кс', 'Y': 'Й', 'Z': 'З',
    
    # Двубуквенные сочетания (должны идти перед одиночными буквами)
    'ch': 'ч', 'sh': 'ш', 'zh': 'ж', 'ts': 'ц', 'ya': 'я', 'yo': 'ё',
    'yu': 'ю', 'ye': 'е', 'yi': 'ы', 'Ch': 'Ч', 'Sh': 'Ш', 'Zh': 'Ж',
    'Ts': 'Ц', 'Ya': 'Я', 'Yo': 'Ё', 'Yu': 'Ю', 'Ye': 'Е', 'Yi': 'Ы',
    'CH': 'Ч', 'SH': 'Ш', 'ZH': 'Ж', 'TS': 'Ц', 'YA': 'Я', 'YO': 'Ё',
    'YU': 'Ю', 'YE': 'Е', 'YI': 'Ы',
    
    # Трехбуквенные сочетания
    'sch': 'щ', 'Sch': 'Щ', 'SCH': 'Щ',
}

# Популярные имена для более точной транслитерации
COMMON_NAMES = {
    # Мужские имена
    'alexander': 'Александр', 'alex': 'Алекс', 'andrey': 'Андрей', 'andrei': 'Андрей',
    'anton': 'Антон', 'artem': 'Артем', 'artur': 'Артур', 'boris': 'Борис',
    'dmitry': 'Дмитрий', 'dmitri': 'Дмитрий', 'dmitriy': 'Дмитрий', 'dima': 'Дима',
    'eugene': 'Евгений', 'evgeny': 'Евгений', 'evgeniy': 'Евгений', 'zhenya': 'Женя',
    'igor': 'Игорь', 'ivan': 'Иван', 'vanya': 'Ваня', 'kirill': 'Кирилл',
    'konstantin': 'Константин', 'kostya': 'Костя', 'maksim': 'Максим', 'maxim': 'Максим',
    'mikhail': 'Михаил', 'misha': 'Миша', 'nikolay': 'Николай', 'kolya': 'Коля',
    'oleg': 'Олег', 'pavel': 'Павел', 'pasha': 'Паша', 'roman': 'Роман',
    'sergey': 'Сергей', 'sergei': 'Сергей', 'seryozha': 'Сережа', 'vladimir': 'Владимир',
    'volodya': 'Володя', 'yuri': 'Юрий', 'yury': 'Юрий', 'yura': 'Юра',
    
    # Женские имена
    'alexandra': 'Александра', 'sasha': 'Саша', 'anastasia': 'Анастасия', 'nastya': 'Настя',
    'anna': 'Анна', 'anya': 'Аня', 'elena': 'Елена', 'lena': 'Лена',
    'elizaveta': 'Елизавета', 'liza': 'Лиза', 'irina': 'Ирина', 'ira': 'Ира',
    'katerina': 'Екатерина', 'katya': 'Катя', 'maria': 'Мария', 'masha': 'Маша',
    'natalia': 'Наталья', 'natalya': 'Наталья', 'natasha': 'Наташа', 'olga': 'Ольга',
    'sophia': 'Софья', 'sofia': 'Софья', 'sonya': 'Соня', 'tatyana': 'Татьяна',
    'tanya': 'Таня', 'valentina': 'Валентина', 'valya': 'Валя', 'vera': 'Вера',
    'victoria': 'Виктория', 'vika': 'Вика', 'yulia': 'Юлия', 'yulya': 'Юля',
}

def transliterate_name(name: str) -> str:
    """
    Транслитерирует имя с латиницы на кириллицу.
    
    Args:
        name: Имя для транслитерации
        
    Returns:
        Транслитерированное имя на кириллице
    """
    if not name or not isinstance(name, str):
        return name
    
    # Убираем лишние пробелы
    name = name.strip()
    
    # Если имя уже на кириллице, возвращаем как есть
    if _is_cyrillic(name):
        return name
    
    # Проверяем, есть ли это популярное имя
    name_lower = name.lower()
    if name_lower in COMMON_NAMES:
        # Сохраняем регистр первой буквы
        if name[0].isupper():
            return COMMON_NAMES[name_lower]
        else:
            return COMMON_NAMES[name_lower].lower()
    
    # Транслитерация по буквам
    result = name
    
    # Сначала обрабатываем многосимвольные сочетания
    for latin, cyrillic in sorted(TRANSLITERATION_MAP.items(), key=len, reverse=True):
        if len(latin) > 1:  # Только многосимвольные сочетания
            result = result.replace(latin, cyrillic)
    
    # Затем одиночные символы
    for latin, cyrillic in TRANSLITERATION_MAP.items():
        if len(latin) == 1:  # Только одиночные символы
            result = result.replace(latin, cyrillic)
    
    return result

def _is_cyrillic(text: str) -> bool:
    """
    Проверяет, содержит ли текст кириллические символы.
    
    Args:
        text: Текст для проверки
        
    Returns:
        True, если текст содержит кириллические символы
    """
    return bool(re.search(r'[а-яё]', text, re.IGNORECASE))

def get_russian_name(original_name: str) -> str:
    """
    Возвращает русское имя, транслитерируя при необходимости.
    
    Args:
        original_name: Оригинальное имя из Telegram
        
    Returns:
        Имя на кириллице для использования в ответах бота
    """
    if not original_name:
        return ""
    
    # Берём только первое слово (до пробела/многословного ФИО)
    first_token = original_name.strip().split()[0] if isinstance(original_name, str) else original_name
    
    # Транслитерируем только первое слово
    russian_name = transliterate_name(first_token)
    
    # Если транслитерация не изменила имя (уже кириллица), возвращаем как есть
    if russian_name == first_token:
        return first_token
    
    # Если имя изменилось, возвращаем транслитерированное
    return russian_name