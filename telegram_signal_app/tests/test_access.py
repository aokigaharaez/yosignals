import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.db import Database
from app.web import create_app
from test_core import TOKEN, signed


def make_app(tmp_path):
    return create_app(Settings(bot_token=TOKEN, owner_id=42, owner_ids=(42, 44),
                               bot_enabled=False, db_path=str(tmp_path / "owners.db")))


def authorize(client, user=42):
    client.headers["Authorization"] = "tma " + signed(user=user)


def test_multiple_environment_ids_and_legacy_config(monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda: None)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "42, 43;44")
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "44 45")
    settings = load_settings()
    assert settings.owner_id == 42 and settings.bootstrap_owner_ids == {42, 43, 44, 45}
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "42")
    monkeypatch.delenv("OWNER_TELEGRAM_IDS")
    assert load_settings().bootstrap_owner_ids == {42}
    for value in ("-123", "@username", "0", "123.45", str(2**52)):
        monkeypatch.setenv("OWNER_TELEGRAM_ID", value)
        with pytest.raises(ValueError):
            load_settings()


def test_owners_api_never_allows_preview_or_unapproved_accounts(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/session").json()["preview"]
        for path, method, body in [
            ("/api/owners", "GET", None),
            ("/api/owners", "POST", {"telegram_id": 43}),
            ("/api/owners/44", "DELETE", None),
        ]:
            assert client.request(method, path, json=body).status_code == 401
            authorize(client, 43)
            assert client.request(method, path, json=body).status_code == 403
            client.headers.pop("Authorization")


def test_grant_revoke_and_environment_owners_are_protected(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        authorize(client, 44)
        assert client.get("/api/history").status_code == 200
        authorize(client)
        for invalid in (0, -1, 2**52, "43", True, 43.5):
            assert client.post("/api/owners", json={"telegram_id": invalid}).status_code == 422
        assert client.post("/api/owners", json={"telegram_id": 43}).status_code == 200
        assert client.post("/api/owners", json={"telegram_id": 43}).status_code == 200
        rows = client.get("/api/owners").json()["items"]
        assert len(rows) == 3
        assert next(r for r in rows if r["telegram_id"] == 43)["added_by"] == 42
        authorize(client, 43)
        assert client.get("/api/session").json()["user"]["id"] == 43
        assert client.get("/api/history").status_code == 200
        assert client.post("/api/owners", json={"telegram_id": 45}).status_code == 200
        assert client.delete("/api/owners/42").status_code == 409
        assert client.delete("/api/owners/43").status_code == 409
        assert client.delete("/api/owners/45").status_code == 200
        authorize(client)
        assert client.delete("/api/owners/43").status_code == 200
        # The same still-valid Telegram signature loses access immediately.
        authorize(client, 43)
        assert client.get("/api/history").status_code == 403
        assert client.get("/api/watch").status_code == 403
        assert client.post("/api/owners", json={"telegram_id": 46}).status_code == 403
    # Only undeleted access persists on restart.
    with TestClient(make_app(tmp_path)) as client:
        authorize(client)
        assert client.post("/api/owners", json={"telegram_id": 46}).status_code == 200
    with TestClient(make_app(tmp_path)) as client:
        authorize(client, 46)
        assert client.get("/api/history").status_code == 200


@pytest.mark.asyncio
async def test_legacy_bot_commands_only_link_to_miniapp_and_respect_dynamic_access(tmp_path, monkeypatch):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Message, Update
    from app.handlers import build_router

    db = Database(str(tmp_path / "bot.db"))
    await db.init()
    await db.add_owner(43, 42)
    settings = Settings(bot_token=TOKEN, owner_id=42, webapp_url="https://example.com")
    dp = Dispatcher()
    dp.include_router(build_router(settings, db))
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    bot = Bot(TOKEN)
    try:
        for command in ("/analyze BTCUSDT 3", "/watch EURUSD 1", "/stats"):
            message = Message(message_id=1, date=datetime.now(timezone.utc),
                              chat={"id": 43, "type": "private"},
                              from_user={"id": 43, "is_bot": False, "first_name": "Test"}, text=command)
            await dp.feed_update(bot, Update(update_id=int(time.time()), message=message))
            assert "Mini App" in answer.call_args.args[0]
            assert answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url == settings.webapp_url
        assert await db.history() == []
        assert not (await db.watch())["enabled"]
        await db.remove_owner(43)
        await dp.feed_update(bot, Update(update_id=2, message=message.model_copy(update={"text": "/start"})))
        assert "Доступ пока не выдан" in answer.call_args.args[0]
        assert "reply_markup" not in answer.call_args.kwargs
    finally:
        await bot.session.close()
