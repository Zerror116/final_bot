import os
import re
import warnings
from functools import lru_cache

warnings.filterwarnings(
    "ignore",
    message="You are using a non-supported Python version.*",
    category=FutureWarning,
    module=r"google\.api_core\._python_version_support",
)

from google.oauth2.service_account import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
import httplib2


GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DELIVERY_OPERATION_MARKER_PREFIX = "delivery_item_"
DEFAULT_GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS = 20
DEFAULT_PAYROLL_SHEET_NAME = "Тесты"
PAYROLL_NAME_ALIASES = {
    "Серега": "Сергей",
    "Серёга": "Сергей",
}
PAYROLL_TOTAL_COLUMNS = 28
PAYROLL_DAY_COLUMNS = [
    ("Понедельник", "D", "E", "F"),
    ("Вторник", "G", "H", "I"),
    ("Среда", "J", "K", "L"),
    ("Четверг", "M", "N", "O"),
    ("Пятница", "P", "Q", "R"),
    ("Суббота", "S", "T", "U"),
    ("Воскресенье", "V", "W", "X"),
]


def get_int_env(name, default, minimum=None):
    raw_value = os.environ.get(name)
    try:
        value = int(str(raw_value).strip()) if raw_value is not None and str(raw_value).strip() else default
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


class DeliverySheetError(Exception):
    pass


class DeliverySheetConfigError(DeliverySheetError):
    pass


class DeliverySheetRowNotFound(DeliverySheetError):
    pass


def normalize_phone_for_sheet(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return f"8{digits}"
    if len(digits) == 11 and digits.startswith("7"):
        return f"8{digits[1:]}"
    return digits


def quote_sheet_name(sheet_name):
    return "'" + str(sheet_name).replace("'", "''") + "'"


def parse_sheet_amount(value):
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if not text:
        return 0

    is_negative = text.startswith("-")
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return 0

    amount = int(digits)
    return -amount if is_negative else amount


def normalize_delivery_operation_id(operation_id):
    text = str(operation_id or "").strip()
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)[:80]


def build_delivery_operation_marker(operation_id):
    normalized_id = normalize_delivery_operation_id(operation_id)
    if not normalized_id:
        return ""
    return f'N("{normalized_id}")'


def delivery_formula_has_marker(formula_value, operation_id):
    normalized_id = normalize_delivery_operation_id(operation_id)
    if not normalized_id:
        return False
    marker_pattern = r'\bN\s*\(\s*"' + re.escape(normalized_id) + r'"\s*\)'
    return re.search(marker_pattern, str(formula_value or ""), flags=re.IGNORECASE) is not None


def get_delivery_sheet_config():
    return {
        "spreadsheet_id": os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip(),
        "worksheet": os.environ.get("GOOGLE_SHEETS_WORKSHEET", "База").strip() or "База",
        "phone_column": os.environ.get("GOOGLE_SHEETS_PHONE_COLUMN", "A").strip().upper() or "A",
        "amount_column": os.environ.get("GOOGLE_SHEETS_AMOUNT_COLUMN", "C").strip().upper() or "C",
        "collector_column": os.environ.get("GOOGLE_SHEETS_COLLECTOR_COLUMN", "I").strip().upper() or "I",
        "first_data_row": get_int_env("GOOGLE_SHEETS_FIRST_DATA_ROW", 2, minimum=1),
        "service_account_file": os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip(),
    }


def get_payroll_sheet_config():
    delivery_config = get_delivery_sheet_config()
    return {
        "spreadsheet_id": delivery_config["spreadsheet_id"],
        "worksheet": os.environ.get("PAYROLL_SHEETS_WORKSHEET", DEFAULT_PAYROLL_SHEET_NAME).strip()
        or DEFAULT_PAYROLL_SHEET_NAME,
        "header_row": get_int_env("PAYROLL_SHEETS_HEADER_ROW", 5, minimum=1),
        "first_data_row": get_int_env("PAYROLL_SHEETS_FIRST_DATA_ROW", 6, minimum=1),
        "last_data_row": get_int_env("PAYROLL_SHEETS_LAST_DATA_ROW", 100, minimum=1),
        "service_account_file": delivery_config["service_account_file"],
    }


def validate_delivery_sheet_config(config):
    missing = [
        name
        for name in ("spreadsheet_id", "worksheet", "phone_column", "amount_column", "service_account_file")
        if not config.get(name)
    ]
    if missing:
        raise DeliverySheetConfigError("Не настроен Google Sheets: " + ", ".join(missing))

    if not os.path.exists(config["service_account_file"]):
        raise DeliverySheetConfigError(
            f"Файл сервисного аккаунта не найден: {config['service_account_file']}"
        )


def get_google_sheets_http_timeout_seconds():
    return get_int_env(
        "GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS",
        DEFAULT_GOOGLE_SHEETS_HTTP_TIMEOUT_SECONDS,
        minimum=1,
    )


@lru_cache(maxsize=4)
def get_sheets_service(service_account_file, timeout_seconds):
    credentials = Credentials.from_service_account_file(
        service_account_file,
        scopes=[GOOGLE_SHEETS_SCOPE],
    )
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))
    return build("sheets", "v4", http=http, cache_discovery=False)


def get_delivery_sheets_service(config):
    validate_delivery_sheet_config(config)
    return get_sheets_service(
        config["service_account_file"],
        get_google_sheets_http_timeout_seconds(),
    )


def get_payroll_sheets_service(config):
    validate_delivery_sheet_config({
        "spreadsheet_id": config.get("spreadsheet_id"),
        "worksheet": config.get("worksheet"),
        "phone_column": "A",
        "amount_column": "E",
        "service_account_file": config.get("service_account_file"),
    })
    return get_sheets_service(
        config["service_account_file"],
        get_google_sheets_http_timeout_seconds(),
    )


def find_sheet_id(service, spreadsheet_id, worksheet):
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title))",
    ).execute()
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == worksheet:
            return properties.get("sheetId")
    raise DeliverySheetRowNotFound(f"Лист {worksheet} не найден в Google Sheets.")


def normalize_payroll_name(value):
    name = str(value or "").strip()
    return PAYROLL_NAME_ALIASES.get(name, name)


def column_to_index(column):
    index = 0
    for char in str(column).upper():
        if not ("A" <= char <= "Z"):
            continue
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def build_payroll_day_hours_formula(row_number, start_column, end_column):
    start_cell = f"{start_column}{row_number}"
    end_cell = f"{end_column}{row_number}"
    return (
        f'=IF(OR({start_cell}="";{end_cell}="");"";'
        f'IFERROR(24*({end_cell}-{start_cell}+IF({end_cell}<{start_cell};1;0));""))'
    )


def payroll_day_hours_cells(row_number):
    return [f"{hours_column}{row_number}" for _day, _start, _end, hours_column in PAYROLL_DAY_COLUMNS]


def build_payroll_work_days_formula(row_number):
    return f'=COUNT({";".join(payroll_day_hours_cells(row_number))})'


def build_payroll_total_hours_formula(row_number):
    return f'=SUM({";".join(payroll_day_hours_cells(row_number))})'


def build_payroll_salary_formula(row_number):
    return (
        f'=IF(OR(C{row_number}="";Y{row_number}="");"";'
        f'C{row_number}*Y{row_number})'
    )


def build_payroll_group_header_row():
    row = [""] * PAYROLL_TOTAL_COLUMNS
    row[column_to_index("A")] = "Сотрудник"
    row[column_to_index("C")] = "Ставка"
    for day_name, start_column, _end_column, _hours_column in PAYROLL_DAY_COLUMNS:
        row[column_to_index(start_column)] = day_name
    row[column_to_index("Y")] = "Итоги недели"
    return row


def build_payroll_field_header_row():
    row = [""] * PAYROLL_TOTAL_COLUMNS
    row[column_to_index("A")] = "Кто"
    row[column_to_index("B")] = "Дни"
    row[column_to_index("C")] = "В час"
    for _day_name, start_column, end_column, hours_column in PAYROLL_DAY_COLUMNS:
        row[column_to_index(start_column)] = "Начало"
        row[column_to_index(end_column)] = "Конец"
        row[column_to_index(hours_column)] = "Часы"
    row[column_to_index("Y")] = "Часы за неделю"
    row[column_to_index("Z")] = "Выработка"
    row[column_to_index("AA")] = "Товаров в доставках"
    row[column_to_index("AB")] = "Зп"
    return row


def sync_payroll_sheet_week(post_counts, delivery_item_counts):
    config = get_payroll_sheet_config()
    service = get_payroll_sheets_service(config)
    spreadsheet_id = config["spreadsheet_id"]
    worksheet = config["worksheet"]
    sheet_id = find_sheet_id(service, spreadsheet_id, worksheet)

    first_row = config["first_data_row"]
    last_row = max(config["last_data_row"], first_row)
    sheet_name = quote_sheet_name(worksheet)
    name_range = f"{sheet_name}!A{first_row}:A{last_row}"
    name_response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=name_range,
        valueRenderOption="FORMATTED_VALUE",
    ).execute()

    rows = name_response.get("values", [])
    work_days_values = []
    total_values = []
    day_formula_ranges = []
    for offset, row in enumerate(rows):
        row_number = first_row + offset
        raw_name = row[0] if row else ""
        lookup_name = normalize_payroll_name(raw_name)
        work_days_values.append([build_payroll_work_days_formula(row_number)])
        if not lookup_name:
            total_values.append(["", "", "", ""])
            continue

        total_values.append([
            build_payroll_total_hours_formula(row_number),
            int(post_counts.get(lookup_name, 0) or 0),
            int(delivery_item_counts.get(lookup_name, 0) or 0),
            build_payroll_salary_formula(row_number),
        ])

    for _day_name, start_column, end_column, hours_column in PAYROLL_DAY_COLUMNS:
        day_formula_ranges.append({
            "range": f"{sheet_name}!{hours_column}{first_row}:{hours_column}{first_row + len(rows) - 1}",
            "values": [
                [build_payroll_day_hours_formula(first_row + offset, start_column, end_column)]
                for offset in range(len(rows))
            ],
        })

    data = [
        {
            "range": f"{sheet_name}!A{config['header_row'] - 1}:AB{config['header_row']}",
            "values": [
                build_payroll_group_header_row(),
                build_payroll_field_header_row(),
            ],
        }
    ]
    if rows:
        data.extend(day_formula_ranges)
        data.extend([
            {
                "range": f"{sheet_name}!B{first_row}:B{first_row + len(rows) - 1}",
                "values": work_days_values,
            },
            {
                "range": f"{sheet_name}!Y{first_row}:AB{first_row + len(rows) - 1}",
                "values": total_values,
            },
        ])

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": data,
        },
    ).execute()

    merge_ranges = [
        ("A", "B"),
        ("D", "F"),
        ("G", "I"),
        ("J", "L"),
        ("M", "O"),
        ("P", "R"),
        ("S", "U"),
        ("V", "X"),
        ("Y", "AB"),
    ]
    manual_time_columns = [
        column
        for _day_name, start_column, end_column, _hours_column in PAYROLL_DAY_COLUMNS
        for column in (start_column, end_column)
    ]
    formula_columns = ["B", "F", "I", "L", "O", "R", "U", "X", "Y", "Z", "AA", "AB"]
    requests = [
        {
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": config["header_row"] - 2,
                    "endRowIndex": config["header_row"] - 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": PAYROLL_TOTAL_COLUMNS,
                }
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": config["header_row"],
                        "frozenColumnCount": 2,
                    },
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": config["header_row"] - 2,
                    "endRowIndex": config["header_row"] - 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": PAYROLL_TOTAL_COLUMNS,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.12, "green": 0.24, "blue": 0.38},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                    }
                },
                "fields": (
                    "userEnteredFormat.backgroundColor,userEnteredFormat.horizontalAlignment,"
                    "userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat"
                ),
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": config["header_row"] - 1,
                    "endRowIndex": config["header_row"],
                    "startColumnIndex": 0,
                    "endColumnIndex": PAYROLL_TOTAL_COLUMNS,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.86, "green": 0.92, "blue": 0.98},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"bold": True},
                    }
                },
                "fields": (
                    "userEnteredFormat.backgroundColor,userEnteredFormat.horizontalAlignment,"
                    "userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat"
                ),
            }
        },
    ]

    for start_column, end_column in merge_ranges:
        requests.append({
            "mergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": config["header_row"] - 2,
                    "endRowIndex": config["header_row"] - 1,
                    "startColumnIndex": column_to_index(start_column),
                    "endColumnIndex": column_to_index(end_column) + 1,
                },
                "mergeType": "MERGE_ALL",
            }
        })

    for column in manual_time_columns:
        requests.extend([
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": first_row - 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": column_to_index(column),
                        "endColumnIndex": column_to_index(column) + 1,
                    },
                    "cell": {},
                    "fields": "dataValidation",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": first_row - 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": column_to_index(column),
                        "endColumnIndex": column_to_index(column) + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "TIME",
                                "pattern": "hh:mm",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            },
        ])

    for column in formula_columns:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": first_row - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": column_to_index(column),
                    "endColumnIndex": column_to_index(column) + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.96, "green": 0.98, "blue": 1.0},
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    for column in ["F", "I", "L", "O", "R", "U", "X", "Y"]:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": first_row - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": column_to_index(column),
                    "endColumnIndex": column_to_index(column) + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {
                            "type": "NUMBER",
                            "pattern": "0.00",
                        }
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    requests.extend([
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": first_row - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": column_to_index("Z"),
                    "endColumnIndex": column_to_index("AB") + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {
                            "type": "NUMBER",
                            "pattern": "# ##0",
                        }
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": PAYROLL_TOTAL_COLUMNS,
                }
            }
        },
    ])

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()

    return {
        "worksheet": worksheet,
        "rows": len(rows),
    }


def find_delivery_sheet_row(service, config, phone):
    normalized_phone = normalize_phone_for_sheet(phone)
    if not normalized_phone:
        raise DeliverySheetRowNotFound("У клиента нет номера телефона для поиска в Google Sheets.")

    sheet_name = quote_sheet_name(config["worksheet"])
    phone_range = (
        f"{sheet_name}!{config['phone_column']}{config['first_data_row']}:"
        f"{config['phone_column']}"
    )
    response = service.spreadsheets().values().get(
        spreadsheetId=config["spreadsheet_id"],
        range=phone_range,
        valueRenderOption="FORMATTED_VALUE",
    ).execute()

    values = response.get("values", [])
    for offset, row in enumerate(values):
        cell_phone = row[0] if row else ""
        if normalize_phone_for_sheet(cell_phone) == normalized_phone:
            return config["first_data_row"] + offset

    raise DeliverySheetRowNotFound(f"Телефон {normalized_phone} не найден на листе {config['worksheet']}.")


def read_delivery_sheet_amount_cell(service, config, row_number):
    sheet_name = quote_sheet_name(config["worksheet"])
    amount_range = f"{sheet_name}!{config['amount_column']}{row_number}"

    formula_response = service.spreadsheets().values().get(
        spreadsheetId=config["spreadsheet_id"],
        range=amount_range,
        valueRenderOption="FORMULA",
    ).execute()
    formula_values = formula_response.get("values", [])
    formula_value = formula_values[0][0] if formula_values and formula_values[0] else ""

    value_response = service.spreadsheets().values().get(
        spreadsheetId=config["spreadsheet_id"],
        range=amount_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    values = value_response.get("values", [])
    value = values[0][0] if values and values[0] else ""
    return parse_sheet_amount(value), formula_value


def read_delivery_sheet_amount(service, config, row_number):
    amount, _formula_value = read_delivery_sheet_amount_cell(service, config, row_number)
    return amount


def build_delivery_amount_update_value(formula_value, amount_before, amount_to_add, operation_id=None):
    amount_to_add = int(amount_to_add or 0)
    formula_text = str(formula_value or "").strip()
    marker = build_delivery_operation_marker(operation_id)
    marker_suffix = f"+{marker}" if marker else ""

    if formula_text.startswith("="):
        return f"{formula_text}+{amount_to_add}{marker_suffix}"
    if amount_before:
        return f"={int(amount_before)}+{amount_to_add}{marker_suffix}"
    return f"={amount_to_add}{marker_suffix}"


def add_delivery_amount(phone, amount, operation_id=None):
    amount = int(amount or 0)
    if amount <= 0:
        raise DeliverySheetError("Сумма товара для Google Sheets должна быть больше нуля.")

    config = get_delivery_sheet_config()
    service = get_delivery_sheets_service(config)
    row_number = find_delivery_sheet_row(service, config, phone)
    amount_before, formula_value = read_delivery_sheet_amount_cell(service, config, row_number)
    if operation_id and delivery_formula_has_marker(formula_value, operation_id):
        return {
            "row": row_number,
            "amount_before": amount_before,
            "amount_after": amount_before,
            "already_added": True,
        }

    amount_after = amount_before + amount
    update_value = build_delivery_amount_update_value(formula_value, amount_before, amount, operation_id)

    sheet_name = quote_sheet_name(config["worksheet"])
    amount_range = f"{sheet_name}!{config['amount_column']}{row_number}"
    service.spreadsheets().values().update(
        spreadsheetId=config["spreadsheet_id"],
        range=amount_range,
        valueInputOption="USER_ENTERED",
        body={"values": [[update_value]]},
    ).execute()

    return {
        "row": row_number,
        "amount_before": amount_before,
        "amount_after": amount_after,
        "already_added": False,
    }


def write_delivery_collector_name(phone, collector_name):
    collector_name = str(collector_name or "").strip()
    if not collector_name:
        raise DeliverySheetError("Имя сборщика для Google Sheets не указано.")

    config = get_delivery_sheet_config()
    service = get_delivery_sheets_service(config)
    row_number = find_delivery_sheet_row(service, config, phone)

    sheet_name = quote_sheet_name(config["worksheet"])
    collector_range = f"{sheet_name}!{config['collector_column']}{row_number}"
    service.spreadsheets().values().update(
        spreadsheetId=config["spreadsheet_id"],
        range=collector_range,
        valueInputOption="USER_ENTERED",
        body={"values": [[collector_name]]},
    ).execute()

    return {
        "row": row_number,
        "collector_name": collector_name,
    }
