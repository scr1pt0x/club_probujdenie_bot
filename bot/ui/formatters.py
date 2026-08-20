from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings


def format_price_rub(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_local_date(value: datetime) -> str:
    timezone = ZoneInfo(settings.scheduler_timezone)
    return value.astimezone(timezone).strftime("%d.%m.%Y")


def format_flow_period(start_at: datetime, end_at: datetime) -> str:
    return f"{format_local_date(start_at)} — {format_local_date(end_at)}"
