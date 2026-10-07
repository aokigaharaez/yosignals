import asyncio
import json
import time

import httpx
import pytest

from app.config import Settings
from app.gpt import GPTError, GPTReview


SNAPSHOT = {"fresh": True, "symbol": "BTCUSDT", "expiry": 3,
            "data_as_of": int(time.time()), "entry_at": int(time.time()) + 60,
            "direction": "CALL", "probability": {"value": 55}, "candles": [{"close": 1}] * 120}


@pytest.mark.asyncio
async def test_gpt_payload_result_and_cooldown():
    calls = []
    def respond(request):
        calls.append(request)
        payload = json.loads(request.content)
        context = json.loads(payload["input"])
        assert len(context["candles"]) == 30
        assert payload["store"] is False
        assert payload["model"] == "gpt-6.1-sol"
        assert "secret-key" not in request.content.decode()
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Разбор"}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret-key"), client)
        result = await review.review(SNAPSHOT, "gpt-6.1-sol", 42)
        assert result["text"] == "Разбор" and result["symbol"] == "BTCUSDT"
        with pytest.raises(GPTError, match="раз в минуту"):
            await review.review(SNAPSHOT, "gpt-6.1-sol", 42)
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
async def test_gpt_provider_errors_do_not_leak_details(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"error": "secret-provider-details"}))) as client:
        review = GPTReview(Settings(openai_api_key="secret-key"), client)
        with pytest.raises(GPTError) as error:
            await review.review(SNAPSHOT, "gpt-6-astra", 42)
        assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_gpt_rejects_missing_key_stale_data_and_unknown_models():
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        review = GPTReview(Settings(), client)
        with pytest.raises(GPTError) as error:
            await review.review(SNAPSHOT, "gpt-6-luna", 42)
        assert error.value.code == "missing_openai_key"
        review = GPTReview(Settings(openai_api_key="secret"), client)
        for snapshot, model, code in [({**SNAPSHOT, "fresh": False}, "gpt-6-luna", "stale_market"),
                                       (SNAPSHOT, "unknown", "unsupported_model")]:
            with pytest.raises(GPTError) as error:
                await review.review(snapshot, model, 42)
            assert error.value.code == code
        assert calls == []


@pytest.mark.asyncio
async def test_gpt_timeout_and_incomplete_response():
    def timeout(request):
        raise httpx.ReadTimeout("secret", request=request)
    for handler, expected in [(timeout, "gpt_timeout"),
                              (lambda r: httpx.Response(200, json={"status": "incomplete", "output": []}), "gpt_empty")]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(GPTError) as error:
                await GPTReview(Settings(openai_api_key="secret"), client).review(SNAPSHOT, "gpt-6-luna", 42)
            assert error.value.code == expected


@pytest.mark.asyncio
async def test_gpt_blocks_concurrent_requests():
    started, release = asyncio.Event(), asyncio.Event()
    async def respond(request):
        started.set()
        await release.wait()
        return httpx.Response(500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        first = asyncio.create_task(review.review(SNAPSHOT, "gpt-6-luna", 42))
        await started.wait()
        with pytest.raises(GPTError) as error:
            await review.review(SNAPSHOT, "gpt-6-luna", 43)
        assert error.value.code == "gpt_busy"
        release.set()
        with pytest.raises(GPTError):
            await first

@pytest.mark.asyncio
async def test_gpt_independent_prediction_and_engine_journal(tmp_path):
    from app.db import Database
    from app.services import SignalService
    db = Database(str(tmp_path / 'signals.db'))
    await db.init()
    base = {**SNAPSHOT, 'label': 'BTC/USDT', 'provider': 'Binance',
            'candle_time': int(time.time()) - 60, 'entry_at': int(time.time()) + 180,
            'close_at': int(time.time()) + 360, 'indicators': {}, 'model': 'ML',
            'direction': 'PUT', 'probability': {'value': 90}}
    def respond(request):
        payload = json.loads(request.content)
        context = json.loads(payload['input'])
        assert 'direction' not in context and 'probability' not in context and 'model' not in context
        assert payload['text']['format']['type'] == 'json_schema'
        return httpx.Response(200, json={'status':'completed', 'output':[
            {'type':'message','role':'assistant','content':[{'type':'output_text',
            'text':json.dumps({'direction':'CALL','summary':'Рост по свечам','risks':['Волатильность']})}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = SignalService(Settings(), None, db)
        async def snapshot(symbol, expiry, ml=True):
            assert ml is False
            return dict(base)
        service.snapshot = snapshot
        result = await service.create_gpt('BTCUSDT', 3, 'gpt-6-luna', 42,
                                         GPTReview(Settings(openai_api_key='secret'), client))
        assert result['direction'] == 'CALL' and result['engine'] == 'gpt'
        assert result['probability'] is None and result['validation'] is None
        ml_result = await db.save({**result, 'engine':'ml', 'direction':'PUT', 'model':'ML'})
        assert ml_result['id'] != result['id']
        assert (await db.get(result['id']))['direction'] == 'CALL'
        await db.init()
        assert len(await db.history()) == 2


@pytest.mark.asyncio
async def test_gpt_late_prediction_becomes_wait(tmp_path):
    from app.db import Database
    from app.services import SignalService
    db = Database(str(tmp_path / 'late.db'))
    await db.init()
    service = SignalService(Settings(), None, db)
    base = {**SNAPSHOT, 'candle_time': 100, 'data_as_of':int(time.time()),
            'entry_at':int(time.time())+180, 'close_at':int(time.time())+360}
    async def snapshot(symbol, expiry, ml=True): return dict(base)
    class SlowGPT:
        async def review(self, data, model, user_id, signal=False):
            data['data_as_of'] = int(time.time()) - 200
            return {'prediction':{'direction':'PUT','summary':'Снижение','risks':[]}}
    service.snapshot = snapshot
    result = await service.create_gpt('BTCUSDT', 3, 'gpt-6-luna', 42, SlowGPT())
    assert result['direction'] == 'WAIT'
    assert result['probability'] is None

@pytest.mark.asyncio
async def test_existing_journal_migration_preserves_ids_results_and_owners(tmp_path):
    import aiosqlite
    from app.db import Database
    path = str(tmp_path / 'old.db')
    payload = {**SNAPSHOT, 'candle_time':100, 'entry_at':200, 'close_at':300}
    async with aiosqlite.connect(path) as conn:
        await conn.execute('''CREATE TABLE analysis_runs_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, expiry INTEGER, candle_time INTEGER,
            direction TEXT, entry_at INTEGER, close_at INTEGER, created_at INTEGER,
            payload TEXT, result TEXT, result_source TEXT,
            UNIQUE(symbol,expiry,candle_time,entry_at))''')
        await conn.execute('INSERT INTO analysis_runs_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (9,'BTCUSDT',3,100,'CALL',200,300,150,json.dumps(payload),'WIN','user_reported'))
        await conn.commit()
    db = Database(path)
    await db.init()
    record = await db.get(9)
    assert record['result'] == 'WIN'
    await db.add_owner(42, 43)
    new = await db.save({**payload, 'direction':'PUT', 'engine':'gpt', 'model':'gpt-6-luna'})
    assert new['id'] > 9
    await db.init()
    assert (await db.get(9))['result'] == 'WIN'
    assert await db.is_owner(42)

@pytest.mark.asyncio
async def test_fast_luna_forecast_reuses_completed_result_without_another_charge():
    calls = []
    def respond(request):
        payload = json.loads(request.content)
        assert payload['model'] == 'gpt-6-luna'
        assert payload['reasoning']['effort'] == 'none'
        assert payload['max_output_tokens'] == 1200
        calls.append(request)
        return httpx.Response(200, json={'status':'completed','output':[
            {'type':'message','role':'assistant','content':[{'type':'output_text',
            'text':json.dumps({'direction':'PUT','summary':'Импульс вниз','risks':['Слабый сигнал']})}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key='secret'), client)
        first = await review.review(SNAPSHOT, 'gpt-6-luna', 42, signal=True)
        second = await review.review(SNAPSHOT, 'gpt-6-luna', 42, signal=True)
        assert second['prediction']['direction'] == 'PUT'
        assert len(calls) == 1
        second['prediction']['direction'] = 'CALL'
        assert first['prediction']['direction'] == 'PUT'
        with pytest.raises(GPTError) as error:
            await review.review({**SNAPSHOT, 'entry_at':SNAPSHOT['entry_at']+60}, 'gpt-6-luna', 42, signal=True)
        assert error.value.code == 'gpt_rate_limit'


@pytest.mark.asyncio
async def test_gpt_deadline_cancels_slow_provider_and_releases_lock(monkeypatch):
    monkeypatch.setattr('app.gpt.SIGNAL_TIMEOUT', .01)
    async def respond(request):
        await asyncio.sleep(.2)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key='secret'), client)
        with pytest.raises(GPTError) as error:
            await review.review(SNAPSHOT, 'gpt-6-luna', 42, signal=True)
        assert error.value.code == 'gpt_timeout'
        assert not review.lock.locked()


@pytest.mark.asyncio
async def test_gpt_market_snapshot_does_not_wait_for_ml_training(tmp_path):
    from app.services import SignalService
    from app.db import Database
    from test_core import candles
    class Market:
        async def candles(self, symbol): return candles(400)
    service = SignalService(Settings(), Market(), Database(str(tmp_path/'unused.db')))
    await service.lock.acquire()
    try:
        result = await asyncio.wait_for(service.snapshot('BTCUSDT', 3, ml=False), timeout=1)
        assert result['fresh'] and result['candles']
    finally:
        service.lock.release()
