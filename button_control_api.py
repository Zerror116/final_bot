import html
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def parse_int_env(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


SERVICE_NAME = os.environ.get("FINAL_BOT_SERVICE_NAME", "final_bot.service").strip() or "final_bot.service"
RESTART_COOLDOWN_SECONDS = max(0, parse_int_env("FINAL_BOT_BUTTON_RESTART_COOLDOWN_SECONDS", 30))


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_int_env(name, default):
    return parse_int_env(name, default)


def run_systemctl(*args, timeout=20):
    completed = subprocess.run(
        ["systemctl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode, output


def get_service_status():
    code, output = run_systemctl("is-active", SERVICE_NAME, timeout=5)
    return {
        "service": SERVICE_NAME,
        "active": output == "active",
        "state": output or f"exit_{code}",
    }


def build_control_page():
    escaped_service = html.escape(SERVICE_NAME)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Final Bot Control</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #12100d;
      color: #f7f1e7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 24px;
    }}
    main {{
      width: min(100%, 760px);
      background: #f7f1e7;
      color: #17120d;
      border-radius: 10px;
      padding: 32px;
      box-shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
    }}
    h1 {{ margin: 0 0 10px; font-size: 34px; letter-spacing: 0; }}
    p {{ margin: 8px 0; font-size: 18px; line-height: 1.4; color: #51483f; }}
    .status {{
      margin-top: 26px;
      min-height: 92px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: #17120d;
      color: #f7f1e7;
      font-size: 28px;
      font-weight: 800;
      text-align: center;
      padding: 18px;
    }}
    .status.ok {{ background: #0f766e; }}
    .status.warn {{ background: #b45309; }}
    .status.err {{ background: #b42318; }}
    .buttons {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 20px;
    }}
    button {{
      height: 52px;
      border: 0;
      border-radius: 8px;
      background: #17120d;
      color: #f7f1e7;
      font-size: 16px;
      font-weight: 800;
      cursor: pointer;
    }}
    button.secondary {{ background: #d8d0c4; color: #17120d; }}
    code {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 4px;
      background: #e6ded2;
      color: #17120d;
      font-size: 0.92em;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Перезапуск бота</h1>
    <p>Сервис: <code>{escaped_service}</code></p>
    <p id="bindingText"></p>
    <section id="status" class="status">Загрузка...</section>
    <div class="buttons">
      <button type="button" id="restartButton">Перезапустить</button>
      <button type="button" class="secondary" id="resetButton">Сбросить кнопку</button>
    </div>
  </main>

  <script>
    const TOKEN_KEY = "finalBotButtonToken";
    const BINDING_KEY = "finalBotButtonBinding";
    const params = new URLSearchParams(location.hash.replace(/^#/, ""));
    const tokenFromHash = params.get("token");
    if (tokenFromHash) {{
      localStorage.setItem(TOKEN_KEY, tokenFromHash);
      history.replaceState(null, "", location.pathname);
    }}

    const statusEl = document.getElementById("status");
    const bindingText = document.getElementById("bindingText");
    const restartButton = document.getElementById("restartButton");
    const resetButton = document.getElementById("resetButton");
    let busy = false;

    function token() {{
      return localStorage.getItem(TOKEN_KEY) || "";
    }}

    function binding() {{
      return localStorage.getItem(BINDING_KEY) || "";
    }}

    function setStatus(text, cls = "") {{
      statusEl.className = "status" + (cls ? " " + cls : "");
      statusEl.textContent = text;
    }}

    function renderBinding() {{
      const bound = binding();
      bindingText.textContent = "";
      if (bound) {{
        bindingText.appendChild(document.createTextNode("Кнопка привязана: "));
        const code = document.createElement("code");
        code.textContent = bound;
        bindingText.appendChild(code);
        bindingText.appendChild(document.createTextNode(". Нажмите ее для перезапуска."));
      }} else {{
        bindingText.textContent = "Нажмите нужную кнопку на мини-клавиатуре один раз, чтобы привязать ее.";
      }}
    }}

    async function request(path, method = "GET") {{
      const response = await fetch(path, {{
        method,
        headers: {{
          "Authorization": "Bearer " + token(),
          "Accept": "application/json"
        }},
        cache: "no-store"
      }});
      const payload = await response.json().catch(() => ({{}}));
      if (!response.ok) {{
        throw new Error(payload.error || "HTTP " + response.status);
      }}
      return payload;
    }}

    async function loadStatus() {{
      if (!token()) {{
        setStatus("Нет токена в ссылке", "err");
        return;
      }}
      try {{
        const payload = await request("/api/status");
        setStatus(payload.state === "active" ? "Бот работает" : "Состояние: " + payload.state, payload.state === "active" ? "ok" : "warn");
      }} catch (error) {{
        setStatus("Ошибка статуса: " + error.message, "err");
      }}
    }}

    async function restartBot() {{
      if (busy) return;
      busy = true;
      setStatus("Перезапускаю...", "warn");
      try {{
        const payload = await request("/api/restart", "POST");
        setStatus(payload.state === "active" ? "Бот перезапущен" : "После рестарта: " + payload.state, payload.state === "active" ? "ok" : "warn");
      }} catch (error) {{
        setStatus("Ошибка рестарта: " + error.message, "err");
      }} finally {{
        window.setTimeout(() => {{ busy = false; loadStatus(); }}, 3000);
      }}
    }}

    document.addEventListener("keydown", event => {{
      if (event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const code = event.code || event.key;
      if (!code) return;

      const bound = binding();
      if (!bound) {{
        event.preventDefault();
        localStorage.setItem(BINDING_KEY, code);
        renderBinding();
        setStatus("Кнопка привязана", "ok");
        return;
      }}

      if (code === bound) {{
        event.preventDefault();
        restartBot();
      }}
    }});

    restartButton.addEventListener("click", restartBot);
    resetButton.addEventListener("click", () => {{
      localStorage.removeItem(BINDING_KEY);
      renderBinding();
      setStatus("Нажмите кнопку для новой привязки", "warn");
    }});

    renderBinding();
    loadStatus();
  </script>
</body>
</html>"""


class ButtonControlHandler(BaseHTTPRequestHandler):
    server_version = "FinalBotButtonControl/1.0"

    def do_CONNECT(self):
        self.write_json(405, {"error": "method_not_allowed"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/button"}:
            self.write_html(200, build_control_page())
            return

        if parsed.path == "/health":
            self.write_json(200, {"ok": True})
            return

        if parsed.path == "/api/status":
            if not self.is_authorized():
                self.write_json(401, {"error": "unauthorized"})
                return
            self.write_json(200, get_service_status())
            return

        self.write_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/restart":
            self.write_json(404, {"error": "not_found"})
            return

        if not self.is_authorized():
            self.write_json(401, {"error": "unauthorized"})
            return

        current = time.time()
        with self.server.restart_lock:
            if current - self.server.last_restart_at < RESTART_COOLDOWN_SECONDS:
                self.write_json(429, {"error": "cooldown"})
                return
            self.server.last_restart_at = current

        try:
            logger.warning("Restart requested for %s from %s", SERVICE_NAME, self.client_address[0])
            code, output = run_systemctl("restart", SERVICE_NAME, timeout=30)
            status = get_service_status()
            status["restart_exit_code"] = code
            if output:
                status["message"] = output
            self.write_json(200 if code == 0 else 500, status)
        except Exception:
            logger.exception("Restart request failed")
            self.write_json(500, {"error": "restart_failed"})

    def is_authorized(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return False
        supplied = authorization.removeprefix("Bearer ").strip()
        return secrets.compare_digest(supplied, self.server.api_token)

    def write_html(self, status, text):
        encoded = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_json(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.client_address[0], fmt % args)


class ButtonControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, api_token):
        import threading

        super().__init__(server_address, handler_class)
        self.api_token = api_token
        self.restart_lock = threading.Lock()
        self.last_restart_at = 0.0

    def handle_error(self, request, client_address):
        exc_type, exc, _traceback = sys.exc_info()
        if exc_type and issubclass(exc_type, (BrokenPipeError, ConnectionResetError)):
            logger.debug("Client connection closed by %s: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


def main():
    host = os.environ.get("FINAL_BOT_BUTTON_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = get_int_env("FINAL_BOT_BUTTON_PORT", 8092)
    token = get_required_env("FINAL_BOT_BUTTON_TOKEN")
    server = ButtonControlServer((host, port), ButtonControlHandler, token)
    logger.info("Final bot button control listening on %s:%s for %s", host, port, SERVICE_NAME)
    server.serve_forever()


if __name__ == "__main__":
    main()
