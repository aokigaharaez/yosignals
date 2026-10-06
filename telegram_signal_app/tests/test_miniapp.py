"""Behavioural checks for on-demand forecasts and Mini App delivery."""
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import aiosqlite
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.analysis import analyze, chronological_split, dataset, probability_estimate
from app.config import Settings
from app.db import Database
from app.market import MarketData
from app.scheduler import SignalScheduler
from app.services import SignalService
from app.web import create_app
from test_core import TOKEN, candles, run_payload, signed


def test_calibration_counts_draws_as_nonwins_and_keeps_bin_boundaries():
    # A draw is False just like a losing direction; unrelated confidence bins are excluded.
    scores = np.array([.60] * 40 + [.59] * 100)
    wins = np.array([True] * 24 + [False] * 16 + [True] * 100)
    p = probability_estimate(.60, scores, wins)
    assert p["method"] == "historical_bin"
    assert p["samples"] == 40 and p["wins"] == 24
    assert p["value"] == round(25 / 42 * 100, 1)
    assert 0 < p["interval"][0] < p["value"] < p["interval"][1] < 100
    for score in (.50, .70, 1.):
        fallback = probability_estimate(score, scores, wins, draw_rate=.1)
        assert fallback["method"] == "uncalibrated_model"
        assert fallback["value"] == round(score * .9 * 100, 1)
        assert fallback["interval"] is None


@pytest.mark.parametrize("horizon,lead", [(1, 1), (3, 2), (15, 2)])
def test_calibration_and_final_test_are_purged_and_independent(horizon, lead):
    data = candles()
    _, indices, targets, _ = dataset(data, horizon, entry_delay=lead)
    train, calibration, test = chronological_split(indices, horizon, lead)
    gap = horizon + lead
    assert indices[train].max() + gap < indices[calibration].min()
    assert indices[calibration].max() + gap < indices[test].min()
    assert np.all(np.diff(indices[calibration]) >= gap)
    assert np.all(np.diff(indices[test]) >= gap)
    for i, label in zip(indices, targets):
        change = data[i + gap].close - data[i + lead + 1].open
        assert label == (-1 if change == 0 else int(change > 0))
    # Changing final-test outcomes cannot change a forecast's calibration estimate.
    cal_scores = np.full(len(calibration), .55)
    before = probability_estimate(.55, cal_scores, targets[calibration] == 1)
    targets[test] = 1 - targets[test]
    assert probability_estimate(.55, cal_scores, targets[calibration] == 1) == before


@pytest.mark.parametrize("expiry", [1, 3, 5, 15])
def test_weak_forecast_remains_visible_for_every_expiry(expiry):
    run = analyze(candles(1500), expiry, Settings(model_min_score=.99, model_min_validation=.99))
    assert run["direction"] in {"CALL", "PUT"}
    assert run["model_ready"] and run["quality"] == "weak"
    assert 0 <= run["probability"]["value"] <= 100
    assert run["validation"]["calibration_samples"] > 0
    json.dumps(run, allow_nan=False)


def test_recent_gap_and_missing_history_still_block_forecasts():
    data = candles()
    data[-3] = replace(data[-3], time=data[-3].time + 1)
    run = analyze(data, 3, Settings())
    assert run["direction"] == "WAIT" and run["probability"] is None


@pytest.mark.asyncio
async def test_late_minute_analysis_moves_entry_and_preserves_both_runs(tmp_path, monkeypatch):
    boundary = 1_800_000_000  # Exact minute boundary.
    clock = [boundary + 5]
    monkeypatch.setattr("app.services.time", SimpleNamespace(time=lambda: clock[0]))
    rows = candles(1000, end=boundary)

    class FreshMarket:
        async def candles(self, symbol):
            return rows

    calls = []

    def analyzer(data, expiry, settings, entry_delay):
        calls.append(entry_delay)
        return analyze(data, expiry, settings, entry_delay)

    db = Database(str(tmp_path / "timing.db"))
    await db.init()
    service = SignalService(Settings(), FreshMarket(), db)
    service.analyzer, service.model_status = analyzer, "ready"
    first = await service.create("EURUSD", 3)
    assert first["entry_at"] == boundary + 60 and first["entry_delay"] == 1
    clock[0] = boundary + 55
    late = await service.create("EURUSD", 3)
    assert late["direction"] in {"CALL", "PUT"}
    assert late["entry_at"] == boundary + 120 and late["entry_delay"] == 2
    assert late["entry_at"] - clock[0] >= 10
    assert first["id"] != late["id"]
    duplicate = await service.create("EURUSD", 3)
    assert duplicate["id"] == late["id"] and calls == [1, 2]
    assert "candles" not in (await db.history())[0]
    assert (await db.history())[0]["probability"] == late["probability"]


@pytest.mark.asyncio
async def test_model_completing_after_entry_recomputes_for_next_minute(tmp_path, monkeypatch):
    boundary = 1_800_000_000
    clock = [boundary + 49]
    monkeypatch.setattr("app.services.time", SimpleNamespace(time=lambda: clock[0]))

    class FreshMarket:
        async def candles(self, symbol):
            return candles(1000, end=boundary)

    calls = []

    def analyzer(data, expiry, settings, entry_delay):
        calls.append(entry_delay)
        clock[0] += 3
        return analyze(data, expiry, settings, entry_delay)

    service = SignalService(Settings(), FreshMarket(), Database(str(tmp_path / "slow.db")))
    service.analyzer, service.model_status = analyzer, "ready"
    run = await service.snapshot("EURUSD", 3)
    assert calls == [1, 2] and run["entry_at"] == boundary + 120
    assert run["direction"] in {"CALL", "PUT"}


@pytest.mark.asyncio
async def test_new_minute_refreshes_provider_before_ttl_expires(monkeypatch):
    clock = [1_800_000_059]
    monkeypatch.setattr("app.market.time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    calls = []

    def respond(request):
        calls.append(request)
        rows = candles(4, end=int(clock[0]) // 60 * 60)
        return httpx.Response(200, json=[[c.time * 1000, c.open, c.high, c.low, c.close] for c in rows])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        market = MarketData(Settings(), client)
        first = await market.candles("BTCUSDT")
        clock[0] += 2
        second = await market.candles("BTCUSDT")
        assert second[-1].time == first[-1].time + 60
        assert await market.candles("BTCUSDT") == second
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_v1_journal_migration_retains_ids_results_and_original_table(tmp_path):
    path = str(tmp_path / "v1.db")
    p = run_payload()
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""CREATE TABLE analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, expiry INTEGER, candle_time INTEGER, direction TEXT,
            entry_at INTEGER, close_at INTEGER, created_at INTEGER, payload TEXT,
            result TEXT, result_source TEXT, UNIQUE(symbol,expiry,candle_time))""")
        await conn.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (7, p["symbol"], p["expiry"], p["candle_time"], p["direction"], p["entry_at"],
                            p["close_at"], 1000, json.dumps(p), "WIN", "user_reported"))
        await conn.commit()
    db = Database(path)
    await db.init()
    new = await db.save({**p, "entry_at": p["entry_at"] + 60})
    await db.init()
    assert new["id"] > 7
    assert len(await db.history()) == 2
    assert (await db.get(7))["result"] == "WIN"
    async with aiosqlite.connect(path) as conn:
        assert (await (await conn.execute("SELECT result FROM analysis_runs WHERE id=7")).fetchone())[0] == "WIN"


@pytest.mark.asyncio
async def test_scanner_saves_in_miniapp_without_telegram_and_stops(tmp_path, monkeypatch):
    clock = [1_800_000_001]
    monkeypatch.setattr("app.scheduler.time", SimpleNamespace(time=lambda: clock[0]))
    db = Database(str(tmp_path / "scan.db"))
    await db.init()
    calls = []

    class Service:
        model_status = "ready"

        async def create(self, symbol, expiry):
            calls.append((symbol, expiry))
            return await db.save({**run_payload(), "entry_at": int(clock[0] // 60) * 60 + 60})

    scheduler = SignalScheduler(Settings(bot_enabled=False), db, Service())
    await db.set_watch(True, "EURUSD", 3)
    await scheduler.tick()
    await scheduler.tick()
    assert len(calls) == 1 and len(await db.history()) == 1
    clock[0] += 60
    await scheduler.tick()
    assert len(calls) == 2 and len(await db.history()) == 2
    await db.set_watch(False, "EURUSD", 3)
    clock[0] += 60
    await scheduler.tick()
    assert len(calls) == 2


def test_manual_analysis_and_autoscan_work_with_bot_polling_disabled(tmp_path, monkeypatch):
    async def fresh_candles(self, symbol):
        return candles(1500)

    monkeypatch.setattr(MarketData, "candles", fresh_candles)
    app = create_app(Settings(bot_token=TOKEN, owner_id=42, bot_enabled=False, db_path=str(tmp_path / "api.db")))
    with TestClient(app) as client:
        app.state.service.analyzer, app.state.service.model_status = analyze, "ready"
        client.headers["Authorization"] = "tma " + signed()
        run_response = client.post("/api/analyses", json={"symbol": "BTCUSDT", "expiry": 3})
        assert run_response.status_code == 200
        run = run_response.json()
        assert run["direction"] in {"CALL", "PUT"} and run["probability"]
        assert len(run["candles"]) == 120
        history = client.get("/api/history").json()["items"]
        assert history[0]["id"] == run["id"] and history[0]["probability"] == run["probability"]
        assert not client.get("/api/session").json()["bot_ready"]
        watch = client.put("/api/watch", json={"enabled": True, "symbol": "BTCUSDT", "expiry": 3})
        assert watch.status_code == 200 and watch.json()["enabled"]
        assert client.put("/api/watch", json={"enabled": False, "symbol": "BTCUSDT", "expiry": 3}).status_code == 200
