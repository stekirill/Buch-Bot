import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import gspread
import re
from google.oauth2.service_account import Credentials
from loguru import logger

from telegram_bot.config.settings import BotSettings


@dataclass
class RosterEntry:
    chat_id: int
    bitrix_responsible_id: Optional[int]
    bitrix_responsible_name: Optional[str]
    tg_responsibles: List[str]
    chat_title: Optional[str]


class RosterService:
    """
    Loads a mapping of Telegram chat_id -> {bitrix_responsible_id, tg_responsibles[]} from Google Sheets.
    Expected columns (case/space insensitive, ru headers supported):
      - "Название чата"
      - "айди чата"
      - "имя бухгалтера в битрикс"
      - "айди бухгалтера в битрикс"
      - "ответсвенные в чате"
    """

    def __init__(self, settings: BotSettings):
        self.settings = settings
        self._entries_by_chat_id: Dict[int, RosterEntry] = {}
        self._refresh_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        await asyncio.get_event_loop().run_in_executor(None, self._load_once)

    def start_periodic_refresh(self) -> None:
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def shutdown(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

    async def _refresh_loop(self) -> None:
        interval = max(30, int(self.settings.roster_refresh_seconds or 300))
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._load_once)
            except Exception as e:
                logger.error(f"Roster refresh failed: {e}")
            await asyncio.sleep(interval)

    def _load_once(self) -> None:
        if not self.settings.google_sheets_id:
            logger.warning("GOOGLE_SHEETS_ID is not set; roster will be empty")
            return
        creds = self._build_credentials()
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(self.settings.google_sheets_id)
        ws = sh.sheet1  # first worksheet
        records = ws.get_all_records()  # list[dict], header from first row
        parsed: Dict[int, RosterEntry] = {}
        for row in records:
            try:
                key_map = {self._norm_key(k): v for k, v in row.items()}
                # chat_id by various headers: supports RU/EN, with/without TG, with hyphens
                chat_id_raw = (
                    key_map.get("айдичата") or
                    key_map.get("идчата") or
                    key_map.get("chatid") or
                    key_map.get("tgid") or
                    key_map.get("tgchatid") or
                    key_map.get("chat_id") or
                    key_map.get("idchata") or
                    key_map.get("tgidchata")
                )
                if chat_id_raw is None:
                    # fuzzy: find a key that includes 'чат' and ('id' or 'айди') or contains 'tgid'
                    for k, v in key_map.items():
                        if (
                            ("чат" in k and ("id" in k or "айди" in k)) or
                            ("tgid" in k)
                        ):
                            chat_id_raw = v
                            break
                if chat_id_raw in (None, ""):
                    continue
                chat_id = int(str(chat_id_raw).strip())

                chat_title = key_map.get("названиечата") or key_map.get("chat_title")
                resp_name = (
                    key_map.get("имябухгалтеравбитрикс") or
                    key_map.get("имябухгалтера") or
                    key_map.get("bitrix_accountant_name")
                )
                resp_id_raw = (
                    key_map.get("айдябухгалтеравбитрикс") or
                    key_map.get("айдябухгалтера") or
                    key_map.get("idбухгалтеравбитрикс") or
                    key_map.get("idбухгалтера") or
                    key_map.get("bitrix_accountant_id")
                )
                try:
                    resp_id = int(str(resp_id_raw).strip()) if resp_id_raw not in (None, "") else None
                except Exception:
                    resp_id = None
                tg_resp_raw = (
                    key_map.get("ответственныевчате") or
                    key_map.get("ответсвенныевчате") or  # backward compatibility (typo)
                    key_map.get("telegram_responsibles") or
                    ""
                )
                tg_list = self._parse_usernames(str(tg_resp_raw))

                parsed[chat_id] = RosterEntry(
                    chat_id=chat_id,
                    bitrix_responsible_id=resp_id,
                    bitrix_responsible_name=str(resp_name) if resp_name not in (None, "") else None,
                    tg_responsibles=tg_list,
                    chat_title=str(chat_title) if chat_title not in (None, "") else None,
                )
            except Exception as e:
                logger.warning(f"Skip roster row due to error: {e}; row={row}")

        self._entries_by_chat_id = parsed
        logger.info(f"Roster loaded; entries={len(self._entries_by_chat_id)}")

    def _build_credentials(self) -> Credentials:
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
            raise RuntimeError("Google service account credentials not provided")

    @staticmethod
    def _norm_key(key: str) -> str:
        s = (key or "").strip().lower()
        # remove spaces and punctuation/hyphens to be tolerant to headers like "TG-ID чата"
        s = s.replace(" ", "")
        s = re.sub(r"[^a-zа-я0-9_]+", "", s)
        return s

    @staticmethod
    def _parse_usernames(value: str) -> List[str]:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        cleaned: List[str] = []
        for p in parts:
            u = p.lstrip("@")
            if u:
                cleaned.append(u)
        return cleaned

    def get_responsible_id(self, chat_id: int) -> Optional[int]:
        entry = self._entries_by_chat_id.get(chat_id)
        if entry and entry.bitrix_responsible_id is not None:
            return entry.bitrix_responsible_id
        return self.settings.default_responsible_id

    def get_tg_responsibles(self, chat_id: int) -> List[str]:
        entry = self._entries_by_chat_id.get(chat_id)
        return entry.tg_responsibles if entry else []

    def get_entry(self, chat_id: int) -> Optional[RosterEntry]:
        return self._entries_by_chat_id.get(chat_id)


