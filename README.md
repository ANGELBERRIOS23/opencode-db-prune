# opencode-db-prune

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

## Where it looks for the database

| Platform | Path |
|---|---|
| Linux | `$XDG_DATA_HOME/opencode/opencode.db`, `~/.local/share/opencode/opencode.db` |
| macOS | `~/.local/share/opencode/opencode.db`, `~/Library/Application Support/opencode/opencode.db` |
| Windows | `%LOCALAPPDATA%\opencode\opencode.db`, `%APPDATA%\opencode\opencode.db` |

If several exist, it picks the largest — that's the one with the problem. Or
pass `--db` explicitly.

Developed and tested on macOS. Path and lock detection for Linux and Windows
are implemented but less exercised; reports welcome.

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

## En español

Tu `opencode.db` probablemente pesa muchísimo, y lo más probable es que el 90 %
sea una sola tabla que no necesitas.

**El motivo:** OpenCode guarda una copia completa del mensaje **cada vez que lo
actualiza**. Mientras una respuesta se va escribiendo, eso ocurre muchas veces, y
cada copia arrastra el texto acumulado entero. En una instalación real: 39 GB de
base, de los cuales 35 GB eran esas copias y solo 3 GB las conversaciones.

**Por qué se puede borrar:** el contenido definitivo vive en las tablas
`message` y `part` — el script lo comprueba antes de tocar nada, abriendo la
sesión más antigua y verificando que su texto siga ahí. Y el propio OpenCode
borra esa tabla en una de sus migraciones, sin tocar `session`, `message` ni
`part`.

**No borra sesiones, ni mensajes, ni archivos.** Solo eventos redundantes.

```bash
python3 opencode-db-prune.py            # informe, no toca nada
python3 opencode-db-prune.py --apply    # limpia, respaldando antes
```

Cierra OpenCode antes de ejecutarlo.

## License

MIT
