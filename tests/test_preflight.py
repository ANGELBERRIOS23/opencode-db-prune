"""Pre-flight check and post-prune verification on small made-up databases.

Run from the repository root:  python3 -m unittest discover -s tests
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "opencode-db-prune.py")

spec = importlib.util.spec_from_file_location("prune", SCRIPT)
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)

V1_SCHEMA = """
create table session (id text primary key, time_created integer, time_updated integer);
create table message (id text primary key, session_id text not null,
                      time_created integer, data text);
create table part (id text primary key, message_id text not null, session_id text not null,
                   time_created integer, time_updated integer, data text);
create table event (id text primary key, aggregate_id text not null, seq integer not null,
                    type text not null, data text not null, created integer);
create table event_sequence (aggregate_id text primary key, seq integer, owner_id text);
"""

V2_SCHEMA = """
create table session_v2 (id text primary key, time_created integer);
create table session_message (id text primary key, session_id text not null,
                              type text, data text);
"""


class Db:
    """Builds an OpenCode 1 database one session at a time."""

    def __init__(self, path):
        self.cx = sqlite3.connect(path)
        self.cx.executescript(V1_SCHEMA)
        self.t = 0

    def session(self, sid, texts=(), tools=0, keep_parts=True):
        """A session with one message holding these text parts and tool parts.

        Each part also gets streaming snapshots in `event`, like OpenCode 1
        writes. keep_parts=False leaves `part` without them.
        """
        self.t += 1
        self.cx.execute("insert into session values (?,?,?)", (sid, self.t, self.t))
        mid = f"msg_{sid}"
        self.cx.execute("insert into message values (?,?,?,?)",
                        (mid, sid, self.t, json.dumps({"role": "assistant"})))
        parts = [{"type": "text", "text": t} for t in texts]
        parts += [{"type": "tool", "tool": "bash", "state": {"output": "ok"}}] * tools
        seq = 0
        for i, data in enumerate(parts):
            pid = f"prt_{sid}_{i}"
            if keep_parts:
                self.cx.execute("insert into part values (?,?,?,?,?,?)",
                                (pid, mid, sid, self.t, self.t, json.dumps(data)))
            for step in range(3):
                snap = dict(data, id=pid, messageID=mid, sessionID=sid)
                if "text" in snap:
                    snap["text"] = snap["text"][:len(snap["text"]) * (step + 1) // 3]
                self.cx.execute(
                    "insert into event values (?,?,?,?,?,?)",
                    (f"evt_{sid}_{i}_{step}", sid, seq, "message.part.updated.1",
                     json.dumps({"sessionID": sid, "part": snap}), self.t))
                seq += 1
        self.cx.execute("insert into event_sequence values (?,?,?)", (sid, seq, None))
        return self

    def done(self):
        self.cx.commit()
        return self.cx


def preflight(cx):
    return prune.content_survives_outside_event(cx, prune.detect_layout(cx))


class PreflightV1(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "opencode.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_oldest_session_without_text(self):
        # Issue #4: the oldest session has 3 parts and no text.
        db = Db(self.path).session("ses_0", tools=3)
        for i in range(1, 12):
            db.session(f"ses_{i}", texts=[f"reply number {i}, long enough"])
        ok, detail = preflight(db.done())
        self.assertTrue(ok, detail)

    def test_text_only_in_event(self):
        db = Db(self.path)
        for i in range(10):
            db.session(f"ses_{i}", texts=[f"reply number {i}"], keep_parts=False)
        ok, detail = preflight(db.done())
        self.assertFalse(ok, detail)

    def test_events_of_deleted_sessions_are_ignored(self):
        db = Db(self.path)
        for i in range(10):
            db.session(f"ses_{i}", texts=[f"reply number {i}"])
        cx = db.done()
        # Deleting sessions leaves their events behind.
        for i in range(6):
            cx.execute("delete from part where session_id=?", (f"ses_{i}",))
            cx.execute("delete from message where session_id=?", (f"ses_{i}",))
            cx.execute("delete from session where id=?", (f"ses_{i}",))
        cx.commit()
        ok, detail = preflight(cx)
        self.assertTrue(ok, detail)

    def test_no_text_in_event(self):
        db = Db(self.path)
        for i in range(5):
            db.session(f"ses_{i}", tools=2)
        cx = db.done()
        cx.execute("insert into part values ('p','msg_ses_0','ses_0',1,1,?)",
                   (json.dumps({"type": "text", "text": "hello"}),))
        cx.commit()
        ok, detail = preflight(cx)
        self.assertTrue(ok, detail)
        cx.execute("delete from part where id='p'")
        cx.commit()
        ok, detail = preflight(cx)
        self.assertFalse(ok, detail)

    def test_apply_end_to_end(self):
        db = Db(self.path).session("ses_0", tools=3)
        for i in range(1, 20):
            db.session(f"ses_{i}", texts=[f"reply number {i}, long enough"], tools=1)
        db.done().close()
        run = subprocess.run(
            [sys.executable, SCRIPT, "--apply", "--no-backup", "--db", self.path],
            capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("0 failed", run.stdout)
        cx = sqlite3.connect(self.path)
        self.assertEqual(cx.execute("select count(*) from session").fetchone()[0], 20)
        # 3 tool parts in ses_0, a text and a tool part in each of the other 19.
        self.assertEqual(cx.execute("select count(*) from part").fetchone()[0], 41)
        # Events of the 5 newest sessions are kept by default.
        kept = cx.execute("select count(distinct aggregate_id) from event").fetchone()[0]
        self.assertEqual(kept, 5)


class PreflightV2(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cx = sqlite3.connect(os.path.join(self.dir.name, "opencode.db"))
        self.cx.executescript(V2_SCHEMA)

    def tearDown(self):
        self.cx.close()
        self.dir.cleanup()

    def add(self, sid, t, text):
        self.cx.execute("insert into session_v2 values (?,?)", (sid, t))
        content = [{"type": "text", "text": text}] if text else [{"type": "tool"}]
        self.cx.execute("insert into session_message values (?,?,?,?)",
                        (f"m_{sid}", sid, "assistant", json.dumps({"content": content})))

    def test_oldest_session_without_text(self):
        self.add("ses_0", 0, None)
        for i in range(1, 12):
            self.add(f"ses_{i}", i, f"reply {i}")
        ok, detail = preflight(self.cx)
        self.assertTrue(ok, detail)

    def test_no_text_anywhere(self):
        for i in range(12):
            self.add(f"ses_{i}", i, None)
        ok, detail = preflight(self.cx)
        self.assertFalse(ok, detail)


if __name__ == "__main__":
    unittest.main()
