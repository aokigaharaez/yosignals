"""Optional Telegram dependencies, imported in a worker after the web server starts."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import MenuButtonWebApp, WebAppInfo

from .handlers import build_router

log = logging.getLogger(__name__)


async def run(app, settings, db, service):
    bot = None
    try:
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
        dp = Dispatcher()
        dp.include_router(build_router(settings))
        await bot.get_me()
        if settings.webapp_url:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Signal Lab", web_app=WebAppInfo(url=settings.webapp_url)))
        app.state.bot_ready = True
        app.state.bot_status = "ready"
        log.info("Telegram bot connected")
        await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        app.state.bot_status = "error"
        log.warning("Bot connection failed. Check token and network, then restart.")
    finally:
        app.state.bot_ready = False
        if bot:
            await bot.session.close()
