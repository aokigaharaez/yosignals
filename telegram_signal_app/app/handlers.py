from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from .config import Settings
from .ui import main_keyboard


def build_router(settings: Settings) -> Router:
    router = Router()
    router.message.filter(F.from_user.id == settings.owner_id, F.chat.type == "private")
    router.callback_query.filter(F.from_user.id == settings.owner_id)

    @router.message(CommandStart())
    @router.message(Command("app", "analyze", "watch", "stop", "stats"))
    async def open_app(message: Message):
        await message.answer(
            "<b>Signal Lab</b>\n\nАнализы, сигналы, оценка шанса и история теперь в Mini App. "
            "Откройте приложение и нажмите «Получить сигнал». Автоанализ также настраивается внутри приложения."
            + ("\n\nВ Railway Variables задайте WEBAPP_URL с HTTPS-адресом приложения." if not settings.webapp_url else ""),
            reply_markup=main_keyboard(settings.webapp_url))

    @router.callback_query()
    async def old_button(callback: CallbackQuery):
        await callback.answer("Сигналы и результаты перенесены в Mini App. Откройте его кнопкой меню.", show_alert=True)

    return router
