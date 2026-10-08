import asyncio
import copy
import json
import time

import httpx
import pytest

from app.config import Settings
from app.gpt import CONSENSUS_MODELS, MODELS, PROMPT_VERSION, GPTError, GPTReview


def snapshot():
    boundary = int(time.time()) // 60 * 60
    return {
        "fresh": True, "symbol": "EURUSD", "expiry": 3,
        "provider": "Twelve Data", "candle_time": boundary - 60, "data_as_of": boundary,
        "entry_at": boundary + 120, "close_at": boundary + 300,
        "direction": "PUT", "raw_direction": "PUT", "score": 95,
        "probability": {"value": 95}, "signal_eligible": True,
        "candles": [{"time": boundary - 60 * (60 - i), "open": 1.1, "high": 1.11,
                     "low": 1.09, "close": 1.1} for i in range(60)],
        "market_context": {"timeframes": {"1min": {"available": True},
                                          "5min": {"available": True},
                                          "15min": {"available": True}},
                           "news_available": False, "volume_available": False},
    }


def forecast(direction="CALL"):
    return httpx.Response(200, json={"status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{
            "type": "output_text", "text": json.dumps({
                "direction": direction, "summary": "Наблюдаемый импульс", "risks": ["Волатильность"]})}]}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["CALL", "PUT"])
async def test_consensus_independent_parallel_models_same_snapshot_and_metadata(direction):
    calls, active, peak = [], 0, 0
    async def respond(request):
        nonlocal active, peak
        payload = json.loads(request.content)
        calls.append(payload)
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(.01)
        active -= 1
        return forecast(direction)
    data = snapshot()
    original = copy.deepcopy(data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        result = await review.review(data, "gpt-consensus", 42, signal=True)
    assert data == original
    assert peak == 3 and len(calls) == 3
    assert {p["model"] for p in calls} == set(CONSENSUS_MODELS)
    assert len({p["input"] for p in calls}) == 1
    context = json.loads(calls[0]["input"])
    assert all(field not in context for field in ["direction", "raw_direction", "score", "probability", "signal_eligible"])
    assert context["market_context"]["news_available"] is False
    assert context["forecast_horizon"]["entry_at"] == data["entry_at"]
    assert "При слабых" in calls[0]["instructions"] and "выбирай WAIT" in calls[0]["instructions"]
    assert result["prediction"]["direction"] == direction
    assert result["model_id"] == "gpt-consensus" and result["prompt_version"] == PROMPT_VERSION
    assert result["consensus"]["unanimous"] and result["consensus"]["votes"][direction] == 3
    constituents = result["constituent_forecasts"]
    assert {c["model_id"] for c in constituents} == set(CONSENSUS_MODELS)
    assert all(c["prompt_version"] == PROMPT_VERSION and c["status"] == "completed" for c in constituents)
    assert len({c["input_hash"] for c in constituents}) == 1
    assert "probability" not in result and "accuracy" not in result["consensus"]
    assert "gpt-consensus" in MODELS


@pytest.mark.asyncio
@pytest.mark.parametrize("directions", [
    ("CALL", "CALL", "PUT"), ("CALL", "CALL", "WAIT"), ("WAIT", "WAIT", "WAIT")])
async def test_consensus_disagreement_or_wait_never_becomes_direction(directions):
    choices = dict(zip(CONSENSUS_MODELS, directions))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: forecast(choices[json.loads(r.content)["model"]]))) as client:
        result = await GPTReview(Settings(openai_api_key="secret"), client).review(
            snapshot(), "gpt-consensus", 42, signal=True)
    assert result["prediction"]["direction"] == "WAIT" and not result["consensus"]["unanimous"]
    assert all(model in result["prediction"]["summary"] for model in CONSENSUS_MODELS)
    assert result["consensus"]["state"] == "no_consensus"


@pytest.mark.asyncio
async def test_consensus_unavailable_model_is_wait_and_does_not_leak_provider_details():
    def respond(request):
        if json.loads(request.content)["model"] == "gpt-6-astra":
            return httpx.Response(404, json={"error": "secret-provider-error"})
        return forecast()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GPTReview(Settings(openai_api_key="secret"), client).review(
            snapshot(), "gpt-consensus", 42, signal=True)
    assert result["prediction"]["direction"] == "WAIT"
    assert result["consensus"]["completed_models"] == 2
    assert result["constituent_forecasts"][2]["status"] == "error"
    assert result["constituent_forecasts"][2]["error_code"] == "gpt_provider_error"
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_consensus_inflight_and_cache_dedup_only_three_provider_calls():
    calls, started, release = [], asyncio.Event(), asyncio.Event()
    async def respond(request):
        calls.append(request)
        if len(calls) == 3:
            started.set()
        await release.wait()
        return forecast()
    data = snapshot()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        first = asyncio.create_task(review.review(data, "gpt-consensus", 42, signal=True))
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(review.review(data, "gpt-consensus", 42, signal=True))
        await asyncio.sleep(0)
        release.set()
        a, b = await asyncio.gather(first, second)
        cached = await review.review(data, "gpt-consensus", 42, signal=True)
        b["prediction"]["direction"] = "PUT"
        assert a["prediction"]["direction"] == cached["prediction"]["direction"] == "CALL"
        assert len(calls) == 3 and not review.inflight
        with pytest.raises(GPTError) as error:
            await review.review({**data, "entry_at": data["entry_at"] + 60},
                                "gpt-consensus", 42, signal=True)
        assert error.value.code == "gpt_rate_limit"


@pytest.mark.asyncio
async def test_single_model_has_per_owner_model_limits_and_constituent_metadata():
    calls = []
    def respond(request):
        calls.append(request)
        return forecast("WAIT")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        data = snapshot()
        first = await review.review(data, "gpt-6-luna", 42, signal=True)
        await review.review(data, "gpt-6-astra", 42, signal=True)
        await review.review(data, "gpt-6-luna", 43, signal=True)
        assert len(calls) == 3
        assert first["constituent_forecasts"][0]["direction"] == "WAIT"
        assert first["model_id"] == "gpt-6-luna" and first["prompt_version"] == PROMPT_VERSION
        with pytest.raises(GPTError) as error:
            await review.review({**data, "expiry": 5}, "gpt-6-luna", 42, signal=True)
        assert error.value.code == "gpt_rate_limit"


@pytest.mark.asyncio
async def test_consensus_deadline_keeps_completed_forecasts_and_wait(monkeypatch):
    monkeypatch.setattr("app.gpt.SIGNAL_TIMEOUT", .1)
    async def respond(request):
        if json.loads(request.content)["model"] == "gpt-6-astra":
            await asyncio.sleep(3)
        return forecast()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        started = time.monotonic()
        result = await review.review(snapshot(), "gpt-consensus", 42, signal=True)
        assert time.monotonic() - started < 1
        assert result["prediction"]["direction"] == "WAIT"
        assert result["consensus"]["completed_models"] == 2
        assert result["constituent_forecasts"][2]["error_code"] == "gpt_timeout"
        # Shutdown is an explicit lifecycle operation, not a guessed delay
        # for producer done callbacks under concurrent training CPU load.
        await review.close()
        assert not review.inflight and review.slots._value == 3


@pytest.mark.asyncio
async def test_canceled_client_does_not_duplicate_inflight_paid_request():
    calls, started, release = [], asyncio.Event(), asyncio.Event()
    async def respond(request):
        calls.append(request)
        started.set()
        await release.wait()
        return forecast()
    data = snapshot()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        first = asyncio.create_task(review.review(data, "gpt-6-luna", 42, signal=True))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = asyncio.create_task(review.review(data, "gpt-6-luna", 42, signal=True))
        release.set()
        assert (await retry)["prediction"]["direction"] == "CALL"
        assert len(calls) == 1 and not review.inflight


@pytest.mark.asyncio
async def test_cached_model_with_different_market_input_cannot_form_consensus():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: forecast())) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        data = snapshot()
        await review.review(data, "gpt-6-luna", 42, signal=True)
        changed = copy.deepcopy(data)
        changed["live_market"] = {"quote": {"price": 1.2, "fresh": True}}
        result = await review.review(changed, "gpt-consensus", 42, signal=True)
    assert result["prediction"]["direction"] == "WAIT"
    assert result["consensus"]["state"] == "different_snapshots"



@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_all_consensus_provider_work():
    started, all_started = [], asyncio.Event()
    canceled = []
    async def respond(request):
        model = json.loads(request.content)["model"]
        started.append(model)
        if len(started) == 3:
            all_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceled.append(model)
            raise
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        review = GPTReview(Settings(openai_api_key="secret"), client)
        request = asyncio.create_task(review.review(snapshot(), "gpt-consensus", 42, signal=True))
        await asyncio.wait_for(all_started.wait(), 1)
        await review.close()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert set(canceled) == set(CONSENSUS_MODELS)
        assert not review.inflight and review.slots._value == 3
        await review.close()
        with pytest.raises(GPTError) as error:
            await review.review(snapshot(), "gpt-6-luna", 42, signal=True)
        assert error.value.code == "gpt_closed"


@pytest.mark.asyncio
async def test_disconnected_client_provider_failure_is_consumed_without_loop_warning():
    started, release = asyncio.Event(), asyncio.Event()
    warnings = []
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda current_loop, context: warnings.append(context))
    async def respond(request):
        started.set()
        await release.wait()
        return httpx.Response(500)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            review = GPTReview(Settings(openai_api_key="secret"), client)
            request = asyncio.create_task(review.review(snapshot(), "gpt-6-luna", 42, signal=True))
            await started.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            providers = list(review.inflight.values())
            release.set()
            terminal = await asyncio.gather(*providers, return_exceptions=True)
            assert len(terminal) == 1 and isinstance(terminal[0], GPTError)
            await review.close()
            await asyncio.sleep(0)
            assert not review.inflight and review.slots._value == 3
            assert warnings == []
    finally:
        loop.set_exception_handler(original_handler)
