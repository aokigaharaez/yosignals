from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str = ""
    owner_id: int = 0
    owner_ids: tuple[int, ...] = ()
    timezone_name: str = "Europe/Simferopol"
    db_path: str = "signals.db"
    webapp_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    local_preview: bool = True
    bot_enabled: bool = True
    twelve_data_api_key: str = ""
    openai_api_key: str = ""
    market_cache_seconds: int = 60
    max_data_age_seconds: int = 90
    model_min_score: float = .60
    model_min_validation: float = .54
    auth_max_age_seconds: int = 3600

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def bootstrap_owner_ids(self) -> frozenset[int]:
        return frozenset(i for i in (self.owner_id, *self.owner_ids) if i > 0)


def load_settings() -> Settings:
    load_dotenv()
    defaults = Settings()
    values = {}
    for name in defaults.__dataclass_fields__:
        if name in {"owner_id", "owner_ids"}:
            continue
        env_name = {"owner_id": "OWNER_TELEGRAM_ID", "timezone_name": "TIMEZONE"}.get(name, name.upper())
        raw = os.getenv(env_name)
        default = getattr(defaults, name)
        if raw is None or not raw.strip():
            values[name] = default
        elif isinstance(default, bool):
            if raw.lower() not in {"true", "false", "1", "0"}:
                raise ValueError(f"{env_name} must be true or false")
            values[name] = raw.lower() in {"true", "1"}
        else:
            values[name] = type(default)(raw.strip())
    owners = []
    for key in ("OWNER_TELEGRAM_ID", "OWNER_TELEGRAM_IDS"):
        for item in re.split(r"[,;\s]+", os.getenv(key, "").strip()):
            if not item:
                continue
            if not item.isascii() or not item.isdigit() or not 0 < int(item) < 2**52:
                raise ValueError(f"{key} must contain positive Telegram user IDs separated by commas")
            owners.append(int(item))
    owners = list(dict.fromkeys(owners))
    settings = Settings(**values, owner_id=owners[0] if owners else 0, owner_ids=tuple(owners))
    ZoneInfo(settings.timezone_name)
    if settings.webapp_url and (urlparse(settings.webapp_url).scheme != "https" or not urlparse(settings.webapp_url).hostname):
        raise ValueError("WEBAPP_URL must be a public HTTPS URL")
    if not .5 < settings.model_min_score < 1 or not .5 <= settings.model_min_validation < 1:
        raise ValueError("Invalid model thresholds")
    if not 1 <= settings.market_cache_seconds <= 120 or not 30 <= settings.max_data_age_seconds <= 180:
        raise ValueError("Invalid market freshness settings")
    if settings.auth_max_age_seconds < 60 or not 1 <= settings.port <= 65535 or settings.owner_id < 0:
        raise ValueError("Invalid server settings")
    return settings

