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
from db import init_db
from handlers.black_list import *
from handlers.clients_manage import *
from handlers.posts_manage import *
from handlers.reservations_manage import *
from types import SimpleNamespace
from handlers.reservations_manage import calculate_total_sum, calculate_processed_sum
from handlers.classess import *
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from datetime import datetime, timedelta
from db.bot_session import BotSession
from services.pricing import calculate_audit_price
from services.session_store import PersistentBucket
from services.telegram_safe import (
    is_message_not_modified_error,
    safe_delete_message,
    safe_edit_message_text,
    send_photo_or_text,
)


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
ADMIN_ROLES = {"admin", "supreme_leader"}
DELIVERY_ROLES = {"admin", "supreme_leader"}
DELIVERY_THRESHOLD = 1500
RESERVATION_AUTO_FULFILL_SECONDS = 60 * 60
RESERVATION_AUTO_FULFILL_CHECK_SECONDS = 60
POST_ID_REUSE_STALE_GRACE = timedelta(days=2)
DELIVERY_COLLECTION_PAGE_SIZE = 8
LEGACY_DELIVERY_CUTOFF_AT = datetime(2026, 5, 16, 14, 0, 0)
SAMARA_TZ = ZoneInfo("Europe/Samara")
AUTO_CHANNEL_POST_HOURS = {10, 12, 14, 16}
AUTO_CHANNEL_POST_CHECK_SECONDS = 30
CHANNEL_POST_SEND_ATTEMPTS = 3
CHANNEL_POST_SEND_RETRY_SECONDS = 5
DELIVERY_CLEANUP_WEEKDAYS = {0, 2, 4}
DELIVERY_CLEANUP_HOUR = 22
DELIVERY_CLEANUP_CHECK_SECONDS = 60
RESERVATION_STATS_REPORT_HOUR = 16
RESERVATION_STATS_REPORT_MINUTE = 10
RESERVATION_STATS_CHECK_SECONDS = 30
RESERVATION_STATS_EVENT_CREATED = "created"
RESERVATION_STATS_EVENT_CANCELED = "canceled"
RESERVATION_STATS_EVENT_FULFILLED = "fulfilled"
RESERVATION_STATS_REPORT_STATE_KEY = 2
RESERVED_GROUP_FLOW_STATE_KEY = 0
RESERVED_GROUP_RESUME_BATCH_SIZE = 50
RESERVED_GROUP_SEND_INTERVAL_SECONDS = 5
RESERVED_GROUP_MESSAGE_SKIPPED = -1
PHOENIX_BROADCAST_BUTTON = "Рассылка о Фениксе"
PHOENIX_BROADCAST_DELAY_SECONDS = float(os.environ.get("PHOENIX_BROADCAST_DELAY_SECONDS", "1.5"))
PHOENIX_BROADCAST_BATCH_SIZE = int(os.environ.get("PHOENIX_BROADCAST_BATCH_SIZE", "50"))
PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS = float(os.environ.get("PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS", "10"))
PHOENIX_QR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "phoenix_qr.jpg")
PHOENIX_BROADCAST_TEXT = (
    "Доброго времени суток, уважаемые клиенты Мега Скидки\n"
    "Сегодня происходит выкладка товара как в Телеграме, так и в Фениксе\n"
    "Т.к никто не знает судьбу телеграмма, просим в основном полагаться на Феникс\n"
    "В ближайшее время полностью перейдем в Феникс, так же будут разыгрываться призы среди пользователей Феникса\n"
    "Сайт: https://garphoenix.com/join/INV-R9JQ-F8UW?tenant=default (скопируйте и вставьте в браузер, т.к телеграмм может не пропустить на сайт)\n"
    "Или можете перейти через QR-код"
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


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
    return str(exc).lower()


def telegram_error_contains(exc, *markers):
    text = telegram_error_text(exc)
    return any(marker in text for marker in markers)


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


def is_connect_timeout_error(exc):
    error_text = telegram_error_text(exc)
    return "connecttimeout" in error_text or "connect timeout" in error_text


def notify_publish_status(chat_id, text):
    if not chat_id:
        return False
    try:
        bot.send_message(chat_id, text)
        return True
    except Exception as exc:
        logger.warning("Channel post publish notification failed for chat_id=%s: %s", chat_id, exc)
        return False


def edit_channel_post_caption(post, caption, markup, action):
    for attempt in range(2):
        try:
            bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=post.message_id,
                caption=caption,
                reply_markup=markup,
            )
            return True
        except Exception as exc:
            if is_message_not_modified_error(exc):
                logger.debug("Channel post %s already up to date for post_id=%s", action, post.id)
                return True
            if attempt == 0 and sleep_for_short_retry_after(exc):
                continue
            log_channel_sync_error(f"{action} caption", post.id, exc)
            if telegram_error_contains(exc, "message to edit not found"):
                return False
            return None


def edit_channel_post_text(post, text, markup, action):
    for attempt in range(2):
        try:
            bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=post.message_id,
                text=text,
                reply_markup=markup,
            )
            return True
        except Exception as exc:
            if is_message_not_modified_error(exc):
                logger.debug("Channel post %s already up to date for post_id=%s", action, post.id)
                return True
            if attempt == 0 and sleep_for_short_retry_after(exc):
                continue
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


def delete_delivered_channel_post_message(post, source="delivery_cleanup"):
    if not post or not post.message_id:
        return True

    message_id = post.message_id
    try:
        bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
    except Exception as exc:
        if "message to delete not found" not in str(exc).lower():
            log_channel_sync_error("delivered delete", post.id, exc)
            return disable_channel_post_reservation(post, reason="Товар уехал в доставку")

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
configure_telegram_proxy()
bot = telebot.TeleBot(TOKEN)
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
phoenix_broadcast_lock = threading.Lock()
channel_post_publish_lock = threading.Lock()
channel_post_auto_publish_started = False
channel_post_auto_publish_stop_event = threading.Event()
delivery_cleanup_started = False
delivery_cleanup_stop_event = threading.Event()
reserved_group_flow_lock = threading.Lock()
reservation_stats_report_started = False
reservation_stats_report_stop_event = threading.Event()
reservation_stats_live_lock = threading.Lock()
reservation_stats_live_sessions = {}


def safe_answer_callback_query(*args, **kwargs):
    try:
        return bot.answer_callback_query(*args, **kwargs)
    except Exception as exc:
        error_text = str(exc).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            logger.debug("Telegram callback answer skipped: %s", exc)
        else:
            logger.warning("Telegram callback answer failed: %s", exc)
        return None


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
    user_id = message.from_user.id
    if has_role(user_id, roles):
        return True

    bot.send_message(message.chat.id, "У вас недостаточно прав для этой команды.")
    return False


def is_creator(user_id):
    return int(user_id) == ADMIN_USER_ID


def require_creator(message):
    if is_creator(message.from_user.id):
        return True

    bot.send_message(message.chat.id, "Эта команда доступна только создателю.")
    return False


def get_state_action(user_id):
    state = get_user_state(user_id)
    if isinstance(state, dict):
        return state.get("action")
    return None


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
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def add_reservation_stat_event(session, event_type, reservation, event_time=None):
    if not reservation or not getattr(reservation, "id", None):
        return False

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
        "updated_at": value or now_local(),
    }


def build_reservation_stats_text(snapshot=None, title="Статистика броней"):
    snapshot = snapshot or get_reservation_stats_snapshot()
    return (
        f"{title}\n"
        f"Дата: {snapshot['updated_at'].strftime('%d.%m.%Y')}\n"
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
        try:
            bot.send_message(user_id, text)
            sent_count += 1
            time.sleep(0.3)
        except Exception as exc:
            logger.warning("Daily reservation stats report failed for user_id=%s: %s", user_id, exc)
    logger.info("Daily reservation stats report sent to supreme leaders: %s", sent_count)
    return sent_count


def get_reservation_stats_report_state():
    state = user_data.get(RESERVATION_STATS_REPORT_STATE_KEY, {})
    return state if isinstance(state, dict) else {}


def save_reservation_stats_report_state(state):
    user_data[RESERVATION_STATS_REPORT_STATE_KEY] = state


def should_send_reservation_stats_report(value):
    value = value.astimezone(SAMARA_TZ) if value.tzinfo else value.replace(tzinfo=SAMARA_TZ)
    return value.hour == RESERVATION_STATS_REPORT_HOUR and value.minute == RESERVATION_STATS_REPORT_MINUTE


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
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=build_reservation_stats_text(title="Живая статистика броней"),
                reply_markup=build_reservation_stats_close_markup(user_id),
            )
        except Exception as exc:
            if is_message_not_modified_error(exc):
                continue
            logger.warning("Live reservation stats update failed user_id=%s message_id=%s: %s", user_id, message_id, exc)


@bot.message_handler(func=lambda message: message.text == "БроньСтатистик")
def show_live_reservation_stats(message):
    user_id = message.from_user.id
    if get_client_role(user_id) != "supreme_leader":
        bot.send_message(message.chat.id, "У вас нет прав доступа к этой функции.")
        return

    sent_message = bot.send_message(
        message.chat.id,
        build_reservation_stats_text(title="Живая статистика броней"),
        reply_markup=build_reservation_stats_close_markup(user_id),
    )
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


@bot.callback_query_handler(func=lambda call: call.data.startswith("reservation_stats_close_"))
def close_live_reservation_stats(call):
    try:
        target_user_id = int(call.data.rsplit("_", 1)[1])
    except ValueError:
        safe_answer_callback_query(call.id, "Некорректная кнопка.", show_alert=True)
        return

    if call.from_user.id != target_user_id or get_client_role(call.from_user.id) != "supreme_leader":
        safe_answer_callback_query(call.id, "У вас нет прав доступа.", show_alert=True)
        return

    with reservation_stats_live_lock:
        reservation_stats_live_sessions.pop(target_user_id, None)

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Статистика броней закрыта.",
            reply_markup=None,
        )
    except Exception as exc:
        if not is_message_not_modified_error(exc):
            logger.warning("Live reservation stats close edit failed: %s", exc)

    bot.send_message(
        call.message.chat.id,
        "Главное меню",
        reply_markup=supreme_leader_main_menu(),
    )
    safe_answer_callback_query(call.id)

# Получение неотправленных постов
@bot.message_handler(commands=["unsent_posts"])
def list_unsent_posts(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем роль пользователя
    if role not in ["admin", "worker", "supreme_leader", "audit"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Получаем неотправленные посты через метод класса
    try:
        unsent_posts = Posts.get_unsent_posts()
    except Exception:
        return

    # Формируем и отправляем ответ
    if unsent_posts:
        response = "📮 Неотправленные посты:\n"
        for post in unsent_posts:
            response += (
                f"ID: {post.id} | Цена: {post.price} ₽ | "
                f"Описание: {post.description} | Количество: {post.quantity}\n"
            )
        bot.send_message(user_id, response)
    else:
        bot.send_message(user_id, "✅ Все посты отправлены.")

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
            print(
                f"Не удалось удалить старое приветственное сообщение для {user_id}: {e}"
            )
        try:
            # Удаляем сообщение с ресурсами
            safe_delete_message(
                bot, user_id, last_bot_message[user_id].get("resources"), logger=logger
            )
        except Exception:
            pass

    # Отправляем новое сообщение с приветствием
    try:
        sent_greeting = bot.send_message(user_id, greeting, reply_markup=reply_markup)

        # Если роль клиента, отправляем сообщение с ресурсами
        if role == "client":
            sent_resources = bot.send_message(
                user_id, "Посетите наши ресурсы:", reply_markup=inline_markup
            )
        else:
            sent_resources = None

        # Сохраняем ID новых сообщений
        last_bot_message[user_id] = {
            "greeting": sent_greeting.message_id,
            "resources": sent_resources.message_id if sent_resources else None,
        }
    except Exception as e:
        print(f"Ошибка при отправке сообщения для {user_id}: {e}")

    # Удаляем сообщение пользователя (команда /start)
    try:
        safe_delete_message(bot, user_id, message.message_id, logger=logger)
    except Exception:
        pass


# Обработчик нажатия на кнопку "Правила"
@bot.callback_query_handler(func=lambda call: call.data == "rules")
def show_rules(call):
    # Указываем текст правил
    rules_text = ("Правила Мега Скидок:\n1.В описании к посту всегда пишется дефект(если он имеется) и количество."
                  "\n2.Купленный товар возврату и обмену не подлежат."
                  "\n3.Одежда дешевле 1 500₽, не подошедшая по размеру возврату не подлежат."
                  "\n4.Администратор не знает что находится в корзине(Смотрите  Мои заказы)"
                  "\n5.Просьба доложить товар будет проигнорирована, товары обрабатываются в аблолютно случайном порядке"
                  "\n6.Бронь уходит первому нажавшему, и держится в течении некоторого времени"
                  "\n7.До обработки товара вы можете отказаться от товара, как только товар оказался у вас в корзине, отказаться уже нельзя, только полная расформировка"
                  "\n8.Доставка бесплатная от 2 000₽"
                  "\n9.Если не набралось данной суммы, можно осуществить платную доставку стоимостью в 350₽"
                  "\n10.Электрические товары, приобретенные у нас, имеют гарантию 7 дней."
                  "\n11.В случае, если товар пришел с дефектом, который не был указан, можете обратиться в поддержку."
                  "\nВо время доставки:"
                  "\n12.Курьер не может звонить заранее более чем за 5 минут в связи с загруженностью"
                  "\n13.Товар проверяется исключительно после оплаты"
                  "\n14.Курьер не знает что находится у вас в корзине(Смотрите  Заказы в доставке)")

    # Создаём разметку с кнопкой "Назад"
    markup = InlineKeyboardMarkup()
    back_button = InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")  # Callback для возврата
    markup.add(back_button)

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
        print(f"Ошибка при изменении текста сообщения: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "back_to_start")
def back_to_start(call):
    try:
        # Удаляем текущее сообщение с правилами
        safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)

        # Отправляем уведомление о возврате
        notification_message = bot.send_message(
            chat_id=call.message.chat.id,
            text="Вы вернулись в главное меню."
        )

        # Используем таймер для удаления уведомления через 5 секунд
        threading.Timer(
            5.0,
            safe_delete_message,
            args=(bot, call.message.chat.id, notification_message.message_id),
            kwargs={"logger": logger},
        ).start()

        # Отправляем приветственное сообщение, вызывая handle_start
        handle_start(call.message)

    except Exception as e:
        print(f"Ошибка при обработке возврата в главное меню: {e}")


# Хэндлер регистрации
@bot.message_handler(func=lambda message: message.text == "Регистрация")
def handle_registration(message):
    chat_id = message.chat.id

    # Проверяем, находится ли пользователь в черном списке
    if is_user_blacklisted(chat_id):
        bot.send_message(chat_id, "К сожалению, вы не можете зарегистрироваться, так как находитесь в черном списке.")
        return

    # Проверяем, зарегистрирован ли уже пользователь
    if Clients.get_row_by_user_id(chat_id):
        bot.send_message(chat_id, "Вы уже зарегистрированы!")
        handle_start(message)
        return

    # Сохраняем состояние для шага регистрации имени
    set_user_state(chat_id, Registration.REGISTERING_NAME)
    bot.send_message(chat_id, "Введите ваше имя:")

# Имя для регистрации
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == Registration.REGISTERING_NAME)
def handle_name_registration(message):
    chat_id = message.chat.id
    user_name = message.text.strip()

    # Проверяем валидность имени
    if len(user_name) < 2:
        bot.send_message(chat_id, "Имя слишком короткое. Пожалуйста, введите хотя бы 2 символа.")
        return

    # Сохраняем имя во временные данные
    if chat_id not in temp_user_data:
        temp_user_data[chat_id] = {}
    temp_user_data[chat_id]["name"] = user_name

    # Переход к следующему шагу - вводу номера
    set_user_state(chat_id, Registration.STARTED_REGISTRATION)
    bot.send_message(chat_id, "Введите ваш номер телефона:")

# Номер для регистрации
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == Registration.STARTED_REGISTRATION)
def handle_phone_registration(message):
    chat_id = message.chat.id
    raw_phone = message.text.strip()
    cleaned_phone = normalize_phone(raw_phone)

    # Проверка валидности номер через регулярное выражение
    if is_phone_valid(cleaned_phone):
        # Проверяем наличие номера в базе
        existing_client = Clients.get_row_by_phone(cleaned_phone)
        if existing_client:
            # Номер уже существует
            bot.send_message(
                chat_id,
                f"⚠️ Номер телефона {cleaned_phone} уже зарегистрирован. Вы уверены, что хотите его привязать к своему аккаунту? "
                "Эта операция уведомит текущего владельца номера.",
                reply_markup=create_yes_no_keyboard()  # Генерируем клавиатуру
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
        bot.send_message(chat_id, "❌ Введите корректный номер телефона. Например, +7XXXXXXXXXX")

# Валидация номера телефона
def is_phone_valid(phone):
    normalized = normalize_phone(phone)
    return len(normalized) == 11 and normalized.startswith("8")

# Подтверждение номера
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == Registration.REGISTERING_PHONE)
def confirm_phone_registration(message):
    chat_id = message.chat.id
    response = message.text.strip().lower()

    # Проверка временных данных (подстраховка)
    if chat_id not in temp_user_data or "phone" not in temp_user_data[chat_id]:
        bot.send_message(chat_id, "❌ Ошибка: временные данные регистрации потеряны. Попробуйте снова.")
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
            bot.send_message(
                existing_client.user_id,
                f"⚠️ Новый пользователь ({name}) зарегистрировался с номером телефона {phone}. "
                "Все заказы для этого телефона будут сохраняться в вашей корзине."
            )

            # Оповещаем нового пользователя
            bot.send_message(
                chat_id,
                f"✅ Вы успешно зарегистрированы! Корзина будет привязана к номеру {phone}, "
                "используемому несколькими пользователями.",
                reply_markup=types.ReplyKeyboardRemove()
            )

            # Завершаем регистрацию (важно!)
            clear_user_state(chat_id)
            handle_start(message)
        else:
            # Если номер уникальный, завершаем обычную регистрацию
            complete_registration(chat_id, phone)
    elif response == "нет":
        # Если пользователь отказался, предлагаем ввести новый номер телефона
        bot.send_message(chat_id, "❌ Регистрация номера отменена. Введите новый номер телефона:")
        set_user_state(chat_id, Registration.STARTED_REGISTRATION)
    else:
        # Если пользователь ввел что-то не то
        bot.send_message(
            chat_id,
            "⚠️ Пожалуйста, введите *'Да'* для подтверждения или *'Нет'* для отказа.",
            parse_mode="Markdown"
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
            bot.send_message(
                chat_id,
                f"✅ Вы успешно зарегистрированы. Ваш номер телефона {phone} уже используется, "
                "и ваша корзина будет объединена с текущими бронированиями.",
                reply_markup=types.ReplyKeyboardRemove()
            )

            # Уведомляем первого владельца телефона (если важно)
            bot.send_message(
                existing_client.user_id,
                f"⚠️ Новый пользователь ({name}) зарегистрировался под вашим номером телефона ({phone}). "
                "Бронирования будут объединены."
            )

        else:
            # Если номер уникальный, продолжаем обычную регистрацию
            Clients.insert(
                user_id=chat_id,
                name=name,
                phone=phone,
                role=role  # Роль по умолчанию
            )

            bot.send_message(
                chat_id,
                f"✅ Регистрация завершена! Ваш номер телефона {phone} сохранен.",
                reply_markup=types.ReplyKeyboardRemove()
            )

        # Завершаем процесс регистрации
        clear_user_state(chat_id)
        handle_start(SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=None))

    except Exception as e:
        bot.send_message(chat_id, "❌ Во время регистрации произошла ошибка. Попробуйте позже.")
        print(f"[ERROR]: Ошибка завершения регистрации: {e}")

# Создание клавиатуры да или нет для подтверждения номера
def create_yes_no_keyboard():
    """Генерирует клавиатуру для подтверждения"""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("Да"), types.KeyboardButton("Нет"))
    return markup

# Обработчик запроса бронирования
@bot.callback_query_handler(func=lambda call: call.data.startswith("reserve_"))
def handle_reservation(call):
    post_id = int(call.data.split("_", 1)[1])
    user_id = call.from_user.id
    if is_user_blacklisted(user_id):
        return "Вы не можете бронировать товары, так как вы были заблокированы"
    if not is_registered(user_id):
        safe_answer_callback_query(
            callback_query_id=call.id,
            text="Вы не зарегистрированы! Для регистрации перейдите в бота",
            show_alert=True,
        )
        return
    with Session(bind=engine) as session:
        try:
            # Получаем текущий товар с блокировкой строки
            post = session.query(Posts).filter(Posts.id == post_id).with_for_update().first()
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
            add_reservation_stat_event(session, RESERVATION_STATS_EVENT_CREATED, reservation, event_time=reservation.created_at)
            session.commit()

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
            try:
                bot.send_photo(
                    chat_id=user_id,
                    photo=post.photo,  # Ссылка на фото товара
                    caption=(
                        f"Вы забронировали товар!\n\n"
                        f"🏷️ Название: {post.description}\n"
                        f"💲 Цена: {post.price} ₽\n"
                        f"Если это была ошибка, нажмите кнопку ниже."
                    ),
                    reply_markup=cancel_button,
                )
            except Exception as e:
                bot.send_message(
                    chat_id=user_id,
                    text=f"Не удалось отправить информацию о бронировании. Ошибка: {e}",
                )

            # Уведомление пользователя
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
        except IntegrityError:
            session.rollback()
            safe_answer_callback_query(
                callback_query_id=call.id,
                text="Произошла ошибка при бронировании. Попробуйте снова.",
                show_alert=True,
            )
        except Exception as exc:
            session.rollback()
            logger.exception("Reservation failed for user_id=%s post_id=%s: %s", user_id, post_id, exc)
            safe_answer_callback_query(
                callback_query_id=call.id,
                text="Произошла ошибка при бронировании. Попробуйте снова.",
                show_alert=True,
            )

# Получение бронирования пользователя
def get_user_reservations(user_id):
    """
    phone = normalize_phone(phone)
    Получение всех заказов текущего пользователя и других пользователей с тем же полным номером телефона.
    """
    # Получаем текущие данные пользователя
    client = Clients.get_row_by_user_id(user_id)
    if client is None:
        print("Пользователь не найден.")
        return []  # Пользователь не зарегистрирован

    # Находим всех пользователей с таким же полным номером телефона.
    related_clients = Clients.get_rows_by_phone(client.phone)
    if not related_clients:
        print("Связанные пользователи по полному номеру телефона не найдены.")
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
            try:
                bot.send_message(
                    admin_chat_id,
                    "Не удалось полностью возобновить поток забронированного товара. "
                    "Новые брони пока остаются в очереди.",
                )
            except Exception:
                logger.exception("Reserved group resume failure notification failed")


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


def edit_reserved_group_message(message_id, post, caption):
    if not message_id:
        return False

    try:
        if post and post.photo and len(caption) <= 1024:
            bot.edit_message_caption(
                chat_id=TARGET_GROUP_ID,
                message_id=message_id,
                caption=caption,
                reply_markup=None,
            )
        else:
            bot.edit_message_text(
                chat_id=TARGET_GROUP_ID,
                message_id=message_id,
                text=caption,
                reply_markup=None,
            )
        return True
    except Exception as exc:
        if is_message_not_modified_error(exc):
            logger.debug("Reserved group message already up to date: %s", exc)
            return True

    try:
        bot.edit_message_text(
            chat_id=TARGET_GROUP_ID,
            message_id=message_id,
            text=caption,
            reply_markup=None,
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
    return session.query(Reservations).filter(
        Reservations.user_id.in_(related_user_ids),
        Reservations.is_fulfilled == True,
        Reservations.fulfilled_at != None,
        Reservations.fulfilled_at <= cutoff_at,
    )


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
    now = now or now_local()
    deadline = now - timedelta(seconds=older_than_seconds)

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
        ).all()

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
    query = Clients.get_row(user_id=user_id)
    # Проверка регистрации пользователя
    # if not is_registered(user_id):
    if query is None:
        msg = bot.send_message(
            user_id,
            "Вы не зарегистрированы! Для регистрации используйте команду /start register.",
        )
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
                try:
                    sent_photo = bot.send_photo(user_id, photo=post.photo, caption=caption)
                    user_messages.setdefault(user_id, []).append(sent_photo.message_id)
                except Exception as e:
                    bot.send_message(
                        user_id, f"Ошибка при показе фотографии: {e}"
                    )  # Показываем ошибку
            else:
                sent_message = bot.send_message(user_id, caption)
                user_messages.setdefault(user_id, []).append(sent_message.message_id)
    else:
        bot.send_message(user_id, "У вас пока нет заказов.")

# Хэндлер для обработки нажатий на заказ
@bot.callback_query_handler(func=lambda call: call.data.startswith("order_"))
def order_details(call):
    reservation_id = int(call.data.split("_")[1])

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
            safe_answer_callback_query(call.id, "Товар не найден.", show_alert=True)
            return

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

        # Обновляем сообщение с деталями заказа
        if post.photo:
            bot.edit_message_media(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                media=InputMediaPhoto(media=post.photo, caption=caption),
                reply_markup=markup
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
        print(f"Ошибка отображения деталей заказа: {e}")
        safe_answer_callback_query(call.id, "Произошла ошибка.", show_alert=True)

# Отображает список заказов
@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def show_my_orders(call):
    message = call.message
    my_orders(message)  # Вызываем my_orders, передаём исходное сообщение
    safe_answer_callback_query(call.id)  # Подтверждаем обработку нажатия

# Обработчик функции Мои заказы
@bot.message_handler(func=lambda message: message.text == "🛒 Мои заказы")
def my_orders(message):
    user_id = message.chat.id

    # Сначала удаляем сообщение пользователя
    try:
        safe_delete_message(bot, user_id, message.message_id, logger=logger)
    except Exception:
        pass

    try:
        # Удаляем предыдущее сообщение бота, если оно есть
        if user_id in user_last_message_id:
            try:
                safe_delete_message(bot, user_id, user_last_message_id[user_id], logger=logger)
            except Exception:
                pass

        # Проверяем, зарегистрирован ли пользователь
        current_user = Clients.get_row_by_user_id(user_id)
        if not current_user:
            sent_message = bot.send_message(chat_id=user_id, text="❌ Вы не зарегистрированы.")
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
            sent_message = bot.send_message(
                chat_id=user_id,
                text="У вас пока нет заказов. Начните покупки, перейдя на наш канал.",
                reply_markup=keyboard,
            )
            user_last_message_id[user_id] = sent_message.message_id  # Сохраняем ID последнего сообщения
    except Exception as ex:
        print(f"Ошибка в обработке команды '🛒 Мои заказы': {ex}")


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
        post = posts_by_id.get(order.post_id)
        posts_by_id[order.post_id] = post
        amount = calculate_order_amount(order, post)
        total_sum_all += amount
        if order.is_fulfilled:
            total_sum_fulfilled += amount

    # Формирование текста для страницы. Колонки: описание, цена, статус заказа.
    text = f"Ваши заказы (стр. {page + 1} из {total_pages}):\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)

    for order in selected_orders:
        post = posts_by_id.get(order.post_id)
        if post:
            status = "✅В корзине" if order.is_fulfilled else "⏳В обработке"
            amount = calculate_order_amount(order, post)
            keyboard.add(InlineKeyboardButton(
                text=f"({status})- {amount} ₽ - {post.description}",
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

    # Фото для страницы
    photo_path = "images/my_cart.jpg"
    with open(photo_path, "rb") as photo:
        if message_id:
            try:
                return bot.edit_message_media(
                    chat_id=user_id,
                    message_id=message_id,
                    media=InputMediaPhoto(photo, caption=text),
                    reply_markup=keyboard
                )
            except Exception as exc:
                if is_message_not_modified_error(exc):
                    logger.debug("Orders page was already up to date for user=%s page=%s", user_id, page)
                    return SimpleNamespace(message_id=message_id)
                raise
        else:
            return bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=text,
                reply_markup=keyboard
            )

# Обработчик навигации между страницами
@bot.callback_query_handler(func=lambda call: call.data.startswith("orders_page_"))
def paginate_orders(call):
    user_id = call.message.chat.id
    message_id = call.message.message_id
    page = int(call.data.split("_")[2])

    # Получаем заказы пользователя и связанных клиентов
    orders = get_user_reservations(user_id)

    # Отправляем страницу с заказами
    try:
        new_message = send_order_page(user_id=user_id, message_id=message_id, orders=orders, page=page)
        user_last_message_id[user_id] = new_message.message_id  # Обновляем последний ID
    except Exception as e:
        logger.warning("Order pagination failed for user_id=%s page=%s: %s", user_id, page, e)
    finally:
        safe_answer_callback_query(call.id)  # Подтверждаем успешную обработку

# Обработка отмены заказа
@bot.callback_query_handler(
    func=lambda call: call.data.startswith(("cancel_order_", "cancel_reservation_"))
)
def cancel_reservation(call):
    if call.data.startswith("cancel_order_"):
        call.data = call.data.replace("cancel_order_", "cancel_reservation_", 1)
    logger.debug("Cancel reservation callback received: %s", call.data)
    try:
        # Универсальная обработка двух форматов данных
        if call.data.startswith("cancel_reservation_"):
            parts = call.data.split("_")
            if len(parts) == 3 and parts[2].isdigit():
                reservation_id = int(parts[2])
            else:
                raise ValueError(f"Некорректный формат callback_data: {call.data}")
        elif call.data.startswith("cancel_"):
            parts = call.data.split("_")
            if len(parts) == 2 and parts[1].isdigit():
                reservation_id = int(parts[1])
            else:
                raise ValueError(f"Некорректный формат callback_data: {call.data}")
        else:
            raise ValueError(f"Некорректный формат callback_data: {call.data}")

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

        post = Posts.get_row_by_id(order.post_id)
        if not post:
            safe_answer_callback_query(call.id, "Товар для отмены не найден.", show_alert=True)
            return

        order_client = Clients.get_row_by_user_id(order.user_id)
        mark_reserved_group_message_canceled(order, post, order_client)

        success = Reservations.cancel_order_by_id(reservation_id)
        if not success:
            safe_answer_callback_query(call.id, "Ошибка отмены заказа.", show_alert=True)
            return
        record_reservation_stat_event(RESERVATION_STATS_EVENT_CANCELED, order)

        with Session(bind=engine) as session:
            next_in_queue = session.query(TempReservations).filter(
                TempReservations.post_id == order.post_id,
                TempReservations.temp_fulfilled == False
            ).order_by(TempReservations.created_at).first()

            if next_in_queue:
                queued_user_id = next_in_queue.user_id
                queued_post = session.query(Posts).filter(Posts.id == order.post_id).first()
                queued_client = session.query(Clients).filter(
                    Clients.user_id == queued_user_id
                ).first()
                queued_reservation = Reservations(
                    user_id=queued_user_id,
                    post_id=order.post_id,
                    quantity=1,
                    is_fulfilled=False,
                    old_price=queued_post.price if queued_post else post.price,
                    created_at=now_local(),
                )
                session.add(queued_reservation)
                session.flush()
                queued_reservation_id = queued_reservation.id
                add_reservation_stat_event(
                    session,
                    RESERVATION_STATS_EVENT_CREATED,
                    queued_reservation,
                    event_time=queued_reservation.created_at,
                )
                next_in_queue.temp_fulfilled = True
                session.commit()
                if queued_post and queued_client:
                    queued_reservation.id = queued_reservation_id
                    send_reserved_group_message(session, queued_reservation, queued_post, queued_client)

                bot.send_message(
                    chat_id=queued_user_id,
                    text="Ваш товар в очереди стал доступен и добавлен в вашу корзину."
                )

                safe_answer_callback_query(call.id, "Вы успешно отказались от товара. Он передан следующему в очереди.",
                                          show_alert=False)
                my_orders(call.message)
                return

        Posts.increment_quantity_by_id(order.post_id)

        update_channel_post_message(Posts.get_row_by_id(order.post_id))

        safe_answer_callback_query(call.id, "Вы успешно отказались от товара. Товар доступен в канале.",
                                  show_alert=False)
        my_orders(call.message)

    except ValueError as ve:
        print(f"Некорректные callback-данные: {ve}")
        safe_answer_callback_query(call.id, "Некорректные данные для отмены.", show_alert=True)
    except Exception as e:
        print(f"Ошибка при попытке отказаться от заказа: {e}")
        safe_answer_callback_query(call.id, "Произошла ошибка при обработке отмены.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("enqueue_"))
def handle_enqueue(call):
    user_id = call.message.chat.id
    post_id = int(call.data.split("_")[1])

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
    bot.send_message(user_id, "Вы добавлены в очередь. Как только товар станет доступен, вы будете уведомлены.")

# Возврат в меню заказов
@bot.callback_query_handler(func=lambda call: call.data == "go_back")
def go_back_to_menu(call):
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
    bot.send_message(chat_id, "Вы вернулись в главное меню.")

# Обработчик функции 🚗 Заказы в доставке
@bot.message_handler(func=lambda message: message.text == "🚗 Заказы в доставке")
def show_delivery_orders(message):
    user_id = message.chat.id  # Получаем ID текущего пользователя

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
            bot.send_message(
                chat_id=user_id,
                text="📭 У вас нет товаров в доставке.",
            )
            return

        # Отправляем список первым сообщением
        send_delivery_order_page(
            user_id=user_id,
            message_id=None,  # Потому что отправляется впервые
            orders=unique_items,
            page=0,
        )

    except Exception as e:
        # Если возникла ошибка — информируем пользователя
        bot.send_message(
            chat_id=user_id,
            text=f"❌ Ошибка при загрузке списка заказов: {str(e)}",
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

    # Гифка для уведомления
    gif_path = "images/delivery_order.gif"
    with open(gif_path, "rb") as gif:
        if message_id:  # Если сообщение уже существует, обновляем его
            bot.edit_message_media(
                chat_id=user_id,
                message_id=message_id,
                media=InputMediaAnimation(gif, caption=text),
                reply_markup=keyboard,
            )
        else:  # Иначе отправляем новое сообщение
            bot.send_animation(
                chat_id=user_id,
                animation=gif,
                caption=text,
                reply_markup=keyboard,
            )

# Хэндлер для команды "👔 Назначить работника"
@bot.message_handler(func=lambda message: message.text == "👔 Назначить работника")
def manage_user(message):
    # Проверяем, является ли пользователь администратором или лидером
    user_id = message.from_user.id
    if not (is_admin(user_id) or is_leader(user_id)):
        bot.send_message(message.chat.id, "У вас недостаточно прав для выполнения этой команды.")
        return

    # Если пользователь имеет доступ, продолжаем выполнение функции
    bot.send_message(
        message.chat.id,
        "Введите Имя пользователя и последние 4 цифры номера через пробел (например, Иван 1234):"
    )
    bot.register_next_step_handler(message, process_user_input)


def show_role_controls(chat_id, client):
    response = f"Данные пользователя:\nИмя: {client.name}\nТелефон: {client.phone}\nТекущая роль: {client.role}"
    if client.role in SPECIAL_ROLES:
        bot.send_message(chat_id, response + "\nЭту роль нельзя изменить.")
        return

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Повысить", callback_data=f"promote_{client.user_id}"),
        InlineKeyboardButton("Понизить", callback_data=f"demote_{client.user_id}"),
    )
    bot.send_message(chat_id, response, reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith("role_select_"))
def handle_role_user_selection(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    client_id = int(call.data.split("_")[2])
    client = Clients.get_row_by_id(client_id)
    if not client:
        safe_answer_callback_query(call.id, "Пользователь не найден.", show_alert=True)
        return

    show_role_controls(call.message.chat.id, client)
    safe_answer_callback_query(call.id)

# Обработка ввода имени и последних 4 цифр номера для поиска
def process_user_input(message):
    try:
        # Разбиваем данные на имя и последние цифры
        name, last_digits = message.text.split()
        last_digits = last_digits.strip()

        if not last_digits.isdigit() or len(last_digits) != 4:
            bot.send_message(message.chat.id, "Пожалуйста, введите корректные последние 4 цифры номера.")
            return

        # Поиск пользователя по имени и последним 4 цифрам номера
        matching_users = Clients.get_rows_for_work_name_number(name=name, phone_ending=last_digits)
        if not matching_users:
            bot.send_message(message.chat.id, "Пользователь не найден.")
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
            bot.send_message(
                message.chat.id,
                "Найдено несколько пользователей. Выберите нужного по полному номеру:",
                reply_markup=keyboard,
            )
            return

        show_role_controls(message.chat.id, matching_users[0])
        return

        user = find_user_by_name_and_last_digits(name, last_digits)

        if user:
            # Формируем сообщение с данными пользователя
            response = f"Данные пользователя:\nИмя: {user['name']}\nТекущая роль: {user['role']}"

            # Если роль из списка SPECIAL_ROLES, запрещаем изменение
            if user['role'] in SPECIAL_ROLES:
                response += "\nЭту роль нельзя изменить."
                bot.send_message(message.chat.id, response)
                return

            # Создаем кнопки для повышения/понижения роли
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton("Повысить", callback_data=f"promote_{user['user_id']}"),
                InlineKeyboardButton("Понизить", callback_data=f"demote_{user['user_id']}")
            )
            bot.send_message(message.chat.id, response, reply_markup=keyboard)
        else:
            bot.send_message(message.chat.id, "Пользователь не найден.")
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите данные в формате 'Имя 1234'.")
    except Exception as e:
        bot.send_message(message.chat.id, "Произошла ошибка при обработке данных.")
        print(f"Ошибка: {e}")

# Обработчик изменения роли
@bot.callback_query_handler(func=lambda call: call.data.startswith("promote_") or call.data.startswith("demote_"))
def handle_role_change(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    try:
        # Получаем данные из callback (action, user_id)
        action, user_id = call.data.split("_")

        # Получаем пользователя через Clients
        user = Clients.get_row_by_user_id(int(user_id))  # Используем существующий метод get_row_by_user_id
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
                print(f"Ошибка обновления сообщения: {e}")
                safe_answer_callback_query(call.id, "Ошибка отображения новых данных, но роль изменена.")
                return

            # Уведомляем пользователя об успешном изменении роли
            safe_answer_callback_query(call.id, f"Роль изменена на {new_role}.")
        else:
            safe_answer_callback_query(call.id, "Ошибка при обновлении данных.")
    except Exception as e:
        safe_answer_callback_query(call.id, "Ошибка при обработке запроса.")
        print(f"Ошибка: {e}")

# Поиск пользователя по имени и последним 4 цифрам номера
def find_user_by_name_and_last_digits(name, last_digits):
    try:
        user = Clients.get_row_for_work_name_number(name=name, phone_ending=last_digits)
        if not user:
            print("Пользователь не найден.")  # отладка
            return None
        # Возвращаем user_id, чтобы использовать его далее
        return {
            'user_id': user.user_id,
            'name': user.name,
            'role': user.role,
        }
    except Exception as e:
        print(f"Ошибка при поиске пользователя: {e}")
        return None

# Обработчик навигации между страницами для заказов в доставке
@bot.callback_query_handler(func=lambda call: call.data.startswith("delivery_page_"))
def paginate_delivery_orders(call):
    user_id = call.message.chat.id
    message_id = call.message.message_id
    page = int(call.data.split("_")[2])

    # Получаем заказы пользователя
    orders = InDelivery.get_all_rows()
    related_user_ids = get_related_user_ids_by_full_phone(user_id)
    user_orders = [order for order in orders if order.user_id in related_user_ids]
    user_orders = aggregate_delivery_items(user_orders)

    try:
        # Отправка обновления страницы
        send_delivery_order_page(user_id=user_id, message_id=message_id, orders=user_orders, page=page)
    except Exception as e:
        print(f"Ошибка при попытке пагинации заказов в доставке: {e}")
    finally:
        safe_answer_callback_query(call.id)  # Подтверждаем успешную обработку

# Перессылка забронированного товара в группу Брони Мега Скидки
@bot.message_handler(func=lambda message: message.text == "📦 Заказы клиентов")
def send_all_reserved_to_group(message):
    user_id = message.chat.id
    role = get_client_role(user_id)  # Получение роли клиента
    # Проверка ролей
    if role not in ["supreme_leader", "admin"]:
        bot.send_message(user_id, f"У вас нет прав доступа к этой функции. Ваша роль: {role}")
        return
    bot.send_message(
        user_id,
        "Эта стадия больше не нужна: новые брони сразу отправляются в группу, "
        "а бот автоматически переводит бронь в «Обработано» через 1 час.",
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_group_"))
def mark_fulfilled_group(call):
    safe_answer_callback_query(
        call.id,
        "Ручная обработка заказов отключена: бронь отправляется в группу сразу и обрабатывается автоматически через 1 час.",
        show_alert=True,
    )

# Хэндлер для очистки корзины
@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_cart_"))
def handle_clear_cart(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    # Получаем ID клиента из callback данных
    client_id = int(call.data.split("_")[2])

    client = Clients.get_row_by_id(client_id)

    if not client:
        bot.send_message(call.message.chat.id, "Клиент не найден.")
        return

    related_clients = Clients.get_rows_by_phone(client.phone)
    deleted_count = sum(
        Reservations.delete_rows_by_user_id(related_client.user_id)
        for related_client in related_clients
    )

    bot.send_message(
        call.message.chat.id,
        f"Корзина клиента успешно расформирована. Удалено позиций: {deleted_count}.",
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
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    # Создаем клавиатуру с кнопками
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🗑 Удалить клиента 📞", "🧺 Просмотреть корзину", "🚚 Управление доставкой","❌ Брак", "⬅️ Назад")
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)

# Обработчик для кнопки "❌ Брак"
@bot.message_handler(func=lambda message: message.text == "❌ Брак")
def defective_order(message):
    if not require_role(message, ADMIN_ROLES):
        return
    # Устанавливаем состояние пользователя
    set_user_state(message.chat.id, "awaiting_last_digits_defective")
    bot.send_message(message.chat.id, "Введите последние 4 цифры номера телефона для поиска пользователя:")

# Поиск пользователя по последним 4 цифрам телефона
def ask_defective_confirmation(chat_id, client):
    keyboard = create_defective_confirmation_keyboard()
    bot.send_message(
        chat_id,
        f"Найден пользователь:\nИмя: {client.name}\nТелефон: {client.phone}\nПродолжить обработку брака для этого пользователя?",
        reply_markup=keyboard,
    )
    temp_user_data[chat_id] = {"user_id": client.user_id}
    set_user_state(chat_id, "awaiting_defective_action")


@bot.callback_query_handler(
    func=lambda call: get_user_state(call.message.chat.id) == "awaiting_defective_client_choice"
    and call.data.startswith("defective_client_")
)
def handle_defective_client_choice(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    client_id = int(call.data.split("_")[2])
    client = Clients.get_row_by_id(client_id)
    if not client:
        safe_answer_callback_query(call.id, "Пользователь не найден.", show_alert=True)
        return

    ask_defective_confirmation(call.message.chat.id, client)
    safe_answer_callback_query(call.id)


@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "awaiting_last_digits_defective")
def search_user_for_defective(message):
    last_digits = message.text.strip()

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
            bot.send_message(
                message.chat.id,
                "Найдено несколько пользователей. Выберите нужного по полному номеру:",
                reply_markup=keyboard,
            )
            set_user_state(message.chat.id, "awaiting_defective_client_choice")
            return

        ask_defective_confirmation(message.chat.id, users[0])
        return
        user = users[0]  # Берем первого пользователя или делаем выбор из нескольких
        user_id = user.user_id
        user_name = user.name
        user_phone = user.phone

        # Отправляем информацию о пользователе и подтверждение
        keyboard = create_defective_confirmation_keyboard()
        bot.send_message(
            message.chat.id,
            f"Найден пользователь:\nИмя: {user_name}\nТелефон: {user_phone}\nВы хотите продолжить обработку для данного пользователя?",
            reply_markup=keyboard
        )

        # Сохраняем user_id для дальнейшей обработки
        temp_user_data[message.chat.id] = {"user_id": user_id}
        set_user_state(message.chat.id, "awaiting_defective_action")
    else:
        bot.send_message(message.chat.id, "Пользователи с такими цифрами номера не найдены. Попробуйте еще раз.")

# Обработка действия (подтверждения или отмены)
@bot.callback_query_handler(func=lambda call: get_user_state(call.message.chat.id) == "awaiting_defective_action")
def handle_defective_action(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    if call.data == "confirm_defective":
        set_user_state(call.message.chat.id, "awaiting_defective_sum")
        bot.send_message(call.message.chat.id, "Введите сумму брака:")
    elif call.data == "cancel_defective":
        bot.send_message(call.message.chat.id, "Операция отменена. Возвращаю вас в главное меню.")
        clear_user_state(call.message.chat.id)
        go_back_to_menu(call.message)

# Ввод суммы брака
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "awaiting_defective_sum")
def handle_defective_sum_entry(message):
    try:
        defective_sum = int(message.text.strip())
        user_id = temp_user_data[message.chat.id]["user_id"]  # Берем найденный user_id

        # Получаем заказы пользователя из таблицы Reservations
        reservations = get_user_reservations(user_id)

        if reservations:
            # Указание места, где будет добавлена сумма брака
            keyboard = create_select_reservation_keyboard(reservations)
            bot.send_message(
                message.chat.id,
                "Выберите заказ, чтобы добавить сумму брака:",
                reply_markup=keyboard
            )
            set_user_state(message.chat.id, "select_reservation_for_defective")
            temp_user_data[message.chat.id]["defective_sum"] = defective_sum
        else:
            bot.send_message(message.chat.id, "Заказы у данного пользователя не найдены. Попробуйте еще раз.")
            clear_user_state(message.chat.id)
            go_back_to_menu(message)
    except ValueError:
        bot.send_message(message.chat.id, "Некорректное значение. Введите числовую сумму.")

# Обработка выбора заказа для дефектного товара
@bot.callback_query_handler(func=lambda call: get_user_state(call.message.chat.id) == "select_reservation_for_defective")
def handle_reservation_selection(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    # Отвечаем на callback_query сразу
    safe_answer_callback_query(call.id, text="Ваш выбор обрабатывается...")

    reservation_id = int(call.data.split("_")[1])  # Получаем ID заказа из callback_data
    defective_sum = temp_user_data[call.message.chat.id]["defective_sum"]
    selected_user_id = temp_user_data[call.message.chat.id]["user_id"]
    related_user_ids = get_related_user_ids_by_full_phone(selected_user_id)

    # Обновляем return_order в базе данных
    with Session(bind=engine) as session:
        reservation = session.query(Reservations).filter_by(id=reservation_id).first()
        if reservation and reservation.user_id in related_user_ids:
            reservation.return_order += defective_sum
            session.commit()
            bot.send_message(call.message.chat.id, f"Сумма брака {defective_sum} успешно добавлена в заказ.")
        else:
            bot.send_message(call.message.chat.id, "Ошибка: Заказ не найден.")

    # Завершаем процесс
    clear_user_state(call.message.chat.id)
    go_back_to_menu(call.message)  # Передаем только сообщение

# Клавиатура для выбора конкретного заказа
def create_select_reservation_keyboard(reservations):
    keyboard = types.InlineKeyboardMarkup()
    for reservation in reservations:
        btn = types.InlineKeyboardButton(
            text=f"Заказ ID {reservation.id} (Возврат: {reservation.return_order})",
            callback_data=f"select_{reservation.id}"
        )
        keyboard.add(btn)
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
    bot.send_message(
        message.chat.id,
        "Введите последние 4 цифры номера телефона клиента:",
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
        "Список Клиентов",
        "🗄 Архив доставки",
        "⬅️ Назад",
    )
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "Список Клиентов")
def show_delivery_clients_summary(message):
    if not require_role(message, DELIVERY_ROLES):
        return

    clients = get_delivery_clients_summary()
    if not clients:
        bot.send_message(
            message.chat.id,
            f"Клиентов с обработанными заказами на {DELIVERY_THRESHOLD} ₽ и более не найдено.",
        )
        return

    messages = build_delivery_clients_summary_messages(clients)
    for text in messages:
        bot.send_message(message.chat.id, text)
        time.sleep(0.2)


# Хэедлнр для поиска по последним 4 цифрам номера
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "AWAITING_PHONE_LAST_4")
def handle_phone_input(message):
    if not require_role(message, ADMIN_ROLES):
        clear_user_state(message.chat.id)
        return
    input_text = message.text.strip()

    # Проверяем, что введены последние 4 цифры номера телефона
    if not input_text.isdigit() or len(input_text) != 4:
        bot.send_message(
            message.chat.id,
            "Введите корректные последние 4 цифры номера телефона (4 цифры).",
        )
        return

    # Показ корзины по последним 4 цифрам номера телефона
    show_cart_by_last_phone_digits(message, input_text)

# Получаем всех клиентов с такими последними цифрами телефона
def show_cart_by_last_phone_digits(message, last_4_digits):
    clients = Clients.get_row_by_phone_digits(last_4_digits)

    if not clients:
        bot.send_message(
            message.chat.id,
            "Пользователи с такими последними цифрами номера телефона не найдены.",
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
        bot.send_message(
            message.chat.id,
            "Найдено несколько клиентов с такими последними цифрами. Выберите нужного:",
            reply_markup=markup,
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


def build_item_list_caption(description, price, quantity, created_at, post_id=None):
    lines = []
    if post_id is not None:
        lines.append(f"Id товара: {post_id}")
    lines.extend([
        f"Описание: {description}",
        f"Цена: {price} ₽",
        f"Количество: {quantity}",
        f"Дата создания: {format_cart_date(created_at)}",
    ])
    return "\n".join(lines)


def show_cart_for_client(chat_id, client):
    related_clients = Clients.get_rows_by_phone(client.phone)
    total_orders = sum(calculate_total_sum(related.user_id) for related in related_clients)
    processed_orders = sum(calculate_processed_sum(related.user_id) for related in related_clients)

    bot.send_message(
        chat_id,
        f"Пользователь: {client.name}\n"
        f"Телефон: {format_admin_phone(client.phone)}\n"
        f"Общая сумма заказов: {total_orders} руб.\n"
        f"Общая сумма обработанных заказов: {processed_orders} руб."
    )

    reservations = get_user_reservations(client.user_id)
    if not reservations:
        bot.send_message(chat_id, f"Корзина пользователя {client.name} пуста.")
        return

    send_cart_content(chat_id, reservations, client.user_id)


def build_cart_item_caption(post, reservation):
    unit_price = reservation_unit_price(reservation, post)
    return build_item_list_caption(
        description=post.description,
        price=unit_price,
        quantity=reservation.quantity,
        created_at=post.created_at,
    )


def empty_cart_cleanup_stats():
    return {
        "deleted": 0,
        "processed_deleted": 0,
        "unprocessed_deleted": 0,
        "temp_deleted": 0,
        "returned": 0,
        "queued": 0,
        "for_delivery_removed": 0,
        "for_delivery_updated": 0,
        "missing_posts": 0,
    }


def merge_cart_cleanup_stats(target, source):
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value or 0)
    return target


def format_cart_cleanup_result(stats):
    if not stats.get("deleted"):
        return "Товар уже удален или корзина пуста."

    parts = [
        f"Удалено позиций: {stats['deleted']}",
        f"обработанных: {stats['processed_deleted']}",
        f"в обработке: {stats['unprocessed_deleted']}",
    ]
    if stats.get("temp_deleted"):
        parts.append(f"очищено обработанных строк: {stats['temp_deleted']}")
    if stats.get("returned"):
        parts.append(f"возвращено в остаток: {stats['returned']}")
    if stats.get("queued"):
        parts.append(f"передано очереди: {stats['queued']}")
    if stats.get("for_delivery_removed"):
        parts.append(f"удалено заявок доставки: {stats['for_delivery_removed']}")
    if stats.get("for_delivery_updated"):
        parts.append(f"обновлено заявок доставки: {stats['for_delivery_updated']}")
    if stats.get("missing_posts"):
        parts.append(f"без карточки товара: {stats['missing_posts']}")
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


def create_reservation_from_queue(session, queued_entry, post):
    queued_client = session.query(Clients).filter(
        Clients.user_id == queued_entry.user_id
    ).first()
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


def send_queue_transfer_notifications(queued_notifications):
    for item in queued_notifications:
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

            bot.send_message(
                chat_id=item["user_id"],
                text="Ваш товар в очереди стал доступен и добавлен в вашу корзину.",
            )
        except Exception as exc:
            logger.warning(
                "Queue transfer notification failed for reservation_id=%s: %s",
                item.get("reservation_id"),
                exc,
            )


def cleanup_or_refresh_for_delivery_by_phone(session, phone):
    stats = empty_cart_cleanup_stats()
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return stats

    related_clients = session.query(Clients).filter(
        Clients.phone.in_(phone_variants(normalized_phone))
    ).all()
    related_user_ids = [client.user_id for client in related_clients]
    if not related_user_ids:
        return stats

    processed_sum = session.query(
        func.sum(
            (func.coalesce(Reservations.old_price, 0) * Reservations.quantity)
            - func.coalesce(Reservations.return_order, 0)
        )
    ).select_from(
        Reservations
    ).filter(
        Reservations.user_id.in_(related_user_ids),
        Reservations.is_fulfilled == True,
    ).scalar()
    processed_sum = int(processed_sum or 0)

    delivery_entries = session.query(ForDelivery).filter(
        or_(
            ForDelivery.phone.in_(phone_variants(normalized_phone)),
            ForDelivery.user_id.in_(related_user_ids),
        )
    ).all()

    if processed_sum < DELIVERY_THRESHOLD:
        for delivery_entry in delivery_entries:
            session.delete(delivery_entry)
        stats["for_delivery_removed"] = len(delivery_entries)
        return stats

    for delivery_entry in delivery_entries:
        if delivery_entry.total_sum != processed_sum:
            delivery_entry.total_sum = processed_sum
            stats["for_delivery_updated"] += 1
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
        next_in_queue = session.query(TempReservations).filter(
            TempReservations.post_id == post_id,
            TempReservations.temp_fulfilled == False,
        ).order_by(TempReservations.created_at).first()

        if next_in_queue and post:
            queued_notifications.append(create_reservation_from_queue(session, next_in_queue, post))
            stats["queued"] += 1
            continue

        if post:
            post.quantity += 1
            stats["returned"] += 1

    return post_id


def remove_processed_reservation_from_cart(session, reservation, stats):
    stats["temp_deleted"] += delete_temp_fulfilled_for_reservation(session, reservation)
    if not session.query(Posts.id).filter(Posts.id == reservation.post_id).first():
        stats["missing_posts"] += 1
    session.delete(reservation)
    stats["deleted"] += 1
    stats["processed_deleted"] += 1
    return reservation.post_id


def remove_reservation_from_cart(session, reservation, stats, queued_notifications):
    post = session.query(Posts).filter(Posts.id == reservation.post_id).first()
    if reservation.is_fulfilled:
        return remove_processed_reservation_from_cart(session, reservation, stats)
    return remove_unprocessed_reservation_from_cart(session, reservation, post, stats, queued_notifications)


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
            send_photo_or_text(bot, chat_id, post.photo, caption, reply_markup=item_markup)
        else:
            bot.send_message(
                chat_id,
                f"Товар с ID {reservation.post_id} не найден!",
                reply_markup=item_markup,
            )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Расформировать обработанные", callback_data=f"clear_processed_{user_id}"))
    markup.add(types.InlineKeyboardButton("Расформировать полностью", callback_data=f"clear_full_cart_{user_id}"))
    bot.send_message(chat_id, "Выберите действие:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_remove_cart_item_"))
def handle_admin_remove_cart_item(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    try:
        reservation_id = int(call.data.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        safe_answer_callback_query(call.id, "Некорректный товар.", show_alert=True)
        return

    try:
        stats = clear_client_cart(call.from_user.id, reservation_id=reservation_id)
    except Exception as exc:
        logger.exception("Admin cart item remove failed for reservation_id=%s", reservation_id)
        safe_answer_callback_query(call.id, "Ошибка при удалении товара.", show_alert=True)
        bot.send_message(call.message.chat.id, f"Ошибка при удалении товара: {exc}")
        return

    if not stats.get("deleted"):
        safe_answer_callback_query(call.id, "Товар уже удален.", show_alert=True)
        return

    safe_answer_callback_query(call.id, "Товар убран из корзины.")
    safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)
    bot.send_message(call.message.chat.id, format_cart_cleanup_result(stats))


# Callback для кнопки "Расформировать обработанные"
@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_processed_"))
def handle_clear_processed(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    user_id = int(call.data.split("_")[2])  # Извлекаем ID пользователя из callback_data

    try:
        stats = clear_processed(user_id)
    except Exception as exc:
        logger.exception("Clear processed cart failed for user_id=%s", user_id)
        safe_answer_callback_query(call.id, "Ошибка при расформировании обработанных.", show_alert=True)
        bot.send_message(call.message.chat.id, f"Ошибка при расформировании обработанных: {exc}")
        return

    if stats.get("deleted", 0) > 0:
        safe_answer_callback_query(call.id, f"Удалено обработанных товаров: {stats['deleted']}.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            f"Обработанные товары удалены из корзины. {format_cart_cleanup_result(stats)}",
        )
    else:
        safe_answer_callback_query(call.id, "В корзине нет обработанных товаров для удаления.", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_full_cart_"))
def handle_clear_full_cart(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return

    try:
        user_id = int(call.data.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        safe_answer_callback_query(call.id, "Некорректный клиент.", show_alert=True)
        return

    try:
        stats = clear_client_cart(user_id, processed_only=False)
    except Exception as exc:
        logger.exception("Clear full cart failed for user_id=%s", user_id)
        safe_answer_callback_query(call.id, "Ошибка при полном расформировании.", show_alert=True)
        bot.send_message(call.message.chat.id, f"Ошибка при полном расформировании: {exc}")
        return

    if stats.get("deleted", 0) > 0:
        safe_answer_callback_query(call.id, f"Корзина расформирована. Удалено: {stats['deleted']}.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            f"Корзина полностью расформирована. {format_cart_cleanup_result(stats)}",
        )
    else:
        safe_answer_callback_query(call.id, "Корзина уже пуста.", show_alert=True)


# Удаляет обработанные товары из корзины пользователя
def clear_processed(user_id):
    return clear_client_cart(user_id, processed_only=True)


# Callback для инлайн-кнопок "Просмотреть корзину"
@bot.callback_query_handler(func=lambda call: call.data.startswith("view_cart_"))
def callback_view_cart(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    try:
        client_id = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        safe_answer_callback_query(call.id, "Некорректный выбор клиента.", show_alert=True)
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
        bot.send_message(user_id, "У вас недостаточно прав.")
        return
    bot.send_message(message.chat.id, "Введите номер телефона клиента для удаления:")
    set_user_state(message.chat.id, "DELETE_CLIENT_PHONE")

# Функция для удаления клиента по номеру телефона
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "DELETE_CLIENT_PHONE")
def process_delete_client_phone(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем права пользователя
    if role not in ["admin","supreme_leader"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    phone = normalize_phone(message.text)  # Убираем лишние пробелы

    try:
        # Получаем клиента по номеру телефона
        client = Clients.get_row_by_phone(phone)

        if client:
            client_user_id = client.user_id  # Извлекаем user_id клиента

            # Проверяем, не выполняются ли действия с защищённым пользователем
            if client_user_id == protected_user_id:
                bot.send_message(
                    user_id, f"Клиент с номером телефона {phone} защищен от удаления."
                )
                return

            # Добавляем клиента в черный список (защищенный пользователь не будет добавлен)
            if client_user_id != protected_user_id:
                BlackList.insert(user_id=client_user_id, phone=phone)

            # Удаляем клиента из таблицы reservations
            # Используем SQLAlchemy напрямую или другую существующую логику для удаления
            with Session(bind=engine) as session:
                deleted_reservations_count = session.query(Reservations).filter(
                    Reservations.user_id == client_user_id
                ).delete()
                session.commit()

            # Удаляем клиента из таблицы clients
            Clients.delete_row(client.id)

            bot.send_message(
                user_id,
                f"Клиент с номером телефона {phone} успешно удалён. "
                f"Связанных записей в таблице reservations удалено: {deleted_reservations_count}.",
            )
        else:
            bot.send_message(user_id, f"Клиент с номером телефона {phone} не найден.")
    except Exception as e:
        # Сообщаем об ошибке
        bot.send_message(user_id, f"Произошла ошибка при удалении данных: {e}")
    finally:
        clear_user_state(user_id)

# Возможность установить клиенту статус рабочего
@bot.callback_query_handler(func=lambda call: call.data.startswith("set_worker_") or call.data.startswith("set_client_"))
def handle_set_role(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    client_id = int(call.data.split("_")[2])
    new_role = "worker" if "set_worker" in call.data else "client"

    client = Clients.get_row_by_id(client_id)

    if not client:
        safe_answer_callback_query(call.id, f"Клиент с ID {client_id} не найден.")
        return

    update_result = Clients.update_row_for_work(client.user_id, {"role": new_role})

    if update_result:
        safe_answer_callback_query(call.id, f"Роль успешно изменена на {new_role}.")
        bot.send_message(
            call.message.chat.id,
            f"Роль пользователя с ID {client_id} обновлена на {new_role}.",
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

# Новый пост
@bot.message_handler(func=lambda message: message.text == "➕ Новый пост")
def create_new_post(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    if role not in ["worker", "admin", "supreme_leader", "audit"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    try:
        reserved_post_id = Posts.reserve_next_id(chat_id=user_id)
    except Exception as exc:
        logger.exception("Post ID reservation failed for user_id=%s: %s", user_id, exc)
        bot.send_message(user_id, "Не удалось зарезервировать ID товара. Попробуйте ещё раз.")
        return

    bot.send_message(
        message.chat.id,
        f"Id товара: {reserved_post_id}\n"
        "Отправьте фото",
    )
    temp_post_data[message.chat.id] = {"post_id": reserved_post_id}
    set_user_state(message.chat.id, CreatingPost.CREATING_POST)

# Фото
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.chat.id
    role = get_client_role(user_id)
    state = get_user_state(message.chat.id)
    if role not in ["worker", "admin","supreme_leader", "audit"]:
        bot.send_message(
            user_id, "Если у вас возникли вопросы, задайте их в чате поддержки"
        )
        return
    if state == CreatingPost.CREATING_POST:
        temp_post_data[message.chat.id]["photo"] = message.photo[-1].file_id
        bot.send_message(message.chat.id, "Теперь введите цену на товар.")
    else:
        bot.send_message(message.chat.id, "Неправильная последовательность действий")

# Описание
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.CREATING_POST)
def handle_post_details(message):
    chat_id = message.chat.id
    if "photo" in temp_post_data[chat_id] and "price" not in temp_post_data[chat_id]:
        if not message.text.isdigit():
            bot.send_message(
                chat_id, "Ошибка: Цена должна быть числом. Попробуйте снова."
            )
            return
        temp_post_data[chat_id]["price"] = message.text
        bot.send_message(chat_id, "Введите описание товара.")
    elif (
            "price" in temp_post_data[chat_id]
            and "description" not in temp_post_data[chat_id]
    ):
        # Поле "description" сохраняем без проверки, но заменяем "*" на "x"
        description = message.text.replace("*", "x")
        temp_post_data[chat_id]["description"] = description
        bot.send_message(chat_id, "Введите количество товара.")
    elif (
            "description" in temp_post_data[chat_id]
            and "quantity" not in temp_post_data[chat_id]
    ):
        if not message.text.isdigit():
            bot.send_message(
                chat_id, "Ошибка: Количество должно быть числом. Попробуйте снова."
            )
            return
        quantity = int(message.text)
        if quantity <= 0:
            bot.send_message(chat_id, "Ошибка: Количество должно быть больше нуля. Попробуйте снова.")
            return
        temp_post_data[chat_id]["quantity"] = quantity

        # Сохраняем пост
        data = temp_post_data[chat_id]
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
            bot.send_message(chat_id, "Не удалось сохранить пост. Нажмите «Новый пост» и попробуйте ещё раз.")
            return

        bot.send_message(
            chat_id,
            f"Id товара: {created_post_id}\n"
            "Ваш пост успешно создан!",
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
        print(f"Не удалось удалить сообщение-запрос пользователя {user_id}: {e}")

    role = get_client_role(user_id)

    # Проверяем, имеет ли пользователь соответствующую роль
    if role not in ["admin", "worker", "supreme_leader", "audit"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
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
            print(f"Не удалось удалить сообщение {msg_id} для пользователя {user_id}: {e}")

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
        error_msg = bot.send_message(user_id, f"Ошибка получения постов: {e}")
        user_last_message_id[user_id].append(error_msg.message_id)
        return

    if not posts:
        no_posts_msg = bot.send_message(user_id, "Нет доступных постов.")
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
                msg = bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=f"Пост #{post_id}:\n"
                            f"📍 Описание: {description}\n"
                            f"💰 Цена: {price} ₽\n"
                            f"📦 Количество: {quantity}",
                    reply_markup=markup,
                )
            else:
                msg = bot.send_message(
                    chat_id=user_id,
                    text=f"Пост #{post_id}:\n"
                         f"📍 Описание: {description}\n"
                         f"💰 Цена: {price} ₽\n"
                         f"📦 Количество: {quantity}",
                    reply_markup=markup,
                )
            # Сохраняем ID отправленных сообщений
            user_last_message_id[user_id].append(msg.message_id)
        except Exception as e:
            error_msg = bot.send_message(user_id, f"Ошибка при отправке поста #{post_id}: {e}")
            user_last_message_id[user_id].append(error_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_post_"))
def edit_post(call):
    post_id = int(call.data.split("_")[2])  # Получаем ID поста из callback_data
    user_id = call.from_user.id

    # Проверяем права на редактирование
    role = get_client_role(user_id)
    if role not in ["admin", "worker", "supreme_leader", "audit"]:
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
        msg = bot.send_message(
            chat_id=call.message.chat.id,
            text="Что вы хотите поменять?",
            reply_markup=markup
        )
        user_last_message_id.setdefault(user_id, []).append(msg.message_id)  # Сохраняем ID сообщения

# Обработчик кнопки "Редактировать цену"
@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_price_"))
def handle_edit_price(call):
    user_id = call.from_user.id
    post_id = int(call.data.split("_")[2])  # Получаем ID поста

    # Устанавливаем состояние пользователя
    set_user_state(user_id, CreatingPost.EDITING_POST_PRICE)
    temp_post_data[user_id] = {"post_id": post_id}

    # Просим пользователя ввести новую цену
    bot.send_message(user_id, "Введите новую цену для поста:")

# Обработчик кнопки "Редактировать описание"
@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_description_"))
def handle_edit_description(call):
    user_id = call.from_user.id
    post_id = int(call.data.split("_")[2])  # Получаем ID поста

    # Устанавливаем состояние пользователя
    set_user_state(user_id, CreatingPost.EDITING_POST_DESCRIPTION)
    temp_post_data[user_id] = {"post_id": post_id}

    # Просим ввести новое описание
    bot.send_message(user_id, "Введите новое описание для поста:")

# Обработчик кнопки "Редактировать количество"
@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_quantity_"))
def handle_edit_quantity(call):
    user_id = call.from_user.id
    post_id = int(call.data.split("_")[2])  # Получаем ID поста

    # Устанавливаем состояние пользователя
    set_user_state(user_id, CreatingPost.EDITING_POST_QUANTITY)
    temp_post_data[user_id] = {"post_id": post_id}

    # Просим ввести новое количество
    bot.send_message(user_id, "Введите новое количество товара:")

# Обработчик ввода новой цены
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.EDITING_POST_PRICE)
def edit_post_price(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id]["post_id"]  # Получаем ID поста

    # Проверка, что введено число
    if not message.text.isdigit():
        bot.send_message(user_id, "Ошибка: Цена должна быть числом. Попробуйте снова.")
        return

    new_price = int(message.text)
    temp_post_data[user_id]["price"] = new_price

    try:
        post = Posts.get_row_by_id(post_id)  # Получаем старые данные поста
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
            bot.send_message(user_id, "Цена успешно обновлена!")
        else:
            bot.send_message(user_id, f"Ошибка обновления цены: {msg}")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка обновления цены: {e}")
    finally:
        clear_user_state(user_id)  # Сбрасываем состояние пользователя

# Обработчик ввода нового описания
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.EDITING_POST_DESCRIPTION)
def edit_post_description(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id]["post_id"]  # Получаем ID поста

    new_description = message.text  # Новое описание
    temp_post_data[user_id]["description"] = new_description

    try:
        post = Posts.get_row_by_id(post_id)  # Получаем старые данные поста
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
            bot.send_message(user_id, "Описание успешно обновлено!")
        else:
            bot.send_message(user_id, f"Ошибка обновления описания: {msg}")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка обновления описания: {e}")
    finally:
        clear_user_state(user_id)  # Сбрасываем состояние пользователя

# Обработчик ввода нового количества
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == CreatingPost.EDITING_POST_QUANTITY)
def edit_post_quantity(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id]["post_id"]  # Получаем ID поста

    # Проверяем, что ввод является числом
    if not message.text.isdigit():
        bot.send_message(user_id, "Ошибка: Количество должно быть числом. Попробуйте снова.")
        return

    new_quantity = int(message.text)
    temp_post_data[user_id]["quantity"] = new_quantity

    try:
        post = Posts.get_row_by_id(post_id)  # Получаем старые данные
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
            bot.send_message(user_id, "Количество успешно обновлено!")
        else:
            bot.send_message(user_id, f"Ошибка обновления количества: {msg}")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка обновления количества: {e}")
    finally:
        clear_user_state(user_id)  # Очистка состояния

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_post_"))
def delete_post_handler(call):
    post_id = int(call.data.split("_")[2])  # Извлечение ID поста
    try:
        with Session(bind=engine) as session:
            post = session.query(Posts).filter(Posts.id == post_id).first()
            if not post:
                safe_answer_callback_query(call.id, "Пост не найден.", show_alert=True)
                return

            has_related_rows = any([
                session.query(Reservations.id).filter(Reservations.post_id == post_id).first(),
                session.query(TempReservations.id).filter(TempReservations.post_id == post_id).first(),
                session.query(Temp_Fulfilled.id).filter(Temp_Fulfilled.post_id == post_id).first(),
                session.query(InDelivery.id).filter(InDelivery.post_id == post_id).first(),
            ])
            if has_related_rows:
                post.quantity = 0
                session.commit()
                disable_channel_post_reservation(post)
                safe_answer_callback_query(
                    call.id,
                    "Пост связан с заказами, поэтому карточка оставлена в базе, а бронь в канале отключена.",
                    show_alert=True,
                )
                safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)
                return

        if post and post.message_id:
            deleted_from_channel = safe_delete_message(bot, CHANNEL_ID, post.message_id, logger=logger)
            if not deleted_from_channel:
                disable_channel_post_reservation(post)

        # Удалить пост из базы данных (если успешно)
        result, msg = Posts.delete_row(post_id=post_id)
        if result:
            # Сообщаем о результате
            safe_answer_callback_query(call.id, "Пост успешно удалён.")

            safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)
        else:
            # Возникает ошибка при удалении поста
            safe_answer_callback_query(call.id, f"Ошибка: {msg}")
    except Exception as e:
        # Обработка исключений, если что-то пошло не так
        safe_answer_callback_query(call.id, f"Ошибка: {e}")

# Кнопка назад
@bot.message_handler(func=lambda message: message.text == "⬅️ Назад")
def go_back(message):
    try:
        # Проверяем роль пользователя и возвращаем соответствующее меню
        if is_admin(message.chat.id):
            markup = admin_main_menu()  # Получаем меню администратора
            bot.send_message(
                message.chat.id, "Возвращаюсь в главное меню администратора.", reply_markup=markup
            )
        elif is_leader(message.chat.id):
            markup = supreme_leader_main_menu()  # Получаем меню лидера
            bot.send_message(
                message.chat.id, "Возвращаюсь в главное меню лидера.", reply_markup=markup
            )
        elif is_audit(message.chat.id):
            markup = audit_main_menu()
            bot.send_message(
                message.chat.id,"Возвращаюсь в главное меню", reply_markup=markup
            )
        else:
            markup = client_main_menu()  # Получаем меню клиента
            bot.send_message(
                message.chat.id, "Возвращаюсь в главное меню.", reply_markup=markup
            )
    except Exception as e:
        # При возникновении исключения отправляем сообщение об ошибке
        print(f"Ошибка при обработке команды '⬅️ Назад': {e}")
        bot.send_message(
            message.chat.id, "Произошла ошибка. Пожалуйста, попробуйте снова позже."
        )


def send_channel_post_with_retries(post, caption, markup, source):
    for attempt in range(1, CHANNEL_POST_SEND_ATTEMPTS + 1):
        try:
            return bot.send_photo(
                CHANNEL_ID,
                photo=post.photo,
                caption=caption,
                reply_markup=markup,
            )
        except Exception as exc:
            retry_after = telegram_retry_after_seconds(exc)
            can_retry = retry_after is not None or is_connect_timeout_error(exc)
            if not can_retry or attempt == CHANNEL_POST_SEND_ATTEMPTS:
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
            photo = post.photo
            creator_name = Clients.get_name_by_user_id(post.chat_id) or "Неизвестный автор"
            caption = build_channel_post_caption(post)
            markup = build_channel_post_markup(post)

            try:
                sent_message = send_channel_post_with_retries(post, caption, markup, source)
            except Exception as exc:
                failed_post_ids.append(post_id)
                logger.exception("Channel post send failed for post_id=%s source=%s: %s", post_id, source, exc)
                time.sleep(1)
                continue

            Posts.mark_as_sent(post_id=post_id, message_id=sent_message.message_id)
            sent_count += 1

            group_caption = f"Пост был создан пользователем: {creator_name}\n\n{caption}"
            try:
                bot.send_photo(ARCHIVE, photo=photo, caption=group_caption)
            except Exception as exc:
                logger.warning("Archive copy failed for post_id=%s: %s", post_id, exc)

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
        notify_publish_status(notify_chat_id, f"Ошибка при отправке постов: {exc}")
        return sent_count
    finally:
        channel_post_publish_lock.release()


def send_phoenix_channel_footer():
    try:
        with open(PHOENIX_QR_PATH, "rb") as photo:
            bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo,
                caption=PHOENIX_BROADCAST_TEXT,
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
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
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
    total_posts = {"today": 0, "week": 0, "last_week": 0}
    total_revisions = {"today": 0, "week": 0, "last_week": 0}

    # Получение данных из базы данных
    all_posts = Posts.get_row_all()  # Получаем все посты
    all_clients = Clients.get_row_all()  # Получаем всех клиентов
    with Session(bind=engine) as session:
        revision_logs = session.query(RevisionLog).all()

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

    # Генерация статистики постов
    for key, date_range in days_range.items():
        for post in all_posts:
            if not post.created_at:
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

        for revision_log in revision_logs:
            if not revision_log.created_at:
                continue
            revision_date = revision_log.created_at.date()
            if date_range[0] <= revision_date <= date_range[1]:
                auditor_name = clients_dict.get(revision_log.auditor_user_id, "Неизвестный пользователь")
                if auditor_name not in revision_statistics[key]:
                    revision_statistics[key][auditor_name] = 0
                revision_statistics[key][auditor_name] += 1
                total_revisions[key] += 1

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
            response += f"  - {name}: {count} постов\n"

    response += f"\nОбщее количество созданных постов:\n"
    response += f"  - Сегодня: {total_posts['today']} постов\n"
    response += f"  - На этой неделе: {total_posts['week']} постов\n"
    response += f"  - На прошлой неделе: {total_posts['last_week']} постов\n"

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

    bot.send_message(message.chat.id, response)

# Обработчик для кнопки 'Отправить рассылку'.
@bot.message_handler(func=lambda message: message.text == "📤 Отправить рассылку")
def send_broadcast(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    user_id = message.from_user.id
    bot.send_message(chat_id=user_id, text="Начинаю рассылку подходящим клиентам...")
    try:
        auto_fulfill_expired_reservations()
        eligible_users = calculate_for_delivery()
        send_delivery_candidates_summary(eligible_users)
        logger.info("Delivery broadcast candidates: %s", len(eligible_users))

        if eligible_users:
            for user in eligible_users:
                try:
                    send_delivery_offer(bot, user["user_id"], user["name"])
                    time.sleep(1)
                except Exception as e:
                    logger.warning("Delivery offer failed for user_id=%s: %s", user["user_id"], e)
            bot.send_message(chat_id=user_id, text=f"Рассылка отправлена. Клиентов: {len(eligible_users)}.")
        else:
            bot.send_message(chat_id=user_id, text="Подходящих клиентов для рассылки не найдено.")
    except Exception as e:
        bot.send_message(chat_id=user_id, text=f"Ошибка при выполнении рассылки: {str(e)}")


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
    with open(PHOENIX_QR_PATH, "rb") as photo:
        bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=PHOENIX_BROADCAST_TEXT,
        )


def run_phoenix_broadcast(creator_user_id):
    sent_count = 0
    failed_count = 0
    recipients = []

    try:
        recipients = get_phoenix_broadcast_recipients()
        bot.send_message(
            creator_user_id,
            f"Начинаю рассылку о Фениксе. Получателей: {len(recipients)}. "
            f"Задержка: {PHOENIX_BROADCAST_DELAY_SECONDS} сек.",
        )

        if not os.path.exists(PHOENIX_QR_PATH):
            bot.send_message(creator_user_id, "QR-код не найден. Рассылка остановлена.")
            return

        for index, recipient_id in enumerate(recipients, start=1):
            try:
                send_phoenix_message(recipient_id)
                sent_count += 1
            except Exception as exc:
                failed_count += 1
                logger.warning("Phoenix broadcast failed for user_id=%s: %s", recipient_id, exc)

            if index % PHOENIX_BROADCAST_BATCH_SIZE == 0 or index == len(recipients):
                bot.send_message(
                    creator_user_id,
                    f"Рассылка о Фениксе: обработано {index}/{len(recipients)}, "
                    f"отправлено {sent_count}, ошибок {failed_count}.",
                )

            if index < len(recipients):
                time.sleep(PHOENIX_BROADCAST_DELAY_SECONDS)
                if PHOENIX_BROADCAST_BATCH_SIZE > 0 and index % PHOENIX_BROADCAST_BATCH_SIZE == 0:
                    time.sleep(PHOENIX_BROADCAST_BATCH_PAUSE_SECONDS)

    except Exception as exc:
        logger.exception("Phoenix broadcast crashed: %s", exc)
        bot.send_message(creator_user_id, f"Рассылка о Фениксе остановлена из-за ошибки: {exc}")
    finally:
        phoenix_broadcast_lock.release()
        if recipients:
            bot.send_message(
                creator_user_id,
                f"Рассылка о Фениксе завершена. Отправлено: {sent_count}, ошибок: {failed_count}.",
            )


@bot.message_handler(func=lambda message: message.text == PHOENIX_BROADCAST_BUTTON)
def start_phoenix_broadcast(message):
    if not require_creator(message):
        return

    if not phoenix_broadcast_lock.acquire(blocking=False):
        bot.send_message(message.chat.id, "Рассылка о Фениксе уже выполняется.")
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


# Обрабатывает ответ пользователя на предложение доставки с инлайн-клавиатуры.
@bot.callback_query_handler(
    func=lambda call: call.data in ["delivery_yes", "delivery_no"] or is_legacy_delivery_callback(call)
)
def handle_delivery_response_callback(call):
    # Получаем данные пользователя
    user_id = call.from_user.id
    message_id = call.message.message_id  # ID сообщения с кнопками
    response = call.data
    if response == "yes":
        response = "delivery_yes"
    elif response == "no":
        response = "delivery_no"

    # Проверяем текущее время
    current_time = now_local().time()  # Текущее самарское время

    if response == "delivery_yes" and current_time.hour >= 16:
        # Если нажато "Да" после 14:00 — удаляем сообщение с кнопками
        safe_delete_message(bot, user_id, message_id, logger=logger)
        # Отправляем сообщение об отказе
        bot.send_message(chat_id=user_id,
                         text="Извините, но лист на доставку уже сформирован. Ожидайте следующую отправку.")
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
        delivery_cutoff_at = (
            existing_cutoff
            if current_state in active_delivery_states and existing_cutoff
            else now_local()
        )
        temp_user_data[user_id] = {
            **existing_temp,
            "delivery_cutoff_at": serialize_datetime(delivery_cutoff_at),
        }

        # Если согласие до 16:00, запрашиваем адрес
        bot.send_message(chat_id=user_id, text="Пожалуйста, укажите город, адрес и подъезд")
        # Сохраняем состояние пользователя для дальнейшего ввода адреса
        set_user_state(user_id, "WAITING_FOR_ADDRESS")
    elif response == "delivery_no":
        # Если отказ, удаляем сообщение с кнопками и уведомляем об ожидании следующей доставки
        safe_delete_message(bot, user_id, message_id, logger=logger)
        bot.send_message(chat_id=user_id, text="Вы отказались от доставки. Оповестим вас при следующей доставке.")

    # Уведомляем Telegram, что callback обработан
    safe_answer_callback_query(call.id)

# Обрабатывает ввод адреса пользователя.
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "WAITING_FOR_ADDRESS")
def handle_address_input(message):
    user_id = message.chat.id
    address = message.text
    logger.info("Delivery address received for user_id=%s", user_id)
    # Проверяем наличие данных о пользователе
    user_data = Clients.get_row_by_user_id(user_id)
    if not user_data:
        logger.warning("Delivery address received for unknown user_id=%s", user_id)
        bot.send_message(chat_id=user_id, text="Ошибка! Данные пользователя отсутствуют.")
        return
    name = user_data.name
    phone = user_data.phone
    logger.debug("Delivery user data loaded for user_id=%s", user_id)
    existing_temp = temp_user_data.get(user_id, {})
    if not isinstance(existing_temp, dict):
        existing_temp = {}
    delivery_cutoff_at = parse_datetime(existing_temp.get("delivery_cutoff_at")) or now_local()
    # Вычисление суммы заказов пользователя
    user_orders_sum = calculate_sum_for_user(user_id, cutoff_at=delivery_cutoff_at)
    logger.debug("Delivery fulfilled order sum loaded for user_id=%s", user_id)
    # Поиск всех пользователей с таким же телефоном
    from db import Session, engine
    with Session(bind=engine) as session:
        same_phone_users = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(phone))
        ).all()
    if not same_phone_users:
        logger.warning("No related delivery users found for user_id=%s", user_id)
        bot.send_message(chat_id=user_id, text="Ошибка! Не удалось найти других заказов с данным номером телефона.")
        return
    # Подсчет общей суммы всех заказов
    total_sum_by_phone = user_orders_sum
    all_user_orders_details = []
    for client in same_phone_users:
        client_sum = calculate_sum_for_user(client.user_id, cutoff_at=delivery_cutoff_at)
        all_user_orders_details.append({
            "name": client.name,
            "orders_sum": client_sum
        })
        if client.user_id != user_id:
            total_sum_by_phone += client_sum
    logger.debug("Delivery total sum loaded for user_id=%s", user_id)
    # Генерация текста для подтверждения
    orders_details_text = f"Ваши заказы: {user_orders_sum}\n"
    for detail in all_user_orders_details:
        if detail["name"] != name:
            orders_details_text += f"{detail['name']}: {detail['orders_sum']}\n"
    orders_details_text += f"Общая сумма: {total_sum_by_phone}"
    # Отправляем подтверждающее сообщение
    bot.send_message(
        chat_id=user_id,
        text=f"Ваши данные:\nИмя: {name}\nТелефон: {phone}\nАдрес: {address}\n\n{orders_details_text}\n\nПодтверждаете?",
        reply_markup=keyboard_for_confirmation()
    )
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
        bot.send_message(chat_id=user_id, text="Ваши данные на доставку были удалены.")
    else:
        logger.warning("Temporary delivery data not found for user_id=%s", user_id)
        bot.send_message(chat_id=user_id, text="Нет данных для удаления.")

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
                Reservations.fulfilled_at != None,
                Reservations.fulfilled_at <= cutoff_at,
            )
        result = query.first()

        return result.final_sum if result.final_sum else 0


def delivery_target_label(now=None):
    now = now or now_local()
    return "Доставка на понедельник" if now.weekday() == 5 else "Доставка на завтра"


def send_delivery_candidates_summary(eligible_users):
    if not eligible_users:
        text = "📤 Рассылка доставки: подходящих клиентов с суммой 1500+ не найдено."
    else:
        lines = [
            "📤 Клиенты для рассылки доставки",
            f"Всего клиентов: {len(eligible_users)}",
            "",
        ]
        for index, user in enumerate(eligible_users, start=1):
            lines.append(
                f"{index}. {user.get('name') or 'Имя не указано'} | "
                f"{user.get('phone') or 'Телефон не указан'} | "
                f"{user.get('total_amount', 0)} ₽"
            )
        text = "\n".join(lines)

    try:
        bot.send_message(chat_id=delivery_channel, text=text)
    except Exception as e:
        print(f"[ERROR] Ошибка отправки списка клиентов для рассылки: {e}")


def upsert_for_delivery_entry(session, user_id, name, phone, address, total_sum, delivery_cutoff_at=None):
    deduplicate_for_delivery_entries(session)
    normalized_phone = normalize_phone(phone)
    delivery_cutoff_at = delivery_cutoff_at or now_local()
    delivery_entry = session.query(ForDelivery).filter(ForDelivery.phone == normalized_phone).first()

    if delivery_entry:
        delivery_entry.user_id = user_id
        delivery_entry.name = name
        delivery_entry.phone = normalized_phone or phone
        if address:
            delivery_entry.address = address
        elif delivery_entry.address is None:
            delivery_entry.address = ""
        delivery_entry.total_sum = total_sum
        if delivery_entry.delivery_cutoff_at is None:
            delivery_entry.delivery_cutoff_at = delivery_cutoff_at
        return delivery_entry

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
        if entry.delivery_cutoff_at and (
            existing.delivery_cutoff_at is None
            or entry.delivery_cutoff_at < existing.delivery_cutoff_at
        ):
            existing.delivery_cutoff_at = entry.delivery_cutoff_at
        session.delete(entry)

@bot.message_handler(func=lambda message: message.text == "👨‍🦯 Засунуть в доставку")
def push_in_delivery(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    # Шаг 1. Запрос списка номеров у пользователя
    msg = bot.send_message(message.chat.id, "Введите номера телефонов, каждый с новой строки:")
    bot.register_next_step_handler(msg, process_numbers)


def process_numbers(message):
    if not has_role(message.from_user.id, DELIVERY_ROLES):
        bot.send_message(message.chat.id, "У вас недостаточно прав для этой команды.")
        return
    try:
        # Шаг 2. Извлечение списка номеров телефонов
        numbers = message.text.splitlines()
        phone_numbers = [normalize_phone(num) for num in numbers if is_phone_valid(num)]

        if not phone_numbers:
            bot.send_message(message.chat.id, "Список номеров пуст. Попробуйте снова.")
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
                    bot.send_message(message.chat.id, f"Клиент с номером {phone} не найден.")
                    continue

                # Найти выполненные заказы клиента
                related_user_ids = [client_row.user_id for client_row in clients]
                reservations = get_delivery_reservations_query(
                    session,
                    related_user_ids,
                    delivery_cutoff_at,
                ).all()

                if not reservations:
                    bot.send_message(message.chat.id, f"У клиента {phone} нет обработанных заказов в текущем срезе доставки.")
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
                        bot.send_message(message.chat.id, f"Ошибка при добавлении данных клиента {phone}: {str(e)}")
                else:
                    bot.send_message(message.chat.id, f"У клиента {phone} нет товаров для добавления в доставку.")

        # Шаг 4. Уведомление о результатах
        if successful_deliveries:
            bot.send_message(
                message.chat.id,
                f"Заказы для следующих номеров успешно добавлены в доставку: {', '.join(successful_deliveries)}"
            )
        else:
            bot.send_message(message.chat.id, "Не удалось добавить заказы в доставку.")
    except Exception as e:
        bot.send_message(message.chat.id, f"Произошла ошибка: {str(e)}")


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
        bot.send_message(message.chat.id, "Нет данных для архивации.")
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
    document =  InputFile(output, file_name=file_name)

    # Отправка файла в канал delivery_archive
    bot.send_document(chat_id=delivery_archive, document=document)

    # Уведомление пользователя об отправке
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("🧹 Очистить in_delivery", callback_data="archive_delivery_clear_yes")
    )
    keyboard.add(
        InlineKeyboardButton("Оставить записи", callback_data="archive_delivery_clear_no")
    )
    bot.send_message(
        message.chat.id,
        "Архив доставок отправлен в канал. Очистить таблицу in_delivery?",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: call.data in ["archive_delivery_clear_yes", "archive_delivery_clear_no"])
def handle_archive_delivery_clear_confirmation(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    if call.data == "archive_delivery_clear_no":
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Записи in_delivery оставлены без изменений.",
        )
        safe_answer_callback_query(call.id)
        return

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
    safe_answer_callback_query(call.id)


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


def post_has_active_links(session, post_id):
    checks = [
        session.query(Reservations.id).filter(Reservations.post_id == post_id).first(),
        session.query(InDelivery.id).filter(InDelivery.post_id == post_id).first(),
        session.query(Temp_Fulfilled.id).filter(Temp_Fulfilled.post_id == post_id).first(),
        session.query(TempReservations.id).filter(TempReservations.post_id == post_id).first(),
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

        for post in midnight_posts:
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
    try:
        # Удаляем сообщение рассылки
        safe_delete_message(bot, call.message.chat.id, call.message.message_id, logger=logger)

        # Отправляем уведомление пользователю
        bot.send_message(chat_id=call.message.chat.id,
                         text="Вы отказались от доставки. Оповестим вас при следующей доставке.")

        # Отвечаем на Callback, чтобы Telegram понял, что она обработана
        safe_answer_callback_query(callback_query_id=call.id)
    except Exception as e:
        logger.warning("Delivery cancel callback failed for user_id=%s: %s", call.from_user.id, e)

@bot.callback_query_handler(func=lambda call: get_user_state(call.from_user.id) == "WAITING_FOR_DATA_EDIT")
def handle_data_editing(call):
    user_id = call.from_user.id
    action = call.data


    if action == "new_phone":
        set_user_state(user_id, "WAITING_FOR_NEW_PHONE")
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text="Введите новый номер телефона:"
        )
    elif action == "edit_address":
        set_user_state(user_id, "WAITING_FOR_NEW_ADDRESS")
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text="Введите новый адрес доставки:"
        )
    else:
        logger.warning("Unknown delivery edit action=%s for user_id=%s", action, user_id)

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id) == "WAITING_FOR_NEW_ADDRESS")
def handle_new_address(message):
    """
    Обработка нового адреса от пользователя.
    """
    user_id = message.from_user.id
    new_address = message.text
    temp_user_data[user_id]["address"] = new_address  # Сохранение нового адреса

    # Получаем временные данные пользователя
    name = temp_user_data[user_id].get("name", "Не указано")
    phone = temp_user_data[user_id].get("phone", "Не указан")
    final_sum = temp_user_data[user_id].get("final_sum", 0)
    delivery_cutoff_at = parse_datetime(temp_user_data[user_id].get("delivery_cutoff_at")) or now_local()

    # Получаем всех клиентов с таким же номером телефона
    from db import Session, engine
    with Session(bind=engine) as session:
        same_phone_users = session.query(Clients).filter(
            Clients.phone.in_(phone_variants(phone))
        ).all()

    # Считаем общую сумму заказов и собираем имена всех клиентов
    total_sum_by_phone = final_sum
    all_names = [name]  # Добавляем текущее имя
    for client in same_phone_users:
        if client.user_id != user_id:  # Пропускаем текущего клиента
            all_names.append(client.name)
            total_sum_by_phone += calculate_sum_for_user(client.user_id, cutoff_at=delivery_cutoff_at)

    # Формируем строку с именами всех клиентов
    all_names_str = ", ".join(all_names)

    # Отправляем обновлённое сообщение с данными
    bot.send_message(
        chat_id=user_id,
        text=(
            f"Данные обновлены:\n"
            f"Имя: {name}\nТелефон: {phone}\nНовый адрес: {new_address}\n"
            f"Имена заказчиков: {all_names_str}\n"
            f"Общая сумма заказов: {total_sum_by_phone}.\n\n"
            f"Подтверждаете изменения?"
        ),
        reply_markup=keyboard_for_confirmation()
    )
    set_user_state(user_id, "WAITING_FOR_CONFIRMATION")

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id) == "WAITING_FOR_NEW_PHONE")
def handle_new_phone(message):
    """
    Обработка нового номера телефона пользователя.
    Должен учитывать информацию по старому номеру телефона и временно сохранять новый номер.
    """
    user_id = message.from_user.id
    new_phone = normalize_phone(message.text)  # Убираем лишние пробелы
    if not is_phone_valid(new_phone):
        bot.send_message(user_id, "Введите корректный номер телефона.")
        return

    # Временные данные текущего пользователя
    name = temp_user_data[user_id].get("name", "Не указано")
    current_phone = temp_user_data[user_id].get("phone", "Не указан")  # Это старый номер телефона
    address = temp_user_data[user_id].get("address", "Не указан")
    final_sum = temp_user_data[user_id].get("final_sum", 0)
    delivery_cutoff_at = parse_datetime(temp_user_data[user_id].get("delivery_cutoff_at")) or now_local()


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
    for client in same_phone_users:
        if client.user_id != user_id:  # Избегаем дублирования текущего пользователя
            all_names.append(client.name)
            order_sum = calculate_sum_for_user(client.user_id, cutoff_at=delivery_cutoff_at)
            total_sum_by_phone += order_sum

    # Формируем строку с именами всех клиентов
    all_names_str = ", ".join(all_names)

    # Сохраняем новый номер временно
    temp_user_data[user_id]["phone"] = new_phone

    # Отправляем итоговое сообщение
    bot.send_message(
        chat_id=user_id,
        text=(
            f"Обновление данных:\n"
            f"Текущий номер (старый): {current_phone}\n"
            f"Новый номер: {new_phone}\n"
            f"Имя: {name}\nАдрес: {address}\n"
            f"Имена заказчиков с текущим номером: {all_names_str}\n"
            f"Общая сумма заказов: {total_sum_by_phone}.\n\n"
            f"Подтверждаете изменения?"
        ),
        reply_markup=keyboard_for_confirmation()
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
    confirmation = call.data  # "confirm_yes" или "confirm_no"

    if confirmation == "confirm_yes":
        # Получаем временные данные пользователя (новые данные)
        user_temp_data = temp_user_data.get(user_id)

        if not user_temp_data:
            bot.send_message(
                chat_id=user_id,
                text="Ошибка! Временные данные отсутствуют. Попробуйте снова."
            )
            set_user_state(user_id, None)
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
                    bot.send_message(
                        chat_id=user_id,
                        text="Ошибка! Клиент не найден в базе данных. Попробуйте снова.",
                    )
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
                    for other_client in same_phone_users:
                        if other_client.user_id != user_id:  # Исключаем текущего клиента
                            all_names.append(other_client.name)
                            order_sum = calculate_sum_for_user(other_client.user_id, cutoff_at=delivery_cutoff_at)
                            total_sum_by_phone += order_sum
                else:
                    logger.debug("Delivery confirmation related clients not found for user_id=%s", user_id)

                # Формируем список имен клиентов
                all_names_str = ", ".join(all_names)

            except Exception as e:
                logger.warning("Delivery confirmation database error for user_id=%s: %s", user_id, e)
                bot.send_message(
                    chat_id=user_id,
                    text="Произошла ошибка при обработке данных. Попробуйте снова.",
                )
                return

        # Сохраняем новые данные в таблицу ForDelivery
        with Session(bind=engine) as session:
            try:
                upsert_for_delivery_entry(
                    session,
                    user_id,
                    name,
                    phone,
                    address,
                    total_sum_by_phone,
                    delivery_cutoff_at=delivery_cutoff_at,
                )
                session.commit()
            except Exception as e:
                session.rollback()
                logger.warning("Delivery confirmation save failed for user_id=%s: %s", user_id, e)
                bot.send_message(
                    chat_id=user_id,
                    text="Произошла ошибка при сохранении данных. Попробуйте снова.",
                )
                return

        message_for_channel = (
            f"📦 Новый заказ на доставку:\n"
            f"👤 Имя: {name}\n"
            f"📞 Телефон: {phone}\n"
            f"💰 Общая сумма заказов: {total_sum_by_phone}\n"
            f"📍 Адрес доставки: {address}"
        )
        try:
            bot.send_message(chat_id=delivery_channel, text=message_for_channel)
        except Exception as e:
            logger.warning("Delivery channel notification failed for user_id=%s: %s", user_id, e)

        # Отправляем подтверждающее сообщение пользователю
        safe_edit_message_text(
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

        # Удаляем временные данные и сбрасываем состояние пользователя
        if user_id in temp_user_data:
            del temp_user_data[user_id]
        set_user_state(user_id, None)

    elif confirmation == "confirm_no":
        # Пользователь отказался подтверждать данные
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=user_id,
            message_id=call.message.message_id,
            text="Вы хотите изменить данные? Выберите вариант ниже:",
            reply_markup=keyboard_for_editing()
        )
        set_user_state(user_id, "WAITING_FOR_DATA_EDIT")

    # Завершаем callback
    safe_answer_callback_query(call.id)

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

def calculate_for_delivery():
    """
    Вычисляет общую сумму обработанных заказов клиентов, объединяет заказы для клиентов с одинаковым номером телефона.
    Сообщение отправляется одному клиенту с минимальным ID. Логи содержат индивидуальную сумму, суммы других клиентов, и итоговую сумму.
    """
    with Session(bind=engine) as session:
        rows = session.query(Reservations, Posts, Clients).join(
            Posts, Posts.id == Reservations.post_id
        ).join(
            Clients, Clients.user_id == Reservations.user_id
        ).filter(
            Reservations.is_fulfilled == True,
            Reservations.fulfilled_at != None,
        ).all()

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
        })
        group["total_amount"] += calculate_order_amount(reservation, post)
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
        rows = session.query(Reservations, Posts, Clients).join(
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
        amount = calculate_order_amount(reservation, post)
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
def send_delivery_offer(bot, user_id, user_name):
    try:
        bot.send_message(
            chat_id=user_id,
            text=f"{user_name}, {delivery_target_label()}. Готовы принять доставку с 10:00 до 16:00?",
            reply_markup=keyboard_for_delivery()  # Используем новую клавиатуру
        )
        logger.debug("Delivery offer sent to user_id=%s", user_id)
    except Exception as e:
        logger.warning("Delivery offer send failed for user_id=%s: %s", user_id, e)


def get_related_delivery_user_ids(session, delivery_entry):
    client = session.query(Clients).filter(Clients.user_id == delivery_entry.user_id).first()
    source_phone = normalize_phone(client.phone if client else delivery_entry.phone)
    if not source_phone:
        return [delivery_entry.user_id]

    related_clients = session.query(Clients).filter(
        Clients.phone.in_(phone_variants(source_phone))
    ).all()
    related_user_ids = [row.user_id for row in related_clients]
    return related_user_ids or [delivery_entry.user_id]


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
    bot.send_message(TARGET_GROUP_ID, "Брони на доставку")
    time.sleep(RESERVED_GROUP_SEND_INTERVAL_SECONDS)

    items = get_delivery_collection_reserved_group_items()
    if not items:
        bot.send_message(TARGET_GROUP_ID, "Товаров для сборки доставки не найдено.")
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


def get_delivery_entry_cart_items(session, delivery_entry):
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

    grouped_items = {}
    for reservation in reservations:
        post = posts_by_id.get(reservation.post_id)
        client = clients_by_user_id.get(reservation.user_id)
        temp_item = get_temp_fulfilled_for_reservation(session, reservation)
        amount = calculate_delivery_row_amount(reservation, post=post, temp_item=temp_item)
        unit_price = max((amount // reservation.quantity), 0) if reservation.quantity else 0
        description = build_delivery_row_description(reservation, post=post, temp_item=temp_item)
        item_key = (reservation.post_id, unit_price, description)
        if item_key not in grouped_items:
            grouped_items[item_key] = {
                "post_id": reservation.post_id,
                "photo": post.photo if post else None,
                "description": description,
                "unit_price": unit_price,
                "quantity": 0,
                "total_price": 0,
                "names": set(),
                "created_at": get_delivery_row_created_at(reservation, post=post, temp_item=temp_item),
            }
        item = grouped_items[item_key]
        item["quantity"] += reservation.quantity
        item["total_price"] += amount
        if client and client.name:
            item["names"].add(client.name)
        if not item.get("created_at"):
            item["created_at"] = get_delivery_row_created_at(reservation, post=post, temp_item=temp_item)

    return list(grouped_items.values())


def build_delivery_collection_keyboard(session, page=0):
    deduplicate_for_delivery_entries(session)
    session.flush()
    total_count = session.query(ForDelivery).count()
    rows = session.query(ForDelivery).order_by(
        ForDelivery.name,
        ForDelivery.phone,
        ForDelivery.id,
    ).offset(page * DELIVERY_COLLECTION_PAGE_SIZE).limit(DELIVERY_COLLECTION_PAGE_SIZE).all()

    keyboard = InlineKeyboardMarkup()
    for row in rows:
        button_text = f"{row.name} | {row.phone} | {row.total_sum} ₽"
        keyboard.add(InlineKeyboardButton(button_text[:64], callback_data=f"collect_delivery_{row.id}"))

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
            safe_edit_message_text(bot, logger=logger, chat_id=chat_id, message_id=message_id, text=text)
        else:
            bot.send_message(chat_id, text)
        return

    text = f"Клиенты для сборки доставки: {total_count}\nВыберите клиента, чтобы посмотреть корзину."
    if message_id:
        safe_edit_message_text(
            bot,
            logger=logger,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )
    else:
        bot.send_message(chat_id, text, reply_markup=keyboard)


@bot.message_handler(func=lambda message: message.text == "🧺 Собрать доставку")
def collect_delivery(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    auto_fulfill_expired_reservations()
    started_snapshot, snapshot_count = start_delivery_reserved_group_pause_and_snapshot()
    if started_snapshot:
        bot.send_message(
            message.chat.id,
            f"Поток забронированного товара в группе временно остановлен. "
            f"В группу отправлено товаров для доставки: {snapshot_count}.",
        )
    elif start_reserved_group_resume_flush_if_delivery_done(message.chat.id):
        bot.send_message(
            message.chat.id,
            "Список доставки пуст. Повторно запускаю возобновление потока забронированного товара.",
        )
    show_delivery_collection_list(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("collect_delivery_page_"))
def paginate_delivery_collection(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    page = int(call.data.rsplit("_", 1)[1])
    show_delivery_collection_list(call.message.chat.id, call.message.message_id, page)
    safe_answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("collect_delivery_")
    and not call.data.startswith("collect_delivery_page_")
)
def show_delivery_collection_client(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    delivery_id = int(call.data.rsplit("_", 1)[1])
    with Session(bind=engine) as session:
        delivery_entry = session.query(ForDelivery).filter(ForDelivery.id == delivery_id).first()
        if not delivery_entry:
            safe_answer_callback_query(call.id, "Клиент уже обработан или удалён из списка.", show_alert=True)
            return

        items = get_delivery_entry_cart_items(session, delivery_entry)
        header = (
            f"🧺 Сборка доставки\n"
            f"Клиент: {delivery_entry.name}\n"
            f"Телефон: {delivery_entry.phone}\n"
            f"Сумма: {delivery_entry.total_sum} ₽\n"
            f"Адрес: {delivery_entry.address or 'адрес не указан'}\n"
            f"Срез доставки: {format_datetime(get_delivery_cutoff_at(delivery_entry))}"
        )

    bot.send_message(call.message.chat.id, header)
    if not items:
        bot.send_message(call.message.chat.id, "У клиента нет обработанных товаров в корзине.")
        safe_answer_callback_query(call.id)
        return

    for item in items:
        caption = build_item_list_caption(
            description=item["description"],
            price=item["unit_price"],
            quantity=item["quantity"],
            created_at=item.get("created_at"),
            post_id=item["post_id"],
        )
        try:
            send_photo_or_text(bot, call.message.chat.id, item["photo"], caption)
            time.sleep(0.3)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"{caption}\n\nНе удалось отправить фото: {e}")

    total_processed_sum = sum(item["total_price"] for item in items)
    bot.send_message(
        call.message.chat.id,
        f"Общая сумма обработанных заказов: {total_processed_sum} ₽",
    )

    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✅ Собрано", callback_data=f"delivery_collected_{delivery_id}"))
    keyboard.add(InlineKeyboardButton("⬅️ К списку", callback_data="collect_delivery_page_0"))
    bot.send_message(call.message.chat.id, "Когда корзина собрана, нажмите «Собрано».", reply_markup=keyboard)
    safe_answer_callback_query(call.id)


def move_for_delivery_to_in_delivery(delivery_id, manual_total_sum=None):
    with Session(bind=engine) as session:
        delivery_entry = session.query(ForDelivery).filter(ForDelivery.id == delivery_id).first()
        if not delivery_entry:
            return False, "Клиент уже обработан или удалён из списка.", 0

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


@bot.callback_query_handler(func=lambda call: call.data.startswith("delivery_collected_"))
def mark_delivery_collected(call):
    if not has_role(call.from_user.id, DELIVERY_ROLES):
        safe_answer_callback_query(call.id, "У вас нет прав для этой функции.", show_alert=True)
        return

    delivery_id = int(call.data.rsplit("_", 1)[1])
    with Session(bind=engine) as session:
        delivery_entry = session.query(ForDelivery).filter(ForDelivery.id == delivery_id).first()
        if not delivery_entry:
            safe_answer_callback_query(call.id, "Клиент уже обработан или удалён из списка.", show_alert=True)
            return

    set_user_state(call.from_user.id, {
        "action": "AWAITING_DELIVERY_COLLECTED_SUM",
        "delivery_id": delivery_id,
        "source_message_id": call.message.message_id,
    })
    safe_edit_message_text(
        bot,
        logger=logger,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Введите итоговую сумму по собранной доставке одним числом.",
    )
    safe_answer_callback_query(call.id)


@bot.message_handler(func=lambda message: get_state_action(message.chat.id) == "AWAITING_DELIVERY_COLLECTED_SUM")
def handle_delivery_collected_sum(message):
    if not require_role(message, DELIVERY_ROLES):
        clear_user_state(message.chat.id)
        return

    manual_total_sum = parse_delivery_total_input(message.text)
    if manual_total_sum is None:
        bot.send_message(message.chat.id, "Введите сумму цифрами, например: 3500")
        return

    state = get_user_state(message.chat.id) or {}
    delivery_id = state.get("delivery_id")
    source_message_id = state.get("source_message_id")
    clear_user_state(message.chat.id)

    if not delivery_id:
        bot.send_message(message.chat.id, "Не удалось найти сборку доставки. Откройте список заново.")
        show_delivery_collection_list(message.chat.id, message_id=source_message_id, page=0)
        return

    safe_delete_message(bot, message.chat.id, message.message_id, logger=logger)
    success, message_text, moved_count = move_for_delivery_to_in_delivery(
        delivery_id,
        manual_total_sum=manual_total_sum,
    )
    bot.send_message(
        message.chat.id,
        f"{'✅' if success else '❌'} {message_text} Товарных строк: {moved_count}.",
    )
    if success and start_reserved_group_resume_flush_if_delivery_done(message.chat.id):
        bot.send_message(
            message.chat.id,
            "Все корзины доставки собраны. Возобновляю поток забронированного товара в группе.",
        )
    show_delivery_collection_list(message.chat.id, message_id=source_message_id, page=0)

@bot.message_handler(func=lambda message: message.text == "✅ Подтвердить доставку")
def confirm_delivery(message):
    if not require_role(message, DELIVERY_ROLES):
        return
    bot.send_message(message.chat.id, "Теперь доставка собирается по клиентам. Открываю список для сборки.")
    show_delivery_collection_list(message.chat.id)

@bot.callback_query_handler(
    func=lambda call: call.data == "edit_address"
    and get_user_state(call.from_user.id) != "WAITING_FOR_DATA_EDIT"
)
def handle_edit_choice(call):
    logger.debug("Edit choice callback received: %s", call.data)

    try:
        data_parts = call.data.split("_")  # Разделяем строку
        if len(data_parts) == 2:  # Для команд без ID (например, "edit_address")
            action = data_parts[0]  # Действие (edit)
            target = data_parts[1]  # Цель (address)

            if action == "edit" and target == "address":
                # Переход в состояние изменения адреса
                set_user_state(call.from_user.id, "EDITING_ADDRESS")
                bot.send_message(chat_id=call.from_user.id, text="Введите новый адрес:")
            else:
                bot.send_message(chat_id=call.from_user.id, text="Неизвестная команда.")
        elif len(data_parts) == 3:  # Для команд с ID (например, "edit_post_123")
            action = data_parts[0]
            target = data_parts[1]
            post_id = int(data_parts[2])  # ID поста

            if action == "edit" and target == "post":
                bot.send_message(chat_id=call.from_user.id, text=f"Вы выбрали редактирование поста с ID {post_id}")
            else:
                bot.send_message(chat_id=call.from_user.id, text="Неизвестная команда.")
        else:
            raise ValueError("Неверный формат callback_data")

    except ValueError as e:
        bot.send_message(chat_id=call.from_user.id, text="Ошибка: Неверный формат команды.")
        print(f"Ошибка обработки команды: {e}")
    except Exception as e:
        bot.send_message(chat_id=call.from_user.id, text="Произошла ошибка при обработке вашего выбора.")
        print(f"Общая ошибка: {e}")

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
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=keyboard)

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
        bot.send_message(message.chat.id, "Нет свободных постов для ревизии.")
        return

    # Уникальные даты по постам
    unique_dates = sorted(list(set(post.created_at.date() for post in posts)))

    if not unique_dates:
        bot.send_message(message.chat.id, "Нет доступных дат для ревизии.")
        return

    # Клавиатура для выбора даты
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for date in unique_dates[:2]:  # Показываем максимум 2 даты
        # Форматируем дату в виде: "число месяц"
        formatted_date = date.strftime("%d %B")
        keyboard.add(types.KeyboardButton(formatted_date))

    keyboard.add(types.KeyboardButton("⬅️ Назад"))
    bot.send_message(message.chat.id, "Выберите дату для ревизии:", reply_markup=keyboard)

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
            post.price = new_price
            post.is_sent = False
            post.message_id = None
            post.created_at = now
            post.chat_id = audit_user_id
            summary["processed_count"] += 1
            summary["old_total"] += old_price
            summary["new_total"] += new_price

        session.commit()

    return summary


def answer_manual_audit_disabled(target):
    message = "Ручная ревизия отключена, используйте автоматическую."
    if hasattr(target, "data") and hasattr(target, "id"):
        safe_answer_callback_query(target.id, message, show_alert=True)
    else:
        bot.send_message(target.chat.id, message)
        clear_user_state(target.chat.id)


@bot.message_handler(
    func=lambda message: message.text in temp_user_data.get(message.chat.id, {}).get("unique_dates", []))
def show_posts_by_date(message):
    selected_date_text = message.text
    audit_data = temp_user_data.get(message.chat.id, {})
    selected_date_iso = audit_data.get("audit_date_map", {}).get(selected_date_text)

    if not selected_date_iso:
        bot.send_message(message.chat.id, "Дата не найдена в базе. Пожалуйста, выберите другую дату.")
        return

    selected_date = datetime.fromisoformat(selected_date_iso).date()
    summary = apply_auto_audit_for_date(selected_date, message.chat.id)

    if (
        summary["processed_count"] == 0
        and summary["blocked_count"] == 0
        and summary["no_stock_count"] == 0
    ):
        bot.send_message(message.chat.id, f"Нет свободных постов за дату {selected_date}.")
        return

    if message.chat.id in temp_user_data:
        del temp_user_data[message.chat.id]
    active_audit[message.chat.id] = False

    bot.send_message(
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
    )

@bot.message_handler(func=lambda message: message.text == "Отменить ревизию")
def cancel_audit(message):
    global active_audit

    # Проверяем, активна ли ревизия
    if not active_audit.get(message.chat.id):
        bot.send_message(message.chat.id, "Нет активной ревизии для отмены.")
        return

    # Завершаем ревизию
    active_audit[message.chat.id] = False
    bot.send_message(message.chat.id, "Ревизия успешно отменена.", reply_markup=types.ReplyKeyboardRemove())

@bot.callback_query_handler(
    func=lambda call: call.data.startswith((
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
    func=lambda message: get_user_state(message.chat.id) in {
        "EDITING_AUDIT_PRICE",
        "EDITING_AUDIT_DESCRIPTION",
        "EDITING_AUDIT_QUANTITY",
    }
)
def disabled_manual_audit_message(message):
    answer_manual_audit_disabled(message)

@bot.message_handler(func=lambda message: message.text == "😞 У меня брак")
def defect(message):
    user_id = message.chat.id

    with Session(bind=engine) as session:
        related_user_ids = get_related_user_ids_by_full_phone(user_id)
        # Получаем записи из Temp_Fulfilled с необходимыми условиями
        user_items = session.query(Temp_Fulfilled).filter(
            Temp_Fulfilled.user_id.in_(related_user_ids),
            Temp_Fulfilled.in_delivery == True,
            Temp_Fulfilled.defect == False,
            Temp_Fulfilled.skidka == False
        ).all()

        if not user_items:
            bot.send_message(user_id, "У вас нет товаров, которые подходят для возврата по браку.")
            return

        # Создаем клавиатуру с выбором товара
        markup = InlineKeyboardMarkup()
        for item in user_items:
            button = InlineKeyboardButton(
                text=f"{item.item_description} (x{item.quantity})",
                callback_data=f"select_defective_{item.id}"  # ID товара из Temp_Fulfilled
            )
            markup.add(button)

        # Отправляем сообщение с выбором товара
        bot.send_message(
            user_id,
            "Выберите товар, который хотите вернуть по браку:",
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_defective_"))
def select_defective_order(call):
    user_id = call.from_user.id
    item_id = int(call.data.split("_")[2])  # ID записи в Temp_Fulfilled

    # Сохраняем состояние, чтобы отследить следующий шаг (ввод причины)
    set_user_state(user_id, {"action": "defect_reason", "item_id": item_id})

    # Показываем кнопку для перехода к вводу причины
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📋 Указать причину", callback_data="enter_defect_reason"))

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
    state = get_user_state(user_id)

    if not isinstance(state, dict) or state.get("action") != "defect_reason":
        safe_answer_callback_query(call.id, "Ошибка! Попробуйте снова.", show_alert=True)
        return

    bot.send_message(
        user_id,
        "Пожалуйста, опишите проблему с товаром. Фотография не нужна, только текст"
    )
    set_user_state(user_id, {"action": "wait_defect_reason", "item_id": state["item_id"]})


@bot.message_handler(
    func=lambda message: get_state_action(message.chat.id) == "wait_defect_reason"
)
def handle_defect_reason(message):
    user_id = message.chat.id
    state = get_user_state(user_id)

    if not state or "item_id" not in state:
        bot.send_message(user_id, "Ошибка! Попробуйте снова.")
        return

    reason = message.text
    item_id = state["item_id"]

    with Session(bind=engine) as session:
        # Получаем запись о товаре
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            bot.send_message(user_id, "Ошибка! Товар не найден.")
            return
        if item.user_id not in get_related_user_ids_by_full_phone(user_id):
            bot.send_message(user_id, "Этот товар не найден в ваших заказах.")
            return

        # Отправляем сообщение администратору
        admin_users = session.query(Clients).filter(Clients.role.in_(["admin", "supreme_leader"])).all()

        # Получаем фото товара из живой карточки или snapshot удаленного поста.
        post = get_post_or_snapshot(session, item.post_id)
        if not post:
            bot.send_message(
                user_id,
                "Не удалось найти данные о товаре. Попробуйте позже."
            )
            return

        # Получаем номер телефона клиента из таблицы Clients
        client = session.query(Clients).filter_by(user_id=item.user_id).first()
        if not client:
            bot.send_message(
                user_id,
                "Не удалось найти данные о вашем профиле. Попробуйте позже."
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
                bot.send_photo(
                    admin.user_id,
                    photo=post.photo,  # Фото из таблицы Posts
                    caption=message_text,
                    reply_markup=markup,
                )
            else:
                # Если фото отсутствует, отправляем только текст
                bot.send_message(
                    admin.user_id,
                    message_text,
                    reply_markup=markup,
                )

    bot.send_message(user_id, "Ваш запрос отправлен администратору. Спасибо!")
    clear_user_state(user_id)

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("defect_") or call.data.startswith("discount_") or call.data.startswith(
        "contact_"))
def handle_inline_buttons(call):
    if not has_role(call.from_user.id, ADMIN_ROLES):
        safe_answer_callback_query(call.id, "У вас недостаточно прав.", show_alert=True)
        return
    user_id = call.from_user.id
    action, item_id = call.data.split("_")
    item_id = int(item_id)

    if action == "defect":
        handle_defect_action(call, item_id)
    elif action == "discount":
        request_discount_amount(call, item_id)
    elif action == "contact":
        contact_client(call, item_id)

def handle_defect_action(call, item_id):
    with Session(bind=engine) as session:
        # Находим запись в Temp_Fulfilled
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            bot.send_message(call.message.chat.id, "Не удалось найти запись.")
            return

        # Ставим статус "defect = True" в Temp_Fulfilled
        item.defect = True
        session.commit()

        # Получаем user_id клиента через Clients
        client = session.query(Clients).filter_by(user_id=item.user_id).first()
        if client:
            bot.send_message(
                client.user_id,  # ID клиента
                f"Ваш возврат оформлен!\n\n🔔 Товар: {item.item_description}\n💰 Сумма возврата: {item.price}₽"
            )

    # Уведомляем администратора
    bot.send_message(call.message.chat.id, "Возврат оформлен")

def request_discount_amount(call, item_id):
    # Сохраняем состояние для администратора
    set_user_state(call.from_user.id, {"action": "discount_request", "item_id": item_id, "admin_id": call.from_user.id})

    bot.send_message(
        call.message.chat.id,
        "Введите желаемую сумму скидки для клиента:"
    )

@bot.message_handler(
    func=lambda message: get_state_action(message.chat.id) == "discount_request")
def handle_discount_amount(message):
    admin_id = message.chat.id  # ID администратора, который предложил скидку
    state = get_user_state(admin_id)

    if not state:
        bot.send_message(admin_id, "Произошла ошибка. Попробуйте ещё раз.")
        return

    try:
        discount_amount = int(message.text)
        if discount_amount <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(admin_id, "Введите корректную сумму скидки (положительное число).")
        return

    # Получаем ID товара
    item_id = state["item_id"]

    with Session(bind=engine) as session:
        # Получаем информацию о товаре
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            bot.send_message(admin_id, "Ошибка! Товар не найден.")
            return

        # Получаем данные клиента
        client = session.query(Clients).filter_by(user_id=item.user_id).first()
        if not client:
            bot.send_message(admin_id, "Ошибка! Не удалось найти клиента.")
            return

        # Сохраняем состояние для клиента
        set_user_state(
            client.user_id,
            {"action": "confirm_discount", "item_id": item_id, "discount_amount": discount_amount, "admin_id": admin_id}
        )

        # Уведомляем клиента о скидке
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Да", callback_data=f"confirm_discount_{item_id}"),
            InlineKeyboardButton("Отказаться", callback_data=f"return_discount_{item_id}")
        )

        bot.send_message(
            client.user_id,
            f"Вам поступило предложение о скидке по вашему товару:\n\n"
            f"📦 Товар: {item.item_description}\n"
            f"💰 Размер скидки: {discount_amount}₽\n\n"
            f"Вы согласны на данную скидку?",
            reply_markup=markup
        )

    # Подтверждаем администратору
    bot.send_message(
        admin_id,
        f"Скидка в размере {discount_amount}₽ отправлена клиенту на подтверждение."
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_discount_") or call.data.startswith("return_discount_")
)
def handle_discount_confirmation(call):
    user_id = call.from_user.id
    try:
        action, item_id = call.data.rsplit("_", 1)  # Разделяем строку с конца
        item_id = int(item_id)  # Преобразуем item_id в число
    except ValueError:
        safe_answer_callback_query(call.id, "Ошибка: некорректные данные.")
        return

    state = get_user_state(user_id)
    if not isinstance(state, dict) or state.get("item_id") != item_id:
        safe_answer_callback_query(call.id, "Ошибка! Товар не найден.")
        return

    discount_amount = state.get("discount_amount")
    admin_id = state.get("admin_id")  # Получаем ID администратора

    with Session(bind=engine) as session:
        # Получаем информацию о товаре
        item = session.query(Temp_Fulfilled).filter_by(id=item_id).first()
        if not item:
            safe_answer_callback_query(call.id, "Ошибка! Запись о товаре не найдена.")
            return

        if action == "confirm_discount":
            # Клиент согласен на скидку: Обновляем данные в базе
            item.skidka_price = discount_amount
            item.skidka = True
            session.commit()

            # Уведомляем клиента
            safe_answer_callback_query(call.id, "Скидка успешно активирована.")
            bot.send_message(
                call.message.chat.id,
                f"Скидка в размере {discount_amount}₽ успешно активирована! Спасибо за ваше решение!"
            )

            # Уведомляем администратора
            if admin_id:
                admin_message = (
                    f"Клиент согласился на скидку для товара:\n\n"
                    f"📦 Товар: {item.item_description}\n"
                    f"💰 Сумма скидки: {discount_amount}₽"
                )
                bot.send_message(admin_id, admin_message)

        elif action == "return_discount":
            # Клиент отказался от скидки: Отмечаем товар как "на возврат" и уведомляем
            item.defect = True  # Помечаем товар как "на возврат"
            session.commit()

            # Уведомляем клиента
            safe_answer_callback_query(call.id, "Хорошо, оформлен возврат.")
            bot.send_message(
                call.message.chat.id,
                "Хорошо, оформлен возврат. При следующей доставке товар будет возвращён."
            )

            # Уведомляем администратора
            if admin_id:
                admin_message = (
                    f"Клиент отказался от скидки, и товар был отмечен на возврат:\n\n"
                    f"📦 Товар: {item.item_description}\n"
                    f"💰 Предлагавшаяся скидка: {discount_amount}₽"
                )
                bot.send_message(admin_id, admin_message)

    clear_user_state(user_id)

def contact_client(call, user_id):
    with Session(bind=engine) as session:
        # Получаем данные клиента
        client = session.query(Clients).filter_by(user_id=user_id).first()
        if not client:
            bot.send_message(call.message.chat.id, "Не удалось найти данные клиента.")
            return

        # Отправляем ссылку на чат с клиентом администратору
        bot.send_message(
            call.from_user.id,
            f"[Нажмите, чтобы начать чат с клиентом](tg://user?id={client.user_id})",
            parse_mode="Markdown"  # Используем Markdown для создания кликабельной ссылки
        )



# Запуск бота
def run_bot():
    start_reservation_auto_fulfill_worker()
    start_channel_post_auto_publish_worker()
    start_delivery_cleanup_worker()
    start_reservation_stats_daily_report_worker()
    start_reserved_group_resume_flush_if_delivery_done(recover_stale=True)
    retry_delay = 5

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
            retry_delay = 5
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.warning("Bot polling failed: %s. Retrying in %s seconds.", exc, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    run_bot()
