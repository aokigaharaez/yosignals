from datetime import datetime
from html import escape
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


def main_keyboard(url: str) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть Signal Lab", web_app=WebAppInfo(url=url))
    ]])


def result_keyboard(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"result:{run_id}:{value}")
        for label, value in (("✅ WIN", "WIN"), ("❌ LOSS", "LOSS"), ("➖ DRAW", "DRAW"))
    ]])


def format_signal(run: dict, tz) -> str:
    direction = {"CALL": "🟢 CALL ↑", "PUT": "🔴 PUT ↓", "WAIT": "⏸ ЖДАТЬ"}[run["direction"]]
    entry = datetime.fromtimestamp(run["entry_at"], tz).strftime("%H:%M:%S")
    close = datetime.fromtimestamp(run["close_at"], tz).strftime("%H:%M:%S")
    score = f'{run["score"]:.1f}%' if run["score"] is not None else "—"
    return (
        f'<b>Signal Lab · {escape(run["label"])}</b>\n\n<b>{direction}</b>\n'
        f'Оценка модели: {score}\n'
        + (f'Вход: {entry} · закрытие: {close}\nЭкспирация: {run["expiry"]} мин\n' if run["direction"] != "WAIT" else "")
        + f'Часовой пояс: {escape(str(tz))}\nИсточник: {escape(run["provider"])}\n\n'
        + "\n".join(escape(r) for r in run["reasons"])
        + "\n\n<i>Внешние котировки; сверяйте с Pocket Option. Не для OTC. Оценка модели не гарантирует результат.</i>"
    )

