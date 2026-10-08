from dataclasses import replace
import time

import httpx
import numpy as np
import pytest

from app.analysis import MLTrainer, selective_validation
from app.config import Settings
from app.db import Database
from app.market import MarketData
from app.services import SignalService
from test_core import candles


def test_strong_signals_require_untouched_test_success_not_raw_confidence():
    settings = Settings()
    scores = np.full(200, .95)
    cal_wins = np.ones(200, dtype=bool)
    bad = selective_validation(scores, cal_wins, scores, np.zeros(200, dtype=bool), settings)
    good = selective_validation(scores, cal_wins, scores, cal_wins, settings)
    assert bad["threshold"] == good["threshold"] == .50
    assert not bad["passed"] and good["passed"]
    assert bad["accuracy"] == 0 and good["accuracy"] == 100
    scarce = selective_validation(scores[:10], cal_wins[:10], scores, cal_wins, settings)
    assert scarce["threshold"] is None and not scarce["passed"]


def test_training_reuses_model_but_predicts_new_candles(monkeypatch):
    import app.analysis as module
    original = module.fit_selected_model
    fits = []
    def spy(*args):
        fits.append(1)
        return original(*args)
    monkeypatch.setattr(module, "fit_selected_model", spy)
    trainer = MLTrainer()
    rows = candles(1000)
    first = trainer(rows, 3, Settings())
    next_bar = replace(rows[-1], time=rows[-1].time+60, open=rows[-1].close,
                       high=rows[-1].close+1, low=rows[-1].close-.1, close=rows[-1].close+.9)
    second = trainer(rows+[next_bar], 3, Settings())
    assert len(fits) == 1
    assert first["validation"] == second["validation"]
    assert first["score"] != second["score"]
    trainer.state["fitted_monotonic"] -= 601
    trainer(rows+[next_bar], 3, Settings())
    assert len(fits) == 2


@pytest.mark.asyncio
async def test_candle_archive_survives_restart_deduplicates_and_prunes(tmp_path):
    path = str(tmp_path/"history.db")
    db = Database(path)
    await db.init()
    rows = candles(20)
    await db.store_candles("EURUSD", rows[:15], limit=10)
    await db.store_candles("EURUSD", rows[10:], limit=10)
    restarted = Database(path)
    await restarted.init()
    assert await restarted.candle_history("EURUSD") == rows[-10:]
    assert await restarted.candle_history("GBPUSD") == []


@pytest.mark.asyncio
async def test_background_backfill_extends_archive_without_changing_live_quote(tmp_path):
    db = Database(str(tmp_path/"backfill.db")); await db.init()
    rows = candles(2000)
    live, older = rows[500:], rows[:500]
    await db.store_candles("BTCUSDT", live)
    def respond(request):
        assert int(request.url.params["endTime"]) < live[0].time*1000
        return httpx.Response(200, json=[[c.time*1000,c.open,c.high,c.low,c.close] for c in older])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        market = MarketData(Settings(crypto_backfill_candles=2000), client, history=db)
        await market._backfill("BTCUSDT", live[0].time, len(live))
        history = await db.candle_history("BTCUSDT")
        assert len(history) == 2000 and history[-1] == live[-1]
        await market.close()


@pytest.mark.asyncio
async def test_strict_analysis_saves_wait_without_weak_trade(tmp_path):
    db = Database(str(tmp_path/"strict.db")); await db.init()
    service = SignalService(Settings(), None, db)
    service.model_status = "ready"
    async def snapshot(*args, **kwargs):
        now = int(time.time())
        return {"symbol":"EURUSD", "expiry":3, "candle_time":now//60*60,
                "entry_at":now+60, "close_at":now+240, "fresh":True,
                "direction":"CALL", "signal_eligible":False, "reasons":[], "candles":[]}
    service.snapshot = snapshot
    result = await service.create("EURUSD", 3, strict=True)
    assert result["direction"] == "WAIT" and result["raw_direction"] == "CALL"
    assert not await db.upcoming()


@pytest.mark.asyncio
async def test_scheduler_preserves_strict_mode_on_restart(tmp_path):
    from app.scheduler import SignalScheduler
    path = str(tmp_path/"watch.db")
    db = Database(path); await db.init()
    await db.set_watch(True, "EURUSD", 3, strict=True)
    restarted = Database(path); await restarted.init()
    calls = []
    class Service:
        model_status = "ready"
        async def create(self, symbol, expiry, **kwargs):
            calls.append((symbol, expiry, kwargs))
    await SignalScheduler(Settings(), restarted, Service()).tick()
    assert calls == [("EURUSD", 3, {"strict": True})]
