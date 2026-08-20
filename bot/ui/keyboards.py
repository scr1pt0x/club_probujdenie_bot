from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 Оплата", callback_data="nav:payment"),
                InlineKeyboardButton(text="👤 Мой доступ", callback_data="nav:status"),
            ],
            [
                InlineKeyboardButton(text="✨ Участие", callback_data="nav:shop"),
                InlineKeyboardButton(text="🗓 Расписание", callback_data="nav:schedule"),
            ],
            [
                InlineKeyboardButton(
                    text="⏳ Оплатить позже", callback_data="nav:pay_later"
                ),
                InlineKeyboardButton(text="🏷 Промокод", callback_data="nav:promo"),
            ],
            [InlineKeyboardButton(text="💬 Помощь", callback_data="nav:help")],
        ],
    )


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Главное меню", callback_data="nav:home")]
        ]
    )
