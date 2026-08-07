#!/usr/bin/env python3
"""Reclaim the disk space OpenCode accumulates in its `event` table.

OpenCode stores **a full copy of a message every time that message is updated**.
During streaming a single assistant reply is updated many times, and every one
of those events carries the whole accumulated text again. The result is a
database that is mostly duplicated snapshots of text you already have.

Numbers from one real installation (about two thousand sessions, heavy agent
use over a few weeks):

    total database                              39.37 GB
      event table                               35.42 GB   <- 90 %
        message.updated.1        228,444 rows   33.87 GB
        message.part.updated.1   659,569 rows    1.51 GB
        session.updated.1         63,095 rows    0.05 GB
      message + part (the real conversations)     3.1 GB

    average final message (message table)        35.6 KB
    average message.updated event               155.6 KB   <- 4.4x larger

After pruning, that database went from 39.37 GB to 3.15 GB — 36.23 GB reclaimed
— with integrity_check returning ok and all 2,106 sessions, 59,294 messages and
293,777 file parts still present.

## Why this is safe

`event` is a **change feed**, not the source of truth. Two independent pieces of
evidence:

1. The final content lives in the `message` and `part` tables. This script
   verifies that before touching anything: it opens the oldest session in your
   database and checks that its text is still readable outside `event`. If it
   is not, the script refuses to run.

2. OpenCode deletes this table itself. Its own schema migration
   `reset_v2_session_state` runs:

       DELETE FROM `session_context_epoch`;
       DELETE FROM `session_input`;
       DELETE FROM `session_message`;
       DELETE FROM `event`;
       DELETE FROM `event_sequence`;
       DELETE FROM `workspace`;

   Note what it does **not** touch: `session`, `message`, `part`. That is the
   upstream project's own statement about which tables hold your data.

The table's indexes — `(aggregate_id, seq)` and `(aggregate_id, type, seq)` —
exist to answer "give me the events for this session after sequence N", which is
how a reconnecting client catches up with a **live** session. For a finished
session it serves no purpose.

This script is deliberately more conservative than what OpenCode does to itself:
it deletes rows from `event` only, leaves `event_sequence` alone so sequence
numbering continues normally, and keeps the most recent sessions untouched.

## What it never deletes

Sessions, messages, or file parts. Only redundant change-feed events.

## Note on existing tools

Other cleaners run `VACUUM`, which reclaims *free pages*. That does not help
here: this space is live rows, not free pages. VACUUM is still run at the end,
after the rows are gone, to actually shrink the file on disk.

Usage:

    python3 opencode-db-prune.py                 # report only, changes nothing
    python3 opencode-db-prune.py --apply         # prune, with a backup first
    python3 opencode-db-prune.py --apply --no-backup --keep 10

Requires only the Python standard library.

Tested on macOS only. Linux support is written but unverified. Windows path
detection follows OpenCode's documented data directory and its in-use check uses
the native Windows file-sharing API. Field confirmation on Windows is welcome.
You can always bypass detection with --db.
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time

# Event types that are copies of state already stored in another table.
REDUNDANT = ("message.updated.1", "message.part.updated.1", "session.updated.1")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def find_database():
    """Locate OpenCode's database on macOS, Linux or Windows.

    OpenCode stores its data under ~/.local/share/opencode on Windows too.
    Every candidate is tried; if several exist, the largest one wins, because
    that is the one with the problem.
    """
    candidates = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "opencode", "opencode.db"))
    candidates.append(os.path.expanduser("~/.local/share/opencode/opencode.db"))
    if sys.platform == "darwin":
        candidates.append(
            os.path.expanduser("~/Library/Application Support/opencode/opencode.db"))

    existing = [c for c in dict.fromkeys(candidates) if os.path.exists(c)]
    return max(existing, key=os.path.getsize) if existing else None


def windows_file_in_use(path):
    """Check whether a Windows process already has the database open.

    Requesting an exclusive handle (share mode 0) conflicts with any existing
    handle that is actively reading or writing the file. That makes this check
    stricter than Python's normal open(), whose sharing flags are not suitable
    for detecting SQLite usage.
    """
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
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
            return True
        return None

    kernel32.CloseHandle(handle)
    return False


def database_in_use(path):
    """Is anyone holding *the file* open?

    The useful question is not "is a process named opencode running?". A leftover
    Electron helper or a crash handler will match that name while the database
    itself is closed, and blocking on it is a false positive that pushes people
    into bypassing the safeguard. A safeguard that gets bypassed routinely has
    stopped being one.

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
    """The definitive text must live in `part`, not only in `event`.

    This is the check that authorises deletion. If the oldest session no longer
    holds its text outside `event`, then `event` *is* the source of truth on
    this installation and must not be touched.
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


def measure(cx):
    per_type = {}
    for t in REDUNDANT:
        n, b = cx.execute(
            "select count(*), coalesce(sum(length(data)),0) from event where type=?",
            (t,)).fetchone()
        per_type[t] = (n, b)
    total = cx.execute(
        "select count(*), coalesce(sum(length(data)),0) from event").fetchone()
    return per_type, total


def main():
    ap = argparse.ArgumentParser(
        description="Prune OpenCode's redundant event log and shrink the database.")
    ap.add_argument("--apply", action="store_true",
                    help="actually prune (without this it only reports)")
    ap.add_argument("--keep", type=int, default=5, metavar="N",
                    help="leave the N most recent sessions untouched (default 5)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the backup copy (it needs as much space as the database)")
    ap.add_argument("--db", metavar="PATH", help="path to opencode.db, if autodetection fails")
    args = ap.parse_args()

    db = args.db or find_database()
    if not db or not os.path.exists(db):
        print("Could not find opencode.db. Tried $XDG_DATA_HOME/opencode,\n"
              "~/.local/share/opencode (including %USERPROFILE%\\.local\\share\\opencode on Windows),\n"
              "and ~/Library/Application Support/opencode on macOS.\n"
              "Pass the path explicitly with --db.")
        return 1

    size_before = os.path.getsize(db)
    print(f"database : {db}")
    print(f"platform : {sys.platform}")
    print(f"size     : {human(size_before)}")

    in_use = database_in_use(db)
    print(f"file open by another process: "
          f"{'yes' if in_use else ('no' if in_use is False else 'could not determine')}")
    if args.apply and in_use:
        print("\nRefusing to run: the database is open. Quit OpenCode first — "
              "modifying it while in use is exactly how people have corrupted theirs.")
        return 2
    if args.apply and in_use is None:
        print("Warning: could not verify. Make sure OpenCode is closed.")

    cx = sqlite3.connect(db)
    ok, detail = content_survives_outside_event(cx)
    print(f"pre-flight: {detail}")
    if not ok:
        print("\nRefusing to run: message content is not readable outside the event "
              "table on this installation.")
        return 3

    per_type, (total_rows, total_bytes) = measure(cx)

    # `event` has no timestamp column, so pruning is done per session. Pruning
    # "by age" would also miss the point: on a machine driving agents, nearly
    # every session is recent. Keeping the N newest is what actually maps to
    # "don't disturb anything I might still be looking at".
    recent = [r[0] for r in cx.execute(
        "select id from session order by time_created desc limit ?", (args.keep,))]
    placeholders = ",".join("?" * len(recent)) or "''"
    doomed_rows, doomed_bytes = cx.execute(
        f"select count(*), coalesce(sum(length(data)),0) from event "
        f"where type in ({','.join('?' * len(REDUNDANT))}) "
        f"and aggregate_id not in ({placeholders})",
        (*REDUNDANT, *recent)).fetchone()

    print(f"\nevent table: {total_rows:,} rows, {human(total_bytes)}")
    for t, (n, b) in sorted(per_type.items(), key=lambda x: -x[1][1]):
        print(f"  {t:26} {n:>9,}  {human(b):>10}")
    print(f"\nwould prune (all but the {args.keep} newest sessions): "
          f"{doomed_rows:,} rows, {human(doomed_bytes)}")
    print(f"estimated size afterwards: ~{human(size_before - doomed_bytes)}")

    if not args.apply:
        print("\nReport only. Re-run with --apply to prune.")
        return 0

    if not args.no_backup:
        backup = f"{db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        free = shutil.disk_usage(os.path.dirname(db)).free
        if free < size_before * 1.2:
            print(f"\nNot enough space for a backup ({human(size_before)} needed, "
                  f"{human(free)} free). Use --no-backup to proceed without one.")
            return 4
        print(f"\nbacking up to {os.path.basename(backup)} ...", flush=True)
        shutil.copy2(db, backup)
        print(f"backup written ({human(os.path.getsize(backup))})")

    print("integrity check ...", flush=True)
    if cx.execute("pragma integrity_check").fetchone()[0] != "ok":
        print("The database was already corrupt. Nothing was changed.")
        return 5

    print("deleting redundant events ...", flush=True)
    cx.execute(
        f"delete from event where type in ({','.join('?' * len(REDUNDANT))}) "
        f"and aggregate_id not in ({placeholders})", (*REDUNDANT, *recent))
    cx.commit()

    print("VACUUM (this can take several minutes on a large database) ...", flush=True)
    cx.execute("pragma auto_vacuum = INCREMENTAL")   # keeps future growth reclaimable
    cx.execute("vacuum")
    status = cx.execute("pragma integrity_check").fetchone()[0]

    sessions = cx.execute("select count(*) from session").fetchone()[0]
    messages = cx.execute("select count(*) from message").fetchone()[0]
    parts = cx.execute("select count(*) from part").fetchone()[0]
    cx.close()

    size_after = os.path.getsize(db)
    print(f"\nintegrity  : {status}")
    print(f"still there: {sessions:,} sessions, {messages:,} messages, {parts:,} parts")
    print(f"size       : {human(size_before)} -> {human(size_after)}  "
          f"(reclaimed {human(size_before - size_after)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
