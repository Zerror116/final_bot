"""Тихая сверка номеров: таблица заявок (E = 1) против базы клиентов.

Найденных присылает в Telegram только одному человеку (PHONE_MATCH_RECIPIENT_ID).
Таблицы только читаются, ничего не записывается.
"""
import logging
import re
from datetime import datetime, timedelta

from services.google_sheets_delivery import (
    get_delivery_sheet_config,
    get_google_sheets_http_timeout_seconds,
    get_sheets_service,
    parse_sheet_amount,
    quote_sheet_name,
)

logger = logging.getLogger(__name__)

PHONE_MATCH_RECIPIENT_ID = 5411051275
PHONE_MATCH_REQUESTS_SPREADSHEET_ID = "1RQ4Xx8iH1R_A_Y5OrNS4R0c5dPW5R5T89oBwotLxm0s"
PHONE_MATCH_BASE_SPREADSHEET_ID = "1NSnTphoqidUY0NyPlMZZDYLGwWzJquk4gjg482923x8"
PHONE_MATCH_WORKSHEET = "База"
PHONE_MATCH_CHECK_SECONDS = 600
PHONE_MATCH_RESEND_AFTER = timedelta(hours=20)
PHONE_MATCH_SUMMARY_HOUR = 17
PHONE_MATCH_MAX_DIFF = 2

MATCH_EXACT = "exact"
MATCH_LAST4 = "last4"
MATCH_FUZZY = "fuzzy"


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def digit_distance(a, b):
    """Сколько цифр надо заменить, вставить или убрать, чтобы из a получить b."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (char_a != char_b),
            ))
        previous = current
    return previous[-1]


def find_base_matches(phone, base_rows):
    """Полный номер → последние 4 цифры → отличие в 1–2 цифрах. Возвращает (тип, строки)."""
    if len(phone) < 4:
        return None, []

    exact = [row for row in base_rows if row["norm"] == phone]
    if exact:
        return MATCH_EXACT, exact

    last4 = [row for row in base_rows if len(row["norm"]) >= 4 and row["norm"][-4:] == phone[-4:]]
    if last4:
        return MATCH_LAST4, last4

    if len(phone) < 10:
        return None, []
    scored = [
        (digit_distance(phone, row["norm"]), row)
        for row in base_rows
        if len(row["norm"]) >= 10
    ]
    scored = [(distance, row) for distance, row in scored if distance <= PHONE_MATCH_MAX_DIFF]
    if not scored:
        return None, []
    best = min(distance for distance, _ in scored)
    return MATCH_FUZZY, [row for distance, row in scored if distance == best]


def parse_request_rows(values):
    requests = []
    for row in values:
        if len(row) < 5 or str(row[4]).strip() != "1":
            continue
        phone = normalize_phone(row[0])
        if phone:
            requests.append({"phone": str(row[0]).strip(), "norm": phone})
    return requests


def parse_base_rows(values):
    rows = []
    for row in values[1:]:
        if not row or not str(row[0]).strip():
            continue
        rows.append({
            "phone": str(row[0]).strip(),
            "norm": normalize_phone(row[0]),
            "name": str(row[1]).strip() if len(row) > 1 else "",
            "amount": parse_sheet_amount(row[2] if len(row) > 2 else ""),
        })
    return rows


def collect_matches(requests, base_rows):
    """Найденные клиенты базы с суммой > 0, ключ — номер из базы."""
    found = {}
    for request in requests:
        kind, rows = find_base_matches(request["norm"], base_rows)
        for row in rows:
            if row["amount"] <= 0:
                continue
            key = f"{row['norm']}|{row['name']}"
            if key in found:
                continue
            found[key] = {
                "phone": row["phone"],
                "name": row["name"],
                "kind": kind,
                "request_phone": request["phone"],
            }
    return found


def format_match_line(match):
    line = f"📞 {match['phone']} — {match['name'] or 'без имени'}"
    if match["kind"] == MATCH_LAST4:
        line += f"\n   ⚠️ совпали последние 4 цифры (в заявке {match['request_phone']})"
    elif match["kind"] == MATCH_FUZZY:
        line += f"\n   ⚠️ номер похож (в заявке {match['request_phone']})"
    return line


def read_sheet_values(spreadsheet_id, cell_range, service_account_file):
    service = get_sheets_service(service_account_file, get_google_sheets_http_timeout_seconds())
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{quote_sheet_name(PHONE_MATCH_WORKSHEET)}!{cell_range}",
    ).execute()
    return response.get("values", [])


def load_current_matches(service_account_file):
    requests = parse_request_rows(
        read_sheet_values(PHONE_MATCH_REQUESTS_SPREADSHEET_ID, "A:E", service_account_file)
    )
    base_rows = parse_base_rows(
        read_sheet_values(PHONE_MATCH_BASE_SPREADSHEET_ID, "A:C", service_account_file)
    )
    return collect_matches(requests, base_rows)


def plan_phone_match_messages(current, state, now):
    """Чистая логика: что отправить и каким станет состояние. now — наивное время Самары."""
    sent = dict(state.get("sent") or {})
    sent = {
        key: info for key, info in sent.items()
        if now - datetime.fromisoformat(info["at"]) < PHONE_MATCH_RESEND_AFTER
    }
    messages = []

    new_keys = [key for key in current if key not in sent]
    if new_keys:
        lines = [format_match_line(current[key]) for key in new_keys]
        messages.append("🆕 Найдены в базе:\n\n" + "\n".join(lines))
        for key in new_keys:
            sent[key] = {"at": now.isoformat(), "phone": current[key]["phone"], "name": current[key]["name"]}

    today = now.date().isoformat()
    new_state = {"sent": sent, "last_summary_date": state.get("last_summary_date")}
    if now.hour >= PHONE_MATCH_SUMMARY_HOUR and state.get("last_summary_date") != today:
        refused = [key for key in sent if key not in current]
        lines = [f"🔄 Проверка на {PHONE_MATCH_SUMMARY_HOUR}:00"]
        if current:
            lines.append("\nВ таблице сейчас:")
            lines.extend(format_match_line(match) for match in current.values())
        else:
            lines.append("\nВ таблице сейчас никого.")
        if refused:
            lines.append("\n❌ Отказались от доставки:")
            lines.extend(f"📞 {sent[key]['phone']} — {sent[key]['name'] or 'без имени'}" for key in refused)
            for key in refused:
                sent.pop(key)
        messages.append("\n".join(lines))
        new_state["last_summary_date"] = today

    return messages, new_state


def split_message(text, limit=4000):
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def run_phone_match_check(send_text, get_state, save_state, now, service_account_file=None):
    """Один проход. Ошибка чтения таблиц — исключение наверх, без «отказов».
    Если Telegram не принял сообщение, состояние не сохраняется и проход повторится."""
    service_account_file = service_account_file or get_delivery_sheet_config()["service_account_file"]
    current = load_current_matches(service_account_file)
    messages, new_state = plan_phone_match_messages(current, get_state(), now)
    for text in messages:
        for chunk in split_message(text):
            if send_text(PHONE_MATCH_RECIPIENT_ID, chunk) is None:
                logger.warning("Phone match message was not delivered, will retry next run")
                return 0
    save_state(new_state)
    return len(messages)
