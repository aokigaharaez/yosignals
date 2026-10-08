import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gpt import GPTReview
from app.live import LiveQuotes
from app.market import MarketData, MarketError
from app.technical import market_context
from app.web import create_app
from test_core import candles, signed, TOKEN


def test_timeframes_require_complete_contiguous_closed_bars():
    end = int(time.time()) // 900 * 900
    rows = candles(600, end=end)
    context = market_context(rows)
    assert context['timeframes']['5min']['bar_count'] == 120
    assert context['timeframes']['15min']['bar_count'] == 40
    assert context['timeframes']['15min']['data_as_of'] == end
    assert context['timeframes']['15min']['indicators']['rsi'] is not None
    assert context['missing_minutes_last_60'] == 0
    broken = market_context(rows[:-3] + rows[-2:])
    assert broken['timeframes']['5min']['bar_count'] == 119
    assert broken['timeframes']['15min']['bar_count'] == 39
    assert broken['missing_minutes_last_60'] == 1
    assert not broken['news_available'] and not broken['volume_available']


def test_ticks_are_validated_and_never_change_closed_candles():
    feed = LiveQuotes(Settings())
    now = int(time.time())
    def tick(price, timestamp=now, symbol='EUR/USD'):
        feed.ingest({'event':'price','symbol':symbol,'price':price,'timestamp':timestamp})
    tick('1.12')
    tick('1.11', now-1)
    tick('nan')
    tick('inf')
    tick('-1')
    tick('1.5', now+100)
    tick('1.5', symbol='EUR/USD OTC')
    assert feed.view('EURUSD')['quote']['price'] == 1.12
    assert feed.view('EURUSD')['quote']['fresh']
    assert feed.view('BTCUSDT')['quote'] is None
    feed.quotes['EURUSD']['time'] = now-20
    assert not feed.view('EURUSD')['quote']['fresh']


@pytest.mark.asyncio
async def test_live_reads_return_immediately_and_share_one_background_refresh():
    feed = MarketData(Settings(), None)
    old = candles(100)
    feed.cache['EURUSD'] = (time.monotonic(), old)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def slow(symbol):
        calls.append(symbol)
        started.set()
        await release.wait()
        raise MarketError('network', 'Источник недоступен')
    feed.candles = slow
    try:
        first = feed.live_snapshot('EURUSD')
        await started.wait()
        for _ in range(60):
            result = feed.live_snapshot('EURUSD')
            assert result['candle_time'] == old[-1].time
        assert calls == ['EURUSD']
        assert first['refreshing']
        release.set()
        await feed.refreshes['EURUSD']
    finally:
        await feed.close()


def test_live_auth_and_60_refreshes_do_not_exhaust_analysis_quota(tmp_path):
    app = create_app(Settings(bot_token=TOKEN, owner_id=42, bot_enabled=False,
                             local_preview=False, db_path=str(tmp_path/'live.db')))
    with TestClient(app) as client:
        market = app.state.service.market
        rows = candles(100)
        market.cache['EURUSD'] = (time.monotonic(), rows)
        market.cache_minute['EURUSD'] = int(time.time())//60
        assert client.get('/api/live').status_code == 401
        assert client.get('/api/live', headers={'Authorization':'tma '+signed(43)}).status_code == 403
        client.headers['Authorization'] = 'tma '+signed()
        for _ in range(60):
            result = client.get('/api/live')
            assert result.status_code == 200
            assert 'candles' not in result.json() and result.json()['candle_price'] == rows[-1].close
        assert client.get('/api/market?symbol=EURUSD&engine=gpt').status_code == 200
        assert client.get('/api/live?symbol=EURUSD_OTC').json()['code'] == 'unsupported'


@pytest.mark.asyncio
async def test_gpt_receives_multiframe_context_and_entry_horizon_without_ml_bias():
    rows = candles(600)
    now = int(time.time())
    snapshot = {'symbol':'EURUSD','expiry':3,'fresh':True,'entry_at':now+60,'close_at':now+240,
                'data_as_of':now,'candle_time':rows[-1].time,'direction':'PUT','probability':{'value':99},
                'market_context':market_context(rows),'candles':[c.to_dict() for c in rows[-120:]]}
    def respond(request):
        context = json.loads(json.loads(request.content)['input'])
        assert len(context['candles']) == 60
        assert context['market_context']['timeframes']['15min']['available']
        assert context['forecast_horizon']['close_at'] == now+240
        assert 'direction' not in context and 'probability' not in context
        return httpx.Response(200, json={'status':'completed','output':[{'type':'message','role':'assistant',
            'content':[{'type':'output_text','text':json.dumps({'direction':'CALL','summary':'EMA вверх','risks':[]})}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GPTReview(Settings(openai_api_key='test'), client).review(snapshot,'gpt-6-luna',42,signal=True)
        assert result['prediction']['direction'] == 'CALL'


@pytest.mark.asyncio
async def test_late_bar_gets_one_extra_fetch_without_per_second_provider_requests(monkeypatch):
    from types import SimpleNamespace
    end = 1800000000
    clock = {'mono':20}
    monkeypatch.setattr('app.market.time', SimpleNamespace(time=lambda:end+17, monotonic=lambda:clock['mono']))
    feed = MarketData(Settings(), None)
    old = candles(100, end=end-60)
    feed.cache['EURUSD'] = (0, old)
    feed.cache_minute['EURUSD'] = end//60
    calls = []
    async def fetch(symbol):
        calls.append(symbol)
        return old
    feed._fetch = fetch
    await feed.candles('EURUSD')
    clock['mono'] = 45
    for _ in range(60):
        await feed.candles('EURUSD')
    assert calls == ['EURUSD']
    await feed.close()


@pytest.mark.asyncio
async def test_incremental_forex_refresh_keeps_full_archive(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.db import Database
    from test_eurusd import forex_payload
    end = 1800000000
    clock = [end+5]
    monkeypatch.setattr('app.market.time', SimpleNamespace(time=lambda:clock[0], monotonic=lambda:clock[0]))
    rows = candles(1500, end=end)
    newer = rows + [replace(rows[-1],time=rows[-1].time+60)]
    sizes = []
    def respond(request):
        sizes.append(request.url.params['outputsize'])
        return httpx.Response(200, json=forex_payload(rows if len(sizes)==1 else newer[-10:]))
    db = Database(str(tmp_path/'archive.db'))
    await db.init()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        feed = MarketData(Settings(twelve_data_api_key='test',forex_history_candles=1500,forex_backfill_candles=1500),client,history=db)
        await feed.candles('EURUSD')
        clock[0] += 60
        updated = await feed.candles('EURUSD')
        assert sizes == ['1500','10']
        assert len(updated) == 1501 and updated[-1].time == end
        assert await db.candle_history('BTCUSDT') == []
        await feed.close()
