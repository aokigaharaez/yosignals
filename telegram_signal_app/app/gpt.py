"""Optional, explicitly requested GPT reviews of server-generated market snapshots."""
import asyncio
import copy
import json
import time

import httpx


SIGNAL_TIMEOUT = 25

MODELS = {
    "gpt-6-luna": "GPT-6 Luna · быстрый прогноз",
    "gpt-6.1-sol": "GPT-6.1 Sol · баланс цены и качества",
    "gpt-6-astra": "GPT-6 Astra · подробный анализ",
}


class GPTError(Exception):
    def __init__(self, code, message, status=503):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class GPTReview:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.lock = asyncio.Lock()
        self.last_request = {}
        self.cache = {}

    async def review(self, snapshot, model, user_id, signal=False):
        if model not in MODELS:
            raise GPTError("unsupported_model", "Выберите модель из списка.", 422)
        if not self.settings.openai_api_key:
            raise GPTError("missing_openai_key", "Добавьте OPENAI_API_KEY в Railway Variables и перезапустите сервер.")
        if not snapshot.get("fresh"):
            raise GPTError("stale_market", "Для GPT-разбора нужны свежие рыночные данные.", 409)
        cache_key = (user_id, model, snapshot.get("symbol"), snapshot.get("expiry"),
                     snapshot.get("candle_time"), snapshot.get("entry_at"))
        cached = self.cache.get(cache_key) if signal else None
        if cached and time.monotonic() - cached[0] < 45:
            return copy.deepcopy(cached[1])
        if self.lock.locked():
            raise GPTError("gpt_busy", "Другой GPT-разбор уже выполняется. Повторите позже.", 429)
        async with self.lock:
            now = time.monotonic()
            if now - self.last_request.get(user_id, -1000) < 60:
                raise GPTError("gpt_rate_limit", "GPT-разбор доступен раз в минуту для каждого владельца.", 429)
            self.last_request[user_id] = now
            context = {k: v for k, v in snapshot.items() if k not in {"candles"}}
            context["candles"] = snapshot.get("candles", [])[-30:]
            extra = {}
            if signal:
                for field in ("direction", "score", "probability", "quality", "validation", "reasons", "model", "model_ready", "status"):
                    context.pop(field, None)
                extra = {"text": {"format": {"type": "json_schema", "name": "market_signal", "strict": True,
                    "schema": {"type": "object", "properties": {
                        "direction": {"type": "string", "enum": ["CALL", "PUT", "WAIT"]},
                        "summary": {"type": "string"},
                        "risks": {"type": "array", "items": {"type": "string"}}},
                        "required": ["direction", "summary", "risks"], "additionalProperties": False}}}}
            signal_instructions = (
                "Ты анализируешь рынок самостоятельно по предоставленным свечам и индикаторам. "
                "Ответ на русском в заданном JSON. Выбери направление CALL (вверх), PUT (вниз) "
                "или WAIT (пропустить) для заданной экспирации и серверного времени входа. "
                "На пригодных данных выбирай наиболее вероятное направление CALL или PUT. "
                "При слабых или противоречивых признаках отмечай низкую надёжность в risks. "
                "WAIT используй при непригодных данных, без оснований для направления. "
                "Summary: одна короткая фраза до 15 слов; risks: максимум два кратких риска. "
                "Не выдумывай новости, цены, точность или вероятность выигрыша. "
                "Источник внешний, цены Pocket Option могут отличаться; OTC не поддерживается."
            )
            try:
                async with asyncio.timeout(SIGNAL_TIMEOUT if signal else 75):
                    response = await self.client.post(
                        "https://api.openai.com/v1/responses",
                        headers={"Authorization": "Bearer " + self.settings.openai_api_key},
                        timeout=SIGNAL_TIMEOUT if signal else 75,
                        json={"model": model, "store": False, "max_output_tokens": (1200 if signal else 2200),
                              "reasoning": {"effort": "none" if signal and model == "gpt-6-luna" else "low"},
                              **extra,
                              "instructions": signal_instructions if signal else (
                                  "Ты аналитический помощник. Ответ на русском, до 250 слов. "
                                  "Разбери только предоставленный сервером снимок внешнего рынка: тренд, "
                                  "импульс, волатильность, аргументы за и против текущего ML-прогноза. "
                                  "Укажи актив, источник и время актуальности. Отмечай отсутствие данных. "
                                  "Не придумывай новости, цены, точность, вероятность успеха или гарантии. "
                                  "Числовую вероятность можно только процитировать из probability с её ограничениями. "
                                  "Это дополнительный разбор, а не новый сигнал. Не назначай новое время входа. "
                                  "При WAIT объясни причину; не превращай его в CALL/PUT. "
                                  "Укажи, что цены Pocket Option могут отличаться, OTC не поддерживается."),
                              "input": json.dumps(context, ensure_ascii=False, allow_nan=False)},
                    )
            except (httpx.TimeoutException, TimeoutError):
                raise GPTError("gpt_timeout", "OpenAI не ответил за отведённое время. Выберите GPT-6 Luna для быстрого прогноза или ML Model.") from None
            except httpx.HTTPError:
                raise GPTError("gpt_network", "Не удалось подключиться к OpenAI. Повторите позже.") from None
            if response.status_code != 200:
                messages = {
                    401: "OpenAI отклонил API-ключ. Проверьте OPENAI_API_KEY в Railway.",
                    403: "Нет доступа к OpenAI или выбранной модели для этого проекта.",
                    404: "Выбранная модель недоступна для API-ключа. Попробуйте другую.",
                    429: "Лимит OpenAI или баланс API исчерпан. Проверьте Billing и Limits.",
                    400: "OpenAI отклонил запрос к выбранной модели. Попробуйте другую модель.",
                }
                raise GPTError("gpt_provider_error", messages.get(response.status_code, "Ошибка OpenAI. Повторите позже."))
            try:
                data = response.json()
                text = "\n".join(part["text"] for item in data.get("output", [])
                                 if item.get("type") == "message" and item.get("role") == "assistant"
                                 for part in item.get("content", []) if part.get("type") == "output_text").strip()
                if data.get("status") != "completed" or not text:
                    raise ValueError("Incomplete response")
            except (ValueError, TypeError, KeyError, AttributeError):
                raise GPTError("gpt_empty", "OpenAI не завершил разбор. Попробуйте другую модель или повторите позже.") from None
            generated_at = int(time.time())
            result = {"text": text, "model": model, "symbol": snapshot["symbol"],
                    "expiry": snapshot["expiry"], "data_as_of": snapshot["data_as_of"],
                    "generated_at": generated_at,
                    "stale": generated_at - snapshot["data_as_of"] > snapshot.get("max_data_age_seconds", self.settings.max_data_age_seconds),
                    "entry_expired": generated_at >= snapshot.get("entry_at", 0)}
            if signal:
                try:
                    prediction = json.loads(text)
                    if (prediction["direction"] not in {"CALL", "PUT", "WAIT"}
                            or not isinstance(prediction["summary"], str) or not prediction["summary"].strip()
                            or not isinstance(prediction["risks"], list)
                            or not all(isinstance(r, str) for r in prediction["risks"])):
                        raise ValueError("Invalid prediction")
                except (ValueError, KeyError, TypeError):
                    raise GPTError("gpt_invalid_signal", "GPT вернул некорректный прогноз. Повторите позже.") from None
                result["prediction"] = prediction
                self.cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
                if len(self.cache) > 128:
                    self.cache.pop(next(iter(self.cache)))
            return result
