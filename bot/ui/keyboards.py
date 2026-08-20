from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="💳 Моя оплата"),
                KeyboardButton(text="👤 Мой статус"),
            ],
            [KeyboardButton(text="🛍 Тарифы"), KeyboardButton(text="📅 Расписание")],
            [KeyboardButton(text="⏳ Оплачу позже"), KeyboardButton(text="🏷 Промокод")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )
