from __future__ import annotations

import asyncio
import copy
import importlib
import logging
import time
from .config import Settings
from .db import Database
from .market import MarketData, MarketError
from .models import INSTRUMENTS, EXPIRIES


class SignalService:
    def __init__(self, settings: Settings, market: MarketData, db: Database):
        self.settings, self.market, self.db = settings, market, db
        self.cache: dict[tuple[str, int], tuple[int, dict]] = {}
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

    async def snapshot(self, symbol: str, expiry: int) -> dict:
        if expiry not in EXPIRIES:
            raise MarketError("unsupported_expiry", "Выберите экспирацию 1, 3, 5 или 15 минут.")
        candles = await self.market.candles(symbol)
        last = candles[-1]
        now = int(time.time())
        age = max(0, now - last.time - 60)
        fresh = age <= self.settings.max_data_age_seconds
        key = symbol, expiry
        async with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] == last.time:
                result = copy.deepcopy(cached[1])
            elif fresh and self.analyzer is not None:
                result = await asyncio.to_thread(self.analyzer, candles, expiry, self.settings)
                self.cache[key] = (last.time, copy.deepcopy(result))
            else:
                result = {"direction": "WAIT", "score": None, "model": "Logistic regression · v1",
                          "validation": None, "indicators": {}, "reasons": [
                              "Модель загружается. График доступен; анализ появится после подготовки библиотек."
                              if self.model_status == "loading" else "Библиотеки модели недоступны. Проверьте зависимости сервера."
                          ], "model_ready": False}
        # Recheck time after potentially expensive training, even for cached models.
        now = int(time.time())
        age = max(0, now - last.time - 60)
        fresh = age <= self.settings.max_data_age_seconds
        entry = last.time + 120
        if not fresh:
            result.update(direction="WAIT", reasons=["Котировки устарели или рынок закрыт. Дождитесь свежих данных."])
        elif entry - now < 10:
            result.update(direction="WAIT", reasons=["Окно входа заканчивается. Дождитесь следующей закрытой свечи."])
        result.update(symbol=symbol, label=INSTRUMENTS[symbol].label, provider=INSTRUMENTS[symbol].provider,
                      category=INSTRUMENTS[symbol].category, expiry=expiry, candle_time=last.time,
                      data_as_of=last.time + 60, data_age_seconds=age, fresh=fresh, price=last.close,
                      entry_at=entry, close_at=entry + expiry * 60, server_time=now,
                      change_percent=round((last.close / candles[max(0, len(candles)-61)].close - 1) * 100, 3),
                      candles=[c.to_dict() for c in candles[-120:]], sample_count=len(candles),
                      price_note="Внешние котировки. Сверьте актив и цену в Pocket Option; OTC не поддерживается.",
                      score_note="Оценка модели не является подтверждённой вероятностью выигрыша.")
        return result

    async def create(self, symbol: str, expiry: int) -> dict:
        result = await self.snapshot(symbol, expiry)
        if not result["fresh"]:
            raise MarketError("stale_data", result["reasons"][0])
        if self.model_status != "ready":
            raise MarketError("model_loading" if self.model_status == "loading" else "model_error", result["reasons"][0])
        # Do not retain 120 chart candles in every journal entry.
        return await self.db.save({k: v for k, v in result.items() if k != "candles"})

