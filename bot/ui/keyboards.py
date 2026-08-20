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


def access_links_kb(
    channel_link: str | None, group_link: str | None
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if channel_link:
        rows.append([InlineKeyboardButton(text="📢 Войти в канал", url=channel_link)])
    if group_link:
        rows.append([InlineKeyboardButton(text="💬 Войти в группу", url=group_link)])
    if not rows:
        return None
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
