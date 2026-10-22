import re
import time


TELEGRAM_BOT_URL_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")

EXPECTED_DELETE_ERRORS = (
    "message can't be deleted",
    "message to delete not found",
)

MESSAGE_NOT_MODIFIED = "message is not modified"
TRANSIENT_TELEGRAM_ERROR_CODES = {500, 502, 503, 504}
TRANSIENT_TELEGRAM_ERRORS = (
    "connect timeout",
    "connecttimeout",
    "connection reset",
    "connection aborted",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "read timed out",
    "max retries exceeded",
    "service unavailable",
    "temporarily unavailable",
    "remote end closed connection",
)


def sanitize_telegram_error_text(value):
    text = str(value)
    text = TELEGRAM_BOT_URL_TOKEN_RE.sub("bot<telegram-token>", text)
    return TELEGRAM_TOKEN_RE.sub("<telegram-token>", text)


def _error_text(exc):
    return sanitize_telegram_error_text(exc).lower()


def is_message_not_modified_error(exc):
    return MESSAGE_NOT_MODIFIED in _error_text(exc)


def is_expected_delete_error(exc):
    text = _error_text(exc)
    return any(marker in text for marker in EXPECTED_DELETE_ERRORS)


def telegram_retry_after_seconds(exc):
    result_json = getattr(exc, "result_json", None) or {}
    parameters = result_json.get("parameters") or {}
    retry_after = parameters.get("retry_after")
    if retry_after is None:
        return None

    try:
        return int(retry_after)
    except (TypeError, ValueError):
        return None


def telegram_error_code(exc):
    for value in (
        getattr(exc, "error_code", None),
        getattr(getattr(exc, "result", None), "status_code", None),
        (getattr(exc, "result_json", None) or {}).get("error_code"),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue

    match = re.search(r"error code:\s*(\d{3})", _error_text(exc))
    if match:
        return int(match.group(1))
    return None


def is_transient_telegram_error(exc):
    if telegram_retry_after_seconds(exc) is not None:
        return True

    error_code = telegram_error_code(exc)
    if error_code in TRANSIENT_TELEGRAM_ERROR_CODES:
        return True

    text = _error_text(exc)
    return any(marker in text for marker in TRANSIENT_TELEGRAM_ERRORS)


def call_telegram_with_retries(
    operation,
    logger=None,
    attempts=3,
    base_delay=2,
    max_retry_after=30,
    description="Telegram request",
):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            retry_after = telegram_retry_after_seconds(exc)
            should_retry = (
                attempt < attempts
                and is_transient_telegram_error(exc)
                and (retry_after is None or retry_after <= max_retry_after)
            )
            if not should_retry:
                raise

            wait_seconds = retry_after + 1 if retry_after is not None else base_delay * attempt
            if logger:
                logger.warning(
                    "%s failed attempt=%s/%s, retrying in %ss: %s",
                    description,
                    attempt,
                    attempts,
                    wait_seconds,
                    sanitize_telegram_error_text(exc),
                )
            time.sleep(wait_seconds)

    raise last_error


def safe_delete_message(bot, chat_id, message_id, logger=None):
    if not message_id:
        return False

    try:
        call_telegram_with_retries(
            lambda: bot.delete_message(chat_id=chat_id, message_id=message_id),
            logger=logger,
            attempts=2,
            description="Telegram delete_message",
        )
        return True
    except Exception as exc:
        if logger:
            if is_expected_delete_error(exc):
                logger.debug(
                    "Telegram delete_message skipped for chat=%s message=%s: %s",
                    chat_id,
                    message_id,
                    sanitize_telegram_error_text(exc),
                )
            else:
                logger.warning(
                    "Telegram delete_message failed for chat=%s message=%s: %s",
                    chat_id,
                    message_id,
                    sanitize_telegram_error_text(exc),
                )
        return False


def safe_edit_message_text(bot, logger=None, **kwargs):
    try:
        return call_telegram_with_retries(
            lambda: bot.edit_message_text(**kwargs),
            logger=logger,
            attempts=3,
            description="Telegram edit_message_text",
        )
    except Exception as exc:
        if logger:
            if is_message_not_modified_error(exc):
                logger.debug("Telegram edit_message_text skipped: %s", sanitize_telegram_error_text(exc))
            else:
                logger.warning("Telegram edit_message_text failed: %s", sanitize_telegram_error_text(exc))
        return None


def safe_edit_message_caption(bot, logger=None, **kwargs):
    try:
        return call_telegram_with_retries(
            lambda: bot.edit_message_caption(**kwargs),
            logger=logger,
            attempts=3,
            description="Telegram edit_message_caption",
        )
    except Exception as exc:
        if logger:
            if is_message_not_modified_error(exc):
                logger.debug("Telegram edit_message_caption skipped: %s", sanitize_telegram_error_text(exc))
            else:
                logger.warning("Telegram edit_message_caption failed: %s", sanitize_telegram_error_text(exc))
        return None


def edit_message_media_with_retries(bot, logger=None, attempts=3, **kwargs):
    return call_telegram_with_retries(
        lambda: bot.edit_message_media(**kwargs),
        logger=logger,
        attempts=attempts,
        description="Telegram edit_message_media",
    )


def safe_send_message(bot, chat_id, text, logger=None, attempts=3, **kwargs):
    try:
        return call_telegram_with_retries(
            lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
            logger=logger,
            attempts=attempts,
            description="Telegram send_message",
        )
    except Exception as exc:
        if logger:
            logger.warning("Telegram send_message failed for chat=%s: %s", chat_id, sanitize_telegram_error_text(exc))
        return None


def safe_send_photo(bot, chat_id, photo, logger=None, attempts=3, **kwargs):
    try:
        return call_telegram_with_retries(
            lambda: bot.send_photo(chat_id=chat_id, photo=photo, **kwargs),
            logger=logger,
            attempts=attempts,
            description="Telegram send_photo",
        )
    except Exception as exc:
        if logger:
            logger.warning("Telegram send_photo failed for chat=%s: %s", chat_id, sanitize_telegram_error_text(exc))
        return None


def safe_send_animation(bot, chat_id, animation, logger=None, attempts=3, **kwargs):
    try:
        return call_telegram_with_retries(
            lambda: bot.send_animation(chat_id=chat_id, animation=animation, **kwargs),
            logger=logger,
            attempts=attempts,
            description="Telegram send_animation",
        )
    except Exception as exc:
        if logger:
            logger.warning("Telegram send_animation failed for chat=%s: %s", chat_id, sanitize_telegram_error_text(exc))
        return None


def safe_send_document(bot, chat_id, document, logger=None, attempts=3, **kwargs):
    try:
        return call_telegram_with_retries(
            lambda: bot.send_document(chat_id=chat_id, document=document, **kwargs),
            logger=logger,
            attempts=attempts,
            description="Telegram send_document",
        )
    except Exception as exc:
        if logger:
            logger.warning("Telegram send_document failed for chat=%s: %s", chat_id, sanitize_telegram_error_text(exc))
        return None


def send_photo_or_text(bot, chat_id, photo, text, reply_markup=None, caption_limit=1024):
    if photo and len(text) <= caption_limit:
        return call_telegram_with_retries(
            lambda: bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=reply_markup),
            attempts=3,
            description="Telegram send_photo",
        )

    if photo:
        call_telegram_with_retries(
            lambda: bot.send_photo(chat_id, photo=photo),
            attempts=3,
            description="Telegram send_photo",
        )

    return call_telegram_with_retries(
        lambda: bot.send_message(chat_id, text, reply_markup=reply_markup),
        attempts=3,
        description="Telegram send_message",
    )
