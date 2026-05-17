EXPECTED_DELETE_ERRORS = (
    "message can't be deleted",
    "message to delete not found",
)

MESSAGE_NOT_MODIFIED = "message is not modified"


def _error_text(exc):
    return str(exc).lower()


def is_message_not_modified_error(exc):
    return MESSAGE_NOT_MODIFIED in _error_text(exc)


def is_expected_delete_error(exc):
    text = _error_text(exc)
    return any(marker in text for marker in EXPECTED_DELETE_ERRORS)


def safe_delete_message(bot, chat_id, message_id, logger=None):
    if not message_id:
        return False

    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as exc:
        if logger:
            if is_expected_delete_error(exc):
                logger.debug(
                    "Telegram delete_message skipped for chat=%s message=%s: %s",
                    chat_id,
                    message_id,
                    exc,
                )
            else:
                logger.warning(
                    "Telegram delete_message failed for chat=%s message=%s: %s",
                    chat_id,
                    message_id,
                    exc,
                )
        return False


def safe_edit_message_text(bot, logger=None, **kwargs):
    try:
        return bot.edit_message_text(**kwargs)
    except Exception as exc:
        if logger:
            if is_message_not_modified_error(exc):
                logger.debug("Telegram edit_message_text skipped: %s", exc)
            else:
                logger.warning("Telegram edit_message_text failed: %s", exc)
        return None


def send_photo_or_text(bot, chat_id, photo, text, reply_markup=None, caption_limit=1024):
    if photo and len(text) <= caption_limit:
        return bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=reply_markup)

    if photo:
        bot.send_photo(chat_id, photo=photo)

    return bot.send_message(chat_id, text, reply_markup=reply_markup)
