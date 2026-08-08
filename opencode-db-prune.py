#!/usr/bin/env python3
"""opencode-db-prune — Reclaim storage from OpenCode's redundant event log.

OpenCode stores a full copy of every message on each streaming update. A single
reply may generate dozens of intermediate snapshots, each carrying the full
accumulated text. The result: databases that are 90%+ redundant data.

This tool safely removes those redundant snapshots without deleting any session,
message, or file part.

Features:
  - Incremental compaction in configurable batches (safe for 50+ GB databases)
  - Smart deduplication: detects byte-identical consecutive snapshots
  - Per-session targeting (--session <id>)
  - Respects sync/workspace ownership (won't touch synced aggregates)
  - Tool-output directory cleanup (--tool-output)
  - Fast stats mode (--stats) for quick diagnostics
  - Real-time progress reporting
  - Cron-friendly scheduling (--schedule)
  - Post-prune verification with random session sampling

Usage:
    python3 opencode-db-prune.py                      # report only
    python3 opencode-db-prune.py --stats              # quick stats, no heavy queries
    python3 opencode-db-prune.py --apply              # prune with backup
    python3 opencode-db-prune.py --apply --batch 5000 # custom batch size
    python3 opencode-db-prune.py --apply --session <id>
    python3 opencode-db-prune.py --apply --tool-output
    python3 opencode-db-prune.py --apply --tool-output --max-age 30
    python3 opencode-db-prune.py --schedule 6h        # run every 6 hours

Requires only the Python standard library. Python 3.8+.
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "2.0.0"

# Event types that are redundant copies of state stored in message/part tables.
REDUNDANT = ("message.updated.1", "message.part.updated.1", "session.updated.1")

# Default batch size for incremental deletion.
DEFAULT_BATCH = 10000

# Tables that indicate sync/workspace ownership — aggregates in these should
# not be compacted without explicit override.
SYNC_TABLES = ("workspace",)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def human(n):
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def progress_bar(current, total, width=40, prefix=""):
    """Print a progress bar that updates in place."""
    if total == 0:
        return
    pct = current / total
    filled = int(width * pct)
    bar = "=" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r{prefix}[{bar}] {pct:.1%} ({human(current)} / {human(total)})")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")


def timestamp_now():
    """ISO timestamp for logging."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------

def find_database():
    """Locate OpenCode's database on macOS, Linux or Windows.

    Tries all known paths. If several exist, picks the largest (that's the one
    with the problem). $OPENCODE_DB wins over everything.
    """
    override = os.environ.get("OPENCODE_DB")
    if override and os.path.exists(override):
        return override

    candidates = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "opencode", "opencode.db"))
    candidates.append(os.path.expanduser("~/.local/share/opencode/opencode.db"))
    if sys.platform == "darwin":
        candidates.append(
            os.path.expanduser("~/Library/Application Support/opencode/opencode.db"))
    if os.name == "nt":
        home = os.environ.get("USERPROFILE", "")
        candidates.append(os.path.join(home, ".local", "share", "opencode", "opencode.db"))
        for var in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "opencode", "data", "opencode.db"))
                candidates.append(os.path.join(base, "opencode", "opencode.db"))

    existing = [c for c in dict.fromkeys(candidates) if os.path.exists(c)]
    return max(existing, key=os.path.getsize) if existing else None


def find_tool_output_dir(db_path):
    """Locate the tool-output directory relative to the database."""
    db_dir = os.path.dirname(db_path)
    tool_dir = os.path.join(db_dir, "tool-output")
    if os.path.isdir(tool_dir):
        return tool_dir
    return None


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def windows_file_in_use(path):
    """Check whether a Windows process has the database open (exclusive handle)."""
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    ERROR_SHARING_VIOLATION = 32
    ERROR_LOCK_VIOLATION = 33
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        path, GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
            return True
        return None
    kernel32.CloseHandle(handle)
    return False


def database_in_use(path):
    """Is the database file held open by another process?

    Returns True, False, or None when it cannot be determined.
    """
    try:
        if os.name == "nt":
            return windows_file_in_use(path)
        result = subprocess.run(["lsof", "--", path],
                                capture_output=True, text=True)
        return bool(result.stdout.strip())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def content_survives_outside_event(cx):
    """Verify the final message content lives in `part`, not only in `event`.

    This is the authorization check. If content is only in `event`, the tool
    refuses to run.
    """
    row = cx.execute(
        "select id from session order by time_created asc limit 1").fetchone()
    if not row:
        return True, "database has no sessions"
    session = row[0]
    parts = cx.execute(
        "select count(*) from part where message_id in "
        "(select id from message where session_id=?)", (session,)).fetchone()[0]
    text = cx.execute(
        "select json_extract(data,'$.text') from part where message_id in "
        "(select id from message where session_id=?) "
        "and json_extract(data,'$.type')='text' "
        "and length(json_extract(data,'$.text')) > 20 limit 1",
        (session,)).fetchone()
    ok = parts > 0 and text and text[0]
    return bool(ok), f"oldest session: {parts} parts, text {'present' if ok else 'MISSING'}"


# ---------------------------------------------------------------------------
# Sync/workspace ownership detection
# ---------------------------------------------------------------------------

def get_synced_aggregates(cx):
    """Return set of aggregate_ids that are owned by a workspace/sync.

    These should not be compacted unless explicitly overridden, because the
    sync protocol has no checkpoint-marker version negotiation.
    """
    synced = set()
    for table in SYNC_TABLES:
        try:
            rows = cx.execute(f"select distinct id from {table}").fetchall()
            synced.update(r[0] for r in rows)
        except sqlite3.OperationalError:
            pass  # table doesn't exist in this version
    # Also check event_sequence for aggregates with active consumers
    try:
        rows = cx.execute(
            "select distinct aggregate_id from event_sequence "
            "where consumer_id is not null and consumer_id != ''").fetchall()
        synced.update(r[0] for r in rows)
    except sqlite3.OperationalError:
        pass
    return synced


# ---------------------------------------------------------------------------
# Measurement and stats
# ---------------------------------------------------------------------------

def measure(cx):
    """Measure event table breakdown by type."""
    per_type = {}
    for t in REDUNDANT:
        n, b = cx.execute(
            "select count(*), coalesce(sum(length(data)),0) from event where type=?",
            (t,)).fetchone()
        per_type[t] = (n, b)
    total = cx.execute(
        "select count(*), coalesce(sum(length(data)),0) from event").fetchone()
    return per_type, total


def quick_stats(db_path):
    """Fast stats mode — minimal queries, no full table scans."""
    size = os.path.getsize(db_path)
    cx = sqlite3.connect(db_path)
    cx.execute("pragma query_only = ON")

    sessions = cx.execute("select count(*) from session").fetchone()[0]
    messages = cx.execute("select count(*) from message").fetchone()[0]
    parts = cx.execute("select count(*) from part").fetchone()[0]

    # Approximate event count using sqlite_stat1 if available, else count
    try:
        event_rows = cx.execute(
            "select stat from sqlite_stat1 where tbl='event' limit 1").fetchone()
        if event_rows:
            event_count = int(event_rows[0].split()[0])
        else:
            event_count = cx.execute("select count(*) from event").fetchone()[0]
    except sqlite3.OperationalError:
        event_count = cx.execute("select count(*) from event").fetchone()[0]

    # Page-based size estimate for event table
    page_size = cx.execute("pragma page_size").fetchone()[0]
    page_count = cx.execute("pragma page_count").fetchone()[0]
    freelist = cx.execute("pragma freelist_count").fetchone()[0]
    auto_vacuum = cx.execute("pragma auto_vacuum").fetchone()[0]
    journal = cx.execute("pragma journal_mode").fetchone()[0]

    # Tool-output size
    tool_dir = find_tool_output_dir(db_path)
    tool_size = 0
    tool_files = 0
    if tool_dir:
        for entry in os.scandir(tool_dir):
            if entry.is_file():
                tool_size += entry.stat().st_size
                tool_files += 1

    # WAL size
    wal_path = db_path + "-wal"
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

    cx.close()

    print(f"\n{'='*60}")
    print(f" opencode-db-prune v{__version__} — Quick Stats")
    print(f"{'='*60}")
    print(f"\n  database    : {db_path}")
    print(f"  platform    : {sys.platform}")
    print(f"  db size     : {human(size)}")
    print(f"  wal size    : {human(wal_size)}")
    print(f"  total disk  : {human(size + wal_size)}")
    print(f"\n  sessions    : {sessions:,}")
    print(f"  messages    : {messages:,}")
    print(f"  parts       : {parts:,}")
    print(f"  events      : {event_count:,}")
    print(f"\n  page_size   : {page_size}")
    print(f"  page_count  : {page_count:,}")
    print(f"  freelist    : {freelist:,} pages ({human(freelist * page_size)})")
    print(f"  auto_vacuum : {['none','full','incremental'][auto_vacuum]}")
    print(f"  journal     : {journal}")
    if tool_dir:
        print(f"\n  tool-output : {tool_dir}")
        print(f"  tool files  : {tool_files:,}")
        print(f"  tool size   : {human(tool_size)}")
    print(f"\n  estimated event overhead: ~{human(max(0, size - (size * 0.1)))}")
    print(f"\n  Run without --stats for full breakdown.")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Deduplication analysis
# ---------------------------------------------------------------------------

def count_duplicates(cx, session_filter=None):
    """Count byte-identical consecutive event snapshots.

    These are events where the data payload is identical to the previous event
    for the same aggregate+type — pure waste (e.g., wget progress bar redraws).
    Returns (duplicate_count, duplicate_bytes).
    """
    where_clause = ""
    params = list(REDUNDANT)
    if session_filter:
        where_clause = " and aggregate_id = ?"
        params.append(session_filter)

    # Use a window function approach for efficiency
    query = f"""
        select count(*), coalesce(sum(length(data)), 0)
        from (
            select data,
                   lag(data) over (partition by aggregate_id, type order by seq) as prev_data
            from event
            where type in ({','.join('?' * len(REDUNDANT))})
            {where_clause}
        )
        where data = prev_data
    """
    try:
        row = cx.execute(query, params).fetchone()
        return row[0], row[1]
    except sqlite3.OperationalError:
        # Older SQLite without window functions — skip dedup analysis
        return 0, 0


# ---------------------------------------------------------------------------
# Tool-output cleanup
# ---------------------------------------------------------------------------

def measure_tool_output(tool_dir, max_age_days=None):
    """Measure tool-output directory, optionally filtering by age."""
    total_size = 0
    total_files = 0
    prunable_size = 0
    prunable_files = 0
    cutoff = None
    if max_age_days is not None:
        cutoff = time.time() - (max_age_days * 86400)

    for entry in os.scandir(tool_dir):
        if entry.is_file():
            stat = entry.stat()
            total_size += stat.st_size
            total_files += 1
            if cutoff and stat.st_mtime < cutoff:
                prunable_size += stat.st_size
                prunable_files += 1

    if cutoff is None:
        prunable_size = total_size
        prunable_files = total_files

    return total_files, total_size, prunable_files, prunable_size


def prune_tool_output(tool_dir, max_age_days=None, dry_run=False):
    """Delete old tool-output files."""
    cutoff = None
    if max_age_days is not None:
        cutoff = time.time() - (max_age_days * 86400)

    deleted = 0
    freed = 0
    entries = list(os.scandir(tool_dir))
    total = len(entries)

    for i, entry in enumerate(entries):
        if entry.is_file():
            stat = entry.stat()
            should_delete = (cutoff is None) or (stat.st_mtime < cutoff)
            if should_delete:
                if not dry_run:
                    os.unlink(entry.path)
                deleted += 1
                freed += stat.st_size
        if (i + 1) % 100 == 0:
            progress_bar(i + 1, total, prefix="  tool-output: ")

    if total > 100:
        progress_bar(total, total, prefix="  tool-output: ")

    return deleted, freed


# ---------------------------------------------------------------------------
# Incremental compaction
# ---------------------------------------------------------------------------

def prune_events_batched(cx, keep_sessions, synced_ids, batch_size=DEFAULT_BATCH,
                         session_filter=None, force_synced=False):
    """Delete redundant events in batches with progress reporting.

    Returns (total_deleted, total_bytes_freed).
    """
    # Build exclusion set
    excluded = set(keep_sessions)
    if not force_synced:
        excluded.update(synced_ids)

    excluded_list = list(excluded)
    placeholders_excl = ",".join("?" * len(excluded_list)) if excluded_list else "''"

    # Session filter
    session_clause = ""
    session_params = []
    if session_filter:
        session_clause = " and aggregate_id = ?"
        session_params = [session_filter]

    # Count what we'll delete
    count_query = (
        f"select count(*) from event "
        f"where type in ({','.join('?' * len(REDUNDANT))}) "
        f"and aggregate_id not in ({placeholders_excl})"
        f"{session_clause}"
    )
    total_doomed = cx.execute(
        count_query, (*REDUNDANT, *excluded_list, *session_params)).fetchone()[0]

    if total_doomed == 0:
        print("  nothing to prune.")
        return 0, 0

    print(f"  pruning {total_doomed:,} rows in batches of {batch_size:,}...")

    total_deleted = 0
    total_freed = 0
    batch_num = 0

    while True:
        # Get batch of rowids to delete
        select_query = (
            f"select rowid, length(data) from event "
            f"where type in ({','.join('?' * len(REDUNDANT))}) "
            f"and aggregate_id not in ({placeholders_excl})"
            f"{session_clause} "
            f"limit ?"
        )
        rows = cx.execute(
            select_query, (*REDUNDANT, *excluded_list, *session_params, batch_size)
        ).fetchall()

        if not rows:
            break

        rowids = [r[0] for r in rows]
        batch_bytes = sum(r[1] for r in rows)
        rid_placeholders = ",".join("?" * len(rowids))

        cx.execute(f"delete from event where rowid in ({rid_placeholders})", rowids)
        cx.commit()

        total_deleted += len(rowids)
        total_freed += batch_bytes
        batch_num += 1

        progress_bar(total_deleted, total_doomed,
                     prefix=f"  batch {batch_num}: ")

    print(f"  completed: {total_deleted:,} rows deleted, {human(total_freed)} freed")
    return total_deleted, total_freed


# ---------------------------------------------------------------------------
# Post-prune verification
# ---------------------------------------------------------------------------

def verify_sessions(cx, sample_size=5):
    """Verify random sessions still have readable content after pruning.

    Returns (passed, failed, details).
    """
    all_sessions = cx.execute(
        "select id from session order by time_created desc").fetchall()
    if not all_sessions:
        return 0, 0, "no sessions to verify"

    sample = random.sample(all_sessions, min(sample_size, len(all_sessions)))
    passed = 0
    failed = 0
    details = []

    for (session_id,) in sample:
        msg_count = cx.execute(
            "select count(*) from message where session_id=?",
            (session_id,)).fetchone()[0]
        part_count = cx.execute(
            "select count(*) from part where message_id in "
            "(select id from message where session_id=?)",
            (session_id,)).fetchone()[0]
        # Check at least one text part is readable
        text = cx.execute(
            "select json_extract(data,'$.text') from part where message_id in "
            "(select id from message where session_id=?) "
            "and json_extract(data,'$.type')='text' "
            "and length(json_extract(data,'$.text')) > 0 limit 1",
            (session_id,)).fetchone()

        if msg_count > 0 and part_count > 0 and text:
            passed += 1
            details.append(f"  {session_id[:8]}... {msg_count} msgs, {part_count} parts: OK")
        elif msg_count == 0:
            # Empty session — not a failure
            passed += 1
            details.append(f"  {session_id[:8]}... empty session: OK")
        else:
            failed += 1
            details.append(
                f"  {session_id[:8]}... {msg_count} msgs, {part_count} parts, "
                f"text={'yes' if text else 'NO'}: FAILED")

    return passed, failed, "\n".join(details)


# ---------------------------------------------------------------------------
# Schedule / daemon mode
# ---------------------------------------------------------------------------

def parse_interval(s):
    """Parse interval string like '6h', '30m', '1d' into seconds."""
    s = s.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in multipliers:
        return int(s[:-1]) * multipliers[s[-1]]
    return int(s)  # assume seconds


def run_scheduled(args, interval_str):
    """Run prune in a loop at the given interval."""
    interval = parse_interval(interval_str)
    print(f"[{timestamp_now()}] Schedule mode: running every {interval_str} "
          f"({interval}s). Ctrl+C to stop.\n")

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        print(f"\n[{timestamp_now()}] Received signal, stopping scheduler.")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        print(f"\n[{timestamp_now()}] Starting prune cycle...")
        try:
            run_prune(args)
        except Exception as e:
            print(f"[{timestamp_now()}] Error during prune: {e}")
        print(f"[{timestamp_now()}] Next run in {interval_str}.")

        # Sleep in small chunks so we can respond to signals
        end_time = time.time() + interval
        while running and time.time() < end_time:
            time.sleep(min(10, end_time - time.time()))

    print(f"[{timestamp_now()}] Scheduler stopped.")
    return 0


# ---------------------------------------------------------------------------
# Main prune logic
# ---------------------------------------------------------------------------

def run_prune(args):
    """Execute one prune cycle. Returns exit code."""
    db = args.db or find_database()
    if not db or not os.path.exists(db):
        print("Could not find opencode.db. Tried $OPENCODE_DB, $XDG_DATA_HOME/opencode,\n"
              "~/.local/share/opencode (%USERPROFILE%\\.local\\share\\opencode on Windows),\n"
              "~/Library/Application Support/opencode on macOS, and on Windows also\n"
              "%LOCALAPPDATA%/opencode/data and %APPDATA%/opencode.\n"
              "Pass the path explicitly with --db.")
        return 1

    # Quick stats mode
    if args.stats:
        quick_stats(db)
        return 0

    size_before = os.path.getsize(db)
    print(f"\n{'='*60}")
    print(f" opencode-db-prune v{__version__}")
    print(f"{'='*60}")
    print(f"\n  database : {db}")
    print(f"  platform : {sys.platform}")
    print(f"  size     : {human(size_before)}")

    # File-in-use check
    in_use = database_in_use(db)
    status_str = 'yes' if in_use else ('no' if in_use is False else 'unknown')
    print(f"  in use   : {status_str}")
    if args.apply and in_use:
        print("\n  REFUSED: database is open. Quit OpenCode first.")
        return 2
    if args.apply and in_use is None:
        print("  WARNING: could not verify file lock. Make sure OpenCode is closed.")

    # Connect
    cx = sqlite3.connect(db)

    # Pre-flight content check
    ok, detail = content_survives_outside_event(cx)
    print(f"  pre-flight: {detail}")
    if not ok:
        print("\n  REFUSED: message content not readable outside event table.")
        cx.close()
        return 3

    # Detect synced aggregates
    synced = get_synced_aggregates(cx)
    if synced:
        print(f"  synced aggregates: {len(synced)} (will be skipped)")

    # Measure
    per_type, (total_rows, total_bytes) = measure(cx)

    # Deduplication analysis
    dup_count, dup_bytes = count_duplicates(cx, args.session)
    dedup_pct = (dup_count / total_rows * 100) if total_rows > 0 else 0

    # Determine what to prune
    keep_ids = []
    if not args.session:
        keep_ids = [r[0] for r in cx.execute(
            "select id from session order by time_created desc limit ?",
            (args.keep,))]

    excluded = set(keep_ids)
    if not args.force_synced:
        excluded.update(synced)
    excluded_list = list(excluded)
    placeholders = ",".join("?" * len(excluded_list)) if excluded_list else "''"

    session_clause = ""
    session_params = []
    if args.session:
        session_clause = " and aggregate_id = ?"
        session_params = [args.session]

    doomed_rows, doomed_bytes = cx.execute(
        f"select count(*), coalesce(sum(length(data)),0) from event "
        f"where type in ({','.join('?' * len(REDUNDANT))}) "
        f"and aggregate_id not in ({placeholders})"
        f"{session_clause}",
        (*REDUNDANT, *excluded_list, *session_params)).fetchone()

    # Report
    print(f"\n  {'─'*56}")
    print(f"  Event table: {total_rows:,} rows, {human(total_bytes)}")
    print(f"  {'─'*56}")
    for t, (n, b) in sorted(per_type.items(), key=lambda x: -x[1][1]):
        pct = (b / total_bytes * 100) if total_bytes > 0 else 0
        print(f"    {t:30} {n:>9,}  {human(b):>10}  ({pct:.1f}%)")
    print(f"  {'─'*56}")
    if dup_count > 0:
        print(f"  Byte-identical duplicates: {dup_count:,} rows, "
              f"{human(dup_bytes)} ({dedup_pct:.1f}% of events)")
    print(f"\n  Would prune: {doomed_rows:,} rows, {human(doomed_bytes)}")
    if args.session:
        print(f"  Target session: {args.session}")
    else:
        print(f"  Keeping {args.keep} newest sessions untouched")
    if synced and not args.force_synced:
        print(f"  Skipping {len(synced)} synced/owned aggregates")
    print(f"  Estimated size after: ~{human(max(0, size_before - doomed_bytes))}")

    # Tool-output report
    tool_dir = find_tool_output_dir(db)
    tool_prunable_size = 0
    if tool_dir and args.tool_output:
        t_total, t_size, t_prunable, t_psize = measure_tool_output(
            tool_dir, args.max_age)
        tool_prunable_size = t_psize
        age_str = f" older than {args.max_age} days" if args.max_age else ""
        print(f"\n  Tool-output: {t_total:,} files, {human(t_size)}")
        print(f"  Would delete{age_str}: {t_prunable:,} files, {human(t_psize)}")
    elif tool_dir:
        t_total, t_size, _, _ = measure_tool_output(tool_dir)
        print(f"\n  Tool-output: {t_total:,} files, {human(t_size)} "
              f"(use --tool-output to clean)")

    # Total savings
    total_savings = doomed_bytes + tool_prunable_size
    print(f"\n  Total potential savings: {human(total_savings)}")

    if not args.apply:
        print(f"\n  Report only. Re-run with --apply to prune.")
        cx.close()
        return 0

    # --- APPLY MODE ---
    print(f"\n{'─'*60}")
    print(f"  APPLYING CHANGES")
    print(f"{'─'*60}")

    # Backup
    if not args.no_backup:
        backup = f"{db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        free = shutil.disk_usage(os.path.dirname(db)).free
        if free < size_before * 1.2:
            print(f"\n  Not enough space for backup ({human(size_before)} needed, "
                  f"{human(free)} free). Use --no-backup to skip.")
            cx.close()
            return 4
        print(f"\n  backing up to {os.path.basename(backup)} ...", flush=True)
        shutil.copy2(db, backup)
        print(f"  backup: {human(os.path.getsize(backup))}")

    # Integrity check before
    print("  integrity check (pre) ...", flush=True)
    if cx.execute("pragma integrity_check").fetchone()[0] != "ok":
        print("  REFUSED: database already corrupt. Nothing changed.")
        cx.close()
        return 5

    # Incremental deletion in batches
    print(f"\n  Deleting events (batch size: {args.batch:,})...")
    deleted, freed = prune_events_batched(
        cx, keep_ids, synced, batch_size=args.batch,
        session_filter=args.session, force_synced=args.force_synced)

    # Tool-output cleanup
    if tool_dir and args.tool_output:
        print(f"\n  Cleaning tool-output...")
        t_deleted, t_freed = prune_tool_output(tool_dir, args.max_age)
        print(f"  tool-output: {t_deleted:,} files deleted, {human(t_freed)} freed")

    # VACUUM
    print("\n  VACUUM (may take several minutes on large databases)...", flush=True)
    cx.execute("pragma auto_vacuum = INCREMENTAL")
    cx.execute("vacuum")

    # Post-prune integrity
    print("  integrity check (post) ...", flush=True)
    status = cx.execute("pragma integrity_check").fetchone()[0]

    # Verification — sample random sessions
    print(f"\n  Verifying {min(10, args.keep + 5)} random sessions...", flush=True)
    passed, failed, verify_details = verify_sessions(cx, sample_size=10)

    # Final stats
    sessions = cx.execute("select count(*) from session").fetchone()[0]
    messages = cx.execute("select count(*) from message").fetchone()[0]
    parts = cx.execute("select count(*) from part").fetchone()[0]
    cx.close()

    size_after = os.path.getsize(db)

    print(f"\n{'='*60}")
    print(f"  RESULTS")
    print(f"{'='*60}")
    print(f"  integrity    : {status}")
    print(f"  verification : {passed} passed, {failed} failed")
    if verify_details:
        print(verify_details)
    print(f"\n  still there  : {sessions:,} sessions, {messages:,} messages, {parts:,} parts")
    print(f"  size         : {human(size_before)} -> {human(size_after)}")
    print(f"  reclaimed    : {human(size_before - size_after)}")
    print(f"{'='*60}\n")

    if failed > 0:
        print("  WARNING: Some sessions failed verification. Check the backup.")
        return 6

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="opencode-db-prune",
        description="Prune OpenCode's redundant event log and reclaim storage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s                          report only, changes nothing
  %(prog)s --stats                  quick diagnostics
  %(prog)s --apply                  prune with backup
  %(prog)s --apply --batch 5000     custom batch size
  %(prog)s --apply --session <id>   prune one session only
  %(prog)s --apply --tool-output    also clean tool-output/
  %(prog)s --apply --tool-output --max-age 30
  %(prog)s --schedule 6h            run every 6 hours
""")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--apply", action="store_true",
                    help="actually prune (without this it only reports)")
    ap.add_argument("--stats", action="store_true",
                    help="quick stats mode — minimal queries, fast diagnostics")
    ap.add_argument("--keep", type=int, default=5, metavar="N",
                    help="leave N most recent sessions untouched (default: 5)")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH, metavar="N",
                    help=f"rows per deletion batch (default: {DEFAULT_BATCH})")
    ap.add_argument("--session", metavar="ID",
                    help="target a specific session by ID")
    ap.add_argument("--tool-output", action="store_true",
                    help="also clean the tool-output directory")
    ap.add_argument("--max-age", type=int, metavar="DAYS",
                    help="only delete tool-output files older than N days")
    ap.add_argument("--force-synced", action="store_true",
                    help="also prune synced/owned aggregates (risky)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip backup (it needs as much free space as the database)")
    ap.add_argument("--schedule", metavar="INTERVAL",
                    help="run repeatedly at interval (e.g., 6h, 30m, 1d)")
    ap.add_argument("--db", metavar="PATH",
                    help="explicit path to opencode.db")
    args = ap.parse_args()

    # Schedule mode
    if args.schedule:
        args.apply = True  # schedule implies apply
        return run_scheduled(args, args.schedule)

    return run_prune(args)


if __name__ == "__main__":
    sys.exit(main())
