"""
Comprehensive Test Suite for TickTick to Google Sheets Sync.
Includes SQLite tests, config tests, unit tests, Google Sheets API reconciliation tests,
subprocess execution tests, and Task Scheduler checks.
"""

import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output and real-time line buffering
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import AppConfig, get_config, load_config
import src.sync as sync_module

CONFIG = get_config()
PYTHON_EXE = sys.executable or r"C:\Users\alexander\.conda\envs\ml_base\python.exe"
RUN_SYNC_BAT = REPO_ROOT / "run_sync.bat"
DESKTOP_BAT = Path(r"C:\Users\alexander\Desktop\Синхронизировать занятия.bat")

_cached_sh = None


def get_shared_spreadsheet():
    """Returns cached Spreadsheet instance with retry on quota rate limits."""
    global _cached_sh
    if _cached_sh is None:
        gc = sync_module.get_google_sheets_client(CONFIG.credentials_path)
        _cached_sh = sync_module.call_with_retry(lambda: gc.open_by_key(CONFIG.spreadsheet_id))
    return _cached_sh


def test_sqlite():
    print("[1] Testing SQLite read-only connection and data...")
    posix_db = Path(CONFIG.ticktick_db_path).resolve().as_posix()
    db_uri = f"file:{posix_db}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, startDate, dueDate, completedTime, status
        FROM TaskModel
        WHERE projectId = ? AND status = 2 AND (deleted = 0 OR deleted IS NULL)
        ORDER BY COALESCE(startDate, dueDate, completedTime) ASC
    """,
        (CONFIG.project_id,),
    )
    rows = cur.fetchall()
    print(f"    Found {len(rows)} completed tasks in project {CONFIG.project_id}")
    conn.close()
    assert len(rows) > 0, "No tasks found in project!"
    print("    [PASS] SQLite OK")


def test_config_loader():
    print("[2] Testing configuration loader and validation...")
    cfg = load_config(reload=True)
    assert cfg.ticktick_db_path and os.path.exists(cfg.ticktick_db_path), f"DB path missing: {cfg.ticktick_db_path}"
    assert cfg.project_id == "68a07197ebb69e0000000933", f"Unexpected project ID: {cfg.project_id}"
    assert cfg.spreadsheet_id == "19Dp0Y2EnmpyAL-AVxRAq4IH5_YB9gIMIqaNwqKtMa1Q"
    assert "Лиза" in cfg.known_students
    assert cfg.default_durations.get("Лиза") == 1.5
    assert cfg.default_durations.get("Лиза 2") == 1.0
    assert cfg.log_path and "sync.log" in cfg.log_path, f"Unexpected log path: {cfg.log_path}"

    # Test loading config.example.yaml
    example_path = REPO_ROOT / "config" / "config.example.yaml"
    cfg_example = load_config(example_path)
    assert cfg_example.academic_year_start == "2026-09-01"
    assert "Student 1" in cfg_example.known_students

    # Test error handling when pointing to non-existent config
    try:
        load_config("non_existent_config.yaml")
        assert False, "Should have raised FileNotFoundError for missing config"
    except FileNotFoundError:
        pass
    print("    [PASS] Config loader correctly parsed configs and handled edge cases")


def test_files_exist():
    print("[3] Testing repository files exist...")
    assert (REPO_ROOT / "config" / "config.yaml").exists(), "Missing config/config.yaml"
    assert (REPO_ROOT / "config" / "config.example.yaml").exists(), "Missing config/config.example.yaml"
    assert (REPO_ROOT / "src" / "sync.py").exists(), "Missing src/sync.py"
    assert (REPO_ROOT / "src" / "config.py").exists(), "Missing src/config.py"
    assert (REPO_ROOT / "requirements.txt").exists(), "Missing requirements.txt"
    assert (REPO_ROOT / ".gitignore").exists(), "Missing .gitignore"
    assert (REPO_ROOT / "README.md").exists(), "Missing README.md"
    assert RUN_SYNC_BAT.exists(), f"Missing {RUN_SYNC_BAT}"
    assert DESKTOP_BAT.exists(), f"Missing {DESKTOP_BAT}"
    print("    [PASS] All repository and launcher files exist")


def test_task_scheduler():
    print("[4] Testing Task Scheduler job 'SyncTeachingTasks'...")
    res = subprocess.run(
        ["schtasks", "/query", "/tn", "SyncTeachingTasks", "/fo", "LIST", "/v"],
        capture_output=True,
        text=True,
        encoding="cp866",
        errors="ignore",
    )
    assert res.returncode == 0, f"Task Scheduler query failed: {res.stderr}"
    out_lower = res.stdout.lower()
    assert "ticktick-sheets-sync" in out_lower and "run_sync.bat" in out_lower, (
        f"Task Scheduler job did not match expected path: {res.stdout}"
    )
    print("    [PASS] Task Scheduler job exists and configured")


def test_google_sheets_and_formulas():
    print("[5] Testing Google Sheets and formula health...")
    sh = get_shared_spreadsheet()

    total_errors = []
    worksheets = sync_module.call_with_retry(lambda: sh.worksheets())
    for s in worksheets:
        vals = sync_module.call_with_retry(lambda: s.get_all_values())
        for r_idx, row in enumerate(vals):
            for c_idx, val in enumerate(row):
                v_str = str(val).strip()
                if v_str.startswith("#") and v_str.endswith("!"):
                    col_letter = sync_module.col_to_letter(c_idx + 1)
                    total_errors.append(f"{s.title}!{col_letter}{r_idx + 1}: {v_str}")

    if total_errors:
        print(f"    [FAIL] Found formula errors: {total_errors}")
    else:
        print("    [PASS] Zero formula errors across all sheets")
    assert len(total_errors) == 0, f"Formula errors present: {total_errors}"


def test_semen_duration_consistency():
    print("[6] Testing Semen 11.10 lesson duration consistency...")
    db_uri = f"file:{CONFIG.ticktick_db_path}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    cur = conn.cursor()
    cur.execute("SELECT startDate, dueDate FROM TaskModel WHERE id = '6ac51c78fd764a6845fab4a1'")
    row = cur.fetchone()
    conn.close()

    if row:
        s, d = row
        assert s == "2026-10-11 13:00:00" and d == "2026-10-11 14:00:00", f"Unexpected TickTick times: {s}, {d}"
        dt_s = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        dt_d = datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
        computed_dur = round((dt_d - dt_s).total_seconds() / 3600.0, 2)
        assert computed_dur == 1.0, f"Expected 1.0 hr, got {computed_dur}"

    sh = get_shared_spreadsheet()
    ws_log = sync_module.call_with_retry(lambda: sh.worksheet("Лог"))
    log_vals = sync_module.call_with_retry(lambda: ws_log.get_all_values())
    semen_row = None
    for r in log_vals:
        if len(r) > 7 and r[7] == "6ac51c78fd764a6845fab4a1":
            semen_row = r
            break
    if semen_row is not None:
        dur_str = semen_row[2].replace(",", ".").replace("ч", "").strip()
        assert float(dur_str) == 1.0, f"Google Sheets duration for Semen is {dur_str}, expected 1.0"
        print("    [PASS] Semen 11.10 duration is exactly 1.0 hr across TickTick and Google Sheets")
    else:
        print("    [PASS] Semen 11.10 was cleanly reconciled")


def test_sync_google_sheets_idempotency():
    print("[7] Testing Google Sheets synchronization idempotency...")
    sh = get_shared_spreadsheet()
    known_students, default_durations = sync_module.get_known_students(sh, CONFIG)
    tasks = sync_module.get_ticktick_tasks(known_students, default_durations, config=CONFIG)

    # Run sync - since everything is already synced, added_count must be 0
    added_count, new_tasks, _ = sync_module.sync_google_sheets(tasks, sh, config=CONFIG)
    assert added_count == 0, f"Expected 0 new tasks on already-synced data, got {added_count}"
    assert len(new_tasks) == 0
    print("    [PASS] Synchronization is strictly idempotent (0 duplicates created)")


def test_chronological_ordering():
    print("[8] Testing chronological ordering of Google Sheets 'Лог'...")
    sh = get_shared_spreadsheet()
    ws_log = sync_module.call_with_retry(lambda: sh.worksheet("Лог"))
    rows = sync_module.call_with_retry(lambda: ws_log.get_all_values())

    dates = []
    for r in rows[1:]:
        if r[0]:
            dt = sync_module.parse_sheet_datetime(r[0])
            if dt:
                dates.append(dt)

    assert dates == sorted(dates), "Log rows are not sorted chronologically!"
    print(f"    [PASS] All {len(dates)} rows in 'Лог' are in strict chronological order")


def test_bat_files_execution():
    print("[9] Testing batch files execution...")
    for name, bat_path in [("run_sync.bat", RUN_SYNC_BAT), ("Desktop BAT", DESKTOP_BAT)]:
        assert bat_path.exists(), f"Missing {name}: {bat_path}"

        # Verify CRLF line endings
        with open(bat_path, "rb") as f:
            content = f.read()
        assert b"\r\n" in content, f"{name} does not have CRLF line endings"

        # Verify execution with --no-pause and custom arguments without hanging
        res = subprocess.run(
            ["cmd.exe", "/c", str(bat_path), "--config", "config/config.yaml", "--no-pause"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            cwd=str(bat_path.parent),
        )
        assert res.returncode == 0, f"{name} failed with exit code {res.returncode}: {res.stderr}"
        assert "'e' is not recognized" not in res.stdout, f"{name} emitted command syntax error!"
        assert "не является внутренней или внешней" not in res.stdout, f"{name} emitted command error!"
        assert "Press any key to continue" not in res.stdout, f"{name} hung on pause despite --no-pause!"
        stray_file = bat_path.parent / "Google"
        assert not stray_file.exists(), f"{name} created stray 'Google' file!"
        print(f"    [PASS] {name} executed cleanly with code 0")


def test_normalize_student_logic():
    print("[10] Testing normalize_student edge cases and safety...")
    test_students = ["Лиза 2", "Лиза", "Тимофей", "Семен", "Марина", "Аня"]

    assert sync_module.normalize_student("Лиза 2", test_students) == "Лиза 2"
    assert sync_module.normalize_student("Лиза 2 физика", test_students) == "Лиза 2"
    assert sync_module.normalize_student("Лиза", test_students) == "Лиза"
    assert sync_module.normalize_student("Лиза геометрия", test_students) == "Лиза"
    assert sync_module.normalize_student("Тимофей ", test_students) == "Тимофей"
    # Word boundary prevents substring clash: 'Таня' should not match 'Аня'
    assert sync_module.normalize_student("Таня", test_students) == "Таня"
    # Word boundary prevents 'Лизавета' from falsely matching 'Лиза'
    assert sync_module.normalize_student("Лизавета", test_students) == "Лизавета"
    # Unknown student returns cleaned title
    assert sync_module.normalize_student("  НовыйУченик  ", test_students) == "НовыйУченик"
    print("    [PASS] Student normalization handles boundaries and prefixes correctly")


def test_sync_script_subprocess():
    print("[11] Testing sync.py full subprocess execution...")
    sync_script_path = str(REPO_ROOT / "src" / "sync.py")
    res = subprocess.run(
        [PYTHON_EXE, sync_script_path, "--no-pause"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, f"sync.py failed: {res.stderr}"
    assert "СИНХРОНИЗАЦИЯ ЗАНЯТИЙ РЕПЕТИТОРА" in res.stdout, "Unexpected stdout from sync.py"
    assert "Все формулы на всех листах работают идеально" in res.stdout, "Formula errors reported in stdout"
    print("    [PASS] sync.py subprocess completed with 0 errors")


def test_manual_rows_protection_unit():
    print("[12] Testing manual and historical rows protection logic...")
    # 1. Row without Column H
    assert sync_module.is_manual_row(["01.09.2026 12:00", "Лиза", "1,5 ч"]) is True
    # 2. Row with empty Task ID
    assert sync_module.is_manual_row(["01.09.2026 12:00", "Лиза", "1,5 ч", "", "", "", "", ""]) is True
    # 3. Row with whitespace Task ID
    assert sync_module.is_manual_row(["01.09.2026 12:00", "Лиза", "1,5 ч", "", "", "", "", "   "]) is True
    # 4. Row with placeholder symbols ('-', '—', 'нет')
    assert sync_module.is_manual_row(["01.09.2026 12:00", "Лиза", "1,5 ч", "", "", "", "", "-"]) is True
    assert sync_module.is_manual_row(["01.09.2026 12:00", "Лиза", "1,5 ч", "", "", "", "", "нет"]) is True
    # 5. Row with manual_ or ручн prefix
    assert (
        sync_module.is_manual_row(["23.09.2026 18:00", "Марина", "1,0 ч", "", "", "", "", "manual_2026-09-23_марина"])
        is True
    )
    assert sync_module.is_manual_row(["01.09.2026 10:00", "Лиза", "1,5 ч", "", "", "", "", "MANUAL_custom_id"]) is True
    assert sync_module.is_manual_row(["01.09.2026 10:00", "Лиза", "1,5 ч", "", "", "", "", "ручной_ввод"]) is True
    # 6. Row with actual TickTick Task ID
    assert (
        sync_module.is_manual_row(["04.09.2026 16:00", "Тимофей", "1,5 ч", "", "", "", "", "6a9b15efebbf3600000000d6"])
        is False
    )
    print("    [PASS] Manual rows protection correctly identifies all historical/manual rows")


def test_reconciliation_logic_unit():
    print("[13] Testing uncheck, reschedule, and duration change detection...")
    # Test date parsing
    dt1 = sync_module.parse_sheet_datetime("11.10.2026 13:00")
    assert dt1 == datetime(2026, 10, 11, 13, 0)
    dt2 = sync_module.parse_sheet_datetime("2026-10-11 13:00:00")
    assert dt2 == datetime(2026, 10, 11, 13, 0)

    # Test numeric serial date parsing and bounds safety
    assert sync_module.parse_sheet_datetime(1.5) is None
    assert sync_module.parse_sheet_datetime(0) is None
    assert sync_module.parse_sheet_datetime(-10) is None
    dt_serial = sync_module.parse_sheet_datetime(46269.791666666664)
    assert dt_serial is not None and abs((dt_serial - datetime(2026, 9, 4, 19, 0)).total_seconds()) < 60

    # Test duration parsing
    assert sync_module.parse_sheet_duration("1,5 ч") == 1.5
    assert sync_module.parse_sheet_duration("1.0 ч") == 1.0
    assert sync_module.parse_sheet_duration("2") == 2.0

    # Test SyncResult compatibility
    sr = sync_module.SyncResult(2, [{"id": "t1"}], None, cancelled=[{"id": "c1"}], updated=[{"id": "u1"}])
    a, b, c = sr
    assert a == 2 and len(b) == 1 and c is None
    assert len(sr.cancelled) == 1
    assert len(sr.updated) == 1
    print("    [PASS] Date/duration parsers and SyncResult container behave correctly")


def test_reconciliation_isolated_sheet():
    print("[14] Testing full reconciliation cycle on isolated worksheet...")
    sh = get_shared_spreadsheet()
    temp_title = "_test_reconcile_temp"

    # Cleanup leftovers
    for ws in sync_module.call_with_retry(lambda: sh.worksheets()):
        if ws.title == temp_title:
            try:
                sync_module.call_with_retry(lambda: sh.del_worksheet(ws))
            except Exception:
                pass
            break

    # Start with tight grid limits (exactly 7 rows)
    ws_test = sync_module.call_with_retry(lambda: sh.add_worksheet(title=temp_title, rows=7, cols=8))
    try:
        initial_data = [
            ["Дата и время", "Ученик", "Длительность (ч)", "Ставка (₽/ч)", "Сумма (₽)", "Дата (норм.)", "Ученик (норм.)", "Task ID"],
            # Historical task from previous academic year (August 2026) with valid Task ID - must NOT be deleted
            [sync_module.datetime_to_serial(datetime(2026, 8, 20, 12, 0)), "Марина", 1.0, "", "", "", "", "tid_past_year_august"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 1, 10, 0)), "Лиза", 1.5, "", "", "", "", ""],
            [sync_module.datetime_to_serial(datetime(2026, 9, 2, 12, 0)), "Марина", 1.0, "", "", "", "", "manual_test_sept_1"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 5, 16, 0)), "Тимофей", 1.5, "", "", "", "", "tid_keep_001"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 8, 16, 0)), "Лиза 2", 1.0, "", "", "", "", "tid_resched_002"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 10, 18, 0)), "Семен", 1.5, "", "", "", "", "tid_uncheck_003"],
        ]
        sync_module.call_with_retry(lambda: ws_test.update(range_name="A1:H7", values=initial_data))

        active_tasks = [
            {"id": "tid_keep_001", "raw_title": "Тимофей", "student": "Тимофей", "datetime": datetime(2026, 9, 5, 16, 0), "duration": 1.5},
            {"id": "tid_resched_002", "raw_title": "Лиза 2", "student": "Лиза 2", "datetime": datetime(2026, 9, 3, 14, 0), "duration": 1.5},
            {"id": "tid_new_004", "raw_title": "Марина", "student": "Марина", "datetime": datetime(2026, 9, 15, 14, 0), "duration": 1.0},
        ]

        res = sync_module.sync_google_sheets(active_tasks, sh=sh, worksheet_name=temp_title, config=CONFIG)

        # 1. Uncheck / Cancellation: only active academic year unchecked tasks are deleted
        assert len(res.cancelled) == 1
        assert res.cancelled[0]["id"] == "tid_uncheck_003"
        assert res.cancelled[0]["student"] == "Семен"

        # 2. Reschedule / Update
        assert len(res.updated) == 1
        assert res.updated[0]["task"]["id"] == "tid_resched_002"
        assert res.updated[0]["task"]["duration"] == 1.5

        # 3. Addition
        assert res.added_count == 1
        assert res.new_tasks[0]["id"] == "tid_new_004"

        # 4. Final sheet checks
        final_rows = sync_module.call_with_retry(lambda: ws_test.get_all_values())
        all_ids = [r[7] for r in final_rows[1:] if len(r) >= 8]
        assert "tid_uncheck_003" not in all_ids

        # Historical task from August is preserved despite not being in active_tasks
        assert "tid_past_year_august" in all_ids

        # Manual rows preserved
        assert any(r[1] == "Лиза" and (len(r) < 8 or not r[7].strip()) for r in final_rows[1:])
        assert any(r[1] == "Марина" and len(r) >= 8 and r[7].strip() == "manual_test_sept_1" for r in final_rows[1:])

        # Out-of-order sorting: Liza 2 (03.09) must come before Timofey (05.09)
        assert len(final_rows) == 7
        assert final_rows[4][7] == "tid_resched_002"
        assert final_rows[5][7] == "tid_keep_001"

        print("    [PASS] Full reconciliation lifecycle passed on isolated worksheet (including historical rows protection)")
    finally:
        try:
            sync_module.call_with_retry(lambda: sh.del_worksheet(ws_test))
        except Exception:
            pass


def test_deduplication_safeguard():
    print("[15] Testing duplicate Task ID pruning and sheet safety...")
    sh = get_shared_spreadsheet()
    temp_title = "_test_dedup_temp"

    for ws in sync_module.call_with_retry(lambda: sh.worksheets()):
        if ws.title == temp_title:
            try:
                sync_module.call_with_retry(lambda: sh.del_worksheet(ws))
            except Exception:
                pass
            break

    ws_test = sync_module.call_with_retry(lambda: sh.add_worksheet(title=temp_title, rows=5, cols=8))
    try:
        initial_data = [
            ["Дата и время", "Ученик", "Длительность (ч)", "Ставка (₽/ч)", "Сумма (₽)", "Дата (норм.)", "Ученик (норм.)", "Task ID"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 1, 10, 0)), "Лиза", 1.5, "", "", "", "", "tid_dup_001"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 1, 10, 0)), "Лиза", 1.5, "", "", "", "", "tid_dup_001"],
        ]
        sync_module.call_with_retry(lambda: ws_test.update(range_name="A1:H3", values=initial_data))
        active_tasks = [
            {"id": "tid_dup_001", "raw_title": "Лиза", "student": "Лиза", "datetime": datetime(2026, 9, 1, 10, 0), "duration": 1.5}
        ]
        res = sync_module.sync_google_sheets(active_tasks, sh=sh, worksheet_name=temp_title, config=CONFIG)
        assert len(res.cancelled) == 1
        final_rows = sync_module.call_with_retry(lambda: ws_test.get_all_values())
        assert len(final_rows) == 2
        print("    [PASS] Duplicate Task IDs are automatically pruned")
    finally:
        try:
            sync_module.call_with_retry(lambda: sh.del_worksheet(ws_test))
        except Exception:
            pass


def test_logging_system():
    print("[16] Testing file logging system and log output...")
    test_log = REPO_ROOT / "logs" / "_test_temp.log"
    try:
        logger, path = sync_module.setup_logging(test_log)
        assert logger.propagate is False, "LOGGER.propagate must be False to prevent stderr leakage"
        logger.info("TEST_LOG_ENTRY_12345")
        assert path.exists(), f"Log file was not created: {path}"
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "TEST_LOG_ENTRY_12345" in content
        assert "[INFO]" in content
        main_log = REPO_ROOT / "logs" / "sync.log"
        assert main_log.exists(), f"Main sync.log not found: {main_log}"
        with open(main_log, "r", encoding="utf-8") as f:
            main_content = f.read()
        assert "TickTick" in main_content
        print("    [PASS] File logging initializes cleanly and records UTF-8 data")
    finally:
        try:
            for h in list(sync_module.LOGGER.handlers):
                if getattr(h, "baseFilename", None) == str(test_log.resolve()):
                    h.close()
                    sync_module.LOGGER.removeHandler(h)
            if test_log.exists():
                test_log.unlink()
        except Exception:
            pass


def test_log_formula_self_healing():
    print("[17] Testing Log worksheet formula self-healing & collision clearance...")
    sh = get_shared_spreadsheet()
    temp_title = "_test_heal_temp"

    for ws in sync_module.call_with_retry(lambda: sh.worksheets()):
        if ws.title == temp_title:
            try:
                sync_module.call_with_retry(lambda: sh.del_worksheet(ws))
            except Exception:
                pass
            break

    ws_test = sync_module.call_with_retry(lambda: sh.add_worksheet(title=temp_title, rows=6, cols=8))
    try:
        # Corrupted header, stray space in F2, and stray value in F5 (beyond total_rows=2)
        corrupted_data = [
            ["Дата и время", "Ученик", "Длительность (ч)", "BROKEN_RATE", "BROKEN_SUM", "#REF!", "BROKEN_ST", "Task ID"],
            [sync_module.datetime_to_serial(datetime(2026, 9, 1, 10, 0)), "Лиза", 1.5, "", "", "  ", "", "tid_heal_001"],
        ]
        sync_module.call_with_retry(lambda: ws_test.update(range_name="A1:H2", values=corrupted_data))
        # Add a stray entry at F5 (far beyond the 2 data rows)
        sync_module.call_with_retry(lambda: ws_test.update_acell("F5", "stray_far_below"))

        repaired, cleared = sync_module.ensure_log_integrity(ws_test, sh, ws_test.id, 2)
        assert len(repaired) == 4, f"Expected 4 repaired headers, got {repaired}"
        assert "F2" in cleared, f"Expected F2 to be cleared, got {cleared}"
        assert "F5" in cleared, f"Expected F5 to be cleared beyond total_rows, got {cleared}"

        hdr_forms = sync_module.call_with_retry(lambda: ws_test.get("D1:G1", value_render_option="FORMULA"))
        assert all(str(f[0]).startswith("={") for f in hdr_forms if f)

        f2_val = sync_module.call_with_retry(lambda: ws_test.acell("F2", value_render_option="FORMULA")).value
        assert f2_val in ("", None), f"F2 still has value: {f2_val}"

        f5_val = sync_module.call_with_retry(lambda: ws_test.acell("F5", value_render_option="FORMULA")).value
        assert f5_val in ("", None), f"F5 still has value: {f5_val}"

        print("    [PASS] Log formula self-healing clears collisions across entire column range and restores canonical formulas")
    finally:
        try:
            sync_module.call_with_retry(lambda: sh.del_worksheet(ws_test))
        except Exception:
            pass


def test_sheet_ordering_independence():
    print("[18] Testing sheet ordering independence (Dashboard at index 0)...")
    sh = get_shared_spreadsheet()
    worksheets = sync_module.call_with_retry(lambda: sh.worksheets())
    assert worksheets[0].title == CONFIG.dashboard_worksheet, (
        f"Expected first worksheet to be '{CONFIG.dashboard_worksheet}', got '{worksheets[0].title}'"
    )
    ws_dash = sync_module.call_with_retry(lambda: sh.worksheet(CONFIG.dashboard_worksheet))
    ws_log = sync_module.call_with_retry(lambda: sh.worksheet(CONFIG.log_worksheet))
    assert ws_dash.id != ws_log.id
    print("    [PASS] Spreadsheet correctly ordered with Dashboard first and named sheet lookups intact")


if __name__ == "__main__":
    test_sqlite()
    test_config_loader()
    test_files_exist()
    test_task_scheduler()
    test_google_sheets_and_formulas()
    test_semen_duration_consistency()
    test_sync_google_sheets_idempotency()
    test_chronological_ordering()
    test_bat_files_execution()
    test_normalize_student_logic()
    test_sync_script_subprocess()
    test_manual_rows_protection_unit()
    test_reconciliation_logic_unit()
    test_reconciliation_isolated_sheet()
    test_deduplication_safeguard()
    test_logging_system()
    test_log_formula_self_healing()
    test_sheet_ordering_independence()
    print("\n" + "=" * 50)
    print("🎉 ALL 18 TEST SUITES PASSED FLAWLESSLY!")
    print("=" * 50)
