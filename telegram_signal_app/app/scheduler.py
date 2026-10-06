"""Background analysis writes to the Mini App journal; it never sends chat messages."""
import asyncio
import logging
import time
from .market import MarketError

log = logging.getLogger(__name__)


class SignalScheduler:
    def __init__(self, settings, db, service):
        self.settings, self.db, self.service = settings, db, service
        self.last_bucket = None
        self.scan_error = None

    async def tick(self):
        watch = await self.db.watch()
        # A setting change is applied without waiting for the next minute.
        bucket = (int(time.time()) // 60, watch["symbol"], watch["expiry"])
        if not watch["enabled"]:
            self.last_bucket = None
            self.scan_error = None
            return
        if bucket == self.last_bucket or self.service.model_status == "loading":
            return
        self.last_bucket = bucket
        try:
            await self.service.create(watch["symbol"], watch["expiry"])
            self.scan_error = None
        except MarketError as exc:
            self.scan_error = exc.message

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                self.scan_error = "Автоанализ временно недоступен. Следующая попытка — через минуту."
                log.warning("Background analysis failed; retrying next minute")
            await asyncio.sleep(3)
