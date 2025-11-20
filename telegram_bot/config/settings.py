from dataclasses import dataclass, field
from typing import Dict, List, Optional
import os
from dotenv import load_dotenv
from pathlib import Path

# Путь к файлу .env теперь будет telegram_bot/.env
# settings.py -> config -> telegram_bot
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / '.env'

load_dotenv(ENV_PATH)


@dataclass
class BotSettings:
    token: str = os.getenv("BOT_TOKEN")
    openai_api_key: str = os.getenv("OPENAI_API_KEY") 
    bitrix_webhook: str = os.getenv("BITRIX_WEBHOOK")
    database_url: str = os.getenv("DATABASE_URL")
    
    # Perplexity (OpenAI-compatible) API key
    pplx_api_key: Optional[str] = os.getenv("PPLX_API_KEY")

    # Google Sheets mapping for chat -> responsible
    google_sheets_id: Optional[str] = os.getenv("GOOGLE_SHEETS_ID")
    # Service account credentials: either path to JSON or base64-encoded JSON
    google_sa_json_path: Optional[str] = os.getenv("GOOGLE_SA_JSON")
    google_sa_b64: Optional[str] = os.getenv("GOOGLE_SA_B64")
    
    # Google Sheets ID for stop words
    stop_words_sheet_id: str = os.getenv("STOP_WORDS_SHEET_ID", "1pnFHG61uL0VzAu2fm4uYiShHUsbVCHd5r2R_X65sD_A")

    # Default Bitrix responsible if mapping is missing
    default_responsible_id: Optional[int] = int(os.getenv("DEFAULT_RESPONSIBLE_ID")) if os.getenv("DEFAULT_RESPONSIBLE_ID") else None

    # Roster refresh interval (seconds)
    roster_refresh_seconds: int = int(os.getenv("ROSTER_REFRESH_SECONDS", "120"))
    
    processing_schedule: Dict = field(default_factory=lambda: {
        "weekdays": "09:00-18:00"
    })
    admin_users: List[int] = field(default_factory=list)

    # Telegram usernames of staff members (CSV), bot will not reply to them
    staff_usernames: List[str] = field(default_factory=lambda: [
        u.strip().lstrip('@') for u in (os.getenv("STAFF_USERNAMES", "")).split(",") if u.strip()
    ])

    sales_responsible_ids: List[int] = field(default_factory=lambda: [
        int(u.strip()) for u in (os.getenv("SALES_RESPONSIBLE_IDS", "2891,53")).split(",") if u.strip()
    ])