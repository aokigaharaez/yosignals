"""One shared Twelve Data connection; no API credentials reach the browser."""
import asyncio
import json
import math
import ssl
import time
from urllib.parse import urlencode

from .models import INSTRUMENTS


class LiveQuotes:
    def __init__(self, settings):
        self.settings = settings
        self.active = {}
        self.quotes = {}
        self.status = "connecting" if settings.twelve_data_api_key and settings.twelve_data_stream_enabled else "disabled"

    def touch(self, symbol):
        if symbol in INSTRUMENTS and INSTRUMENTS[symbol].category == "forex":
            self.active[symbol] = time.monotonic()

    def ingest(self, event):
        if event.get("event") != "price":
            return
        symbol = next((key for key, item in INSTRUMENTS.items()
                       if item.category == "forex" and item.provider_symbol == event.get("symbol")), None)
        try:
            stamp, price = int(event["timestamp"]), float(event["price"])
        except (KeyError, ValueError, TypeError, OverflowError):
            return
        if not symbol or not math.isfinite(price) or price <= 0 or stamp > time.time() + 5 or stamp <= 0:
            return
        previous = self.quotes.get(symbol)
        if previous and stamp < previous["time"]:
            return
        self.quotes[symbol] = {"price": price, "time": stamp, "provider": "Twelve Data", "source": "websocket"}

    def view(self, symbol):
        quote = self.quotes.get(symbol)
        if not quote:
            return {"stream_status": self.status, "quote": None}
        age = max(0, int(time.time()) - quote["time"])
        return {"stream_status": self.status, "quote": {**quote, "age_seconds": age, "fresh": age <= 15}}

    async def run(self):
        if self.status == "disabled":
            return
        # Lazy import keeps startup responsive even when the optional feed is unavailable.
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            self.status = "unavailable"
            return
        retry = 5
        while True:
            wanted = {key for key, seen in self.active.items() if time.monotonic() - seen < 120}
            if not wanted:
                self.status = "idle"
                await asyncio.sleep(1)
                continue
            try:
                url = "wss://ws.twelvedata.com/v1/quotes/price?" + urlencode({"apikey": self.settings.twelve_data_api_key})
                self.status = "connecting"
                async with connect(url, ssl=ssl.create_default_context(), open_timeout=10, close_timeout=2,
                                   max_size=65536, ping_interval=20) as socket:
                    subscribed, heartbeat = set(), 0
                    while True:
                        wanted = {key for key, seen in self.active.items() if time.monotonic() - seen < 120}
                        if not wanted:
                            break
                        for action, symbols in (("unsubscribe", subscribed - wanted), ("subscribe", wanted - subscribed)):
                            if symbols:
                                await socket.send(json.dumps({"action": action, "params": {
                                    "symbols": ",".join(INSTRUMENTS[key].provider_symbol for key in sorted(symbols))}}))
                        subscribed = wanted
                        if time.monotonic() - heartbeat >= 10:
                            await socket.send('{"action":"heartbeat"}')
                            heartbeat = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=1)
                        except TimeoutError:
                            continue
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            continue
                        if event.get("event") == "subscribe-status" and (event.get("status") == "error"
                                or event.get("fails") and not event.get("success")):
                            self.status = "unavailable"
                            retry = 300
                            break
                        self.ingest(event)
                        if event.get("event") == "price":
                            self.status, retry = "connected", 5
            except asyncio.CancelledError:
                raise
            except Exception:
                # Provider exceptions may contain the key-bearing connection URL.
                self.status = "unavailable"
                retry = min(300, retry * 2)
            if self.status == "unavailable":
                await asyncio.sleep(retry)
