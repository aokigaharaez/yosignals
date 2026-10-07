import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def validate_init_data(raw: str, bot_token: str, max_age: int, now: int | None = None) -> dict:
    if not raw or len(raw) > 8192 or not bot_token:
        raise AuthError("Откройте приложение через Telegram.")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError("Duplicate fields")
        supplied = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        # Telegram Mini Apps: WebAppData is the HMAC key; the token is the message.
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("Invalid signature")
        age = (int(time.time()) if now is None else now) - int(data["auth_date"])
        if age < -30 or age > max_age:
            raise ValueError("Expired session")
        user = json.loads(data["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError("Invalid user")
        return user
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthError("Сессия Telegram недействительна или истекла. Откройте Mini App заново.") from exc
