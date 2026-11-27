"""Агент включения админского ПК (Termux на Android-телефоне рядом с ПК).

Раз в несколько секунд спрашивает бота через Tailscale, есть ли команда «💻 Включить админ ПК».
Получив её, шлёт Wake-on-LAN в местную сеть и сообщает боту результат.
Запуск: python ~/wake_agent.py (автозапуск — ~/.termux/boot/start.sh).
"""
import fcntl
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

API_URL = "http://100.115.139.109:8093"  # бот на домашнем сервере через Tailscale
PC_MAC = "00-E0-4B-50-F0-68"  # сетевая карта админского ПК
BROADCASTS = ("255.255.255.255", "192.168.0.255")
POLL_SECONDS = 5
ERROR_SLEEP_SECONDS = 30
VPN_KICK_AFTER_ERRORS = 3
VPN_KICK_INTERVAL_SECONDS = 300

HOME = os.path.expanduser("~")
LOG_PATH = os.path.join(HOME, "wake_agent.log")
LOCK_PATH = os.path.join(HOME, ".wake_agent.lock")
TOKEN_PATH = os.path.join(HOME, ".wake_token")  # необязательно: если на сервере задан PC_WAKE_API_TOKEN


def log(message):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 200_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def send_magic_packet(mac=PC_MAC):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            for address in BROADCASTS:
                for port in (9, 7):
                    sock.sendto(packet, (address, port))
            time.sleep(0.3)


def api_post(path, body=None):
    headers = {"Content-Type": "application/json"}
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except OSError:
        pass
    request = urllib.request.Request(API_URL + path, data=json.dumps(body or {}).encode(),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def kick_tailscale():
    """Если связи нет — просим Tailscale подключиться (после перезагрузки он сам не стартует)."""
    try:
        subprocess.run(["am", "broadcast", "-a", "com.tailscale.ipn.CONNECT_VPN",
                        "-n", "com.tailscale.ipn/.IPNReceiver"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        log("tailscale connect requested")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"tailscale connect request failed: {exc}")


def handle_job(job):
    job_id = job["id"]
    try:
        send_magic_packet()
        log(f"job {job_id}: magic packet sent to {PC_MAC}")
        api_post(f"/api/wake/{job_id}/result", {"ok": True})
    except OSError as exc:
        log(f"job {job_id}: send failed: {exc}")
        api_post(f"/api/wake/{job_id}/result", {"ok": False, "error": str(exc)})


def main():
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # агент уже работает
    log("agent started")
    errors = 0
    last_kick = 0.0
    last_error = ""
    while True:
        try:
            job = api_post("/api/wake/next").get("job")
            if errors:
                log("connection restored")
            errors, last_error = 0, ""
            if job:
                handle_job(job)
                continue
            time.sleep(POLL_SECONDS)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors += 1
            text = str(exc)
            if text != last_error:
                log(f"api error: {text}")
                last_error = text
            if errors >= VPN_KICK_AFTER_ERRORS and time.time() - last_kick > VPN_KICK_INTERVAL_SECONDS:
                kick_tailscale()
                last_kick = time.time()
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
