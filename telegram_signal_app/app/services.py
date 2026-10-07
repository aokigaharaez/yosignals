from __future__ import annotations

import asyncio
import copy
import importlib
import logging
import math
import time
from contextlib import nullcontext
from .config import Settings
from .db import Database
from .market import MarketData, MarketError
from .models import INSTRUMENTS, EXPIRIES
from .timing import plan_entry, validate_entry
from .technical import indicators


class SignalService:
    def __init__(self, settings: Settings, market: MarketData, db: Database):
        self.settings, self.market, self.db = settings, market, db
        self.cache: dict[tuple[str, int, int], tuple[int, dict]] = {}
        self.lock = asyncio.Lock()
        self.analyzer = None
        self.model_status = "loading"

    async def prepare_model(self):
        try:
            module = await asyncio.to_thread(importlib.import_module, "app.analysis")
            self.analyzer = module.analyze
            self.model_status = "ready"
            logging.getLogger(__name__).info("ML model libraries ready")
        except Exception:
            self.model_status = "error"
            logging.getLogger(__name__).error("Cannot load ML libraries. Install requirements.txt and restart.")

    async def snapshot(self, symbol: str, expiry: int, ml: bool = True, entry_at: int | None = None) -> dict:
        if type(expiry) is not int or not 1 <= expiry <= 60:
            raise MarketError("unsupported_expiry", "Выберите экспирацию от 1 до 60 минут.")
        validate_entry(entry_at, now=int(time.time()))
        candles = await self.market.candles(symbol)
        last = candles[-1]
        data_as_of = last.time + 60
        freshness_limit = (self.settings.forex_max_data_age_seconds if INSTRUMENTS[symbol].category == "forex"
                           else self.settings.max_data_age_seconds)
        result = {"direction": "WAIT", "score": None, "probability": None, "quality": "unavailable",
                  "model": "Logistic regression · v2", "validation": None, "indicators": {},
                  "reasons": ["Модель загружается. Анализ появится после подготовки библиотек."
                              if self.model_status == "loading" else "Библиотеки модели недоступны. Проверьте зависимости сервера."],
                  "model_ready": False}
        if not ml:
            result.update(engine="gpt", model="GPT", indicators=indicators(candles),
                          reasons=["GPT-прогноз ещё не запрошен. Нажмите «Получить сигнал»."], forecast_pending=True)
        async with (self.lock if ml else nullcontext()):
            for _ in range(3):
                now = int(time.time())
                entry, lead = plan_entry(data_as_of, entry_at, now=now)
                if not ml or now - data_as_of > freshness_limit or self.analyzer is None:
                    break
                key = symbol, expiry, lead
                cached = self.cache.get(key)
                if cached and cached[0] == last.time:
                    result = copy.deepcopy(cached[1])
                else:
                    result = await asyncio.to_thread(self.analyzer, candles, expiry, self.settings, entry_delay=lead)
                    self.cache[key] = (last.time, copy.deepcopy(result))
                    if len(self.cache) > 256:
                        self.cache.pop(next(iter(self.cache)))
                if entry - int(time.time()) >= 10:
                    break
        # Recheck time after potentially expensive training, even for cached models.
        now = int(time.time())
        age = max(0, now - last.time - 60)
        fresh = age <= freshness_limit
        if not fresh:
            result.update(direction="WAIT", probability=None, quality="unavailable", reasons=["Котировки устарели или рынок закрыт. Для круглосуточного анализа доступны BTC/USDT и ETH/USDT, если их источник отвечает."])
        elif entry - now < 10:
            result.update(direction="WAIT", probability=None, quality="unavailable", reasons=["Расчёт задержался. Повторите анализ для нового времени входа."])
        result.update(symbol=symbol, label=INSTRUMENTS[symbol].label, provider=INSTRUMENTS[symbol].provider,
                      category=INSTRUMENTS[symbol].category, expiry=expiry, candle_time=last.time,
                      data_as_of=last.time + 60, data_age_seconds=age, fresh=fresh, price=last.close,
                      max_data_age_seconds=freshness_limit, delayed=age > 90,
                      entry_at=entry, entry_delay=lead, close_at=entry + expiry * 60, server_time=now,
                      status="scheduled" if result["direction"] != "WAIT" else ("loading" if fresh and self.model_status == "loading" else "unavailable"),
                      change_percent=round((last.close / candles[max(0, len(candles)-61)].close - 1) * 100, 3),
                      candles=[c.to_dict() for c in candles[-120:]], sample_count=len(candles),
                      price_note="Внешние котировки. Сверьте актив и цену в Pocket Option; OTC не поддерживается.",
                      score_note="Оценка успеха направления на внешнем рынке. Вероятность выигрыша в Pocket Option не проверена.")
        return result

    async def create_gpt(self, symbol, expiry, model, user_id, gpt, entry_at=None):
        validate_entry(entry_at, now=int(time.time()))
        snapshot = await self.snapshot(symbol, expiry, ml=False, **({"entry_at": entry_at} if entry_at is not None else {}))
        # Reserve time for the API before the scheduled entry; never shift a completed forecast.
        snapshot["entry_at"], _ = plan_entry(snapshot["data_as_of"], entry_at, reserve=90, now=int(time.time()))
        snapshot["close_at"] = snapshot["entry_at"] + expiry * 60
        snapshot["entry_delay"] = (snapshot["entry_at"] - snapshot["data_as_of"]) // 60
        review = await gpt.review(snapshot, model, user_id, signal=True)
        now = int(time.time())
        prediction = review["prediction"]
        snapshot.update(engine="gpt", model=model, model_ready=True, direction=prediction["direction"],
                        forecast_pending=False, raw_direction=prediction["direction"],
                        forecast_state="model_wait" if prediction["direction"] == "WAIT" else "ready",
                        reasons=[prediction["summary"], *prediction["risks"]],
                        probability=None, score=None, validation=None, quality="unvalidated",
                        server_time=now, status="scheduled" if prediction["direction"] != "WAIT" else "unavailable",
                        score_note="Для GPT-прогноза вероятность выигрыша не откалибрована.")
        snapshot["data_age_seconds"] = max(0, now - snapshot["data_as_of"])
        snapshot["fresh"] = snapshot["data_age_seconds"] <= snapshot.get("max_data_age_seconds", self.settings.max_data_age_seconds)
        snapshot["delayed"] = snapshot["data_age_seconds"] > 90
        if not snapshot["fresh"] or snapshot["entry_at"] - now < 10:
            snapshot.update(direction="WAIT", status="unavailable", quality="unavailable",
                            forecast_state="stale_data" if not snapshot["fresh"] else "expired_entry",
                            reasons=["GPT завершил расчёт после безопасного времени входа или данные устарели. Запросите новый анализ.", *snapshot["reasons"]])
        if snapshot["delayed"]:
            snapshot["reasons"].append(f"Свечи источника задержаны на {snapshot['data_age_seconds']} сек; прогноз предварительный.")
        saved = await self.db.save({k: v for k, v in snapshot.items() if k != "candles"})
        return {**saved, "candles": snapshot["candles"], "server_time": now}

    async def create(self, symbol: str, expiry: int, entry_at: int | None = None) -> dict:
        result = await self.snapshot(symbol, expiry, **({"entry_at": entry_at} if entry_at is not None else {}))
        if not result["fresh"]:
            raise MarketError("stale_data", result["reasons"][0])
        if self.model_status != "ready":
            raise MarketError("model_loading" if self.model_status == "loading" else "model_error", result["reasons"][0])
        # Do not retain 120 chart candles in every journal entry.
        saved = await self.db.save({k: v for k, v in result.items() if k != "candles"})
        return {**saved, "candles": result["candles"], "server_time": int(time.time())}

