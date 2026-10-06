from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Candle:
    time: int  # UTC opening timestamp, seconds
    open: float
    high: float
    low: float
    close: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    label: str
    name: str
    category: str
    provider: str
    provider_symbol: str


INSTRUMENTS = {
    item.symbol: item for item in [
        Instrument("EURUSD", "EUR/USD", "Евро / Доллар", "forex", "Twelve Data", "EUR/USD"),
        Instrument("GBPUSD", "GBP/USD", "Фунт / Доллар", "forex", "Twelve Data", "GBP/USD"),
        Instrument("USDJPY", "USD/JPY", "Доллар / Иена", "forex", "Twelve Data", "USD/JPY"),
        Instrument("AUDUSD", "AUD/USD", "Австралийский доллар", "forex", "Twelve Data", "AUD/USD"),
        Instrument("EURJPY", "EUR/JPY", "Евро / Иена", "forex", "Twelve Data", "EUR/JPY"),
        Instrument("BTCUSDT", "BTC/USDT", "Bitcoin · внешний рынок", "crypto", "Binance", "BTCUSDT"),
        Instrument("ETHUSDT", "ETH/USDT", "Ethereum · внешний рынок", "crypto", "Binance", "ETHUSDT"),
    ]
}
EXPIRIES = (1, 3, 5, 15)

