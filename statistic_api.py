import json
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg2


SAMARA_TZ = ZoneInfo("Europe/Samara")
UNKNOWN_USER = "Неизвестный пользователь"
PERIOD_LABELS = {
    "today": "Сегодня",
    "week": "На этой неделе",
    "last_week": "На прошлой неделе",
}
DEFAULT_REFRESH_SECONDS = 60 * 60


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_env_file(path):
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ[key] = value


def load_default_env():
    load_env_file(os.environ.get("ENV_FILE", ".env"))


def get_required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_int_env(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid integer environment variable %s=%r, using default=%s", name, value, default)
        return default


def parse_cors_origins(value):
    return [
        origin.strip()
        for origin in str(value or "").split(",")
        if origin.strip()
    ]


def same_loopback_origin(left, right):
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    loopback_hosts = {"127.0.0.1", "localhost"}
    return (
        left_parsed.scheme == right_parsed.scheme
        and left_parsed.hostname in loopback_hosts
        and right_parsed.hostname in loopback_hosts
        and left_parsed.port == right_parsed.port
    )


def allowed_cors_origin(request_origin, configured_origins):
    if "*" in configured_origins:
        return "*"
    if not request_origin:
        return configured_origins[0] if configured_origins else ""
    if request_origin in configured_origins:
        return request_origin
    if any(same_loopback_origin(request_origin, origin) for origin in configured_origins):
        return request_origin
    return ""


def now_samara_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


def build_period_ranges(current):
    monday = current - timedelta(days=current.weekday())
    last_monday = monday - timedelta(days=7)
    last_sunday = monday - timedelta(days=1)
    return {
        "today": (current.date(), current.date()),
        "week": (monday.date(), current.date()),
        "last_week": (last_monday.date(), last_sunday.date()),
    }


def get_database_connection():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        connection = psycopg2.connect(database_url)
    else:
        connection = psycopg2.connect(
            host=get_required_env("DB_HOST"),
            port=get_int_env("DB_PORT", None),
            dbname=get_required_env("DB_NAME"),
            user=get_required_env("DB_USER"),
            password=get_required_env("DB_PASSWORD"),
        )
    connection.set_session(readonly=True, autocommit=False)
    return connection


def rows_to_clients_dict(rows):
    clients = {}
    for user_id, name in rows:
        clients[user_id] = name or UNKNOWN_USER
    return clients


def empty_period_payload(key, start, end):
    return {
        "key": key,
        "label": PERIOD_LABELS[key],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "created_posts": {
            "total": 0,
            "by_user": [],
        },
        "revisions": {
            "total": 0,
            "by_user": [],
        },
        "delivery_collections": {
            "total": 0,
            "by_user": [],
        },
        "delivery_collection_items": {
            "total": 0,
            "by_user": [],
        },
    }


def counts_to_sorted_rows(counts):
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: item[0])
    ]


def calculate_statistics_snapshot(current=None):
    current = current or now_samara_naive()
    period_ranges = build_period_ranges(current)

    post_statistics = {key: {} for key in period_ranges}
    revision_statistics = {key: {} for key in period_ranges}
    delivery_collection_statistics = {key: {} for key in period_ranges}
    delivery_collection_item_statistics = {key: {} for key in period_ranges}
    total_posts = {key: 0 for key in period_ranges}
    total_revisions = {key: 0 for key in period_ranges}
    total_delivery_collections = {key: 0 for key in period_ranges}
    total_delivery_collection_items = {key: 0 for key in period_ranges}
    revision_post_ids_by_period = {key: set() for key in period_ranges}
    first_period_date = min(start for start, _end in period_ranges.values())
    last_period_date = max(end for _start, end in period_ranges.values())
    query_start = datetime.combine(first_period_date, datetime.min.time())
    query_end = datetime.combine(last_period_date + timedelta(days=1), datetime.min.time())

    connection = get_database_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT user_id, name FROM clients")
            clients_dict = rows_to_clients_dict(cursor.fetchall())

            cursor.execute(
                "SELECT id, chat_id, created_at FROM posts "
                "WHERE created_at >= %s AND created_at < %s "
                "AND created_at::time <> TIME '00:00:00'",
                (query_start, query_end),
            )
            posts = cursor.fetchall()

            cursor.execute(
                "SELECT post_id, auditor_user_id, created_at FROM revision_logs "
                "WHERE created_at >= %s AND created_at < %s",
                (query_start, query_end),
            )
            revision_logs = cursor.fetchall()

            cursor.execute(
                "SELECT delivery_id, collector_user_id, collector_name, MAX(placed_at) AS completed_at "
                "FROM delivery_collection_items "
                "WHERE placed_at IS NOT NULL AND placed_at < %s "
                "AND collector_user_id IS NOT NULL "
                "GROUP BY delivery_id, collector_user_id, collector_name "
                "HAVING MAX(placed_at) >= %s",
                (query_end, query_start),
            )
            delivery_collections = cursor.fetchall()

            cursor.execute(
                "SELECT collector_user_id, collector_name, placed_at, quantity "
                "FROM delivery_collection_items "
                "WHERE placed_at >= %s AND placed_at < %s "
                "AND collector_user_id IS NOT NULL "
                "AND reservation_id IS NOT NULL",
                (query_start, query_end),
            )
            delivery_collection_items = cursor.fetchall()
    finally:
        connection.close()

    for key, date_range in period_ranges.items():
        start_date, end_date = date_range

        for post_id, auditor_user_id, created_at in revision_logs:
            if not created_at:
                continue
            revision_date = created_at.date()
            if start_date <= revision_date <= end_date:
                revision_post_ids_by_period[key].add(post_id)
                auditor_name = clients_dict.get(auditor_user_id, UNKNOWN_USER)
                revision_statistics[key][auditor_name] = revision_statistics[key].get(auditor_name, 0) + 1
                total_revisions[key] += 1

        for _delivery_id, collector_user_id, collector_name, completed_at in delivery_collections:
            if not collector_user_id or not completed_at:
                continue
            collection_date = completed_at.date()
            if start_date <= collection_date <= end_date:
                collector_display_name = collector_name or clients_dict.get(collector_user_id, UNKNOWN_USER)
                delivery_collection_statistics[key][collector_display_name] = (
                    delivery_collection_statistics[key].get(collector_display_name, 0) + 1
                )
                total_delivery_collections[key] += 1

        for collector_user_id, collector_name, placed_at, quantity in delivery_collection_items:
            if not collector_user_id or not placed_at:
                continue
            collection_date = placed_at.date()
            if start_date <= collection_date <= end_date:
                collector_display_name = collector_name or clients_dict.get(collector_user_id, UNKNOWN_USER)
                item_count = max(int(quantity or 0), 1)
                delivery_collection_item_statistics[key][collector_display_name] = (
                    delivery_collection_item_statistics[key].get(collector_display_name, 0) + item_count
                )
                total_delivery_collection_items[key] += item_count

        for post_id, chat_id, created_at in posts:
            if not created_at:
                continue
            if post_id in revision_post_ids_by_period[key]:
                continue

            created_at_date = created_at.date()
            created_at_time = created_at.time()
            if created_at_time == datetime.min.time():
                continue

            if start_date <= created_at_date <= end_date:
                creator_name = clients_dict.get(chat_id, UNKNOWN_USER)
                post_statistics[key][creator_name] = post_statistics[key].get(creator_name, 0) + 1
                total_posts[key] += 1

    periods = {}
    for key, date_range in period_ranges.items():
        payload = empty_period_payload(key, *date_range)
        payload["created_posts"]["total"] = total_posts[key]
        payload["created_posts"]["by_user"] = counts_to_sorted_rows(post_statistics[key])
        payload["revisions"]["total"] = total_revisions[key]
        payload["revisions"]["by_user"] = counts_to_sorted_rows(revision_statistics[key])
        payload["delivery_collections"]["total"] = total_delivery_collections[key]
        payload["delivery_collections"]["by_user"] = counts_to_sorted_rows(delivery_collection_statistics[key])
        payload["delivery_collection_items"]["total"] = total_delivery_collection_items[key]
        payload["delivery_collection_items"]["by_user"] = counts_to_sorted_rows(delivery_collection_item_statistics[key])
        periods[key] = payload

    return {
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": "Europe/Samara",
        "refresh_after_seconds": get_int_env("STATISTIC_API_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS),
        "periods": periods,
    }


class StatisticApiHandler(BaseHTTPRequestHandler):
    server_version = "StatisticApi/1.0"

    def do_CONNECT(self):
        self.write_json(405, {"error": "method_not_allowed"})

    def do_OPTIONS(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/statistic":
            self.send_response(404)
        else:
            self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.write_json(200, {"ok": True})
            return

        if parsed.path != "/api/statistic":
            self.write_json(404, {"error": "not_found"})
            return

        if not self.is_authorized():
            self.write_json(401, {"error": "unauthorized"})
            return

        try:
            self.write_json(200, calculate_statistics_snapshot())
        except Exception:
            logger.exception("Statistic API request failed")
            self.write_json(500, {"error": "internal_error"})

    def is_authorized(self):
        token = self.server.api_token
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), token)
        return False

    def write_json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def send_cors_headers(self):
        configured_origins = parse_cors_origins(os.environ.get("STATISTIC_API_CORS_ORIGIN", ""))
        cors_origin = allowed_cors_origin(self.headers.get("Origin", "").strip(), configured_origins)
        if not cors_origin:
            return

        self.send_header("Access-Control-Allow-Origin", cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


class StatisticApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, api_token):
        super().__init__(server_address, handler_class)
        self.api_token = api_token

    def handle_error(self, request, client_address):
        exc_type, exc, _traceback = sys.exc_info()
        if exc_type and issubclass(exc_type, (BrokenPipeError, ConnectionResetError)):
            logger.debug("Client connection closed by %s: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


def main():
    load_default_env()
    api_token = get_required_env("STATISTIC_API_TOKEN")
    host = os.environ.get("STATISTIC_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = get_int_env("STATISTIC_API_PORT", 8091)

    server = StatisticApiServer((host, port), StatisticApiHandler, api_token)
    logger.info("Statistic API listening on %s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
