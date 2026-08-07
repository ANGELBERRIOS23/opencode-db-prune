# opencode-db-prune

*[Versión en español](LEEME.md)*

> **This version is tested on macOS only.** See [Platforms](#platforms).

Your `opencode.db` is probably enormous, and probably 90 % of it is one table
you don't need.

This is a single-file Python script, standard library only, that reclaims it —
without deleting a single session, message or file part.

```
database : /home/you/.local/share/opencode/opencode.db
size     : 39.37 GB

event table: 953,214 rows, 35.42 GB
  message.updated.1            228,444    33.87 GB
  message.part.updated.1       659,569     1.51 GB
  session.updated.1             63,095     0.05 GB

would prune (all but the 5 newest sessions): 949,404 rows, 35.42 GB
estimated size afterwards: ~3.96 GB
```

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

```bash
python3 opencode-db-prune.py                          # report only, changes nothing
python3 opencode-db-prune.py --apply                  # prune, backing up first
python3 opencode-db-prune.py --apply --no-backup      # skip the backup copy
python3 opencode-db-prune.py --apply --keep 20        # leave 20 newest sessions alone
python3 opencode-db-prune.py --db /path/to/opencode.db
```

On Windows, the Python launcher works too:

```powershell
py opencode-db-prune.py
py opencode-db-prune.py --apply
```

**Quit OpenCode first.** The script checks whether the file is open and refuses
to run if it is.

No dependencies. Python 3.8+.

## Safeguards

- Refuses to run while the database file is held open by another process.
- Refuses to run if message content is not readable outside `event`.
- Refuses to run if the database was already corrupt.
- Backs up first unless you pass `--no-backup` (the backup needs as much free
  space as the database).
- Runs `PRAGMA integrity_check` before and after.
- Reports how many sessions, messages and parts survived, so you can see that
  nothing was lost.
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
| Windows | implemented, needs field confirmation | `%USERPROFILE%\.local\share\opencode\opencode.db` (CLI), `%LOCALAPPDATA%\opencode\data\opencode.db` (desktop app) |

If several databases exist, the largest is picked — that's the one with the
problem. `OPENCODE_DB` is honoured and wins over everything else. You can also
bypass detection entirely with `--db /path/to/opencode.db`.

### Contributions for Linux and Windows are welcome

If you hit the same problem on another platform and want to improve that part,
the most useful things are:

- Confirm where OpenCode actually keeps the database on your system.
- Check the "file in use" detection. On Windows it requests an exclusive handle
  with `CreateFileW`; on Linux it shells out to `lsof`, which is not always
  installed.
- Say whether your numbers look like these, or whether the breakdown of the
  `event` table is different on your setup.

Opening an issue with the output of the report (the command without `--apply`,
which changes nothing) is enough. Pull requests welcome.

## How this differs from a VACUUM

Other cleanup tools run `VACUUM`, which reclaims **free pages** — space that was
freed inside the file but never returned to the filesystem. That is a real
problem and those tools solve it.

It does not solve *this* one. Here the space is **live rows**. VACUUM alone
reclaims nothing until the rows are gone. This script deletes the redundant rows
first and then runs VACUUM to shrink the file.

If your database is bloated but the `event` table is small, you want a VACUUM
tool, not this one. Run the report first and you'll see which case you're in.

## Related upstream issues

- [#22110 — Session storage grows unboundedly](https://github.com/anomalyco/opencode/issues/22110)
- [#31391 — why opencode.db so large?](https://github.com/anomalyco/opencode/issues/31391)
- [#16777 — High memory usage and database bloat](https://github.com/anomalyco/opencode/issues/16777)

The real fix belongs upstream: don't persist a full snapshot per streaming
update, or prune the change feed when a session completes. Until then, this.

---

## Español

Hay una versión completa en español: **[LEEME.md](LEEME.md)**.

## License

MIT
