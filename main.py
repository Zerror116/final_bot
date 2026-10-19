import io
import logging
import os
import re
import threading
import time
import telebot
import locale
from urllib.parse import quote
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from bot import admin_main_menu, client_main_menu, worker_main_menu, unknown_main_menu, supreme_leader_main_menu, audit_main_menu
from telebot import types, apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputFile, InputMediaAnimation
from database.config import *
from db.for_delivery import ForDelivery
from db.temp_reservations import TempReservations
from db.in_delivery import InDelivery
from db.temp_fulfilied import Temp_Fulfilled
from db.deleted_post_snapshots import DeletedPostSnapshot
from db.delivery_cleanup_runs import DeliveryCleanupRun
from db.revision_logs import RevisionLog
from db.reservation_stat_events import ReservationStatEvent
from db.delivery_broadcast_campaigns import DeliveryBroadcastCampaign
from db.delivery_broadcast_recipients import DeliveryBroadcastRecipient
from db.delivery_collection_items import DeliveryCollectionItem
from db.missing_delivery_items import MissingDeliveryItem
from db import init_db
from handlers.black_list import *
from handlers.clients_manage import *
from handlers.posts_manage import *
from handlers.reservations_manage import *
from types import SimpleNamespace
from handlers.reservations_manage import calculate_total_sum, calculate_processed_sum
from handlers.classess import *
from sqlalchemy import and_, func, or_, text, tuple_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from datetime import datetime, timedelta, time as datetime_time
from db.bot_session import BotSession
from services.pricing import calculate_audit_price
from services.session_store import PersistentBucket
from services.telegram_safe import (
    call_telegram_with_retries,
    edit_message_media_with_retries,
    is_transient_telegram_error,
    is_message_not_modified_error,
    safe_delete_message,
    safe_edit_message_caption,
    safe_edit_message_text,
    safe_send_animation,
    safe_send_document,
    safe_send_message,
    safe_send_photo,
    sanitize_telegram_error_text,
    send_photo_or_text,
)
from services.google_sheets_delivery import (
    DeliverySheetError,
    add_delivery_amount,
    sync_payroll_sheet_week,
    write_delivery_collector_name,
)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default

    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def env_int(name, default, minimum=None):
    raw_value = os.environ.get(name)
    try:
        value = int(str(raw_value).strip()) if raw_value is not None and str(raw_value).strip() else default
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name, default, minimum=None):
    raw_value = os.environ.get(name)
    try:
        value = float(str(raw_value).strip()) if raw_value is not None and str(raw_value).strip() else default
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
ADMIN_ROLES = {"admin", "supreme_leader"}
POST_EDITOR_ROLES = {"worker", "admin", "supreme_leader", "audit"}
DELIVERY_ROLES = {"admin", "supreme_leader"}
DELIVERY_COLLECTION_ROLES = DELIVERY_ROLES | {"audit"}
DELIVERY_THRESHOLD = 1500
RESERVATION_AUTO_FULFILL_SECONDS = 60 * 60
RESERVATION_AUTO_FULFILL_CHECK_SECONDS = 60
POST_ID_REUSE_STALE_GRACE = timedelta(days=2)
DELIVERY_COLLECTION_PAGE_SIZE = 8
MISSING_DELIVERY_PAGE_SIZE = 8
DEFECT_ITEM_PAGE_SIZE = 8
DEFECTIVE_RESERVATION_PAGE_SIZE = 8
MISSING_DELIVERY_STATUS_MISSING = "missing"
MISSING_DELIVERY_STATUS_RETURNED_CHANNEL = "returned_channel"
MISSING_DELIVERY_STATUS_RETURNED_CART = "returned_cart"
MISSING_DELIVERY_STATUS_LABELS = {
    MISSING_DELIVERY_STATUS_MISSING: "В пропаже",
    MISSING_DELIVERY_STATUS_RETURNED_CHANNEL: "Вернули на канал",
    MISSING_DELIVERY_STATUS_RETURNED_CART: "Вернули в корзину",
}
LEGACY_DELIVERY_CUTOFF_AT = datetime(2026, 5, 16, 14, 0, 0)
SAMARA_TZ = ZoneInfo("Europe/Samara")
DELIVERY_BROADCAST_CUTOFF_HOUR = 14
DELIVERY_BROADCAST_CHECK_SECONDS = 60
DELIVERY_BROADCAST_STALE_SENDING_MINUTES = 5
DELIVERY_BROADCAST_STATUS_ACTIVE = "active"
DELIVERY_BROADCAST_STATUS_FINISHED = "finished"
DELIVERY_RECIPIENT_STATUS_SENDING = "sending"
DELIVERY_RECIPIENT_STATUS_SENT = "sent"
DELIVERY_RECIPIENT_STATUS_FAILED = "failed"
DELIVERY_RECIPIENT_STATUS_UNKNOWN = "unknown"
DELIVERY_RECIPIENT_RESPONSE_ACCEPTED = "accepted"
DELIVERY_RECIPIENT_RESPONSE_DECLINED = "declined"
DELIVERY_RECIPIENT_RESPONSE_LABELS = {
    DELIVERY_RECIPIENT_RESPONSE_ACCEPTED: "Согласился",
    DELIVERY_RECIPIENT_RESPONSE_DECLINED: "Отказался",
}
AUTO_CHANNEL_POST_HOURS = {10, 12, 14, 16}
AUTO_CHANNEL_POST_CHECK_SECONDS = 30
CHANNEL_POST_SEND_ATTEMPTS = 3
CHANNEL_POST_SEND_RETRY_SECONDS = 5
TELEGRAM_STARTUP_RETRY_SECONDS = env_float("TELEGRAM_STARTUP_RETRY_SECONDS", 5, minimum=0)
TELEGRAM_STARTUP_MAX_RETRY_SECONDS = env_float("TELEGRAM_STARTUP_MAX_RETRY_SECONDS", 60, minimum=1)
TELEGRAM_API_RETRY_ON_ERROR = env_bool("TELEGRAM_API_RETRY_ON_ERROR", True)
TELEGRAM_API_MAX_RETRIES = env_int("TELEGRAM_API_MAX_RETRIES", 3, minimum=1)
TELEGRAM_API_RETRY_TIMEOUT_SECONDS = env_float("TELEGRAM_API_RETRY_TIMEOUT_SECONDS", 2, minimum=0)
TELEGRAM_POLLING_TIMEOUT_SECONDS = env_int("TELEGRAM_POLLING_TIMEOUT_SECONDS", 15, minimum=1)
TELEGRAM_LONG_POLLING_TIMEOUT_SECONDS = env_int("TELEGRAM_LONG_POLLING_TIMEOUT_SECONDS", 30, minimum=1)
DELIVERY_CLEANUP_WEEKDAYS = {0, 2, 4}
DELIVERY_CLEANUP_HOUR = 22
DELIVERY_CLEANUP_CHECK_SECONDS = 60
RESERVATION_STATS_REPORT_HOUR = 16
RESERVATION_STATS_REPORT_MINUTE = 10
RESERVATION_STATS_CHECK_SECONDS = 30
RESERVATION_STATS_DAY_START_HOUR = 6
RESERVATION_STATS_DAY_END_HOUR = 23
RESERVATION_STATS_EVENT_CREATED = "created"
RESERVATION_STATS_EVENT_CANCELED = "canceled"
RESERVATION_STATS_EVENT_FULFILLED = "fulfilled"
RESERVATION_STATS_REPORT_STATE_KEY = 2
PAYROLL_SHEET_SYNC_STATE_KEY = 3
PAYROLL_SHEET_SYNC_WEEKDAY = 4
PAYROLL_SHEET_SYNC_HOUR = 23
PAYROLL_SHEET_SYNC_MINUTE = 0
PAYROLL_SHEET_SYNC_CHECK_SECONDS = 60
RESERVATION_DB_RETRY_ATTEMPTS = env_int("RESERVATION_DB_RETRY_ATTEMPTS", 5, minimum=1)
RESERVATION_DB_RETRY_BASE_SECONDS = env_float("RESERVATION_DB_RETRY_BASE_SECONDS", 0.5, minimum=0)
RESERVED_GROUP_FLOW_STATE_KEY = 0
RESERVED_GROUP_RESUME_BATCH_SIZE = 50
RESERVED_GROUP_SEND_INTERVAL_SECONDS = 5
RESERVED_GROUP_MESSAGE_SKIPPED = -1
DELIVERY_COLLECTION_REPORT_GROUP_ID = env_int("DELIVERY_COLLECTION_REPORT_GROUP_ID", -1004453060578)
DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS = env_float(
    "DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS", 5, minimum=0
)
DELIVERY_COLLECTION_REPORT_LOCK_KEY = env_int("DELIVERY_COLLECTION_REPORT_LOCK_KEY", 4453060578)
DELIVERY_SHEET_LOCK_KEY = env_int("DELIVERY_SHEET_LOCK_KEY", 4453060579)
DELIVERY_SHEET_RETRY_SECONDS = env_float("DELIVERY_SHEET_RETRY_SECONDS", 60, minimum=1)
DELIVERY_SHEET_RETRY_BATCH_SIZE = env_int("DELIVERY_SHEET_RETRY_BATCH_SIZE", 25, minimum=1)
MERGE_DELIVERY_CARTS_BUTTON = "Объединить корзины"
MERGE_DELIVERY_MAIN_STATE = "MERGE_DELIVERY_MAIN"
MERGE_DELIVERY_EXTRA_STATE = "MERGE_DELIVERY_EXTRA"
PHOENIX_BROADCAST_BUTTON = "Рассылка о Фениксе"
PHOENIX_BROADCAST_DELAY_SECONDS = env_float("PHOENIX_BROADCAST_DELAY_SECONDS", 1.5, minimum=0)
PHOENIX_BROADCAST_BATCH_SIZE = env_int("PHOENIX_BROADCAST_BATCH_SIZE", 50, minimum=1)
PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS = env_float(
    "PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS", 10, minimum=0
)
PHOENIX_QR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "phoenix_qr.jpg")
PHOENIX_CHANNEL_FOOTER_IMAGE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "images",
    "phoenix_channel_footer.jpg",
)
ORDER_CART_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "my_cart.jpg")
DELIVERY_ORDER_GIF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "delivery_order.gif")
PHOENIX_BROADCAST_TEXT = (
    "Доброго времени суток, уважаемые клиенты Мега Скидки\n"
    "Сегодня происходит выкладка товара как в Телеграме, так и в Фениксе\n"
    "Т.к никто не знает судьбу телеграмма, просим в основном полагаться на Феникс\n"
    "В ближайшее время полностью перейдем в Феникс, так же будут разыгрываться призы среди пользователей Феникса\n"
    "Сайт: https://garphoenix.com/join/INV-R9JQ-F8UW?tenant=default (скопируйте и вставьте в браузер, т.к телеграмм может не пропустить на сайт)\n"
    "Или можете перейти через QR-код"
)
PHOENIX_CHANNEL_FOOTER_TEXT = (
    "🔥 Важные новости от «Мега Скидки»: Возвращение Феникса, большое обновление ВК и наши запасные площадки!\n"
    "Друзья, у нас накопилось много важных новостей и обновлений. Мы постоянно работаем над тем, чтобы наши сервисы были удобными и надежными для вас. Рассказываем по порядку, что изменилось:\n"
    "🦅 Феникс снова в строю!\n"
    "Мы исправили все ошибки, возобновили отгрузки и полностью перезапустили работу Феникса!\n"
    "- Внимание: весь канал и все корзины в Фениксе были очищены. Мы начали с чистого листа!\n"
    "- Уже стартовала новая выкладка товаров, и они прямо сейчас доступны к бронированию.\n"
    "💙 Глобальное обновление ВКонтакте\n"
    "Мы активно улучшаем наше сообщество в ВК, чтобы сделать его таким же удобным, как наш Телеграм-канал.\n"
    "- В ближайшее время мы запустим там точно такого же бота, к которому вы привыкли здесь.\n"
    "- На странице сообщества появится кнопка «Перейти в канал». Там будет работать привычная выкладка: те же кнопочки «Забронировать», удобный просмотр корзин, рассылка на доставку и весь остальной функционал.\n"
    "- Важно: Весь товар со стены сообщества будет удален и перенесен в этот канал для удобного бронирования кнопкой. На самой стене останутся только важные новости и правила.\n"
    "🤔 Зачем нам так много разных каналов?\n"
    "В последнее время мы получаем много вопросов на эту тему. Отвечаем честно: дальнейшая судьба Телеграма нам, как и всем, неизвестна.\n"
    "Мы хотим предотвратить любые риски для наших клиентов. В случае, если Телеграм заблокируют или он перестанет работать, мы просто продолжим радовать вас скидками в Фениксе и ВКонтакте.\n"
    "Кроме того, это вопрос вашего удобства:\n"
    "- Кому-то нравится Феникс, но нет странички в ВК.\n"
    "- Кому-то Феникс непривычен, и гораздо проще использовать ВКонтакте.\n"
    "- У некоторых клиентов Телеграм уже сейчас работает с сильными перебоями, и они теряют с нами связь — для них новые площадки станут отличной альтернативой.\n"
    "Наша команда делает всё, чтобы вам было комфортно с нами сотрудничать, независимо от внешних обстоятельств!\n"
    "📌 Что будет с Телеграмом?\n"
    "Телеграм ПОКА остается нашим основным каналом. Именно здесь сейчас будет происходить наибольшая выкладка товара. Но если с Телеграмом возникнет глобальная проблема, мы будем готовы за один день полностью перейти на ВК и Феникс без потери связи с вами.\n"
    "👇 Обязательно подпишитесь и сохраните ссылки, чтобы мы с вами никогда не потерялись!\n"
    "🔗 Наш Феникс: https://garphoenix.com/join/INV-R9JQ-F8UW?tenant=default\n"
    "🔗 Наше сообщество ВК: https://vk.ru/club239413025\n"
    "С заботой о вас, ваша команда «Мега Скидки»! ❤️"
)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class TelegramTokenRedactingFormatter(logging.Formatter):
    def format(self, record):
        return sanitize_telegram_error_text(super().format(record))


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format=LOG_FORMAT,
)
for log_handler in logging.getLogger().handlers:
    log_handler.setFormatter(TelegramTokenRedactingFormatter(LOG_FORMAT))
logger = logging.getLogger(__name__)


class TelegramPollingExceptionHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        if not is_transient_telegram_error(exception):
            return False

        logger.warning(
            "Telegram polling transient error, polling will continue: %s",
            sanitize_telegram_error_text(exception),
        )
        return True


def now_local():
    return now_samara().replace(tzinfo=None)


def now_samara():
    return datetime.now(SAMARA_TZ)


def build_channel_post_caption(post):
    return f"Цена: {post.price} ₽\nОписание: {post.description}\nОстаток: {post.quantity}"


def build_bot_only_markup():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("В бота", url=f"{bot_link}?start=start"))
    return markup


def build_channel_post_markup(post):
    markup = InlineKeyboardMarkup()
    if post.quantity > 0:
        markup.add(
            InlineKeyboardButton("🛒 Забронировать", callback_data=f"reserve_{post.id}"),
            InlineKeyboardButton("В бота", url=f"{bot_link}?start=start"),
        )
    else:
        return build_bot_only_markup()
    return markup


def build_unavailable_channel_post_caption(post, reason="Товар снят с продажи"):
    return (
        f"{reason}\n\n"
        f"Цена: {post.price} ₽\n"
        f"Описание: {post.description}\n"
        "Остаток: 0"
    )


def telegram_error_text(exc):
    return sanitize_telegram_error_text(exc).lower()


def telegram_error_contains(exc, *markers):
    text = telegram_error_text(exc)
    return any(marker in text for marker in markers)


TRANSIENT_DATABASE_SQLSTATES = {"40001", "40P01", "55P03"}
TRANSIENT_DATABASE_ERROR_MARKERS = (
    "canceling statement due to lock timeout",
    "could not obtain lock",
    "could not serialize access",
    "deadlock detected",
    "lock timeout",
)


def database_error_sqlstate(exc):
    seen = set()
    current = exc
    while current and id(current) not in seen:
        seen.add(id(current))
        for attr_name in ("pgcode", "sqlstate"):
            value = getattr(current, attr_name, None)
            if value:
                return str(value)

        orig = getattr(current, "orig", None)
        if orig and id(orig) not in seen:
            current = orig
            continue

        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


def is_transient_database_error(exc):
    sqlstate = database_error_sqlstate(exc)
    if sqlstate in TRANSIENT_DATABASE_SQLSTATES:
        return True

    text = str(exc).lower()
    return any(marker in text for marker in TRANSIENT_DATABASE_ERROR_MARKERS)


def sleep_before_database_retry(attempt):
    if RESERVATION_DB_RETRY_BASE_SECONDS <= 0:
        return
    time.sleep(RESERVATION_DB_RETRY_BASE_SECONDS * max(attempt, 1))


def is_message_to_edit_missing_error(exc):
    return telegram_error_contains(exc, "message to edit not found")


def telegram_retry_after_seconds(exc):
    match = re.search(r"retry after (\d+)", telegram_error_text(exc))
    return int(match.group(1)) if match else None


def log_channel_sync_error(action, post_id, exc):
    expected_errors = (
        "message to edit not found",
        "message to delete not found",
        "there is no text in the message to edit",
        "message can't be deleted",
    )
    retry_after = telegram_retry_after_seconds(exc)
    if retry_after is not None:
        logger.info(
            "Channel post %s rate-limited for post_id=%s retry_after=%ss",
            action,
            post_id,
            retry_after,
        )
        return

    if telegram_error_contains(exc, *expected_errors):
        logger.debug("Channel post %s skipped for post_id=%s: %s", action, post_id, exc)
        return

    logger.warning("Channel post %s failed for post_id=%s: %s", action, post_id, exc)


def sleep_for_short_retry_after(exc, max_retry_after=5):
    retry_after = telegram_retry_after_seconds(exc)
    if retry_after is None or retry_after > max_retry_after:
        return False

    time.sleep(retry_after + 0.2)
    return True


def notify_publish_status(chat_id, text):
    if not chat_id:
        return False
    return bool(safe_send_message(bot, chat_id, text, logger=logger))


def edit_channel_post_caption(post, caption, markup, action):
    try:
        call_telegram_with_retries(
            lambda: bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=post.message_id,
                caption=caption,
                reply_markup=markup,
            ),
            logger=logger,
            attempts=3,
            base_delay=CHANNEL_POST_SEND_RETRY_SECONDS,
            description=f"Telegram channel post {action} caption edit",
        )
        return True
    except Exception as exc:
        if is_message_not_modified_error(exc):
            logger.debug("Channel post %s already up to date for post_id=%s", action, post.id)
            return True
        log_channel_sync_error(f"{action} caption", post.id, exc)
        if is_message_to_edit_missing_error(exc):
            return False
        return None


def edit_channel_post_text(post, text, markup, action):
    try:
        call_telegram_with_retries(
            lambda: bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=post.message_id,
                text=text,
                reply_markup=markup,
            ),
            logger=logger,
            attempts=3,
            base_delay=CHANNEL_POST_SEND_RETRY_SECONDS,
            description=f"Telegram channel post {action} text edit",
        )
        return True
    except Exception as exc:
        if is_message_not_modified_error(exc):
            logger.debug("Channel post %s already up to date for post_id=%s", action, post.id)
            return True
        log_channel_sync_error(f"{action} text", post.id, exc)
        return False


def edit_channel_post_message(post, text, markup, action):
    caption_result = edit_channel_post_caption(post, text, markup, action)
    if caption_result is True or caption_result is False:
        return caption_result
    return edit_channel_post_text(post, text, markup, action)


def update_channel_post_message(post):
    if not post or not post.message_id:
        logger.debug("Channel post update skipped: missing post/message_id")
        return False

    caption = build_channel_post_caption(post)
    markup = build_channel_post_markup(post)
    return edit_channel_post_message(post, caption, markup, "update")


def disable_channel_post_reservation(post, reason="Товар снят с продажи"):
    if not post or not post.message_id:
        return False

    caption = build_unavailable_channel_post_caption(post, reason)
    markup = build_bot_only_markup()
    return edit_channel_post_message(post, caption, markup, "disable")


def delete_delivered_channel_post_message(
    post,
    source="delivery_cleanup",
    disable_reason="Товар уехал в доставку",
):
    if not post or not post.message_id:
        return True

    message_id = post.message_id
    try:
        call_telegram_with_retries(
            lambda: bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id),
            logger=logger,
            attempts=3,
            base_delay=CHANNEL_POST_SEND_RETRY_SECONDS,
            description="Telegram delivered channel post delete",
        )
    except Exception as exc:
        if not telegram_error_contains(exc, "message to delete not found"):
            log_channel_sync_error("delivered delete", post.id, exc)
            return disable_channel_post_reservation(post, reason=disable_reason)

    post.message_id = None
    logger.info(
        "Deleted delivered channel post for post_id=%s message_id=%s source=%s",
        post.id,
        message_id,
        source,
    )
    return True


def build_telegram_proxy_url():
    proxy_url = os.environ.get("TELEGRAM_PROXY_URL")
    if proxy_url:
        return proxy_url.strip()

    host = os.environ.get("TELEGRAM_PROXY_HOST")
    port = os.environ.get("TELEGRAM_PROXY_PORT")
    if not host or not port:
        return None

    scheme = os.environ.get("TELEGRAM_PROXY_SCHEME", "socks5").strip().lower()
    username = os.environ.get("TELEGRAM_PROXY_USERNAME")
    password = os.environ.get("TELEGRAM_PROXY_PASSWORD")

    auth = ""
    if username:
        auth = quote(username, safe="")
        if password is not None:
            auth = f"{auth}:{quote(password, safe='')}"
        auth = f"{auth}@"

    return f"{scheme}://{auth}{host}:{port}"


def sanitize_proxy_url(proxy_url):
    scheme, rest = proxy_url.split("://", 1)
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return f"{scheme}://{rest}"


def configure_telegram_proxy():
    proxy_url = build_telegram_proxy_url()
    if not proxy_url:
        return

    scheme = proxy_url.split("://", 1)[0].lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(
            f"Unsupported TELEGRAM_PROXY scheme '{scheme}'. "
            "Use http, https, socks5 or socks5h."
        )

    apihelper.proxy = {"http": proxy_url, "https": proxy_url}
    logger.info("Telegram proxy enabled: %s", sanitize_proxy_url(proxy_url))


def configure_telegram_api_retries():
    apihelper.RETRY_ON_ERROR = TELEGRAM_API_RETRY_ON_ERROR
    apihelper.MAX_RETRIES = max(1, TELEGRAM_API_MAX_RETRIES)
    apihelper.RETRY_TIMEOUT = max(0, TELEGRAM_API_RETRY_TIMEOUT_SECONDS)
    logger.info(
        "Telegram API retries configured: enabled=%s max_retries=%s retry_timeout=%s",
        apihelper.RETRY_ON_ERROR,
        apihelper.MAX_RETRIES,
        apihelper.RETRY_TIMEOUT,
    )


def configure_locale():
    for locale_name in ("ru_RU.UTF-8", "ru_RU", "Russian_Russia.1251"):
        try:
            locale.setlocale(locale.LC_TIME, locale_name)
            return
        except locale.Error:
            continue

    logger.warning("Russian locale is unavailable; using the system default locale.")


# Настройка бота и кэш
init_db()
configure_telegram_api_retries()
configure_telegram_proxy()
bot = telebot.TeleBot(TOKEN, exception_handler=TelegramPollingExceptionHandler())
user_messages = {}
user_pages = {}
PAGE_SIZE = 5
user_last_message_id = {}
last_bot_message = {}
user_data = PersistentBucket("user_data")
user_states = {}
temp_user_data = PersistentBucket("temp_user_data")
temp_post_data = PersistentBucket("temp_post_data")
last_start_time = {}
delivery_active = False
configure_locale()
active_audit = {}
reservation_auto_fulfill_started = False
reservation_auto_fulfill_stop_event = threading.Event()
reservation_auto_fulfill_lock = threading.Lock()
phoenix_broadcast_lock = threading.Lock()
channel_post_publish_lock = threading.Lock()
channel_post_auto_publish_started = False
channel_post_auto_publish_stop_event = threading.Event()
delivery_cleanup_started = False
delivery_cleanup_stop_event = threading.Event()
reserved_group_flow_lock = threading.Lock()
delivery_collection_report_lock = threading.Lock()
delivery_collection_report_target_group_id = DELIVERY_COLLECTION_REPORT_GROUP_ID
reservation_stats_report_started = False
reservation_stats_report_stop_event = threading.Event()
reservation_stats_live_lock = threading.Lock()
reservation_stats_live_sessions = {}
delivery_broadcast_monitor_started = False
delivery_broadcast_monitor_stop_event = threading.Event()
delivery_broadcast_scan_lock = threading.Lock()
payroll_sheet_sync_started = False
payroll_sheet_sync_stop_event = threading.Event()
payroll_sheet_sync_lock = threading.Lock()
delivery_sheet_retry_started = False
delivery_sheet_retry_stop_event = threading.Event()
delivery_sheet_sync_lock = threading.Lock()


def safe_answer_callback_query(*args, **kwargs):
    try:
        return bot.answer_callback_query(*args, **kwargs)
    except Exception as exc:
        error_text = sanitize_telegram_error_text(exc).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            logger.debug("Telegram callback answer skipped: %s", sanitize_telegram_error_text(exc))
        else:
            logger.warning("Telegram callback answer failed: %s", sanitize_telegram_error_text(exc))
        return None


def parse_callback_int_suffix(call, prefix, error_text="Некорректная кнопка."):
    data = str(getattr(call, "data", "") or "")
    if not data.startswith(prefix):
        safe_answer_callback_query(call.id, error_text, show_alert=True)
        return None

    suffix = data[len(prefix):]
    if not suffix.isdigit():
        safe_answer_callback_query(call.id, error_text, show_alert=True)
        return None

    return int(suffix)


def is_silent_blocked_user(user_id):
    if user_id is None:
        return False

    try:
        return is_user_silent_blocked(int(user_id))
    except Exception as exc:
        logger.warning("Silent blacklist check failed for user_id=%s: %s", user_id, exc)
        return False


def ignore_silent_blocked_message(message):
    user_id = getattr(getattr(message, "chat", None), "id", None)
    if not is_silent_blocked_user(user_id):
        return False

    logger.info("Silent blocked message ignored for user_id=%s", user_id)
    return True


def ignore_silent_blocked_callback(call):
    user_id = getattr(getattr(call, "from_user", None), "id", None)
    if not is_silent_blocked_user(user_id):
        return False

    logger.info(
        "Silent blocked callback ignored for user_id=%s data=%s",
        user_id,
        getattr(call, "data", None),
    )
    safe_answer_callback_query(call.id)
    return True


def message_from_user_id(message):
    return getattr(getattr(message, "from_user", None), "id", None)


def message_user_state_is(message, expected_state):
    user_id = message_from_user_id(message)
    return user_id is not None and get_user_state(user_id) == expected_state


def callback_data_startswith(call, prefixes):
    return str(getattr(call, "data", "") or "").startswith(prefixes)


def callback_message_chat_id(call):
    return getattr(getattr(getattr(call, "message", None), "chat", None), "id", None)


def callback_message_user_state_is(call, expected_state):
    chat_id = callback_message_chat_id(call)
    return chat_id is not None and get_user_state(chat_id) == expected_state


def require_message_user_id(message, context):
    user_id = message_from_user_id(message)
    if user_id is None:
        logger.warning("%s skipped: missing message.from_user", context)
        return None
    return user_id


def normalize_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"8{digits}"
    if len(digits) == 11 and digits.startswith("7"):
        return f"8{digits[1:]}"
    return digits


def phone_variants(phone):
    normalized = normalize_phone(phone)
    variants = {str(phone or "").strip(), normalized}
    if len(normalized) == 11 and normalized.startswith("8"):
        variants.add(f"7{normalized[1:]}")
        variants.add(f"+7{normalized[1:]}")
    return {variant for variant in variants if variant}


def get_phone_tail(phone, length=4):
    normalized = normalize_phone(phone)
    return normalized[-length:] if len(normalized) >= length else normalized


def has_role(user_id, roles):
    return get_client_role(user_id) in roles


def require_role(message, roles):
    user_id = require_message_user_id(message, "Role check")
    if user_id is None:
        return False
    if has_role(user_id, roles):
        return True

    safe_send_message(bot, message.chat.id, "У вас недостаточно прав для этой команды.", logger=logger)
    return False


def is_creator(user_id):
    return int(user_id) == ADMIN_USER_ID


def require_creator(message):
    user_id = require_message_user_id(message, "Creator check")
    if user_id is None:
        return False
    if is_creator(user_id):
        return True

    safe_send_message(bot, message.chat.id, "Эта команда доступна только создателю.", logger=logger)
    return False


def get_state_action(user_id):
    state = get_user_state(user_id)
    if isinstance(state, dict):
        return state.get("action")
    return None


def get_temp_user_data_or_reset(user_id, required_fields=None, notice_chat_id=None, notice_text=None):
    data = temp_user_data.get(user_id)
    if not isinstance(data, dict):
        data = {}

    missing_fields = [
        field
        for field in (required_fields or [])
        if field not in data or data.get(field) is None
    ]
    if not data or missing_fields:
        logger.warning(
            "Temporary user flow data missing for user_id=%s state=%s missing_fields=%s",
            user_id,
            get_user_state(user_id),
            missing_fields,
        )
        temp_user_data.pop(user_id, None)
        clear_user_state(user_id)
        if notice_chat_id and notice_text:
            safe_send_message(bot, notice_chat_id, notice_text, logger=logger)
        return None

    return data


def require_text_message(message, notice_text="Отправьте ответ текстовым сообщением."):
    text = getattr(message, "text", None)
    if text is None:
        safe_send_message(bot, message.chat.id, notice_text, logger=logger)
        return None
    return text


def get_related_clients_by_full_phone(user_id):
    client = Clients.get_row_by_user_id(user_id)
    if not client:
        return []
    return Clients.get_rows_by_phone(client.phone)


def get_related_user_ids_by_phone(phone):
    return [client.user_id for client in Clients.get_rows_by_phone(phone)]


def today_bounds(value=None):
    current = value or now_local()
    if current.tzinfo is not None:
        current = current.astimezone(SAMARA_TZ).replace(tzinfo=None)
    start = current.replace(hour=RESERVATION_STATS_DAY_START_HOUR, minute=0, second=0, microsecond=0)
    end = current.replace(hour=RESERVATION_STATS_DAY_END_HOUR, minute=0, second=0, microsecond=0)
    return start, end


def delivery_campaign_date(value=None):
    current = value or now_samara()
    if current.tzinfo is not None:
        current = current.astimezone(SAMARA_TZ)
    return current.date()


def delivery_campaign_cutoff_at(value=None):
    return datetime.combine(
        delivery_campaign_date(value),
        datetime_time(hour=DELIVERY_BROADCAST_CUTOFF_HOUR, minute=0),
    )


def is_delivery_campaign_before_cutoff(value=None, cutoff_at=None):
    current = value or now_local()
    if current.tzinfo is not None:
        current = current.astimezone(SAMARA_TZ).replace(tzinfo=None)
    return current < (cutoff_at or delivery_campaign_cutoff_at(current))


def delivery_response_deadline_label():
    return f"{DELIVERY_BROADCAST_CUTOFF_HOUR:02d}:00"


def add_reservation_stat_event(session, event_type, reservation, event_time=None):
    if not reservation or not getattr(reservation, "id", None):
        return False

    with session.no_autoflush:
        exists = session.query(ReservationStatEvent.id).filter(
            ReservationStatEvent.event_type == event_type,
            ReservationStatEvent.reservation_id == reservation.id,
        ).first()
    if exists:
        return False

    session.add(ReservationStatEvent(
        event_type=event_type,
        reservation_id=reservation.id,
        user_id=reservation.user_id,
        post_id=reservation.post_id,
        quantity=max(int(reservation.quantity or 0), 1),
        created_at=event_time or now_local(),
    ))
    return True


def record_reservation_stat_event(event_type, reservation, event_time=None):
    try:
        with Session(bind=engine) as session:
            added = add_reservation_stat_event(session, event_type, reservation, event_time=event_time)
            if added:
                session.commit()
            return added
    except Exception as exc:
        logger.warning(
            "Reservation stat event failed type=%s reservation_id=%s: %s",
            event_type,
            getattr(reservation, "id", None),
            exc,
        )
        return False


def get_reservation_stats_snapshot(value=None):
    start, end = today_bounds(value)
    with Session(bind=engine) as session:
        current_total = session.query(func.count(Reservations.id)).scalar() or 0
        today_created = session.query(func.count(ReservationStatEvent.id)).filter(
            ReservationStatEvent.event_type == RESERVATION_STATS_EVENT_CREATED,
            ReservationStatEvent.created_at >= start,
            ReservationStatEvent.created_at < end,
        ).scalar() or 0
        today_canceled = session.query(func.count(ReservationStatEvent.id)).filter(
            ReservationStatEvent.event_type == RESERVATION_STATS_EVENT_CANCELED,
            ReservationStatEvent.created_at >= start,
            ReservationStatEvent.created_at < end,
        ).scalar() or 0
        today_fulfilled = session.query(func.count(ReservationStatEvent.id)).filter(
            ReservationStatEvent.event_type == RESERVATION_STATS_EVENT_FULFILLED,
            ReservationStatEvent.created_at >= start,
            ReservationStatEvent.created_at < end,
        ).scalar() or 0
    return {
        "current_total": int(current_total),
        "today_created": int(today_created),
        "today_canceled": int(today_canceled),
        "today_fulfilled": int(today_fulfilled),
        "period_start": start,
        "period_end": end,
        "updated_at": value or now_local(),
    }


def build_reservation_stats_text(snapshot=None, title="Статистика броней"):
    snapshot = snapshot or get_reservation_stats_snapshot()
    return (
        f"{title}\n"
        f"Дата: {snapshot['updated_at'].strftime('%d.%m.%Y')}\n"
        f"Период: {snapshot['period_start'].strftime('%H:%M')}-{snapshot['period_end'].strftime('%H:%M')} по Самаре\n"
        f"Обновлено: {snapshot['updated_at'].strftime('%H:%M:%S')}\n\n"
        f"Броней сейчас: {snapshot['current_total']}\n"
        f"Броней сегодня всего: {snapshot['today_created']}\n"
        f"Отказались сегодня: {snapshot['today_canceled']}\n"
        f"Обработано сегодня: {snapshot['today_fulfilled']}"
    )


def build_reservation_stats_close_markup(user_id):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Закрыть", callback_data=f"reservation_stats_close_{user_id}"))
    return markup


def get_supreme_leader_user_ids():
    with Session(bind=engine) as session:
        rows = session.query(Clients.user_id).filter(Clients.role == "supreme_leader").all()
    return [row[0] for row in rows]


def send_daily_reservation_stats_report(target_date=None):
    snapshot = get_reservation_stats_snapshot(target_date or now_local())
    text = build_reservation_stats_text(snapshot, title="Ежедневная статистика броней")
    sent_count = 0
    for user_id in get_supreme_leader_user_ids():
        if safe_send_message(bot, user_id, text, logger=logger):
            sent_count += 1
            time.sleep(0.3)
    logger.info("Daily reservation stats report sent to supreme leaders: %s", sent_count)
    return sent_count


def get_reservation_stats_report_state():
    state = user_data.get(RESERVATION_STATS_REPORT_STATE_KEY, {})
    return state if isinstance(state, dict) else {}


def save_reservation_stats_report_state(state):
    user_data[RESERVATION_STATS_REPORT_STATE_KEY] = state


def should_send_reservation_stats_report(value):
    value = value.astimezone(SAMARA_TZ) if value.tzinfo else value.replace(tzinfo=SAMARA_TZ)
    report_at = value.replace(
        hour=RESERVATION_STATS_REPORT_HOUR,
        minute=RESERVATION_STATS_REPORT_MINUTE,
        second=0,
        microsecond=0,
    )
    return value >= report_at


def run_reservation_stats_daily_report(current=None):
    current = current or now_samara()
    if not should_send_reservation_stats_report(current):
        return False

    slot_key = current.date().isoformat()
    state = get_reservation_stats_report_state()
    if state.get("last_daily_report_date") == slot_key:
        return False

    send_daily_reservation_stats_report(current.replace(tzinfo=None))
    state["last_daily_report_date"] = slot_key
    state["last_daily_report_sent_at"] = serialize_datetime(now_local())
    save_reservation_stats_report_state(state)
    return True


def reservation_stats_daily_report_loop():
    while True:
        try:
            run_reservation_stats_daily_report()
        except Exception:
            logger.exception("Reservation stats daily report loop failed")

        if reservation_stats_report_stop_event.wait(RESERVATION_STATS_CHECK_SECONDS):
            return


def start_reservation_stats_daily_report_worker():
    global reservation_stats_report_started
    if reservation_stats_report_started:
        return

    reservation_stats_report_started = True
    thread = threading.Thread(
        target=reservation_stats_daily_report_loop,
        name="reservation-stats-daily-report",
        daemon=True,
    )
    thread.start()


def update_live_reservation_stats(user_id, chat_id, message_id):
    while True:
        time.sleep(10)
        with reservation_stats_live_lock:
            session_info = reservation_stats_live_sessions.get(user_id)
            if not session_info or session_info.get("message_id") != message_id:
                return

        try:
            call_telegram_with_retries(
                lambda: bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=build_reservation_stats_text(title="Живая статистика броней"),
                    reply_markup=build_reservation_stats_close_markup(user_id),
                ),
                logger=logger,
                attempts=3,
                description="Telegram live reservation stats edit",
            )
        except Exception as exc:
            if is_message_not_modified_error(exc):
                continue
            logger.warning("Live reservation stats update failed user_id=%s message_id=%s: %s", user_id, message_id, exc)


@bot.message_handler(func=lambda message: message.text == "БроньСтатистик")
def show_live_reservation_stats(message):
    user_id = require_message_user_id(message, "Live reservation statistic command")
    if user_id is None:
        return
    if get_client_role(user_id) != "supreme_leader":
        safe_send_message(bot, message.chat.id, "У вас нет прав доступа к этой функции.", logger=logger)
        return

    sent_message = safe_send_message(
        bot,
        message.chat.id,
        build_reservation_stats_text(title="Живая статистика броней"),
        reply_markup=build_reservation_stats_close_markup(user_id),
        logger=logger,
    )
    if not sent_message:
        return
    with reservation_stats_live_lock:
        reservation_stats_live_sessions[user_id] = {
            "chat_id": message.chat.id,
            "message_id": sent_message.message_id,
        }

    thread = threading.Thread(
        target=update_live_reservation_stats,
        args=(user_id, message.chat.id, sent_message.message_id),
        name=f"reservation-stats-live-{user_id}",
        daemon=True,
    )
    thread.start()


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "reservation_stats_close_"))
def close_live_reservation_stats(call):
    target_user_id = parse_callback_int_suffix(call, "reservation_stats_close_")
    if target_user_id is None:
        return

    if call.from_user.id != target_user_id or get_client_role(call.from_user.id) != "supreme_leader":
        safe_answer_callback_query(call.id, "У вас нет прав доступа.", show_alert=True)
        return

    safe_answer_callback_query(call.id)
    with reservation_stats_live_lock:
        reservation_stats_live_sessions.pop(target_user_id, None)

    chat_id = callback_message_chat_id(call)
    message_id = getattr(getattr(call, "message", None), "message_id", None)
    if chat_id is None or message_id is None:
        logger.warning("Live reservation stats close skipped: missing callback message")
        return

    try:
        call_telegram_with_retries(
            lambda: bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Статистика броней закрыта.",
                reply_markup=None,
            ),
            logger=logger,
            attempts=3,
            description="Telegram live reservation stats close edit",
        )
    except Exception as exc:
        if not is_message_not_modified_error(exc):
            logger.warning("Live reservation stats close edit failed: %s", exc)

    safe_send_message(
        bot,
        chat_id,
        "Главное меню",
        reply_markup=supreme_leader_main_menu(),
        logger=logger,
    )

# Получение неотправленных постов
@bot.message_handler(commands=["unsent_posts"])
def list_unsent_posts(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем роль пользователя
    if role not in POST_EDITOR_ROLES:
        safe_send_message(bot, user_id, "У вас нет прав доступа к этой функции.", logger=logger)
        return

    # Получаем неотправленные посты через метод класса
    try:
        unsent_posts = Posts.get_unsent_posts()
    except Exception as exc:
        logger.exception("Unsent posts lookup failed for user_id=%s: %s", user_id, exc)
        safe_send_message(bot, user_id, "Не удалось получить список неотправленных постов.", logger=logger)
        return

    # Формируем и отправляем ответ
    if unsent_posts:
        response = "📮 Неотправленные посты:\n"
        for post in unsent_posts:
            response += (
                f"ID: {post.id} | Цена: {post.price} ₽ | "
                f"Описание: {post.description} | Количество: {post.quantity}\n"
            )
        safe_send_message(bot, user_id, response, logger=logger)
    else:
        safe_send_message(bot, user_id, "✅ Все посты отправлены.", logger=logger)

# Инициализация старта бота
@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.chat.id

    # Получаем роль пользователя
    role = get_client_role(user_id)

    # Формируем персонализированное приветствие
    greetings = {
        "client": "Добро пожаловать в интерфейс бота, здесь вы можете просмотреть свою корзину или задать вопросы в чате поддержки.",
        "worker": "Давай за работу!",
        "audit": "Давай за работу!",
        "supreme_leader": "С возвращением, Повелитель!",
        "admin": "С возвращением в меню администратора",
    }
    greeting = greetings.get(role, "Привет, прошу пройти регистрацию")

    # Создаём инлайн-клавиатуру с кнопками
    inline_markup = InlineKeyboardMarkup()
    inline_markup.add(InlineKeyboardButton("В поддержку", url=support_link))
    inline_markup.add(InlineKeyboardButton("На канал", url=channel_link))
    inline_markup.add(InlineKeyboardButton("Правила", callback_data="rules"))  # Кнопка "Правила"

    # Определяем reply-клавиатуру по роли пользователя
    if role == "admin":
        reply_markup = admin_main_menu()
    elif role == "client":
        reply_markup = client_main_menu()
    elif role == "audit":
        reply_markup = audit_main_menu()
    elif role == "worker":
        reply_markup = worker_main_menu()
    elif role == "supreme_leader":
        reply_markup = supreme_leader_main_menu()
    else:
        reply_markup = unknown_main_menu()

    # Удаляем старые сообщения бота, если они существуют
    if user_id in last_bot_message:
        try:
            # Удаляем сообщение с приветствием
            safe_delete_message(
                bot, user_id, last_bot_message[user_id].get("greeting"), logger=logger
            )
        except Exception as e:
            logger.warning("Old greeting delete failed for user_id=%s: %s", user_id, e)
        try:
            # Удаляем сообщение с ресурсами
            safe_delete_message(
                bot, user_id, last_bot_message[user_id].get("resources"), logger=logger
            )
        except Exception as e:
            logger.warning("Old resources message delete failed for user_id=%s: %s", user_id, e)

    # Отправляем новое сообщение с приветствием
    try:
        sent_greeting = safe_send_message(bot, user_id, greeting, reply_markup=reply_markup, logger=logger)

        # Если роль клиента, отправляем сообщение с ресурсами
        if role == "client":
            sent_resources = safe_send_message(
                bot,
                user_id,
                "Посетите наши ресурсы:",
                reply_markup=inline_markup,
                logger=logger,
            )
        else:
            sent_resources = None

        # Сохраняем ID новых сообщений
        last_bot_message[user_id] = {
            "greeting": sent_greeting.message_id if sent_greeting else None,
            "resources": sent_resources.message_id if sent_resources else None,
        }
    except Exception as e:
        logger.warning("Start messages send failed for user_id=%s: %s", user_id, e)

    # Удаляем сообщение пользователя (команда /start)
    safe_delete_message(bot, user_id, message.message_id, logger=logger)



# Обработчик нажатия на кнопку "Правила"
@bot.callback_query_handler(func=lambda call: call.data == "rules")
def show_rules(call):
    rules_text = """
👋 Приветствуем, уважаемые подписчики!

На нашем канале вы можете легко приобрести товар, нажав кнопку «Забронировать».

📝 Как начать?

Для бронирования вам необходимо пройти предварительную регистрацию в нашем боте Биг Босс, указав в нём ваше имя и номер телефона.

🛒 Информация о товаре в постах

В каждом посте мы предоставляем всю необходимую информацию о товаре:

О товаре:

• Под фотографией всегда есть подробное описание, включая количество товара и возможные дефекты.

• Важно: Если товар имеет дефект, это будет обязательно указано.

Упаковка:

• Дефекты упаковки указываются только для скоропортящихся товаров и товаров личной гигиены.

🛡 Гарантия и возврат

Гарантия:

• На электротовары действует гарантия в течение 7 дней после покупки.

Возврат и обмен:

• Товары, купленные у нас, не подлежат возврату и обмену, за исключением:

• Одежды стоимостью более 1 500₽, если не подошёл размер (для уточнения деталей свяжитесь с поддержкой).

• Внимание! Одежда стоимостью до 1 500₽ возврату не подлежит. Рекомендуем тщательно изучить описание перед покупкой.

📐 Важная информация о размерах

Стоит обратить особое внимание на размер одежды и обуви. У большинства производителей своя размерная сетка, которая может «большемерить» или «маломерить».

В таких случаях мы указываем размер в сантиметрах, измеряя всю одежду и обувь. Если вы забронировали вещь/обувь, опираясь только на размер производителя и проигнорировав наши замеры в сантиметрах, в возврате будет отказано.

🚚 Согласование и график доставок

• Рассылка о приближающейся доставке приходит всем клиентам, зарегистрировавшимся в Биг Босс, чья корзина составляет 1 500₽ и более.

🗓 График ближайших доставок

Если рассылка пришла в:

СУББОТУ → доставка в ПОНЕДЕЛЬНИК

ВТОРНИК → доставка в СРЕДУ

ЧЕТВЕРГ → доставка в ПЯТНИЦУ

• Время доставки: с 10:00 до 16:00.

⚠️ Правила ответа боту

Бот присылает сообщение в формате:

«Имя клиента», готовы ли Вы принять ближайшую доставку (пн, ср, пт) с 10:00 до 16:00?

• Кнопка «Да»: Нажимайте только, если точно готовы принять БЛИЖАЙШУЮ доставку в указанный день.

• После нажатия «Да»: Немедленно отправьте ОДНИМ СООБЩЕНИЕМ всю информацию: полный адрес, ограничения по времени.

• Доставка только до подъезда. Если нужно поднять на этаж, договариваетесь с курьером на его усмотрение и за отдельную плату, которую назначит сам курьер.

• БОТ принимает от вас только ОДНО сообщение! Все последующие сообщения он нам не передаст.

• Кнопка «Нет»: Нажмите, если не готовы принять доставку в ближайшее время по графику.

❗️ Убедительная просьба: Не соглашайтесь на доставку (нажимая «Да») в субботнюю рассылку на понедельник, если вы не можете принять её в понедельник, но при этом пишете, что примете в пятницу. Нажимайте в таком случае кнопку «Нет».

📞 Что, если не ответили?

• Если вы не ответили боту, не увидели сообщение, отсутствовал интернет или нажали кнопку «Нет», с вами свяжется администратор для согласования доставки.

• В случае, если вы подтвердили доставку через Бот («Да» и адрес в 1 сообщении), вам звонить уже не будут.

📦 Бронирование и отмена

• ✅ Бронь уходит первому человеку, нажавшему кнопку «Забронировать».

• 🔄 После бронирования вы можете отменить товар до момента обработки заказа.

❗️ Важно: Если после обработки товар оказался в вашей корзине, отказ от него уже невозможен — потребуется полная расформировка.

💰 Условия доставки

• Оплата наличными курьеру в руки при получении вашего заказа.

• Так же можно оплатить переводом по карте +10% к сумме вашего заказа.

• Бесплатная доставка: Для заказов на сумму от 1 500₽.

• Платная доставка: Если сумма меньше, стоимость составит 350₽.

❗️ Если не приняли доставку

Если вы НЕ приняли доставку (не взяли трубку, проигнорировали звонки от курьера или администратора), заказ возвращается на склад.

• Следующая доставка для вас будет платная (+350 рублей к сумме вашего заказа, единоразово).

• Исключение: Вы заранее (утром) предупредили администратора о том, что не сможете принять доставку (изменились планы, нужно уехать и т.п.), чтобы курьер не тратил своё время и топливо.

🆘 Обратная связь

• Если вы обнаружили дефект, который не был указан в описании, свяжитесь с нашей поддержкой.

• Поддержка:
https://t.me/+rzsgGsfbyPgyM2My

• Телефон администратора:

📞 8 987 988 27 54

📞 8 996 620 13 78

📞 8 987 980 15 66

Мы всегда на связи и готовы помочь! 😊
"""

    # Telegram позволяет максимум 4096 символов в одном сообщении
    max_length = 4096

    for i in range(0, len(rules_text), max_length):
        bot.send_message(
            call.message.chat.id,
            rules_text[i:i + max_length],
            disable_web_page_preview=True
        )

    bot.answer_callback_query(call.id)


    # Создаём разметку с кнопкой "Назад"
    markup = InlineKeyboardMarkup()
    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")  # Callback для возврата
    markup.add(back_button)

    safe_answer_callback_query(call.id)
    try:
        # Редактируем текущее сообщение: добавляем текст и кнопку
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=rules_text,
            reply_markup=markup  # Кнопка для возврата
        )
    except Exception as e:
        logger.warning("Rules message edit failed for user_id=%s: %s", call.from_user.id, e)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_start")
def back_to_start(call):
    safe_answer_callback_query(call.id)
    try:
        # Удаляем текущее сообщение с правилами
        safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)

        # Отправляем уведомление о возврате
        notification_message = safe_send_message(
            bot,
            call.message.chat.id,
            "Вы вернулись в главное меню.",
            logger=logger,
        )

        # Используем таймер для удаления уведомления через 5 секунд
        if notification_message:
            threading.Timer(
                5.0,
                safe_delete_message,
                args=(bot, call.message.chat.id, notification_message.message_id),
                kwargs={"logger": logger},
            ).start()

        # Отправляем приветственное сообщение, вызывая handle_start
        handle_start(call.message)

    except Exception as e:
        logger.exception("Back to start failed for user_id=%s: %s", call.from_user.id, e)


# Хэндлер регистрации
@bot.message_handler(func=lambda message: message.text == "Регистрация")
def handle_registration(message):
    chat_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return

    # Проверяем, находится ли пользователь в черном списке
    if is_user_blacklisted(chat_id):
        safe_send_message(
            bot,
            chat_id,
            "К сожалению, вы не можете зарегистрироваться, так как находитесь в черном списке.",
            logger=logger,
        )
        return

    # Проверяем, зарегистрирован ли уже пользователь
    if Clients.get_row_by_user_id(chat_id):
        safe_send_message(bot, chat_id, "Вы уже зарегистрированы!", logger=logger)
        handle_start(message)
        return

    # Сохраняем состояние для шага регистрации имени
    set_user_state(chat_id, Registration.REGISTERING_NAME)
    safe_send_message(bot, chat_id, "Введите ваше имя:", logger=logger)

# Имя для регистрации
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == Registration.REGISTERING_NAME)
def handle_name_registration(message):
    chat_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return
    text = require_text_message(message, "Введите имя текстом.")
    if text is None:
        return
    user_name = text.strip()

    # Проверяем валидность имени
    if len(user_name) < 2:
        safe_send_message(bot, chat_id, "Имя слишком короткое. Пожалуйста, введите хотя бы 2 символа.", logger=logger)
        return

    # Сохраняем имя во временные данные
    if chat_id not in temp_user_data:
        temp_user_data[chat_id] = {}
    temp_user_data[chat_id]["name"] = user_name

    # Переход к следующему шагу - вводу номера
    set_user_state(chat_id, Registration.STARTED_REGISTRATION)
    safe_send_message(bot, chat_id, "Введите ваш номер телефона:", logger=logger)

# Номер для регистрации
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == Registration.STARTED_REGISTRATION)
def handle_phone_registration(message):
    chat_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return
    text = require_text_message(message, "Введите номер телефона текстом.")
    if text is None:
        return
    raw_phone = text.strip()
    cleaned_phone = normalize_phone(raw_phone)

    # Проверка валидности номер через регулярное выражение
    if is_phone_valid(cleaned_phone):
        # Проверяем наличие номера в базе
        existing_client = Clients.get_row_by_phone(cleaned_phone)
        if existing_client:
            # Номер уже существует
            safe_send_message(
                bot,
                chat_id,
                f"⚠️ Номер телефона {cleaned_phone} уже зарегистрирован. Вы уверены, что хотите его привязать к своему аккаунту? "
                "Эта операция уведомит текущего владельца номера.",
                reply_markup=create_yes_no_keyboard(),  # Генерируем клавиатуру
                logger=logger,
            )
            # Сохраняем телефон во временные данные для подтверждения
            if chat_id not in temp_user_data:
                temp_user_data[chat_id] = {}
            temp_user_data[chat_id]["phone"] = cleaned_phone
            set_user_state(chat_id, Registration.REGISTERING_PHONE)  # Переходим в состояние подтверждения номера
        else:
            # Если номер уникальный, завершаем регистрацию
            complete_registration(chat_id, cleaned_phone)
    else:
        safe_send_message(bot, chat_id, "❌ Введите корректный номер телефона. Например, +7XXXXXXXXXX", logger=logger)

# Валидация номера телефона
def is_phone_valid(phone):
    normalized = normalize_phone(phone)
    return len(normalized) == 11 and normalized.startswith("8")

# Подтверждение номера
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == Registration.REGISTERING_PHONE)
def confirm_phone_registration(message):
    chat_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return
    text = require_text_message(message, "Ответьте текстом: Да или Нет.")
    if text is None:
        return
    response = text.strip().lower()

    # Проверка временных данных (подстраховка)
    if chat_id not in temp_user_data or "phone" not in temp_user_data[chat_id]:
        safe_send_message(bot, chat_id, "❌ Ошибка: временные данные регистрации потеряны. Попробуйте снова.", logger=logger)
        clear_user_state(chat_id)
        return

    # Получаем телефон и имя из временных данных
    phone = temp_user_data[chat_id]["phone"]
    name = temp_user_data[chat_id].get("name", "Неизвестный")

    if response == "да":
        # Проверяем, существует ли уже пользователь с таким номером телефона
        existing_client = Clients.get_row_by_phone(phone)
        if existing_client:
            # Если телефон уже существует, все равно добавляем нового пользователя с этим телефоном.
            # Корзина объединяется через `resolve_user_id` автоматически.

            # Добавляем нового пользователя
            Clients.insert(
                user_id=chat_id,
                name=name,
                phone=phone,  # Используем тот же номер телефона
                role="client"  # Роль по умолчанию
            )

            # Сообщаем текущему владельцу
            safe_send_message(
                bot,
                existing_client.user_id,
                f"⚠️ Новый пользователь ({name}) зарегистрировался с номером телефона {phone}. "
                "Все заказы для этого телефона будут сохраняться в вашей корзине.",
                logger=logger,
            )

            # Оповещаем нового пользователя
            safe_send_message(
                bot,
                chat_id,
                f"✅ Вы успешно зарегистрированы! Корзина будет привязана к номеру {phone}, "
                "используемому несколькими пользователями.",
                reply_markup=types.ReplyKeyboardRemove(),
                logger=logger,
            )

            # Завершаем регистрацию (важно!)
            clear_user_state(chat_id)
            handle_start(message)
        else:
            # Если номер уникальный, завершаем обычную регистрацию
            complete_registration(chat_id, phone)
    elif response == "нет":
        # Если пользователь отказался, предлагаем ввести новый номер телефона
        safe_send_message(bot, chat_id, "❌ Регистрация номера отменена. Введите новый номер телефона:", logger=logger)
        set_user_state(chat_id, Registration.STARTED_REGISTRATION)
    else:
        # Если пользователь ввел что-то не то
        safe_send_message(
            bot,
            chat_id,
            "⚠️ Пожалуйста, введите *'Да'* для подтверждения или *'Нет'* для отказа.",
            parse_mode="Markdown",
            logger=logger,
        )

# Завершение регистрации
def complete_registration(chat_id, phone):
    """
    Завершает регистрацию. Если номер уже существует, все равно добавляет нового пользователя в базу.
    """
    # Получаем имя из временных данных
    name = temp_user_data.get(chat_id, {}).get("name", "Неизвестный")

    try:
        # Устанавливаем роль (по умолчанию 'client')
        role = "supreme_leader" if chat_id == ADMIN_USER_ID else "client"

        # Проверяем существующего пользователя с таким телефоном
        existing_client = Clients.get_row_by_phone(phone)

        if existing_client:
            # Если номер телефона существует, добавляем нового пользователя с этим номером
            Clients.insert(
                user_id=chat_id,
                name=name,
                phone=phone,  # Используем уже существующий телефон
                role=role  # Роль по умолчанию
            )

            # Уведомляем нового пользователя
            safe_send_message(
                bot,
                chat_id,
                f"✅ Вы успешно зарегистрированы. Ваш номер телефона {phone} уже используется, "
                "и ваша корзина будет объединена с текущими бронированиями.",
                reply_markup=types.ReplyKeyboardRemove(),
                logger=logger,
            )

            # Уведомляем первого владельца телефона (если важно)
            safe_send_message(
                bot,
                existing_client.user_id,
                f"⚠️ Новый пользователь ({name}) зарегистрировался под вашим номером телефона ({phone}). "
                "Бронирования будут объединены.",
                logger=logger,
            )

        else:
            # Если номер уникальный, продолжаем обычную регистрацию
            Clients.insert(
                user_id=chat_id,
                name=name,
                phone=phone,
                role=role  # Роль по умолчанию
            )

            safe_send_message(
                bot,
                chat_id,
                f"✅ Регистрация завершена! Ваш номер телефона {phone} сохранен.",
                reply_markup=types.ReplyKeyboardRemove(),
                logger=logger,
            )

        # Завершаем процесс регистрации
        clear_user_state(chat_id)
        handle_start(SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=None))

    except Exception as e:
        safe_send_message(bot, chat_id, "❌ Во время регистрации произошла ошибка. Попробуйте позже.", logger=logger)
        logger.exception("Registration finish failed for chat_id=%s: %s", chat_id, e)

# Создание клавиатуры да или нет для подтверждения номера
def create_yes_no_keyboard():
    """Генерирует клавиатуру для подтверждения"""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("Да"), types.KeyboardButton("Нет"))
    return markup

# Обработчик запроса бронирования
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "reserve_"))
def handle_reservation(call):
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    post_id = parse_callback_int_suffix(call, "reserve_")
    if post_id is None:
        return
    if is_user_blacklisted(user_id):
        safe_answer_callback_query(
            callback_query_id=call.id,
            text="Вы не можете бронировать товары, так как вы были заблокированы",
            show_alert=True,
        )
        return
    if not is_registered(user_id):
        safe_answer_callback_query(
            callback_query_id=call.id,
            text="Вы не зарегистрированы! Для регистрации перейдите в бота",
            show_alert=True,
        )
        return
    for attempt in range(1, RESERVATION_DB_RETRY_ATTEMPTS + 1):
        reservation_committed = False
        callback_answered = False
        with Session(bind=engine) as session:
            try:
                # Получаем текущий товар с блокировкой строки
                post = session.query(Posts).filter(Posts.id == post_id).with_for_update(nowait=True).first()
                if not post:
                    safe_answer_callback_query(
                        callback_query_id=call.id,
                        text="Товар больше недоступен.",
                        show_alert=True,
                    )
                    return

                if post.quantity <= 0:
                    # Проверка очереди
                    user_in_queue = session.query(TempReservations).filter(
                        and_(
                            TempReservations.user_id == user_id,
                            TempReservations.post_id == post_id,
                            TempReservations.temp_fulfilled == False
                        )
                    ).first()
                    if user_in_queue:
                        safe_answer_callback_query(
                            callback_query_id=call.id,
                            text="Вы уже стоите в очереди за этим товаром!",
                            show_alert=True,
                        )
                        return
                    # Добавляем в очередь
                    temp_reservation = TempReservations(
                        user_id=user_id,
                        post_id=post_id,
                        quantity=1,
                        temp_fulfilled=False
                    )
                    session.add(temp_reservation)
                    session.commit()
                    safe_answer_callback_query(
                        callback_query_id=call.id,
                        text="Вы добавлены в очередь на этот товар.",
                        show_alert=True,
                    )
                    return

                # Остаток и бронь фиксируются одной транзакцией.
                post.quantity -= 1
                reservation = Reservations(
                    user_id=user_id,
                    post_id=post_id,
                    quantity=1,
                    is_fulfilled=False,
                    old_price=post.price,
                    created_at=now_local(),
                )
                session.add(reservation)
                session.flush()
                reservation_id = reservation.id
                add_reservation_stat_event(
                    session,
                    RESERVATION_STATS_EVENT_CREATED,
                    reservation,
                    event_time=reservation.created_at,
                )
                session.commit()
                reservation_committed = True

                if post.quantity == 0:
                    safe_answer_callback_query(
                        callback_query_id=call.id,
                        text="Вы забронировали последний экземпляр товара!",
                        show_alert=True,
                    )
                else:
                    safe_answer_callback_query(
                        callback_query_id=call.id,
                        text="Вы забронировали товар!",
                        show_alert=True,
                    )
                callback_answered = True

                update_channel_post_message(post)

                client = session.query(Clients).filter(Clients.user_id == user_id).first()
                if client and send_reserved_group_message(session, reservation, post, client):
                    logger.debug("Reserved group message sent for reservation_id=%s", reservation_id)

                # Отправляем личное сообщение с фото товара, описанием и кнопкой отмены
                cancel_button = InlineKeyboardMarkup()
                cancel_button.add(
                    InlineKeyboardButton(
                        text="🚫 Это я не заказывал",
                        callback_data=f"cancel_order_{reservation.id}"

                    )
                )
                reservation_notice = (
                    f"Вы забронировали товар!\n\n"
                    f"🏷️ Название: {post.description}\n"
                    f"💲 Цена: {post.price} ₽\n"
                    f"Если это была ошибка, нажмите кнопку ниже."
                )
                try:
                    send_photo_or_text(
                        bot,
                        user_id,
                        post.photo,
                        reservation_notice,
                        reply_markup=cancel_button,
                    )
                except Exception as exc:
                    if telegram_error_contains(exc, "bot was blocked by the user", "forbidden"):
                        logger.info("Reservation DM skipped: bot is blocked by user_id=%s", user_id)
                    else:
                        logger.warning("Reservation DM photo failed for user_id=%s: %s", user_id, exc)
                        safe_send_message(
                            bot,
                            user_id,
                            reservation_notice,
                            logger=logger,
                            reply_markup=cancel_button,
                        )
                return
            except IntegrityError:
                session.rollback()
                safe_answer_callback_query(
                    callback_query_id=call.id,
                    text="Произошла ошибка при бронировании. Попробуйте снова.",
                    show_alert=True,
                )
                return
            except Exception as exc:
                if reservation_committed:
                    logger.warning(
                        "Reservation follow-up failed after commit for user_id=%s post_id=%s: %s",
                        user_id,
                        post_id,
                        exc,
                    )
                    if not callback_answered:
                        safe_answer_callback_query(
                            callback_query_id=call.id,
                            text="Бронь создана, но часть уведомлений не отправилась.",
                            show_alert=True,
                        )
                    return
                session.rollback()
                if is_transient_database_error(exc) and attempt < RESERVATION_DB_RETRY_ATTEMPTS:
                    logger.warning(
                        "Reservation transient database error user_id=%s post_id=%s attempt=%s/%s: %s",
                        user_id,
                        post_id,
                        attempt,
                        RESERVATION_DB_RETRY_ATTEMPTS,
                        exc,
                    )
                    sleep_before_database_retry(attempt)
                    continue
                logger.exception("Reservation failed for user_id=%s post_id=%s: %s", user_id, post_id, exc)
                safe_answer_callback_query(
                    callback_query_id=call.id,
                    text="Сервер занят. Попробуйте забронировать ещё раз через несколько секунд."
                    if is_transient_database_error(exc)
                    else "Произошла ошибка при бронировании. Попробуйте снова.",
                    show_alert=True,
                )
                return

# Получение бронирования пользователя
def get_user_reservations(user_id):
    """
    phone = normalize_phone(phone)
    Получение всех заказов текущего пользователя и других пользователей с тем же полным номером телефона.
    """
    # Получаем текущие данные пользователя
    client = Clients.get_row_by_user_id(user_id)
    if client is None:
        logger.debug("Client not found for user_id=%s while loading reservations", user_id)
        return []  # Пользователь не зарегистрирован

    # Находим всех пользователей с таким же полным номером телефона.
    related_clients = Clients.get_rows_by_phone(client.phone)
    if not related_clients:
        logger.debug("Related clients not found for user_id=%s phone=%s", user_id, client.phone)
        return []

    # Debug: какие пользователи найдены
    related_user_ids = [related_client.user_id for related_client in related_clients]

    # Собираем все бронирования для этих пользователей
    with Session(bind=engine) as session:
        reservations = session.query(Reservations).filter(
            Reservations.user_id.in_(related_user_ids)
        ).all()

    return reservations


def get_related_user_ids_by_full_phone(user_id):
    client = Clients.get_row_by_user_id(user_id)
    if not client:
        return []

    return [related.user_id for related in Clients.get_rows_by_phone(client.phone)]


def calculate_order_amount(order, post=None):
    fallback_price = post.price if post is not None else 0
    unit_price = order.old_price if order.old_price is not None else fallback_price
    return (unit_price * order.quantity) - (order.return_order or 0)


def reservation_unit_price(order, post=None):
    fallback_price = post.price if post is not None else 0
    return order.old_price if order.old_price is not None else fallback_price


def product_view_from_snapshot(snapshot):
    if not snapshot:
        return None

    return SimpleNamespace(
        id=snapshot.post_id,
        chat_id=snapshot.chat_id,
        photo=snapshot.photo,
        price=snapshot.price,
        description=snapshot.description,
        message_id=snapshot.message_id,
        quantity=snapshot.quantity,
        is_sent=snapshot.is_sent,
        created_at=snapshot.created_at,
        from_snapshot=True,
    )


def product_view_from_reservation(reservation):
    return SimpleNamespace(
        id=reservation.post_id,
        chat_id=None,
        photo=None,
        price=reservation.old_price or 0,
        description=f"Товар удалён из карточек (ID {reservation.post_id})",
        message_id=None,
        quantity=reservation.quantity,
        is_sent=False,
        created_at=reservation.created_at,
        from_reservation=True,
    )


def get_post_or_snapshot(session, post_id):
    post = session.query(Posts).filter(Posts.id == post_id).first()
    if post:
        return post

    snapshot = session.query(DeletedPostSnapshot).filter(
        DeletedPostSnapshot.post_id == post_id,
    ).first()
    return product_view_from_snapshot(snapshot)


def get_posts_or_snapshots_by_ids(session, post_ids):
    post_ids = {post_id for post_id in post_ids if post_id is not None}
    if not post_ids:
        return {}

    products_by_id = {
        post.id: post
        for post in session.query(Posts).filter(Posts.id.in_(post_ids)).all()
    }
    missing_ids = post_ids - set(products_by_id.keys())
    if missing_ids:
        for snapshot in session.query(DeletedPostSnapshot).filter(
            DeletedPostSnapshot.post_id.in_(missing_ids),
        ).all():
            products_by_id[snapshot.post_id] = product_view_from_snapshot(snapshot)

    return products_by_id


def format_datetime(value):
    if not value:
        return "не указано"
    return value.strftime("%d.%m.%Y %H:%M")


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def serialize_datetime(value):
    return value.isoformat() if isinstance(value, datetime) else value


def get_delivery_cutoff_at(delivery_entry):
    return delivery_entry.delivery_cutoff_at or LEGACY_DELIVERY_CUTOFF_AT


def build_reserved_group_caption(reservation, post, client):
    status = "✅ Обработан" if reservation.is_fulfilled else "⏳ В обработке"
    price = reservation_unit_price(reservation, post) if post else "Не указана"
    lines = [
        f"Id товара: {reservation.post_id}",
        f"👤 Клиент: {client.name if client and client.name else 'Имя не указано'}",
        f"📞 Телефон: {client.phone if client and client.phone else 'Телефон не указан'}",
        f"💰 Цена: {price}₽",
        f"📦 Описание: {post.description if post else 'Описание отсутствует'}",
        f"📅 Дата поста: {post.created_at.strftime('%d.%m') if post and post.created_at else 'Дата отсутствует'}",
        f"📦 Количество: {reservation.quantity}",
        f"🕒 Забронировано: {format_datetime(reservation.created_at)}",
        f"Статус: {status}",
    ]
    if reservation.fulfilled_at:
        lines.append(f"🕒 Обработано: {format_datetime(reservation.fulfilled_at)}")
    return "\n".join(lines)


def reservation_is_stale_for_current_post(reservation, post):
    if not reservation or not post:
        return False
    if not isinstance(post, Posts):
        return False
    if not reservation.created_at or not post.created_at:
        return False
    return reservation.created_at < post.created_at - POST_ID_REUSE_STALE_GRACE


def get_reserved_group_flow_state():
    state = user_data.get(RESERVED_GROUP_FLOW_STATE_KEY, {})
    return state if isinstance(state, dict) else {}


def save_reserved_group_flow_state(state):
    user_data[RESERVED_GROUP_FLOW_STATE_KEY] = state


def is_reserved_group_delivery_paused():
    state = get_reserved_group_flow_state()
    return bool(state.get("delivery_collection_paused"))


def activate_reserved_group_delivery_pause():
    with reserved_group_flow_lock:
        state = get_reserved_group_flow_state()
        if state.get("delivery_collection_paused"):
            return False

        state.update({
            "delivery_collection_paused": True,
            "paused_at": serialize_datetime(now_local()),
            "resume_flush_running": False,
        })
        save_reserved_group_flow_state(state)
        return True


def get_reserved_group_queue_count():
    with Session(bind=engine) as session:
        return session.query(func.count(Reservations.id)).filter(
            Reservations.reserved_group_message_id == None,
        ).scalar() or 0


def get_reserved_group_queued_reservation_ids(limit=RESERVED_GROUP_RESUME_BATCH_SIZE):
    with Session(bind=engine) as session:
        rows = session.query(Reservations.id).filter(
            Reservations.reserved_group_message_id == None,
        ).order_by(
            Reservations.created_at,
            Reservations.id,
        ).limit(limit).all()
    return [row[0] for row in rows]


def has_reserved_group_message(reservation):
    message_id = getattr(reservation, "reserved_group_message_id", None)
    return bool(message_id and message_id > 0)


def mark_reserved_group_message_skipped(session, reservation, reason):
    reservation.reserved_group_message_id = RESERVED_GROUP_MESSAGE_SKIPPED
    session.commit()
    logger.warning(
        "Reserved group message skipped for reservation_id=%s post_id=%s: %s",
        reservation.id,
        reservation.post_id,
        reason,
    )


def reserved_group_resume_delay(remaining_count=None):
    return RESERVED_GROUP_SEND_INTERVAL_SECONDS


def clear_reserved_group_delivery_pause(sent_count=0):
    with reserved_group_flow_lock:
        state = get_reserved_group_flow_state()
        state.update({
            "delivery_collection_paused": False,
            "resume_flush_running": False,
            "resumed_at": serialize_datetime(now_local()),
            "last_resume_sent_count": sent_count,
        })
        save_reserved_group_flow_state(state)


def mark_reserved_group_flush_stopped():
    with reserved_group_flow_lock:
        state = get_reserved_group_flow_state()
        state["resume_flush_running"] = False
        save_reserved_group_flow_state(state)


def flush_reserved_group_queue_after_delivery(admin_chat_id=None):
    sent_count = 0
    try:
        while True:
            queued_ids = get_reserved_group_queued_reservation_ids()
            if not queued_ids:
                time.sleep(0.5)
                if get_reserved_group_queue_count():
                    continue
                clear_reserved_group_delivery_pause(sent_count)
                logger.info("Reserved group flow resumed after delivery collection. Sent queued=%s", sent_count)
                return

            for reservation_id in queued_ids:
                with Session(bind=engine) as session:
                    reservation = session.query(Reservations).filter(
                        Reservations.id == reservation_id,
                        Reservations.reserved_group_message_id == None,
                    ).first()
                    if not reservation:
                        continue

                    post = get_post_or_snapshot(session, reservation.post_id)
                    client = session.query(Clients).filter(Clients.user_id == reservation.user_id).first()
                    if not post:
                        mark_reserved_group_message_skipped(
                            session,
                            reservation,
                            "post and deleted snapshot are missing",
                        )
                        continue
                    if reservation_is_stale_for_current_post(reservation, post):
                        mark_reserved_group_message_skipped(
                            session,
                            reservation,
                            "reservation predates the current post with the same ID",
                        )
                        continue
                    if not send_reserved_group_message(session, reservation, post, client, force=True):
                        raise RuntimeError(f"queued reserved message send failed for reservation_id={reservation_id}")
                    sent_count += 1

                remaining_count = get_reserved_group_queue_count()
                if remaining_count:
                    time.sleep(reserved_group_resume_delay(remaining_count))
    except Exception as exc:
        logger.exception("Reserved group resume flush failed: %s", exc)
        mark_reserved_group_flush_stopped()
        if admin_chat_id:
            safe_send_message(
                bot,
                admin_chat_id,
                "Не удалось полностью возобновить поток забронированного товара. "
                "Новые брони пока остаются в очереди.",
                logger=logger,
            )


def start_reserved_group_resume_flush_if_delivery_done(admin_chat_id=None, recover_stale=False):
    with Session(bind=engine) as session:
        remaining_delivery_count = session.query(func.count(ForDelivery.id)).scalar() or 0
    if remaining_delivery_count > 0:
        return False

    with reserved_group_flow_lock:
        state = get_reserved_group_flow_state()
        if not state.get("delivery_collection_paused"):
            return False
        if state.get("resume_flush_running"):
            if not recover_stale:
                return False
            logger.warning("Recovering stale reserved group resume flag after process start")

        resumed_at = serialize_datetime(now_local())
        state["delivery_collection_paused"] = False
        state["resume_flush_running"] = True
        state["resume_flush_started_at"] = resumed_at
        state["resumed_at"] = resumed_at
        save_reserved_group_flow_state(state)

    thread = threading.Thread(
        target=flush_reserved_group_queue_after_delivery,
        args=(admin_chat_id,),
        name="reserved-group-resume-flush",
        daemon=True,
    )
    thread.start()
    return True


def truncate_telegram_caption(caption, limit=1024):
    caption = str(caption or "")
    if len(caption) <= limit:
        return caption

    separator = "\n...\n"
    tail_length = min(360, max(0, limit - len(separator)))
    head_length = max(0, limit - len(separator) - tail_length)
    return f"{caption[:head_length].rstrip()}{separator}{caption[-tail_length:].lstrip()}"


def edit_reserved_group_message(message_id, post, caption):
    if not message_id:
        return False

    try:
        if post and post.photo:
            call_telegram_with_retries(
                lambda: bot.edit_message_caption(
                    chat_id=TARGET_GROUP_ID,
                    message_id=message_id,
                    caption=truncate_telegram_caption(caption),
                    reply_markup=None,
                ),
                logger=logger,
                attempts=3,
                description="Telegram reserved group caption edit",
            )
        else:
            call_telegram_with_retries(
                lambda: bot.edit_message_text(
                    chat_id=TARGET_GROUP_ID,
                    message_id=message_id,
                    text=caption,
                    reply_markup=None,
                ),
                logger=logger,
                attempts=3,
                description="Telegram reserved group text edit",
            )
        return True
    except Exception as exc:
        if is_message_not_modified_error(exc):
            logger.debug("Reserved group message already up to date: %s", exc)
            return True

    try:
        call_telegram_with_retries(
            lambda: bot.edit_message_text(
                chat_id=TARGET_GROUP_ID,
                message_id=message_id,
                text=caption,
                reply_markup=None,
            ),
            logger=logger,
            attempts=3,
            description="Telegram reserved group fallback text edit",
        )
        return True
    except Exception as exc:
        if is_message_not_modified_error(exc):
            return True
        logger.warning("Reserved group message update failed for message_id=%s: %s", message_id, exc)
        return False


def send_reserved_group_message(session, reservation, post, client, force=False, attempts=3):
    if not force and is_reserved_group_delivery_paused():
        logger.debug("Reserved group message queued while delivery collection is active: reservation_id=%s", reservation.id)
        return False

    caption = build_reserved_group_caption(reservation, post, client)
    for attempt in range(1, attempts + 1):
        try:
            message = send_photo_or_text(bot, TARGET_GROUP_ID, post.photo if post else None, caption)
            return store_reserved_group_message_id(reservation.id, message.message_id)
        except Exception as exc:
            retry_after = telegram_retry_after_seconds(exc)
            if retry_after is not None and attempt < attempts:
                wait_seconds = max(retry_after + 1, RESERVED_GROUP_SEND_INTERVAL_SECONDS)
                logger.info(
                    "Reserved group message rate-limited for reservation_id=%s retry_after=%ss wait=%ss",
                    reservation.id,
                    retry_after,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            logger.warning(
                "Reserved group message send failed for reservation_id=%s attempt=%s/%s: %s",
                reservation.id,
                attempt,
                attempts,
                exc,
            )
            return False

    return False


def store_reserved_group_message_id(reservation_id, message_id, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            with Session(bind=engine) as db_session:
                reservation = db_session.query(Reservations).filter(Reservations.id == reservation_id).first()
                if not reservation:
                    logger.warning("Reserved group message id skipped: reservation_id=%s not found", reservation_id)
                    return False
                reservation.reserved_group_message_id = message_id
                db_session.commit()
                return True
        except SQLAlchemyError as exc:
            logger.warning(
                "Reserved group message id update failed for reservation_id=%s attempt=%s/%s: %s",
                reservation_id,
                attempt,
                attempts,
                exc,
            )
            time.sleep(0.3 * attempt)
    return False


def update_reserved_group_message_by_id(reservation_id):
    with Session(bind=engine) as session:
        reservation = session.query(Reservations).filter(Reservations.id == reservation_id).first()
        if not reservation or not has_reserved_group_message(reservation):
            return False
        post = get_post_or_snapshot(session, reservation.post_id)
        client = session.query(Clients).filter(Clients.user_id == reservation.user_id).first()
        caption = build_reserved_group_caption(reservation, post, client)
        return edit_reserved_group_message(reservation.reserved_group_message_id, post, caption)


def mark_reserved_group_message_canceled(reservation, post, client):
    if not has_reserved_group_message(reservation):
        return False
    caption = (
        f"{build_reserved_group_caption(reservation, post, client)}\n"
        f"❌ Заказ отменён клиентом: {format_datetime(now_local())}"
    )
    return edit_reserved_group_message(reservation.reserved_group_message_id, post, caption)


def get_delivery_reservations_query(session, related_user_ids, cutoff_at):
    query = session.query(Reservations).filter(
        Reservations.user_id.in_(related_user_ids),
        Reservations.is_fulfilled == True,
    )
    if cutoff_at:
        query = query.filter(
            or_(Reservations.fulfilled_at == None, Reservations.fulfilled_at <= cutoff_at),
        )
    return query


def ensure_temp_fulfilled_for_reservation(session, reservation):
    post = get_post_or_snapshot(session, reservation.post_id)
    client = session.query(Clients).filter(Clients.user_id == reservation.user_id).first()
    if not post or not client:
        return False
    if reservation_is_stale_for_current_post(reservation, post):
        reservation.reserved_group_message_id = RESERVED_GROUP_MESSAGE_SKIPPED
        logger.warning(
            "Auto-fulfill skipped stale reservation_id=%s post_id=%s reservation_created_at=%s post_created_at=%s",
            reservation.id,
            reservation.post_id,
            reservation.created_at,
            post.created_at,
        )
        return False

    amount = calculate_order_amount(reservation, post)
    fulfilled_at = reservation.fulfilled_at or now_local()
    existing = session.query(Temp_Fulfilled).filter(
        Temp_Fulfilled.reservation_id == reservation.id,
    ).first()

    if existing:
        existing.user_name = client.name
        existing.item_description = post.description
        existing.quantity = reservation.quantity
        existing.price = amount
        existing.created_at = fulfilled_at
    else:
        session.add(Temp_Fulfilled(
            reservation_id=reservation.id,
            post_id=reservation.post_id,
            user_id=reservation.user_id,
            user_name=client.name,
            item_description=post.description,
            quantity=reservation.quantity,
            price=amount,
            created_at=fulfilled_at,
        ))

    reservation.is_fulfilled = True
    reservation.fulfilled_at = fulfilled_at
    add_reservation_stat_event(session, RESERVATION_STATS_EVENT_FULFILLED, reservation, event_time=fulfilled_at)
    return True


def auto_fulfill_expired_reservations(now=None, older_than_seconds=RESERVATION_AUTO_FULFILL_SECONDS):
    if not reservation_auto_fulfill_lock.acquire(blocking=False):
        logger.debug("Auto-fulfill skipped: another auto-fulfill run is active")
        return 0

    now = now or now_local()
    deadline = now - timedelta(seconds=older_than_seconds)

    try:
        with Session(bind=engine) as session:
            expired_reservations = session.query(Reservations).filter(
                Reservations.is_fulfilled == False,
                or_(
                    Reservations.reserved_group_message_id == None,
                    Reservations.reserved_group_message_id != RESERVED_GROUP_MESSAGE_SKIPPED,
                ),
                or_(
                    Reservations.created_at == None,
                    Reservations.created_at <= deadline,
                ),
            ).with_for_update(skip_locked=True).all()

            fulfilled_count = 0
            fulfilled_ids = []
            for reservation in expired_reservations:
                if ensure_temp_fulfilled_for_reservation(session, reservation):
                    fulfilled_count += 1
                    fulfilled_ids.append(reservation.id)

            if fulfilled_count:
                session.commit()

            for reservation_id in fulfilled_ids:
                update_reserved_group_message_by_id(reservation_id)

            return fulfilled_count
    finally:
        reservation_auto_fulfill_lock.release()


def reservation_auto_fulfill_loop():
    while True:
        try:
            fulfilled_count = auto_fulfill_expired_reservations()
            if fulfilled_count:
                logger.info("Auto-fulfilled %s expired reservations", fulfilled_count)
        except Exception:
            logger.exception("Failed to auto-fulfill expired reservations")

        if reservation_auto_fulfill_stop_event.wait(RESERVATION_AUTO_FULFILL_CHECK_SECONDS):
            return


def start_reservation_auto_fulfill_worker():
    global reservation_auto_fulfill_started
    if reservation_auto_fulfill_started:
        return

    reservation_auto_fulfill_started = True
    thread = threading.Thread(
        target=reservation_auto_fulfill_loop,
        name="reservation-auto-fulfill",
        daemon=True,
    )
    thread.start()

# Обработчик моих забронированных товаров
@bot.message_handler(commands=["my_reservations"])
def show_reservations(message):
    user_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return

    query = Clients.get_row(user_id=user_id)
    # Проверка регистрации пользователя
    # if not is_registered(user_id):
    if query is None:
        msg = safe_send_message(
            bot,
            user_id,
            "Вы не зарегистрированы! Для регистрации используйте команду /start register.",
            logger=logger,
        )
        if msg:
            user_messages[user_id] = [msg.message_id]
        return

    # Получаем заказы пользователя
    reservations = get_user_reservations(user_id)

    if reservations:
        for idx, reservation in enumerate(reservations, start=1):
            with Session(bind=engine) as session:
                post = get_post_or_snapshot(session, reservation.post_id)
            if not post:
                continue

            status = "✅ Положено" if reservation.is_fulfilled else "⏳ Ожидает выполнения"
            amount = calculate_order_amount(reservation, post)
            unit_price = reservation_unit_price(reservation, post)

            # Формируем описание
            caption = (
                f"{idx}. Описание: {post.description}\n"
                f"💰 Цена: {unit_price}₽ x {reservation.quantity} = {amount}₽\n"
                f"Статус: {status}"
            )

            if post.photo:
                sent_photo = safe_send_photo(bot, user_id, post.photo, caption=caption, logger=logger)
                if sent_photo:
                    user_messages.setdefault(user_id, []).append(sent_photo.message_id)
                else:
                    sent_message = safe_send_message(
                        bot,
                        user_id,
                        f"{caption}\n\nНе удалось показать фотографию товара.",
                        logger=logger,
                    )
                    if sent_message:
                        user_messages.setdefault(user_id, []).append(sent_message.message_id)
            else:
                sent_message = safe_send_message(bot, user_id, caption, logger=logger)
                if sent_message:
                    user_messages.setdefault(user_id, []).append(sent_message.message_id)
    else:
        safe_send_message(bot, user_id, "У вас пока нет заказов.", logger=logger)

# Хэндлер для обработки нажатий на заказ
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "order_"))
def order_details(call):
    if ignore_silent_blocked_callback(call):
        return

    callback_answered = False
    reservation_id = parse_callback_int_suffix(call, "order_")
    if reservation_id is None:
        return

    try:
        # Получаем информацию о заказе через ORM
        order = Reservations.get_row_by_id(reservation_id)
        if not order:
            safe_answer_callback_query(call.id, "Заказ не найден.", show_alert=True)
            return

        related_user_ids = get_related_user_ids_by_full_phone(call.from_user.id)
        if order.user_id not in related_user_ids:
            safe_answer_callback_query(call.id, "Заказ не найден или не принадлежит вам.", show_alert=True)
            return

        # Получаем живую карточку или snapshot удаленного поста.
        with Session(bind=engine) as session:
            post = get_post_or_snapshot(session, order.post_id)
        if not post:
            post = product_view_from_reservation(order)

        status = "✔️ Обработан" if order.is_fulfilled else "⌛ В обработке"
        amount = calculate_order_amount(order, post)
        unit_price = reservation_unit_price(order, post)
        caption = (
            f"Цена: {unit_price} ₽ x {order.quantity} = {amount} ₽\n"
            f"Описание: {post.description}\n"
            f"Статус: {status}"
        )
        # Создаём кнопки возврата или отмены
        markup = InlineKeyboardMarkup()
        back_btn = InlineKeyboardButton("⬅️ Назад", callback_data="my_orders")
        markup.add(back_btn)
        # Добавляем кнопку отмены, если заказ ещё не обработан
        if not order.is_fulfilled:
            cancel_btn = InlineKeyboardButton("❌ Отказаться", callback_data=f"cancel_order_{reservation_id}")
            markup.add(cancel_btn)

        safe_answer_callback_query(call.id)
        callback_answered = True

        # Обновляем сообщение с деталями заказа
        if post.photo:
            edit_message_media_with_retries(
                bot,
                logger=logger,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                media=InputMediaPhoto(media=post.photo, caption=caption),
                reply_markup=markup,
            )
        else:
            safe_edit_message_text(
                bot,
                logger=logger,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=caption,
                reply_markup=markup,
            )
    except Exception as e:
        if is_message_not_modified_error(e):
            logger.debug("Order details were already up to date for user_id=%s reservation_id=%s", call.from_user.id, reservation_id)
            if not callback_answered:
                safe_answer_callback_query(call.id)
            return
        logger.exception("Order details display failed for user_id=%s: %s", call.from_user.id, e)
        if callback_answered:
            safe_send_message(bot, call.message.chat.id, "Не удалось открыть заказ. Попробуйте ещё раз.", logger=logger)
        else:
            safe_answer_callback_query(call.id, "Произошла ошибка.", show_alert=True)


def cancel_unfulfilled_reservation(reservation_id, related_user_ids):
    related_user_ids = set(related_user_ids or [])
    queued_notifications = []
    canceled_group_update = None
    post_id = None
    channel_update_needed = False

    with Session(bind=engine) as session:
        reservation = session.query(Reservations).filter(
            Reservations.id == reservation_id,
        ).with_for_update().first()
        if not reservation:
            return {"status": "not_found"}
        if reservation.user_id not in related_user_ids:
            return {"status": "forbidden"}
        if reservation.is_fulfilled:
            return {"status": "fulfilled"}

        post = session.query(Posts).filter(
            Posts.id == reservation.post_id,
        ).with_for_update().first()
        if not post:
            return {"status": "post_missing"}

        post_id = reservation.post_id
        post_photo = post.photo
        quantity_to_release = max(int(reservation.quantity or 0), 1)
        order_client = session.query(Clients).filter(
            Clients.user_id == reservation.user_id,
        ).first()

        if has_reserved_group_message(reservation):
            canceled_group_update = (
                reservation.reserved_group_message_id,
                post_photo,
                (
                    f"{build_reserved_group_caption(reservation, post, order_client)}\n"
                    f"❌ Заказ отменён клиентом: {format_datetime(now_local())}"
                ),
            )

        add_reservation_stat_event(
            session,
            RESERVATION_STATS_EVENT_CANCELED,
            reservation,
            event_time=now_local(),
        )

        session.delete(reservation)

        queued_notification = promote_next_queue_entry(session, post, lock=True)
        if queued_notification:
            queued_notifications.append(queued_notification)
            result_status = "queued"
        else:
            post.quantity = int(post.quantity or 0) + quantity_to_release
            channel_update_needed = True
            result_status = "returned"

        session.commit()

    if canceled_group_update:
        message_id, post_photo, caption = canceled_group_update
        edit_reserved_group_message(message_id, SimpleNamespace(photo=post_photo), caption)

    if channel_update_needed and post_id:
        updated_post = Posts.get_row_by_id(post_id)
        if not update_channel_post_message(updated_post):
            logger.warning("Channel post was not refreshed after cancellation for post_id=%s", post_id)

    if queued_notifications:
        send_queue_transfer_notifications(queued_notifications)

    return {"status": result_status, "post_id": post_id}

# Отображает список заказов
@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def show_my_orders(call):
    if ignore_silent_blocked_callback(call):
        return

    message = call.message
    my_orders(message)  # Вызываем my_orders, передаём исходное сообщение
    safe_answer_callback_query(call.id)  # Подтверждаем обработку нажатия

# Обработчик функции Мои заказы
@bot.message_handler(func=lambda message: message.text == "🛒 Мои заказы")
def my_orders(message):
    user_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return

    # Сначала удаляем сообщение пользователя
    safe_delete_message(bot, user_id, message.message_id, logger=logger)

    try:
        # Удаляем предыдущее сообщение бота, если оно есть
        if user_id in user_last_message_id:
            safe_delete_message(bot, user_id, user_last_message_id[user_id], logger=logger)

        # Проверяем, зарегистрирован ли пользователь
        current_user = Clients.get_row_by_user_id(user_id)
        if not current_user:
            sent_message = safe_send_message(bot, user_id, "❌ Вы не зарегистрированы.", logger=logger)
            if sent_message:
                user_last_message_id[user_id] = sent_message.message_id  # Сохраняем ID последнего сообщения
            return

        # Получаем заказы всех связанных пользователей
        orders = get_user_reservations(user_id)

        if orders:
            user_pages[user_id] = 0  # Устанавливаем текущую страницу на первую
            sent_message = send_order_page(user_id=user_id, message_id=None, orders=orders, page=user_pages[user_id])
            if sent_message:
                user_last_message_id[user_id] = sent_message.message_id  # Сохраняем ID последнего сообщения
        else:
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(InlineKeyboardButton(text="На канал", url=channel_link))
            sent_message = safe_send_message(
                bot,
                user_id,
                "У вас пока нет заказов. Начните покупки, перейдя на наш канал.",
                reply_markup=keyboard,
                logger=logger,
            )
            if sent_message:
                user_last_message_id[user_id] = sent_message.message_id  # Сохраняем ID последнего сообщения
    except Exception as ex:
        logger.exception("My orders command failed for user_id=%s: %s", user_id, ex)


def edit_or_send_text_page(chat_id, message_id, text, keyboard, description):
    if message_id:
        try:
            return call_telegram_with_retries(
                lambda: bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=keyboard,
                ),
                logger=logger,
                attempts=3,
                description=f"Telegram {description} text edit",
            )
        except Exception as exc:
            if is_message_not_modified_error(exc):
                logger.debug("%s text page is already up to date for chat=%s", description, chat_id)
                return SimpleNamespace(message_id=message_id)
            logger.warning(
                "%s text page edit failed for chat=%s message_id=%s: %s",
                description,
                chat_id,
                message_id,
                exc,
            )

    return safe_send_message(bot, chat_id, text, reply_markup=keyboard, logger=logger)


# Создает страницу с заказами
def send_order_page(user_id, message_id, orders, page):
    orders_per_page = 5  # Количество заказов на одной странице
    start = page * orders_per_page
    end = start + orders_per_page
    total_pages = (len(orders) - 1) // orders_per_page + 1
    selected_orders = orders[start:end]

    posts_by_id = {}
    total_sum_all = 0
    total_sum_fulfilled = 0
    with Session(bind=engine) as session:
        posts_by_id = get_posts_or_snapshots_by_ids(
            session,
            {order.post_id for order in orders},
        )

    for order in orders:
        post = posts_by_id.get(order.post_id) or product_view_from_reservation(order)
        posts_by_id[order.post_id] = post
        amount = calculate_order_amount(order, post)
        total_sum_all += amount
        if order.is_fulfilled:
            total_sum_fulfilled += amount

    # Формирование текста для страницы. Колонки: описание, цена, статус заказа.
    text = f"Ваши заказы (стр. {page + 1} из {total_pages}):\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)

    for order in selected_orders:
        post = posts_by_id.get(order.post_id) or product_view_from_reservation(order)
        status = "✅В корзине" if order.is_fulfilled else "⏳В обработке"
        amount = calculate_order_amount(order, post)
        keyboard.add(InlineKeyboardButton(
            text=truncate_button_text(f"({status})- {amount} ₽ - {post.description}", 64),
            callback_data=f"order_{order.id}"
        ))

    # Добавляем строки с общей суммой заказов и суммой выполненных заказов
    text += f"\nОбщая сумма заказов: {total_sum_all} ₽"
    text += f"\nОбщая сумма обработанных заказов: {total_sum_fulfilled} ₽\n"

    # Навигация по страницам
    if page > 0:
        keyboard.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"orders_page_{page - 1}"))
    if end < len(orders):
        keyboard.add(InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"orders_page_{page + 1}"))

    try:
        with open(ORDER_CART_IMAGE_PATH, "rb") as photo:
            if message_id:
                try:
                    return edit_message_media_with_retries(
                        bot,
                        logger=logger,
                        chat_id=user_id,
                        message_id=message_id,
                        media=InputMediaPhoto(photo, caption=text),
                        reply_markup=keyboard,
                    )
                except Exception as exc:
                    if is_message_not_modified_error(exc):
                        logger.debug("Orders page was already up to date for user=%s page=%s", user_id, page)
                        return SimpleNamespace(message_id=message_id)
                    if is_message_to_edit_missing_error(exc):
                        logger.info(
                            "Orders page message missing for user=%s page=%s; sending a new one",
                            user_id,
                            page,
                        )
                        photo.seek(0)
                        try:
                            return send_photo_or_text(bot, user_id, photo, text, reply_markup=keyboard)
                        except Exception as send_exc:
                            logger.warning(
                                "Orders page replacement media send failed for user=%s page=%s: %s",
                                user_id,
                                page,
                                send_exc,
                            )
                    else:
                        logger.warning(
                            "Orders page media edit failed for user=%s page=%s: %s",
                            user_id,
                            page,
                            exc,
                        )
            else:
                try:
                    return send_photo_or_text(bot, user_id, photo, text, reply_markup=keyboard)
                except Exception as exc:
                    logger.warning("Orders page media send failed for user=%s page=%s: %s", user_id, page, exc)
    except OSError as exc:
        logger.warning("Orders page image unavailable path=%s: %s", ORDER_CART_IMAGE_PATH, exc)

    return edit_or_send_text_page(user_id, message_id, text, keyboard, "orders")

# Обработчик навигации между страницами
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "orders_page_"))
def paginate_orders(call):
    if ignore_silent_blocked_callback(call):
        return

    user_id = call.message.chat.id
    message_id = call.message.message_id
    page = parse_callback_int_suffix(call, "orders_page_", "Некорректная страница.")
    if page is None:
        return

    # Получаем заказы пользователя и связанных клиентов
    orders = get_user_reservations(user_id)

    # Отправляем страницу с заказами
    try:
        new_message = send_order_page(user_id=user_id, message_id=message_id, orders=orders, page=page)
        if new_message:
            user_last_message_id[user_id] = new_message.message_id  # Обновляем последний ID
    except Exception as e:
        logger.warning("Order pagination failed for user_id=%s page=%s: %s", user_id, page, e)
    finally:
        safe_answer_callback_query(call.id)  # Подтверждаем успешную обработку

# Обработка отмены заказа
@bot.callback_query_handler(
    func=lambda call: callback_data_startswith(call, ("cancel_order_", "cancel_reservation_"))
)
def cancel_reservation(call):
    if ignore_silent_blocked_callback(call):
        return

    logger.debug("Cancel reservation callback received: %s", call.data)
    prefix = "cancel_order_" if call.data.startswith("cancel_order_") else "cancel_reservation_"
    reservation_id = parse_callback_int_suffix(call, prefix, "Некорректные данные для отмены.")
    if reservation_id is None:
        return

    try:
        # Извлекаем ID пользователя
        user_id = call.from_user.id  # Берём ID пользователя

        # Основная логика
        current_user = Clients.get_row_by_user_id(user_id)
        if not current_user:
            safe_answer_callback_query(call.id, "Вы не зарегистрированы.", show_alert=True)
            return

        related_user_ids = get_related_user_ids_by_full_phone(user_id)

        order = Reservations.get_row_by_id(reservation_id)
        if not order or order.user_id not in related_user_ids:
            safe_answer_callback_query(call.id, "Резерв не найден или не принадлежит вам.", show_alert=True)
            return

        if order.is_fulfilled:
            safe_answer_callback_query(call.id, "Невозможно отказаться от уже обработанного заказа.", show_alert=True)
            return

        cancel_result = cancel_unfulfilled_reservation(reservation_id, related_user_ids)
        if cancel_result["status"] in {"not_found", "forbidden"}:
            safe_answer_callback_query(call.id, "Резерв не найден или не принадлежит вам.", show_alert=True)
            return
        if cancel_result["status"] == "fulfilled":
            safe_answer_callback_query(call.id, "Невозможно отказаться от уже обработанного заказа.", show_alert=True)
            return
        if cancel_result["status"] == "post_missing":
            safe_answer_callback_query(call.id, "Товар для отмены не найден.", show_alert=True)
            return
        if cancel_result["status"] == "queued":
            safe_answer_callback_query(
                call.id,
                "Вы успешно отказались от товара. Он передан следующему в очереди.",
                show_alert=False,
            )
            my_orders(call.message)
            return

        safe_answer_callback_query(
            call.id,
            "Вы успешно отказались от товара. Товар доступен в канале.",
            show_alert=False,
        )
        my_orders(call.message)

    except Exception as e:
        logger.exception("Reservation cancellation callback failed for reservation_id=%s: %s", reservation_id, e)
        safe_answer_callback_query(call.id, "Произошла ошибка при обработке отмены.", show_alert=True)

@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "enqueue_"))
def handle_enqueue(call):
    user_id = call.message.chat.id
    if ignore_silent_blocked_callback(call):
        return

    post_id = parse_callback_int_suffix(call, "enqueue_")
    if post_id is None:
        return

    # Проверяем, существует ли запись уже в TempReservations
    with Session(bind=engine) as session:
        post = session.query(Posts).filter(Posts.id == post_id).first()
        if not post:
            safe_answer_callback_query(call.id, "Товар больше недоступен.", show_alert=True)
            return

        existing_entry = session.query(TempReservations).filter(
            TempReservations.user_id == user_id,
            TempReservations.post_id == post_id,
            TempReservations.temp_fulfilled == False
        ).first()

        if existing_entry:
            safe_answer_callback_query(call.id, "Вы уже стоите в очереди за этим товаром.", show_alert=True)
            return

        session.add(TempReservations(
            user_id=user_id,
            quantity=1,
            post_id=post_id,
            temp_fulfilled=False,
        ))
        session.commit()

    safe_answer_callback_query(call.id)
    safe_send_message(
        bot,
        user_id,
        "Вы добавлены в очередь. Как только товар станет доступен, вы будете уведомлены.",
        logger=logger,
    )

# Возврат в меню заказов
@bot.callback_query_handler(func=lambda call: call.data == "go_back")
def go_back_to_menu(call):
    if not isinstance(call, telebot.types.Message) and ignore_silent_blocked_callback(call):
        return
    if isinstance(call, telebot.types.Message) and ignore_silent_blocked_message(call):
        return

    # Если объект — сообщение (Message), то работаем с ним напрямую
    if isinstance(call, telebot.types.Message):
        chat_id = call.chat.id
    # Если объект — CallbackQuery, извлекаем его компонент message
    elif isinstance(call, telebot.types.CallbackQuery):
        chat_id = call.message.chat.id
        # Сразу подтверждаем callback_query
        try:
            safe_answer_callback_query(call.id)
        except Exception as e:
            logger.debug("Failed to answer callback query: %s", e)
    else:
        logger.warning("Unsupported object type passed to go_back_to_menu: %s", type(call).__name__)
        return

    # Отправляем сообщение пользователю
    safe_send_message(bot, chat_id, "Вы вернулись в главное меню.", logger=logger)

# Обработчик функции 🚗 Заказы в доставке
@bot.message_handler(func=lambda message: message.text == "🚗 Заказы в доставке")
def show_delivery_orders(message):
    user_id = message.chat.id  # Получаем ID текущего пользователя
    if ignore_silent_blocked_message(message):
        return

    try:
        related_user_ids = get_related_user_ids_by_full_phone(user_id)
        with Session(bind=engine) as session:
            user_items = session.query(InDelivery).filter(
                InDelivery.user_id.in_(related_user_ids)
            ).all()

        # Проверяем сами данные

        # Создаём словарь для агрегации данных:
        aggregated_items = {}
        for item in user_items:
            if item.item_description not in aggregated_items:
                # Если описание ещё не добавлено, записываем его
                aggregated_items[item.item_description] = {
                    "quantity": item.quantity,  # Количество
                    "total_sum": item.price,  # Итоговая сумма
                }
            else:
                # Если описание уже есть, увеличиваем количество и итоговую сумму
                aggregated_items[item.item_description]["quantity"] += item.quantity
                aggregated_items[item.item_description]["total_sum"] += item.price

        # Преобразуем словарь обратно в список (для передачи на следующий этап)
        unique_items = [
            {
                "item_description": description,
                "quantity": data["quantity"],
                "total_sum": data["total_sum"],
            }
            for description, data in aggregated_items.items()
        ]

        # Если товаров нет, отправляем сообщение об этом
        if not unique_items:
            safe_send_message(bot, user_id, "📭 У вас нет товаров в доставке.", logger=logger)
            return

        # Отправляем список первым сообщением
        send_delivery_order_page(
            user_id=user_id,
            message_id=None,  # Потому что отправляется впервые
            orders=unique_items,
            page=0,
        )

    except Exception as e:
        logger.exception("Delivery order list failed for user_id=%s: %s", user_id, e)
        safe_send_message(
            bot,
            user_id,
            "❌ Ошибка при загрузке списка заказов. Подробности записаны в лог.",
            logger=logger,
        )

# Создает страницу с заказами в доставке
def aggregate_delivery_items(items):
    aggregated_items = {}
    for item in items:
        if item.item_description not in aggregated_items:
            aggregated_items[item.item_description] = {
                "quantity": item.quantity,
                "total_sum": item.price,
            }
        else:
            aggregated_items[item.item_description]["quantity"] += item.quantity
            aggregated_items[item.item_description]["total_sum"] += item.price

    return [
        {
            "item_description": description,
            "quantity": data["quantity"],
            "total_sum": data["total_sum"],
        }
        for description, data in aggregated_items.items()
    ]


def send_delivery_order_page(user_id, message_id, orders, page):
    orders_per_page = 5  # Количество товаров на странице
    start = page * orders_per_page
    end = start + orders_per_page
    total_pages = (len(orders) - 1) // orders_per_page + 1  # Всего страниц
    selected_orders = orders[start:end]  # Текущая страница товаров

    # Формируем сообщение для текущей страницы
    text = f"🚚 Ваши товары в доставке (страница {page + 1} из {total_pages}):\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)

    # Добавляем товары на страницу
    for idx, order in enumerate(selected_orders, start=start + 1):
        text += (
            f"{idx}) {order['item_description']}\n"
            f"Количество: {order['quantity']}\n"
            f"Сумма: {order['total_sum']} руб.\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
        )

    # Добавляем кнопки для навигации
    if page > 0:
        keyboard.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"delivery_page_{page - 1}"))
    if end < len(orders):
        keyboard.add(InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"delivery_page_{page + 1}"))

    try:
        with open(DELIVERY_ORDER_GIF_PATH, "rb") as gif:
            if message_id:  # Если сообщение уже существует, обновляем его
                try:
                    return edit_message_media_with_retries(
                        bot,
                        logger=logger,
                        chat_id=user_id,
                        message_id=message_id,
                        media=InputMediaAnimation(gif, caption=text),
                        reply_markup=keyboard,
                    )
                except Exception as exc:
                    if is_message_not_modified_error(exc):
                        logger.debug("Delivery orders page was already up to date for user=%s page=%s", user_id, page)
                        return SimpleNamespace(message_id=message_id)
                    if is_message_to_edit_missing_error(exc):
                        logger.info(
                            "Delivery orders page message missing for user=%s page=%s; sending a new one",
                            user_id,
                            page,
                        )
                        gif.seek(0)
                        sent_animation = safe_send_animation(
                            bot,
                            user_id,
                            gif,
                            caption=text,
                            reply_markup=keyboard,
                            logger=logger,
                        )
                        if sent_animation:
                            return sent_animation
                    else:
                        logger.warning(
                            "Delivery orders page media edit failed for user=%s page=%s: %s",
                            user_id,
                            page,
                            exc,
                        )
            else:  # Иначе отправляем новое сообщение
                sent_animation = safe_send_animation(
                    bot,
                    user_id,
                    gif,
                    caption=text,
                    reply_markup=keyboard,
                    logger=logger,
                )
                if sent_animation:
                    return sent_animation
                logger.warning("Delivery orders page animation send returned no message for user=%s page=%s", user_id, page)
    except OSError as exc:
        logger.warning("Delivery orders animation unavailable path=%s: %s", DELIVERY_ORDER_GIF_PATH, exc)

    return edit_or_send_text_page(user_id, message_id, text, keyboard, "delivery orders")

# Хэндлер для команды "👔 Назначить работника"
@bot.message_handler(func=lambda message: message.text == "👔 Назначить работника")
def manage_user(message):
    # Проверяем, является ли пользователь администратором или лидером
    user_id = require_message_user_id(message, "Worker assignment command")
    if user_id is None:
        return
    if not (is_admin(user_id) or is_leader(user_id)):
        safe_send_message(
            bot,
            message.chat.id,
            "У вас недостаточно прав для выполнения этой команды.",
            logger=logger,
        )
        return

    # Если пользователь имеет доступ, продолжаем выполнение функции
    safe_send_message(
        bot,
        message.chat.id,
        "Введите Имя пользователя и последние 4 цифры номера через пробел (например, Иван 1234):",
        logger=logger,
    )
    bot.register_next_step_handler(message, process_user_input)


def show_role_controls(chat_id, client):
    response = f"Данные пользователя:\nИмя: {client.name}\nТелефон: {client.phone}\nТекущая роль: {client.role}"
    if client.role in SPECIAL_ROLES:
        safe_send_message(bot, chat_id, response + "\nЭту роль нельзя изменить.", logger=logger)
        return

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Повысить", callback_data=f"promote_{client.user_id}"),
        InlineKeyboardButton("Понизить", callback_data=f"demote_{client.user_id}"),
    )
    safe_send_message(bot, chat_id, response, reply_markup=keyboard, logger=logger)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "role_select_"))
def handle_role_user_selection(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    client_id = parse_callback_int_suffix(call, "role_select_")
    if client_id is None:
        return
    client = Clients.get_row_by_id(client_id)
    if not client:
        safe_answer_callback_query(call.id, "Пользователь не найден.", show_alert=True)
        return

    show_role_controls(call.message.chat.id, client)
    safe_answer_callback_query(call.id)

# Обработка ввода имени и последних 4 цифр номера для поиска
def process_user_input(message):
    try:
        # Последний фрагмент считаем цифрами телефона, все до него - именем.
        parts = str(message.text or "").split()
        if len(parts) < 2:
            safe_send_message(
                bot,
                message.chat.id,
                "Пожалуйста, введите данные в формате 'Имя 1234'.",
                logger=logger,
            )
            return

        name = " ".join(parts[:-1]).strip()
        last_digits = parts[-1].strip()

        if not last_digits.isdigit() or len(last_digits) != 4:
            safe_send_message(
                bot,
                message.chat.id,
                "Пожалуйста, введите корректные последние 4 цифры номера.",
                logger=logger,
            )
            return

        # Поиск пользователя по имени и последним 4 цифрам номера
        matching_users = Clients.get_rows_for_work_name_number(name=name, phone_ending=last_digits)
        if not matching_users:
            safe_send_message(bot, message.chat.id, "Пользователь не найден.", logger=logger)
            return

        if len(matching_users) > 1:
            keyboard = InlineKeyboardMarkup()
            for client in matching_users:
                keyboard.add(
                    InlineKeyboardButton(
                        f"{client.name} | {client.phone} | {client.role}",
                        callback_data=f"role_select_{client.id}",
                    )
                )
            safe_send_message(
                bot,
                message.chat.id,
                "Найдено несколько пользователей. Выберите нужного по полному номеру:",
                reply_markup=keyboard,
                logger=logger,
            )
            return

        show_role_controls(message.chat.id, matching_users[0])
        return
    except ValueError:
        safe_send_message(bot, message.chat.id, "Пожалуйста, введите данные в формате 'Имя 1234'.", logger=logger)
    except Exception as e:
        safe_send_message(bot, message.chat.id, "Произошла ошибка при обработке данных.", logger=logger)
        logger.exception("Role search input failed for chat_id=%s: %s", message.chat.id, e)

# Обработчик изменения роли
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, ("promote_", "demote_")))
def handle_role_change(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    try:
        # Получаем данные из callback (action, user_id)
        if call.data.startswith("promote_"):
            action = "promote"
            user_id = parse_callback_int_suffix(call, "promote_")
        else:
            action = "demote"
            user_id = parse_callback_int_suffix(call, "demote_")
        if user_id is None:
            return

        # Получаем пользователя через Clients
        user = Clients.get_row_by_user_id(user_id)  # Используем существующий метод get_row_by_user_id
        if not user:
            safe_answer_callback_query(call.id, "Пользователь не найден.")
            return

        current_role = user.role

        # Проверка корректности текущей роли
        if current_role not in ROLES:
            safe_answer_callback_query(call.id, "Некорректная роль пользователя.")
            return

        # Проверка, не относится ли пользователь к защищённым ролям
        if current_role in SPECIAL_ROLES:
            safe_answer_callback_query(call.id, "Эту роль нельзя менять.")
            return

        # Вычисление новой роли
        current_index = ROLES.index(current_role)
        if action == "promote" and current_index < len(ROLES) - 1:
            new_role = ROLES[current_index + 1]
        elif action == "demote" and current_index > 0:
            new_role = ROLES[current_index - 1]
        else:
            safe_answer_callback_query(call.id, "Дальнейшее изменение роли невозможно.")
            return

        # Используем метод для обновления роли пользователя
        success = Clients.update_row_for_work(user_id=user.user_id, updates={'role': new_role})

        if success:
            # Генерируем обновленную клавиатуру
            keyboard = InlineKeyboardMarkup()
            if new_role != ROLES[-1]:  # Проверяем, можно ли повысить
                keyboard.add(InlineKeyboardButton("Повысить", callback_data=f"promote_{user_id}"))
            if new_role != ROLES[0]:  # Проверяем, можно ли понизить
                keyboard.add(InlineKeyboardButton("Понизить", callback_data=f"demote_{user_id}"))

            safe_answer_callback_query(call.id, f"Роль изменена на {new_role}.")
            # Обновляем сообщение с пользовательскими данными и клавиатурой
            try:
                safe_edit_message_text(
                    bot,
                    logger=logger,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"Данные пользователя:\nИмя: {user.name}\nТекущая роль: {new_role}",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.warning("Role change message refresh failed for target_user_id=%s: %s", user_id, e)
                return
        else:
            safe_answer_callback_query(call.id, "Ошибка при обновлении данных.")
    except Exception as e:
        safe_answer_callback_query(call.id, "Ошибка при обработке запроса.")
        logger.exception("Role change failed for call_data=%s: %s", call.data, e)

# Поиск пользователя по имени и последним 4 цифрам номера
def find_user_by_name_and_last_digits(name, last_digits):
    try:
        user = Clients.get_row_for_work_name_number(name=name, phone_ending=last_digits)
        if not user:
            logger.debug("User not found by name=%s phone_ending=%s", name, last_digits)
            return None
        # Возвращаем user_id, чтобы использовать его далее
        return {
            'user_id': user.user_id,
            'name': user.name,
            'role': user.role,
        }
    except Exception as e:
        logger.exception("User search by name and phone failed: %s", e)
        return None

# Обработчик навигации между страницами для заказов в доставке
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "delivery_page_"))
def paginate_delivery_orders(call):
    if ignore_silent_blocked_callback(call):
        return

    user_id = call.message.chat.id
    message_id = call.message.message_id
    page = parse_callback_int_suffix(call, "delivery_page_", "Некорректная страница.")
    if page is None:
        return

    # Получаем заказы пользователя
    orders = InDelivery.get_all_rows()
    related_user_ids = get_related_user_ids_by_full_phone(user_id)
    user_orders = [order for order in orders if order.user_id in related_user_ids]
    user_orders = aggregate_delivery_items(user_orders)

    try:
        # Отправка обновления страницы
        new_message = send_delivery_order_page(user_id=user_id, message_id=message_id, orders=user_orders, page=page)
        if new_message:
            user_last_message_id[user_id] = new_message.message_id
    except Exception as e:
        logger.warning("Delivery order pagination failed for user_id=%s page=%s: %s", user_id, page, e)
    finally:
        safe_answer_callback_query(call.id)  # Подтверждаем успешную обработку

# Перессылка забронированного товара в группу Брони Мега Скидки
@bot.message_handler(func=lambda message: message.text == "📦 Заказы клиентов")
def send_all_reserved_to_group(message):
    user_id = message.chat.id
    role = get_client_role(user_id)  # Получение роли клиента
    # Проверка ролей
    if role not in ["supreme_leader", "admin"]:
        safe_send_message(bot, user_id, f"У вас нет прав доступа к этой функции. Ваша роль: {role}", logger=logger)
        return
    safe_send_message(
        bot,
        user_id,
        "Эта стадия больше не нужна: новые брони сразу отправляются в группу, "
        "а бот автоматически переводит бронь в «Обработано» через 1 час.",
        logger=logger,
    )

@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "mark_fulfilled_group_"))
def mark_fulfilled_group(call):
    safe_answer_callback_query(
        call.id,
        "Ручная обработка заказов отключена: бронь отправляется в группу сразу и обрабатывается автоматически через 1 час.",
        show_alert=True,
    )

# Хэндлер для очистки корзины
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "clear_cart_"))
def handle_clear_cart(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    # Получаем ID клиента из callback данных
    client_id = parse_callback_int_suffix(call, "clear_cart_", "Некорректный клиент.")
    if client_id is None:
        return

    client = Clients.get_row_by_id(client_id)

    if not client:
        safe_answer_callback_query(call.id, "Клиент не найден.", show_alert=True)
        return

    safe_answer_callback_query(call.id, "Расформировываю корзину.")
    try:
        stats = clear_client_cart(client.user_id, processed_only=False)
    except Exception as exc:
        logger.exception("Legacy clear cart failed for client_id=%s user_id=%s: %s", client_id, client.user_id, exc)
        safe_send_message(
            bot,
            call.message.chat.id,
            "Ошибка при расформировании корзины. Подробности записаны в лог.",
            logger=logger,
        )
        return

    if stats.get("deleted", 0) <= 0:
        safe_send_message(bot, call.message.chat.id, "Корзина уже пуста.", logger=logger)
        return

    safe_send_message(
        bot,
        call.message.chat.id,
        f"Корзина клиента успешно расформирована. {format_cart_cleanup_result(stats)}",
        logger=logger,
    )

# Проверка на регистрацию(стэйты статуса)
def is_registered(user_id):
    """
    Проверяет, зарегистрирован ли пользователь в таблице clients.
    Использует метод get_row для получения данных.
    """
    client = Clients.get_row(user_id=user_id)
    return client is not None
def set_user_state(user_id, state):
    if state is None:
        clear_user_state(user_id)
    else:
        user_states[user_id] = state
        BotSession.set_state(user_id, state)


def get_user_state(chat_id):
    if chat_id in user_states:
        return user_states[chat_id]

    state = BotSession.get_state(chat_id)
    user_states[chat_id] = state
    return state


def user_state_in(chat_id, expected_states):
    state = get_user_state(chat_id)
    return isinstance(state, str) and state in expected_states


def clear_user_state(user_id):
    user_states[user_id] = None
    BotSession.clear_state(user_id)

# Обработчик кнопки "⚙️ Клиенты"
@bot.message_handler(func=lambda message: message.text == "⚙️ Клиенты")
def manage_clients(message):
    user_id = message.chat.id
    role = get_client_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin","supreme_leader"]:
        safe_send_message(bot, user_id, "У вас недостаточно прав.", logger=logger)
        return

    # Создаем клавиатуру с кнопками
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🗑 Удалить клиента 📞", "🧺 Просмотреть корзину", "🚚 Управление доставкой","❌ Брак", "⬅️ Назад")
    safe_send_message(bot, message.chat.id, "Выберите действие:", reply_markup=markup, logger=logger)

# Обработчик для кнопки "❌ Брак"
@bot.message_handler(func=lambda message: message.text == "❌ Брак")
def defective_order(message):
    if not require_role(message, ADMIN_ROLES):
        return
    # Устанавливаем состояние пользователя
    set_user_state(message.chat.id, "awaiting_last_digits_defective")
    safe_send_message(
        bot,
        message.chat.id,
        "Введите последние 4 цифры номера телефона для поиска пользователя:",
        logger=logger,
    )

# Поиск пользователя по последним 4 цифрам телефона
def ask_defective_confirmation(chat_id, client):
    keyboard = create_defective_confirmation_keyboard()
    safe_send_message(
        bot,
        chat_id,
        f"Найден пользователь:\nИмя: {client.name}\nТелефон: {client.phone}\nПродолжить обработку брака для этого пользователя?",
        reply_markup=keyboard,
        logger=logger,
    )
    temp_user_data[chat_id] = {"user_id": client.user_id}
    set_user_state(chat_id, "awaiting_defective_action")


@bot.callback_query_handler(
    func=lambda call: callback_message_user_state_is(call, "awaiting_defective_client_choice")
    and callback_data_startswith(call, "defective_client_")
)
def handle_defective_client_choice(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    client_id = parse_callback_int_suffix(call, "defective_client_")
    if client_id is None:
        return
    client = Clients.get_row_by_id(client_id)
    if not client:
        safe_answer_callback_query(call.id, "Пользователь не найден.", show_alert=True)
        return

    safe_answer_callback_query(call.id)
    ask_defective_confirmation(call.message.chat.id, client)


@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "awaiting_last_digits_defective")
def search_user_for_defective(message):
    text = require_text_message(message, "Введите последние 4 цифры номера телефона текстом.")
    if text is None:
        return
    last_digits = text.strip()
    if not last_digits.isdigit() or len(last_digits) != 4:
        safe_send_message(
            bot,
            message.chat.id,
            "Введите корректные последние 4 цифры номера телефона (4 цифры).",
            logger=logger,
        )
        return

    # Ищем пользователя в Clients
    users = Clients.get_row_by_phone_digits(last_digits)

    if users:  # Если список пользователей найден
        if len(users) > 1:
            keyboard = InlineKeyboardMarkup()
            for client in users:
                keyboard.add(
                    InlineKeyboardButton(
                        f"{client.name} | {client.phone}",
                        callback_data=f"defective_client_{client.id}",
                    )
                )
            safe_send_message(
                bot,
                message.chat.id,
                "Найдено несколько пользователей. Выберите нужного по полному номеру:",
                reply_markup=keyboard,
                logger=logger,
            )
            set_user_state(message.chat.id, "awaiting_defective_client_choice")
            return

        ask_defective_confirmation(message.chat.id, users[0])
        return
    else:
        safe_send_message(bot, message.chat.id, "Пользователи с такими цифрами номера не найдены. Попробуйте еще раз.", logger=logger)

# Обработка действия (подтверждения или отмены)
@bot.callback_query_handler(func=lambda call: callback_message_user_state_is(call, "awaiting_defective_action"))
def handle_defective_action(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    if call.data == "confirm_defective":
        set_user_state(call.message.chat.id, "awaiting_defective_sum")
        safe_answer_callback_query(call.id)
        safe_send_message(bot, call.message.chat.id, "Введите сумму брака:", logger=logger)
    elif call.data == "cancel_defective":
        safe_answer_callback_query(call.id)
        safe_send_message(bot, call.message.chat.id, "Операция отменена. Возвращаю вас в главное меню.", logger=logger)
        clear_user_state(call.message.chat.id)
        go_back_to_menu(call.message)
    else:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data in ["confirm_defective", "cancel_defective"])
def handle_stale_defective_action(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    safe_answer_callback_query(
        call.id,
        "Сессия оформления брака потерялась. Начните заново через кнопку «❌ Брак».",
        show_alert=True,
    )


# Ввод суммы брака
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "awaiting_defective_sum")
def handle_defective_sum_entry(message):
    flow_data = get_temp_user_data_or_reset(
        message.chat.id,
        required_fields=("user_id",),
        notice_chat_id=message.chat.id,
        notice_text="Сессия оформления брака потерялась. Начните заново через кнопку «❌ Брак».",
    )
    if not flow_data:
        return

    text = require_text_message(message, "Введите сумму брака текстом.")
    if text is None:
        return

    try:
        defective_sum = int(text.strip())
        user_id = flow_data["user_id"]  # Берем найденный user_id

        # Получаем заказы пользователя из таблицы Reservations
        reservations = get_user_reservations(user_id)

        if reservations:
            flow_data["defective_sum"] = defective_sum
            temp_user_data[message.chat.id] = flow_data
            set_user_state(message.chat.id, "select_reservation_for_defective")
            send_defective_reservation_page(message.chat.id, page=0)
        else:
            safe_send_message(bot, message.chat.id, "Заказы у данного пользователя не найдены. Попробуйте еще раз.", logger=logger)
            clear_user_state(message.chat.id)
            go_back_to_menu(message)
    except ValueError:
        safe_send_message(bot, message.chat.id, "Некорректное значение. Введите числовую сумму.", logger=logger)


@bot.callback_query_handler(
    func=lambda call: callback_message_user_state_is(call, "select_reservation_for_defective")
    and callback_data_startswith(call, "defective_res_page_")
)
def paginate_defective_reservations(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    page = parse_callback_int_suffix(call, "defective_res_page_", "Некорректная страница.")
    if page is None:
        return

    safe_answer_callback_query(call.id)
    send_defective_reservation_page(
        call.message.chat.id,
        page=page,
        message_id=call.message.message_id,
    )


# Обработка выбора заказа для дефектного товара
@bot.callback_query_handler(
    func=lambda call: callback_message_user_state_is(call, "select_reservation_for_defective")
    and callback_data_startswith(call, "select_")
    and not callback_data_startswith(call, "select_defective_")
)
def handle_reservation_selection(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    # Отвечаем на callback_query сразу
    safe_answer_callback_query(call.id, text="Ваш выбор обрабатывается...")

    reservation_id = parse_callback_int_suffix(call, "select_")
    if reservation_id is None:
        return
    flow_data = get_temp_user_data_or_reset(
        call.message.chat.id,
        required_fields=("defective_sum", "user_id"),
        notice_chat_id=call.message.chat.id,
        notice_text="Сессия оформления брака потерялась. Начните заново через кнопку «❌ Брак».",
    )
    if not flow_data:
        return

    defective_sum = flow_data["defective_sum"]
    selected_user_id = flow_data["user_id"]
    related_user_ids = get_related_user_ids_by_full_phone(selected_user_id)

    # Обновляем return_order в базе данных
    with Session(bind=engine) as session:
        reservation = session.query(Reservations).filter_by(id=reservation_id).first()
        if reservation and reservation.user_id in related_user_ids:
            reservation.return_order += defective_sum
            session.commit()
            safe_send_message(bot, call.message.chat.id, f"Сумма брака {defective_sum} успешно добавлена в заказ.", logger=logger)
        else:
            safe_send_message(bot, call.message.chat.id, "Ошибка: Заказ не найден.", logger=logger)

    # Завершаем процесс
    clear_user_state(call.message.chat.id)
    go_back_to_menu(call.message)  # Передаем только сообщение


@bot.callback_query_handler(
    func=lambda call: callback_data_startswith(call, "defective_client_")
    or callback_data_startswith(call, "defective_res_page_")
    or (
        callback_data_startswith(call, "select_")
        and not callback_data_startswith(call, "select_defective_")
    )
)
def handle_stale_defective_flow_callback(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    safe_answer_callback_query(
        call.id,
        "Сессия оформления брака потерялась. Начните заново через кнопку «❌ Брак».",
        show_alert=True,
    )


def send_defective_reservation_page(chat_id, page=0, message_id=None):
    state = temp_user_data.get(chat_id) or {}
    user_id = state.get("user_id")
    if not user_id:
        safe_send_message(bot, chat_id, "Не удалось найти выбранного пользователя. Начните заново.", logger=logger)
        clear_user_state(chat_id)
        return

    reservations = get_user_reservations(user_id)
    if not reservations:
        safe_send_message(bot, chat_id, "Заказы у данного пользователя не найдены. Попробуйте еще раз.", logger=logger)
        clear_user_state(chat_id)
        return

    page = max(int(page or 0), 0)
    total_pages = max((len(reservations) - 1) // DEFECTIVE_RESERVATION_PAGE_SIZE + 1, 1)
    page = min(page, total_pages - 1)
    keyboard = create_select_reservation_keyboard(reservations, page=page)
    text = f"Выберите заказ, чтобы добавить сумму брака (стр. {page + 1} из {total_pages}):"

    if message_id:
        edited_message = safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )
        if edited_message is not None:
            return

    safe_send_message(bot, chat_id, text, logger=logger, reply_markup=keyboard)


# Клавиатура для выбора конкретного заказа
def create_select_reservation_keyboard(reservations, page=0):
    keyboard = types.InlineKeyboardMarkup()
    start = max(int(page or 0), 0) * DEFECTIVE_RESERVATION_PAGE_SIZE
    selected_reservations = reservations[start:start + DEFECTIVE_RESERVATION_PAGE_SIZE]
    for reservation in selected_reservations:
        btn = types.InlineKeyboardButton(
            text=truncate_button_text(
                f"Заказ ID {reservation.id} (Возврат: {reservation.return_order or 0})",
                64,
            ),
            callback_data=f"select_{reservation.id}"
        )
        keyboard.add(btn)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"defective_res_page_{page - 1}"))
    if start + DEFECTIVE_RESERVATION_PAGE_SIZE < len(reservations):
        nav_buttons.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"defective_res_page_{page + 1}"))
    if nav_buttons:
        keyboard.row(*nav_buttons)
    return keyboard

# Уникальная клавиатура подтверждения
def create_defective_confirmation_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    btn_confirm = types.InlineKeyboardButton("Подтвердить ❌ Брак", callback_data="confirm_defective")
    btn_cancel = types.InlineKeyboardButton("Отмена ❌ Брак", callback_data="cancel_defective")
    keyboard.add(btn_confirm, btn_cancel)
    return keyboard

# Обработчик нажатия на кнопку "Просмотреть корзину"
@bot.message_handler(func=lambda message: message.text == "🧺 Просмотреть корзину")
def request_phone_last_digits(message):
    if not require_role(message, ADMIN_ROLES):
        return
    safe_send_message(
        bot,
        message.chat.id,
        "Введите последние 4 цифры номера телефона клиента:",
        logger=logger,
    )
    set_user_state(message.chat.id, "AWAITING_PHONE_LAST_4")

# Хэндлер для кнопки Управление доставкой
@bot.message_handler(func=lambda message: message.text == "🚚 Управление доставкой")
def handle_delivery_management(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    # Создаем клавиатуру с кнопками
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        "📤 Отправить рассылку",
        "👨‍🦯 Засунуть в доставку",
        "🧺 Собрать доставку",
        "📦 Список пропажи",
        "Список Клиентов",
        MERGE_DELIVERY_CARTS_BUTTON,
        "🗄 Архив доставки",
        "⬅️ Назад",
    )
    safe_send_message(bot, message.chat.id, "Выберите действие:", reply_markup=markup, logger=logger)


@bot.message_handler(func=lambda message: message.text == "Список Клиентов")
def show_delivery_clients_summary(message):
    if not require_role(message, DELIVERY_ROLES):
        return

    clients = get_delivery_clients_summary()
    if not clients:
        safe_send_message(
            bot,
            message.chat.id,
            f"Клиентов с обработанными заказами на {DELIVERY_THRESHOLD} ₽ и более не найдено.",
            logger=logger,
        )
        return

    messages = build_delivery_clients_summary_messages(clients)
    for text in messages:
        safe_send_message(bot, message.chat.id, text, logger=logger)
        time.sleep(0.2)


@bot.message_handler(func=lambda message: message.text == MERGE_DELIVERY_CARTS_BUTTON)
def request_delivery_cart_merge_main(message):
    if not require_role(message, DELIVERY_ROLES):
        return

    temp_user_data[message.chat.id] = {}
    set_user_state(message.chat.id, MERGE_DELIVERY_MAIN_STATE)
    safe_send_message(
        bot,
        message.chat.id,
        "Введите телефон основной корзины. В списке сборки будет показываться именно она.",
        logger=logger,
    )


@bot.message_handler(func=lambda message: message_user_state_is(message, MERGE_DELIVERY_MAIN_STATE))
def handle_delivery_cart_merge_main_input(message):
    if not require_role(message, DELIVERY_ROLES):
        clear_user_state(message.chat.id)
        temp_user_data.pop(message.chat.id, None)
        return

    text = require_text_message(message, "Введите телефон основной корзины текстом.")
    if text is None:
        return

    with Session(bind=engine) as session:
        delivery_entry, error_text = find_delivery_entry_by_phone_input(session, text)
        if error_text:
            safe_send_message(bot, message.chat.id, error_text, logger=logger)
            return

        root_entry = get_delivery_root_entry(session, delivery_entry)
        if not root_entry:
            safe_send_message(bot, message.chat.id, "Основная корзина не найдена.", logger=logger)
            return

        temp_user_data[message.chat.id] = {
            "main_delivery_id": root_entry.id,
            "main_phone": root_entry.phone,
            "main_name": root_entry.name,
        }

    set_user_state(message.chat.id, MERGE_DELIVERY_EXTRA_STATE)
    safe_send_message(
        bot,
        message.chat.id,
        "Введите телефон дополнительной корзины. Ее товары будут идти после основной корзины.",
        logger=logger,
    )


@bot.message_handler(func=lambda message: message_user_state_is(message, MERGE_DELIVERY_EXTRA_STATE))
def handle_delivery_cart_merge_extra_input(message):
    if not require_role(message, DELIVERY_ROLES):
        clear_user_state(message.chat.id)
        temp_user_data.pop(message.chat.id, None)
        return

    text = require_text_message(message, "Введите телефон дополнительной корзины текстом.")
    if text is None:
        return

    flow_data = temp_user_data.get(message.chat.id) or {}
    main_delivery_id = flow_data.get("main_delivery_id")
    if not main_delivery_id:
        clear_user_state(message.chat.id)
        temp_user_data.pop(message.chat.id, None)
        safe_send_message(
            bot,
            message.chat.id,
            "Данные объединения потерялись. Нажмите «Объединить корзины» заново.",
            logger=logger,
        )
        return

    with Session(bind=engine) as session:
        success, result_text = merge_delivery_cart_entries(session, main_delivery_id, text)
        if success:
            session.commit()
        else:
            session.rollback()

    if success:
        clear_user_state(message.chat.id)
        temp_user_data.pop(message.chat.id, None)

    safe_send_message(bot, message.chat.id, result_text, logger=logger)


# Хэедлнр для поиска по последним 4 цифрам номера
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "AWAITING_PHONE_LAST_4")
def handle_phone_input(message):
    if not require_role(message, ADMIN_ROLES):
        clear_user_state(message.chat.id)
        return
    text = require_text_message(message, "Введите последние 4 цифры номера телефона текстом.")
    if text is None:
        return
    input_text = text.strip()

    # Проверяем, что введены последние 4 цифры номера телефона
    if not input_text.isdigit() or len(input_text) != 4:
        safe_send_message(
            bot,
            message.chat.id,
            "Введите корректные последние 4 цифры номера телефона (4 цифры).",
            logger=logger,
        )
        return

    # Показ корзины по последним 4 цифрам номера телефона
    show_cart_by_last_phone_digits(message, input_text)

# Получаем всех клиентов с такими последними цифрами телефона
def show_cart_by_last_phone_digits(message, last_4_digits):
    clients = Clients.get_row_by_phone_digits(last_4_digits)

    if not clients:
        safe_send_message(
            bot,
            message.chat.id,
            "Пользователи с такими последними цифрами номера телефона не найдены.",
            logger=logger,
        )
        clear_user_state(message.chat.id)
        return

    clients = deduplicate_clients_by_full_phone(clients)
    if len(clients) > 1:
        markup = types.InlineKeyboardMarkup()
        for client in clients:
            button_text = truncate_button_text(
                f"{client.name} | {format_admin_phone(client.phone)}"
            )
            markup.add(types.InlineKeyboardButton(
                button_text,
                callback_data=f"view_cart_{client.id}",
            ))
        safe_send_message(
            bot,
            message.chat.id,
            "Найдено несколько клиентов с такими последними цифрами. Выберите нужного:",
            reply_markup=markup,
            logger=logger,
        )
    else:
        show_cart_for_client(message.chat.id, clients[0])

    # Очистить состояние пользователя
    clear_user_state(message.chat.id)


def deduplicate_clients_by_full_phone(clients):
    unique_clients = []
    seen_phones = set()
    for client in clients:
        phone_key = Clients.normalize_phone(client.phone) or f"user:{client.user_id}"
        if phone_key in seen_phones:
            continue
        seen_phones.add(phone_key)
        unique_clients.append(client)
    return unique_clients


def format_admin_phone(phone):
    return Clients.normalize_phone(phone) or "номер не указан"


def truncate_button_text(text, max_length=60):
    return text if len(text) <= max_length else f"{text[:max_length - 3]}..."


def format_money(value):
    return int(value or 0)


def format_cart_date(value):
    if not value:
        return "не указана"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    return str(value)


def format_post_author(post):
    if not post or not getattr(post, "chat_id", None):
        return "неизвестно"
    author_name = Clients.get_name_by_user_id(post.chat_id)
    return author_name or f"user_id {post.chat_id}"


def build_item_list_caption(description, price, quantity, created_at, post_id=None, author=None, show_quantity=True):
    lines = []
    if post_id is not None:
        lines.append(f"Id товара: {post_id}")
    lines.extend([
        f"Описание: {description}",
        f"Цена: {price} ₽",
    ])
    if show_quantity:
        lines.append(f"Количество: {quantity}")
    lines.append(f"Дата создания: {format_cart_date(created_at)}")
    if author:
        lines.append(f"Выложил: {author}")
    return "\n".join(lines)


def show_cart_for_client(chat_id, client):
    related_clients = Clients.get_rows_by_phone(client.phone)
    total_orders = sum(calculate_total_sum(related.user_id) for related in related_clients)
    processed_orders = sum(calculate_processed_sum(related.user_id) for related in related_clients)

    safe_send_message(
        bot,
        chat_id,
        f"Пользователь: {client.name}\n"
        f"Телефон: {format_admin_phone(client.phone)}\n"
        f"Общая сумма заказов: {total_orders} руб.\n"
        f"Общая сумма обработанных заказов: {processed_orders} руб.",
        logger=logger,
    )

    reservations = get_user_reservations(client.user_id)
    if not reservations:
        safe_send_message(bot, chat_id, f"Корзина пользователя {client.name} пуста.", logger=logger)
        return

    send_cart_content(chat_id, reservations, client.user_id)


def build_cart_item_caption(post, reservation):
    unit_price = reservation_unit_price(reservation, post)
    return build_item_list_caption(
        description=post.description,
        price=unit_price,
        quantity=reservation.quantity,
        created_at=post.created_at,
        post_id=reservation.post_id,
        author=format_post_author(post),
    )


def empty_cart_cleanup_stats():
    return {
        "deleted": 0,
        "processed_deleted": 0,
        "unprocessed_deleted": 0,
        "temp_deleted": 0,
        "wait_queue_deleted": 0,
        "orphan_queue_deleted": 0,
        "returned": 0,
        "queued": 0,
        "not_returned_to_channel": 0,
        "for_delivery_removed": 0,
        "for_delivery_updated": 0,
        "delivery_collection_items_removed": 0,
        "missing_posts": 0,
    }


def merge_cart_cleanup_stats(target, source):
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value or 0)
    return target


def format_cart_cleanup_result(stats):
    parts = []
    if stats.get("deleted"):
        parts.extend([
            f"Удалено позиций: {stats['deleted']}",
            f"обработанных: {stats['processed_deleted']}",
            f"в обработке: {stats['unprocessed_deleted']}",
        ])
    if stats.get("temp_deleted"):
        parts.append(f"очищено обработанных строк: {stats['temp_deleted']}")
    if stats.get("wait_queue_deleted"):
        parts.append(f"удалено из очереди ожидания: {stats['wait_queue_deleted']}")
    if stats.get("orphan_queue_deleted"):
        parts.append(f"очищено очередей без клиента: {stats['orphan_queue_deleted']}")
    if stats.get("returned"):
        parts.append(f"возвращено в остаток: {stats['returned']}")
    if stats.get("queued"):
        parts.append(f"передано очереди: {stats['queued']}")
    if stats.get("not_returned_to_channel"):
        parts.append(f"без возврата на канал: {stats['not_returned_to_channel']}")
    if stats.get("for_delivery_removed"):
        parts.append(f"удалено заявок доставки: {stats['for_delivery_removed']}")
    if stats.get("for_delivery_updated"):
        parts.append(f"обновлено заявок доставки: {stats['for_delivery_updated']}")
    if stats.get("delivery_collection_items_removed"):
        parts.append(f"очищено строк сборки доставки: {stats['delivery_collection_items_removed']}")
    if stats.get("missing_posts"):
        parts.append(f"без карточки товара: {stats['missing_posts']}")
    if not parts:
        return "Товар уже удален или корзина пуста."
    return ". ".join(parts) + "."


def delete_temp_fulfilled_for_reservation(session, reservation):
    temp_items = session.query(Temp_Fulfilled).filter(
        Temp_Fulfilled.reservation_id == reservation.id,
    ).all()

    if not temp_items:
        legacy_item = session.query(Temp_Fulfilled).filter(
            Temp_Fulfilled.reservation_id == None,
            Temp_Fulfilled.user_id == reservation.user_id,
            Temp_Fulfilled.post_id == reservation.post_id,
        ).order_by(Temp_Fulfilled.created_at).first()
        if legacy_item:
            temp_items = [legacy_item]

    deleted_count = len(temp_items)
    for temp_item in temp_items:
        session.delete(temp_item)
    return deleted_count


def mark_reserved_group_message_removed_by_admin(reservation, post, client):
    if not has_reserved_group_message(reservation):
        return False
    caption = (
        f"{build_reserved_group_caption(reservation, post, client)}\n"
        f"❌ Заказ расформирован администратором: {format_datetime(now_local())}"
    )
    return edit_reserved_group_message(reservation.reserved_group_message_id, post, caption)


def create_reservation_from_queue(session, queued_entry, post, stats=None):
    queued_client = session.query(Clients).filter(
        Clients.user_id == queued_entry.user_id
    ).first()
    if not queued_client:
        logger.warning(
            "Wait queue entry removed because client is missing: queue_id=%s user_id=%s post_id=%s",
            queued_entry.id,
            queued_entry.user_id,
            queued_entry.post_id,
        )
        session.delete(queued_entry)
        if stats is not None:
            stats["orphan_queue_deleted"] = stats.get("orphan_queue_deleted", 0) + 1
        return None

    queued_reservation = Reservations(
        user_id=queued_entry.user_id,
        post_id=queued_entry.post_id,
        quantity=1,
        is_fulfilled=False,
        old_price=post.price if post else 0,
        created_at=now_local(),
    )
    session.add(queued_reservation)
    session.flush()
    add_reservation_stat_event(
        session,
        RESERVATION_STATS_EVENT_CREATED,
        queued_reservation,
        event_time=queued_reservation.created_at,
    )
    queued_entry.temp_fulfilled = True
    return {
        "reservation_id": queued_reservation.id,
        "user_id": queued_entry.user_id,
        "post_id": queued_entry.post_id,
        "client_name": queued_client.name if queued_client else None,
    }


def promote_next_queue_entry(session, post, stats=None, lock=False):
    if not post:
        return None

    while True:
        query = session.query(TempReservations).filter(
            TempReservations.post_id == post.id,
            TempReservations.temp_fulfilled == False,
        ).order_by(TempReservations.created_at)
        if lock:
            query = query.with_for_update()

        next_in_queue = query.first()
        if not next_in_queue:
            return None

        queued_notification = create_reservation_from_queue(session, next_in_queue, post, stats=stats)
        if queued_notification:
            return queued_notification


def send_queue_transfer_notifications(queued_notifications):
    for item in queued_notifications:
        if not item:
            continue
        try:
            with Session(bind=engine) as session:
                reservation = session.query(Reservations).filter(
                    Reservations.id == item["reservation_id"],
                ).first()
                post = get_post_or_snapshot(session, item["post_id"])
                client = session.query(Clients).filter(
                    Clients.user_id == item["user_id"],
                ).first()
                if reservation and post and client:
                    send_reserved_group_message(session, reservation, post, client)

            safe_send_message(
                bot,
                item["user_id"],
                "Ваш товар в очереди стал доступен и добавлен в вашу корзину.",
                logger=logger,
            )
        except Exception as exc:
            logger.warning(
                "Queue transfer notification failed for reservation_id=%s: %s",
                item.get("reservation_id"),
                exc,
            )


def delete_delivery_collection_items_for_delivery_entries(session, delivery_entries, stats=None):
    delivery_ids = [delivery_entry.id for delivery_entry in delivery_entries if delivery_entry and delivery_entry.id]
    if not delivery_ids:
        return 0

    collection_items = session.query(DeliveryCollectionItem).filter(
        DeliveryCollectionItem.delivery_id.in_(delivery_ids),
    ).with_for_update().all()
    deleted_count = len(collection_items)
    for collection_item in collection_items:
        session.delete(collection_item)

    if stats is not None:
        stats["delivery_collection_items_removed"] = (
            stats.get("delivery_collection_items_removed", 0) + deleted_count
        )
    return deleted_count


def cleanup_or_refresh_for_delivery_by_phone(session, phone):
    stats = empty_cart_cleanup_stats()
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return stats

    with session.no_autoflush:
        related_clients = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(normalized_phone))
        ).all()
    related_user_ids = sorted({client.user_id for client in related_clients})
    if not related_user_ids:
        return stats

    with session.no_autoflush:
        delivery_entries = session.query(ForDelivery).filter(
            or_(
                ForDelivery.phone.in_(phone_variants(normalized_phone)),
                ForDelivery.user_id.in_(related_user_ids),
            )
        ).all()

    entries_to_remove = []
    for delivery_entry in delivery_entries:
        processed_sum = calculate_delivery_total_for_user_ids(
            session,
            related_user_ids,
            get_delivery_cutoff_at(delivery_entry),
        )
        if processed_sum < DELIVERY_THRESHOLD:
            entries_to_remove.append(delivery_entry)
            continue
        if delivery_entry.total_sum != processed_sum:
            delivery_entry.total_sum = processed_sum
            stats["for_delivery_updated"] += 1

    if entries_to_remove:
        delete_delivery_collection_items_for_delivery_entries(session, entries_to_remove, stats)
        for delivery_entry in entries_to_remove:
            session.delete(delivery_entry)
        stats["for_delivery_removed"] = len(entries_to_remove)
    return stats


def remove_unprocessed_reservation_from_cart(session, reservation, post, stats, queued_notifications):
    client = session.query(Clients).filter(Clients.user_id == reservation.user_id).first()
    if post:
        mark_reserved_group_message_removed_by_admin(reservation, post, client)
    else:
        stats["missing_posts"] += 1

    quantity_to_release = max(int(reservation.quantity or 0), 1)
    post_id = reservation.post_id
    session.delete(reservation)
    stats["deleted"] += 1
    stats["unprocessed_deleted"] += 1

    for _ in range(quantity_to_release):
        queued_notification = promote_next_queue_entry(session, post, stats=stats)
        if queued_notification:
            queued_notifications.append(queued_notification)
            stats["queued"] += 1
            continue

        if post:
            return_post_stock_to_today(post)
            stats["returned"] += 1

    return post_id


def remove_processed_reservation_from_cart(session, reservation, post, stats, queued_notifications):
    client = session.query(Clients).filter(Clients.user_id == reservation.user_id).first()
    if post:
        mark_reserved_group_message_removed_by_admin(reservation, post, client)
    else:
        stats["missing_posts"] += 1

    stats["temp_deleted"] += delete_temp_fulfilled_for_reservation(session, reservation)
    quantity_to_release = max(int(reservation.quantity or 0), 1)
    post_id = reservation.post_id
    session.delete(reservation)
    stats["deleted"] += 1
    stats["processed_deleted"] += 1

    for _ in range(quantity_to_release):
        queued_notification = promote_next_queue_entry(session, post, stats=stats)
        if queued_notification:
            queued_notifications.append(queued_notification)
            stats["queued"] += 1
            continue

        if post:
            return_post_stock_to_today(post)
            stats["returned"] += 1

    return post_id


def return_post_stock_to_today(post, quantity=1):
    if not post:
        return 0
    post.quantity = int(post.quantity or 0) + max(int(quantity or 0), 1)
    post.created_at = Posts.next_created_at()
    return post.quantity


def remove_reservation_from_cart(session, reservation, stats, queued_notifications):
    post = session.query(Posts).filter(Posts.id == reservation.post_id).first()
    if reservation.is_fulfilled:
        return remove_processed_reservation_from_cart(session, reservation, post, stats, queued_notifications)
    return remove_unprocessed_reservation_from_cart(session, reservation, post, stats, queued_notifications)


def remove_reservation_without_channel_return(session, reservation, stats):
    post = get_post_or_snapshot(session, reservation.post_id)
    client = session.query(Clients).filter(Clients.user_id == reservation.user_id).first()
    if post:
        mark_reserved_group_message_removed_by_admin(reservation, post, client)
    else:
        stats["missing_posts"] += 1

    if reservation.is_fulfilled:
        stats["temp_deleted"] += delete_temp_fulfilled_for_reservation(session, reservation)
        stats["processed_deleted"] += 1
    else:
        stats["unprocessed_deleted"] += 1

    stats["not_returned_to_channel"] += max(int(reservation.quantity or 0), 1)
    session.delete(reservation)
    stats["deleted"] += 1


def clear_client_cart(user_id, processed_only=False, reservation_id=None):
    stats = empty_cart_cleanup_stats()
    queued_notifications = []
    channel_post_ids = set()

    with Session(bind=engine) as session:
        if reservation_id is not None:
            reservation = session.query(Reservations).filter(
                Reservations.id == reservation_id,
            ).first()
            if not reservation:
                return stats
            source_client = session.query(Clients).filter(
                Clients.user_id == reservation.user_id,
            ).first()
            source_phone = source_client.phone if source_client else None
            channel_post_ids.add(remove_reservation_from_cart(session, reservation, stats, queued_notifications))
        else:
            source_client = session.query(Clients).filter(Clients.user_id == user_id).first()
            if not source_client:
                return stats

            related_clients = session.query(Clients).filter(
                Clients.phone.in_(phone_variants(source_client.phone))
            ).all()
            related_user_ids = [client.user_id for client in related_clients] or [user_id]
            query = session.query(Reservations).filter(
                Reservations.user_id.in_(related_user_ids),
            ).order_by(Reservations.id)
            if processed_only:
                query = query.filter(Reservations.is_fulfilled == True)

            reservations = query.all()
            source_phone = source_client.phone
            for reservation in reservations:
                channel_post_ids.add(remove_reservation_from_cart(session, reservation, stats, queued_notifications))

        if source_phone:
            merge_cart_cleanup_stats(stats, cleanup_or_refresh_for_delivery_by_phone(session, source_phone))

        session.commit()

    for post_id in channel_post_ids:
        if post_id:
            post = Posts.get_row_by_id(post_id)
            if not post:
                continue
            update_channel_post_message(post)
    send_queue_transfer_notifications(queued_notifications)
    return stats


def clear_client_cart_without_channel_return(user_id):
    stats = empty_cart_cleanup_stats()

    with Session(bind=engine) as session:
        source_client = session.query(Clients).filter(Clients.user_id == user_id).first()
        if not source_client:
            return stats

        related_clients = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(source_client.phone))
        ).all()
        related_user_ids = [client.user_id for client in related_clients] or [user_id]
        reservations = session.query(Reservations).filter(
            Reservations.user_id.in_(related_user_ids),
        ).order_by(Reservations.id).all()

        for reservation in reservations:
            remove_reservation_without_channel_return(session, reservation, stats)

        merge_cart_cleanup_stats(stats, cleanup_or_refresh_for_delivery_by_phone(session, source_client.phone))
        session.commit()

    return stats


def delete_client_active_records(client_user_id, phone):
    stats = empty_cart_cleanup_stats()
    queued_notifications = []
    channel_post_ids = set()

    with Session(bind=engine) as session:
        wait_queue_entries = session.query(TempReservations).filter(
            TempReservations.user_id == client_user_id,
        ).with_for_update().all()
        stats["wait_queue_deleted"] += len(wait_queue_entries)
        for wait_queue_entry in wait_queue_entries:
            session.delete(wait_queue_entry)
        session.flush()

        reservations = session.query(Reservations).filter(
            Reservations.user_id == client_user_id,
        ).order_by(Reservations.id).with_for_update().all()
        for reservation in reservations:
            channel_post_ids.add(remove_reservation_from_cart(session, reservation, stats, queued_notifications))

        legacy_temp_items = session.query(Temp_Fulfilled).filter(
            Temp_Fulfilled.user_id == client_user_id,
            Temp_Fulfilled.reservation_id == None,
        ).all()
        stats["temp_deleted"] += len(legacy_temp_items)
        for temp_item in legacy_temp_items:
            session.delete(temp_item)

        merge_cart_cleanup_stats(stats, cleanup_or_refresh_for_delivery_by_phone(session, phone))
        session.commit()

    for post_id in channel_post_ids:
        if post_id:
            post = Posts.get_row_by_id(post_id)
            if not post:
                continue
            update_channel_post_message(post)
    send_queue_transfer_notifications(queued_notifications)
    return stats


# Отображает содержимое корзины и добавляет кнопку для расформирования обработанных товаров
def send_cart_content(chat_id, reservations, user_id):
    with Session(bind=engine) as session:
        products_by_id = get_posts_or_snapshots_by_ids(
            session,
            {reservation.post_id for reservation in reservations},
        )

    for reservation in reservations:
        post = products_by_id.get(reservation.post_id)
        item_markup = types.InlineKeyboardMarkup()
        item_markup.add(
            types.InlineKeyboardButton(
                "Убрать товар",
                callback_data=f"admin_remove_cart_item_{reservation.id}",
            )
        )

        if post:
            caption = build_cart_item_caption(post, reservation)
            try:
                send_photo_or_text(bot, chat_id, post.photo, caption, reply_markup=item_markup)
            except Exception as exc:
                logger.warning(
                    "Admin cart item send failed chat_id=%s reservation_id=%s post_id=%s: %s",
                    chat_id,
                    reservation.id,
                    reservation.post_id,
                    exc,
                )
                safe_send_message(
                    bot,
                    chat_id,
                    f"{caption}\n\nНе удалось отправить фото товара. Подробности записаны в лог.",
                    reply_markup=item_markup,
                    logger=logger,
                )
        else:
            safe_send_message(
                bot,
                chat_id,
                f"Товар с ID {reservation.post_id} не найден!",
                reply_markup=item_markup,
                logger=logger,
            )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Расформировать обработанные", callback_data=f"clear_processed_{user_id}"))
    markup.add(types.InlineKeyboardButton("Расформировать полностью", callback_data=f"clear_full_cart_{user_id}"))
    markup.add(types.InlineKeyboardButton("Прикарманить", callback_data=f"clear_no_channel_{user_id}"))
    safe_send_message(bot, chat_id, "Выберите действие:", reply_markup=markup, logger=logger)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "admin_remove_cart_item_"))
def handle_admin_remove_cart_item(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    reservation_id = parse_callback_int_suffix(call, "admin_remove_cart_item_", "Некорректный товар.")
    if reservation_id is None:
        return

    safe_answer_callback_query(call.id, "Удаляю товар из корзины.")
    try:
        stats = clear_client_cart(call.from_user.id, reservation_id=reservation_id)
    except Exception as exc:
        logger.exception("Admin cart item remove failed for reservation_id=%s", reservation_id)
        safe_send_message(
            bot,
            call.message.chat.id,
            "Ошибка при удалении товара. Подробности записаны в лог.",
            logger=logger,
        )
        return

    if not stats.get("deleted"):
        safe_send_message(bot, call.message.chat.id, "Товар уже удален.", logger=logger)
        return

    safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)
    safe_send_message(bot, call.message.chat.id, format_cart_cleanup_result(stats), logger=logger)


# Callback для кнопки "Расформировать обработанные"
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "clear_processed_"))
def handle_clear_processed(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    user_id = parse_callback_int_suffix(call, "clear_processed_")
    if user_id is None:
        return

    safe_answer_callback_query(call.id, "Расформировываю обработанные товары.")
    try:
        stats = clear_processed(user_id)
    except Exception as exc:
        logger.exception("Clear processed cart failed for user_id=%s", user_id)
        safe_send_message(
            bot,
            call.message.chat.id,
            "Ошибка при расформировании обработанных. Подробности записаны в лог.",
            logger=logger,
        )
        return

    if stats.get("deleted", 0) > 0:
        safe_send_message(
            bot,
            call.message.chat.id,
            f"Обработанные товары удалены из корзины. {format_cart_cleanup_result(stats)}",
            logger=logger,
        )
    else:
        safe_send_message(bot, call.message.chat.id, "В корзине нет обработанных товаров для удаления.", logger=logger)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "clear_full_cart_"))
def handle_clear_full_cart(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    user_id = parse_callback_int_suffix(call, "clear_full_cart_", "Некорректный клиент.")
    if user_id is None:
        return

    safe_answer_callback_query(call.id, "Расформировываю корзину полностью.")
    try:
        stats = clear_client_cart(user_id, processed_only=False)
    except Exception as exc:
        logger.exception("Clear full cart failed for user_id=%s", user_id)
        safe_send_message(
            bot,
            call.message.chat.id,
            "Ошибка при полном расформировании. Подробности записаны в лог.",
            logger=logger,
        )
        return

    if stats.get("deleted", 0) > 0:
        safe_send_message(
            bot,
            call.message.chat.id,
            f"Корзина полностью расформирована. {format_cart_cleanup_result(stats)}",
            logger=logger,
        )
    else:
        safe_send_message(bot, call.message.chat.id, "Корзина уже пуста.", logger=logger)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "clear_no_channel_"))
def handle_clear_without_channel_return(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    user_id = parse_callback_int_suffix(call, "clear_no_channel_", "Некорректный клиент.")
    if user_id is None:
        return

    safe_answer_callback_query(call.id, "Удаляю корзину без возврата на канал.")
    try:
        stats = clear_client_cart_without_channel_return(user_id)
    except Exception as exc:
        logger.exception("Clear cart without channel return failed for user_id=%s", user_id)
        safe_send_message(
            bot,
            call.message.chat.id,
            "Ошибка при удалении без возврата на канал. Подробности записаны в лог.",
            logger=logger,
        )
        return

    if stats.get("deleted", 0) > 0:
        safe_send_message(
            bot,
            call.message.chat.id,
            f"Корзина удалена без возврата товара на канал. {format_cart_cleanup_result(stats)}",
            logger=logger,
        )
    else:
        safe_send_message(bot, call.message.chat.id, "Корзина уже пуста.", logger=logger)


# Удаляет обработанные товары из корзины пользователя
def clear_processed(user_id):
    return clear_client_cart(user_id, processed_only=True)


# Callback для инлайн-кнопок "Просмотреть корзину"
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "view_cart_"))
def callback_view_cart(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    client_id = parse_callback_int_suffix(call, "view_cart_", "Некорректный выбор клиента.")
    if client_id is None:
        return

    # Получаем данные клиента
    client = Clients.get_row_by_id(client_id)

    if not client:
        safe_answer_callback_query(call.id, "Пользователь не найден.", show_alert=True)
        return

    safe_answer_callback_query(call.id)
    show_cart_for_client(call.message.chat.id, client)

# Удаление клиента по номеру телефона
@bot.message_handler(func=lambda message: message.text == "🗑 Удалить клиента 📞")
def delete_client_by_phone(message):
    user_id = message.chat.id
    role = get_client_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin","supreme_leader"]:
        safe_send_message(bot, user_id, "У вас недостаточно прав.", logger=logger)
        return
    safe_send_message(bot, message.chat.id, "Введите номер телефона клиента для удаления:", logger=logger)
    set_user_state(message.chat.id, "DELETE_CLIENT_PHONE")

# Функция для удаления клиента по номеру телефона
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "DELETE_CLIENT_PHONE")
def process_delete_client_phone(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем права пользователя
    if role not in ["admin","supreme_leader"]:
        safe_send_message(bot, user_id, "У вас недостаточно прав.", logger=logger)
        return

    text = require_text_message(message, "Введите номер телефона клиента текстом.")
    if text is None:
        return
    phone = normalize_phone(text)  # Убираем лишние пробелы

    try:
        # Получаем клиента по номеру телефона
        client = Clients.get_row_by_phone(phone)

        if client:
            client_user_id = client.user_id  # Извлекаем user_id клиента

            # Проверяем, не выполняются ли действия с защищённым пользователем
            if client_user_id == protected_user_id:
                safe_send_message(
                    bot,
                    user_id,
                    f"Клиент с номером телефона {phone} защищен от удаления.",
                    logger=logger,
                )
                return

            # Добавляем клиента в черный список (защищенный пользователь не будет добавлен)
            if client_user_id != protected_user_id:
                BlackList.insert(user_id=client_user_id, phone=phone)

            cleanup_stats = delete_client_active_records(client_user_id, phone)

            # Удаляем клиента из таблицы clients
            Clients.delete_row(client.id)

            safe_send_message(
                bot,
                user_id,
                f"Клиент с номером телефона {phone} успешно удалён. "
                f"{format_cart_cleanup_result(cleanup_stats)}",
                logger=logger,
            )
        else:
            safe_send_message(bot, user_id, f"Клиент с номером телефона {phone} не найден.", logger=logger)
    except Exception as e:
        logger.exception("Client delete failed for phone=%s user_id=%s: %s", phone, user_id, e)
        safe_send_message(bot, user_id, "Произошла ошибка при удалении данных. Подробности записаны в лог.", logger=logger)
    finally:
        clear_user_state(user_id)

# Возможность установить клиенту статус рабочего
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, ("set_worker_", "set_client_")))
def handle_set_role(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    prefix = "set_worker_" if call.data.startswith("set_worker_") else "set_client_"
    client_id = parse_callback_int_suffix(call, prefix)
    if client_id is None:
        return
    new_role = "worker" if "set_worker" in call.data else "client"

    client = Clients.get_row_by_id(client_id)

    if not client:
        safe_answer_callback_query(call.id, f"Клиент с ID {client_id} не найден.")
        return

    update_result = Clients.update_row_for_work(client.user_id, {"role": new_role})

    if update_result:
        safe_answer_callback_query(call.id, f"Роль успешно изменена на {new_role}.")
        safe_send_message(
            bot,
            call.message.chat.id,
            f"Роль пользователя с ID {client_id} обновлена на {new_role}.",
            logger=logger,
        )
    else:
        safe_answer_callback_query(call.id, "Не удалось обновить роль, попробуйте позже.")

# Проверка на админа
def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    role = get_client_role(user_id)  # Предполагается, что эта функция получает роль из Clients
    return role and "admin" in role  # Если роль хранится как строка или список

def is_leader(user_id):
    """Проверяет, является ли пользователь администратором."""
    role = get_client_role(user_id)  # Предполагается, что эта функция получает роль из Clients
    return role and "supreme_leader" in role  # Если роль хранится как строка или список

def is_audit(user_id):
    """Проверяет, является ли пользователь Аудитом"""
    role = get_client_role(user_id)
    return role and "audit" in role

def get_temp_post_payload(user_id):
    payload = temp_post_data.get(user_id, {})
    return dict(payload) if isinstance(payload, dict) else {}


def get_temp_post_id_or_reset(user_id):
    payload = get_temp_post_payload(user_id)
    post_id = payload.get("post_id")
    if post_id:
        return post_id

    clear_user_state(user_id)
    temp_post_data.pop(user_id, None)
    safe_send_message(bot, user_id, "Данные поста потерялись. Нажмите «Новый пост» и начните заново.", logger=logger)
    return None


# Новый пост
@bot.message_handler(func=lambda message: message.text == "➕ Новый пост")
def create_new_post(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    if role not in POST_EDITOR_ROLES:
        safe_send_message(bot, user_id, "У вас нет прав доступа к этой функции.", logger=logger)
        return

    try:
        reserved_post_id = Posts.reserve_next_id(chat_id=user_id)
    except Exception as exc:
        logger.exception("Post ID reservation failed for user_id=%s: %s", user_id, exc)
        safe_send_message(bot, user_id, "Не удалось зарезервировать ID товара. Попробуйте ещё раз.", logger=logger)
        return

    try:
        temp_post_data[message.chat.id] = {"post_id": reserved_post_id}
        set_user_state(message.chat.id, CreatingPost.CREATING_POST)
        safe_send_message(
            bot,
            message.chat.id,
            f"Id товара: {reserved_post_id}\n"
            "Отправьте фото",
            logger=logger,
        )
    except Exception as exc:
        Posts.release_reserved_id(reserved_post_id, chat_id=user_id)
        temp_post_data.pop(message.chat.id, None)
        clear_user_state(message.chat.id)
        logger.exception("Post creation state init failed for user_id=%s: %s", user_id, exc)
        safe_send_message(bot, user_id, "Не удалось начать создание поста. Попробуйте ещё раз.", logger=logger)

# Фото
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.chat.id
    role = get_client_role(user_id)
    state = get_user_state(message.chat.id)
    if role not in POST_EDITOR_ROLES:
        safe_send_message(
            bot,
            user_id,
            "Если у вас возникли вопросы, задайте их в чате поддержки",
            logger=logger,
        )
        return
    if state == CreatingPost.CREATING_POST:
        payload = get_temp_post_payload(message.chat.id)
        if not payload.get("post_id"):
            clear_user_state(message.chat.id)
            temp_post_data.pop(message.chat.id, None)
            safe_send_message(
                bot,
                message.chat.id,
                "Сессия создания поста потерялась. Нажмите «Новый пост» и отправьте фото заново.",
                logger=logger,
            )
            return

        photos = getattr(message, "photo", None) or []
        if not photos:
            safe_send_message(bot, message.chat.id, "Не удалось получить фото. Отправьте фото товара ещё раз.", logger=logger)
            return

        temp_post_data.update_dict(message.chat.id, {"photo": photos[-1].file_id})
        safe_send_message(bot, message.chat.id, "Теперь введите цену на товар.", logger=logger)
    else:
        safe_send_message(bot, message.chat.id, "Неправильная последовательность действий", logger=logger)

# Описание
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.CREATING_POST)
def handle_post_details(message):
    chat_id = message.chat.id
    data = get_temp_post_payload(chat_id)
    if not data.get("post_id"):
        clear_user_state(chat_id)
        temp_post_data.pop(chat_id, None)
        safe_send_message(
            bot,
            chat_id,
            "Сессия создания поста потерялась. Нажмите «Новый пост» и начните заново.",
            logger=logger,
        )
        return

    if "photo" not in data:
        safe_send_message(bot, chat_id, "Сначала отправьте фото товара.", logger=logger)
        return

    text = require_text_message(message, "Введите данные поста текстом.")
    if text is None:
        return

    if "price" not in data:
        if not text.isdigit():
            safe_send_message(
                bot,
                chat_id,
                "Ошибка: Цена должна быть числом. Попробуйте снова.",
                logger=logger,
            )
            return
        temp_post_data.update_dict(chat_id, {"price": text})
        safe_send_message(bot, chat_id, "Введите описание товара.", logger=logger)
    elif "description" not in data:
        # Поле "description" сохраняем без проверки, но заменяем "*" на "x"
        description = text.replace("*", "x")
        temp_post_data.update_dict(chat_id, {"description": description})
        safe_send_message(bot, chat_id, "Введите количество товара.", logger=logger)
    elif "quantity" not in data:
        if not text.isdigit():
            safe_send_message(
                bot,
                chat_id,
                "Ошибка: Количество должно быть числом. Попробуйте снова.",
                logger=logger,
            )
            return
        quantity = int(text)
        if quantity <= 0:
            safe_send_message(bot, chat_id, "Ошибка: Количество должно быть больше нуля. Попробуйте снова.", logger=logger)
            return
        data = dict(temp_post_data.update_dict(chat_id, {"quantity": quantity}))

        # Сохраняем пост
        try:
            created_post_id = save_post(
                chat_id,
                data["photo"],
                data["price"],
                data["description"],
                data["quantity"],
                post_id=data.get("post_id"),
            )
        except Exception as exc:
            Posts.release_reserved_id(data.get("post_id"), chat_id=chat_id)
            temp_post_data.pop(chat_id, None)
            clear_user_state(chat_id)
            logger.exception("Post save failed for user_id=%s: %s", chat_id, exc)
            safe_send_message(
                bot,
                chat_id,
                "Не удалось сохранить пост. Нажмите «Новый пост» и попробуйте ещё раз.",
                logger=logger,
            )
            return

        safe_send_message(
            bot,
            chat_id,
            f"Id товара: {created_post_id}\n"
            "Ваш пост успешно создан!",
            logger=logger,
        )

        # Очищаем состояние пользователя после завершения
        temp_post_data.pop(chat_id, None)
        clear_user_state(chat_id)

# Управление постами
@bot.message_handler(func=lambda message: message.text == "📄 Посты")
def manage_posts(message):
    user_id = message.chat.id
    message_id = message.message_id  # ID самого запроса

    # Удаляем запрос пользователя сразу же
    try:
        safe_delete_message(bot, user_id, message_id, logger=logger)
    except Exception as e:
        logger.warning("Post list request message delete failed for user_id=%s: %s", user_id, e)

    role = get_client_role(user_id)

    # Проверяем, имеет ли пользователь соответствующую роль
    if role not in POST_EDITOR_ROLES:
        safe_send_message(bot, user_id, "У вас нет прав доступа к этой функции.", logger=logger)
        return

    # Убедимся, что user_last_message_id[user_id] - это список
    if user_id not in user_last_message_id:
        user_last_message_id[user_id] = []
    elif not isinstance(user_last_message_id[user_id], list):
        user_last_message_id[user_id] = [user_last_message_id[user_id]]

    # Удаляем предыдущие сообщения, если они есть
    for msg_id in user_last_message_id[user_id]:
        try:
            safe_delete_message(bot, user_id, msg_id, logger=logger)
        except Exception as e:
            logger.warning("Post list old message delete failed for user_id=%s message_id=%s: %s", user_id, msg_id, e)

    # Очищаем список сообщений пользователя после удаления
    user_last_message_id[user_id] = []

    try:
        # Получаем посты в зависимости от роли пользователя
        if role in ["admin", "supreme_leader"]:
            posts = Posts.get_all_posts()  # Используем метод класса для получения всех постов
        else:
            posts = Posts.get_user_posts(
                user_id)  # Используем метод класса для получения постов только текущего пользователя

    except Exception as e:
        logger.exception("Post list fetch failed for user_id=%s: %s", user_id, e)
        error_msg = safe_send_message(bot, user_id, "Не удалось получить список постов. Попробуйте позже.", logger=logger)
        if error_msg:
            user_last_message_id[user_id].append(error_msg.message_id)
        return

    if not posts:
        no_posts_msg = safe_send_message(bot, user_id, "Нет доступных постов.", logger=logger)
        if no_posts_msg:
            user_last_message_id[user_id].append(no_posts_msg.message_id)
        return

    # Выводим информацию о каждом посте
    for post in posts:
        post_id = post.id
        description = post.description
        price = post.price
        quantity = post.quantity
        photo = post.photo  # Если фото есть

        # Создаем клавиатуру для управления постом
        markup = InlineKeyboardMarkup()
        edit_btn = InlineKeyboardButton(
            "✏️ Изменить", callback_data=f"edit_post_{post_id}"
        )
        delete_btn = InlineKeyboardButton(
            "🗑 Удалить", callback_data=f"delete_post_{post_id}"
        )
        markup.add(edit_btn, delete_btn)

        # Отправляем сообщение с фото или текстом
        try:
            if photo:
                msg = safe_send_photo(
                    bot,
                    user_id,
                    photo,
                    caption=f"Пост #{post_id}:\n"
                            f"📍 Описание: {description}\n"
                            f"💰 Цена: {price} ₽\n"
                            f"📦 Количество: {quantity}",
                    reply_markup=markup,
                    logger=logger,
                )
            else:
                msg = safe_send_message(
                    bot,
                    user_id,
                    f"Пост #{post_id}:\n"
                    f"📍 Описание: {description}\n"
                    f"💰 Цена: {price} ₽\n"
                    f"📦 Количество: {quantity}",
                    reply_markup=markup,
                    logger=logger,
                )
            # Сохраняем ID отправленных сообщений
            if msg:
                user_last_message_id[user_id].append(msg.message_id)
        except Exception as e:
            logger.warning("Post preview send failed for user_id=%s post_id=%s: %s", user_id, post_id, e)
            error_msg = safe_send_message(bot, user_id, f"Не удалось отправить предпросмотр поста #{post_id}.", logger=logger)
            if error_msg:
                user_last_message_id[user_id].append(error_msg.message_id)

@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "edit_post_"))
def edit_post(call):
    post_id = parse_callback_int_suffix(call, "edit_post_")
    if post_id is None:
        return
    user_id = call.from_user.id

    # Проверяем права на редактирование
    role = get_client_role(user_id)
    if role not in POST_EDITOR_ROLES:
        safe_answer_callback_query(
            callback_query_id=call.id,
            text="У вас нет прав доступа к этой функции.",
            show_alert=True,
        )
        return

    # Сохраняем временные данные о посте, который редактируется
    temp_post_data[user_id] = {"post_id": post_id}

    # Отправляем инлайн-клавиатуру с вариантами редактирования
    markup = InlineKeyboardMarkup()
    edit_price_btn = InlineKeyboardButton("💰 Цена", callback_data=f"edit_price_{post_id}")
    edit_description_btn = InlineKeyboardButton("📍 Описание", callback_data=f"edit_description_{post_id}")
    edit_quantity_btn = InlineKeyboardButton("📦 Количество", callback_data=f"edit_quantity_{post_id}")
    markup.add(edit_price_btn, edit_description_btn, edit_quantity_btn)

    safe_answer_callback_query(call.id)
    # Обновляем сообщение или отправляем новое с клавиатурой
    if call.message.text:
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Что вы хотите поменять?",
            reply_markup=markup
        )
    else:
        msg = safe_send_message(
            bot,
            call.message.chat.id,
            "Что вы хотите поменять?",
            reply_markup=markup,
            logger=logger,
        )
        if msg:
            user_last_message_id.setdefault(user_id, []).append(msg.message_id)  # Сохраняем ID сообщения

# Обработчик кнопки "Редактировать цену"
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "edit_price_"))
def handle_edit_price(call):
    user_id = call.from_user.id
    post_id = parse_callback_int_suffix(call, "edit_price_")
    if post_id is None:
        return

    # Устанавливаем состояние пользователя
    set_user_state(user_id, CreatingPost.EDITING_POST_PRICE)
    temp_post_data[user_id] = {"post_id": post_id}

    safe_answer_callback_query(call.id)
    # Просим пользователя ввести новую цену
    safe_send_message(bot, user_id, "Введите новую цену для поста:", logger=logger)

# Обработчик кнопки "Редактировать описание"
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "edit_description_"))
def handle_edit_description(call):
    user_id = call.from_user.id
    post_id = parse_callback_int_suffix(call, "edit_description_")
    if post_id is None:
        return

    # Устанавливаем состояние пользователя
    set_user_state(user_id, CreatingPost.EDITING_POST_DESCRIPTION)
    temp_post_data[user_id] = {"post_id": post_id}

    safe_answer_callback_query(call.id)
    # Просим ввести новое описание
    safe_send_message(bot, user_id, "Введите новое описание для поста:", logger=logger)

# Обработчик кнопки "Редактировать количество"
@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "edit_quantity_"))
def handle_edit_quantity(call):
    user_id = call.from_user.id
    post_id = parse_callback_int_suffix(call, "edit_quantity_")
    if post_id is None:
        return

    # Устанавливаем состояние пользователя
    set_user_state(user_id, CreatingPost.EDITING_POST_QUANTITY)
    temp_post_data[user_id] = {"post_id": post_id}

    safe_answer_callback_query(call.id)
    # Просим ввести новое количество
    safe_send_message(bot, user_id, "Введите новое количество товара:", logger=logger)

# Обработчик ввода новой цены
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.EDITING_POST_PRICE)
def edit_post_price(message):
    user_id = message.chat.id
    post_id = get_temp_post_id_or_reset(user_id)
    if not post_id:
        return

    text = require_text_message(message, "Введите новую цену текстом.")
    if text is None:
        return

    # Проверка, что введено число
    if not text.isdigit():
        safe_send_message(bot, user_id, "Ошибка: Цена должна быть числом. Попробуйте снова.", logger=logger)
        return

    new_price = int(text)
    temp_post_data.update_dict(user_id, {"price": new_price})

    try:
        post = Posts.get_row_by_id(post_id)  # Получаем старые данные поста
        if not post:
            safe_send_message(bot, user_id, "Пост не найден.", logger=logger)
            return
        success, msg = Posts.update_row(
            post_id=post_id,
            price=new_price,
            description=post.description,
            quantity=post.quantity
        )
        if success:
            updated_post = Posts.get_row_by_id(post_id)
            if updated_post and updated_post.is_sent:
                update_channel_post_message(updated_post)
            safe_send_message(bot, user_id, "Цена успешно обновлена!", logger=logger)
        else:
            safe_send_message(bot, user_id, f"Ошибка обновления цены: {msg}", logger=logger)
    except Exception as e:
        logger.exception("Post price update failed for user_id=%s post_id=%s: %s", user_id, post_id, e)
        safe_send_message(bot, user_id, "Ошибка обновления цены. Попробуйте позже.", logger=logger)
    finally:
        temp_post_data.pop(user_id, None)
        clear_user_state(user_id)  # Сбрасываем состояние пользователя

# Обработчик ввода нового описания
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.EDITING_POST_DESCRIPTION)
def edit_post_description(message):
    user_id = message.chat.id
    post_id = get_temp_post_id_or_reset(user_id)
    if not post_id:
        return

    new_description = require_text_message(message, "Введите новое описание текстом.")  # Новое описание
    if new_description is None:
        return
    temp_post_data.update_dict(user_id, {"description": new_description})

    try:
        post = Posts.get_row_by_id(post_id)  # Получаем старые данные поста
        if not post:
            safe_send_message(bot, user_id, "Пост не найден.", logger=logger)
            return
        success, msg = Posts.update_row(
            post_id=post_id,
            price=post.price,
            description=new_description,
            quantity=post.quantity
        )
        if success:
            updated_post = Posts.get_row_by_id(post_id)
            if updated_post and updated_post.is_sent:
                update_channel_post_message(updated_post)
            safe_send_message(bot, user_id, "Описание успешно обновлено!", logger=logger)
        else:
            safe_send_message(bot, user_id, f"Ошибка обновления описания: {msg}", logger=logger)
    except Exception as e:
        logger.exception("Post description update failed for user_id=%s post_id=%s: %s", user_id, post_id, e)
        safe_send_message(bot, user_id, "Ошибка обновления описания. Попробуйте позже.", logger=logger)
    finally:
        temp_post_data.pop(user_id, None)
        clear_user_state(user_id)  # Сбрасываем состояние пользователя

# Обработчик ввода нового количества
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.EDITING_POST_QUANTITY)
def edit_post_quantity(message):
    user_id = message.chat.id
    post_id = get_temp_post_id_or_reset(user_id)
    if not post_id:
        return

    text = require_text_message(message, "Введите новое количество текстом.")
    if text is None:
        return

    # Проверяем, что ввод является числом
    if not text.isdigit():
        safe_send_message(bot, user_id, "Ошибка: Количество должно быть числом. Попробуйте снова.", logger=logger)
        return

    new_quantity = int(text)
    temp_post_data.update_dict(user_id, {"quantity": new_quantity})

    try:
        post = Posts.get_row_by_id(post_id)  # Получаем старые данные
        if not post:
            safe_send_message(bot, user_id, "Пост не найден.", logger=logger)
            return
        success, msg = Posts.update_row(
            post_id=post_id,
            price=post.price,
            description=post.description,
            quantity=new_quantity
        )
        if success:
            updated_post = Posts.get_row_by_id(post_id)
            if updated_post and updated_post.is_sent:
                update_channel_post_message(updated_post)
            safe_send_message(bot, user_id, "Количество успешно обновлено!", logger=logger)
        else:
            safe_send_message(bot, user_id, f"Ошибка обновления количества: {msg}", logger=logger)
    except Exception as e:
        logger.exception("Post quantity update failed for user_id=%s post_id=%s: %s", user_id, post_id, e)
        safe_send_message(bot, user_id, "Ошибка обновления количества. Попробуйте позже.", logger=logger)
    finally:
        temp_post_data.pop(user_id, None)
        clear_user_state(user_id)  # Очистка состояния

@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "delete_post_"))
def delete_post_handler(call):
    post_id = parse_callback_int_suffix(call, "delete_post_")
    if post_id is None:
        return
    callback_answered = False
    try:
        with Session(bind=engine) as session:
            post = session.query(Posts).filter(Posts.id == post_id).first()
            if not post:
                safe_answer_callback_query(call.id, "Пост не найден.", show_alert=True)
                return

            has_related_rows = post_has_active_links(session, post_id)
            if has_related_rows:
                post.quantity = 0
                session.commit()
                safe_answer_callback_query(
                    call.id,
                    "Пост связан с заказами, поэтому карточка оставлена в базе, а бронь в канале отключена.",
                    show_alert=True,
                )
                callback_answered = True
                disable_channel_post_reservation(post)
                safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)
                return

        if post and post.message_id:
            safe_answer_callback_query(call.id, "Удаляю пост.")
            callback_answered = True
            deleted_from_channel = safe_delete_message(bot, CHANNEL_ID, post.message_id, logger=logger)
            if not deleted_from_channel:
                disable_channel_post_reservation(post)

        # Удалить пост из базы данных (если успешно)
        result, msg = Posts.delete_row(post_id=post_id)
        if result:
            # Сообщаем о результате
            if not callback_answered:
                safe_answer_callback_query(call.id, "Пост успешно удалён.")
            safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)
        else:
            # Возникает ошибка при удалении поста
            if not callback_answered:
                safe_answer_callback_query(call.id, "Ошибка при удалении поста.", show_alert=True)
            else:
                safe_send_message(bot, call.message.chat.id, "Ошибка при удалении поста.", logger=logger)
    except Exception as e:
        logger.exception("Post delete failed for post_id=%s: %s", post_id, e)
        if not callback_answered:
            safe_answer_callback_query(call.id, "Ошибка при удалении поста.", show_alert=True)
        else:
            safe_send_message(bot, call.message.chat.id, "Ошибка при удалении поста.", logger=logger)

# Кнопка назад
@bot.message_handler(func=lambda message: message.text == "⬅️ Назад")
def go_back(message):
    try:
        # Проверяем роль пользователя и возвращаем соответствующее меню
        if is_admin(message.chat.id):
            markup = admin_main_menu()  # Получаем меню администратора
            safe_send_message(
                bot,
                message.chat.id,
                "Возвращаюсь в главное меню администратора.",
                reply_markup=markup,
                logger=logger,
            )
        elif is_leader(message.chat.id):
            markup = supreme_leader_main_menu()  # Получаем меню лидера
            safe_send_message(
                bot,
                message.chat.id,
                "Возвращаюсь в главное меню лидера.",
                reply_markup=markup,
                logger=logger,
            )
        elif is_audit(message.chat.id):
            markup = audit_main_menu()
            safe_send_message(
                bot,
                message.chat.id,
                "Возвращаюсь в главное меню",
                reply_markup=markup,
                logger=logger,
            )
        else:
            markup = client_main_menu()  # Получаем меню клиента
            safe_send_message(
                bot,
                message.chat.id,
                "Возвращаюсь в главное меню.",
                reply_markup=markup,
                logger=logger,
            )
    except Exception as e:
        # При возникновении исключения отправляем сообщение об ошибке
        logger.exception("Back command failed for chat_id=%s: %s", message.chat.id, e)
        safe_send_message(bot, message.chat.id, "Произошла ошибка. Пожалуйста, попробуйте снова позже.", logger=logger)


def send_channel_post_with_retries(post, caption, markup, source):
    for attempt in range(1, CHANNEL_POST_SEND_ATTEMPTS + 1):
        try:
            if not post.photo:
                return bot.send_message(
                    CHANNEL_ID,
                    text=caption,
                    reply_markup=markup,
                )
            return bot.send_photo(
                CHANNEL_ID,
                photo=post.photo,
                caption=caption,
                reply_markup=markup,
            )
        except Exception as exc:
            retry_after = telegram_retry_after_seconds(exc)
            if not is_transient_telegram_error(exc) or attempt == CHANNEL_POST_SEND_ATTEMPTS:
                raise

            wait_seconds = retry_after + 1 if retry_after is not None else CHANNEL_POST_SEND_RETRY_SECONDS * attempt
            logger.warning(
                "Channel post send retry for post_id=%s source=%s attempt=%s/%s wait=%ss: %s",
                post.id,
                source,
                attempt,
                CHANNEL_POST_SEND_ATTEMPTS,
                wait_seconds,
                exc,
            )
            time.sleep(wait_seconds)


def send_archive_post_copy_best_effort(post_id, photo, group_caption):
    try:
        archive_message = send_photo_or_text(bot, ARCHIVE, photo, group_caption)
    except Exception as exc:
        logger.warning("Archive copy failed for post_id=%s: %s", post_id, exc)
        return False

    if not archive_message:
        logger.warning("Archive copy returned no message for post_id=%s", post_id)
        return False
    return True


def publish_unsent_posts_to_channel(notify_chat_id=None, source="manual"):
    if not channel_post_publish_lock.acquire(blocking=False):
        notify_publish_status(
            notify_chat_id,
            "Отправка постов в канал уже выполняется. Дождитесь окончания, чтобы не создать дубли.",
        )
        logger.info("Channel post publish skipped: already running, source=%s", source)
        return 0

    sent_count = 0
    failed_post_ids = []
    try:
        posts = Posts.get_unsent_posts()
        if not posts:
            notify_publish_status(notify_chat_id, "Нет новых постов для отправки.")
            logger.info("Channel post publish found no unsent posts, source=%s", source)
            return 0

        notify_publish_status(notify_chat_id, f"Начинаю отправку постов в канал. Постов: {len(posts)}.")

        for post in posts:
            post_id = post.id
            with Session(bind=engine) as session:
                live = session.query(Posts).filter(Posts.id == post_id).first()
                if (
                    not live
                    or int(live.quantity or 0) <= 0
                    or live.is_sent
                    or live.message_id is not None
                    or post_has_active_links(session, post_id)
                ):
                    logger.info(
                        "Channel post publish skipped unavailable post_id=%s source=%s",
                        post_id,
                        source,
                    )
                    continue
                photo = live.photo
                creator_user_id = live.chat_id
                caption = build_channel_post_caption(live)
                markup = build_channel_post_markup(live)
                publish_post = SimpleNamespace(id=live.id, photo=live.photo)

            creator_name = Clients.get_name_by_user_id(creator_user_id) or "Неизвестный автор"

            try:
                sent_message = send_channel_post_with_retries(publish_post, caption, markup, source)
            except Exception as exc:
                failed_post_ids.append(post_id)
                logger.exception("Channel post send failed for post_id=%s source=%s: %s", post_id, source, exc)
                time.sleep(1)
                continue

            Posts.mark_as_sent(post_id=post_id, message_id=sent_message.message_id)
            sent_count += 1

            group_caption = f"Id товара: {post_id}\nПост был создан пользователем: {creator_name}\n\n{caption}"
            send_archive_post_copy_best_effort(post_id, photo, group_caption)

            time.sleep(4)

        if source == "auto" and sent_count and not failed_post_ids:
            send_phoenix_channel_footer()

        if failed_post_ids:
            failed_preview = ", ".join(str(post_id) for post_id in failed_post_ids[:20])
            extra = "" if len(failed_post_ids) <= 20 else f" и ещё {len(failed_post_ids) - 20}"
            notify_publish_status(
                notify_chat_id,
                f"Отправка завершена частично. Отправлено: {sent_count}. "
                f"Не отправлено: {len(failed_post_ids)} ({failed_preview}{extra}). "
                "Нажмите отправку ещё раз позже, чтобы дослать только оставшиеся.",
            )
        else:
            notify_publish_status(
                notify_chat_id,
                f"✅ Все новые посты ({sent_count}) успешно отправлены в канал.",
            )
        logger.info(
            "Channel post publish completed: sent=%s failed=%s source=%s",
            sent_count,
            len(failed_post_ids),
            source,
        )
        return sent_count
    except Exception as exc:
        logger.exception("Channel post publish failed, source=%s: %s", source, exc)
        notify_publish_status(notify_chat_id, "Ошибка при отправке постов. Подробности записаны в лог.")
        return sent_count
    finally:
        channel_post_publish_lock.release()


def send_phoenix_channel_footer():
    try:
        with open(PHOENIX_CHANNEL_FOOTER_IMAGE_PATH, "rb") as photo:
            send_photo_or_text(
                bot,
                CHANNEL_ID,
                photo,
                PHOENIX_CHANNEL_FOOTER_TEXT,
            )
        logger.info("Phoenix footer sent to channel after post publish")
        return True
    except Exception as exc:
        logger.warning("Phoenix footer send failed after post publish: %s", exc)
        return False


def should_auto_publish_channel_posts(value):
    return (
        value.weekday() != 6
        and value.hour in AUTO_CHANNEL_POST_HOURS
        and value.minute == 0
    )


def channel_post_auto_publish_loop():
    last_slot = None
    while True:
        try:
            current = now_samara()
            slot = (current.date(), current.hour)
            if should_auto_publish_channel_posts(current) and slot != last_slot:
                last_slot = slot
                logger.info("Auto channel post publish started for Samara slot %s:00", current.hour)
                publish_unsent_posts_to_channel(source="auto")
        except Exception:
            logger.exception("Auto channel post publish loop failed")

        if channel_post_auto_publish_stop_event.wait(AUTO_CHANNEL_POST_CHECK_SECONDS):
            return


def start_channel_post_auto_publish_worker():
    global channel_post_auto_publish_started
    if channel_post_auto_publish_started:
        return

    channel_post_auto_publish_started = True
    thread = threading.Thread(
        target=channel_post_auto_publish_loop,
        name="channel-post-auto-publish",
        daemon=True,
    )
    thread.start()


# Отправка в канал
@bot.message_handler(func=lambda message: message.text == "📢 Отправить посты в канал")
def send_new_posts_to_channel(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    if role not in ["admin", "supreme_leader"]:
        safe_send_message(bot, user_id, "У вас нет прав доступа к этой функции.", logger=logger)
        return

    publish_unsent_posts_to_channel(notify_chat_id=user_id, source="manual")

# Статистика
@bot.message_handler(commands=['statistic'])
def handle_statistic(message):
    today = now_local()
    monday = today - timedelta(days=today.weekday())
    last_monday = monday - timedelta(days=7)
    last_sunday = monday - timedelta(days=1)

    days_range = {
        'today': (today.date(), today.date()),
        'week': (monday.date(), today.date()),
        'last_week': (last_monday.date(), last_sunday.date())
    }

    post_statistics = {"today": {}, "week": {}, "last_week": {}}
    revision_statistics = {"today": {}, "week": {}, "last_week": {}}
    delivery_collection_statistics = {"today": {}, "week": {}, "last_week": {}}
    delivery_collection_item_statistics = {"today": {}, "week": {}, "last_week": {}}
    total_posts = {"today": 0, "week": 0, "last_week": 0}
    total_revisions = {"today": 0, "week": 0, "last_week": 0}
    total_delivery_collections = {"today": 0, "week": 0, "last_week": 0}
    total_delivery_collection_items = {"today": 0, "week": 0, "last_week": 0}

    first_period_date = min(start for start, _end in days_range.values())
    last_period_date = max(end for _start, end in days_range.values())
    query_start = datetime.combine(first_period_date, datetime.min.time())
    query_end = datetime.combine(last_period_date + timedelta(days=1), datetime.min.time())

    # Получение данных из базы данных
    all_clients = Clients.get_row_all()  # Получаем всех клиентов
    with Session(bind=engine) as session:
        all_posts = session.query(Posts).filter(
            Posts.created_at >= query_start,
            Posts.created_at < query_end,
        ).all()
        revision_logs = session.query(RevisionLog).filter(
            RevisionLog.created_at >= query_start,
            RevisionLog.created_at < query_end,
        ).all()
        delivery_collections = session.query(
            DeliveryCollectionItem.delivery_id,
            DeliveryCollectionItem.collector_user_id,
            DeliveryCollectionItem.collector_name,
            func.max(DeliveryCollectionItem.placed_at).label("completed_at"),
        ).filter(
            DeliveryCollectionItem.placed_at != None,
            DeliveryCollectionItem.placed_at < query_end,
            DeliveryCollectionItem.collector_user_id != None,
        ).group_by(
            DeliveryCollectionItem.delivery_id,
            DeliveryCollectionItem.collector_user_id,
            DeliveryCollectionItem.collector_name,
        ).having(
            func.max(DeliveryCollectionItem.placed_at) >= query_start,
        ).all()
        delivery_collection_items = session.query(
            DeliveryCollectionItem.collector_user_id,
            DeliveryCollectionItem.collector_name,
            DeliveryCollectionItem.placed_at,
            DeliveryCollectionItem.quantity,
        ).filter(
            DeliveryCollectionItem.placed_at >= query_start,
            DeliveryCollectionItem.placed_at < query_end,
            DeliveryCollectionItem.collector_user_id != None,
            DeliveryCollectionItem.reservation_id != None,
        ).all()

    # Преобразование клиентов в словарь {user_id: name}
    clients_dict = {}
    if not all_clients:
        clients_dict = {}
    elif isinstance(all_clients, dict):
        clients_dict = {key: value.get("name", "Неизвестный пользователь") for key, value in all_clients.items()}
    elif isinstance(all_clients, list):
        if all(isinstance(client, dict) for client in all_clients):
            clients_dict = {client["user_id"]: client.get("name", "Неизвестный пользователь") for client in all_clients}
        else:
            clients_dict = {client.user_id: client.name for client in all_clients}
    else:
        raise TypeError(f"Unsupported data type for 'all_clients': {type(all_clients)}")

    revision_post_ids_by_period = {"today": set(), "week": set(), "last_week": set()}

    # Генерация статистики ревизии и списка товаров, которые нужно исключить из созданных постов
    for key, date_range in days_range.items():
        for revision_log in revision_logs:
            if not revision_log.created_at:
                continue
            revision_date = revision_log.created_at.date()
            if date_range[0] <= revision_date <= date_range[1]:
                revision_post_ids_by_period[key].add(revision_log.post_id)
                auditor_name = clients_dict.get(revision_log.auditor_user_id, "Неизвестный пользователь")
                if auditor_name not in revision_statistics[key]:
                    revision_statistics[key][auditor_name] = 0
                revision_statistics[key][auditor_name] += 1
                total_revisions[key] += 1

        for _delivery_id, collector_user_id, collector_name, completed_at in delivery_collections:
            if not collector_user_id or not completed_at:
                continue
            collection_date = completed_at.date()
            if date_range[0] <= collection_date <= date_range[1]:
                collector_display_name = collector_name or clients_dict.get(collector_user_id, "Неизвестный пользователь")
                if collector_display_name not in delivery_collection_statistics[key]:
                    delivery_collection_statistics[key][collector_display_name] = 0
                delivery_collection_statistics[key][collector_display_name] += 1
                total_delivery_collections[key] += 1

        for collector_user_id, collector_name, placed_at, quantity in delivery_collection_items:
            if not collector_user_id or not placed_at:
                continue
            collection_date = placed_at.date()
            if date_range[0] <= collection_date <= date_range[1]:
                collector_display_name = collector_name or clients_dict.get(collector_user_id, "Неизвестный пользователь")
                item_count = max(int(quantity or 0), 1)
                if collector_display_name not in delivery_collection_item_statistics[key]:
                    delivery_collection_item_statistics[key][collector_display_name] = 0
                delivery_collection_item_statistics[key][collector_display_name] += item_count
                total_delivery_collection_items[key] += item_count

        for post in all_posts:
            if not post.created_at:
                continue
            if post.id in revision_post_ids_by_period[key]:
                continue

            created_at_date = post.created_at.date()
            created_at_time = post.created_at.time()

            # Исключаем посты с временем "00:00:00"
            if created_at_time == datetime.min.time():
                continue

            if date_range[0] <= created_at_date <= date_range[1]:
                creator_name = clients_dict.get(post.chat_id, "Неизвестный пользователь")
                if creator_name not in post_statistics[key]:
                    post_statistics[key][creator_name] = 0
                post_statistics[key][creator_name] += 1
                total_posts[key] += 1

    # Формирование текста ответа
    response = "📊 Статистика:\n"
    response += "\nСозданные посты:\n"
    for period, names_data in post_statistics.items():
        if period == "today":
            period_label = "Сегодня"
        elif period == "week":
            period_label = "На этой неделе"
        elif period == "last_week":
            period_label = "На прошлой неделе"
        else:
            period_label = "Неизвестный период"

        response += f"\n{period_label}:\n"
        if not names_data:
            response += "  - нет данных\n"
        for name, count in sorted(names_data.items()):
            response += f"  - {name}: {count} товаров\n"

    response += f"\nОбщее количество созданных товаров:\n"
    response += f"  - Сегодня: {total_posts['today']} товаров\n"
    response += f"  - На этой неделе: {total_posts['week']} товаров\n"
    response += f"  - На прошлой неделе: {total_posts['last_week']} товаров\n"

    response += "\nСделанная ревизия:\n"
    for period, names_data in revision_statistics.items():
        if period == "today":
            period_label = "Сегодня"
        elif period == "week":
            period_label = "На этой неделе"
        elif period == "last_week":
            period_label = "На прошлой неделе"
        else:
            period_label = "Неизвестный период"

        response += f"\n{period_label}:\n"
        if not names_data:
            response += "  - нет данных\n"
        for name, count in sorted(names_data.items()):
            response += f"  - {name}: {count} товаров\n"

    response += f"\nОбщее количество ревизии:\n"
    response += f"  - Сегодня: {total_revisions['today']} товаров\n"
    response += f"  - На этой неделе: {total_revisions['week']} товаров\n"
    response += f"  - На прошлой неделе: {total_revisions['last_week']} товаров\n"

    response += "\nСобранные доставки:\n"
    for period, names_data in delivery_collection_statistics.items():
        if period == "today":
            period_label = "Сегодня"
        elif period == "week":
            period_label = "На этой неделе"
        elif period == "last_week":
            period_label = "На прошлой неделе"
        else:
            period_label = "Неизвестный период"

        response += f"\n{period_label}:\n"
        if not names_data:
            response += "  - нет данных\n"
        for name, count in sorted(names_data.items()):
            response += f"  - {name}: {count} доставок\n"

    response += f"\nОбщее количество собранных доставок:\n"
    response += f"  - Сегодня: {total_delivery_collections['today']} доставок\n"
    response += f"  - На этой неделе: {total_delivery_collections['week']} доставок\n"
    response += f"  - На прошлой неделе: {total_delivery_collections['last_week']} доставок\n"

    response += "\nТоваров собрано в доставках:\n"
    for period, names_data in delivery_collection_item_statistics.items():
        if period == "today":
            period_label = "Сегодня"
        elif period == "week":
            period_label = "На этой неделе"
        elif period == "last_week":
            period_label = "На прошлой неделе"
        else:
            period_label = "Неизвестный период"

        response += f"\n{period_label}:\n"
        if not names_data:
            response += "  - нет данных\n"
        for name, count in sorted(names_data.items()):
            response += f"  - {name}: {count} товаров\n"

    response += f"\nОбщее количество товаров в доставках:\n"
    response += f"  - Сегодня: {total_delivery_collection_items['today']} товаров\n"
    response += f"  - На этой неделе: {total_delivery_collection_items['week']} товаров\n"
    response += f"  - На прошлой неделе: {total_delivery_collection_items['last_week']} товаров\n"

    try:
        sync_payroll_sheet_week(post_statistics["week"], delivery_collection_item_statistics["week"])
    except Exception:
        logger.exception("Payroll sheet sync from /statistic failed")

    safe_send_message(bot, message.chat.id, response, logger=logger)


def get_payroll_sheet_sync_state():
    state = user_data.get(PAYROLL_SHEET_SYNC_STATE_KEY, {})
    return state if isinstance(state, dict) else {}


def save_payroll_sheet_sync_state(state):
    user_data[PAYROLL_SHEET_SYNC_STATE_KEY] = state


def payroll_sheet_sync_week_key(value):
    value = value.astimezone(SAMARA_TZ) if value.tzinfo else value.replace(tzinfo=SAMARA_TZ)
    monday = value - timedelta(days=value.weekday())
    return monday.date().isoformat()


def should_sync_payroll_sheet(value):
    value = value.astimezone(SAMARA_TZ) if value.tzinfo else value.replace(tzinfo=SAMARA_TZ)
    if value.weekday() != PAYROLL_SHEET_SYNC_WEEKDAY:
        return False

    sync_at = value.replace(
        hour=PAYROLL_SHEET_SYNC_HOUR,
        minute=PAYROLL_SHEET_SYNC_MINUTE,
        second=0,
        microsecond=0,
    )
    return value >= sync_at


def calculate_payroll_week_counts(current=None):
    current = current or now_local()
    if current.tzinfo:
        current = current.astimezone(SAMARA_TZ).replace(tzinfo=None)

    monday = current - timedelta(days=current.weekday())
    week_start = datetime.combine(monday.date(), datetime.min.time())
    week_end = datetime.combine(current.date() + timedelta(days=1), datetime.min.time())

    with Session(bind=engine) as session:
        clients_dict = {
            client.user_id: client.name or "Неизвестный пользователь"
            for client in session.query(Clients).all()
        }
        revision_post_ids = {
            row[0]
            for row in session.query(RevisionLog.post_id).filter(
                RevisionLog.created_at >= week_start,
                RevisionLog.created_at < week_end,
            ).all()
        }

        post_counts = {}
        posts = session.query(Posts.id, Posts.chat_id, Posts.created_at).filter(
            Posts.created_at >= week_start,
            Posts.created_at < week_end,
        ).all()
        for post_id, chat_id, created_at in posts:
            if not created_at:
                continue
            if post_id in revision_post_ids:
                continue
            if created_at.time() == datetime.min.time():
                continue

            creator_name = clients_dict.get(chat_id, "Неизвестный пользователь")
            post_counts[creator_name] = post_counts.get(creator_name, 0) + 1

        delivery_item_counts = {}
        delivery_items = session.query(
            DeliveryCollectionItem.collector_user_id,
            DeliveryCollectionItem.collector_name,
            DeliveryCollectionItem.quantity,
        ).filter(
            DeliveryCollectionItem.placed_at >= week_start,
            DeliveryCollectionItem.placed_at < week_end,
            DeliveryCollectionItem.collector_user_id != None,
            DeliveryCollectionItem.reservation_id != None,
        ).all()

        for collector_user_id, collector_name, quantity in delivery_items:
            if not collector_user_id:
                continue
            item_count = max(int(quantity or 0), 1)
            collector_display_name = collector_name or clients_dict.get(collector_user_id, "Неизвестный пользователь")
            delivery_item_counts[collector_display_name] = delivery_item_counts.get(collector_display_name, 0) + item_count

    return post_counts, delivery_item_counts


def run_payroll_sheet_weekly_sync(current=None, force=False):
    current = current or now_samara()
    if not force and not should_sync_payroll_sheet(current):
        return False

    slot_key = payroll_sheet_sync_week_key(current)
    state = get_payroll_sheet_sync_state()
    if not force and state.get("last_weekly_sync_key") == slot_key:
        return False

    if not payroll_sheet_sync_lock.acquire(blocking=False):
        return False

    try:
        post_counts, delivery_counts = calculate_payroll_week_counts(current)
        result = sync_payroll_sheet_week(post_counts, delivery_counts)
        state["last_weekly_sync_key"] = slot_key
        state["last_weekly_sync_at"] = serialize_datetime(now_local())
        state["last_weekly_sync_result"] = result
        save_payroll_sheet_sync_state(state)
        logger.info(
            "Payroll sheet weekly sync finished for week=%s worksheet=%s rows=%s",
            slot_key,
            result.get("worksheet"),
            result.get("rows"),
        )
        return True
    finally:
        payroll_sheet_sync_lock.release()


def payroll_sheet_sync_loop():
    while True:
        try:
            run_payroll_sheet_weekly_sync()
        except Exception:
            logger.exception("Payroll sheet weekly sync loop failed")

        if payroll_sheet_sync_stop_event.wait(PAYROLL_SHEET_SYNC_CHECK_SECONDS):
            return


def start_payroll_sheet_sync_worker():
    global payroll_sheet_sync_started
    if payroll_sheet_sync_started:
        return

    payroll_sheet_sync_started = True
    thread = threading.Thread(
        target=payroll_sheet_sync_loop,
        name="payroll-sheet-sync",
        daemon=True,
    )
    thread.start()

def close_expired_delivery_broadcast_campaigns(session, current=None):
    current = current or now_local()
    expired = session.query(DeliveryBroadcastCampaign).filter(
        DeliveryBroadcastCampaign.status == DELIVERY_BROADCAST_STATUS_ACTIVE,
        DeliveryBroadcastCampaign.cutoff_at <= current,
    ).all()
    for campaign in expired:
        campaign.status = DELIVERY_BROADCAST_STATUS_FINISHED
        campaign.finished_at = current
    return len(expired)


def get_or_create_delivery_broadcast_campaign(started_by_user_id, current=None):
    current = current or now_local()
    cutoff_at = delivery_campaign_cutoff_at(current)
    if current >= cutoff_at:
        with Session(bind=engine) as session:
            close_expired_delivery_broadcast_campaigns(session, current)
            session.commit()
        return None, False, "Рассылку доставки можно запускать только до 14:00 по Самаре."

    with Session(bind=engine) as session:
        campaign = session.query(DeliveryBroadcastCampaign).filter(
            DeliveryBroadcastCampaign.campaign_date == delivery_campaign_date(current)
        ).first()
        created = False
        if campaign:
            if campaign.status != DELIVERY_BROADCAST_STATUS_ACTIVE:
                campaign.status = DELIVERY_BROADCAST_STATUS_ACTIVE
                campaign.finished_at = None
            if not campaign.cutoff_at:
                campaign.cutoff_at = cutoff_at
        else:
            campaign = DeliveryBroadcastCampaign(
                campaign_date=delivery_campaign_date(current),
                started_at=current,
                cutoff_at=cutoff_at,
                status=DELIVERY_BROADCAST_STATUS_ACTIVE,
                started_by_user_id=started_by_user_id,
            )
            session.add(campaign)
            session.flush()
            created = True
        campaign_id = campaign.id
        session.commit()
        return campaign_id, created, None


def get_active_delivery_broadcast_campaign_id(current=None):
    current = current or now_local()
    with Session(bind=engine) as session:
        close_expired_delivery_broadcast_campaigns(session, current)
        campaign = session.query(DeliveryBroadcastCampaign).filter(
            DeliveryBroadcastCampaign.campaign_date == delivery_campaign_date(current),
            DeliveryBroadcastCampaign.status == DELIVERY_BROADCAST_STATUS_ACTIVE,
        ).first()
        campaign_id = campaign.id if campaign and current < campaign.cutoff_at else None
        session.commit()
        return campaign_id


def mark_stale_delivery_broadcast_recipients_unknown(session, campaign_id, current=None):
    current = current or now_local()
    stale_before = current - timedelta(minutes=DELIVERY_BROADCAST_STALE_SENDING_MINUTES)
    return session.query(DeliveryBroadcastRecipient).filter(
        DeliveryBroadcastRecipient.campaign_id == campaign_id,
        DeliveryBroadcastRecipient.status == DELIVERY_RECIPIENT_STATUS_SENDING,
        DeliveryBroadcastRecipient.attempted_at < stale_before,
    ).update(
        {
            "status": DELIVERY_RECIPIENT_STATUS_UNKNOWN,
            "error": "Бот перезапустился или не получил ответ Telegram после начала отправки.",
        },
        synchronize_session=False,
    )


def reserve_delivery_broadcast_recipient(campaign_id, user):
    current = now_local()
    with Session(bind=engine) as session:
        existing = session.query(DeliveryBroadcastRecipient).filter(
            DeliveryBroadcastRecipient.campaign_id == campaign_id,
            DeliveryBroadcastRecipient.phone == user["phone"],
        ).first()
        if existing:
            return None, existing.status

        recipient = DeliveryBroadcastRecipient(
            campaign_id=campaign_id,
            phone=user["phone"],
            user_id=user["user_id"],
            name=user.get("name"),
            total_sum=int(user.get("total_amount") or 0),
            status=DELIVERY_RECIPIENT_STATUS_SENDING,
            attempted_at=current,
        )
        session.add(recipient)
        try:
            session.commit()
            return recipient.id, DELIVERY_RECIPIENT_STATUS_SENDING
        except IntegrityError:
            session.rollback()
            return None, "reserved"


def finalize_delivery_broadcast_recipient(recipient_id, status, message_id=None, error=None):
    with Session(bind=engine) as session:
        recipient = session.query(DeliveryBroadcastRecipient).filter(
            DeliveryBroadcastRecipient.id == recipient_id
        ).first()
        if not recipient:
            return
        recipient.status = status
        recipient.telegram_message_id = message_id
        recipient.error = str(error)[:2000] if error else None
        if status == DELIVERY_RECIPIENT_STATUS_SENT:
            recipient.sent_at = now_local()
        session.commit()


def send_delivery_campaign_offer(campaign_id, user):
    recipient_id, _ = reserve_delivery_broadcast_recipient(campaign_id, user)
    if not recipient_id:
        return "skipped"

    try:
        sent_message = send_delivery_offer_message(user["user_id"], user["name"])
        finalize_delivery_broadcast_recipient(
            recipient_id,
            DELIVERY_RECIPIENT_STATUS_SENT,
            message_id=getattr(sent_message, "message_id", None),
        )
        return "sent"
    except Exception as exc:
        finalize_delivery_broadcast_recipient(
            recipient_id,
            DELIVERY_RECIPIENT_STATUS_FAILED,
            error=exc,
        )
        logger.warning("Delivery offer failed for user_id=%s: %s", user["user_id"], exc)
        return "failed"


def get_delivery_broadcast_status_counts(session, campaign_id):
    rows = session.query(
        DeliveryBroadcastRecipient.status,
        func.count(DeliveryBroadcastRecipient.id),
    ).filter(
        DeliveryBroadcastRecipient.campaign_id == campaign_id,
    ).group_by(DeliveryBroadcastRecipient.status).all()
    return {status: int(count) for status, count in rows}


def scan_delivery_broadcast_campaign(campaign_id):
    with delivery_broadcast_scan_lock:
        current = now_local()
        with Session(bind=engine) as session:
            campaign = session.query(DeliveryBroadcastCampaign).filter(
                DeliveryBroadcastCampaign.id == campaign_id
            ).first()
            if not campaign:
                return {"status": "missing", "campaign_id": campaign_id, "eligible_users": []}
            if campaign.status != DELIVERY_BROADCAST_STATUS_ACTIVE or current >= campaign.cutoff_at:
                campaign.status = DELIVERY_BROADCAST_STATUS_FINISHED
                campaign.finished_at = current
                session.commit()
                return {"status": "finished", "campaign_id": campaign_id, "eligible_users": []}
            mark_stale_delivery_broadcast_recipients_unknown(session, campaign.id, current)
            campaign.last_scan_at = current
            cutoff_at = campaign.cutoff_at
            session.commit()

        auto_fulfill_expired_reservations()
        eligible_users = calculate_for_delivery(cutoff_at=cutoff_at)
        sent_count = 0
        skipped_count = 0
        failed_count = 0
        stopped_by_cutoff = False
        for user in eligible_users:
            if now_local() >= cutoff_at:
                stopped_by_cutoff = True
                break
            result = send_delivery_campaign_offer(campaign_id, user)
            if result == "sent":
                sent_count += 1
                time.sleep(1)
            elif result == "failed":
                failed_count += 1
                time.sleep(1)
            else:
                skipped_count += 1

        with Session(bind=engine) as session:
            campaign = session.query(DeliveryBroadcastCampaign).filter(
                DeliveryBroadcastCampaign.id == campaign_id
            ).first()
            if campaign:
                campaign.last_scan_at = now_local()
                if stopped_by_cutoff:
                    campaign.status = DELIVERY_BROADCAST_STATUS_FINISHED
                    campaign.finished_at = campaign.last_scan_at
            status_counts = get_delivery_broadcast_status_counts(session, campaign_id)
            session.commit()

        return {
            "status": "finished" if stopped_by_cutoff else "active",
            "campaign_id": campaign_id,
            "eligible_users": eligible_users,
            "sent_count": sent_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "status_counts": status_counts,
        }


def build_delivery_broadcast_result_text(result):
    if result.get("status") == "finished":
        return "Кампания доставки уже завершена: сейчас 14:00 или позже по Самаре."
    if result.get("status") == "missing":
        return "Кампания доставки не найдена."

    status_counts = result.get("status_counts") or {}
    needs_check = (
        status_counts.get(DELIVERY_RECIPIENT_STATUS_SENDING, 0)
        + status_counts.get(DELIVERY_RECIPIENT_STATUS_UNKNOWN, 0)
        + status_counts.get(DELIVERY_RECIPIENT_STATUS_FAILED, 0)
    )
    return (
        "Рассылка доставки проверена.\n"
        f"Подходящих телефонов сейчас: {len(result.get('eligible_users') or [])}\n"
        f"Новых отправлено: {result.get('sent_count', 0)}\n"
        f"Уже были в кампании: {result.get('skipped_count', 0)}\n"
        f"Ошибок новых отправок: {result.get('failed_count', 0)}\n"
        f"Требуют проверки: {needs_check}"
    )


def delivery_broadcast_monitor_loop():
    while True:
        try:
            campaign_id = get_active_delivery_broadcast_campaign_id()
            if campaign_id:
                result = scan_delivery_broadcast_campaign(campaign_id)
                refresh_delivery_candidates_summary(campaign_id, result.get("eligible_users") or [], result)
                logger.info("Delivery broadcast monitor scan: %s", build_delivery_broadcast_result_text(result))
        except Exception:
            logger.exception("Delivery broadcast monitor failed")

        if delivery_broadcast_monitor_stop_event.wait(DELIVERY_BROADCAST_CHECK_SECONDS):
            return


def start_delivery_broadcast_monitor_worker():
    global delivery_broadcast_monitor_started
    if delivery_broadcast_monitor_started:
        return
    delivery_broadcast_monitor_started = True
    thread = threading.Thread(
        target=delivery_broadcast_monitor_loop,
        name="delivery-broadcast-monitor",
        daemon=True,
    )
    thread.start()


# Обработчик для кнопки 'Отправить рассылку'.
@bot.message_handler(func=lambda message: message.text == "📤 Отправить рассылку")
def send_broadcast(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    user_id = require_message_user_id(message, "Delivery broadcast command")
    if user_id is None:
        return
    safe_send_message(bot, user_id, "Запускаю кампанию доставки до 14:00 по Самаре...", logger=logger)
    try:
        campaign_id, created, error_text = get_or_create_delivery_broadcast_campaign(user_id)
        if error_text:
            safe_send_message(bot, user_id, error_text, logger=logger)
            return

        result = scan_delivery_broadcast_campaign(campaign_id)
        send_delivery_candidates_summary(campaign_id, result.get("eligible_users") or [], result)
        prefix = "Кампания создана." if created else "Кампания уже была активна, сделал внеочередной пересчет."
        safe_send_message(bot, user_id, f"{prefix}\n{build_delivery_broadcast_result_text(result)}", logger=logger)
    except Exception as e:
        logger.exception("Delivery broadcast failed")
        safe_send_message(bot, user_id, "Ошибка при выполнении рассылки. Подробности записаны в лог.", logger=logger)


def get_phoenix_broadcast_recipients():
    with Session(bind=engine) as session:
        client_ids = session.query(Clients.user_id).filter(Clients.user_id != None).all()
        session_ids = session.query(BotSession.user_id).filter(BotSession.user_id != None).all()

    recipients = {
        int(row[0])
        for row in [*client_ids, *session_ids]
        if row[0] and int(row[0]) > 0
    }
    return sorted(recipients)


def send_phoenix_message(user_id):
    if os.path.exists(PHOENIX_QR_PATH):
        try:
            with open(PHOENIX_QR_PATH, "rb") as photo:
                sent_message = safe_send_photo(
                    bot,
                    user_id,
                    photo,
                    caption=PHOENIX_BROADCAST_TEXT,
                    logger=logger,
                )
            if sent_message:
                return True
            logger.warning("Phoenix broadcast photo send failed for user_id=%s, falling back to text", user_id)
        except OSError as exc:
            logger.warning("Phoenix QR image unavailable path=%s: %s", PHOENIX_QR_PATH, exc)

    sent_message = safe_send_message(bot, user_id, PHOENIX_BROADCAST_TEXT, logger=logger)
    if not sent_message:
        raise RuntimeError("Phoenix broadcast text send failed")
    return True


def run_phoenix_broadcast(creator_user_id):
    sent_count = 0
    failed_count = 0
    recipients = []

    try:
        recipients = get_phoenix_broadcast_recipients()
        safe_send_message(
            bot,
            creator_user_id,
            f"Начинаю рассылку о Фениксе. Получателей: {len(recipients)}. "
            f"Задержка: {PHOENIX_BROADCAST_DELAY_SECONDS} сек.",
            logger=logger,
        )

        if not os.path.exists(PHOENIX_QR_PATH):
            safe_send_message(
                bot,
                creator_user_id,
                "QR-код не найден. Рассылка продолжится текстом без QR-кода.",
                logger=logger,
            )

        for index, recipient_id in enumerate(recipients, start=1):
            try:
                send_phoenix_message(recipient_id)
                sent_count += 1
            except Exception as exc:
                failed_count += 1
                logger.warning("Phoenix broadcast failed for user_id=%s: %s", recipient_id, exc)

            if index % PHOENIX_BROADCAST_BATCH_SIZE == 0 or index == len(recipients):
                safe_send_message(
                    bot,
                    creator_user_id,
                    f"Рассылка о Фениксе: обработано {index}/{len(recipients)}, "
                    f"отправлено {sent_count}, ошибок {failed_count}.",
                    logger=logger,
                )

            if index < len(recipients):
                time.sleep(PHOENIX_BROADCAST_DELAY_SECONDS)
                if PHOENIX_BROADCAST_BATCH_SIZE > 0 and index % PHOENIX_BROADCAST_BATCH_SIZE == 0:
                    time.sleep(PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS)

    except Exception as exc:
        logger.exception("Phoenix broadcast crashed: %s", exc)
        safe_send_message(
            bot,
            creator_user_id,
            "Рассылка о Фениксе остановлена из-за ошибки. Подробности записаны в лог.",
            logger=logger,
        )
    finally:
        phoenix_broadcast_lock.release()
        if recipients:
            safe_send_message(
                bot,
                creator_user_id,
                f"Рассылка о Фениксе завершена. Отправлено: {sent_count}, ошибок: {failed_count}.",
                logger=logger,
            )


@bot.message_handler(func=lambda message: message.text == PHOENIX_BROADCAST_BUTTON)
def start_phoenix_broadcast(message):
    if not require_creator(message):
        return

    if not phoenix_broadcast_lock.acquire(blocking=False):
        safe_send_message(bot, message.chat.id, "Рассылка о Фениксе уже выполняется.", logger=logger)
        return

    thread = threading.Thread(
        target=run_phoenix_broadcast,
        args=(message.chat.id,),
        name="phoenix-broadcast",
        daemon=True,
    )
    thread.start()


def is_legacy_delivery_callback(call):
    if call.data not in ["yes", "no"]:
        return False
    text = call.message.text or call.message.caption or ""
    return "Готовы принять доставку" in text


def delete_delivery_offer_buttons(chat_id, message_id):
    safe_delete_message(bot, chat_id, message_id, logger=logger)


# Обрабатывает ответ пользователя на предложение доставки с инлайн-клавиатуры.
@bot.callback_query_handler(
    func=lambda call: call.data in ["delivery_yes", "delivery_no"] or is_legacy_delivery_callback(call)
)
def handle_delivery_response_callback(call):
    # Получаем данные пользователя
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    message_id = call.message.message_id  # ID сообщения с кнопками
    response = call.data
    if response == "yes":
        response = "delivery_yes"
    elif response == "no":
        response = "delivery_no"

    safe_answer_callback_query(call.id)

    # Проверяем, не сформирован ли уже лист доставки по cutoff кампании.
    current = now_local()

    if response == "delivery_yes" and not is_delivery_campaign_before_cutoff(current):
        # Если нажато "Да" после cutoff — удаляем сообщение с кнопками
        safe_delete_message(bot, user_id, message_id, logger=logger)
        # Отправляем сообщение об отказе
        safe_send_message(
            bot,
            user_id,
            "Извините, но лист на доставку уже сформирован. Ожидайте следующую отправку.",
            logger=logger,
        )
    elif response == "delivery_yes":
        current_state = get_user_state(user_id)
        existing_temp = temp_user_data.get(user_id, {})
        if not isinstance(existing_temp, dict):
            existing_temp = {}

        active_delivery_states = {
            "WAITING_FOR_ADDRESS",
            "WAITING_FOR_CONFIRMATION",
            "WAITING_FOR_DATA_EDIT",
            "WAITING_FOR_NEW_ADDRESS",
            "WAITING_FOR_NEW_PHONE",
        }
        existing_cutoff = parse_datetime(existing_temp.get("delivery_cutoff_at"))
        if current_state in active_delivery_states and existing_cutoff:
            delete_delivery_offer_buttons(user_id, message_id)
            safe_send_message(
                bot,
                user_id,
                "Вы уже начали оформление доставки. Отправьте адрес или завершите текущее подтверждение.",
                logger=logger,
            )
            return
        with Session(bind=engine) as session:
            campaign_cutoff = get_delivery_cutoff_for_user(session, user_id)
            already_in_delivery = has_current_delivery_entry_for_user(
                session,
                user_id,
                campaign_cutoff or current,
            )
        if already_in_delivery:
            campaign_id = mark_delivery_broadcast_response(user_id, DELIVERY_RECIPIENT_RESPONSE_ACCEPTED)
            refresh_delivery_candidates_summary(campaign_id)
            delete_delivery_offer_buttons(user_id, message_id)
            safe_send_message(
                bot,
                user_id,
                "Вы уже согласились на доставку. Повторное подтверждение не требуется.",
                logger=logger,
            )
            return
        delivery_cutoff_at = (
            existing_cutoff
            if current_state in active_delivery_states and existing_cutoff
            else (campaign_cutoff or now_local())
        )
        # Если согласие до 16:00, запрашиваем адрес
        address_prompt = safe_send_message(
            bot,
            user_id,
            "Пожалуйста, укажите город, адрес и подъезд",
            logger=logger,
        )
        if not address_prompt:
            logger.warning("Delivery address prompt was not sent for user_id=%s", user_id)
            return
        safe_delete_message(bot, user_id, message_id, logger=logger)
        temp_user_data[user_id] = {
            **existing_temp,
            "delivery_cutoff_at": serialize_datetime(delivery_cutoff_at),
        }
        # Сохраняем состояние пользователя для дальнейшего ввода адреса
        set_user_state(user_id, "WAITING_FOR_ADDRESS")
    elif response == "delivery_no":
        campaign_id = mark_delivery_broadcast_response(user_id, DELIVERY_RECIPIENT_RESPONSE_DECLINED)
        refresh_delivery_candidates_summary(campaign_id)
        # Если отказ, удаляем сообщение с кнопками и уведомляем об ожидании следующей доставки
        safe_delete_message(bot, user_id, message_id, logger=logger)
        safe_send_message(bot, user_id, "Вы отказались от доставки. Оповестим вас при следующей доставке.", logger=logger)

# Обрабатывает ввод адреса пользователя.
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "WAITING_FOR_ADDRESS")
def handle_address_input(message):
    user_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return

    address = require_text_message(message, "Введите адрес доставки текстом.")
    if address is None:
        return
    logger.info("Delivery address received for user_id=%s", user_id)
    # Проверяем наличие данных о пользователе
    user_data = Clients.get_row_by_user_id(user_id)
    if not user_data:
        logger.warning("Delivery address received for unknown user_id=%s", user_id)
        safe_send_message(bot, user_id, "Ошибка! Данные пользователя отсутствуют.", logger=logger)
        return
    name = user_data.name
    phone = user_data.phone
    logger.debug("Delivery user data loaded for user_id=%s", user_id)
    existing_temp = temp_user_data.get(user_id, {})
    if not isinstance(existing_temp, dict):
        existing_temp = {}
    delivery_cutoff_at = parse_datetime(existing_temp.get("delivery_cutoff_at")) or now_local()
    # Вычисление суммы заказов пользователя
    user_orders_sum = calculate_collectable_sum_for_user(user_id, cutoff_at=delivery_cutoff_at)
    logger.debug("Delivery fulfilled order sum loaded for user_id=%s", user_id)
    # Поиск всех пользователей с таким же телефоном
    from db import Session, engine
    with Session(bind=engine) as session:
        same_phone_users = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(phone))
        ).all()
    if not same_phone_users:
        logger.warning("No related delivery users found for user_id=%s", user_id)
        safe_send_message(
            bot,
            user_id,
            "Ошибка! Не удалось найти других заказов с данным номером телефона.",
            logger=logger,
        )
        return
    # Подсчет общей суммы всех заказов
    total_sum_by_phone = user_orders_sum
    all_user_orders_details = []
    seen_delivery_user_ids = {user_id}
    for client in same_phone_users:
        if client.user_id in seen_delivery_user_ids:
            continue
        seen_delivery_user_ids.add(client.user_id)
        client_sum = calculate_collectable_sum_for_user(client.user_id, cutoff_at=delivery_cutoff_at)
        all_user_orders_details.append({
            "name": client.name,
            "orders_sum": client_sum
        })
        total_sum_by_phone += client_sum
    logger.debug("Delivery total sum loaded for user_id=%s", user_id)
    # Генерация текста для подтверждения
    orders_details_text = f"Ваши заказы: {user_orders_sum}\n"
    for detail in all_user_orders_details:
        if detail["name"] != name:
            orders_details_text += f"{detail['name']}: {detail['orders_sum']}\n"
    orders_details_text += f"Общая сумма: {total_sum_by_phone}"
    # Отправляем подтверждающее сообщение
    confirmation_message = safe_send_message(
        bot,
        user_id,
        f"Ваши данные:\nИмя: {name}\nТелефон: {phone}\nАдрес: {address}\n\n{orders_details_text}\n\nПодтверждаете?",
        reply_markup=keyboard_for_confirmation(),
        logger=logger,
    )
    if not confirmation_message:
        logger.warning("Delivery confirmation prompt was not sent for user_id=%s", user_id)
        return
    # Сохраняем данные во временном хранилище
    temp_user_data[user_id] = {
        **existing_temp,
        "name": name,
        "phone": phone,
        "final_sum": user_orders_sum,
        "total_sum_by_phone": total_sum_by_phone,
        "address": address,
        "delivery_cutoff_at": serialize_datetime(delivery_cutoff_at),
    }
    logger.info("Temporary delivery data saved for user_id=%s", user_id)
    # Устанавливаем состояние
    set_user_state(user_id, "WAITING_FOR_CONFIRMATION")

@bot.message_handler(commands=["empty_delivery"])
def handle_empty_delivery_command(message):
    user_id = message.chat.id
    logger.info("Delivery temp cleanup requested for user_id=%s", user_id)

    # Проверяем наличие данных
    if user_id in temp_user_data:
        del temp_user_data[user_id]
        logger.info("Temporary delivery data deleted for user_id=%s", user_id)
        safe_send_message(bot, user_id, "Ваши данные на доставку были удалены.", logger=logger)
    else:
        logger.warning("Temporary delivery data not found for user_id=%s", user_id)
        safe_send_message(bot, user_id, "Нет данных для удаления.", logger=logger)

# Рассчитывает общую сумму заказов для указанного пользователя.
def calculate_sum_for_user(user_id, cutoff_at=None):
    with Session(bind=engine) as session:
        query = session.query(
            func.sum(
                (func.coalesce(Reservations.old_price, 0) * Reservations.quantity)
                - func.coalesce(Reservations.return_order, 0)
            ).label("final_sum")
        ).select_from(
            Reservations
        ).filter(
            Reservations.user_id == user_id, Reservations.is_fulfilled == True
        )
        if cutoff_at:
            query = query.filter(
                or_(Reservations.fulfilled_at == None, Reservations.fulfilled_at <= cutoff_at),
            )
        result = query.first()

        return result.final_sum if result.final_sum else 0


def calculate_collectable_sum_for_user(user_id, cutoff_at=None):
    cutoff_at = cutoff_at or now_local()
    with Session(bind=engine) as session:
        return calculate_delivery_total_for_user_ids(session, [user_id], cutoff_at)


def delivery_target_label(now=None):
    now = now or now_local()
    return "Доставка на понедельник" if now.weekday() == 5 else "Доставка на завтра"


def get_delivery_broadcast_response_maps(session, campaign_id):
    if not campaign_id:
        return {}, {}

    rows = session.query(
        DeliveryBroadcastRecipient.phone,
        DeliveryBroadcastRecipient.user_id,
        DeliveryBroadcastRecipient.response_status,
    ).filter(
        DeliveryBroadcastRecipient.campaign_id == campaign_id,
        DeliveryBroadcastRecipient.response_status != None,
    ).all()

    by_phone = {}
    by_user_id = {}
    for phone, user_id, response_status in rows:
        if phone:
            by_phone[normalize_phone(phone)] = response_status
        if user_id:
            by_user_id[int(user_id)] = response_status
    return by_phone, by_user_id


def get_delivery_broadcast_response_label(user, responses_by_phone, responses_by_user_id):
    response_status = responses_by_user_id.get(int(user.get("user_id") or 0))
    if not response_status:
        response_status = responses_by_phone.get(normalize_phone(user.get("phone")))
    label = DELIVERY_RECIPIENT_RESPONSE_LABELS.get(response_status)
    return f" ({label})" if label else ""


def build_delivery_candidates_summary_text(eligible_users, scan_result=None, campaign_id=None):
    if not eligible_users:
        return "📤 Рассылка доставки: подходящих клиентов с суммой 1500+ не найдено."

    scan_result = scan_result or {}
    status_counts = scan_result.get("status_counts") or {}
    needs_check = (
        status_counts.get(DELIVERY_RECIPIENT_STATUS_SENDING, 0)
        + status_counts.get(DELIVERY_RECIPIENT_STATUS_UNKNOWN, 0)
        + status_counts.get(DELIVERY_RECIPIENT_STATUS_FAILED, 0)
    )
    lines = [
        "📤 Клиенты для рассылки доставки",
        f"Всего клиентов: {len(eligible_users)}",
        f"Новых отправлено сейчас: {scan_result.get('sent_count', 0)}",
        f"Уже были в кампании: {scan_result.get('skipped_count', 0)}",
        f"Требуют проверки: {needs_check}",
        "",
    ]

    responses_by_phone = {}
    responses_by_user_id = {}
    if campaign_id:
        with Session(bind=engine) as session:
            responses_by_phone, responses_by_user_id = get_delivery_broadcast_response_maps(session, campaign_id)

    for index, user in enumerate(eligible_users, start=1):
        response_label = get_delivery_broadcast_response_label(user, responses_by_phone, responses_by_user_id)
        lines.append(
            f"{index}. {user.get('name') or 'Имя не указано'} | "
            f"{user.get('phone') or 'Телефон не указан'} | "
            f"{user.get('total_amount', 0)} ₽{response_label}"
        )
    return "\n".join(lines)


def save_delivery_campaign_summary_message(campaign_id, sent_message):
    if not campaign_id or not sent_message:
        return

    with Session(bind=engine) as session:
        campaign = session.query(DeliveryBroadcastCampaign).filter(
            DeliveryBroadcastCampaign.id == campaign_id
        ).first()
        if not campaign:
            return
        campaign.summary_chat_id = getattr(getattr(sent_message, "chat", None), "id", None) or delivery_channel
        campaign.summary_message_id = getattr(sent_message, "message_id", None)
        session.commit()


def send_delivery_candidates_summary(campaign_id, eligible_users, scan_result=None):
    text = build_delivery_candidates_summary_text(eligible_users, scan_result, campaign_id=campaign_id)

    try:
        sent_message = safe_send_message(bot, delivery_channel, text, logger=logger)
        save_delivery_campaign_summary_message(campaign_id, sent_message)
        return sent_message
    except Exception as e:
        logger.warning("Delivery candidates summary send failed: %s", e)
        return None


def refresh_delivery_candidates_summary(campaign_id, eligible_users=None, scan_result=None):
    if not campaign_id:
        return False

    with Session(bind=engine) as session:
        campaign = session.query(DeliveryBroadcastCampaign).filter(
            DeliveryBroadcastCampaign.id == campaign_id
        ).first()
        if not campaign or not campaign.summary_chat_id or not campaign.summary_message_id:
            return False
        summary_chat_id = campaign.summary_chat_id
        summary_message_id = campaign.summary_message_id
        cutoff_at = campaign.cutoff_at

    if eligible_users is None:
        eligible_users = calculate_for_delivery(cutoff_at=cutoff_at)

    text = build_delivery_candidates_summary_text(eligible_users, scan_result, campaign_id=campaign_id)
    result = safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=summary_chat_id,
        message_id=summary_message_id,
        text=text,
    )
    return result is not None


def mark_delivery_broadcast_response(user_id, response_status):
    current = now_local()
    with Session(bind=engine) as session:
        campaign = session.query(DeliveryBroadcastCampaign).filter(
            DeliveryBroadcastCampaign.campaign_date == delivery_campaign_date(current),
        ).order_by(DeliveryBroadcastCampaign.id.desc()).first()
        if not campaign:
            return None

        client = session.query(Clients).filter(Clients.user_id == user_id).first()
        conditions = [DeliveryBroadcastRecipient.user_id == user_id]
        if client and client.phone:
            conditions.append(DeliveryBroadcastRecipient.phone == normalize_phone(client.phone))

        recipient = session.query(DeliveryBroadcastRecipient).filter(
            DeliveryBroadcastRecipient.campaign_id == campaign.id,
            or_(*conditions),
        ).order_by(DeliveryBroadcastRecipient.id.desc()).first()
        if not recipient:
            return None

        if recipient.response_status == response_status:
            return campaign.id

        recipient.response_status = response_status
        recipient.responded_at = current
        session.commit()
        return campaign.id


def get_delivery_cutoff_for_user(session, user_id):
    client = session.query(Clients).filter(Clients.user_id == user_id).first()
    if not client:
        return None
    phone = normalize_phone(client.phone)
    if not phone:
        return None

    row = session.query(DeliveryBroadcastCampaign.cutoff_at).join(
        DeliveryBroadcastRecipient,
        DeliveryBroadcastRecipient.campaign_id == DeliveryBroadcastCampaign.id,
    ).filter(
        DeliveryBroadcastCampaign.campaign_date == delivery_campaign_date(),
        DeliveryBroadcastRecipient.phone == phone,
    ).order_by(DeliveryBroadcastCampaign.id.desc()).first()
    return row[0] if row else None


def same_delivery_cutoff_day(left, right):
    if not left or not right:
        return False
    return left.date() == right.date()


def should_replace_delivery_cutoff(existing_cutoff_at, new_cutoff_at):
    if not new_cutoff_at:
        return False
    if not existing_cutoff_at:
        return True
    if existing_cutoff_at.date() != new_cutoff_at.date():
        return new_cutoff_at.date() > existing_cutoff_at.date()
    return new_cutoff_at < existing_cutoff_at


def delivery_entry_has_collection_items(session, delivery_entry):
    if not delivery_entry or not delivery_entry.id:
        return False
    return session.query(DeliveryCollectionItem.id).filter(
        DeliveryCollectionItem.delivery_id == delivery_entry.id,
    ).first() is not None


def find_for_delivery_entry_for_user(session, user_id):
    client = session.query(Clients).filter(Clients.user_id == user_id).first()
    conditions = [ForDelivery.user_id == user_id]
    if client and client.phone:
        conditions.append(ForDelivery.phone.in_(phone_variants(client.phone)))
    return session.query(ForDelivery).filter(or_(*conditions)).order_by(ForDelivery.id).first()


def has_current_delivery_entry_for_user(session, user_id, cutoff_at):
    delivery_entry = find_for_delivery_entry_for_user(session, user_id)
    return bool(delivery_entry and same_delivery_cutoff_day(delivery_entry.delivery_cutoff_at, cutoff_at))


def upsert_for_delivery_entry(session, user_id, name, phone, address, total_sum, delivery_cutoff_at=None):
    deduplicate_for_delivery_entries(session)
    normalized_phone = normalize_phone(phone)
    delivery_cutoff_at = delivery_cutoff_at or now_local()
    delivery_entry = session.query(ForDelivery).filter(ForDelivery.phone == normalized_phone).first()

    if delivery_entry:
        if (
            should_replace_delivery_cutoff(delivery_entry.delivery_cutoff_at, delivery_cutoff_at)
            and not delivery_entry_has_collection_items(session, delivery_entry)
        ):
            delivery_entry.delivery_cutoff_at = delivery_cutoff_at
            delivery_entry.collector_user_id = None
            delivery_entry.collector_name = None
            delivery_entry.collection_started_at = None
        elif delivery_entry.delivery_cutoff_at is None:
            delivery_entry.delivery_cutoff_at = delivery_cutoff_at

        effective_cutoff_at = delivery_entry.delivery_cutoff_at or delivery_cutoff_at
        delivery_entry.user_id = user_id
        delivery_entry.name = name
        delivery_entry.phone = normalized_phone or phone
        if address:
            delivery_entry.address = address
        elif delivery_entry.address is None:
            delivery_entry.address = ""
        delivery_entry.total_sum = calculate_delivery_total_for_phone(
            session,
            normalized_phone or phone,
            user_id,
            effective_cutoff_at,
        )
        return delivery_entry

    total_sum = calculate_delivery_total_for_phone(
        session,
        normalized_phone or phone,
        user_id,
        delivery_cutoff_at,
    )
    delivery_entry = ForDelivery(
        user_id=user_id,
        name=name,
        phone=normalized_phone or phone,
        address=address or "",
        total_sum=total_sum,
        delivery_cutoff_at=delivery_cutoff_at,
    )
    session.add(delivery_entry)
    return delivery_entry


def deduplicate_for_delivery_entries(session):
    entries = session.query(ForDelivery).order_by(ForDelivery.id).all()
    entries_by_phone = {}
    for entry in entries:
        normalized_phone = normalize_phone(entry.phone)
        if not normalized_phone:
            continue
        entry.phone = normalized_phone

        existing = entries_by_phone.get(normalized_phone)
        if not existing:
            entries_by_phone[normalized_phone] = entry
            continue

        if entry.address and not existing.address:
            existing.address = entry.address
        if entry.total_sum and entry.total_sum > (existing.total_sum or 0):
            existing.total_sum = entry.total_sum
            existing.user_id = entry.user_id
            existing.name = entry.name
        if should_replace_delivery_cutoff(existing.delivery_cutoff_at, entry.delivery_cutoff_at):
            existing.delivery_cutoff_at = entry.delivery_cutoff_at
        if entry.collector_user_id and not existing.collector_user_id:
            existing.collector_user_id = entry.collector_user_id
            existing.collector_name = entry.collector_name
            existing.collection_started_at = entry.collection_started_at
        session.delete(entry)

@bot.message_handler(func=lambda message: message.text == "👨‍🦯 Засунуть в доставку")
def push_in_delivery(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    # Шаг 1. Запрос списка номеров у пользователя
    msg = safe_send_message(bot, message.chat.id, "Введите номера телефонов, каждый с новой строки:", logger=logger)
    if msg:
        bot.register_next_step_handler(msg, process_numbers)


def process_numbers(message):
    user_id = require_message_user_id(message, "Manual delivery phone list")
    if user_id is None:
        return
    if not has_role(user_id, DELIVERY_ROLES):
        safe_send_message(bot, message.chat.id, "У вас недостаточно прав для этой команды.", logger=logger)
        return
    try:
        # Шаг 2. Извлечение списка номеров телефонов
        if not message.text:
            safe_send_message(
                bot,
                message.chat.id,
                "Отправьте номера телефонов текстом, каждый с новой строки.",
                logger=logger,
            )
            return

        numbers = message.text.splitlines()
        phone_numbers = [normalize_phone(num) for num in numbers if is_phone_valid(num)]

        if not phone_numbers:
            safe_send_message(bot, message.chat.id, "Список номеров пуст. Попробуйте снова.", logger=logger)
            return

        # Шаг 3. Обработка номеров телефонов
        successful_deliveries = []
        delivery_cutoff_at = now_local()

        for phone in phone_numbers:
            with Session(bind=engine) as session:
                # Найти клиента по номеру телефона
                clients = session.query(Clients).filter(
                    Clients.phone.in_(phone_variants(phone))
                ).order_by(Clients.id).all()
                client = clients[0] if clients else None
                if not client:
                    safe_send_message(bot, message.chat.id, f"Клиент с номером {phone} не найден.", logger=logger)
                    continue

                # Найти выполненные заказы клиента
                related_user_ids = [client_row.user_id for client_row in clients]
                reservations = get_delivery_reservations_query(
                    session,
                    related_user_ids,
                    delivery_cutoff_at,
                ).all()

                if not reservations:
                    safe_send_message(
                        bot,
                        message.chat.id,
                        f"У клиента {phone} нет обработанных заказов в текущем срезе доставки.",
                        logger=logger,
                    )
                    continue

                # Рассчитать `total_sum` как сумму (quantity * price) для каждого заказа
                total_sum = 0
                for reservation in reservations:
                    post = get_post_or_snapshot(session, reservation.post_id)
                    total_sum += calculate_order_amount(reservation, post)

                # Добавление данных в таблицу ForDelivery
                if total_sum > 0:
                    try:
                        upsert_for_delivery_entry(
                            session,
                            client.user_id,
                            client.name,
                            phone,
                            "",
                            total_sum,
                            delivery_cutoff_at=delivery_cutoff_at,
                        )
                        session.commit()
                        successful_deliveries.append(phone)
                    except Exception as e:
                        session.rollback()
                        logger.exception("Delivery add failed for phone=%s: %s", phone, e)
                        safe_send_message(
                            bot,
                            message.chat.id,
                            f"Ошибка при добавлении данных клиента {phone}. Подробности записаны в лог.",
                            logger=logger,
                        )
                else:
                    safe_send_message(
                        bot,
                        message.chat.id,
                        f"У клиента {phone} нет товаров для добавления в доставку.",
                        logger=logger,
                    )

        # Шаг 4. Уведомление о результатах
        if successful_deliveries:
            safe_send_message(
                bot,
                message.chat.id,
                f"Заказы для следующих номеров успешно добавлены в доставку: {', '.join(successful_deliveries)}",
                logger=logger,
            )
        else:
            safe_send_message(bot, message.chat.id, "Не удалось добавить заказы в доставку.", logger=logger)
    except Exception as e:
        logger.exception("Delivery add flow failed for chat_id=%s: %s", message.chat.id, e)
        safe_send_message(bot, message.chat.id, "Произошла ошибка. Подробности записаны в лог.", logger=logger)


@bot.message_handler(func=lambda message: message.text == "🗄 Архив доставки")
def archive_delivery_to_excel(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    """
    Формирует Excel-файл с архивом доставок из таблицы in_delivery,
    отправляет его в канал delivery_archive, и очищает таблицу.
    """
    # Получение всех данных из таблицы InDelivery
    delivery_rows = InDelivery.get_all_rows()

    # Проверка: если нет данных, завершить выполнение
    if not delivery_rows:
        safe_send_message(bot, message.chat.id, "Нет данных для архивации.", logger=logger)
        return None

    # Создание Excel файла в памяти
    wb = Workbook()
    ws = wb.active
    ws.title = "Архив доставок"

    # Добавление заголовков таблицы
    ws.append(["Телефон", "Имя", "Сумма", "Адрес доставки", "Че за товар"])

    # Получение данных и заполнение строк
    for row in delivery_rows:
        # Получение информации о клиенте по user_id из таблицы Clients
        client_data = Clients.get_row_by_user_id(row.user_id)

        # Заполнение строки для таблицы
        ws.append([
            client_data.phone if client_data else "Неизвестно",
            client_data.name if client_data else "Неизвестно",
            row.price,
            row.delivery_address,
            row.item_description
        ])

    # Сохранение файла в памяти
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)  # Перемещение курсора в начало файла

    # Указание имени файла через InputFile
    file_name = f"Архив_доставок_{now_local().strftime('%Y-%m-%d')}.xlsx"
    document = InputFile(output, file_name=file_name)

    # Отправка файла в канал delivery_archive
    sent_archive = safe_send_document(bot, delivery_archive, document, logger=logger)
    if not sent_archive:
        safe_send_message(
            bot,
            message.chat.id,
            "Не удалось отправить архив доставки. Записи in_delivery не очищены.",
            logger=logger,
        )
        return None

    # Уведомление пользователя об отправке
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("🧹 Очистить in_delivery", callback_data="archive_delivery_clear_yes")
    )
    keyboard.add(
        InlineKeyboardButton("Оставить записи", callback_data="archive_delivery_clear_no")
    )
    safe_send_message(
        bot,
        message.chat.id,
        "Архив доставок отправлен в канал. Очистить таблицу in_delivery?",
        reply_markup=keyboard,
        logger=logger,
    )


@bot.callback_query_handler(func=lambda call: call.data in ["archive_delivery_clear_yes", "archive_delivery_clear_no"])
def handle_archive_delivery_clear_confirmation(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    if call.data == "archive_delivery_clear_no":
        safe_answer_callback_query(call.id)
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Записи in_delivery оставлены без изменений.",
        )
        return

    safe_answer_callback_query(call.id)
    stats = cleanup_in_delivery_records(source="manual_archive")
    safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(
            "Записи доставки очищены после подтверждения.\n"
            f"Удалено in_delivery: {stats['in_delivery_deleted']}\n"
            f"Удалено temp_fulfilled: {stats['temp_fulfilled_deleted']}\n"
            f"Удалено свободных posts: {stats['posts_deleted']}"
        ),
    )


def ensure_deleted_post_snapshot(session, post, reason):
    if not post:
        return False

    existing = session.query(DeletedPostSnapshot.id).filter(
        DeletedPostSnapshot.post_id == post.id,
    ).first()
    if existing:
        return False

    session.add(DeletedPostSnapshot(
        post_id=post.id,
        chat_id=post.chat_id,
        photo=post.photo,
        price=post.price,
        description=post.description,
        message_id=post.message_id,
        quantity=post.quantity,
        is_sent=post.is_sent,
        created_at=post.created_at,
        deleted_at=now_local(),
        reason=reason,
    ))
    return True


def ensure_missing_delivery_item_snapshot(session, item, reason="missing_delivery_return_to_cart"):
    if not item:
        return False

    post_exists = session.query(Posts.id).filter(Posts.id == item.post_id).first()
    if post_exists:
        return False

    existing = session.query(DeletedPostSnapshot.id).filter(
        DeletedPostSnapshot.post_id == item.post_id,
    ).first()
    if existing:
        return False

    quantity = max(int(item.quantity or 0), 1)
    unit_price = max(int((item.price or 0) // quantity), 0)
    session.add(DeletedPostSnapshot(
        post_id=item.post_id,
        chat_id=CHANNEL_ID,
        photo=item.photo,
        price=unit_price,
        description=item.item_description,
        message_id=None,
        quantity=0,
        is_sent=False,
        created_at=item.missing_at or now_local(),
        deleted_at=now_local(),
        reason=reason,
    ))
    return True


def build_post_from_missing_delivery_item(item):
    quantity = max(int(item.quantity or 0), 1)
    unit_price = max(int((item.price or 0) // quantity), 0)
    return Posts(
        id=item.post_id,
        chat_id=CHANNEL_ID,
        photo=item.photo or "",
        price=unit_price,
        description=item.item_description or "",
        message_id=None,
        quantity=0,
        is_sent=False,
        created_at=item.missing_at or now_local(),
    )


def post_has_active_links(session, post_id):
    checks = [
        session.query(Reservations.id).filter(Reservations.post_id == post_id).first(),
        session.query(InDelivery.id).filter(InDelivery.post_id == post_id).first(),
        session.query(Temp_Fulfilled.id).filter(Temp_Fulfilled.post_id == post_id).first(),
        session.query(TempReservations.id).filter(TempReservations.post_id == post_id).first(),
        session.query(DeliveryCollectionItem.id).join(
            ForDelivery,
            ForDelivery.id == DeliveryCollectionItem.delivery_id,
        ).filter(
            DeliveryCollectionItem.post_id == post_id,
        ).first(),
        session.query(MissingDeliveryItem.id).filter(
            MissingDeliveryItem.post_id == post_id,
            MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING,
        ).first(),
    ]
    return any(checks)


def cleanup_in_delivery_records(source="scheduled"):
    stats = {
        "in_delivery_deleted": 0,
        "temp_fulfilled_deleted": 0,
        "posts_deleted": 0,
    }

    with Session(bind=engine) as session:
        delivery_rows = session.query(InDelivery).all()
        if not delivery_rows:
            return stats

        reservation_ids = {
            row.reservation_id for row in delivery_rows if row.reservation_id is not None
        }
        affected_post_ids = {row.post_id for row in delivery_rows if row.post_id is not None}

        temp_items_by_id = {}
        if reservation_ids:
            for temp_item in session.query(Temp_Fulfilled).filter(
                Temp_Fulfilled.reservation_id.in_(reservation_ids),
            ).all():
                temp_items_by_id[temp_item.id] = temp_item

        for row in delivery_rows:
            fallback_temp_items = session.query(Temp_Fulfilled).filter(
                Temp_Fulfilled.in_delivery == True,
                Temp_Fulfilled.user_id == row.user_id,
                Temp_Fulfilled.post_id == row.post_id,
            ).all()
            for temp_item in fallback_temp_items:
                temp_items_by_id[temp_item.id] = temp_item

        for temp_item in temp_items_by_id.values():
            session.delete(temp_item)
            stats["temp_fulfilled_deleted"] += 1

        for row in delivery_rows:
            session.delete(row)
            stats["in_delivery_deleted"] += 1

        session.flush()

        for post_id in affected_post_ids:
            post = session.query(Posts).filter(Posts.id == post_id).first()
            if not post:
                continue
            if post_has_active_links(session, post_id):
                continue
            ensure_deleted_post_snapshot(session, post, f"delivery_cleanup:{source}")
            if not delete_delivered_channel_post_message(post, source=source):
                continue
            session.delete(post)
            stats["posts_deleted"] += 1

        session.commit()

    return stats


def delivery_cleanup_slot_key(value):
    value = value.astimezone(SAMARA_TZ) if value.tzinfo else value.replace(tzinfo=SAMARA_TZ)
    return f"{value.date().isoformat()}T{DELIVERY_CLEANUP_HOUR:02d}:00+04:00"


def should_run_delivery_cleanup(value):
    value = value.astimezone(SAMARA_TZ) if value.tzinfo else value.replace(tzinfo=SAMARA_TZ)
    return (
        value.weekday() in DELIVERY_CLEANUP_WEEKDAYS
        and value.hour == DELIVERY_CLEANUP_HOUR
    )


def run_scheduled_delivery_cleanup(current=None):
    current = current or now_samara()
    if not should_run_delivery_cleanup(current):
        return None

    slot_key = delivery_cleanup_slot_key(current)
    with Session(bind=engine) as session:
        existing = session.query(DeliveryCleanupRun).filter(
            DeliveryCleanupRun.slot_key == slot_key,
        ).first()
        if existing:
            return None

        cleanup_run = DeliveryCleanupRun(
            slot_key=slot_key,
            started_at=now_local(),
            status="started",
        )
        session.add(cleanup_run)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return None

    try:
        stats = cleanup_in_delivery_records(source=f"scheduled:{slot_key}")
        with Session(bind=engine) as session:
            cleanup_run = session.query(DeliveryCleanupRun).filter(
                DeliveryCleanupRun.slot_key == slot_key,
            ).first()
            if cleanup_run:
                cleanup_run.finished_at = now_local()
                cleanup_run.in_delivery_deleted = stats["in_delivery_deleted"]
                cleanup_run.temp_fulfilled_deleted = stats["temp_fulfilled_deleted"]
                cleanup_run.posts_deleted = stats["posts_deleted"]
                cleanup_run.status = "completed"
                cleanup_run.details = "scheduled Samara 22:00 cleanup"
                session.commit()
        logger.info("Scheduled delivery cleanup completed for slot=%s stats=%s", slot_key, stats)
        return stats
    except Exception as exc:
        logger.exception("Scheduled delivery cleanup failed for slot=%s", slot_key)
        with Session(bind=engine) as session:
            cleanup_run = session.query(DeliveryCleanupRun).filter(
                DeliveryCleanupRun.slot_key == slot_key,
            ).first()
            if cleanup_run:
                cleanup_run.finished_at = now_local()
                cleanup_run.status = "failed"
                cleanup_run.details = str(exc)[:1000]
                session.commit()
        return None


def delivery_cleanup_loop():
    while True:
        try:
            run_scheduled_delivery_cleanup()
        except Exception:
            logger.exception("Delivery cleanup loop failed")

        if delivery_cleanup_stop_event.wait(DELIVERY_CLEANUP_CHECK_SECONDS):
            return


def start_delivery_cleanup_worker():
    global delivery_cleanup_started
    if delivery_cleanup_started:
        return

    delivery_cleanup_started = True
    thread = threading.Thread(
        target=delivery_cleanup_loop,
        name="delivery-cleanup",
        daemon=True,
    )
    thread.start()


def count_linked_midnight_posts(session, model, post_ids):
    if not post_ids:
        return 0
    return session.query(func.count(func.distinct(model.post_id))).filter(
        model.post_id.in_(post_ids),
    ).scalar() or 0


def cleanup_midnight_posts_after_snapshot(reason="legacy_midnight_created_at_cleanup"):
    stats = {
        "snapshotted": 0,
        "deleted": 0,
        "with_reservations": 0,
        "with_in_delivery": 0,
        "with_temp_fulfilled": 0,
        "with_wait_queue": 0,
        "with_collection_items": 0,
        "with_missing_delivery": 0,
        "skipped_active": 0,
    }

    with Session(bind=engine) as session:
        midnight_posts = [
            post for post in session.query(Posts).filter(Posts.created_at != None).all()
            if post.created_at.time() == datetime.min.time()
        ]
        post_ids = [post.id for post in midnight_posts]
        stats["with_reservations"] = count_linked_midnight_posts(session, Reservations, post_ids)
        stats["with_in_delivery"] = count_linked_midnight_posts(session, InDelivery, post_ids)
        stats["with_temp_fulfilled"] = count_linked_midnight_posts(session, Temp_Fulfilled, post_ids)
        stats["with_wait_queue"] = count_linked_midnight_posts(session, TempReservations, post_ids)
        stats["with_collection_items"] = count_linked_midnight_posts(session, DeliveryCollectionItem, post_ids)
        stats["with_missing_delivery"] = count_linked_midnight_posts(session, MissingDeliveryItem, post_ids)

        for post in midnight_posts:
            if post_has_active_links(session, post.id):
                stats["skipped_active"] += 1
                continue
            if ensure_deleted_post_snapshot(session, post, reason):
                stats["snapshotted"] += 1
            session.delete(post)
            stats["deleted"] += 1

        session.commit()

    return stats


def keyboard_for_editing():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Изменить адрес", callback_data="edit_address"))
    keyboard.add(types.InlineKeyboardButton("Изменить номер телефона", callback_data="new_phone"))
    keyboard.add(types.InlineKeyboardButton("Отказаться от доставки", callback_data="delivery_otmena"))
    return keyboard

@bot.callback_query_handler(func=lambda call: call.data == "delivery_otmena")
def handle_delivery_otmena(call):
    if ignore_silent_blocked_callback(call):
        return

    safe_answer_callback_query(callback_query_id=call.id)
    try:
        campaign_id = mark_delivery_broadcast_response(call.from_user.id, DELIVERY_RECIPIENT_RESPONSE_DECLINED)
        refresh_delivery_candidates_summary(campaign_id)
    except Exception as e:
        logger.warning("Delivery cancel campaign response update failed for user_id=%s: %s", call.from_user.id, e)

    try:
        # Удаляем сообщение рассылки
        safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)

        # Отправляем уведомление пользователю
        safe_send_message(
            bot,
            call.message.chat.id,
            "Вы отказались от доставки. Оповестим вас при следующей доставке.",
            logger=logger,
        )
    except Exception as e:
        logger.warning("Delivery cancel callback failed for user_id=%s: %s", call.from_user.id, e)

@bot.callback_query_handler(func=lambda call: get_user_state(call.from_user.id) == "WAITING_FOR_DATA_EDIT")
def handle_data_editing(call):
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    flow_data = get_temp_user_data_or_reset(
        user_id,
        required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at"),
        notice_chat_id=user_id,
        notice_text="Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
    )
    if not flow_data:
        safe_answer_callback_query(call.id, "Данные доставки потерялись.", show_alert=True)
        return

    action = call.data


    if action == "new_phone":
        set_user_state(user_id, "WAITING_FOR_NEW_PHONE")
        safe_answer_callback_query(call.id)
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text="Введите новый номер телефона:"
        )
    elif action == "edit_address":
        set_user_state(user_id, "WAITING_FOR_NEW_ADDRESS")
        safe_answer_callback_query(call.id)
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text="Введите новый адрес доставки:"
        )
    else:
        logger.warning("Unknown delivery edit action=%s for user_id=%s", action, user_id)
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)

@bot.message_handler(func=lambda message: message_user_state_is(message, "WAITING_FOR_NEW_ADDRESS"))
def handle_new_address(message):
    """
    Обработка нового адреса от пользователя.
    """
    user_id = message_from_user_id(message)
    if user_id is None:
        logger.warning("Delivery address edit skipped: missing message.from_user")
        return
    if ignore_silent_blocked_message(message):
        return

    new_address = require_text_message(message, "Введите новый адрес текстом.")
    if new_address is None:
        return
    flow_data = get_temp_user_data_or_reset(
        user_id,
        required_fields=("name", "phone", "final_sum", "delivery_cutoff_at"),
        notice_chat_id=user_id,
        notice_text="Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
    )
    if not flow_data:
        return

    flow_data["address"] = new_address  # Сохранение нового адреса
    temp_user_data[user_id] = flow_data

    # Получаем временные данные пользователя
    name = flow_data.get("name", "Не указано")
    phone = flow_data.get("phone", "Не указан")
    final_sum = flow_data.get("final_sum", 0)
    delivery_cutoff_at = parse_datetime(flow_data.get("delivery_cutoff_at")) or now_local()

    # Получаем всех клиентов с таким же номером телефона
    from db import Session, engine
    with Session(bind=engine) as session:
        same_phone_users = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(phone))
        ).all()

    # Считаем общую сумму заказов и собираем имена всех клиентов
    total_sum_by_phone = final_sum
    all_names = [name]  # Добавляем текущее имя
    seen_delivery_user_ids = {user_id}
    for client in same_phone_users:
        if client.user_id in seen_delivery_user_ids:
            continue
        seen_delivery_user_ids.add(client.user_id)
        all_names.append(client.name)
        total_sum_by_phone += calculate_collectable_sum_for_user(client.user_id, cutoff_at=delivery_cutoff_at)

    # Формируем строку с именами всех клиентов
    all_names_str = ", ".join(all_names)

    # Отправляем обновлённое сообщение с данными
    safe_send_message(
        bot,
        user_id,
        (
            f"Данные обновлены:\n"
            f"Имя: {name}\nТелефон: {phone}\nНовый адрес: {new_address}\n"
            f"Имена заказчиков: {all_names_str}\n"
            f"Общая сумма заказов: {total_sum_by_phone}.\n\n"
            f"Подтверждаете изменения?"
        ),
        reply_markup=keyboard_for_confirmation(),
        logger=logger,
    )
    set_user_state(user_id, "WAITING_FOR_CONFIRMATION")

@bot.message_handler(func=lambda message: message_user_state_is(message, "WAITING_FOR_NEW_PHONE"))
def handle_new_phone(message):
    """
    Обработка нового номера телефона пользователя.
    Должен учитывать информацию по старому номеру телефона и временно сохранять новый номер.
    """
    user_id = message_from_user_id(message)
    if user_id is None:
        logger.warning("Delivery phone edit skipped: missing message.from_user")
        return
    if ignore_silent_blocked_message(message):
        return

    text = require_text_message(message, "Введите новый номер телефона текстом.")
    if text is None:
        return
    new_phone = normalize_phone(text)  # Убираем лишние пробелы
    if not is_phone_valid(new_phone):
        safe_send_message(bot, user_id, "Введите корректный номер телефона.", logger=logger)
        return

    flow_data = get_temp_user_data_or_reset(
        user_id,
        required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at"),
        notice_chat_id=user_id,
        notice_text="Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
    )
    if not flow_data:
        return

    # Временные данные текущего пользователя
    name = flow_data.get("name", "Не указано")
    current_phone = flow_data.get("phone", "Не указан")  # Это старый номер телефона
    address = flow_data.get("address", "Не указан")
    final_sum = flow_data.get("final_sum", 0)
    delivery_cutoff_at = parse_datetime(flow_data.get("delivery_cutoff_at")) or now_local()


    # Подключаемся к базе данных, чтобы найти тех, у кого такой же старый номер телефона (current_phone)
    from db import Session, engine, Clients
    with Session(bind=engine) as session:
        try:
            # Найти всех клиентов с текущим (старым) номером телефона
            same_phone_users = session.query(Clients).filter(
                Clients.phone.in_(phone_variants(current_phone))
            ).all()


        except Exception as e:
            logger.warning("Delivery phone lookup failed for user_id=%s: %s", user_id, e)
            same_phone_users = []

    # Подсчитываем общую сумму всех заказов и собираем имена
    total_sum_by_phone = final_sum  # Начинаем с суммы текущего пользователя
    all_names = [name]  # Добавляем название текущего клиента
    seen_delivery_user_ids = {user_id}
    for client in same_phone_users:
        if client.user_id in seen_delivery_user_ids:
            continue
        seen_delivery_user_ids.add(client.user_id)
        all_names.append(client.name)
        order_sum = calculate_collectable_sum_for_user(client.user_id, cutoff_at=delivery_cutoff_at)
        total_sum_by_phone += order_sum

    # Формируем строку с именами всех клиентов
    all_names_str = ", ".join(all_names)

    # Сохраняем новый номер временно
    flow_data["phone"] = new_phone
    temp_user_data[user_id] = flow_data

    # Отправляем итоговое сообщение
    safe_send_message(
        bot,
        user_id,
        (
            f"Обновление данных:\n"
            f"Текущий номер (старый): {current_phone}\n"
            f"Новый номер: {new_phone}\n"
            f"Имя: {name}\nАдрес: {address}\n"
            f"Имена заказчиков с текущим номером: {all_names_str}\n"
            f"Общая сумма заказов: {total_sum_by_phone}.\n\n"
            f"Подтверждаете изменения?"
        ),
        reply_markup=keyboard_for_confirmation(),
        logger=logger,
    )

    # Переводим пользователя в состояние ожидания подтверждения
    set_user_state(user_id, "WAITING_FOR_CONFIRMATION")

def keyboard_for_confirmation():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Да", callback_data="confirm_yes"))
    keyboard.add(types.InlineKeyboardButton("Нет", callback_data="confirm_no"))
    return keyboard

# Обработчик подтверждения или отмены изменений
@bot.callback_query_handler(
    func=lambda call: get_user_state(call.from_user.id) == "WAITING_FOR_CONFIRMATION"
    and call.data in ["confirm_yes", "confirm_no"]
)
def handle_confirmation(call):
    """
    Обработка подтверждения данных. Телефон и другая информация извлекаются:
    - Старый телефон — только из таблицы Clients.
    - Новые данные (телефон, адрес) — из temp_user_data.
    """
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    confirmation = call.data  # "confirm_yes" или "confirm_no"
    safe_answer_callback_query(call.id)

    if confirmation == "confirm_yes":
        # Получаем временные данные пользователя (новые данные)
        user_temp_data = get_temp_user_data_or_reset(
            user_id,
            required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at"),
            notice_chat_id=user_id,
            notice_text="Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
        )
        if not user_temp_data:
            return

        # Извлекаем новые данные из временного хранилища
        name = user_temp_data.get("name", "Не указано")
        phone = user_temp_data.get("phone", "Не указан")  # Новый телефон
        address = user_temp_data.get("address", "Не указан")
        final_sum = user_temp_data.get("final_sum", 0)  # Сумма текущего заказа
        delivery_cutoff_at = parse_datetime(user_temp_data.get("delivery_cutoff_at")) or now_local()


        from db import Session, engine, Clients, ForDelivery

        # Подключаемся к базе для извлечения старого телефона из Clients
        with Session(bind=engine) as session:
            try:
                # Ищем клиента в таблице Clients по user_id
                client = session.query(Clients).filter(Clients.user_id == user_id).first()
                if not client:
                    # Если клиент отсутствует в таблице Clients, сообщаем об ошибке
                    logger.warning("Delivery confirmation client not found for user_id=%s", user_id)
                    safe_send_message(bot, user_id, "Ошибка! Клиент не найден в базе данных. Попробуйте снова.", logger=logger)
                    return

                # Старый телефон: извлекаем его из записи в Clients
                old_phone = client.phone
                logger.debug("Delivery confirmation loaded old phone for user_id=%s", user_id)

                # Инициализируем общую сумму и список связанных клиентов
                total_sum_by_phone = final_sum
                all_names = [name]

                same_phone_users = session.query(Clients).filter(
                    Clients.phone.in_(phone_variants(old_phone))
                ).all()

                if same_phone_users:
                    logger.debug(
                        "Delivery confirmation related clients found for user_id=%s count=%s",
                        user_id,
                        len(same_phone_users),
                    )

                    # Вычисляем общую сумму заказов всех связанных клиентов
                    seen_delivery_user_ids = {user_id}
                    for other_client in same_phone_users:
                        if other_client.user_id in seen_delivery_user_ids:
                            continue
                        seen_delivery_user_ids.add(other_client.user_id)
                        all_names.append(other_client.name)
                        order_sum = calculate_collectable_sum_for_user(other_client.user_id, cutoff_at=delivery_cutoff_at)
                        total_sum_by_phone += order_sum
                else:
                    logger.debug("Delivery confirmation related clients not found for user_id=%s", user_id)

                # Формируем список имен клиентов
                all_names_str = ", ".join(all_names)

            except Exception as e:
                logger.warning("Delivery confirmation database error for user_id=%s: %s", user_id, e)
                safe_send_message(
                    bot,
                    user_id,
                    "Произошла ошибка при обработке данных. Попробуйте снова.",
                    logger=logger,
                )
                return

        # Сохраняем новые данные в таблицу ForDelivery
        notify_delivery_channel = True
        with Session(bind=engine) as session:
            try:
                existing_delivery_entry = find_for_delivery_entry_for_user(session, user_id)
                notify_delivery_channel = not (
                    existing_delivery_entry
                    and same_delivery_cutoff_day(existing_delivery_entry.delivery_cutoff_at, delivery_cutoff_at)
                )
                delivery_entry = upsert_for_delivery_entry(
                    session,
                    user_id,
                    name,
                    phone,
                    address,
                    total_sum_by_phone,
                    delivery_cutoff_at=delivery_cutoff_at,
                )
                total_sum_by_phone = delivery_entry.total_sum
                session.commit()
            except Exception as e:
                session.rollback()
                logger.warning("Delivery confirmation save failed for user_id=%s: %s", user_id, e)
                safe_send_message(
                    bot,
                    user_id,
                    "Произошла ошибка при сохранении данных. Попробуйте снова.",
                    logger=logger,
                )
                return

        campaign_id = mark_delivery_broadcast_response(user_id, DELIVERY_RECIPIENT_RESPONSE_ACCEPTED)
        refresh_delivery_candidates_summary(campaign_id)

        message_for_channel = (
            f"📦 Новый заказ на доставку:\n"
            f"👤 Имя: {name}\n"
            f"📞 Телефон: {phone}\n"
            f"💰 Общая сумма заказов: {total_sum_by_phone}\n"
            f"📍 Адрес доставки: {address}"
        )
        if notify_delivery_channel:
            try:
                safe_send_message(bot, delivery_channel, message_for_channel, logger=logger)
            except Exception as e:
                logger.warning("Delivery channel notification failed for user_id=%s: %s", user_id, e)

        # Отправляем подтверждающее сообщение пользователю
        edited_message = safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text=(
                f"Ваш заказ подтвержден и будет доставлен на указанный адрес:\n"
                f"Связанные клиенты: {all_names_str}\n"
                f"Общая сумма заказов: {total_sum_by_phone}\n"
                f"Адрес доставки: {address}"
            )
        )
        if edited_message is None:
            safe_send_message(
                bot,
                user_id,
                (
                    f"Ваш заказ подтвержден и будет доставлен на указанный адрес:\n"
                    f"Связанные клиенты: {all_names_str}\n"
                    f"Общая сумма заказов: {total_sum_by_phone}\n"
                    f"Адрес доставки: {address}"
                ),
                logger=logger,
            )

        # Удаляем временные данные и сбрасываем состояние пользователя
        if user_id in temp_user_data:
            del temp_user_data[user_id]
        set_user_state(user_id, None)

    elif confirmation == "confirm_no":
        user_temp_data = get_temp_user_data_or_reset(
            user_id,
            required_fields=("name", "phone", "address", "final_sum", "delivery_cutoff_at"),
            notice_chat_id=user_id,
            notice_text="Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
        )
        if not user_temp_data:
            return

        # Пользователь отказался подтверждать данные
        edit_prompt = safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text="Вы хотите изменить данные? Выберите вариант ниже:",
            reply_markup=keyboard_for_editing()
        )
        if edit_prompt is None:
            edit_prompt = safe_send_message(
                bot,
                user_id,
                "Вы хотите изменить данные? Выберите вариант ниже:",
                reply_markup=keyboard_for_editing(),
                logger=logger,
            )
        if not edit_prompt:
            logger.warning("Delivery edit choice prompt was not sent for user_id=%s", user_id)
            return
        set_user_state(user_id, "WAITING_FOR_DATA_EDIT")


@bot.callback_query_handler(func=lambda call: call.data in ["confirm_yes", "confirm_no"])
def handle_stale_delivery_confirmation(call):
    if ignore_silent_blocked_callback(call):
        return
    safe_answer_callback_query(
        call.id,
        "Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
        show_alert=True,
    )


# Клавиатура для доставки да или нет
def keyboard_for_delivery():
    """
        Создает новую inline-клавиатуру с кнопками "Да" и "Нет".
        """
    keyboard = InlineKeyboardMarkup()  # Создаем разметку для клавиатуры
    yes_button = InlineKeyboardButton(text="Да", callback_data="delivery_yes")  # Кнопка "Да"
    no_button = InlineKeyboardButton(text="Нет", callback_data="delivery_no")  # Кнопка "Нет"
    keyboard.add(yes_button, no_button)  # Добавляем кнопки в клавиатуру
    return keyboard

def calculate_for_delivery(cutoff_at=None):
    """
    Вычисляет общую сумму обработанных заказов клиентов, объединяет заказы для клиентов с одинаковым номером телефона.
    Сообщение отправляется одному клиенту с минимальным ID. Логи содержат индивидуальную сумму, суммы других клиентов, и итоговую сумму.
    """
    cutoff_at = cutoff_at or delivery_campaign_cutoff_at()
    with Session(bind=engine) as session:
        query = session.query(Reservations, Posts, Clients).outerjoin(
            Posts, Posts.id == Reservations.post_id
        ).join(
            Clients, Clients.user_id == Reservations.user_id
        ).filter(
            Reservations.is_fulfilled == True,
        )
        if cutoff_at:
            query = query.filter(or_(Reservations.fulfilled_at == None, Reservations.fulfilled_at <= cutoff_at))
        rows = query.all()

    if not rows:
        logger.info("Delivery broadcast: no fulfilled reservations found.")
        return []

    grouped_by_phone = {}
    for reservation, post, client in rows:
        phone = normalize_phone(client.phone)
        if not phone:
            continue

        group = grouped_by_phone.setdefault(phone, {
            "total_amount": 0,
            "clients": {},
            "reservation_ids": set(),
        })
        if reservation.id in group["reservation_ids"]:
            continue
        group["reservation_ids"].add(reservation.id)
        group["total_amount"] += max(calculate_order_amount(reservation, post), 0)
        group["clients"][client.id] = client

    delivery_users = []
    for phone, group in grouped_by_phone.items():
        total_amount = group["total_amount"]
        if total_amount < DELIVERY_THRESHOLD:
            logger.info(
                "Delivery broadcast: phone %s skipped, total %s below threshold %s.",
                phone,
                total_amount,
                DELIVERY_THRESHOLD,
            )
            continue

        selected_client = sorted(group["clients"].values(), key=lambda c: c.id)[0]
        delivery_users.append({
            "user_id": selected_client.user_id,
            "name": selected_client.name,
            "phone": phone,
            "total_amount": total_amount,
        })

    return delivery_users


def get_delivery_clients_summary():
    with Session(bind=engine) as session:
        rows = session.query(Reservations, Posts, Clients).outerjoin(
            Posts, Posts.id == Reservations.post_id
        ).join(
            Clients, Clients.user_id == Reservations.user_id
        ).all()

    grouped_by_phone = {}
    for reservation, post, client in rows:
        phone = normalize_phone(client.phone)
        phone_key = phone or f"user:{client.user_id}"
        group = grouped_by_phone.setdefault(phone_key, {
            "phone": phone or "номер не указан",
            "names": set(),
            "total_orders_sum": 0,
            "processed_orders_sum": 0,
        })

        group["names"].add(client.name or "Имя не указано")
        amount = max(calculate_order_amount(reservation, post), 0)
        group["total_orders_sum"] += amount
        if reservation.is_fulfilled:
            group["processed_orders_sum"] += amount

    clients = []
    for group in grouped_by_phone.values():
        if group["processed_orders_sum"] < DELIVERY_THRESHOLD:
            continue

        names = ", ".join(sorted(group["names"])) or "Имя не указано"
        clients.append({
            "phone": group["phone"],
            "name": truncate_button_text(names, 180),
            "total_orders_sum": group["total_orders_sum"],
            "processed_orders_sum": group["processed_orders_sum"],
        })

    return sorted(
        clients,
        key=lambda row: (row["processed_orders_sum"], row["total_orders_sum"]),
        reverse=True,
    )


def build_delivery_clients_summary_messages(clients, max_length=3500):
    header = (
        "Список клиентов для доставки\n"
        f"Порог: обработанные заказы от {DELIVERY_THRESHOLD} ₽\n"
        f"Всего клиентов: {len(clients)}"
    )
    messages = []
    current = header

    for index, client in enumerate(clients, start=1):
        block = (
            f"{index}. Телефон: {client['phone']}\n"
            f"Имя: {client['name']}\n"
            f"Общая сумма заказов: {format_money(client['total_orders_sum'])} ₽\n"
            f"Общая сумма обработанных заказов: {format_money(client['processed_orders_sum'])} ₽"
        )
        candidate = f"{current}\n\n{block}"
        if len(candidate) > max_length and current != header:
            messages.append(current)
            current = f"Список клиентов для доставки (продолжение)\n\n{block}"
        else:
            current = candidate

    messages.append(current)
    return messages


# Отправка рассылки
def send_delivery_offer_message(user_id, user_name, bot_instance=None):
    bot_instance = bot_instance or bot
    sent_message = call_telegram_with_retries(
        lambda: bot_instance.send_message(
            chat_id=user_id,
            text=(
                f"{user_name}, {delivery_target_label()}. "
                f"Готовы принять доставку с 10:00 до 16:00? "
                f"Ответьте до {delivery_response_deadline_label()}."
            ),
            reply_markup=keyboard_for_delivery(),
        ),
        logger=logger,
        attempts=3,
        description="Telegram delivery offer send_message",
    )
    logger.debug("Delivery offer sent to user_id=%s", user_id)
    return sent_message


def send_delivery_offer(bot_instance, user_id, user_name):
    try:
        return send_delivery_offer_message(user_id, user_name, bot_instance=bot_instance)
    except Exception as e:
        logger.warning("Delivery offer send failed for user_id=%s: %s", user_id, sanitize_telegram_error_text(e))
        raise


def add_user_id_to_set(user_ids, value):
    if value is None:
        return
    try:
        user_ids.add(int(value))
    except (TypeError, ValueError):
        return


def get_related_delivery_user_ids_by_phone(session, phone, fallback_user_id=None):
    normalized_phone = normalize_phone(phone)
    related_user_ids = set()

    if normalized_phone:
        related_clients = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(normalized_phone))
        ).all()
        for client in related_clients:
            add_user_id_to_set(related_user_ids, client.user_id)

    add_user_id_to_set(related_user_ids, fallback_user_id)
    return sorted(related_user_ids)


def get_related_delivery_user_ids(session, delivery_entry):
    client = session.query(Clients).filter(Clients.user_id == delivery_entry.user_id).first()
    source_phone = normalize_phone(client.phone if client else delivery_entry.phone)
    return get_related_delivery_user_ids_by_phone(session, source_phone, delivery_entry.user_id)


def calculate_delivery_total_for_user_ids(session, related_user_ids, cutoff_at):
    unique_user_ids = set()
    for user_id in related_user_ids:
        add_user_id_to_set(unique_user_ids, user_id)
    unique_user_ids = sorted(unique_user_ids)
    if not unique_user_ids:
        return 0

    reservations = get_delivery_reservations_query(
        session,
        unique_user_ids,
        cutoff_at,
    ).all()
    if not reservations:
        return 0

    posts_by_id = get_posts_or_snapshots_by_ids(
        session,
        {reservation.post_id for reservation in reservations},
    )

    total_sum = 0
    seen_reservation_ids = set()
    for reservation in reservations:
        if reservation.id in seen_reservation_ids:
            continue
        seen_reservation_ids.add(reservation.id)
        post = posts_by_id.get(reservation.post_id)
        temp_item = get_temp_fulfilled_for_reservation(session, reservation)
        total_sum += max(calculate_delivery_row_amount(reservation, post=post, temp_item=temp_item), 0)
    return total_sum


def calculate_delivery_total_for_phone(session, phone, fallback_user_id, cutoff_at):
    related_user_ids = get_related_delivery_user_ids_by_phone(session, phone, fallback_user_id)
    return calculate_delivery_total_for_user_ids(session, related_user_ids, cutoff_at)


def get_temp_fulfilled_for_reservation(session, reservation):
    temp_item = session.query(Temp_Fulfilled).filter(
        Temp_Fulfilled.reservation_id == reservation.id,
    ).first()
    if temp_item:
        return temp_item

    return session.query(Temp_Fulfilled).filter(
        Temp_Fulfilled.reservation_id == None,
        Temp_Fulfilled.user_id == reservation.user_id,
        Temp_Fulfilled.post_id == reservation.post_id,
    ).order_by(Temp_Fulfilled.created_at).first()


def calculate_delivery_row_amount(reservation, post=None, temp_item=None):
    if post:
        return max(calculate_order_amount(reservation, post), 0)

    if temp_item and temp_item.price is not None:
        return max(int(temp_item.price), 0)

    unit_price = reservation.old_price or 0
    return max((unit_price * reservation.quantity) - (reservation.return_order or 0), 0)


def build_delivery_row_description(reservation, post=None, temp_item=None):
    if post and post.description:
        return post.description
    if temp_item and temp_item.item_description:
        return temp_item.item_description
    return f"Товар удалён из карточек (ID {reservation.post_id})"


def get_delivery_row_created_at(reservation, post=None, temp_item=None):
    if post and post.created_at:
        return post.created_at
    if temp_item and temp_item.created_at:
        return temp_item.created_at
    return reservation.created_at


def format_delivery_entry_label(delivery_entry):
    if not delivery_entry:
        return "корзина не найдена"
    return f"{delivery_entry.name or 'Имя не указано'} | {delivery_entry.phone or 'телефон не указан'}"


def get_delivery_root_entry(session, delivery_entry, lock=False):
    if not delivery_entry:
        return None

    current = delivery_entry
    seen_ids = set()
    while current and current.merged_into_delivery_id:
        current_id = int(current.id or 0)
        if current_id in seen_ids:
            logger.warning("Delivery cart merge cycle detected at delivery_id=%s", current_id)
            current.merged_into_delivery_id = None
            current.merge_order = 0
            session.flush()
            return current
        seen_ids.add(current_id)

        query = session.query(ForDelivery).filter(ForDelivery.id == current.merged_into_delivery_id)
        if lock:
            query = query.with_for_update()
        parent = query.first()
        if not parent:
            logger.warning(
                "Delivery cart merge parent missing delivery_id=%s parent_id=%s",
                current.id,
                current.merged_into_delivery_id,
            )
            current.merged_into_delivery_id = None
            current.merge_order = 0
            session.flush()
            return current
        current = parent

    return current


def get_delivery_collection_entries(session, delivery_entry, lock=False):
    root_entry = get_delivery_root_entry(session, delivery_entry, lock=lock)
    if not root_entry:
        return []

    if lock:
        root_entry = session.query(ForDelivery).filter(
            ForDelivery.id == root_entry.id,
        ).with_for_update().first()
        if not root_entry:
            return []

    query = session.query(ForDelivery).filter(
        ForDelivery.merged_into_delivery_id == root_entry.id,
    ).order_by(
        ForDelivery.merge_order.asc(),
        ForDelivery.id.asc(),
    )
    if lock:
        query = query.with_for_update()

    entries = [root_entry]
    for entry in query.all():
        if entry.id != root_entry.id:
            entries.append(entry)
    return entries


def get_delivery_collection_entry_ids(session, delivery_entry, lock=False):
    return [
        entry.id
        for entry in get_delivery_collection_entries(session, delivery_entry, lock=lock)
        if entry and entry.id
    ]


def find_delivery_entry_by_phone_input(session, raw_phone):
    normalized_phone = normalize_phone(raw_phone)
    if not normalized_phone:
        return None, "Введите номер телефона корзины."

    query = session.query(ForDelivery)
    if len(normalized_phone) >= 10:
        query = query.filter(ForDelivery.phone.in_(phone_variants(normalized_phone)))
    elif len(normalized_phone) >= 4:
        query = query.filter(ForDelivery.phone.like(f"%{normalized_phone}"))
    else:
        return None, "Введите полный номер телефона или минимум последние 4 цифры."

    rows = query.order_by(
        ForDelivery.name.asc(),
        ForDelivery.phone.asc(),
        ForDelivery.id.asc(),
    ).all()
    if not rows:
        return None, f"Корзина доставки по телефону {normalized_phone} не найдена."
    if len(rows) > 1:
        matches = "\n".join(
            f"- {format_delivery_entry_label(row)}"
            for row in rows[:10]
        )
        if len(rows) > 10:
            matches += f"\n...и еще {len(rows) - 10}"
        return None, f"Найдено несколько корзин. Введите телефон точнее:\n{matches}"
    return rows[0], None


def delivery_entries_have_placed_items(session, delivery_entries):
    delivery_ids = [entry.id for entry in delivery_entries if entry and entry.id]
    if not delivery_ids:
        return False
    return session.query(DeliveryCollectionItem.id).filter(
        DeliveryCollectionItem.delivery_id.in_(delivery_ids),
        DeliveryCollectionItem.placed_at != None,
    ).first() is not None


def merge_delivery_cart_entries(session, main_delivery_id, extra_phone_text):
    main_entry = session.query(ForDelivery).filter(
        ForDelivery.id == main_delivery_id,
    ).with_for_update().first()
    if not main_entry:
        return False, "Основная корзина уже обработана или удалена из списка доставки."

    main_entry = get_delivery_root_entry(session, main_entry, lock=True)
    if not main_entry:
        return False, "Основная корзина не найдена."

    extra_entry, error_text = find_delivery_entry_by_phone_input(session, extra_phone_text)
    if error_text:
        return False, error_text

    extra_entry = session.query(ForDelivery).filter(
        ForDelivery.id == extra_entry.id,
    ).with_for_update().first()
    if not extra_entry:
        return False, "Дополнительная корзина уже обработана или удалена из списка доставки."

    extra_root = get_delivery_root_entry(session, extra_entry, lock=True)
    if not extra_root:
        return False, "Дополнительная корзина не найдена."

    if extra_root.id == main_entry.id:
        return False, "Эти корзины уже объединены."

    main_entries = get_delivery_collection_entries(session, main_entry, lock=True)
    extra_entries = get_delivery_collection_entries(session, extra_root, lock=True)
    blocked_entry = next(
        (
            entry
            for entry in [*main_entries, *extra_entries]
            if entry.collector_user_id
        ),
        None,
    )
    if blocked_entry:
        return False, (
            "Нельзя объединить корзины, которые уже открыты на сборку. "
            f"Сейчас корзину {format_delivery_entry_label(blocked_entry)} собирает "
            f"{blocked_entry.collector_name or f'ID {blocked_entry.collector_user_id}'}."
        )
    if delivery_entries_have_placed_items(session, [*main_entries, *extra_entries]):
        return False, "Нельзя объединить корзины, где уже нажимали «Положил»."

    max_order = session.query(func.max(ForDelivery.merge_order)).filter(
        ForDelivery.merged_into_delivery_id == main_entry.id,
    ).scalar() or 0

    main_entry.merged_into_delivery_id = None
    main_entry.merge_order = 0
    for offset, entry in enumerate(extra_entries, start=1):
        if entry.id == main_entry.id:
            continue
        entry.merged_into_delivery_id = main_entry.id
        entry.merge_order = max_order + offset

    session.flush()
    main_entry.total_sum = calculate_delivery_entry_actual_total(session, main_entry)
    extra_labels = ", ".join(format_delivery_entry_label(entry) for entry in extra_entries)
    return True, (
        "Корзины объединены.\n"
        f"Основная: {format_delivery_entry_label(main_entry)}\n"
        f"Дополнительная: {extra_labels}\n"
        "В списке сборки будет показана только основная корзина, а товары дополнительной пойдут в конце."
    )


def allocate_delivery_amounts(rows_payload, manual_total_sum=None):
    base_amounts = [max(int(row["base_amount"] or 0), 0) for row in rows_payload]
    if manual_total_sum is None:
        return base_amounts

    target_total = max(int(manual_total_sum or 0), 0)
    if not rows_payload:
        return []
    if len(rows_payload) == 1:
        return [target_total]
    if target_total == 0:
        return [0] * len(rows_payload)

    weights = [amount for amount in base_amounts if amount > 0]
    if sum(weights) <= 0:
        weights = [max(int(row["quantity"] or 0), 1) for row in rows_payload]
    else:
        weights = [max(amount, 0) for amount in base_amounts]

    total_weight = sum(weights) or len(rows_payload)
    allocated = []
    allocated_sum = 0

    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            amount = target_total - allocated_sum
        else:
            amount = (target_total * weight) // total_weight
            allocated_sum += amount
        allocated.append(max(amount, 0))

    return allocated


def parse_delivery_total_input(text):
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    return int(digits)


def get_delivery_collection_reserved_group_items():
    with Session(bind=engine) as session:
        deduplicate_for_delivery_entries(session)
        session.flush()
        delivery_entries = session.query(ForDelivery).order_by(
            ForDelivery.name,
            ForDelivery.phone,
            ForDelivery.id,
        ).all()

        items = []
        seen_reservation_ids = set()
        for delivery_entry in delivery_entries:
            related_user_ids = get_related_delivery_user_ids(session, delivery_entry)
            reservations = get_delivery_reservations_query(
                session,
                related_user_ids,
                get_delivery_cutoff_at(delivery_entry),
            ).all()
            if not reservations:
                continue

            posts_by_id = get_posts_or_snapshots_by_ids(
                session,
                {reservation.post_id for reservation in reservations},
            )
            clients_by_user_id = {
                client.user_id: client
                for client in session.query(Clients).filter(
                    Clients.user_id.in_({reservation.user_id for reservation in reservations})
                ).all()
            }

            for reservation in reservations:
                if reservation.id in seen_reservation_ids:
                    continue
                seen_reservation_ids.add(reservation.id)

                post = posts_by_id.get(reservation.post_id)
                client = clients_by_user_id.get(reservation.user_id)
                temp_item = get_temp_fulfilled_for_reservation(session, reservation)
                amount = calculate_delivery_row_amount(reservation, post=post, temp_item=temp_item)
                unit_price = max((amount // reservation.quantity), 0) if reservation.quantity else 0
                items.append({
                    "reservation_id": reservation.id,
                    "post_id": reservation.post_id,
                    "photo": post.photo if post else None,
                    "description": build_delivery_row_description(reservation, post=post, temp_item=temp_item),
                    "unit_price": unit_price,
                    "quantity": reservation.quantity,
                    "total_price": amount,
                    "client_name": client.name if client and client.name else delivery_entry.name,
                    "phone": normalize_phone(client.phone if client else delivery_entry.phone) or delivery_entry.phone,
                    "address": delivery_entry.address or "",
                    "created_at": get_delivery_row_created_at(reservation, post=post, temp_item=temp_item),
                    "reserved_at": reservation.created_at,
                    "fulfilled_at": reservation.fulfilled_at,
                    "cutoff_at": get_delivery_cutoff_at(delivery_entry),
                    "author": format_post_author(post),
                })

        session.commit()

    return sorted(
        items,
        key=lambda item: (
            item.get("created_at") or datetime.min,
            item.get("reserved_at") or datetime.min,
            item["reservation_id"],
        ),
    )


def build_delivery_reserved_group_item_caption(item):
    return "\n".join([
        f"Id товара: {item['post_id']}",
        "Бронь на доставку",
        f"Клиент: {item['client_name'] or 'Имя не указано'}",
        f"Телефон: {item['phone'] or 'Телефон не указан'}",
        f"Описание: {item['description']}",
        f"Цена: {item['unit_price']} ₽",
        f"Количество: {item['quantity']}",
        f"Сумма: {item['total_price']} ₽",
        f"Дата создания: {format_cart_date(item.get('created_at'))}",
        f"Забронировано: {format_datetime(item.get('reserved_at'))}",
        f"Обработано: {format_datetime(item.get('fulfilled_at'))}",
        f"Срез доставки: {format_datetime(item.get('cutoff_at'))}",
        f"Адрес: {item['address'] or 'адрес не указан'}",
        f"Выложил: {item.get('author') or 'неизвестно'}",
    ])


def send_delivery_reserved_group_item(item):
    caption = build_delivery_reserved_group_item_caption(item)
    for attempt in range(2):
        try:
            send_photo_or_text(bot, TARGET_GROUP_ID, item.get("photo"), caption)
            return True
        except Exception as exc:
            if attempt == 0 and sleep_for_short_retry_after(exc, max_retry_after=10):
                continue
            logger.warning(
                "Delivery reserved group item send failed for reservation_id=%s: %s",
                item.get("reservation_id"),
                exc,
            )
            return False


def send_delivery_reserved_group_snapshot():
    safe_send_message(bot, TARGET_GROUP_ID, "Брони на доставку", logger=logger)
    time.sleep(RESERVED_GROUP_SEND_INTERVAL_SECONDS)

    items = get_delivery_collection_reserved_group_items()
    if not items:
        safe_send_message(bot, TARGET_GROUP_ID, "Товаров для сборки доставки не найдено.", logger=logger)
        return 0

    sent_count = 0
    for item in items:
        if send_delivery_reserved_group_item(item):
            sent_count += 1
        time.sleep(RESERVED_GROUP_SEND_INTERVAL_SECONDS)

    return sent_count


def start_delivery_reserved_group_pause_and_snapshot():
    with Session(bind=engine) as session:
        deduplicate_for_delivery_entries(session)
        session.commit()
        delivery_count = session.query(func.count(ForDelivery.id)).scalar() or 0

    if delivery_count == 0:
        return False, 0

    if not activate_reserved_group_delivery_pause():
        logger.info("Delivery collection reserved group pause already active.")
        return False, 0

    try:
        sent_count = send_delivery_reserved_group_snapshot()
    except Exception as exc:
        clear_reserved_group_delivery_pause(0)
        logger.exception("Delivery reserved group snapshot failed: %s", exc)
        return False, 0

    logger.info("Delivery reserved group snapshot sent: items=%s", sent_count)
    return True, sent_count


def get_single_delivery_entry_cart_items(session, delivery_entry):
    related_user_ids = get_related_delivery_user_ids(session, delivery_entry)
    cutoff_at = get_delivery_cutoff_at(delivery_entry)
    reservations = get_delivery_reservations_query(
        session,
        related_user_ids,
        cutoff_at,
    ).all()

    if not reservations:
        return []

    posts_by_id = get_posts_or_snapshots_by_ids(
        session,
        {reservation.post_id for reservation in reservations},
    )
    clients_by_user_id = {
        client.user_id: client
        for client in session.query(Clients).filter(
            Clients.user_id.in_({reservation.user_id for reservation in reservations})
        ).all()
    }

    items = []
    for reservation in reservations:
        post = posts_by_id.get(reservation.post_id)
        client = clients_by_user_id.get(reservation.user_id)
        temp_item = get_temp_fulfilled_for_reservation(session, reservation)
        amount = calculate_delivery_row_amount(reservation, post=post, temp_item=temp_item)
        quantity = max(int(reservation.quantity or 0), 1)
        base_unit_price = max((amount // quantity), 0)
        remainder = max(amount - (base_unit_price * quantity), 0)
        description = build_delivery_row_description(reservation, post=post, temp_item=temp_item)
        created_at = get_delivery_row_created_at(reservation, post=post, temp_item=temp_item)
        author = format_post_author(post)
        names = set()
        if client and client.name:
            names.add(client.name)

        for unit_index in range(quantity):
            unit_price = base_unit_price + (1 if unit_index < remainder else 0)
            items.append({
                "post_id": reservation.post_id,
                "user_id": reservation.user_id,
                "user_name": client.name if client and client.name else delivery_entry.name,
                "phone": normalize_phone(client.phone if client else delivery_entry.phone) or delivery_entry.phone,
                "address": delivery_entry.address or "",
                "photo": post.photo if post else None,
                "description": description,
                "unit_price": unit_price,
                "quantity": 1,
                "total_price": unit_price,
                "names": names,
                "created_at": created_at,
                "reserved_at": reservation.created_at,
                "reservation_id": reservation.id,
                "unit_index": unit_index,
                "author": author,
                "show_quantity": False,
            })

    return sorted(
        items,
        key=lambda item: (
            item.get("created_at") or datetime.min,
            item.get("reserved_at") or datetime.min,
            item["reservation_id"],
            item["unit_index"],
        ),
    )


def get_delivery_entry_cart_items(session, delivery_entry):
    delivery_entries = get_delivery_collection_entries(session, delivery_entry)
    if not delivery_entries:
        return []

    items = []
    seen_keys = set()
    for source_order, source_entry in enumerate(delivery_entries):
        for item in get_single_delivery_entry_cart_items(session, source_entry):
            key = delivery_collection_cart_item_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            item["source_delivery_id"] = source_entry.id
            item["source_delivery_order"] = source_order
            items.append(item)

    return sorted(
        items,
        key=lambda item: (
            item.get("source_delivery_order", 0),
            item.get("created_at") or datetime.min,
            item.get("reserved_at") or datetime.min,
            item["reservation_id"],
            item["unit_index"],
        ),
    )


def get_delivery_entry_full_cart_report_items(session, delivery_entry):
    related_user_ids = get_related_delivery_user_ids(session, delivery_entry)
    reservations = session.query(Reservations).filter(
        Reservations.user_id.in_(related_user_ids),
    ).order_by(
        Reservations.created_at,
        Reservations.id,
    ).all()

    if not reservations:
        return []

    posts_by_id = get_posts_or_snapshots_by_ids(
        session,
        {reservation.post_id for reservation in reservations},
    )

    items = []
    for reservation in reservations:
        post = posts_by_id.get(reservation.post_id)
        temp_item = get_temp_fulfilled_for_reservation(session, reservation)
        amount = calculate_delivery_row_amount(reservation, post=post, temp_item=temp_item)
        items.append({
            "post_id": reservation.post_id,
            "photo": post.photo if post else None,
            "description": build_delivery_row_description(reservation, post=post, temp_item=temp_item),
            "quantity": max(int(reservation.quantity or 0), 1),
            "total_price": max(int(amount or 0), 0),
            "created_at": get_delivery_row_created_at(reservation, post=post, temp_item=temp_item),
            "reserved_at": reservation.created_at,
            "reservation_id": reservation.id,
            "author": format_post_author(post),
        })

    return items


def get_delivery_collection_report_collector_name(session, collector_user_id):
    if not collector_user_id:
        return None

    client = session.query(Clients).filter(Clients.user_id == collector_user_id).first()
    if client and client.name:
        return client.name

    return None


def build_delivery_collection_report_context(session, delivery_entry):
    return {
        "name": delivery_entry.name,
        "phone": delivery_entry.phone,
        "collector_name": get_delivery_collection_report_collector_name(
            session,
            delivery_entry.collector_user_id,
        ),
    }


def build_delivery_collection_report_items_from_collection_items(collection_items):
    return [
        {
            "collection_item_ids": [item.id] if item.id else [],
            "post_id": item.post_id,
            "photo": item.photo,
            "client_name": item.user_name,
            "phone": item.phone,
            "description": item.item_description,
            "quantity": max(int(item.quantity or 0), 1),
            "total_price": max(int(item.amount or 0), 0),
            "created_at": None,
            "reserved_at": item.created_at,
            "reservation_id": item.reservation_id,
            "unit_index": item.unit_index,
            "author": None,
            "placed_count": max(int(item.quantity or 0), 1) if item.placed_at else 0,
        }
        for item in collection_items
    ]


def group_delivery_collection_report_items(items):
    grouped_items = []
    grouped_by_key = {}

    for item in items:
        item_key = (
            item.get("phone"),
            item.get("client_name"),
            item.get("post_id"),
            item.get("description"),
            item.get("created_at"),
            item.get("author"),
        )
        if item_key not in grouped_by_key:
            grouped_item = {
                "collection_item_ids": [],
                "post_id": item.get("post_id"),
                "photo": item.get("photo"),
                "client_name": item.get("client_name"),
                "phone": item.get("phone"),
                "description": item.get("description"),
                "quantity": 0,
                "total_price": 0,
                "created_at": item.get("created_at"),
                "author": item.get("author"),
                "placed_count": 0,
            }
            grouped_by_key[item_key] = grouped_item
            grouped_items.append(grouped_item)

        grouped_item = grouped_by_key[item_key]
        grouped_item["collection_item_ids"].extend(item.get("collection_item_ids") or [])
        grouped_item["quantity"] += max(int(item.get("quantity") or 0), 1)
        grouped_item["total_price"] += max(int(item.get("total_price") or item.get("unit_price") or 0), 0)
        grouped_item["placed_count"] += max(int(item.get("placed_count") or 0), 0)

    return grouped_items


def build_delivery_collection_report_header(context, grouped_items):
    total_quantity = sum(item["quantity"] for item in grouped_items)
    total_sum = sum(item["total_price"] for item in grouped_items)
    return "\n".join([
        "🧺 Начало сборки доставки",
        f"Номер телефона клиента: {context['phone'] or 'не указан'}",
        f"Клиент: {context['name'] or 'имя не указано'}",
        f"Кто собирает: {context['collector_name'] or 'неизвестно'}",
        f"Товаров в списке: {total_quantity}",
        f"Сумма товаров: {total_sum} ₽",
    ])


def format_delivery_collection_report_item_status(item):
    quantity = max(int(item.get("quantity") or 0), 1)
    placed_count = max(int(item.get("placed_count") or 0), 0)
    if placed_count <= 0:
        return "⏳ Не положил"
    if placed_count >= quantity:
        return "✅ Положил"
    return f"🟡 Положил {placed_count}/{quantity}"


def build_delivery_collection_report_item_caption(context, item):
    return "\n".join([
        f"Id товара: {item['post_id']}",
        f"Номер телефона клиента: {item.get('phone') or context['phone'] or 'не указан'}",
        f"Клиент: {item.get('client_name') or context['name'] or 'имя не указано'}",
        f"Кто собирает: {context['collector_name'] or 'неизвестно'}",
        f"Описание товара: {item['description']}",
        f"Сумма товара: {item['total_price']} ₽",
        f"Количество: {item['quantity']}",
        f"Статус: {format_delivery_collection_report_item_status(item)}",
    ])


def build_delivery_collection_report_footer(context, grouped_items):
    total_quantity = sum(item["quantity"] for item in grouped_items)
    total_sum = sum(item["total_price"] for item in grouped_items)
    return "\n".join([
        "✅ Конец корзины",
        f"Номер телефона клиента: {context['phone'] or 'не указан'}",
        f"Клиент: {context['name'] or 'имя не указано'}",
        f"Кто собирает: {context['collector_name'] or 'неизвестно'}",
        f"Товаров в списке: {total_quantity}",
        f"Сумма товаров: {total_sum} ₽",
    ])


def telegram_migrate_to_chat_id(exc):
    result_json = getattr(exc, "result_json", None) or {}
    parameters = result_json.get("parameters") or {}
    migrate_to_chat_id = parameters.get("migrate_to_chat_id")
    return int(migrate_to_chat_id) if migrate_to_chat_id else None


def update_delivery_collection_report_group_id(migrate_to_chat_id):
    global delivery_collection_report_target_group_id
    if not migrate_to_chat_id:
        return False

    old_chat_id = delivery_collection_report_target_group_id
    delivery_collection_report_target_group_id = int(migrate_to_chat_id)
    logger.warning(
        "Delivery collection report group migrated: old_chat_id=%s new_chat_id=%s",
        old_chat_id,
        delivery_collection_report_target_group_id,
    )
    return True


def send_delivery_collection_report_message(text):
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            return bot.send_message(delivery_collection_report_target_group_id, text)
        except Exception as exc:
            migrate_to_chat_id = telegram_migrate_to_chat_id(exc)
            if attempt == 1 and update_delivery_collection_report_group_id(migrate_to_chat_id):
                continue
            retry_after = telegram_retry_after_seconds(exc)
            if is_transient_telegram_error(exc) and attempt < attempts:
                wait_seconds = (
                    max(retry_after + 1, DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS)
                    if retry_after is not None
                    else DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS * attempt
                )
                logger.warning(
                    "Delivery collection report header send failed attempt=%s/%s, retrying in %ss: %s",
                    attempt,
                    attempts,
                    wait_seconds,
                    exc,
                )
                time.sleep(wait_seconds)
                continue
            raise


def send_delivery_collection_report_photo_or_text(photo, text):
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            return send_photo_or_text(bot, delivery_collection_report_target_group_id, photo, text)
        except Exception as exc:
            migrate_to_chat_id = telegram_migrate_to_chat_id(exc)
            if attempt == 1 and update_delivery_collection_report_group_id(migrate_to_chat_id):
                continue
            retry_after = telegram_retry_after_seconds(exc)
            if is_transient_telegram_error(exc) and attempt < attempts:
                wait_seconds = (
                    max(retry_after + 1, DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS)
                    if retry_after is not None
                    else DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS * attempt
                )
                logger.warning(
                    "Delivery collection report item send failed attempt=%s/%s, retrying in %ss: %s",
                    attempt,
                    attempts,
                    wait_seconds,
                    exc,
                )
                time.sleep(wait_seconds)
                continue
            raise


def save_delivery_collection_report_item_message(collection_item_ids, sent_message, message_has_photo):
    collection_item_ids = [
        int(item_id)
        for item_id in (collection_item_ids or [])
        if item_id
    ]
    if not collection_item_ids or not sent_message:
        return False

    chat_id = getattr(getattr(sent_message, "chat", None), "id", None)
    message_id = getattr(sent_message, "message_id", None)
    if not chat_id or not message_id:
        return False

    with Session(bind=engine) as session:
        session.query(DeliveryCollectionItem).filter(
            DeliveryCollectionItem.id.in_(collection_item_ids),
        ).update(
            {
                DeliveryCollectionItem.report_message_chat_id: chat_id,
                DeliveryCollectionItem.report_message_id: message_id,
                DeliveryCollectionItem.report_message_has_photo: bool(message_has_photo),
            },
            synchronize_session=False,
        )
        session.commit()
    return True


def build_delivery_collection_report_group_item_from_items(collection_items):
    report_items = build_delivery_collection_report_items_from_collection_items(collection_items)
    grouped_items = group_delivery_collection_report_items(report_items)
    return grouped_items[0] if grouped_items else None


def load_delivery_collection_report_group_item(session, collection_item_ids):
    collection_item_ids = [
        int(item_id)
        for item_id in (collection_item_ids or [])
        if item_id
    ]
    if not collection_item_ids:
        return None

    collection_items = session.query(DeliveryCollectionItem).filter(
        DeliveryCollectionItem.id.in_(collection_item_ids),
    ).order_by(
        DeliveryCollectionItem.created_at.asc(),
        DeliveryCollectionItem.id.asc(),
    ).all()
    return build_delivery_collection_report_group_item_from_items(collection_items)


def get_delivery_collection_report_item_for_send(item):
    collection_item_ids = item.get("collection_item_ids") or []
    if not collection_item_ids:
        return item

    with Session(bind=engine) as session:
        fresh_item = load_delivery_collection_report_group_item(session, collection_item_ids)
    return fresh_item or item


def edit_delivery_collection_report_item_message(item):
    if not item or not item.report_message_chat_id or not item.report_message_id:
        return False

    with Session(bind=engine) as session:
        related_items = session.query(DeliveryCollectionItem).filter(
            DeliveryCollectionItem.report_message_chat_id == item.report_message_chat_id,
            DeliveryCollectionItem.report_message_id == item.report_message_id,
        ).order_by(
            DeliveryCollectionItem.created_at.asc(),
            DeliveryCollectionItem.id.asc(),
        ).all()
        grouped_item = build_delivery_collection_report_group_item_from_items(related_items)
        if not grouped_item:
            return False
        context = {
            "name": related_items[0].user_name if related_items else None,
            "phone": related_items[0].phone if related_items else None,
            "collector_name": related_items[0].collector_name if related_items else None,
        }
        chat_id = item.report_message_chat_id
        message_id = item.report_message_id
        message_has_photo = bool(item.report_message_has_photo)

    caption = build_delivery_collection_report_item_caption(context, grouped_item)
    if message_has_photo:
        edited_caption = safe_edit_message_caption(
            bot,
            logger=logger,
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
        )
        if edited_caption is not None:
            return True
        logger.debug("Delivery collection report caption edit fallback for item_id=%s", item.id)

    return safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=chat_id,
        message_id=message_id,
        text=caption,
    ) is not None


def acquire_delivery_collection_report_db_lock():
    if engine.dialect.name != "postgresql":
        return None

    connection = engine.connect()
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": DELIVERY_COLLECTION_REPORT_LOCK_KEY},
        )
    except Exception:
        connection.close()
        raise
    return connection


def release_delivery_collection_report_db_lock(connection):
    if connection is None:
        return

    try:
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": DELIVERY_COLLECTION_REPORT_LOCK_KEY},
        )
    finally:
        connection.close()


def send_delivery_collection_report_item_to_group(context, item):
    current_item = get_delivery_collection_report_item_for_send(item)
    caption = build_delivery_collection_report_item_caption(context, current_item)
    message_has_photo = bool(current_item.get("photo") and len(caption) <= 1024)
    for attempt in range(2):
        try:
            sent_message = send_delivery_collection_report_photo_or_text(current_item.get("photo"), caption)
            save_delivery_collection_report_item_message(
                current_item.get("collection_item_ids"),
                sent_message,
                message_has_photo,
            )
            return True
        except Exception as exc:
            if attempt == 0 and sleep_for_short_retry_after(exc, max_retry_after=10):
                continue
            logger.warning(
                "Delivery collection report item send failed for post_id=%s: %s",
                item.get("post_id"),
                exc,
            )
            return False


def send_delivery_collection_report_to_group(context, items):
    grouped_items = group_delivery_collection_report_items(items)
    if not grouped_items:
        return 0

    with delivery_collection_report_lock:
        db_lock_connection = acquire_delivery_collection_report_db_lock()
        try:
            header = build_delivery_collection_report_header(context, grouped_items)
            try:
                send_delivery_collection_report_message(header)
            except Exception as exc:
                logger.warning(
                    "Delivery collection report header send failed for phone=%s: %s",
                    context.get("phone"),
                    exc,
                )
                return 0

            time.sleep(DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS)
            sent_count = 0
            for item in grouped_items:
                if send_delivery_collection_report_item_to_group(context, item):
                    sent_count += 1
                time.sleep(DELIVERY_COLLECTION_REPORT_SEND_INTERVAL_SECONDS)

            try:
                send_delivery_collection_report_message(
                    build_delivery_collection_report_footer(context, grouped_items)
                )
            except Exception as exc:
                logger.warning(
                    "Delivery collection report footer send failed for phone=%s: %s",
                    context.get("phone"),
                    exc,
                )
        finally:
            release_delivery_collection_report_db_lock(db_lock_connection)

    logger.info(
        "Delivery collection report sent: phone=%s collector=%s items=%s",
        context.get("phone"),
        context.get("collector_name"),
        sent_count,
    )
    return sent_count


def start_delivery_collection_report_to_group(context, items):
    thread = threading.Thread(
        target=send_delivery_collection_report_to_group,
        args=(dict(context), [dict(item) for item in items]),
        name="delivery-collection-report",
        daemon=True,
    )
    thread.start()
    return thread


def sort_delivery_collection_items(items):
    return sorted(
        items,
        key=lambda item: (
            getattr(item, "_delivery_order", 0),
            item.created_at or datetime.min,
            item.reservation_id,
            item.unit_index,
            item.id or 0,
        ),
    )


def get_or_create_delivery_collection_items(session, delivery_entry):
    delivery_entries = get_delivery_collection_entries(session, delivery_entry)
    delivery_order = {
        entry.id: index
        for index, entry in enumerate(delivery_entries)
        if entry and entry.id
    }
    delivery_ids = list(delivery_order.keys())
    if not delivery_ids:
        return []

    existing_items = session.query(DeliveryCollectionItem).filter(
        DeliveryCollectionItem.delivery_id.in_(delivery_ids),
    ).all()
    cart_items = get_delivery_entry_cart_items(session, delivery_entry)
    existing_by_key = {
        (int(item.reservation_id), int(item.unit_index or 0)): item
        for item in existing_items
    }
    result = []
    result_keys = set()

    for item in existing_items:
        item._delivery_order = delivery_order.get(item.delivery_id, 0)
        apply_delivery_collection_entry_to_item(item, delivery_entry)

    cart_items_by_key = {
        delivery_collection_cart_item_key(cart_item): cart_item
        for cart_item in cart_items
    }
    missing_keys = [key for key in cart_items_by_key if key not in existing_by_key]
    missing_existing_by_key = find_delivery_collection_items_by_keys(session, missing_keys)
    existing_by_key.update(missing_existing_by_key)

    for key, cart_item in cart_items_by_key.items():
        item = existing_by_key.get(key)
        if item:
            apply_delivery_collection_entry_to_item(item, delivery_entry)
            item._delivery_order = delivery_order.get(item.delivery_id, cart_item.get("source_delivery_order", 0))
            result.append(item)
            result_keys.add(key)
            continue

        item = build_delivery_collection_item_from_cart(delivery_entry, cart_item)
        item._delivery_order = cart_item.get("source_delivery_order", 0)
        try:
            with session.begin_nested():
                session.add(item)
                session.flush()
        except IntegrityError:
            item = find_delivery_collection_items_by_keys(session, [key]).get(key)
            if not item:
                raise
            apply_delivery_collection_entry_to_item(item, delivery_entry)
            item._delivery_order = delivery_order.get(item.delivery_id, cart_item.get("source_delivery_order", 0))
        result.append(item)
        result_keys.add(key)

    for key, item in existing_by_key.items():
        if key in result_keys:
            continue
        if item.delivery_id not in delivery_ids:
            continue
        apply_delivery_collection_entry_to_item(item, delivery_entry)
        item._delivery_order = delivery_order.get(item.delivery_id, 0)
        result.append(item)

    session.flush()
    return sort_delivery_collection_items(result)


def delivery_collection_cart_item_key(cart_item):
    return (
        int(cart_item["reservation_id"]),
        int(cart_item.get("unit_index", 0) or 0),
    )


def find_delivery_collection_items_by_keys(session, keys):
    if not keys:
        return {}

    rows = session.query(DeliveryCollectionItem).filter(
        tuple_(
            DeliveryCollectionItem.reservation_id,
            DeliveryCollectionItem.unit_index,
        ).in_(keys)
    ).all()
    return {
        (int(item.reservation_id), int(item.unit_index or 0)): item
        for item in rows
    }


def apply_delivery_collection_entry_to_item(item, delivery_entry):
    item.collector_user_id = delivery_entry.collector_user_id
    item.collector_name = delivery_entry.collector_name
    return item


def build_delivery_collection_item_from_cart(delivery_entry, cart_item):
    return DeliveryCollectionItem(
        delivery_id=cart_item.get("source_delivery_id") or delivery_entry.id,
        reservation_id=cart_item["reservation_id"],
        unit_index=cart_item.get("unit_index", 0),
        post_id=cart_item["post_id"],
        user_id=cart_item["user_id"],
        user_name=cart_item.get("user_name"),
        phone=cart_item.get("phone") or delivery_entry.phone,
        item_description=cart_item["description"],
        photo=cart_item.get("photo"),
        quantity=cart_item.get("quantity") or 1,
        amount=cart_item.get("total_price") or 0,
        delivery_address=cart_item.get("address") or delivery_entry.address or "",
        collector_user_id=delivery_entry.collector_user_id,
        collector_name=delivery_entry.collector_name,
        created_at=now_local(),
    )


def build_delivery_collection_item_caption(item):
    lines = [
        f"Id товара: {item.post_id}",
        f"Описание: {item.item_description}",
        f"Цена: {item.amount} ₽",
    ]
    if item.quantity and int(item.quantity) != 1:
        lines.append(f"Количество: {item.quantity}")
    lines.append(f"Клиент: {item.user_name or 'Имя не указано'}")
    lines.append(f"Телефон: {item.phone or 'Телефон не указан'}")
    if item.placed_at:
        lines.append(f"✅ Положено: {format_datetime(item.placed_at)}")
    if item.sheet_added_at:
        lines.append(
            f"Google Sheets: {item.sheet_amount_before or 0} ₽ -> {item.sheet_amount_after or 0} ₽"
        )
    elif item.sheet_error:
        lines.append(f"⚠️ Google Sheets не обновлен: {item.sheet_error}")
    return "\n".join(lines)


def build_delivery_collection_item_keyboard(item):
    if item.placed_at:
        return None

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Положил", callback_data=f"delivery_item_placed_{item.id}"))
    return keyboard


def edit_delivery_collection_item_message(item):
    if not item.message_chat_id or not item.message_id:
        return False

    caption = build_delivery_collection_item_caption(item)
    markup = build_delivery_collection_item_keyboard(item)
    if item.message_has_photo:
        edited_caption = safe_edit_message_caption(
            bot,
            logger=logger,
            chat_id=item.message_chat_id,
            message_id=item.message_id,
            caption=caption,
            reply_markup=markup,
        )
        if edited_caption is not None:
            return True
        logger.debug("Delivery collection caption edit fallback for item_id=%s", item.id)

    return safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=item.message_chat_id,
        message_id=item.message_id,
        text=caption,
        reply_markup=markup,
    ) is not None


def save_delivery_collection_item_message(session, item, sent_message, message_has_photo):
    item.message_chat_id = getattr(getattr(sent_message, "chat", None), "id", None)
    item.message_id = getattr(sent_message, "message_id", None)
    item.message_has_photo = bool(message_has_photo)
    session.flush()


def send_delivery_collection_item_message(chat_id, item):
    caption = build_delivery_collection_item_caption(item)
    markup = build_delivery_collection_item_keyboard(item)
    message_has_photo = bool(item.photo and len(caption) <= 1024)
    try:
        sent_message = send_photo_or_text(bot, chat_id, item.photo, caption, reply_markup=markup)
        return sent_message, message_has_photo
    except Exception as exc:
        logger.warning(
            "Delivery collection item primary send failed item_id=%s post_id=%s, falling back to text: %s",
            getattr(item, "id", None),
            getattr(item, "post_id", None),
            exc,
        )
        sent_message = safe_send_message(
            bot,
            chat_id,
            caption,
            reply_markup=markup,
            logger=logger,
        )
        if sent_message:
            return sent_message, False
        raise


def acquire_delivery_sheet_db_lock():
    if engine.dialect.name != "postgresql":
        return None

    connection = engine.connect()
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": DELIVERY_SHEET_LOCK_KEY},
        )
    except Exception:
        connection.close()
        raise
    return connection


def release_delivery_sheet_db_lock(connection):
    if connection is None:
        return

    try:
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": DELIVERY_SHEET_LOCK_KEY},
        )
    finally:
        connection.close()


def update_delivery_collection_items_collector(session, delivery_entry, reset_sheet=False):
    delivery_ids = get_delivery_collection_entry_ids(session, delivery_entry)
    if not delivery_ids:
        return

    values = {
        DeliveryCollectionItem.collector_user_id: delivery_entry.collector_user_id,
        DeliveryCollectionItem.collector_name: delivery_entry.collector_name,
    }
    if reset_sheet:
        values.update({
            DeliveryCollectionItem.collector_sheet_added_at: None,
            DeliveryCollectionItem.collector_sheet_row: None,
            DeliveryCollectionItem.collector_sheet_error: None,
        })

    session.query(DeliveryCollectionItem).filter(
        DeliveryCollectionItem.delivery_id.in_(delivery_ids),
    ).update(values, synchronize_session=False)


def mark_delivery_collection_items_collector_sheet_result(session, delivery_id, result=None, sheet_error=None):
    if result:
        values = {
            DeliveryCollectionItem.collector_sheet_added_at: now_local(),
            DeliveryCollectionItem.collector_sheet_row: result["row"],
            DeliveryCollectionItem.collector_sheet_error: None,
        }
    else:
        values = {
            DeliveryCollectionItem.collector_sheet_error: sheet_error,
        }

    session.query(DeliveryCollectionItem).filter(
        DeliveryCollectionItem.delivery_id == delivery_id,
        DeliveryCollectionItem.collector_name != None,
    ).update(values, synchronize_session=False)


def add_delivery_collector_name_to_sheet(delivery_id):
    with Session(bind=engine, expire_on_commit=False) as session:
        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == delivery_id,
        ).with_for_update().first()
        if delivery_entry:
            if delivery_entry.collector_sheet_added_at:
                result = {"row": delivery_entry.collector_sheet_row}
                mark_delivery_collection_items_collector_sheet_result(session, delivery_id, result=result)
                session.commit()
                return True, delivery_entry
            source_id = None
            phone = delivery_entry.phone
            collector_name = delivery_entry.collector_name
            source = delivery_entry
        else:
            item = session.query(DeliveryCollectionItem).filter(
                DeliveryCollectionItem.delivery_id == delivery_id,
                DeliveryCollectionItem.collector_name != None,
                DeliveryCollectionItem.collector_sheet_added_at == None,
            ).order_by(
                DeliveryCollectionItem.created_at.asc(),
                DeliveryCollectionItem.id.asc(),
            ).with_for_update().first()
            if not item:
                return True, None
            source_id = item.id
            phone = item.phone
            collector_name = item.collector_name
            source = item
        if not collector_name:
            return False, source

    db_lock_connection = None
    try:
        with delivery_sheet_sync_lock:
            db_lock_connection = acquire_delivery_sheet_db_lock()
            result = write_delivery_collector_name(phone, collector_name)
        sheet_error = None
    except DeliverySheetError as exc:
        result = None
        sheet_error = str(exc)
    except Exception as exc:
        logger.exception("Unexpected delivery collector Sheets update failed delivery_id=%s", delivery_id)
        result = None
        sheet_error = f"Ошибка Google Sheets: {exc}"
    finally:
        release_delivery_sheet_db_lock(db_lock_connection)

    with Session(bind=engine, expire_on_commit=False) as session:
        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == delivery_id,
        ).with_for_update().first()
        if result:
            if delivery_entry:
                delivery_entry.collector_sheet_added_at = now_local()
                delivery_entry.collector_sheet_row = result["row"]
                delivery_entry.collector_sheet_error = None
            mark_delivery_collection_items_collector_sheet_result(session, delivery_id, result=result)
            ok = True
        else:
            if delivery_entry:
                delivery_entry.collector_sheet_error = sheet_error
            mark_delivery_collection_items_collector_sheet_result(session, delivery_id, sheet_error=sheet_error)
            logger.warning("Delivery collector Sheets update failed delivery_id=%s: %s", delivery_id, sheet_error)
            ok = False
        session.commit()
        if delivery_entry:
            session.refresh(delivery_entry)
            return ok, delivery_entry
        item = session.query(DeliveryCollectionItem).filter(
            DeliveryCollectionItem.id == source_id,
        ).first()
        if item:
            session.refresh(item)
        return ok, item


def get_pending_delivery_collector_sheet_delivery_ids(limit=DELIVERY_SHEET_RETRY_BATCH_SIZE):
    with Session(bind=engine) as session:
        rows = session.query(ForDelivery.id).filter(
            ForDelivery.collector_name != None,
            ForDelivery.collector_sheet_added_at == None,
        ).order_by(
            ForDelivery.collection_started_at.asc(),
            ForDelivery.id.asc(),
        ).limit(limit).all()
        return [row[0] for row in rows]


def get_pending_delivery_collector_sheet_item_ids(limit=DELIVERY_SHEET_RETRY_BATCH_SIZE, exclude_delivery_ids=None):
    with Session(bind=engine) as session:
        query = session.query(func.min(DeliveryCollectionItem.id)).filter(
            DeliveryCollectionItem.collector_name != None,
            DeliveryCollectionItem.collector_sheet_added_at == None,
        )
        if exclude_delivery_ids:
            query = query.filter(DeliveryCollectionItem.delivery_id.notin_(exclude_delivery_ids))
        rows = query.group_by(
            DeliveryCollectionItem.delivery_id,
        ).order_by(
            func.min(DeliveryCollectionItem.created_at).asc(),
            func.min(DeliveryCollectionItem.id).asc(),
        ).limit(limit).all()
        return [row[0] for row in rows]


def build_delivery_sheet_operation_id(item_id):
    return f"delivery_item_{int(item_id)}"


def add_collection_item_amount_to_sheet(item_id):
    with Session(bind=engine, expire_on_commit=False) as session:
        item = session.query(DeliveryCollectionItem).filter(
            DeliveryCollectionItem.id == item_id,
        ).with_for_update().first()
        if not item or item.sheet_added_at:
            return True, item
        if not item.placed_at:
            return False, item
        phone = item.phone
        amount = item.amount

    db_lock_connection = None
    try:
        with delivery_sheet_sync_lock:
            db_lock_connection = acquire_delivery_sheet_db_lock()
            result = add_delivery_amount(
                phone,
                amount,
                operation_id=build_delivery_sheet_operation_id(item_id),
            )
        sheet_error = None
    except DeliverySheetError as exc:
        result = None
        sheet_error = str(exc)
    except Exception as exc:
        logger.exception("Unexpected delivery item Sheets update failed item_id=%s", item_id)
        result = None
        sheet_error = f"Ошибка Google Sheets: {exc}"
    finally:
        release_delivery_sheet_db_lock(db_lock_connection)

    with Session(bind=engine, expire_on_commit=False) as session:
        item = session.query(DeliveryCollectionItem).filter(
            DeliveryCollectionItem.id == item_id,
        ).with_for_update().first()
        if not item:
            return False, None
        if result:
            item.sheet_added_at = now_local()
            item.sheet_row = result["row"]
            item.sheet_amount_before = result["amount_before"]
            item.sheet_amount_after = result["amount_after"]
            item.sheet_error = None
            ok = True
            if result.get("already_added"):
                logger.info("Delivery Sheets item already recorded item_id=%s row=%s", item_id, result["row"])
        else:
            item.sheet_error = sheet_error
            logger.warning("Delivery Sheets update failed item_id=%s: %s", item_id, sheet_error)
            ok = False
        session.commit()
        session.refresh(item)
        return ok, item


def get_pending_delivery_sheet_item_ids(limit=DELIVERY_SHEET_RETRY_BATCH_SIZE):
    with Session(bind=engine) as session:
        rows = session.query(DeliveryCollectionItem.id).filter(
            DeliveryCollectionItem.placed_at != None,
            DeliveryCollectionItem.sheet_added_at == None,
        ).order_by(
            DeliveryCollectionItem.placed_at.asc(),
            DeliveryCollectionItem.id.asc(),
        ).limit(limit).all()
        return [row[0] for row in rows]


def retry_pending_delivery_sheet_items():
    delivery_ids = get_pending_delivery_collector_sheet_delivery_ids()
    collector_item_ids = get_pending_delivery_collector_sheet_item_ids(exclude_delivery_ids=delivery_ids)
    item_ids = get_pending_delivery_sheet_item_ids()
    if not delivery_ids and not collector_item_ids and not item_ids:
        return 0, 0

    collector_success_count = 0
    collector_failed_count = 0
    for delivery_id in delivery_ids:
        ok, _delivery_entry = add_delivery_collector_name_to_sheet(delivery_id)
        if ok:
            collector_success_count += 1
        else:
            collector_failed_count += 1
    for item_id in collector_item_ids:
        with Session(bind=engine) as session:
            delivery_id = session.query(DeliveryCollectionItem.delivery_id).filter(
                DeliveryCollectionItem.id == item_id,
            ).scalar()
        if delivery_id is None:
            continue
        ok, _source = add_delivery_collector_name_to_sheet(delivery_id)
        if ok:
            collector_success_count += 1
        else:
            collector_failed_count += 1

    success_count = 0
    failed_count = 0
    for item_id in item_ids:
        ok, _item = add_collection_item_amount_to_sheet(item_id)
        if ok:
            success_count += 1
        else:
            failed_count += 1

    logger.info(
        "Delivery Sheets retry finished: collector_pending=%s collector_success=%s collector_failed=%s item_pending=%s item_success=%s item_failed=%s",
        len(delivery_ids) + len(collector_item_ids),
        collector_success_count,
        collector_failed_count,
        len(item_ids),
        success_count,
        failed_count,
    )
    return collector_success_count + success_count, collector_failed_count + failed_count


def delivery_sheet_retry_loop():
    while True:
        if delivery_sheet_retry_stop_event.wait(DELIVERY_SHEET_RETRY_SECONDS):
            return

        try:
            retry_pending_delivery_sheet_items()
        except Exception:
            logger.exception("Delivery Sheets retry loop failed")


def start_delivery_sheet_retry_worker():
    global delivery_sheet_retry_started
    if delivery_sheet_retry_started:
        return

    delivery_sheet_retry_started = True
    thread = threading.Thread(
        target=delivery_sheet_retry_loop,
        name="delivery-sheet-retry",
        daemon=True,
    )
    thread.start()


def get_delivery_collector_name(session, telegram_user):
    user_id = getattr(telegram_user, "id", None)
    if user_id:
        client = session.query(Clients).filter(Clients.user_id == user_id).first()
        if client and client.name:
            return client.name

    first_name = (getattr(telegram_user, "first_name", "") or "").strip()
    last_name = (getattr(telegram_user, "last_name", "") or "").strip()
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    if full_name:
        return full_name

    username = (getattr(telegram_user, "username", "") or "").strip()
    if username:
        return f"@{username}"

    return f"ID {user_id}"


def claim_delivery_collection_entry(session, delivery_entry, telegram_user):
    collector_user_id = getattr(telegram_user, "id", None)
    if not collector_user_id:
        return False, "неизвестный пользователь"

    delivery_entry = get_delivery_root_entry(session, delivery_entry, lock=True)
    delivery_entries = get_delivery_collection_entries(session, delivery_entry, lock=True)
    for entry in delivery_entries:
        if entry.collector_user_id and int(entry.collector_user_id) != int(collector_user_id):
            return False, entry.collector_name or f"ID {entry.collector_user_id}"

    collector_name = get_delivery_collector_name(session, telegram_user)
    collector_changed = any(entry.collector_name != collector_name for entry in delivery_entries)

    for entry in delivery_entries:
        if collector_changed:
            entry.collector_sheet_added_at = None
            entry.collector_sheet_row = None
            entry.collector_sheet_error = None
        entry.collector_user_id = collector_user_id
        entry.collector_name = collector_name
        entry.collection_started_at = entry.collection_started_at or now_local()
    update_delivery_collection_items_collector(session, delivery_entry, reset_sheet=collector_changed)
    session.flush()
    return True, collector_name


def release_delivery_collection_entry(session, delivery_entry, telegram_user):
    collector_user_id = getattr(telegram_user, "id", None)
    if not collector_user_id or not delivery_entry.collector_user_id:
        return True, None

    delivery_entry = get_delivery_root_entry(session, delivery_entry, lock=True)
    delivery_entries = get_delivery_collection_entries(session, delivery_entry, lock=True)
    for entry in delivery_entries:
        if entry.collector_user_id and int(entry.collector_user_id) != int(collector_user_id):
            return False, entry.collector_name or f"ID {entry.collector_user_id}"

    for entry in delivery_entries:
        entry.collector_user_id = None
        entry.collector_name = None
        entry.collection_started_at = None
        entry.collector_sheet_added_at = None
        entry.collector_sheet_row = None
        entry.collector_sheet_error = None
    update_delivery_collection_items_collector(session, delivery_entry, reset_sheet=True)
    session.flush()
    return True, None


def parse_delivery_collection_callback(data, prefix):
    text = str(data or "")
    if not text.startswith(prefix):
        raise ValueError(f"Bad delivery collection callback: {data}")

    tail = text[len(prefix):]
    parts = tail.split("_") if tail else []
    if len(parts) not in {1, 2} or not parts[0].isdigit():
        raise ValueError(f"Bad delivery collection callback: {data}")
    if len(parts) == 2 and not parts[1].isdigit():
        raise ValueError(f"Bad delivery collection callback: {data}")

    delivery_id = int(parts[0])
    page = int(parts[1]) if len(parts) == 2 else 0
    return delivery_id, page


def calculate_delivery_entry_actual_total(session, delivery_entry):
    delivery_ids = get_delivery_collection_entry_ids(session, delivery_entry)
    existing_items = session.query(DeliveryCollectionItem).filter(
        DeliveryCollectionItem.delivery_id.in_(delivery_ids or [delivery_entry.id]),
    ).all()
    if existing_items:
        return sum(max(int(item.amount or 0), 0) for item in existing_items)

    return sum(max(int(item.get("total_price") or 0), 0) for item in get_delivery_entry_cart_items(session, delivery_entry))


def sync_delivery_entry_total_sum(session, delivery_entry):
    actual_total = calculate_delivery_entry_actual_total(session, delivery_entry)
    if delivery_entry.total_sum != actual_total:
        logger.warning(
            "Delivery total_sum corrected for delivery_id=%s phone=%s old_total=%s actual_total=%s cutoff=%s",
            delivery_entry.id,
            delivery_entry.phone,
            delivery_entry.total_sum,
            actual_total,
            get_delivery_cutoff_at(delivery_entry),
        )
        delivery_entry.total_sum = actual_total
    return actual_total


def build_delivery_collection_button_text(row):
    base_text = f"{row.name} | {row.phone} | {row.total_sum} ₽"
    if not row.collector_name:
        return base_text[:64]

    suffix = f" 👷 {row.collector_name}"
    max_base_length = max(0, 64 - len(suffix))
    return f"{base_text[:max_base_length]}{suffix}"


def build_delivery_collection_keyboard(session, page=0):
    deduplicate_for_delivery_entries(session)
    session.flush()
    total_count = session.query(ForDelivery).count()
    rows = session.query(ForDelivery).order_by(
        ForDelivery.name,
        ForDelivery.phone,
        ForDelivery.id,
    ).offset(page * DELIVERY_COLLECTION_PAGE_SIZE).limit(DELIVERY_COLLECTION_PAGE_SIZE).all()
    for row in rows:
        sync_delivery_entry_total_sum(session, row)
    session.flush()

    keyboard = InlineKeyboardMarkup()
    for row in rows:
        button_text = build_delivery_collection_button_text(row)
        keyboard.add(InlineKeyboardButton(button_text, callback_data=f"collect_delivery_{row.id}_{page}"))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"collect_delivery_page_{page - 1}"))
    if (page + 1) * DELIVERY_COLLECTION_PAGE_SIZE < total_count:
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"collect_delivery_page_{page + 1}"))
    if nav_buttons:
        keyboard.row(*nav_buttons)

    return keyboard, total_count


def show_delivery_collection_list(chat_id, message_id=None, page=0):
    with Session(bind=engine) as session:
        keyboard, total_count = build_delivery_collection_keyboard(session, page)
        session.commit()

    if total_count == 0:
        text = "Список доставки пуст. Пока никто не согласился на доставку и не добавлен вручную."
        if message_id:
            edited_message = safe_edit_message_text(bot, logger=logger, chat_id=chat_id, message_id=message_id, text=text)
            if edited_message is not None:
                return
        safe_send_message(bot, chat_id, text, logger=logger)
        return

    text = f"Клиенты для сборки доставки: {total_count}\nВыберите клиента, чтобы посмотреть корзину."
    if message_id:
        edited_message = safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )
        if edited_message is not None:
            return
    safe_send_message(bot, chat_id, text, logger=logger, reply_markup=keyboard)


@bot.message_handler(func=lambda message: message.text == "🧺 Собрать доставку")
def collect_delivery(message):
    if not require_role(message, DELIVERY_COLLECTION_ROLES):
        return
    auto_fulfill_expired_reservations()
    started_snapshot, snapshot_count = start_delivery_reserved_group_pause_and_snapshot()
    if started_snapshot:
        safe_send_message(
            bot,
            message.chat.id,
            f"Поток забронированного товара в группе временно остановлен. "
            f"В группу отправлено товаров для доставки: {snapshot_count}.",
            logger=logger,
        )
    elif start_reserved_group_resume_flush_if_delivery_done(message.chat.id):
        safe_send_message(
            bot,
            message.chat.id,
            "Список доставки пуст. Повторно запускаю возобновление потока забронированного товара.",
            logger=logger,
        )
    show_delivery_collection_list(message.chat.id)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "collect_delivery_page_"))
def paginate_delivery_collection(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    page = parse_callback_int_suffix(call, "collect_delivery_page_", "Некорректная страница.")
    if page is None:
        return
    safe_answer_callback_query(call.id)
    show_delivery_collection_list(call.message.chat.id, call.message.message_id, page)


@bot.callback_query_handler(
    func=lambda call: callback_data_startswith(call, ("collect_delivery_release_", "collect_release_"))
)
def release_delivery_collection_client(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    try:
        callback_prefix = (
            "collect_release_"
            if callback_data_startswith(call, "collect_release_")
            else "collect_delivery_release_"
        )
        delivery_id, page = parse_delivery_collection_callback(call.data, callback_prefix)
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return

    with Session(bind=engine) as session:
        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == delivery_id,
        ).with_for_update().first()
        if delivery_entry:
            released, collector_name = release_delivery_collection_entry(session, delivery_entry, call.from_user)
            session.commit()
        else:
            released, collector_name = True, None

    if released:
        safe_answer_callback_query(call.id)
    else:
        safe_answer_callback_query(
            call.id,
            f"Эту корзину сейчас собирает {collector_name}.",
            show_alert=True,
        )
    show_delivery_collection_list(call.message.chat.id, call.message.message_id, page)


@bot.callback_query_handler(
    func=lambda call: callback_data_startswith(call, "collect_delivery_")
    and not callback_data_startswith(call, "collect_delivery_page_")
    and not callback_data_startswith(call, "collect_delivery_release_")
    and not callback_data_startswith(call, "collect_release_")
)
def show_delivery_collection_client(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    try:
        delivery_id, page = parse_delivery_collection_callback(call.data, "collect_delivery_")
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return

    with Session(bind=engine, expire_on_commit=False) as session:
        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == delivery_id,
        ).with_for_update().first()
        if not delivery_entry:
            safe_answer_callback_query(call.id, "Клиент уже обработан или удалён из списка.", show_alert=True)
            return

        claimed, collector_name = claim_delivery_collection_entry(session, delivery_entry, call.from_user)
        if not claimed:
            session.commit()
            safe_answer_callback_query(
                call.id,
                f"Эту корзину сейчас собирает {collector_name}.",
                show_alert=True,
            )
            show_delivery_collection_list(call.message.chat.id, call.message.message_id, page)
            return

        items = get_or_create_delivery_collection_items(session, delivery_entry)
        if not items:
            release_delivery_collection_entry(session, delivery_entry, call.from_user)
            session.commit()
            safe_answer_callback_query(call.id)
            show_delivery_collection_list(call.message.chat.id, call.message.message_id, page)
            safe_send_message(bot, call.message.chat.id, "У клиента нет обработанных товаров в корзине.", logger=logger)
            return

        actual_total_sum = sum(max(int(item.amount or 0), 0) for item in items)
        if delivery_entry.total_sum != actual_total_sum:
            logger.warning(
                "Delivery opened with stale total_sum delivery_id=%s phone=%s old_total=%s actual_total=%s",
                delivery_entry.id,
                delivery_entry.phone,
                delivery_entry.total_sum,
                actual_total_sum,
            )
            delivery_entry.total_sum = actual_total_sum
        header = (
            f"🧺 Сборка доставки\n"
            f"Клиент: {delivery_entry.name}\n"
            f"Телефон: {delivery_entry.phone}\n"
            f"Сумма: {actual_total_sum} ₽\n"
            f"Адрес: {delivery_entry.address or 'адрес не указан'}\n"
            f"Собирает: {collector_name}\n"
            f"Срез доставки: {format_datetime(get_delivery_cutoff_at(delivery_entry))}"
        )
        report_context = build_delivery_collection_report_context(session, delivery_entry)
        report_items = build_delivery_collection_report_items_from_collection_items(items)
        session.commit()

    safe_answer_callback_query(call.id)
    collector_sheet_ok, _delivery_entry = add_delivery_collector_name_to_sheet(delivery_id)
    if not collector_sheet_ok:
        logger.warning("Delivery collector name queued for Sheets retry delivery_id=%s", delivery_id)

    show_delivery_collection_list(call.message.chat.id, call.message.message_id, page)
    safe_send_message(bot, call.message.chat.id, header, logger=logger)

    for item in items:
        try:
            sent_message, message_has_photo = send_delivery_collection_item_message(call.message.chat.id, item)
            with Session(bind=engine) as session:
                stored_item = session.query(DeliveryCollectionItem).filter(
                    DeliveryCollectionItem.id == item.id,
                ).first()
                if stored_item:
                    save_delivery_collection_item_message(session, stored_item, sent_message, message_has_photo)
                    session.commit()
            time.sleep(0.3)
        except Exception as e:
            logger.warning(
                "Delivery collection item send failed for delivery_id=%s item_id=%s post_id=%s: %s",
                delivery_id,
                item.id,
                item.post_id,
                e,
            )
            safe_send_message(
                bot,
                call.message.chat.id,
                f"{build_delivery_collection_item_caption(item)}\n\nНе удалось отправить товар. Подробности записаны в лог.",
                logger=logger,
            )

    total_processed_sum = sum(item.amount for item in items)
    safe_send_message(
        bot,
        call.message.chat.id,
        f"Общая сумма обработанных заказов: {total_processed_sum} ₽",
        logger=logger,
    )

    safe_send_message(
        bot,
        call.message.chat.id,
        "Нажимайте «Положил» на каждом найденном товаре. Когда закончите, нажмите «Закончить сборку».",
        logger=logger,
        reply_markup=build_delivery_collection_finish_keyboard(delivery_id, page),
    )
    start_delivery_collection_report_to_group(report_context, report_items)


def build_delivery_collection_finish_keyboard(delivery_id, page=0):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🏁 Закончить сборку", callback_data=f"delivery_collected_{delivery_id}"))
    keyboard.add(InlineKeyboardButton("⬅️ К списку", callback_data=f"collect_release_{delivery_id}_{page}"))
    return keyboard


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "delivery_item_placed_"))
def mark_delivery_collection_item_placed(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    item_id = parse_callback_int_suffix(call, "delivery_item_placed_")
    if item_id is None:
        return
    already_placed = False
    with Session(bind=engine) as session:
        item = session.query(DeliveryCollectionItem).filter(
            DeliveryCollectionItem.id == item_id,
        ).with_for_update().first()
        if not item:
            safe_answer_callback_query(call.id, "Товар уже обработан или не найден.", show_alert=True)
            return

        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == item.delivery_id,
        ).with_for_update().first()
        if not delivery_entry:
            safe_answer_callback_query(call.id, "Сборка уже завершена или удалена.", show_alert=True)
            return
        if delivery_entry.collector_user_id and int(delivery_entry.collector_user_id) != int(call.from_user.id):
            collector_name = delivery_entry.collector_name or f"ID {delivery_entry.collector_user_id}"
            safe_answer_callback_query(call.id, f"Эту корзину сейчас собирает {collector_name}.", show_alert=True)
            return

        if item.placed_at:
            already_placed = True
        else:
            item.placed_at = now_local()
            item.collector_user_id = call.from_user.id
            item.collector_name = get_delivery_collector_name(session, call.from_user)
            session.commit()

    if already_placed:
        safe_answer_callback_query(call.id, "Этот товар уже отмечен.")
        return

    safe_answer_callback_query(call.id, "Положено. Записываю сумму в Google Sheets.")
    sheet_ok, item = add_collection_item_amount_to_sheet(item_id)
    if item:
        edit_delivery_collection_item_message(item)
        edit_delivery_collection_report_item_message(item)

    if not sheet_ok:
        logger.warning("Delivery item marked placed but Sheets update is pending item_id=%s", item_id)


def move_for_delivery_to_in_delivery(delivery_id, manual_total_sum=None, collector_user_id=None):
    with Session(bind=engine) as session:
        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == delivery_id,
        ).with_for_update().first()
        if not delivery_entry:
            return False, "Клиент уже обработан или удалён из списка.", 0

        if (
            collector_user_id
            and delivery_entry.collector_user_id
            and int(delivery_entry.collector_user_id) != int(collector_user_id)
        ):
            collector_name = delivery_entry.collector_name or f"ID {delivery_entry.collector_user_id}"
            return False, f"Эту корзину сейчас собирает {collector_name}.", 0

        related_user_ids = get_related_delivery_user_ids(session, delivery_entry)
        cutoff_at = get_delivery_cutoff_at(delivery_entry)
        reservations = get_delivery_reservations_query(
            session,
            related_user_ids,
            cutoff_at,
        ).all()

        if not reservations:
            return False, "У клиента нет обработанных товаров в срезе этой доставки.", 0

        posts_by_id = get_posts_or_snapshots_by_ids(
            session,
            {reservation.post_id for reservation in reservations},
        )

        clients_by_user_id = {
            client.user_id: client
            for client in session.query(Clients).filter(
                Clients.user_id.in_({reservation.user_id for reservation in reservations})
            ).all()
        }

        rows_payload = []
        problematic_count = 0
        for reservation in reservations:
            post = posts_by_id.get(reservation.post_id)
            if not post:
                problematic_count += 1
            temp_item = get_temp_fulfilled_for_reservation(session, reservation)
            reservation_client = clients_by_user_id.get(reservation.user_id)
            rows_payload.append({
                "reservation": reservation,
                "post_id": reservation.post_id,
                "user_id": reservation.user_id,
                "user_name": reservation_client.name if reservation_client else delivery_entry.name,
                "item_description": build_delivery_row_description(reservation, post=post, temp_item=temp_item),
                "quantity": reservation.quantity,
                "base_amount": calculate_delivery_row_amount(reservation, post=post, temp_item=temp_item),
                "temp_item": temp_item,
            })

        allocated_amounts = allocate_delivery_amounts(rows_payload, manual_total_sum=manual_total_sum)
        moved_count = 0
        for row, allocated_amount in zip(rows_payload, allocated_amounts):
            reservation = row["reservation"]
            session.add(InDelivery(
                reservation_id=reservation.id,
                post_id=row["post_id"],
                user_id=row["user_id"],
                user_name=row["user_name"],
                item_description=row["item_description"],
                quantity=row["quantity"],
                price=allocated_amount,
                delivery_address=delivery_entry.address or "",
            ))

            temp_item = row["temp_item"]
            if temp_item:
                temp_item.in_delivery = True

            session.delete(reservation)
            moved_count += 1

        session.delete(delivery_entry)
        session.commit()
        message_parts = ["Клиент перенесён в доставку."]
        if manual_total_sum is not None:
            message_parts.append(f"Итоговая сумма: {manual_total_sum} ₽.")
        if problematic_count:
            message_parts.append(f"Строк без карточек товаров перенесено: {problematic_count}.")
        return True, " ".join(message_parts), moved_count


def build_unplaced_delivery_items_text(items):
    lines = [
        "Не отмечены как найденные товары:",
        "",
    ]
    for index, item in enumerate(items[:10], start=1):
        lines.append(f"{index}. Id {item.post_id} | {item.item_description} | {item.amount} ₽")
    if len(items) > 10:
        lines.append(f"...и ещё {len(items) - 10}")
    lines.append("")
    lines.append("Закончить сборку и отправить эти товары в список пропажи?")
    return "\n".join(lines)


def delivery_collection_items_quantity(items):
    return sum(max(int(item.quantity or 0), 1) for item in items)


def delivery_collection_items_amount(items):
    return sum(max(int(item.amount or 0), 0) for item in items)


def mark_temp_fulfilled_in_delivery_for_items(session, reservation, placed_items):
    temp_item = get_temp_fulfilled_for_reservation(session, reservation)
    if not temp_item:
        return False

    temp_item.in_delivery = True
    temp_item.quantity = delivery_collection_items_quantity(placed_items)
    temp_item.price = delivery_collection_items_amount(placed_items)
    return True


def finish_delivery_collection(delivery_id, collector_user_id=None, move_unplaced_to_missing=False):
    affected_missing_post_ids = set()
    with Session(bind=engine) as session:
        delivery_entry = session.query(ForDelivery).filter(
            ForDelivery.id == delivery_id,
        ).with_for_update().first()
        if not delivery_entry:
            return False, "Клиент уже обработан или удалён из списка.", 0, 0

        if (
            collector_user_id
            and delivery_entry.collector_user_id
            and int(delivery_entry.collector_user_id) != int(collector_user_id)
        ):
            collector_name = delivery_entry.collector_name or f"ID {delivery_entry.collector_user_id}"
            return False, f"Эту корзину сейчас собирает {collector_name}.", 0, 0

        items = get_or_create_delivery_collection_items(session, delivery_entry)
        placed_items = [item for item in items if item.placed_at]
        unplaced_items = [item for item in items if not item.placed_at]
        if unplaced_items and not move_unplaced_to_missing:
            return False, build_unplaced_delivery_items_text(unplaced_items), len(placed_items), len(unplaced_items)

        if not placed_items and not unplaced_items:
            return False, "У клиента нет товаров для завершения сборки.", 0, 0

        reservation_ids = {item.reservation_id for item in items}
        reservations_by_id = {
            reservation.id: reservation
            for reservation in session.query(Reservations).filter(
                Reservations.id.in_(reservation_ids),
            ).with_for_update().all()
        }

        placed_by_reservation = {}
        unplaced_by_reservation = {}
        for item in placed_items:
            placed_by_reservation.setdefault(item.reservation_id, []).append(item)
        for item in unplaced_items:
            unplaced_by_reservation.setdefault(item.reservation_id, []).append(item)

        moved_count = 0
        for item in placed_items:
            if item.reservation_id not in reservations_by_id:
                continue
            session.add(InDelivery(
                reservation_id=item.reservation_id,
                post_id=item.post_id,
                user_id=item.user_id,
                user_name=item.user_name or delivery_entry.name,
                item_description=item.item_description,
                quantity=item.quantity or 1,
                price=item.amount or 0,
                delivery_address=item.delivery_address or delivery_entry.address or "",
            ))
            moved_count += 1

        missing_count = 0
        if move_unplaced_to_missing:
            for item in unplaced_items:
                existing_missing = session.query(MissingDeliveryItem).filter(
                    MissingDeliveryItem.reservation_id == item.reservation_id,
                    MissingDeliveryItem.unit_index == item.unit_index,
                    MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING,
                ).first()
                if not existing_missing:
                    session.add(MissingDeliveryItem(
                        delivery_id=delivery_id,
                        reservation_id=item.reservation_id,
                        unit_index=item.unit_index,
                        post_id=item.post_id,
                        user_id=item.user_id,
                        user_name=item.user_name or delivery_entry.name,
                        phone=item.phone or delivery_entry.phone,
                        delivery_address=item.delivery_address or delivery_entry.address or "",
                        item_description=item.item_description,
                        photo=item.photo,
                        quantity=item.quantity or 1,
                        price=item.amount or 0,
                        collector_user_id=delivery_entry.collector_user_id,
                        collector_name=delivery_entry.collector_name,
                        missing_at=now_local(),
                        status=MISSING_DELIVERY_STATUS_MISSING,
                    ))
                affected_missing_post_ids.add(item.post_id)
                missing_count += 1

        for reservation_id, reservation in reservations_by_id.items():
            placed_for_reservation = placed_by_reservation.get(reservation_id)
            if placed_for_reservation:
                mark_temp_fulfilled_in_delivery_for_items(session, reservation, placed_for_reservation)
            elif unplaced_by_reservation.get(reservation_id):
                delete_temp_fulfilled_for_reservation(session, reservation)
            session.delete(reservation)

        if delivery_entry.collector_name and not delivery_entry.collector_sheet_added_at:
            mark_delivery_collection_items_collector_sheet_result(
                session,
                delivery_id,
                sheet_error=delivery_entry.collector_sheet_error or "Ожидает повторной записи Google Sheets",
            )

        session.delete(delivery_entry)
        session.commit()

    for post_id in affected_missing_post_ids:
        post = Posts.get_row_by_id(post_id)
        if post and int(post.quantity or 0) <= 0:
            disable_channel_post_reservation(post, reason="Товар в списке пропажи")

    message_parts = [f"В доставку перенесено товарных строк: {moved_count}."]
    if missing_count:
        message_parts.append(f"В список пропажи отправлено: {missing_count}.")
    return True, " ".join(message_parts), moved_count, missing_count


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "delivery_collected_"))
def mark_delivery_collected(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    delivery_id = parse_callback_int_suffix(call, "delivery_collected_")
    if delivery_id is None:
        return
    safe_answer_callback_query(call.id)
    success, message_text, moved_count, missing_count = finish_delivery_collection(
        delivery_id,
        collector_user_id=call.from_user.id,
        move_unplaced_to_missing=False,
    )
    if not success and missing_count:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🏁 Закончить", callback_data=f"delivery_finish_confirm_{delivery_id}"))
        keyboard.add(InlineKeyboardButton("⬅️ Продолжить сборку", callback_data=f"delivery_finish_continue_{delivery_id}"))
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=message_text,
            reply_markup=keyboard,
        )
        return

    safe_send_message(bot, call.message.chat.id, f"{'✅' if success else '❌'} {message_text}", logger=logger)
    if success and start_reserved_group_resume_flush_if_delivery_done(call.message.chat.id):
        safe_send_message(
            bot,
            call.message.chat.id,
            "Все корзины доставки собраны. Возобновляю поток забронированного товара в группе.",
            logger=logger,
        )
    show_delivery_collection_list(call.message.chat.id, message_id=call.message.message_id, page=0)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "delivery_finish_confirm_"))
def confirm_finish_delivery_with_missing(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    delivery_id = parse_callback_int_suffix(call, "delivery_finish_confirm_")
    if delivery_id is None:
        return
    safe_answer_callback_query(call.id)
    success, message_text, moved_count, missing_count = finish_delivery_collection(
        delivery_id,
        collector_user_id=call.from_user.id,
        move_unplaced_to_missing=True,
    )
    safe_send_message(bot, call.message.chat.id, f"{'✅' if success else '❌'} {message_text}", logger=logger)
    if success and start_reserved_group_resume_flush_if_delivery_done(call.message.chat.id):
        safe_send_message(
            bot,
            call.message.chat.id,
            "Все корзины доставки собраны. Возобновляю поток забронированного товара в группе.",
            logger=logger,
        )
    show_delivery_collection_list(call.message.chat.id, message_id=call.message.message_id, page=0)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "delivery_finish_continue_"))
def continue_delivery_collection(call):
    if not has_role(call.from_user.id, DELIVERY_COLLECTION_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    delivery_id = parse_callback_int_suffix(call, "delivery_finish_continue_")
    if delivery_id is None:
        return
    safe_answer_callback_query(call.id)

    text = "Нажимайте «Положил» на каждом найденном товаре. Когда закончите, нажмите «Закончить сборку»."
    keyboard = build_delivery_collection_finish_keyboard(delivery_id, page=0)
    edited_message = safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=keyboard,
    )
    if edited_message is None:
        safe_send_message(bot, call.message.chat.id, text, logger=logger, reply_markup=keyboard)


@bot.message_handler(func=lambda message: get_state_action(message.chat.id) == "AWAITING_DELIVERY_COLLECTED_SUM")
def handle_delivery_collected_sum(message):
    if not require_role(message, DELIVERY_COLLECTION_ROLES):
        clear_user_state(message.chat.id)
        return

    state = get_user_state(message.chat.id) or {}
    source_message_id = state.get("source_message_id")
    clear_user_state(message.chat.id)
    safe_send_message(
        bot,
        message.chat.id,
        "Старый способ завершения доставки больше не используется. Откройте «Собрать доставку» и завершите сборку кнопкой.",
        logger=logger,
    )
    show_delivery_collection_list(message.chat.id, message_id=source_message_id, page=0)

@bot.message_handler(func=lambda message: message.text == "✅ Подтвердить доставку")
def confirm_delivery(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    safe_send_message(bot, message.chat.id, "Теперь доставка собирается по клиентам. Открываю список для сборки.", logger=logger)
    show_delivery_collection_list(message.chat.id)


def build_missing_delivery_button_text(item):
    base = f"Id {item.post_id} | {item.user_name or 'Имя не указано'} | {item.phone} | {item.price} ₽"
    return truncate_button_text(base, 64)


def build_missing_delivery_keyboard(session, page=0):
    total_count = session.query(MissingDeliveryItem).filter(
        MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING,
    ).count()
    items = session.query(MissingDeliveryItem).filter(
        MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING,
    ).order_by(
        MissingDeliveryItem.missing_at.desc(),
        MissingDeliveryItem.id.desc(),
    ).offset(page * MISSING_DELIVERY_PAGE_SIZE).limit(MISSING_DELIVERY_PAGE_SIZE).all()

    keyboard = InlineKeyboardMarkup()
    for item in items:
        keyboard.add(InlineKeyboardButton(
            build_missing_delivery_button_text(item),
            callback_data=f"missing_delivery_item_{item.id}_{page}",
        ))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"missing_delivery_page_{page - 1}"))
    if (page + 1) * MISSING_DELIVERY_PAGE_SIZE < total_count:
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"missing_delivery_page_{page + 1}"))
    if nav_buttons:
        keyboard.row(*nav_buttons)

    return keyboard, total_count


def show_missing_delivery_list(chat_id, message_id=None, page=0):
    with Session(bind=engine) as session:
        keyboard, total_count = build_missing_delivery_keyboard(session, page)

    if total_count == 0:
        text = "Список пропажи пуст."
        if message_id:
            edited_message = safe_edit_message_text(bot, logger=logger, chat_id=chat_id, message_id=message_id, text=text)
            if edited_message is not None:
                return
        safe_send_message(bot, chat_id, text, logger=logger)
        return

    text = f"Список пропажи: {total_count}\nВыберите товар."
    if message_id:
        edited_message = safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )
        if edited_message is not None:
            return
    safe_send_message(bot, chat_id, text, logger=logger, reply_markup=keyboard)


def build_missing_delivery_item_text(item):
    status_label = MISSING_DELIVERY_STATUS_LABELS.get(item.status, item.status)
    return "\n".join([
        f"Id товара: {item.post_id}",
        f"Статус: {status_label}",
        f"Клиент: {item.user_name or 'Имя не указано'}",
        f"Телефон: {item.phone or 'Телефон не указан'}",
        f"Адрес: {item.delivery_address or 'адрес не указан'}",
        f"Описание: {item.item_description}",
        f"Количество: {item.quantity or 1}",
        f"Сумма: {item.price or 0} ₽",
        f"Собирал: {item.collector_name or 'не указано'}",
        f"В пропаже с: {format_datetime(item.missing_at)}",
    ])


def show_missing_delivery_item(chat_id, item_id, page=0):
    with Session(bind=engine) as session:
        item = session.query(MissingDeliveryItem).filter(
            MissingDeliveryItem.id == item_id,
        ).first()
        if not item:
            safe_send_message(bot, chat_id, "Товар из списка пропажи не найден.", logger=logger)
            return
        text = build_missing_delivery_item_text(item)
        photo = item.photo

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Нашли", callback_data=f"missing_delivery_found_{item_id}_{page}"))
    keyboard.add(InlineKeyboardButton("⬅️ К списку", callback_data=f"missing_delivery_page_{page}"))
    try:
        send_photo_or_text(bot, chat_id, photo, text, reply_markup=keyboard)
    except Exception as exc:
        logger.warning("Missing delivery item display failed item_id=%s chat_id=%s: %s", item_id, chat_id, exc)
        safe_send_message(bot, chat_id, "Не удалось показать товар из списка пропажи. Попробуйте ещё раз.", logger=logger)


def publish_returned_missing_post_to_channel(post):
    if post.message_id:
        update_result = update_channel_post_message(post)
        if update_result is True:
            return True, post.message_id, None
        if update_result is None:
            return False, post.message_id, "Telegram временно не ответил. Попробуйте вернуть товар на канал позже."
        logger.warning(
            "Returned missing post channel message unavailable post_id=%s message_id=%s; sending a fresh channel post",
            post.id,
            post.message_id,
        )

    try:
        sent_message = send_channel_post_with_retries(
            post,
            build_channel_post_caption(post),
            build_channel_post_markup(post),
            source="missing_return",
        )
    except Exception as exc:
        logger.warning("Returned missing post send failed post_id=%s: %s", post.id, exc)
        return False, None, "Telegram временно не ответил. Попробуйте вернуть товар на канал позже."

    return True, sent_message.message_id, None


def return_missing_item_to_channel(item_id, admin_user_id):
    with Session(bind=engine) as session:
        item = session.query(MissingDeliveryItem).filter(
            MissingDeliveryItem.id == item_id,
        ).with_for_update().first()
        if not item:
            return False, "Товар из списка пропажи не найден."
        if item.status != MISSING_DELIVERY_STATUS_MISSING:
            return False, "Этот товар уже обработан."

        post = session.query(Posts).filter(Posts.id == item.post_id).with_for_update().first()
        if not post:
            ensure_missing_delivery_item_snapshot(
                session,
                item,
                reason="missing_delivery_return_to_channel",
            )
            post = build_post_from_missing_delivery_item(item)
            session.add(post)
            session.flush()

        post.quantity = int(post.quantity or 0) + max(int(item.quantity or 0), 1)
        channel_ok, message_id, error_text = publish_returned_missing_post_to_channel(post)
        if not channel_ok:
            session.rollback()
            return False, error_text

        if message_id:
            post.message_id = message_id
            post.is_sent = True
        item.status = MISSING_DELIVERY_STATUS_RETURNED_CHANNEL
        item.resolved_at = now_local()
        item.resolved_by_user_id = admin_user_id
        session.commit()
    return True, "Товар вернули на канал."


def return_missing_item_to_cart(item_id, admin_user_id):
    with Session(bind=engine) as session:
        item = session.query(MissingDeliveryItem).filter(
            MissingDeliveryItem.id == item_id,
        ).with_for_update().first()
        if not item:
            return False, "Товар из списка пропажи не найден."
        if item.status != MISSING_DELIVERY_STATUS_MISSING:
            return False, "Этот товар уже обработан."

        quantity = max(int(item.quantity or 0), 1)
        unit_price = max(int((item.price or 0) // quantity), 0)
        reservation = Reservations(
            user_id=item.user_id,
            quantity=quantity,
            post_id=item.post_id,
            is_fulfilled=True,
            old_price=unit_price,
            created_at=now_local(),
            fulfilled_at=now_local(),
        )
        session.add(reservation)
        session.flush()
        session.add(Temp_Fulfilled(
            reservation_id=reservation.id,
            post_id=item.post_id,
            user_id=item.user_id,
            user_name=item.user_name or "Имя не указано",
            item_description=item.item_description,
            quantity=quantity,
            price=max(int(item.price or 0), 0),
            created_at=item.missing_at or now_local(),
        ))
        ensure_missing_delivery_item_snapshot(session, item)
        add_reservation_stat_event(session, RESERVATION_STATS_EVENT_CREATED, reservation, event_time=reservation.created_at)
        add_reservation_stat_event(session, RESERVATION_STATS_EVENT_FULFILLED, reservation, event_time=reservation.fulfilled_at)
        item.status = MISSING_DELIVERY_STATUS_RETURNED_CART
        item.resolved_at = now_local()
        item.resolved_by_user_id = admin_user_id
        session.commit()

    return True, "Товар вернули в корзину клиента."


@bot.message_handler(func=lambda message: message.text == "📦 Список пропажи")
def missing_delivery_list_handler(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    show_missing_delivery_list(message.chat.id)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "missing_delivery_page_"))
def paginate_missing_delivery(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    page = parse_callback_int_suffix(call, "missing_delivery_page_", "Некорректная страница.")
    if page is None:
        return
    safe_answer_callback_query(call.id)
    show_missing_delivery_list(call.message.chat.id, call.message.message_id, page=page)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "missing_delivery_item_"))
def show_missing_delivery_item_handler(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    try:
        item_id, page = parse_delivery_collection_callback(call.data, "missing_delivery_item_")
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return
    safe_answer_callback_query(call.id)
    show_missing_delivery_item(call.message.chat.id, item_id, page=page)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "missing_delivery_found_"))
def choose_missing_delivery_return_target(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    try:
        item_id, page = parse_delivery_collection_callback(call.data, "missing_delivery_found_")
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return
    safe_answer_callback_query(call.id)
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📢 Вернуть на канал", callback_data=f"missing_return_channel_{item_id}_{page}"))
    keyboard.add(InlineKeyboardButton("🛒 Вернуть в корзину клиента", callback_data=f"missing_return_cart_{item_id}_{page}"))
    keyboard.add(InlineKeyboardButton("⬅️ К списку", callback_data=f"missing_delivery_page_{page}"))
    safe_send_message(bot, call.message.chat.id, "Куда вернуть найденный товар?", logger=logger, reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "missing_return_channel_"))
def return_missing_delivery_to_channel_handler(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    try:
        item_id, page = parse_delivery_collection_callback(call.data, "missing_return_channel_")
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return
    safe_answer_callback_query(call.id)
    success, text = return_missing_item_to_channel(item_id, call.from_user.id)
    safe_send_message(bot, call.message.chat.id, f"{'✅' if success else '❌'} {text}", logger=logger)
    show_missing_delivery_list(call.message.chat.id, page=page)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "missing_return_cart_"))
def return_missing_delivery_to_cart_handler(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    try:
        item_id, page = parse_delivery_collection_callback(call.data, "missing_return_cart_")
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return
    safe_answer_callback_query(call.id)
    success, text = return_missing_item_to_cart(item_id, call.from_user.id)
    safe_send_message(bot, call.message.chat.id, f"{'✅' if success else '❌'} {text}", logger=logger)
    show_missing_delivery_list(call.message.chat.id, page=page)

@bot.callback_query_handler(
    func=lambda call: call.data == "new_phone"
    and get_user_state(call.from_user.id) != "WAITING_FOR_DATA_EDIT"
)
def handle_new_phone_choice_without_delivery_state(call):
    safe_answer_callback_query(
        call.id,
        "Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
        show_alert=True,
    )


@bot.callback_query_handler(
    func=lambda call: call.data == "edit_address"
    and get_user_state(call.from_user.id) != "WAITING_FOR_DATA_EDIT"
)
def handle_edit_choice(call):
    logger.debug("Edit choice callback received: %s", call.data)
    safe_answer_callback_query(
        call.id,
        "Данные доставки потерялись. Нажмите согласие на доставку и заполните данные заново.",
        show_alert=True,
    )

# Для ревизии
@bot.message_handler(func=lambda message: message.text == "Ревизия")
def audit_menu(message):
    # Создаем клавиатуру
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)

    # Добавляем кнопки
    btn_do_audit = types.KeyboardButton("Сделать ревизию")
    btn_send_audit = types.KeyboardButton("В будущих обновлениях...")
    btn_back = types.KeyboardButton("⬅️ Назад")

    # Добавляем кнопки на клавиатуру
    keyboard.add(btn_do_audit, btn_send_audit, btn_back)

    # Отправляем сообщение с клавиатурой
    safe_send_message(bot, message.chat.id, "Выберите действие:", reply_markup=keyboard, logger=logger)

@bot.message_handler(func=lambda message: message.text == "Сделать ревизию")
def manage_audit_posts(message):
    with Session(bind=engine) as session:
        blocked_post_ids = get_revision_blocked_post_ids(session)
        query = session.query(Posts).filter(
            Posts.quantity > 0,
            Posts.created_at != None,
        )
        if blocked_post_ids:
            query = query.filter(~Posts.id.in_(blocked_post_ids))
        posts = query.all()

    if not posts:
        safe_send_message(bot, message.chat.id, "Нет свободных постов для ревизии.", logger=logger)
        return

    # Уникальные даты по постам
    unique_dates = sorted(list(set(post.created_at.date() for post in posts)))

    if not unique_dates:
        safe_send_message(bot, message.chat.id, "Нет доступных дат для ревизии.", logger=logger)
        return

    # Клавиатура для выбора даты
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for date in unique_dates[:2]:  # Показываем максимум 2 даты
        # Форматируем дату в виде: "число месяц"
        formatted_date = date.strftime("%d %B")
        keyboard.add(types.KeyboardButton(formatted_date))

    keyboard.add(types.KeyboardButton("⬅️ Назад"))
    safe_send_message(bot, message.chat.id, "Выберите дату для ревизии:", reply_markup=keyboard, logger=logger)

    # Сохраняем даты в temp_user_data
    temp_user_data[message.chat.id] = {
        "unique_dates": [date.strftime("%d %B") for date in unique_dates],
        "audit_date_map": {date.strftime("%d %B"): date.isoformat() for date in unique_dates},
    }


def get_revision_blocked_post_ids(session):
    blocked = set()
    sources = [
        session.query(Reservations.post_id),
        session.query(InDelivery.post_id),
        session.query(Temp_Fulfilled.post_id),
        session.query(TempReservations.post_id).filter(
            TempReservations.temp_fulfilled == False,
        ),
        session.query(DeliveryCollectionItem.post_id).join(
            ForDelivery,
            ForDelivery.id == DeliveryCollectionItem.delivery_id,
        ),
        session.query(MissingDeliveryItem.post_id).filter(
            MissingDeliveryItem.status == MISSING_DELIVERY_STATUS_MISSING,
        ),
    ]
    for query in sources:
        blocked.update(post_id for (post_id,) in query.all() if post_id is not None)
    return blocked


def apply_auto_audit_for_date(selected_date, audit_user_id):
    now = Posts.next_created_at()
    selected_start = datetime.combine(selected_date, datetime.min.time())
    selected_end = selected_start + timedelta(days=1)
    summary = {
        "processed_count": 0,
        "blocked_count": 0,
        "no_stock_count": 0,
        "old_total": 0,
        "new_total": 0,
    }

    with Session(bind=engine) as session:
        blocked_post_ids = get_revision_blocked_post_ids(session)
        date_filter = (
            Posts.created_at >= selected_start,
            Posts.created_at < selected_end,
        )
        summary["no_stock_count"] = session.query(Posts.id).filter(
            *date_filter,
            Posts.quantity <= 0,
        ).count()
        if blocked_post_ids:
            summary["blocked_count"] = session.query(Posts.id).filter(
                *date_filter,
                Posts.id.in_(blocked_post_ids),
            ).count()

        selected_query = session.query(Posts).filter(
            *date_filter,
            Posts.quantity > 0,
        )
        if blocked_post_ids:
            selected_query = selected_query.filter(~Posts.id.in_(blocked_post_ids))
        selected_posts = selected_query.order_by(Posts.id).all()
        channel_cleanup = []

        for post in selected_posts:
            old_price = int(post.price or 0)
            new_price = calculate_audit_price(old_price)
            session.add(RevisionLog(
                post_id=post.id,
                auditor_user_id=audit_user_id,
                old_price=old_price,
                new_price=new_price,
                quantity=post.quantity,
                selected_date=selected_start,
                created_at=now,
            ))
            if post.message_id:
                channel_cleanup.append(SimpleNamespace(
                    id=post.id,
                    message_id=post.message_id,
                    photo=post.photo,
                    price=new_price,
                    description=post.description,
                    quantity=post.quantity,
                ))
            post.price = new_price
            post.is_sent = False
            post.message_id = None
            post.created_at = now
            post.chat_id = audit_user_id
            summary["processed_count"] += 1
            summary["old_total"] += old_price
            summary["new_total"] += new_price

        session.commit()

    for snapshot in channel_cleanup:
        delete_delivered_channel_post_message(
            snapshot,
            source="revision",
            disable_reason="Товар на ревизии",
        )

    return summary


def answer_manual_audit_disabled(target):
    message = "Ручная ревизия отключена, используйте автоматическую."
    if hasattr(target, "data") and hasattr(target, "id"):
        safe_answer_callback_query(target.id, message, show_alert=True)
    else:
        safe_send_message(bot, target.chat.id, message, logger=logger)
        clear_user_state(target.chat.id)


@bot.message_handler(
    func=lambda message: message.text in temp_user_data.get(message.chat.id, {}).get("unique_dates", []))
def show_posts_by_date(message):
    selected_date_text = message.text
    audit_data = temp_user_data.get(message.chat.id, {})
    selected_date_iso = audit_data.get("audit_date_map", {}).get(selected_date_text)

    if not selected_date_iso:
        safe_send_message(bot, message.chat.id, "Дата не найдена в базе. Пожалуйста, выберите другую дату.", logger=logger)
        return

    selected_date = datetime.fromisoformat(selected_date_iso).date()
    summary = apply_auto_audit_for_date(selected_date, message.chat.id)

    if (
        summary["processed_count"] == 0
        and summary["blocked_count"] == 0
        and summary["no_stock_count"] == 0
    ):
        safe_send_message(bot, message.chat.id, f"Нет свободных постов за дату {selected_date}.", logger=logger)
        return

    if message.chat.id in temp_user_data:
        del temp_user_data[message.chat.id]
    active_audit[message.chat.id] = False

    safe_send_message(
        bot,
        message.chat.id,
        (
            "✅ Автоматическая ревизия завершена.\n"
            f"Дата: {selected_date.strftime('%d.%m.%Y')}\n"
            f"Обработано постов: {summary['processed_count']}\n"
            f"Исключено из-за корзины/доставки/очереди: {summary['blocked_count']}\n"
            f"Пропущено без остатка: {summary['no_stock_count']}\n"
            f"Старая сумма: {summary['old_total']} ₽\n"
            f"Новая сумма: {summary['new_total']} ₽\n\n"
            "Теперь администратор может нажать «📢 Отправить посты в канал»."
        ),
        reply_markup=types.ReplyKeyboardRemove(),
        logger=logger,
    )

@bot.message_handler(func=lambda message: message.text == "Отменить ревизию")
def cancel_audit(message):
    global active_audit

    # Проверяем, активна ли ревизия
    if not active_audit.get(message.chat.id):
        safe_send_message(bot, message.chat.id, "Нет активной ревизии для отмены.", logger=logger)
        return

    # Завершаем ревизию
    active_audit[message.chat.id] = False
    safe_send_message(bot, message.chat.id, "Ревизия успешно отменена.", reply_markup=types.ReplyKeyboardRemove(), logger=logger)

@bot.callback_query_handler(
    func=lambda call: callback_data_startswith(call, (
        "audit_edit_price_",
        "audit_edit_description_",
        "audit_edit_quantity_",
        "audit_delete_post_",
        "audit_confirm_post_",
    ))
)
def disabled_manual_audit_callback(call):
    answer_manual_audit_disabled(call)


@bot.message_handler(
    func=lambda message: user_state_in(message.chat.id, {
        "EDITING_AUDIT_PRICE",
        "EDITING_AUDIT_DESCRIPTION",
        "EDITING_AUDIT_QUANTITY",
    })
)
def disabled_manual_audit_message(message):
    answer_manual_audit_disabled(message)

@bot.message_handler(func=lambda message: message.text == "😞 У меня брак")
def defect(message):
    user_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return

    show_user_defect_items(user_id, page=0)


def build_user_defect_items_keyboard(user_id, page=0):
    with Session(bind=engine) as session:
        related_user_ids = get_related_user_ids_by_full_phone(user_id)
        if not related_user_ids:
            return InlineKeyboardMarkup(), 0, 0

        query = session.query(Temp_Fulfilled).filter(
            Temp_Fulfilled.user_id.in_(related_user_ids),
            Temp_Fulfilled.in_delivery == True,
            Temp_Fulfilled.defect == False,
            Temp_Fulfilled.skidka == False
        ).order_by(
            Temp_Fulfilled.created_at.desc(),
            Temp_Fulfilled.id.desc(),
        )

        total_count = query.count()
        if total_count == 0:
            return InlineKeyboardMarkup(), 0, 0

        total_pages = max((total_count - 1) // DEFECT_ITEM_PAGE_SIZE + 1, 1)
        page = min(max(int(page or 0), 0), total_pages - 1)
        user_items = query.offset(page * DEFECT_ITEM_PAGE_SIZE).limit(DEFECT_ITEM_PAGE_SIZE).all()

    markup = InlineKeyboardMarkup()
    for item in user_items:
        button = InlineKeyboardButton(
            text=truncate_button_text(f"{item.item_description} (x{item.quantity})", 64),
            callback_data=f"select_defective_{item.id}",
        )
        markup.add(button)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"defect_items_page_{page - 1}"))
    if (page + 1) * DEFECT_ITEM_PAGE_SIZE < total_count:
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"defect_items_page_{page + 1}"))
    if nav_buttons:
        markup.row(*nav_buttons)

    return markup, total_count, page


def show_user_defect_items(user_id, page=0, message_id=None):
    markup, total_count, page = build_user_defect_items_keyboard(user_id, page)
    if total_count == 0:
        text = "У вас нет товаров, которые подходят для возврата по браку."
        if message_id:
            edited_message = safe_edit_message_text(
                bot,
                logger=logger,
                chat_id=user_id,
                message_id=message_id,
                text=text,
            )
            if edited_message is not None:
                return
        safe_send_message(bot, user_id, text, logger=logger)
        return

    total_pages = max((total_count - 1) // DEFECT_ITEM_PAGE_SIZE + 1, 1)
    text = f"Выберите товар, который хотите вернуть по браку (стр. {page + 1} из {total_pages}):"
    if message_id:
        edited_message = safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=message_id,
            text=text,
            reply_markup=markup,
        )
        if edited_message is not None:
            return

    safe_send_message(bot, user_id, text, logger=logger, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "defect_items_page_"))
def paginate_user_defect_items(call):
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    page = parse_callback_int_suffix(call, "defect_items_page_", "Некорректная страница.")
    if page is None:
        return

    show_user_defect_items(user_id, page=page, message_id=call.message.message_id)
    safe_answer_callback_query(call.id)


def user_can_select_defective_item(user_id, item_id):
    with Session(bind=engine) as session:
        related_user_ids = get_related_user_ids_by_full_phone(user_id)
        if not related_user_ids:
            return False

        return session.query(Temp_Fulfilled.id).filter(
            Temp_Fulfilled.id == item_id,
            Temp_Fulfilled.user_id.in_(related_user_ids),
            Temp_Fulfilled.in_delivery == True,
            Temp_Fulfilled.defect == False,
            Temp_Fulfilled.skidka == False,
        ).first() is not None

@bot.callback_query_handler(func=lambda call: callback_data_startswith(call, "select_defective_"))
def select_defective_order(call):
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    item_id = parse_callback_int_suffix(call, "select_defective_")
    if item_id is None:
        return
    if not user_can_select_defective_item(user_id, item_id):
        safe_answer_callback_query(call.id, "Товар уже недоступен для возврата.", show_alert=True)
        return

    # Сохраняем состояние, чтобы отследить следующий шаг (ввод причины)
    set_user_state(user_id, {"action": "defect_reason", "item_id": item_id})

    # Показываем кнопку для перехода к вводу причины
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📋 Указать причину", callback_data="enter_defect_reason"))

    safe_answer_callback_query(call.id)
    safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Нажмите на кнопку ниже, чтобы указать причину возврата.",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "enter_defect_reason")
def request_defect_reason(call):
    user_id = call.from_user.id
    if ignore_silent_blocked_callback(call):
        return

    state = get_user_state(user_id)

    if not isinstance(state, dict) or state.get("action") != "defect_reason":
        safe_answer_callback_query(call.id, "Ошибка! Попробуйте снова.", show_alert=True)
        return

    safe_answer_callback_query(call.id)
    safe_send_message(
        bot,
        user_id,
        "Пожалуйста, опишите проблему с товаром. Фотография не нужна, только текст",
        logger=logger,
    )
    set_user_state(user_id, {"action": "wait_defect_reason", "item_id": state["item_id"]})


@bot.message_handler(
    func=lambda message: get_state_action(message.chat.id) == "wait_defect_reason"
)
def handle_defect_reason(message):
    user_id = message.chat.id
    if ignore_silent_blocked_message(message):
        return

    state = get_user_state(user_id)

    if not state or "item_id" not in state:
        safe_send_message(bot, user_id, "Ошибка! Попробуйте снова.", logger=logger)
        return

    reason = require_text_message(message, "Введите причину брака текстом.")
    if reason is None:
        return
    item_id = state["item_id"]

    with Session(bind=engine) as session:
        # Получаем запись о товаре
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            safe_send_message(bot, user_id, "Ошибка! Товар не найден.", logger=logger)
            return
        if item.user_id not in get_related_user_ids_by_full_phone(user_id):
            safe_send_message(bot, user_id, "Этот товар не найден в ваших заказах.", logger=logger)
            return

        # Отправляем сообщение администратору
        admin_users = session.query(Clients).filter(Clients.role.in_(["admin", "supreme_leader"])).all()

        # Получаем фото товара из живой карточки или snapshot удаленного поста.
        post = get_post_or_snapshot(session, item.post_id)
        if not post:
            safe_send_message(
                bot,
                user_id,
                "Не удалось найти данные о товаре. Попробуйте позже.",
                logger=logger,
            )
            return

        # Получаем номер телефона клиента из таблицы Clients
        client = session.query(Clients).filter_by(user_id=item.user_id).first()
        if not client:
            safe_send_message(
                bot,
                user_id,
                "Не удалось найти данные о вашем профиле. Попробуйте позже.",
                logger=logger,
            )
            return

        for admin in admin_users:
            # Считаем, сколько времени прошло с момента покупки
            time_since_purchase = now_local() - item.created_at
            days_since_purchase = time_since_purchase.days

            # Формируем текст сообщения
            message_text = (
                f"⚠️ Заявка на возврат брака:\n\n"
                f"👤 Клиент: {item.user_name}\n"
                f"📞 Номер телефона: {client.phone or 'Не указан'}\n"
                f"📦 Товар: {post.description}\n"
                f"❌ Причина: {reason}\n"
                f"🕒 Время с покупки: {days_since_purchase} дней назад\n"
                f"💰 Сумма: {item.price}₽\n"
                f"📅 Дата покупки: {item.created_at.strftime('%d.%m.%Y')}"
            )

            # Создаем inline клавиатуру с кнопками
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("✅ Брак", callback_data=f"defect_{item.id}"),
                InlineKeyboardButton("💸 Скидка", callback_data=f"discount_{item.id}"),
                InlineKeyboardButton("📞 Связаться", callback_data=f"contact_{item.user_id}")
            )

            # Если есть фото товара, отправляем фото с текстом
            if post.photo:
                safe_send_photo(
                    bot,
                    admin.user_id,
                    post.photo,
                    caption=message_text,
                    reply_markup=markup,
                    logger=logger,
                )
            else:
                # Если фото отсутствует, отправляем только текст
                safe_send_message(
                    bot,
                    admin.user_id,
                    message_text,
                    reply_markup=markup,
                    logger=logger,
                )

    safe_send_message(bot, user_id, "Ваш запрос отправлен администратору. Спасибо!", logger=logger)
    clear_user_state(user_id)

def is_admin_defect_action_callback(call):
    data = str(getattr(call, "data", "") or "")
    return (
        (data.startswith("defect_") and not data.startswith("defect_items_page_"))
        or data.startswith("discount_")
        or data.startswith("contact_")
    )


@bot.callback_query_handler(func=is_admin_defect_action_callback)
def handle_inline_buttons(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    data = str(call.data or "")
    if data.startswith("defect_"):
        action = "defect"
        item_id = parse_callback_int_suffix(call, "defect_")
    elif data.startswith("discount_"):
        action = "discount"
        item_id = parse_callback_int_suffix(call, "discount_")
    elif data.startswith("contact_"):
        action = "contact"
        target_user_id = parse_callback_int_suffix(call, "contact_")
    else:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return

    if action == "defect":
        if item_id is None:
            return
        handle_defect_action(call, item_id)
    elif action == "discount":
        if item_id is None:
            return
        request_discount_amount(call, item_id)
    elif action == "contact":
        if target_user_id is None:
            return
        contact_client(call, target_user_id)

def handle_defect_action(call, item_id):
    with Session(bind=engine) as session:
        # Находим запись в Temp_Fulfilled
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).with_for_update().first()
        if not item:
            safe_answer_callback_query(call.id, "Не удалось найти запись.", show_alert=True)
            return
        if item.defect or item.skidka:
            safe_answer_callback_query(call.id, "Этот товар уже обработан.", show_alert=True)
            return

        # Ставим статус "defect = True" в Temp_Fulfilled
        item.defect = True
        session.commit()

        # Получаем user_id клиента через Clients
        client = session.query(Clients).filter_by(user_id=item.user_id).first()
        if client:
            safe_send_message(
                bot,
                client.user_id,
                f"Ваш возврат оформлен!\n\n🔔 Товар: {item.item_description}\n💰 Сумма возврата: {item.price}₽",
                logger=logger,
            )

    # Уведомляем администратора
    safe_send_message(bot, call.message.chat.id, "Возврат оформлен", logger=logger)
    safe_answer_callback_query(call.id)

def request_discount_amount(call, item_id):
    with Session(bind=engine) as session:
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            safe_answer_callback_query(call.id, "Не удалось найти запись.", show_alert=True)
            return
        if item.defect or item.skidka:
            safe_answer_callback_query(call.id, "Этот товар уже обработан.", show_alert=True)
            return

    prompt_message = safe_send_message(
        bot,
        call.message.chat.id,
        "Введите желаемую сумму скидки для клиента:",
        logger=logger,
    )
    if not prompt_message:
        safe_answer_callback_query(call.id, "Не удалось отправить запрос суммы. Попробуйте ещё раз.", show_alert=True)
        return

    # Сохраняем состояние для администратора только после успешной отправки запроса.
    set_user_state(call.from_user.id, {"action": "discount_request", "item_id": item_id, "admin_id": call.from_user.id})
    safe_answer_callback_query(call.id)

@bot.message_handler(
    func=lambda message: get_state_action(message.chat.id) == "discount_request")
def handle_discount_amount(message):
    admin_id = message.chat.id  # ID администратора, который предложил скидку
    state = get_user_state(admin_id)

    if not state:
        safe_send_message(bot, admin_id, "Произошла ошибка. Попробуйте ещё раз.", logger=logger)
        return

    text = require_text_message(message, "Введите сумму скидки текстом.")
    if text is None:
        return

    try:
        discount_amount = int(text)
        if discount_amount <= 0:
            raise ValueError
    except ValueError:
        safe_send_message(bot, admin_id, "Введите корректную сумму скидки (положительное число).", logger=logger)
        return

    # Получаем ID товара
    item_id = state["item_id"]

    with Session(bind=engine) as session:
        # Получаем информацию о товаре
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            safe_send_message(bot, admin_id, "Ошибка! Товар не найден.", logger=logger)
            clear_user_state(admin_id)
            return
        if item.defect or item.skidka:
            safe_send_message(bot, admin_id, "Этот товар уже обработан.", logger=logger)
            clear_user_state(admin_id)
            return

        # Получаем данные клиента
        client = session.query(Clients).filter_by(user_id=item.user_id).first()
        if not client:
            safe_send_message(bot, admin_id, "Ошибка! Не удалось найти клиента.", logger=logger)
            clear_user_state(admin_id)
            return

        # Уведомляем клиента о скидке
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Да", callback_data=f"confirm_discount_{item_id}"),
            InlineKeyboardButton("Отказаться", callback_data=f"return_discount_{item_id}")
        )

        discount_message = safe_send_message(
            bot,
            client.user_id,
            f"Вам поступило предложение о скидке по вашему товару:\n\n"
            f"📦 Товар: {item.item_description}\n"
            f"💰 Размер скидки: {discount_amount}₽\n\n"
            f"Вы согласны на данную скидку?",
            reply_markup=markup,
            logger=logger,
        )
        if not discount_message:
            safe_send_message(
                bot,
                admin_id,
                "Не удалось отправить предложение скидки клиенту. Попробуйте позже.",
                logger=logger,
            )
            clear_user_state(admin_id)
            clear_user_state(client.user_id)
            return

        # Сохраняем состояние для клиента только после успешной отправки сообщения с кнопками.
        set_user_state(
            client.user_id,
            {"action": "confirm_discount", "item_id": item_id, "discount_amount": discount_amount, "admin_id": admin_id}
        )

    # Подтверждаем администратору
    safe_send_message(
        bot,
        admin_id,
        f"Скидка в размере {discount_amount}₽ отправлена клиенту на подтверждение.",
        logger=logger,
    )
    clear_user_state(admin_id)

@bot.callback_query_handler(
    func=lambda call: callback_data_startswith(call, ("confirm_discount_", "return_discount_"))
)
def handle_discount_confirmation(call):
    user_id = call.from_user.id
    if call.data.startswith("confirm_discount_"):
        action = "confirm_discount"
        item_id = parse_callback_int_suffix(call, "confirm_discount_", "Ошибка: некорректные данные.")
    else:
        action = "return_discount"
        item_id = parse_callback_int_suffix(call, "return_discount_", "Ошибка: некорректные данные.")
    if item_id is None:
        return

    state = get_user_state(user_id)
    if (
        not isinstance(state, dict)
        or state.get("action") != "confirm_discount"
        or state.get("item_id") != item_id
    ):
        safe_answer_callback_query(call.id, "Ошибка! Товар не найден.", show_alert=True)
        return

    discount_amount = state.get("discount_amount")
    admin_id = state.get("admin_id")  # Получаем ID администратора

    with Session(bind=engine) as session:
        # Получаем информацию о товаре
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).with_for_update().first()
        if not item:
            safe_answer_callback_query(call.id, "Ошибка! Запись о товаре не найдена.", show_alert=True)
            clear_user_state(user_id)
            return
        if item.defect or item.skidka:
            safe_answer_callback_query(call.id, "Этот товар уже обработан.", show_alert=True)
            clear_user_state(user_id)
            return

        if action == "confirm_discount":
            # Клиент согласен на скидку: Обновляем данные в базе
            item.skidka_price = discount_amount
            item.skidka = True
            session.commit()

            # Уведомляем клиента
            safe_answer_callback_query(call.id, "Скидка успешно активирована.")
            safe_send_message(
                bot,
                call.message.chat.id,
                f"Скидка в размере {discount_amount}₽ успешно активирована! Спасибо за ваше решение!",
                logger=logger,
            )

            # Уведомляем администратора
            if admin_id:
                admin_message = (
                    f"Клиент согласился на скидку для товара:\n\n"
                    f"📦 Товар: {item.item_description}\n"
                    f"💰 Сумма скидки: {discount_amount}₽"
                )
                safe_send_message(bot, admin_id, admin_message, logger=logger)

        elif action == "return_discount":
            # Клиент отказался от скидки: Отмечаем товар как "на возврат" и уведомляем
            item.defect = True  # Помечаем товар как "на возврат"
            session.commit()

            # Уведомляем клиента
            safe_answer_callback_query(call.id, "Хорошо, оформлен возврат.")
            safe_send_message(
                bot,
                call.message.chat.id,
                "Хорошо, оформлен возврат. При следующей доставке товар будет возвращён.",
                logger=logger,
            )

            # Уведомляем администратора
            if admin_id:
                admin_message = (
                    f"Клиент отказался от скидки, и товар был отмечен на возврат:\n\n"
                    f"📦 Товар: {item.item_description}\n"
                    f"💰 Предлагавшаяся скидка: {discount_amount}₽"
                )
                safe_send_message(bot, admin_id, admin_message, logger=logger)

    clear_user_state(user_id)

def contact_client(call, user_id):
    with Session(bind=engine) as session:
        # Получаем данные клиента
        client = session.query(Clients).filter_by(user_id=user_id).first()
        if not client:
            safe_answer_callback_query(call.id, "Не удалось найти данные клиента.", show_alert=True)
            return

        # Отправляем ссылку на чат с клиентом администратору
        safe_send_message(
            bot,
            call.from_user.id,
            f"[Нажмите, чтобы начать чат с клиентом](tg://user?id={client.user_id})",
            parse_mode="Markdown",  # Используем Markdown для создания кликабельной ссылки
            logger=logger,
        )
        safe_answer_callback_query(call.id)



def wait_for_telegram_api():
    retry_delay = TELEGRAM_STARTUP_RETRY_SECONDS
    while True:
        try:
            username = bot.user.username
            logger.info("Telegram API is available. Bot username: @%s", username)
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not is_transient_telegram_error(exc):
                logger.exception("Telegram API preflight failed with non-transient error")
                raise
            logger.warning(
                "Telegram API is unavailable before polling: %s. Retrying in %s seconds.",
                sanitize_telegram_error_text(exc),
                retry_delay,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, TELEGRAM_STARTUP_MAX_RETRY_SECONDS)


# Запуск бота
def run_bot():
    wait_for_telegram_api()
    start_reservation_auto_fulfill_worker()
    start_channel_post_auto_publish_worker()
    start_delivery_cleanup_worker()
    start_reservation_stats_daily_report_worker()
    start_delivery_broadcast_monitor_worker()
    start_payroll_sheet_sync_worker()
    start_delivery_sheet_retry_worker()
    start_reserved_group_resume_flush_if_delivery_done(recover_stale=True)
    retry_delay = 5

    while True:
        try:
            bot.infinity_polling(
                timeout=TELEGRAM_POLLING_TIMEOUT_SECONDS,
                long_polling_timeout=TELEGRAM_LONG_POLLING_TIMEOUT_SECONDS,
            )
            retry_delay = 5
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if is_transient_telegram_error(exc):
                logger.warning(
                    "Bot polling transient failure: %s. Retrying in %s seconds.",
                    sanitize_telegram_error_text(exc),
                    retry_delay,
                )
            else:
                logger.exception("Bot polling failed unexpectedly. Retrying in %s seconds.", retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    run_bot()
