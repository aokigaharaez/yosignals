import hashlib
import hmac
import json
import subprocess
import sys
import time
from dataclasses import replace
from urllib.parse import urlencode

import aiosqlite
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.analysis import analyze, dataset, features
from app.auth import AuthError, validate_init_data
from app.config import Settings
from app.db import Database
from app.market import MarketData, MarketError
from app.models import Candle
from app.services import SignalService
from app.web import create_app

TOKEN = "123456:TEST_TOKEN_NOT_REAL"


def signed(user=42, timestamp=None, **extra):
    data = {"auth_date": str(int(time.time()) if timestamp is None else timestamp),
            "user": json.dumps({"id": user, "first_name": "Tester"}), **extra}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def candles(count=1000, end=None):
    # Synthetic data exists exclusively in tests, never in runtime providers.
    end = (int(time.time()) // 60) * 60 if end is None else end
    rng = np.random.default_rng(7)
    prices = 100 + np.cumsum(rng.normal(0, .04, count + 1))
    return [Candle(end - (count-i)*60, float(prices[i]), float(max(prices[i:i+2])+.02),
                   float(min(prices[i:i+2])-.02), float(prices[i+1])) for i in range(count)]


def test_telegram_auth_tampering_expiry_and_duplicate_fields():
    valid = signed(timestamp=1000)
    assert validate_init_data(valid, TOKEN, 3600, now=1100)["id"] == 42
    for raw, now in [(valid.replace("Tester", "Intruder"),1100), (valid,5000),
                     (valid,900), (valid+"&auth_date=1000",1100), ("",1100)]:
        with pytest.raises(AuthError):
            validate_init_data(raw, TOKEN, 3600, now=now)


def test_telegram_auth_independent_node_crypto_vector():
    # Fixed vector generated with Node createHmac, independent of signed().
    raw = urlencode({"auth_date": "1000", "user": '{"id":42,"first_name":"Tester"}',
                     "hash": "7730699af60567bb991790d98fcf5d133d2c69730bc9f018c16b262fb4105341"})
    assert validate_init_data(raw, TOKEN, 3600, now=1100)["id"] == 42
    with pytest.raises(AuthError):
        validate_init_data(raw, "another-bot-token", 3600, now=1100)


def test_telegram_auth_rejects_reversed_hmac_arguments():
    data = {"auth_date": "1000", "user": '{"id":42}'}
    wrong_secret = hmac.new(TOKEN.encode(), b"WebAppData", hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    data["hash"] = hmac.new(wrong_secret, check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(AuthError):
        validate_init_data(urlencode(data), TOKEN, 3600, now=1100)


def test_features_are_causal_and_targets_match_entry_timing():
    data = candles()
    x, idx, target, _ = dataset(data, 3)
    prefix, _ = features(data[:400])
    np.testing.assert_allclose(prefix, x[:400])
    for i, label in zip(idx, target):
        assert label == int(data[i+4].close > data[i+2].open)
    changed = data.copy()
    changed[500] = replace(changed[500], time=changed[500].time + 1)
    _, changed_idx, _, _ = dataset(changed, 3)
    assert not any(496 <= i <= 530 for i in changed_idx)


def test_model_abstains_without_history_and_is_finite():
    assert analyze(candles(200), 3, Settings())["direction"] == "WAIT"
    run = analyze(candles(), 3, Settings())
    assert run["model_ready"]
    assert 50 <= run["score"] <= 100
    assert run["validation"]["samples"] > 20
    json.dumps(run, allow_nan=False)


def test_invalid_and_unclosed_candles():
    data = candles(3, end=1000200)
    future = replace(data[-1], time=1000200)
    assert MarketData.validate(data+[future], now=1000200) == data
    for bad in [replace(data[-1], close=float("nan")), replace(data[-1], high=.1), replace(data[-1], time=data[0].time)]:
        with pytest.raises(MarketError):
            MarketData.validate(data[:-1]+[bad], now=1000200)


@pytest.mark.asyncio
async def test_market_missing_key_and_no_random_fallback():
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(429, json={"error":"limit"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        market=MarketData(Settings(),client)
        for symbol, code in [("EURUSD","missing_key"),("EURUSD_OTC","unsupported"),("BTCUSDT","rate_limit")]:
            with pytest.raises(MarketError) as error:
                await market.candles(symbol)
            assert error.value.code == code
        with pytest.raises(MarketError):
            await market.candles("BTCUSDT")
        assert len(calls)==1  # Error backoff protects upstream quota.


@pytest.mark.asyncio
async def test_provider_parsing_closed_only_and_cache():
    rows=candles(4)
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json=[[c.time*1000,str(c.open),str(c.high),str(c.low),str(c.close)] for c in rows])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        market=MarketData(Settings(),client)
        assert await market.candles("BTCUSDT")==rows
        assert await market.candles("BTCUSDT")==rows
    assert len(calls)==1


@pytest.mark.asyncio
async def test_stale_data_never_creates_signal(tmp_path):
    class StaleMarket:
        async def candles(self, symbol):
            return candles(500,end=(int(time.time())//60)*60-600)
    service=SignalService(Settings(),StaleMarket(),Database(str(tmp_path/"test.db")))
    result=await service.snapshot("EURUSD",3)
    assert result["direction"]=="WAIT" and not result["fresh"]
    with pytest.raises(MarketError,match="устарели"):
        await service.create("EURUSD",3)


def run_payload(direction="CALL", close_at=None):
    return {"symbol":"EURUSD","expiry":3,"candle_time":1000,"direction":direction,
            "entry_at":1000,"close_at":close_at or int(time.time())-100,"reasons":[]}


@pytest.mark.asyncio
async def test_journal_dedup_atomic_result_legacy_preserved(tmp_path):
    path=str(tmp_path/"journal.db")
    async with aiosqlite.connect(path) as conn:
        await conn.execute("CREATE TABLE signals (id INTEGER)")
        await conn.execute("INSERT INTO signals VALUES(123)")
        await conn.commit()
    db=Database(path);await db.init();await db.init()
    a=await db.save(run_payload());b=await db.save(run_payload())
    assert a["id"]==b["id"]
    assert await db.set_result(a["id"],"WIN")
    assert not await db.set_result(a["id"],"LOSS")
    future={**run_payload(close_at=int(time.time())+600),"candle_time":2000}
    f=await db.save(future)
    assert not await db.set_result(f["id"],"WIN")
    wait=await db.save({**run_payload("WAIT"),"candle_time":3000})
    assert not await db.set_result(wait["id"],"WIN")
    assert (await db.stats())["wins"]==1
    assert await db.claim_event(a["id"],"entry")
    assert not await db.claim_event(a["id"],"entry")
    async with aiosqlite.connect(path) as conn:
        assert (await (await conn.execute("SELECT id FROM signals")).fetchone())[0]==123


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(bot_token=TOKEN,owner_id=42,bot_enabled=False,db_path=str(tmp_path/"api.db")))


def test_api_auth_preview_cannot_write_and_owner_isolation(app):
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",1234)) as client:
        assert client.get("/health").status_code==200
        assert client.get("/").status_code==200
        assert client.get("/api/session").json()["preview"]
        assert client.get("/api/history").status_code==401
        assert client.post("/api/analyses",json={"symbol":"EURUSD","expiry":3}).status_code==401
        assert client.post("/api/gpt/review",json={"symbol":"EURUSD","expiry":3}).status_code==401
        assert client.get("/api/session",headers={"Origin":"https://evil.example"}).status_code==401
        assert client.get("/api/history",headers={"Authorization":"tma "+signed(user=43)}).status_code==403
        assert client.post("/api/gpt/review",json={"symbol":"EURUSD","expiry":3},
                           headers={"Authorization":"tma "+signed(user=43)}).status_code==403
        client.headers["Authorization"]="tma "+signed()
        assert not client.get("/api/session").json()["preview"]
        assert not client.get("/api/session").json()["gpt_ready"]
        assert "openai_api_key" not in client.get("/api/session").json()
        assert client.post("/api/gpt/review",json={"symbol":"EURUSD","expiry":3}).json()["code"]=="missing_openai_key"
        assert client.get("/api/history").json()["items"]==[]
        assert client.get("/api/market?symbol=EURUSD").json()["code"]=="missing_key"
        assert client.get("/api/market?symbol=EURUSD_OTC").json()["code"]=="unsupported"
        assert client.post("/api/analyses",json={"symbol":"EURUSD","expiry":0}).status_code==422
        unavailable = client.put("/api/watch",json={"enabled":True,"symbol":"EURUSD","expiry":3})
        assert unavailable.status_code==503 and unavailable.json()["code"]=="missing_key"
        assert client.post("/api/history/999/result",json={"result":"WIN"}).status_code==409


def test_gpt_endpoint_uses_server_snapshot_and_authenticated_owner(tmp_path):
    settings = Settings(bot_token=TOKEN, owner_id=42, bot_enabled=False,
                        openai_api_key="test-secret", db_path=str(tmp_path / "gpt.db"))
    app = create_app(settings)
    calls = []
    async def snapshot(symbol, expiry):
        return {"symbol": symbol, "expiry": expiry, "fresh": True, "data_as_of": 123}
    async def review(data, model, user_id):
        calls.append((data, model, user_id))
        return {"text": "Разбор", "model": model}
    with TestClient(app) as client:
        app.state.service.snapshot = snapshot
        app.state.gpt.review = review
        auth = {"Authorization": "tma " + signed()}
        response = client.post("/api/gpt/review", headers=auth,
                               json={"symbol": "BTCUSDT", "expiry": 3, "model": "gpt-6-astra"})
        assert response.status_code == 200 and response.json()["text"] == "Разбор"
        assert calls == [({"symbol": "BTCUSDT", "expiry": 3, "fresh": True, "data_as_of": 123}, "gpt-6-astra", 42)]
        assert client.post("/api/gpt/review", headers=auth,
                           json={"symbol": "BTCUSDT", "model": "unknown"}).status_code == 422
        assert len(calls) == 1
        assert "test-secret" not in client.get("/api/session", headers=auth).text
        signal_calls = []
        async def create_gpt(symbol, expiry, model, user_id, gpt):
            signal_calls.append((symbol, expiry, model, user_id))
            return {"id": 1, "direction": "CALL", "engine": "gpt", "model": model}
        app.state.service.create_gpt = create_gpt
        response = client.post("/api/analyses", headers=auth,
                               json={"symbol": "BTCUSDT", "expiry": 3, "engine": "gpt", "model": "gpt-6-luna"})
        assert response.status_code == 200 and response.json()["engine"] == "gpt"
        assert signal_calls == [("BTCUSDT", 3, "gpt-6-luna", 42)]


def test_remote_requests_never_gain_preview_access(app):
    with TestClient(app,base_url="http://127.0.0.1",client=("198.51.100.2",1234)) as client:
        assert client.get("/api/session").status_code==401
        assert client.get("/api/session",headers={"X-Forwarded-For":"127.0.0.1"}).status_code==401


def test_web_import_does_not_load_telegram_or_ml_stack():
    result = subprocess.run([sys.executable, "-c",
        "import sys; import app.web; "
        "assert not any(m in sys.modules for m in ('aiogram', 'sklearn', 'numpy')); print('ok')"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_model_loading_does_not_save_placeholder_analysis(tmp_path):
    class FreshMarket:
        async def candles(self, symbol):
            return candles(500)
    db = Database(str(tmp_path / "loading.db"))
    await db.init()
    service = SignalService(Settings(), FreshMarket(), db)
    snapshot = await service.snapshot("EURUSD", 3)
    assert snapshot["direction"] == "WAIT"
    assert not snapshot["model_ready"]
    assert snapshot["candles"]
    with pytest.raises(MarketError) as error:
        await service.create("EURUSD", 3)
    assert error.value.code == "model_loading"
    assert await db.history() == []
