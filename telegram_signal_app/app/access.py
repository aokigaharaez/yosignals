"""Shared authorization for signed Mini App requests and Telegram updates."""
from .config import Settings
from .db import Database


async def is_owner(settings: Settings, db: Database, telegram_id: int) -> bool:
    return telegram_id in settings.bootstrap_owner_ids or await db.is_owner(telegram_id)
