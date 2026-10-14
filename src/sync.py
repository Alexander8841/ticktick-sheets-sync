"""
TickTick to Google Sheets State Reconciliation Engine.
Synchronizes completed tutoring tasks from TickTick SQLite database to Google Sheets
with two-way reconciliation, zero-duplicate guarantee, and manual row protection.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

T = TypeVar("T")


def call_with_retry(func: Callable[[], T], max_retries: int = 5, base_delay: float = 6.0) -> T:
    """Executes a function with automatic retry and backoff when encountering 429 Quota Exceeded."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (attempt + 1)
                print(f"⏳ Превышен лимит запросов Google Sheets (429). Ожидание {delay:.0f}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(delay)
            else:
                raise


# Set UTF-8 encoding for standard output and error in Windows terminal
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Local package imports
try:
    from src.config import AppConfig, get_config, load_config
except ImportError:
    # If run directly as a script without package context
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from src.config import AppConfig, get_config, load_config

# Google OAuth Scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_google_sheets_client(credentials_path: Optional[str] = None) -> gspread.Client:
    """
    Authenticates and returns a gspread Client.
    Checks explicit credentials path, user AppData/config, and refreshes expired tokens.
    """
    candidate_paths: List[Path] = []
    if credentials_path:
        candidate_paths.append(Path(credentials_path))

    # Standard gspread authorized user paths
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidate_paths.append(Path(appdata) / "gspread" / "authorized_user.json")
    candidate_paths.append(Path.home() / ".config" / "gspread" / "authorized_user.json")

    auth_file = None
    for p in candidate_paths:
        if p.exists() and p.is_file():
            auth_file = p
            break

    if auth_file:
        try:
            with open(auth_file, "r", encoding="utf-8") as f:
                info = json.load(f)

            # Check if service account or authorized user
            if info.get("type") == "service_account":
                from google.oauth2.service_account import Credentials as ServiceAccountCredentials
                creds = ServiceAccountCredentials.from_service_account_info(info, scopes=SCOPES)
            else:
                creds = Credentials.from_authorized_user_info(info, SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(auth_file, "w", encoding="utf-8") as f:
                        f.write(creds.to_json())

            return gspread.authorize(creds)
        except Exception as e:
            # Fall back to alternative discovery
            pass

    # Try importing from google-sheets skill helper if on local dev machine
    try:
        sheets_manager_path = Path.home() / ".gemini" / "config" / "skills" / "google-sheets" / "scripts"
        if sheets_manager_path.exists() and str(sheets_manager_path) not in sys.path:
            sys.path.append(str(sheets_manager_path))
        import sheets_manager
        return sheets_manager.get_gc()
    except Exception:
        pass

    # Default fallback to gspread interactive oauth
    return gspread.oauth(scopes=SCOPES)


def col_to_letter(col_idx: int) -> str:
    """Converts 1-based column index to spreadsheet column letter (1 -> A, 27 -> AA)."""
    result = ""
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result = chr(65 + remainder) + result
    return result


def datetime_to_serial(dt: datetime) -> float:
    """Converts a Python datetime object to a Google Sheets serial number."""
    base = datetime(1899, 12, 30)
    delta = dt - base
    return delta.days + (delta.seconds + delta.microseconds / 1e6) / 86400.0


def parse_sheet_datetime(val: Any) -> Optional[datetime]:
    """Parses various date/time representations from Google Sheets into a datetime object."""
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, (int, float)):
        if 30000 <= val <= 60000:
            base = datetime(1899, 12, 30)
            return base + timedelta(days=val)
        return None
    val_str = str(val).replace("\xa0", " ").strip()

    # Try parsing serial number string (e.g. "46277,70833" or "46277.70833")
    try:
        f_val = float(val_str.replace(",", "."))
        if 30000 <= f_val <= 60000:
            base = datetime(1899, 12, 30)
            return base + timedelta(days=f_val)
    except ValueError:
        pass

    for fmt in (
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(val_str, fmt)
        except ValueError:
            continue
    return None


def parse_sheet_duration(val: Any) -> Optional[float]:
    """Parses duration string or number from Google Sheets (e.g. '1,5 ч', '1.0', 1.5)."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).replace("\xa0", " ").replace(",", ".").replace("ч", "").strip()
    try:
        return float(val_str)
    except ValueError:
        return None


def is_manual_row(row: list) -> bool:
    """
    Checks if a row in 'Лог' is a manual or historical record that must not be deleted or modified.
    Protected: rows without Task ID (Column H missing, empty, or whitespace),
    placeholders ('-', '—', '–'), or manual IDs (starting with 'manual' or 'ручн').
    """
    if len(row) < 8:
        return True
    tid = str(row[7]).strip()
    if not tid or tid in ("-", "—", "–") or tid.lower() in ("none", "null", "нет", "ручной", "ручное"):
        return True
    if tid.lower().startswith("manual") or tid.lower().startswith("ручн"):
        return True
    return False


class SyncResult(tuple):
    """
    Result container for sync_google_sheets.
    Acts as a 3-tuple (added_count, new_tasks, sh) for full backward compatibility,
    while exposing detailed reconciliation lists: cancelled, updated, added.
    """

    def __new__(cls, added_count, new_tasks, sh, cancelled=None, updated=None):
        return super().__new__(cls, (added_count, new_tasks, sh))

    def __init__(self, added_count, new_tasks, sh, cancelled=None, updated=None):
        self.added_count = added_count
        self.new_tasks = new_tasks
        self.sh = sh
        self.cancelled = cancelled or []
        self.updated = updated or []


def normalize_student(title: str, known_students: Optional[List[str]] = None) -> str:
    """Normalizes task title to student name using known student list."""
    if not known_students:
        known_students = get_config().known_students
    clean_title = title.strip()

    # Sort known students by length descending to match 'Лиза 2' before 'Лиза'
    for student in sorted(known_students, key=len, reverse=True):
        pattern = r"(?:\b|^)" + re.escape(student) + r"(?:\b|$)"
        if re.search(pattern, clean_title, re.IGNORECASE):
            return student
    return clean_title


def get_known_students(
    sh: Optional[gspread.Spreadsheet] = None,
    config: Optional[AppConfig] = None
) -> Tuple[List[str], Dict[str, float]]:
    """Fetches known student names and default rates from Google Sheets 'Справочник' if available."""
    cfg = config or get_config()
    try:
        if not sh:
            gc = get_google_sheets_client(cfg.credentials_path)
            sh = call_with_retry(lambda: gc.open_by_key(cfg.spreadsheet_id))
        ws_ref = call_with_retry(lambda: sh.worksheet(cfg.reference_worksheet))
        vals = call_with_retry(lambda: ws_ref.get_all_values())
        names = []
        durations = {}
        for r in vals[1:]:
            if not r or not r[0] or "ИТОГО" in r[0].upper():
                continue
            name = r[0].strip()
            names.append(name)
            if len(r) > 1 and r[1]:
                dur_str = r[1].replace(",", ".").replace("ч", "").strip()
                try:
                    durations[name] = float(dur_str)
                except ValueError:
                    pass
        if names:
            return names, durations
    except Exception:
        pass
    return cfg.known_students, cfg.default_durations


def get_ticktick_tasks(
    known_students: Optional[List[str]] = None,
    default_durations: Optional[Dict[str, float]] = None,
    db_path: Optional[str] = None,
    project_id: Optional[str] = None,
    academic_year_start: Optional[str] = None,
    config: Optional[AppConfig] = None
) -> List[Dict[str, Any]]:
    """
    Reads completed tutoring tasks from TickTick SQLite DB in read-only mode for active academic year.
    Supports backward compatibility when db_path is passed as first positional parameter.
    """
    cfg = config or get_config()

    # Backward compatibility with legacy signature (db_path as first arg)
    if isinstance(known_students, str) and (known_students.endswith(".db") or "\\" in known_students or "/" in known_students):
        db_path = known_students
        known_students = None

    db_path = db_path or cfg.ticktick_db_path
    project_id = project_id or cfg.project_id
    academic_year_start = academic_year_start or cfg.academic_year_start
    known_students = known_students if known_students is not None else cfg.known_students
    default_durations = default_durations if default_durations is not None else cfg.default_durations
    ignore_keywords = cfg.ignore_keywords

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Файл базы данных TickTick не найден: {db_path}")

    # Read-only URI connection
    db_uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, title, startDate, dueDate, completedTime 
        FROM TaskModel 
        WHERE projectId = ? 
          AND status = 2 
          AND (deleted = 0 OR deleted IS NULL)
          AND COALESCE(startDate, dueDate, completedTime) >= ?
        ORDER BY COALESCE(startDate, dueDate, completedTime) ASC
    """,
        (project_id, academic_year_start),
    )

    rows = cur.fetchall()
    conn.close()

    tasks = []
    for r in rows:
        task_id, raw_title, start_str, due_str, comp_str = r
        raw_title = raw_title or ""

        # Skip ignored keywords (e.g. free trial lessons)
        lower_title = raw_title.lower()
        if any(kw in lower_title for kw in ignore_keywords):
            continue

        # Parse timestamps
        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S") if start_str else None
        due_dt = datetime.strptime(due_str, "%Y-%m-%d %H:%M:%S") if due_str else None
        comp_dt = datetime.strptime(comp_str, "%Y-%m-%d %H:%M:%S") if comp_str else None

        lesson_dt = start_dt or due_dt or comp_dt
        if not lesson_dt:
            continue

        student_name = normalize_student(raw_title, known_students)

        # Compute duration
        if start_dt and due_dt and due_dt > start_dt:
            duration = round((due_dt - start_dt).total_seconds() / 3600.0, 2)
        else:
            duration = default_durations.get(student_name, default_durations.get("_default", 1.0))

        tasks.append(
            {
                "id": task_id,
                "raw_title": raw_title,
                "student": student_name,
                "datetime": lesson_dt,
                "start_dt": start_dt,
                "due_dt": due_dt,
                "completed_dt": comp_dt,
                "duration": duration,
            }
        )

    return tasks


def sync_google_sheets(
    tasks: List[Dict[str, Any]],
    sh: Optional[gspread.Spreadsheet] = None,
    worksheet_name: Optional[str] = None,
    config: Optional[AppConfig] = None
) -> SyncResult:
    """
    Synchronizes tasks into Google Sheets worksheet with full two-way state reconciliation:
    1. Uncheck / Cancellation: rows with Task ID not present in active TickTick tasks are removed.
       Outputs: [ОТМЕНА] Удалено занятие: <Ученик> за <Дата>
    2. Reschedule / Duration change: existing rows with modified date/time/duration/student are updated.
       Outputs: [ПЕРЕНОС] Обновлено занятие: <Ученик>, новая дата: <Дата>
    3. Protection: manual/historical rows (no Task ID or starting with 'manual') are strictly protected.
    4. New tasks: added to sheet with proper formatting.
    5. Chronological sort: data rows (A2:H) remain sorted by date ascending.
    """
    cfg = config or get_config()
    worksheet_name = worksheet_name or cfg.log_worksheet

    if not sh:
        gc = get_google_sheets_client(cfg.credentials_path)
        sh = call_with_retry(lambda: gc.open_by_key(cfg.spreadsheet_id))
    ws_log = call_with_retry(lambda: sh.worksheet(worksheet_name))

    meta = call_with_retry(lambda: sh.fetch_sheet_metadata())
    log_sheet = [s for s in meta["sheets"] if s["properties"]["title"] == worksheet_name][0]
    sheet_id = log_sheet["properties"]["sheetId"]
    grid_props = log_sheet["properties"].get("gridProperties", {})
    current_grid_rows = grid_props.get("rowCount", 0)
    current_grid_cols = grid_props.get("columnCount", 0)

    # Ensure at least 8 columns exist for Task ID
    if current_grid_cols < 8:
        needed_cols = 8 - current_grid_cols
        call_with_retry(lambda: sh.batch_update(
            {
                "requests": [
                    {
                        "appendDimension": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "length": needed_cols,
                        }
                    }
                ]
            }
        ))
        current_grid_cols += needed_cols

    all_values = call_with_retry(lambda: ws_log.get_all_values())
    if not all_values:
        raise ValueError(f"Лист '{worksheet_name}' пуст!")

    header = all_values[0]
    header_updates = []

    # Ensure C1 is plain header 'Длительность (ч)'
    if len(header) > 2 and header[2] != "Длительность (ч)":
        call_with_retry(lambda: ws_log.update_acell("C1", "Длительность (ч)"))

    # Ensure H1 is 'Task ID'
    if len(header) < 8 or header[7] != "Task ID":
        call_with_retry(lambda: ws_log.update_acell("H1", "Task ID"))
        header_updates.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 7,
                        "endColumnIndex": 8,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.1098, "green": 0.2078, "blue": 0.3568},
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "textFormat": {
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                "fontSize": 10,
                                "bold": True,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
                }
            }
        )
    if header_updates:
        call_with_retry(lambda: sh.batch_update({"requests": header_updates}))

    current_tasks_map = {t["id"]: t for t in tasks}

    to_delete = []
    to_update = []
    existing_ids = set()
    seen_ids = set()

    for idx, row in enumerate(all_values[1:], start=2):
        if is_manual_row(row):
            # Strict protection: skip manual/historical records
            continue

        tid = row[7].strip()

        # Deduplication safeguard: prune duplicate occurrences of the same Task ID
        if tid in seen_ids:
            st_name = row[1].strip() if len(row) > 1 else "Неизвестно"
            sheet_dt = parse_sheet_datetime(row[0]) if len(row) > 0 else None
            dt_str = (
                sheet_dt.strftime("%d.%m.%Y %H:%M")
                if sheet_dt
                else (row[0].strip() if len(row) > 0 else "")
            )
            to_delete.append(
                {
                    "row_num": idx,
                    "0_idx": idx - 1,
                    "id": tid,
                    "student": st_name,
                    "date": dt_str,
                }
            )
            continue

        seen_ids.add(tid)
        existing_ids.add(tid)

        if tid not in current_tasks_map:
            # Task ID in sheet but not in active TickTick completed tasks
            # -> Unchecked or deleted in TickTick!
            st_name = row[1].strip() if len(row) > 1 else "Неизвестно"
            sheet_dt = parse_sheet_datetime(row[0]) if len(row) > 0 else None
            if sheet_dt:
                dt_str = sheet_dt.strftime("%d.%m.%Y %H:%M")
            else:
                dt_str = row[0].strip() if len(row) > 0 else ""
            to_delete.append(
                {
                    "row_num": idx,
                    "0_idx": idx - 1,
                    "id": tid,
                    "student": st_name,
                    "date": dt_str,
                }
            )
        else:
            t = current_tasks_map[tid]

            # Check date change
            sheet_dt = parse_sheet_datetime(row[0]) if len(row) > 0 else None
            date_changed = False
            if sheet_dt is not None:
                if abs((sheet_dt - t["datetime"]).total_seconds()) >= 60:
                    date_changed = True
            else:
                formatted_str = t["datetime"].strftime("%d.%m.%Y %H:%M")
                if len(row) == 0 or row[0].strip() != formatted_str:
                    date_changed = True

            # Check duration change
            sheet_dur = parse_sheet_duration(row[2]) if len(row) > 2 else None
            dur_changed = sheet_dur is None or abs(sheet_dur - t["duration"]) >= 0.01

            # Check student name change
            sheet_st = row[1].strip() if len(row) > 1 else ""
            st_changed = sheet_st != t["student"]

            if date_changed or dur_changed or st_changed:
                to_update.append(
                    {
                        "row_num": idx,
                        "task": t,
                        "old_student": sheet_st,
                        "old_date": row[0] if len(row) > 0 else "",
                        "old_dur": sheet_dur,
                    }
                )

    new_tasks = [t for t in tasks if t["id"] not in existing_ids]
    new_tasks.sort(key=lambda x: x["datetime"])

    # 1. Apply in-place updates first (row numbers are exact)
    if to_update:
        update_data = []
        for u in to_update:
            t = u["task"]
            serial_dt = datetime_to_serial(t["datetime"])
            update_data.append(
                {
                    "range": f"A{u['row_num']}:C{u['row_num']}",
                    "values": [[serial_dt, t["student"], t["duration"]]],
                }
            )
            new_date_str = t["datetime"].strftime("%d.%m.%Y %H:%M")
            print(f"[ПЕРЕНОС] Обновлено занятие: {t['student']}, новая дата: {new_date_str}")
        call_with_retry(lambda: ws_log.batch_update(update_data, value_input_option="USER_ENTERED"))

    # 2. Apply deletions in descending order of row index
    if to_delete:
        sorted_deletions = sorted(to_delete, key=lambda x: x["0_idx"], reverse=True)
        delete_requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": item["0_idx"],
                        "endIndex": item["0_idx"] + 1,
                    }
                }
            }
            for item in sorted_deletions
        ]
        call_with_retry(lambda: sh.batch_update({"requests": delete_requests}))
        current_grid_rows -= len(to_delete)
        for item in to_delete:
            print(f"[ОТМЕНА] Удалено занятие: {item['student']} за {item['date']}")

    # 3. Append new tasks
    rows_after_deletions = len(all_values) - len(to_delete)
    start_row = rows_after_deletions + 1
    added_count = 0

    if new_tasks:
        end_row = start_row + len(new_tasks) - 1

        # Grid limit protection: ensure worksheet has enough rows to hold new tasks
        if end_row > current_grid_rows:
            needed_rows = end_row - current_grid_rows
            call_with_retry(lambda: sh.batch_update(
                {
                    "requests": [
                        {
                            "appendDimension": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "length": needed_rows,
                            }
                        }
                    ]
                }
            ))
            current_grid_rows += needed_rows

        rows_abc = []
        rows_h = []
        for t in new_tasks:
            serial_dt = datetime_to_serial(t["datetime"])
            rows_abc.append([serial_dt, t["student"], t["duration"]])
            rows_h.append([t["id"]])

        call_with_retry(lambda: ws_log.update(range_name=f"A{start_row}:C{end_row}", values=rows_abc))
        call_with_retry(lambda: ws_log.update(range_name=f"H{start_row}:H{end_row}", values=rows_h))
        added_count = len(new_tasks)

    final_total_rows = rows_after_deletions + len(new_tasks)

    # 4. Format & Sort if any changes were made
    if to_update or to_delete or new_tasks:
        batch_requests = []

        if final_total_rows > 1:
            batch_requests.extend(
                [
                    # Col A: datetime format
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": final_total_rows,
                                "startColumnIndex": 0,
                                "endColumnIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "DATE_TIME",
                                        "pattern": "dd.mm.yyyy hh:mm",
                                    },
                                    "horizontalAlignment": "CENTER",
                                    "verticalAlignment": "MIDDLE",
                                }
                            },
                            "fields": "userEnteredFormat(numberFormat,horizontalAlignment,verticalAlignment)",
                        }
                    },
                    # Col C: duration format
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": final_total_rows,
                                "startColumnIndex": 2,
                                "endColumnIndex": 3,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "NUMBER",
                                        "pattern": '0.0 "ч"',
                                    },
                                    "horizontalAlignment": "CENTER",
                                    "verticalAlignment": "MIDDLE",
                                }
                            },
                            "fields": "userEnteredFormat(numberFormat,horizontalAlignment,verticalAlignment)",
                        }
                    },
                    # Col H: centered task ID
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": final_total_rows,
                                "startColumnIndex": 7,
                                "endColumnIndex": 8,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "horizontalAlignment": "CENTER",
                                    "verticalAlignment": "MIDDLE",
                                }
                            },
                            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)",
                        }
                    },
                    # Chronological sort across all data rows (row 2 to final_total_rows)
                    {
                        "sortRange": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": final_total_rows,
                                "startColumnIndex": 0,
                                "endColumnIndex": 8,
                            },
                            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
                        }
                    },
                ]
            )

        if batch_requests:
            call_with_retry(lambda: sh.batch_update({"requests": batch_requests}))

    return SyncResult(added_count, new_tasks, sh, cancelled=to_delete, updated=to_update)


def get_current_stats(
    sh: Optional[gspread.Spreadsheet] = None,
    config: Optional[AppConfig] = None
) -> dict:
    """Fetches summary data and checks formula integrity across all sheets."""
    cfg = config or get_config()
    if not sh:
        gc = get_google_sheets_client(cfg.credentials_path)
        sh = call_with_retry(lambda: gc.open_by_key(cfg.spreadsheet_id))

    ws_dash = call_with_retry(lambda: sh.worksheet(cfg.dashboard_worksheet))

    # Dashboard values
    month_name_map = {
        1: "Январь",
        2: "Февраль",
        3: "Март",
        4: "Апрель",
        5: "Май",
        6: "Июнь",
        7: "Июль",
        8: "Август",
        9: "Сентябрь",
        10: "Октябрь",
        11: "Ноябрь",
        12: "Декабрь",
    }

    # Batch fetch summary block B3:H7 in a single API call
    summary_grid = call_with_retry(lambda: ws_dash.get_values("B3:H7"))

    def safe_cell(grid, r_idx, c_idx, default=""):
        if r_idx < len(grid) and c_idx < len(grid[r_idx]):
            return grid[r_idx][c_idx]
        return default

    year_val = safe_cell(summary_grid, 0, 2)  # D3
    month_val = safe_cell(summary_grid, 0, 4)  # F3
    rev_val = safe_cell(summary_grid, 3, 0)  # B6
    hours_val = safe_cell(summary_grid, 3, 4)  # F6
    lessons_val = safe_cell(summary_grid, 3, 6)  # H6
    plan_val = safe_cell(summary_grid, 4, 0)  # B7
    diff_val = safe_cell(summary_grid, 4, 2)  # D7

    # Student breakdown from Dashboard table (rows 11..35)
    dash_table = call_with_retry(lambda: ws_dash.get_values("B11:K35"))
    student_stats = []
    for r in dash_table:
        if not r or not r[0] or r[0].upper() == "ИТОГО":
            continue
        student_name = r[0]
        st_lessons = r[3] if len(r) > 3 else "0"
        st_hours = r[4] if len(r) > 4 else "0,0"
        st_rev = r[6] if len(r) > 6 else "0 ₽"
        student_stats.append(
            {
                "name": student_name,
                "lessons": st_lessons,
                "hours": st_hours,
                "revenue": st_rev,
            }
        )

    # Check formula errors across all worksheets
    errors = []
    all_worksheets = call_with_retry(lambda: sh.worksheets())
    for s in all_worksheets:
        vals = call_with_retry(lambda: s.get_all_values())
        for r_idx, row in enumerate(vals):
            for c_idx, val in enumerate(row):
                v_str = str(val).strip()
                if v_str.startswith("#") and v_str.endswith("!"):
                    col_str = col_to_letter(c_idx + 1)
                    errors.append(f"Лист '{s.title}', ячейка {col_str}{r_idx+1}: {v_str}")

    return {
        "year": year_val,
        "month": (
            month_name_map.get(int(month_val), str(month_val))
            if str(month_val).isdigit()
            else str(month_val)
        ),
        "revenue": rev_val,
        "plan": plan_val,
        "diff": diff_val,
        "hours": hours_val,
        "lessons": lessons_val,
        "student_stats": student_stats,
        "errors": errors,
    }


def print_report(added_count: int, new_tasks: list, stats: dict, cancelled: Optional[list] = None, updated: Optional[list] = None):
    """Outputs a clean, readable console report for Google Sheets synchronization."""
    cancelled = cancelled or []
    updated = updated or []

    print("\n" + "═" * 68)
    print(" 📚  СИНХРОНИЗАЦИЯ ЗАНЯТИЙ РЕПЕТИТОРА | TickTick ➔ Google Sheets")
    print("═" * 68)

    has_changes = added_count > 0 or len(cancelled) > 0 or len(updated) > 0

    if cancelled:
        print(f"\n🗑️  ОТМЕНЕННЫЕ ЗАНЯТИЯ (удалено из таблицы: {len(cancelled)}):")
        for idx, c in enumerate(cancelled, 1):
            print(f"   {idx}. [ОТМЕНА] Удалено занятие: {c['student']} за {c['date']}  |  ID: {c['id']}")

    if updated:
        print(f"\n🔄  ПЕРЕНЕСЕННЫЕ / ИЗМЕНЕННЫЕ ЗАНЯТИЯ (обновлено: {len(updated)}):")
        for idx, u in enumerate(updated, 1):
            t = u["task"]
            dt_str = t["datetime"].strftime("%d.%m.%Y %H:%M")
            print(
                f"   {idx}. [ПЕРЕНОС] Обновлено занятие: {t['student']}, новая дата: {dt_str} ({t['duration']} ч)  |  ID: {t['id']}"
            )

    if added_count > 0:
        print(f"\n✅  ДОБАВЛЕНО В GOOGLE SHEETS: {added_count} новых занятий:")
        for idx, t in enumerate(new_tasks, 1):
            dt_str = t["datetime"].strftime("%d.%m.%Y %H:%M")
            print(f"   {idx}. {dt_str}  |  {t['student']:<10}  |  {t['duration']} ч  |  ID: {t['id']}")

    if not has_changes:
        print("\n✨ Все занятия уже были синхронизированы ранее (новых уроков: 0).")

    # Monthly stats
    print("\n" + "─" * 68)
    print(f" 📊  ИТОГИ ЗА МЕСЯЦ ({stats['month']} {stats['year']}):")
    print("─" * 68)
    print(f"   • Выручка:             {stats['revenue']} ({stats['plan']}, {stats['diff']})")
    print(f"   • Отработано часов:    {stats['hours']}")
    print(f"   • Проведено уроков:    {stats['lessons']}")

    if stats["student_stats"]:
        print("\n   Детализация по ученикам:")
        for s in stats["student_stats"]:
            print(f"     - {s['name']:<10} : {s['lessons']:>2} ур. | {s['hours']:>5} ч | {s['revenue']}")

    # Formula health check
    print("\n" + "─" * 68)
    print(" 🔍  ПРОВЕРКА ФОРМУЛ (ТАБЛИЦА И ДАШБОРД):")
    if not stats["errors"]:
        print("   ✅ Все формулы на всех листах работают идеально (0 ошибок #REF!, #VALUE!).")
    else:
        print(f"   ⚠️  Обнаружены ошибки формул ({len(stats['errors'])}):")
        for err in stats["errors"][:5]:
            print(f"      • {err}")
    print("═" * 68 + "\n")


def main():
    parser = argparse.ArgumentParser(description="TickTick to Google Sheets Sync Engine")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to custom config.yaml")
    parser.add_argument("--no-pause", action="store_true", help="Do not prompt for pause on exit")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)

        # 1. Load Google Sheets client
        gc = get_google_sheets_client(cfg.credentials_path)
        sh = call_with_retry(lambda: gc.open_by_key(cfg.spreadsheet_id))
        known_students, default_durations = get_known_students(sh, cfg)

        # 2. Fetch TickTick tasks
        tasks = get_ticktick_tasks(
            known_students=known_students,
            default_durations=default_durations,
            config=cfg,
        )

        # 3. Sync to Google Sheets
        res = sync_google_sheets(tasks, sh, config=cfg)

        # 4. Fetch updated dashboard stats
        stats = get_current_stats(res.sh, config=cfg)

        # 5. Output console report
        print_report(res.added_count, res.new_tasks, stats, cancelled=res.cancelled, updated=res.updated)

    except Exception as e:
        print(f"\n❌ Критическая ошибка при синхронизации: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
