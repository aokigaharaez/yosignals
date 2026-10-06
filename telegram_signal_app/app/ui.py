from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


def main_keyboard(url: str) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть Signal Lab", web_app=WebAppInfo(url=url))
    ]])


