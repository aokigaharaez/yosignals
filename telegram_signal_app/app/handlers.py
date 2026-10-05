from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from .config import Settings
from .db import Database
from .market import MarketError
from .models import INSTRUMENTS, EXPIRIES
from .services import SignalService
from .ui import main_keyboard, format_signal


def build_router(settings: Settings, db: Database, service: SignalService) -> Router:
    router = Router()
    router.message.filter(F.from_user.id == settings.owner_id, F.chat.type == "private")
    router.callback_query.filter(F.from_user.id == settings.owner_id)

    @router.message(CommandStart())
    async def start(message: Message):
        await message.answer(
            "<b>Signal Lab</b>\nРеальные рыночные свечи → ML-анализ → CALL / PUT / ЖДАТЬ.\n\n"
            "/analyze EURUSD 3 — анализ актива\n"
            "/watch EURUSD 3 — автоанализ и уведомления\n"
            "/stop — остановить автоанализ\n"
            "/stats — результаты\n\n"
            "OTC не поддерживается. Оценка модели не гарантирует результат."
            + ("\n\nДля Mini App настройте WEBAPP_URL с публичным HTTPS-адресом." if not settings.webapp_url else ""),
            reply_markup=main_keyboard(settings.webapp_url))

    @router.message(Command("analyze", "watch"))
    async def analyze_command(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Формат: /analyze EURUSD 3 или /watch EURUSD 3. Экспирация: 1, 3, 5, 15 мин.")
            return
        symbol = parts[1].upper().replace("/", "")
        if symbol not in INSTRUMENTS or not parts[2].isdigit() or int(parts[2]) not in EXPIRIES:
            await message.answer("Доступны EURUSD, GBPUSD, USDJPY, AUDUSD, EURJPY, BTCUSDT, ETHUSDT. OTC недоступен.")
            return
        expiry = int(parts[2])
        try:
            run = await service.create(symbol, expiry)
            await message.answer(format_signal(run, settings.timezone))
            if parts[0].split("@")[0] == "/watch":
                await db.set_watch(True, symbol, expiry)
                await message.answer("Автоанализ включён. Уведомления поступают только при новом CALL или PUT. /stop — остановить.")
        except MarketError as exc:
            await message.answer(exc.message)

    @router.message(Command("stop"))
    async def stop(message: Message):
        watch = await db.watch()
        await db.set_watch(False, watch["symbol"], watch["expiry"])
        await message.answer("Автоанализ остановлен. Напоминания по уже созданным сигналам сохраняются.")

    @router.message(Command("stats"))
    async def stats(message: Message):
        s = await db.stats()
        rate = f'{s["win_rate"]}%' if s["win_rate"] is not None else "пока нет"
        await message.answer(f'<b>Результаты, отмеченные вами</b>\nWIN: {s["wins"]} · LOSS: {s["losses"]} · DRAW: {s["draws"]}\nWin rate: {rate}')

    @router.callback_query(F.data.startswith("result:"))
    async def result(callback: CallbackQuery):
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"WIN", "LOSS", "DRAW"}:
            await callback.answer("Некорректный результат")
            return
        ok = await db.set_result(int(parts[1]), parts[2])
        await callback.answer("Результат сохранён" if ok else "Уже отмечен или время закрытия ещё не наступило", show_alert=True)
        if ok and callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    return router

