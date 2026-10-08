"""Independent GPT forecasts, bounded concurrency, and conservative consensus."""
import asyncio
import copy
import hashlib
import json
import time

import httpx


SIGNAL_TIMEOUT = 25
CACHE_SECONDS = 60
PROMPT_VERSION = "market-v6"
CONSENSUS_MODELS = ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra")
MODELS = {
    "gpt-6-luna": "GPT-6 Luna · быстрый прогноз",
    "gpt-6.1-sol": "GPT-6.1 Sol · баланс цены и качества",
    "gpt-6-astra": "GPT-6 Astra · подробный анализ",
    "gpt-consensus": "GPT Consensus · согласие трёх моделей",
}

SIGNAL_INSTRUCTIONS = (
    "Ты независимо анализируешь только предоставленные реальные котировки рынка. "
    "Ответ на русском в заданном JSON: CALL (вверх), PUT (вниз) или WAIT (пропустить). "
    "Цель — сравнение цены в close_at с ценой в entry_at, а не с последней свечой. "
    "Вход ещё не состоялся: не называй текущую цену фактической ценой сделки. "
    "Проверь timeframes 1min/5min/15min: тренд, импульс, RSI, ATR, положение цены "
    "относительно support_20/resistance_20 и тела последней закрытой свечи. "
    "Для короткой экспирации основной вес у 1min; старшие интервалы дают контекст. "
    "Сопоставь продолжение и откат: перекупленность сама по себе не означает PUT. "
    "Учитывай возраст каждого таймфрейма и задержку до входа: для далёкого времени "
    "входа текущий краткий импульс не является достаточным основанием. "
    "live_market.quote — отдельный тик, а не закрытая свеча; используй его только "
    "если fresh=true. Не интерпретируй отсутствие свежего тика как изменение цены. "
    "Выбирай CALL/PUT только при понятном преимуществе одного сценария по имеющимся "
    "признакам. При слабых, противоречивых, неполных или устаревших данных выбирай "
    "WAIT и назови конкретную причину. Прогноз направления не обязателен. "
    "Пропуски минут и конфликт таймфреймов учитывай в решении и risks. "
    "Summary: до 35 слов с конкретными наблюдаемыми признаками; risks: максимум "
    "два кратких риска. Не выдумывай объёмы, новости, стакан, цены, точность, "
    "вероятность выигрыша или гарантии. news_available/volume_available=false "
    "означает отсутствие этих данных. Источник внешний; цены Pocket Option "
    "могут отличаться, OTC не поддерживается."
)
REVIEW_INSTRUCTIONS = (
    "Ты аналитический помощник. Ответ на русском, до 250 слов. Разбери только "
    "предоставленный снимок внешнего рынка: тренд, импульс, волатильность, "
    "аргументы за и против текущего ML-прогноза. Укажи актив, источник и время "
    "актуальности. Отмечай отсутствие данных. Не придумывай новости, цены, "
    "точность, вероятность успеха или гарантии. Числовую вероятность можно "
    "только процитировать из probability с её ограничениями. Это дополнительный "
    "разбор, а не новый сигнал. Не назначай новое время входа. При WAIT объясни "
    "причину; не превращай его в CALL/PUT. Цены Pocket Option могут отличаться, "
    "OTC не поддерживается."
)
SIGNAL_FORMAT = {"text": {"format": {"type": "json_schema", "name": "market_signal", "strict": True,
    "schema": {"type": "object", "properties": {
        "direction": {"type": "string", "enum": ["CALL", "PUT", "WAIT"]},
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}}},
        "required": ["direction", "summary", "risks"], "additionalProperties": False}}}}


class GPTError(Exception):
    def __init__(self, code, message, status=503):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class GPTReview:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.slots = asyncio.Semaphore(3)
        self.last_request, self.cache, self.inflight = {}, {}, {}
        self.closed = False

    async def review(self, snapshot, model, user_id, signal=False):
        if model not in MODELS:
            raise GPTError("unsupported_model", "Выберите модель из списка.", 422)
        if not self.settings.openai_api_key:
            raise GPTError("missing_openai_key", "Добавьте OPENAI_API_KEY в Railway Variables и перезапустите сервер.")
        if not snapshot.get("fresh"):
            raise GPTError("stale_market", "Для GPT-разбора нужны свежие рыночные данные.", 409)
        snapshot = copy.deepcopy(snapshot)
        signal = signal or model == "gpt-consensus"
        return await self._shared(snapshot, self._context(snapshot, signal), model, user_id, signal)

    @staticmethod
    def _context(snapshot, signal):
        context = {k: v for k, v in snapshot.items() if k != "candles"}
        context["candles"] = snapshot.get("candles", [])[-60 if signal else -30:]
        if signal:
            for field in ("direction", "raw_direction", "score", "direction_confidence", "probability",
                          "quality", "validation", "reasons", "model", "model_id", "model_ready", "status",
                          "signal_eligible", "training", "constituent_forecasts", "consensus"):
                context.pop(field, None)
            context["forecast_horizon"] = {
                "entry_at": snapshot.get("entry_at"), "close_at": snapshot.get("close_at"),
                "seconds_until_entry": max(0, snapshot.get("entry_at", 0) - int(time.time())),
                "expiry_minutes": snapshot.get("expiry"),
                "target": "Compare market price at close_at with price at entry_at, not with the last candle close."}
        return context

    @staticmethod
    def _key(snapshot, model, user_id, signal):
        # Freeze the forecast for one closed-bar revision and planned trade.
        # Incoming ticks cannot cause duplicate payment for that same request.
        return (user_id, model, signal, snapshot.get("symbol"), snapshot.get("expiry"),
                snapshot.get("candle_time"), snapshot.get("data_as_of"),
                snapshot.get("entry_at"), snapshot.get("close_at"), PROMPT_VERSION)

    async def _shared(self, snapshot, context, model, user_id, signal):
        if self.closed:
            raise GPTError("gpt_closed", "Сервис прогнозов завершает работу. Повторите после перезапуска.")
        key = self._key(snapshot, model, user_id, signal)
        cached = self.cache.get(key) if signal else None
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return copy.deepcopy(cached[1])
        task = self.inflight.get(key)
        if task is None:
            # No await between checking the key and publishing a task.
            now, rate_key = time.monotonic(), (user_id, model)
            if now - self.last_request.get(rate_key, -1000) < 60:
                raise GPTError("gpt_rate_limit", "GPT-разбор доступен раз в минуту для каждого владельца и модели.", 429)
            self.last_request[rate_key] = now
            task = asyncio.create_task(self._run(snapshot, context, model, user_id, signal))
            self.inflight[key] = task
            task.add_done_callback(lambda done: self._completed(key, done, signal))
        # A disconnected client does not discard a paid result or start another call.
        # asyncio.wait never cancels a supplied Task when this consumer is
        # canceled. Unlike shield on Python 3.14, it does not install a logger
        # for already-handled provider errors after the consumer disconnects.
        await asyncio.wait({task})
        return copy.deepcopy(task.result())

    async def close(self):
        """Cancel and drain provider work before closing the HTTPX client."""
        self.closed = True
        tasks = set(self.inflight.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()

    def _completed(self, key, task, signal):
        if self.inflight.get(key) is task:
            self.inflight.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None and signal:
            self.cache[key] = (time.monotonic(), copy.deepcopy(task.result()))
            while len(self.cache) > 256:
                self.cache.pop(next(iter(self.cache)))

    async def _run(self, snapshot, context, model, user_id, signal):
        if model == "gpt-consensus":
            return await self._consensus(snapshot, context, user_id)
        try:
            # The overall request deadline includes queueing for a provider slot.
            async with asyncio.timeout(SIGNAL_TIMEOUT if signal else 75):
                async with self.slots:
                    return await self._provider(snapshot, context, model, signal)
        except (httpx.TimeoutException, TimeoutError):
            raise GPTError("gpt_timeout", "OpenAI не ответил за отведённое время. Выберите GPT-6 Luna для быстрого прогноза или ML Model.") from None
        except httpx.HTTPError:
            raise GPTError("gpt_network", "Не удалось подключиться к OpenAI. Повторите позже.") from None

    async def _consensus(self, snapshot, context, user_id):
        tasks = [asyncio.create_task(self._shared(snapshot, context, model, user_id, True))
                 for model in CONSENSUS_MODELS]
        try:
            _, pending = await asyncio.wait(tasks, timeout=SIGNAL_TIMEOUT)
            constituents = []
            for model, task in zip(CONSENSUS_MODELS, tasks):
                if task in pending:
                    constituents.append(self._failed(model, "gpt_timeout", "Время получения прогноза истекло."))
                    continue
                try:
                    constituents.append(task.result()["constituent_forecasts"][0])
                except GPTError as exc:
                    constituents.append(self._failed(model, exc.code, exc.message))
                except Exception:
                    constituents.append(self._failed(model, "gpt_invalid_signal", "Модель не завершила корректный прогноз."))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        votes = {d: sum(c.get("direction") == d for c in constituents) for d in ("CALL", "PUT", "WAIT")}
        completed = [c for c in constituents if c["status"] == "completed"]
        if len(completed) != len(CONSENSUS_MODELS):
            state, direction = "incomplete", "WAIT"
            failed = ", ".join(c["model_id"] for c in constituents if c["status"] != "completed")
            summary = f"Нет полного консенсуса: прогноз не получен от {failed}."
        elif len({c["input_hash"] for c in completed}) != 1:
            state, direction = "different_snapshots", "WAIT"
            summary = "Модели получили разные снимки рынка. Для сделки нужен общий снимок котировок."
        elif votes["CALL"] == len(CONSENSUS_MODELS) or votes["PUT"] == len(CONSENSUS_MODELS):
            state, direction = "unanimous", "CALL" if votes["CALL"] else "PUT"
            summary = f"Три независимые модели выбрали {direction} для одного времени входа и экспирации. Это согласие, а не измеренная точность."
        else:
            state, direction = "no_consensus", "WAIT"
            decisions = "; ".join(f"{c['model_id']}: {c['direction']}" for c in constituents)
            summary = "Нет единогласного направления: " + decisions + "."
        risks = ["Согласие моделей не подтверждает вероятность выигрыша."]
        first_risk = next((r for c in completed for r in c.get("risks", []) if r.strip()), None)
        if first_risk:
            risks.append(first_risk)
        prediction = {"direction": direction, "summary": summary, "risks": risks}
        result = self._result(snapshot, "gpt-consensus", self._input(context), json.dumps(prediction, ensure_ascii=False))
        result.update(prediction=prediction, constituent_forecasts=constituents,
                      consensus={"state": state, "votes": votes, "required_models": len(CONSENSUS_MODELS),
                                 "completed_models": len(completed), "unanimous": state == "unanimous"})
        return result

    @staticmethod
    def _failed(model, code, message):
        return {"model_id": model, "prompt_version": PROMPT_VERSION, "direction": None,
                "status": "error", "error_code": code, "summary": message, "risks": [],
                "generated_at": int(time.time()), "input_hash": None}

    @staticmethod
    def _input(context):
        return json.dumps(context, ensure_ascii=False, allow_nan=False, sort_keys=True)

    def _result(self, snapshot, model, content, text):
        generated_at = int(time.time())
        return {"text": text, "model": model, "model_id": model, "prompt_version": PROMPT_VERSION,
                "input_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "symbol": snapshot["symbol"], "expiry": snapshot["expiry"],
                "data_as_of": snapshot["data_as_of"], "generated_at": generated_at,
                "stale": generated_at - snapshot["data_as_of"] > snapshot.get("max_data_age_seconds", self.settings.max_data_age_seconds),
                "entry_expired": generated_at >= snapshot.get("entry_at", 0)}

    async def _provider(self, snapshot, context, model, signal):
        content = self._input(context)
        response = await self.client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer " + self.settings.openai_api_key},
            timeout=SIGNAL_TIMEOUT if signal else 75,
            json={"model": model, "store": False, "max_output_tokens": 1200 if signal else 2200,
                  "reasoning": {"effort": "none" if signal and model == "gpt-6-luna" else "low"},
                  **(SIGNAL_FORMAT if signal else {}),
                  "instructions": SIGNAL_INSTRUCTIONS if signal else REVIEW_INSTRUCTIONS, "input": content},
        )
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
        result = self._result(snapshot, model, content, text)
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
            result.update(prediction=prediction, constituent_forecasts=[{
                "model_id": model, "prompt_version": PROMPT_VERSION, "status": "completed",
                **prediction, "generated_at": result["generated_at"], "input_hash": result["input_hash"]}])
        return result
