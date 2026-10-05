from __future__ import annotations

import asyncio
import logging
import time
from aiogram import Bot
from .config import Settings
from .db import Database
from .market import MarketError
from .services import SignalService
from .ui import format_signal, result_keyboard

log = logging.getLogger(__name__)


class SignalScheduler:
    def __init__(self, settings: Settings, db: Database, service: SignalService, bot: Bot):
        self.settings, self.db, self.service, self.bot = settings, db, service, bot
        self.last_scan = 0.
        self.scan_error: str | None = None

    async def send_once(self, run: dict, event: str, text: str, keyboard=None):
        if not await self.db.claim_event(run["id"], event):
            return
        try:
            await self.bot.send_message(self.settings.owner_id, text, reply_markup=keyboard)
        except Exception:
            await self.db.release_event(run["id"], event)
            # Exception URLs may contain bot tokens; never log raw exceptions.
            log.warning("Telegram notification failed; will retry while relevant")

    async def tick(self):
        watch = await self.db.watch()
        if watch["enabled"] and time.monotonic() - self.last_scan >= 60:
            self.last_scan = time.monotonic()
            try:
                run = await self.service.create(watch["symbol"], watch["expiry"])
                self.scan_error = None
                if run["direction"] != "WAIT" and run["entry_at"] - time.time() >= 10:
                    await self.send_once(run, "signal", format_signal(run, self.settings.timezone))
            except MarketError as exc:
                self.scan_error = exc.message
        now = time.time()
        for run in await self.db.upcoming():
            until_entry = run["entry_at"] - now
            if 0 <= until_entry <= 30:
                kind = "t10" if until_entry <= 10 else "t30"
                await self.send_once(run, kind, f'⏳ {run["label"]} · {run["direction"]}\nДо входа около {int(until_entry)} сек. Сверьте цену в Pocket Option.')
            elif -5 <= until_entry < 0:
                await self.send_once(run, "entry", f'▶️ Время входа · {run["label"]} · {run["direction"]}\nЭкспирация {run["expiry"]} мин. При опоздании пропустите сигнал.')
            if 0 <= now - run["close_at"] <= 60:
                await self.send_once(run, "close", f'🏁 {run["label"]} · время закрытия\nОтметьте фактический результат в Pocket Option.', result_keyboard(run["id"]))

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("Background scan failed; retrying on next tick")
            await asyncio.sleep(5)

