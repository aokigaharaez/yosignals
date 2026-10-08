from datetime import datetime, timezone
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.market import MarketData, MarketError
from app.services import SignalService
from test_core import candles


def forex_payload(rows, symbol="EUR/USD"):
    return {"meta":{"symbol":symbol}, "values":[{
        "datetime":datetime.fromtimestamp(c.time, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        **{name:str(getattr(c,name)) for name in ("open","high","low","close")}} for c in rows]}


@pytest.mark.asyncio
async def test_eurusd_training_and_probability_use_only_eurusd_archive(tmp_path):
    db=Database(str(tmp_path/"pairs.db")); await db.init()
    crypto=candles(1000)
    await db.store_candles("BTCUSDT",crypto)
    forex=[replace(c,open=c.open/100,high=c.high/100,low=c.low/100,close=c.close/100) for c in candles(500)]
    requests=[]
    def respond(request):
        if "end_date" in request.url.params:
            return httpx.Response(429,json={"status":"error","code":429})
        requests.append(request)
        assert request.url.host=="api.twelvedata.com"
        assert request.url.params["symbol"]=="EUR/USD"
        assert request.url.params["outputsize"]=="5000"
        return httpx.Response(200,json=forex_payload(forex))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        settings=Settings(twelve_data_api_key="test-key")
        market=MarketData(settings,client,history=db)
        service=SignalService(settings,market,db)
        seen=[]
        def analyzer(rows,expiry,settings,entry_delay):
            seen.append(rows)
            return {"direction":"CALL","probability":{"value":51.,"note":"Test"},
                    "validation":{"samples":30},"training":{"history_candles":len(rows)},
                    "model_ready":True,"reasons":[]}
        service.analyzer=analyzer; service.model_status="ready"
        run=await service.snapshot("EURUSD",3)
        assert run["fresh"] and run["sample_count"]==500
        assert seen==[forex]
        for field in ("probability","validation","training"):
            assert run[field]["symbol"]=="EURUSD" and run[field]["provider"]=="Twelve Data"
            assert run[field]["expiry"]==3
        assert await db.candle_history("BTCUSDT")==crypto
        assert await db.candle_history("EURUSD")==forex
        assert len(requests)==1 and set(market.backfills)=={"EURUSD"}
        await market.close()


@pytest.mark.asyncio
async def test_eurusd_rejects_different_pair_from_provider():
    def respond(request):
        return httpx.Response(200,json=forex_payload(candles(500),symbol="GBP/USD"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        market=MarketData(Settings(twelve_data_api_key="test-key"),client)
        with pytest.raises(MarketError) as exc:
            await market.candles("EURUSD")
        assert exc.value.code=="invalid_data"


@pytest.mark.asyncio
async def test_eurusd_backfill_downloads_earlier_forex_pages(tmp_path):
    db=Database(str(tmp_path/"eurusd-history.db"));await db.init()
    rows=candles(10000)
    await db.store_candles("EURUSD",rows[5000:])
    requests=[]
    def respond(request):
        requests.append(request)
        assert request.url.host=="api.twelvedata.com"
        assert request.url.params["symbol"]=="EUR/USD"
        assert request.url.params["outputsize"]=="5000"
        assert request.url.params["end_date"]==datetime.fromtimestamp(rows[4999].time,timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return httpx.Response(200,json=forex_payload(rows[:5000]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        market=MarketData(Settings(twelve_data_api_key="test-key",forex_backfill_candles=10000),client,history=db)
        await market._backfill("EURUSD",rows[5000].time,5000)
        assert await db.candle_history("EURUSD")==rows
        assert len(requests)==1
        await market.close()
