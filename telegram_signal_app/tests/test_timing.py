import time
from types import SimpleNamespace

import pytest
from app.timing import plan_entry, validate_entry
from app.market import MarketError
from app.services import SignalService
from app.config import Settings
from app.db import Database
from app.analysis import analyze
from test_core import candles


def test_entry_validation_and_custom_plan():
    boundary = 1800000000
    assert plan_entry(boundary, now=boundary+5) == (boundary+60, 1)
    assert plan_entry(boundary, boundary+600, now=boundary+5) == (boundary+600, 10)
    for entry in (boundary, boundary+61, boundary+86460, True):
        with pytest.raises(MarketError):
            validate_entry(entry, now=boundary+5)


@pytest.mark.asyncio
async def test_custom_time_and_expiry_reach_model_and_journal(tmp_path, monkeypatch):
    boundary = 1800000000
    monkeypatch.setattr('app.services.time', SimpleNamespace(time=lambda: boundary+5))
    class Market:
        async def candles(self, symbol): return candles(1000, end=boundary)
    db = Database(str(tmp_path/'custom.db'))
    await db.init()
    service = SignalService(Settings(), Market(), db)
    calls = []
    def model(rows, expiry, settings, entry_delay):
        calls.append((expiry, entry_delay))
        return {'direction':'CALL','reasons':['Рост'],'model_ready':True,'indicators':{}}
    service.analyzer, service.model_status = model, 'ready'
    run = await service.create('BTCUSDT', 7, entry_at=boundary+600)
    assert calls == [(7, 10)]
    assert run['entry_at'] == boundary+600
    assert run['close_at'] == boundary+1020
    assert (await db.get(run['id']))['expiry'] == 7


def test_distant_entry_with_insufficient_history_returns_wait():
    result = analyze(candles(1000), 60, Settings(), entry_delay=1000)
    assert result['direction'] == 'WAIT' and result['reasons']


@pytest.mark.asyncio
async def test_gpt_custom_entry_is_not_shifted(tmp_path):
    db = Database(str(tmp_path/'gpt-time.db'))
    await db.init()
    service = SignalService(Settings(), None, db)
    entry = (int(time.time())//60+10)*60
    async def snapshot(symbol, expiry, ml=True, entry_at=None):
        assert ml is False and entry_at == entry
        return {'symbol':symbol,'expiry':expiry,'fresh':True,'data_as_of':int(time.time())//60*60,
                'candle_time':int(time.time())//60*60-60,'candles':[]}
    class GPT:
        async def review(self, data, model, user_id, signal=False):
            assert data['entry_at'] == entry and data['close_at'] == entry+7*60
            return {'prediction':{'direction':'PUT','summary':'Снижение','risks':[]}}
    service.snapshot = snapshot
    result = await service.create_gpt('BTCUSDT',7,'gpt-6-luna',42,GPT(),entry_at=entry)
    assert result['entry_at'] == entry and result['direction'] == 'PUT'
