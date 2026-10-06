from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from .config import Settings
from .db import Database
from .access import is_owner
from .ui import main_keyboard


def build_router(settings: Settings, db: Database) -> Router:
    router = Router()

    async def owner_filter(event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and await is_owner(settings, db, event.from_user.id)

    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(owner_filter)

    @router.message(CommandStart())
    @router.message(Command("app", "analyze", "watch", "stop", "stats"))
    async def open_app(message: Message):
        if not await owner_filter(message):
            if message.from_user:
                await message.answer(
                    f"Доступ пока не выдан. Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
                    "Передайте его владельцу, чтобы он добавил вас в разделе «Доступ» Mini App. После этого отправьте /start ещё раз.")
            return
        await message.answer(
            "<b>Signal Lab</b>\n\nАнализы, сигналы, оценка шанса и история теперь в Mini App. "
            "Откройте приложение и нажмите «Получить сигнал». Автоанализ также настраивается внутри приложения."
            + ("\n\nВ Railway Variables задайте WEBAPP_URL с HTTPS-адресом приложения." if not settings.webapp_url else ""),
            reply_markup=main_keyboard(settings.webapp_url))

    @router.callback_query()
    async def old_button(callback: CallbackQuery):
        await callback.answer("Сигналы и результаты перенесены в Mini App. Откройте его кнопкой меню.", show_alert=True)

    return router
