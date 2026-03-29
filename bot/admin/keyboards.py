from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admin.templates import TEMPLATE_LABELS


def back_menu_kb(back_to: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=back_to),
                InlineKeyboardButton(text="🏠 Меню", callback_data="admin:menu"),
            ],
        ]
    )


def templates_list_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"admin:text:{key}")]
        for key, label in TEMPLATE_LABELS.items()
    ]
    rows.extend(back_menu_kb("admin:menu").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def template_card_kb(key: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Изменить", callback_data=f"admin:text:edit:{key}"
            ),
            InlineKeyboardButton(
                text="📨 Тест себе", callback_data=f"admin:text:test:{key}"
            ),
        ],
    ]
    rows.extend(back_menu_kb("admin:texts").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    rows.extend(back_menu_kb("admin:menu").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flows_edit_select_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Текущий", callback_data="admin:flows:edit:current"),
            InlineKeyboardButton(text="Следующий", callback_data="admin:flows:edit:next"),
        ],
    ]
    rows.extend(back_menu_kb("admin:flows").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prices_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Вступительная", callback_data="admin:prices:edit:intro"
            ),
            InlineKeyboardButton(
                text="✏️ Продление", callback_data="admin:prices:edit:renewal"
            ),
        ],
        [
            InlineKeyboardButton(text="✏️ Grace", callback_data="admin:prices:edit:grace"),
            InlineKeyboardButton(
                text="✏️ Оплачу позже", callback_data="admin:prices:edit:pay_later"
            ),
        ],
    ]
    rows.extend(back_menu_kb("admin:menu").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mailings_menu_kb(enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "⛔ Выключить" if enabled else "✅ Включить"
    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data="admin:mailings:toggle")],
        [
            InlineKeyboardButton(
                text="👥 Всем",
                callback_data="admin:mailings:custom:all",
            ),
            InlineKeyboardButton(
                text="✅ Текущим",
                callback_data="admin:mailings:custom:active",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🕓 Бывшим",
                callback_data="admin:mailings:custom:former",
            ),
            InlineKeyboardButton(
                text="💳 Не оплатившим",
                callback_data="admin:mailings:custom:current_unpaid",
            )
        ],
    ]
    rows.extend(back_menu_kb("admin:menu").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def users_search_kb() -> InlineKeyboardMarkup:
    return back_menu_kb("admin:menu")


def user_card_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✅ Выдать доступ", callback_data=f"admin:users:grant:{user_id}"
            ),
            InlineKeyboardButton(
                text="⛔ Забрать доступ", callback_data=f"admin:users:revoke:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="➕ Продлить на 7 дней",
                callback_data=f"admin:users:extend7:{user_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🧹 Сбросить 'оплачу позже'",
                callback_data=f"admin:users:reset_pay_later:{user_id}",
            ),
            InlineKeyboardButton(
                text="🧹 Сбросить промокод",
                callback_data=f"admin:users:reset_promo:{user_id}",
            ),
        ],
    ]
    rows.extend(back_menu_kb("admin:users").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promos_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promos:create")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin:promos:list")],
        [InlineKeyboardButton(text="🧼 Отключить промокод", callback_data="admin:promos:disable")],
    ]
    rows.extend(back_menu_kb("admin:menu").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promo_kind_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="% Процент", callback_data="admin:promos:kind:percent"),
            InlineKeyboardButton(text="Фикс", callback_data="admin:promos:kind:fixed"),
        ],
        [InlineKeyboardButton(text="Бесплатно", callback_data="admin:promos:kind:free")],
    ]
    rows.extend(back_menu_kb("admin:promos").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Цена вступления", callback_data="admin:shop:edit:intro"
            ),
            InlineKeyboardButton(
                text="✏️ Цена продления", callback_data="admin:shop:edit:renewal"
            ),
        ],
        [
            InlineKeyboardButton(
                text="✏️ Надпись (бесплатно)", callback_data="admin:shop:edit:free_label"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📝 Тексты витрины", callback_data="admin:shop:texts"
            ),
            InlineKeyboardButton(
                text="🧪 Тест витрины себе", callback_data="admin:shop:test"
            ),
        ],
    ]
    rows.extend(back_menu_kb("admin:menu").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_texts_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛍 Заголовок", callback_data="admin:text:shop_title")],
        [InlineKeyboardButton(text="🛍 Вступление", callback_data="admin:text:shop_intro_desc")],
        [InlineKeyboardButton(text="🛍 Продление", callback_data="admin:text:shop_renewal_desc")],
        [InlineKeyboardButton(text="🛍 Бесплатно", callback_data="admin:text:shop_free_desc")],
        [InlineKeyboardButton(text="🛍 Заказ", callback_data="admin:text:shop_order_text")],
    ]
    rows.extend(back_menu_kb("admin:shop").inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)
