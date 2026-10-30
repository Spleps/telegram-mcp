"""Offline tests for the bot part: no network, no real config.

Run:  python -m unittest discover -s tests -v
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as s  # noqa: E402

OWNER = 111
STRANGER = 999


def msg(chat, text="", **extra):
    m = {"message_id": 1, "date": 0, "chat": {"id": chat}, "text": text}
    m.update(extra)
    return m


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.replies = []
        p = mock.patch.object(s, "_reply", lambda chat, text: self.replies.append((chat, text)))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(s, "_inbox_lines", lambda: [])
        p2.start()
        self.addCleanup(p2.stop)

    def run_msg(self, chat, text, state=None):
        buf = io.StringIO()
        ctx = s.Ctx(msg(chat, text), OWNER, state or {}, buf)
        s.PIPELINE(ctx)
        return ctx, buf.getvalue()

    def test_stranger_text_is_ignored_silently(self):
        ctx, stored = self.run_msg(STRANGER, "привет")
        self.assertEqual(stored, "")
        self.assertEqual(ctx.added, 0)
        self.assertEqual(self.replies, [])

    def test_stranger_command_gets_private_reply_only(self):
        ctx, stored = self.run_msg(STRANGER, "/help")
        self.assertEqual(stored, "")
        self.assertEqual(self.replies, [(STRANGER, "Это приватный бот.")])

    def test_stranger_never_reaches_later_middleware(self):
        seen = []
        spy = lambda ctx, nxt: (seen.append(ctx.chat_id), nxt(ctx))
        pipe = s.build_pipeline([s.owner_only, spy, s.commands], s.store_in_inbox)
        pipe(s.Ctx(msg(STRANGER, "/status"), OWNER, {}, io.StringIO()))
        pipe(s.Ctx(msg(OWNER, "hi"), OWNER, {}, io.StringIO()))
        self.assertEqual(seen, [OWNER])

    def test_owner_help_and_start(self):
        for cmd in ("/help", "/start", "/help@some_bot"):
            self.replies.clear()
            ctx, stored = self.run_msg(OWNER, cmd)
            self.assertEqual(stored, "", cmd)
            self.assertEqual(self.replies, [(OWNER, s.HELP_TEXT)], cmd)

    def test_owner_status_counts_unseen(self):
        with mock.patch.object(s, "_inbox_lines", lambda: ["a", "b", "c"]):
            self.run_msg(OWNER, "/status", state={"seen": 1})
        self.assertEqual(self.replies, [(OWNER, "Ждут Claude: 2 сообщ.")])

    def test_owner_text_is_stored(self):
        ctx, stored = self.run_msg(OWNER, "сделай лабу 4")
        self.assertEqual(ctx.added, 1)
        self.assertEqual(json.loads(stored)["text"], "сделай лабу 4")
        self.assertEqual(self.replies, [])

    def test_unknown_command_is_stored_for_claude(self):
        ctx, stored = self.run_msg(OWNER, "/foo bar")
        self.assertEqual(ctx.added, 1)

    def test_middleware_order(self):
        calls = []
        mk = lambda name: (lambda ctx, nxt: (calls.append(name), nxt(ctx)))
        pipe = s.build_pipeline([mk("a"), mk("b")], lambda ctx: calls.append("final"))
        pipe(s.Ctx(msg(OWNER), OWNER, {}, io.StringIO()))
        self.assertEqual(calls, ["a", "b", "final"])


class PullUpdatesTests(unittest.TestCase):
    """pull_updates with a fake Telegram API and temporary inbox/state files."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.replies = []
        for name, value in [("INBOX", tmp / "inbox.jsonl"), ("STATE", tmp / "state.json"),
                            ("_reply", lambda c, t: self.replies.append((c, t))),
                            ("owner_id", lambda: OWNER)]:
            p = mock.patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_mixed_updates(self):
        updates = [
            {"update_id": 10, "message": msg(STRANGER, "spam")},
            {"update_id": 11, "message": msg(OWNER, "/help")},
            {"update_id": 12, "message": msg(OWNER, "заметка")},
            {"update_id": 13, "edited_message": msg(OWNER, "edited")},
            {"update_id": 14, "message": msg(OWNER, "", photo=[{"file_id": "small"}, {"file_id": "big"}])},
        ]
        with mock.patch.object(s, "bot_api", return_value=updates):
            added = s.pull_updates()
        self.assertEqual(added, 2)
        lines = [json.loads(x) for x in s.INBOX.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[0]["text"], "заметка")
        self.assertEqual(lines[1]["files"][0]["file_id"], "big")
        self.assertEqual(json.loads(s.STATE.read_text())["offset"], 15)
        self.assertEqual(self.replies, [(OWNER, s.HELP_TEXT)])

    def test_offset_is_sent_back(self):
        s.STATE.write_text(json.dumps({"offset": 42}))
        with mock.patch.object(s, "bot_api", return_value=[]) as api:
            s.pull_updates(timeout=5)
        api.assert_called_once_with("getUpdates", json={"offset": 42, "timeout": 5})


class HelperTests(unittest.TestCase):
    def test_describe_forwarded_document(self):
        m = msg(OWNER, "", caption="методичка",
                document={"file_id": "F1", "file_name": "lab.pdf", "file_size": 10},
                forward_origin={"sender_user": {"first_name": "Олег", "last_name": "К"}})
        d = s.describe_bot_message(m)
        self.assertEqual(d["text"], "методичка")
        self.assertEqual(d["forwarded_from"], "Олег К")
        self.assertEqual(d["files"], [{"type": "document", "file_id": "F1", "name": "lab.pdf", "size": 10}])

    def test_safe_name(self):
        self.assertEqual(s.safe_name('a/b:c*?.pdf'), "a_b_c__.pdf")
        self.assertEqual(s.safe_name("   "), "file")

    def test_unique_path(self):
        d = Path(tempfile.mkdtemp())
        (d / "x.txt").write_text("1")
        self.assertEqual(s.unique_path(d, "x.txt").name, "x (1).txt")


if __name__ == "__main__":
    unittest.main()
