from __future__ import annotations

import asyncio
import importlib
import logging
import ssl
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .auth import AuthError, validate_init_data
from .config import Settings
from .db import Database
from .market import MarketData, MarketError
from .models import INSTRUMENTS, EXPIRIES
from .services import SignalService
from .scheduler import SignalScheduler

STATIC = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    expiry: Literal[1, 3, 5, 15] = 3


class WatchRequest(AnalysisRequest):
    enabled: bool


class ResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: Literal["WIN", "LOSS", "DRAW"]


def create_app(settings: Settings) -> FastAPI:
    db = Database(settings.db_path)
    requests: dict[str, deque] = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init()
        # Use the OS trust roots (including Windows enterprise roots); keep TLS verification on.
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, verify=ssl.create_default_context()) as client:
            market = MarketData(settings, client)
            service = SignalService(settings, market, db)
            app.state.service, app.state.db = service, db
            app.state.bot_ready = False
            app.state.bot_status = "disabled" if not settings.bot_enabled else "not_configured"
            app.state.scheduler = SignalScheduler(settings, db, service)
            tasks = [asyncio.create_task(service.prepare_model()), asyncio.create_task(app.state.scheduler.run())]
            if settings.bot_enabled and settings.bot_token and settings.owner_id:
                app.state.bot_status = "loading"

                async def run_bot():
                    try:
                        runtime = await asyncio.to_thread(importlib.import_module, "app.bot_runtime")
                        await runtime.run(app, settings, db, service)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        app.state.bot_status = "error"
                        log.warning("Cannot load Telegram libraries. Install requirements.txt and restart.")

                tasks.append(asyncio.create_task(run_bot()))
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Signal Lab", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def user(request: Request, preview: bool = False) -> dict:
        auth = request.headers.get("authorization", "")
        if auth:
            try:
                if not auth.startswith("tma "):
                    raise AuthError("Откройте Mini App в Telegram.")
                value = validate_init_data(auth[4:], settings.bot_token, settings.auth_max_age_seconds)
            except AuthError as exc:
                raise HTTPException(401, str(exc)) from None
            if not settings.owner_id or value["id"] != settings.owner_id:
                raise HTTPException(403, "Это приватное приложение владельца бота.")
            return value
        # Preview requires both a loopback bind and a loopback peer/Host. No writes or journal.
        loopback = {"127.0.0.1", "::1", "localhost"}
        origin = request.headers.get("origin")
        if (preview and settings.local_preview and settings.host in loopback
                and request.client and request.client.host in loopback
                and request.url.hostname in loopback
                and (not origin or urlparse(origin).netloc == request.url.netloc)):
            return {"id": 0, "first_name": "Локальный просмотр", "preview": True}
        raise HTTPException(401, "Откройте приложение через кнопку бота в Telegram.")

    def rate_limit(identity: dict):
        key = str(identity["id"])
        queue = requests[key]
        now = time.monotonic()
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 30:
            raise HTTPException(429, "Слишком много запросов. Подождите минуту.")
        queue.append(now)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.exception_handler(MarketError)
    async def market_error(request: Request, exc: MarketError):
        return JSONResponse(status_code=503, content={"code": exc.code, "detail": exc.message})

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "2.0", "delivery": "miniapp"}

    @app.get("/api/session")
    async def session(request: Request):
        identity = user(request, preview=True)
        return {"user": {"first_name": identity.get("first_name", "Трейдер")},
                "preview": bool(identity.get("preview")), "timezone": settings.timezone_name,
                "server_time": int(time.time()), "bot_ready": app.state.bot_ready,
                "bot_status": app.state.bot_status, "model_status": app.state.service.model_status,
                "forex_ready": bool(settings.twelve_data_api_key), "webapp_ready": bool(settings.webapp_url),
                "min_score": settings.model_min_score * 100, "max_data_age_seconds": settings.max_data_age_seconds,
                "delivery": "miniapp", "version": "2.0",
                "instruments": [asdict(i) for i in INSTRUMENTS.values()], "expiries": EXPIRIES}

    @app.get("/api/market")
    async def market(request: Request, symbol: str = "EURUSD", expiry: int = 3):
        identity = user(request, preview=True)
        rate_limit(identity)
        return await app.state.service.snapshot(symbol, expiry)

    @app.post("/api/analyses")
    async def create_analysis(request: Request, body: AnalysisRequest):
        rate_limit(user(request))
        return await app.state.service.create(body.symbol, body.expiry)

    @app.get("/api/history")
    async def history(request: Request):
        user(request)
        return {"items": await db.history(), "stats": await db.stats(), "server_time": int(time.time())}

    @app.post("/api/history/{run_id}/result")
    async def result(request: Request, run_id: int, body: ResultRequest):
        user(request)
        if not await db.set_result(run_id, body.result):
            raise HTTPException(409, "Результат уже указан, сигнал отсутствует или ещё не закрыт.")
        return {"ok": True}

    @app.get("/api/watch")
    async def watch(request: Request):
        user(request)
        scheduler = app.state.scheduler
        return {**await db.watch(), "error": scheduler.scan_error if scheduler else None}

    @app.put("/api/watch")
    async def set_watch(request: Request, body: WatchRequest):
        rate_limit(user(request))
        if body.symbol not in INSTRUMENTS:
            raise HTTPException(422, "Неизвестный актив. OTC недоступен.")
        if body.enabled:
            snapshot = await app.state.service.snapshot(body.symbol, body.expiry)
            if not snapshot["fresh"]:
                raise HTTPException(409, "Нет свежих данных для автоанализа.")
        app.state.scheduler.scan_error = None
        return await db.set_watch(body.enabled, body.symbol, body.expiry)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app
