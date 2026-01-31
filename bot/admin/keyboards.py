from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admin.templates import TEMPLATE_LABELS


def templates_list_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"admin:text:{key}")]
        for key, label in TEMPLATE_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def template_card_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить", callback_data=f"admin:text:edit:{key}"
                ),
                InlineKeyboardButton(
                    text="📨 Тест себе", callback_data=f"admin:text:test:{key}"
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:texts"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )


def flows_menu_kb(show_create_paid: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ Изменить даты", callback_data="admin:flows:edit")],
    ]
    if show_create_paid:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🆕 Создать следующий платный",
                    callback_data="admin:flows:create_paid",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:flows"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flows_edit_select_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Текущий", callback_data="admin:flows:edit:current"
                ),
                InlineKeyboardButton(
                    text="Следующий", callback_data="admin:flows:edit:next"
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:flows"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )


def prices_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Вступительная", callback_data="admin:prices:edit:intro"
                ),
                InlineKeyboardButton(
                    text="✏️ Продление", callback_data="admin:prices:edit:renewal"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Grace", callback_data="admin:prices:edit:grace"
                ),
                InlineKeyboardButton(
                    text="✏️ Оплачу позже", callback_data="admin:prices:edit:pay_later"
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )


def mailings_menu_kb(enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "⛔ Выключить" if enabled else "✅ Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="admin:mailings:toggle")],
            [
                InlineKeyboardButton(
                    text="🧪 Тест -7 себе", callback_data="admin:mailings:test:minus_7"
                ),
                InlineKeyboardButton(
                    text="🧪 Тест -3 себе", callback_data="admin:mailings:test:minus_3"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Запустить -7 (всем)",
                    callback_data="admin:mailings:run:minus_7",
                ),
                InlineKeyboardButton(
                    text="🚀 Запустить -3 (всем)",
                    callback_data="admin:mailings:run:minus_3",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )


def users_search_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )


def user_card_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Выдать доступ",
                    callback_data=f"admin:users:grant:{user_id}",
                ),
                InlineKeyboardButton(
                    text="⛔ Забрать доступ",
                    callback_data=f"admin:users:revoke:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Продлить на 7 дней",
                    callback_data=f"admin:users:extend7:{user_id}",
                ),
                InlineKeyboardButton(
                    text="🧹 Сбросить 'оплачу позже'",
                    callback_data=f"admin:users:reset_pay_later:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:users"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )
