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
