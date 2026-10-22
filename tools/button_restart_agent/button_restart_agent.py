import json
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path


DEFAULT_CONFIG = {
    "restart_url": "http://109.196.100.116:8092/api/restart",
    "token": "put-token-here",
    "key": "",
    "cooldown_seconds": 30,
    "bind_and_restart_on_first_press": True,
}


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "button_restart_agent.config.json"
LOG_PATH = BASE_DIR / "button_restart_agent.log"


pressed_keys = set()
last_restart_at = 0.0
config = {}
keyboard = None


def log(message):
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    fallback_log_path = Path(tempfile.gettempdir()) / "button_restart_agent.log"

    for path in (LOG_PATH, fallback_log_path):
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
            return True
        except OSError:
            continue

    return False


def load_config():
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        raw_config = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception as error:
        log(f"config read failed: {error}")
        return dict(DEFAULT_CONFIG)

    if not raw_config.strip():
        log("config file is empty, using defaults")
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        loaded = json.loads(raw_config)
    except JSONDecodeError as error:
        log(f"config JSON parse failed: {error}")
        return dict(DEFAULT_CONFIG)

    if not isinstance(loaded, dict):
        log("config JSON root is not an object, using defaults")
        return dict(DEFAULT_CONFIG)

    merged = {**DEFAULT_CONFIG, **loaded}
    return merged


def save_config(value):
    try:
        CONFIG_PATH.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except Exception as error:
        log(f"config save failed: {error}")
        return False


def get_cooldown_seconds():
    try:
        return max(0, int(config.get("cooldown_seconds") or 30))
    except (TypeError, ValueError):
        log("invalid cooldown_seconds, using 30")
        return 30


def load_keyboard_module():
    global keyboard

    if keyboard is not None:
        return keyboard

    try:
        from pynput import keyboard as pynput_keyboard
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency pynput. Run install_macos.command or install pynput from requirements.txt."
        ) from exc

    keyboard = pynput_keyboard
    return keyboard


def key_signature(key):
    keyboard_module = load_keyboard_module()
    if isinstance(key, keyboard_module.Key):
        return f"key:{key.name}"

    char = getattr(key, "char", None)
    if char:
        return f"char:{char}"

    vk = getattr(key, "vk", None)
    if vk is not None:
        return f"vk:{vk}"

    return repr(key)


def restart_bot():
    global last_restart_at

    now = time.time()
    cooldown_seconds = get_cooldown_seconds()
    if now - last_restart_at < cooldown_seconds:
        log("restart skipped: cooldown")
        return

    token = str(config.get("token") or "").strip()
    if not token or token == DEFAULT_CONFIG["token"]:
        log("restart skipped: missing token")
        return

    request = urllib.request.Request(
        str(config["restart_url"]),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8", errors="replace")
            last_restart_at = now
            log(f"restart response {response.status}: {payload}")
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", errors="replace")
        log(f"restart failed HTTP {error.code}: {payload}")
    except Exception as error:
        log(f"restart failed: {error}")


def bind_key(signature):
    config["key"] = signature
    save_config(config)
    log(f"button bound: {signature}")


def on_press(key):
    signature = key_signature(key)
    if signature in pressed_keys:
        return
    pressed_keys.add(signature)

    configured_key = str(config.get("key") or "").strip()
    if not configured_key:
        bind_key(signature)
        if bool(config.get("bind_and_restart_on_first_press", True)):
            restart_bot()
        return

    if signature == configured_key:
        restart_bot()


def on_release(key):
    pressed_keys.discard(key_signature(key))


def main():
    global config
    config = load_config()
    log("agent started")

    try:
        keyboard_module = load_keyboard_module()
    except RuntimeError as exc:
        log(str(exc))
        return 1

    try:
        with keyboard_module.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
    except KeyboardInterrupt:
        log("agent stopped")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
