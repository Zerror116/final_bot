import os

from database.env_loader import load_dotenv

load_dotenv()


def _get_required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _get_required_int(name: str) -> int:
    return int(_get_required(name))


def _get_bot_link() -> str:
    bot_link = os.environ.get("BOT_LINK", "").strip()
    if bot_link:
        return bot_link

    bot_username = os.environ.get("BOT_USERNAME", "").strip().lstrip("@")
    if bot_username:
        return f"https://t.me/{bot_username}"

    raise RuntimeError("Missing required environment variable: BOT_LINK or BOT_USERNAME")


TOKEN = _get_required("BOT_TOKEN")
CHANNEL_ID = _get_required_int("CHANNEL_ID")
TARGET_GROUP_ID = _get_required_int("TARGET_GROUP_ID")
ARCHIVE = _get_required_int("ARCHIVE_ID")
channel_link = _get_required("CHANNEL_LINK")
delivery_archive = _get_required_int("DELIVERY_ARCHIVE_ID")
delivery_channel = _get_required_int("DELIVERY_CHANNEL_ID")
ADMIN_USER_ID = _get_required_int("ADMIN_USER_ID")
ROLES = ["client", "worker", "audit", "admin"]
SPECIAL_ROLES = ["supreme_leader"]
support_link = _get_required("SUPPORT_LINK")
protected_user_id = int(os.environ.get("PROTECTED_USER_ID", ADMIN_USER_ID))
bot_link = _get_bot_link()
