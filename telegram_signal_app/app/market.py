from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
import httpx
from .config import Settings
from .models import Candle, INSTRUMENTS


class MarketError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class MarketData:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        self.cache: dict[str, tuple[float, list[Candle]]] = {}
        self.cache_minute: dict[str, int] = {}
        self.errors: dict[str, tuple[float, MarketError]] = {}
        self.locks = {symbol: asyncio.Lock() for symbol in INSTRUMENTS}

    async def candles(self, symbol: str) -> list[Candle]:
        if symbol not in INSTRUMENTS:
            raise MarketError("unsupported", "Актив не поддерживается. OTC требует отдельного источника Pocket Option.")
        async with self.locks[symbol]:
            cached = self.cache.get(symbol)
            # Refresh on a new UTC minute instead of keeping the previous bar
            # for an arbitrary 60-second phase after the last user request.
            if (cached and self.cache_minute.get(symbol) == int(time.time()) // 60
                    and time.monotonic() - cached[0] < self.settings.market_cache_seconds):
                return cached[1]
            error = self.errors.get(symbol)
            if error and time.monotonic() - error[0] < 60:
                raise error[1]
            requested_minute = int(time.time()) // 60
            try:
                rows = self.validate(await self._fetch(symbol))
            except MarketError as exc:
                self.errors[symbol] = (time.monotonic(), exc)
                raise
            self.cache[symbol] = (time.monotonic(), rows)
            self.cache_minute[symbol] = requested_minute
            self.errors.pop(symbol, None)
            return rows

    async def _fetch(self, symbol: str) -> list[Candle]:
        instrument = INSTRUMENTS[symbol]
        try:
            if instrument.category == "crypto":
                response = await self.client.get("https://data-api.binance.vision/api/v3/klines", params={
                    "symbol": instrument.provider_symbol, "interval": "1m", "limit": 1000,
                })
                self._check_http(response)
                payload = response.json()
                if len(payload) == 1000:
                    older = await self.client.get("https://data-api.binance.vision/api/v3/klines", params={
                        "symbol": instrument.provider_symbol, "interval": "1m", "limit": 500,
                        "endTime": int(payload[0][0]) - 1,
                    })
                    self._check_http(older)
                    payload = older.json() + payload
                return [Candle(int(row[0]) // 1000, *map(float, row[1:5])) for row in payload]
            if not self.settings.twelve_data_api_key:
                raise MarketError("missing_key", "Для валютных пар добавьте TWELVE_DATA_API_KEY в Railway Variables (локально — в .env) и перезапустите приложение.")
            response = await self.client.get("https://api.twelvedata.com/time_series", params={
                "symbol": instrument.provider_symbol, "interval": "1min", "outputsize": 1500,
                "timezone": "UTC", "order": "ASC", "apikey": self.settings.twelve_data_api_key,
            })
            self._check_http(response)
            payload = response.json()
            if payload.get("status") == "error":
                code = str(payload.get("code"))
                if code == "429":
                    raise MarketError("rate_limit", "Лимит Twelve Data исчерпан. Дождитесь обновления квоты.")
                if code in {"401", "403"}:
                    raise MarketError("provider_access", "Проверьте ключ Twelve Data и доступ к минутным данным в тарифе.")
                raise MarketError("provider_error", "Twelve Data не предоставил свечи для этого актива.")
            return [Candle(
                int(datetime.fromisoformat(row["datetime"]).replace(tzinfo=timezone.utc).timestamp()),
                *(float(row[key]) for key in ("open", "high", "low", "close")),
            ) for row in payload["values"]]
        except httpx.RequestError:
            raise MarketError("network", "Источник котировок не отвечает. Повторите попытку позже.") from None
        except (KeyError, ValueError, TypeError, IndexError):
            raise MarketError("invalid_data", "Источник вернул некорректные рыночные данные.") from None

    @staticmethod
    def _check_http(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise MarketError("rate_limit", "Лимит источника котировок исчерпан. Повторите позже.")
        if response.status_code != 200:
            raise MarketError("provider_error", f"Источник котировок недоступен (HTTP {response.status_code}).")

    @staticmethod
    def validate(rows: list[Candle], now: float | None = None) -> list[Candle]:
        now = time.time() if now is None else now
        closed = sorted((c for c in rows if c.time + 60 <= now), key=lambda c: c.time)
        if not closed:
            raise MarketError("no_data", "Нет закрытых минутных свечей.")
        previous = None
        for c in closed:
            values = (c.open, c.high, c.low, c.close)
            if (not all(math.isfinite(v) and v > 0 for v in values)
                    or c.low > min(c.open, c.close) or c.high < max(c.open, c.close)
                    or c.low > c.high or c.time % 60 or c.time == previous):
                raise MarketError("invalid_data", "Проверка целостности свечей не пройдена.")
            previous = c.time
        return closed
