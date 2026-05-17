def safe_delete_message(bot, chat_id, message_id, logger=None):
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as exc:
        if logger:
            logger.debug("Telegram delete_message failed: %s", exc)
        return False


def send_photo_or_text(bot, chat_id, photo, text, reply_markup=None, caption_limit=1024):
    if photo and len(text) <= caption_limit:
        return bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=reply_markup)

    if photo:
        bot.send_photo(chat_id, photo=photo)

    return bot.send_message(chat_id, text, reply_markup=reply_markup)
