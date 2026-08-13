# opencode-db-prune

*[Versión en español](LEEME.md)*

> **Tested on macOS.** Linux written, Windows implemented. See [Platforms](#platforms).

Your `opencode.db` is probably enormous, and probably 90% of it is one table
you don't need.

This is a single-file Python script, standard library only, that reclaims it —
without deleting a single session, message or file part.

```
============================================================
 opencode-db-prune v2.0.0
============================================================

  database : /home/you/.local/share/opencode/opencode.db
  size     : 39.37 GB

  ────────────────────────────────────────────────────────
  Event table: 953,214 rows, 35.42 GB
  ────────────────────────────────────────────────────────
    message.updated.1                228,444    33.87 GB  (95.6%)
    message.part.updated.1           659,569     1.51 GB  (4.3%)
    session.updated.1                 63,095     0.05 GB  (0.1%)
  ────────────────────────────────────────────────────────
  Byte-identical duplicates: 412,003 rows, 12.80 GB (43.2% of events)

  Would prune: 949,404 rows, 35.42 GB
  Estimated size after: ~3.96 GB
```

## What's new in v2.0

| Feature | Description |
|---------|-------------|
| **Incremental batched deletion** | Safe for 50+ GB databases — no journal overflow risk |
| **Smart deduplication analysis** | Detects byte-identical consecutive snapshots (e.g., `wget --show-progress` generating 39,000 copies of the same 31 KB) |
| **Per-session targeting** | `--session <id>` to prune a single problematic session |
| **Sync/workspace awareness** | Won't touch aggregates owned by sync consumers |
| **Tool-output cleanup** | `--tool-output` reclaims the other big consumer (up to 45 GB) |
| **Quick stats** | `--stats` for instant diagnostics without full table scans |
| **Real-time progress** | Progress bars during large deletions |
| **Schedule mode** | `--schedule 6h` for unattended periodic maintenance |
| **Post-prune verification** | Random session sampling confirms content integrity |
| **Version flag** | `--version` |

## What is actually going on

OpenCode stores **a complete copy of a message every time that message is
updated**. While a reply is streaming in, it gets updated many times, and each
of those events carries the whole accumulated text again — not a delta.

Measured on one real installation (~2,100 sessions, weeks of heavy agent use):

| | |
|---|---|
| Average final message (`message` table) | 35.6 KB |
| Average `message.updated` event | **155.6 KB** — 4.4× larger |
| Real messages | 59,176 |
| Update events for them | **228,444**, totalling 33.87 GB |

So the conversations themselves were about 3 GB. The other 35 GB were snapshots
of text already stored elsewhere.

After running this script that database went from **39.37 GB to 3.15 GB**
(36.23 GB reclaimed), `PRAGMA integrity_check` returned `ok`, and all 2,106
sessions, 59,294 messages and 293,777 file parts were still there — verified by
re-reading a session recorded before the prune and comparing it byte for byte.

### The byte-identical duplicate problem

One `wget --show-progress` command can generate **79,083 events** totalling
**2.3 GB** — because the progress bar redraws in place and OpenCode snapshots
the entire output on every redraw. From update ~1,000 to ~40,000, the content
was byte-for-byte identical (31 KB repeated 39,000 times = 1.2 GB of pure
waste). This tool now detects and reports these.

## Why deleting it is safe

**1. The content lives somewhere else.** The final state of every message is in
the `message` and `part` tables. The script verifies this before touching
anything: it opens the oldest session in your database and checks its text is
still readable outside `event`. If it isn't, it refuses to run.

**2. OpenCode wipes this table itself.** Its own schema migration
`reset_v2_session_state` executes:

```sql
DELETE FROM `session_context_epoch`;
DELETE FROM `session_input`;
DELETE FROM `session_message`;
DELETE FROM `event`;
DELETE FROM `event_sequence`;
DELETE FROM `workspace`;
```

Note which tables it leaves alone: `session`, `message`, `part`. That is the
project's own statement about where your data lives.

**3. The indexes tell you what the table is for.** They are
`(aggregate_id, seq)` and `(aggregate_id, type, seq)` — built to answer *"give
me the events for this session after sequence N"*. That's how a client catches
up with a session that is **still streaming**. For a finished session it has no
reader.

This script is deliberately more conservative than what OpenCode does to
itself: it deletes rows from `event` only, leaves `event_sequence` intact so
sequence numbering continues normally, and keeps the newest sessions untouched.

## Usage

### Quick diagnostics

```bash
python3 opencode-db-prune.py --stats
```

Instant overview: DB size, session/message/part counts, page stats, tool-output
size. No heavy queries.

### Report (default, changes nothing)

```bash
python3 opencode-db-prune.py
```

Full breakdown of the event table, deduplication analysis, and what would be
pruned.

### Prune

```bash
python3 opencode-db-prune.py --apply                    # prune with backup
python3 opencode-db-prune.py --apply --batch 5000       # smaller batches
python3 opencode-db-prune.py --apply --keep 20          # keep 20 sessions
python3 opencode-db-prune.py --apply --no-backup        # skip backup
```

### Target a specific session

```bash
python3 opencode-db-prune.py --apply --session abc123-def456
```

### Clean tool-output

```bash
python3 opencode-db-prune.py --apply --tool-output              # delete all
python3 opencode-db-prune.py --apply --tool-output --max-age 30 # older than 30 days
```

### Scheduled / daemon mode

```bash
python3 opencode-db-prune.py --schedule 6h              # every 6 hours
python3 opencode-db-prune.py --schedule 1d --keep 10    # daily, keep 10 sessions
```

### Windows

```powershell
py opencode-db-prune.py --stats
py opencode-db-prune.py --apply
```

**Quit OpenCode first.** The script checks whether the file is open and refuses
to run if it is.

No dependencies. Python 3.8+.

## Safeguards

- Refuses to run while the database file is held open by another process.
- Refuses to run if message content is not readable outside `event`.
- Refuses to run if the database was already corrupt.
- **Respects sync/workspace ownership** — won't touch synced aggregates unless
  `--force-synced` is passed.
- Backs up first unless you pass `--no-backup`.
- **Incremental deletion in batches** — safe for databases of any size.
- Runs `PRAGMA integrity_check` before and after.
- **Post-prune verification**: reads random sessions to confirm content survives.
- Reports sessions, messages and parts that survived.
- Enables `auto_vacuum = INCREMENTAL` so future growth stays reclaimable.

## Platforms

**Tested on macOS only.** That is where the problem was found, measured, and
where the result was verified.

Linux support is written but untested. Windows now follows OpenCode's documented
data directory and uses the native Windows file-sharing API for the in-use
safeguard, but it still needs field confirmation on a real Windows installation:

| Platform | Status | Paths |
|---|---|---|
| macOS | **tested** | `~/.local/share/opencode/opencode.db`, `~/Library/Application Support/opencode/opencode.db` |
| Linux | written, untested | `$XDG_DATA_HOME/opencode/opencode.db`, `~/.local/share/opencode/opencode.db` |
| Windows | implemented, needs confirmation | `%USERPROFILE%\.local\share\opencode\opencode.db` (CLI), `%LOCALAPPDATA%\opencode\data\opencode.db` (desktop app) |

If several databases exist, the largest is picked. `OPENCODE_DB` is honoured
and wins over everything else. You can also bypass detection with `--db`.

### Contributions for Linux and Windows are welcome

If you hit the same problem on another platform:

- Confirm where OpenCode actually keeps the database on your system.
- Check the "file in use" detection.
- Run `--stats` and share the output.

## How this differs from a VACUUM

Other cleanup tools run `VACUUM`, which reclaims **free pages** — space that was
freed inside the file but never returned to the filesystem.

It does not solve *this* problem. Here the space is **live rows**. VACUUM alone
reclaims nothing until the rows are gone. This script deletes the redundant rows
first and then runs VACUUM to shrink the file.

## How this differs from PR #36710

PR [#36710](https://github.com/anomalyco/opencode/pull/36710) proposes
integrated compaction inside OpenCode itself (TypeScript, part of the core).
This tool is complementary:

| | opencode-db-prune | PR #36710 |
|---|---|---|
| Language | Python (standalone) | TypeScript (integrated) |
| Available now | Yes | Pending merge |
| Batch size | Configurable | Fixed 10,000 |
| Tool-output cleanup | Yes | No |
| Schedule mode | Yes | No |
| Dedup analysis | Yes | No |
| Sync awareness | Yes | Yes |
| Post-prune verification | Yes (random sampling) | Yes (projection check) |

## Related upstream issues

- [#33356 — Unbounded event table growth](https://github.com/anomalyco/opencode/issues/33356)
- [#38362 — OOM crash from event accumulation](https://github.com/anomalyco/opencode/issues/38362)
- [#31391 — Why is opencode.db so large?](https://github.com/anomalyco/opencode/issues/31391)
- [#29694 — Tool-output storage growth](https://github.com/anomalyco/opencode/issues/29694)

The real fix belongs upstream: don't persist a full snapshot per streaming
update, or prune the change feed when a session completes. Until then, this.

---

## Español

Hay una versión completa en español: **[LEEME.md](LEEME.md)**.

## License

MIT

## Want to reclaim even more?

If you also want to free the space from your **conversations across every
tool** (Claude Code, Codex, Antigravity, Command Code) by trimming everything
before the last compaction — with a full backup first and an agent skill —
check out **[conversation-reclaim](https://github.com/ANGELBERRIOS23/conversation-reclaim)**,
which integrates this DB prune together with the other tools.

