import logging
from typing import Callable, Optional, Tuple

from aiogram import types

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n


def resolve_i18n_context(
    i18n_data: dict, settings: Settings
) -> Tuple[str, Optional[JsonI18n], Callable[[str], str]]:
    """Return the current language, i18n instance and a translator helper."""

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if i18n:
        return (
            current_lang,
            i18n,
            lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs),
        )

    return current_lang, None, lambda key, **kwargs: key


async def safe_answer_callback(
    callback: Optional[types.CallbackQuery],
    text: Optional[str] = None,
    *,
    show_alert: bool = False,
) -> bool:
    """Safely answer a callback query without raising if Telegram rejects it."""

    if not callback:
        return False

    try:
        await callback.answer(text, show_alert=show_alert)
        return True
    except Exception:
        logging.debug("Failed to answer callback", exc_info=True)
        return False


async def safe_edit_message_text(
    message: Optional[types.Message], text: str, **kwargs
) -> bool:
    """Attempt to edit a message and swallow recoverable errors."""

    if not message:
        return False

    try:
        await message.edit_text(text, **kwargs)
        return True
    except Exception:
        logging.debug("Failed to edit message text", exc_info=True)
        return False


async def safe_send_message(
    message: Optional[types.Message], text: str, **kwargs
) -> bool:
    """Send a reply message while ignoring transient Telegram errors."""

    if not message:
        return False

    try:
        await message.answer(text, **kwargs)
        return True
    except Exception:
        logging.debug("Failed to send message reply", exc_info=True)
        return False
