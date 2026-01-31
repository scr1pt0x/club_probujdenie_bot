import os
from dataclasses import dataclass


def _get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    # Bot
    bot_token: str = _get_env("BOT_TOKEN")
    admin_tg_ids: list[int] = None  # populated in __post_init__

    # Telegram access control
    primary_channel_id: int = int(_get_env("PRIMARY_CHANNEL_ID"))
    secondary_discussion_id: int = int(_get_env("SECONDARY_DISCUSSION_ID"))

    # DB
    database_url: str = _get_env("DATABASE_URL")

    # Business rules
    intro_price_rub: int = int(_get_env("INTRO_PRICE_RUB", "2990"))
    renewal_price_rub: int = int(_get_env("RENEWAL_PRICE_RUB", "1990"))
    grace_days: int = int(_get_env("GRACE_DAYS", "1"))
    pay_later_max_days: int = int(_get_env("PAY_LATER_MAX_DAYS", "7"))

    # Flow dates (UTC)
    free_flow_start: str = _get_env("FREE_FLOW_START", "2026-03-02")
    free_flow_end: str = _get_env("FREE_FLOW_END", "2026-03-29")
    next_paid_flow_sales_open: str = _get_env("NEXT_PAID_FLOW_SALES_OPEN", "2026-03-23")

    # Mailings
    mailings_enabled: bool = _get_env("MAILINGS_ENABLED", "true").lower() == "true"

    # Scheduler
    scheduler_timezone: str = _get_env("SCHEDULER_TZ", "UTC")

    def __post_init__(self):
        admin_ids_raw = _get_env("ADMIN_TG_IDS", "")
        admin_ids = []
        for item in admin_ids_raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                admin_ids.append(int(item))
            except ValueError as exc:
                raise RuntimeError(
                    "ADMIN_TG_IDS must be a comma-separated list of integers"
                ) from exc
        object.__setattr__(self, "admin_tg_ids", admin_ids)


settings = Settings()
