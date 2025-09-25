from __future__ import annotations
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Tuple

MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    return datetime.now(tz=MSK)


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":", 1)
    return time(int(hh), int(mm), tzinfo=MSK)


def _parse_window(window_str: str) -> Tuple[time, time]:
    start_s, end_s = window_str.split("-", 1)
    return _parse_hhmm(start_s), _parse_hhmm(end_s)


def is_processing_window_now(schedule: Dict, now: datetime | None = None) -> bool:
    """Returns True if current MSK time is within configured processing window on a weekday (Mon-Fri).

    Business hours policy (per user request): 09:00–18:00 MSK, weekdays only.
    If schedule dict contains custom values, still respect them.
    """
    current = now or now_msk()
    print(current)
    # Weekdays: 0..4 Mon-Fri
    if current.weekday() > 4:
        return False
    window: str = schedule.get("weekdays", "09:00-18:00")
    start_t, end_t = _parse_window(window)
    today = current.date()
    start_dt = datetime.combine(today, start_t)
    end_dt = datetime.combine(today, end_t)
    return start_dt <= current <= end_dt


def next_delivery_slot_label(schedule: Dict, now: datetime | None = None, min_lead_minutes: int | None = None) -> str:
    """Deprecated: explicit delivery slots no longer used."""
    current = now or now_msk()
    return f"сегодня в {current.hour:02d}:{current.minute:02d}"
