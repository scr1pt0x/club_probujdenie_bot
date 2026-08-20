import logging

from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)


class ScreenResponder:
    """Small answer-compatible adapter that keeps a flow in one Telegram message."""

    def __init__(self, message: types.Message, *, edit_existing: bool) -> None:
        self.message = message
        self.edit_existing = edit_existing
        self._rendered = False

    @property
    def bot(self):
        return self.message.bot

    async def answer(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
    ) -> types.Message:
        if not self._rendered and not self.edit_existing:
            rendered = await send_clean_screen(self.message, text, reply_markup)
        else:
            rendered = await edit_screen(self.message, text, reply_markup)
        self.message = rendered
        self.edit_existing = True
        self._rendered = True
        return rendered


async def safe_delete(message: types.Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        # The message may already be deleted or too old. Navigation must still work.
        return


async def remove_legacy_keyboard(message: types.Message) -> None:
    """Remove the old persistent reply keyboard without leaving a service message."""
    try:
        marker = await message.answer(
            "Обновляю меню…", reply_markup=types.ReplyKeyboardRemove()
        )
        await safe_delete(marker)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.debug("Could not remove legacy reply keyboard", exc_info=True)


async def send_clean_screen(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> types.Message:
    """Delete a legacy button/command message and send one clean bot screen."""
    await safe_delete(message)
    await remove_legacy_keyboard(message)
    return await message.answer(text, reply_markup=reply_markup)


async def edit_screen(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> types.Message:
    """Replace the current inline screen, falling back to a fresh message."""
    try:
        return await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        await safe_delete(message)
        return await message.answer(text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        return await message.answer(text, reply_markup=reply_markup)


async def edit_saved_screen(
    trigger: types.Message,
    screen_message_id: int | None,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> types.Message:
    """Replace a saved FSM prompt and remove the user's input message."""
    await safe_delete(trigger)
    if screen_message_id is not None:
        try:
            result = await trigger.bot.edit_message_text(
                chat_id=trigger.chat.id,
                message_id=screen_message_id,
                text=text,
                reply_markup=reply_markup,
            )
            if isinstance(result, types.Message):
                return result
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    return await trigger.answer(text, reply_markup=reply_markup)
