"""Command-only retention is opt-in, guild-scoped and harmless to bets."""
import ast
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import types
import unittest

from bettracker_autoclean import (
    ensure_autoclean_schema,
    get_retention_days,
    set_retention_days,
    disable_autoclean,
    enqueue_message,
    due_messages,
    queued_count,
    dequeue_message,
    defer_message,
)


class AutocleanDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE bets (message_id INTEGER PRIMARY KEY, net_units REAL)")
        self.db.execute("INSERT INTO bets VALUES (111, 4.5)")
        self.db.execute("CREATE TABLE pending (message_id INTEGER PRIMARY KEY)")
        self.db.execute("INSERT INTO pending VALUES (222)")
        ensure_autoclean_schema(self.db)

    def tearDown(self):
        self.db.close()

    def test_disabled_by_default_and_no_historical_backfill(self):
        self.assertIsNone(get_retention_days(self.db, 123, 999))
        self.assertFalse(enqueue_message(self.db, 123, 999, 300, 1000))
        set_retention_days(self.db, 123, 999, 3)
        self.assertEqual([], due_messages(self.db, 999999))

    def test_scheduled_deletion_only_after_3_days_and_one_per_message(self):
        set_retention_days(self.db, 123, 999, 3)
        self.assertTrue(enqueue_message(self.db, 123, 999, 301, 1000))
        self.assertTrue(enqueue_message(self.db, 123, 999, 301, 1000))
        self.assertEqual(1, queued_count(self.db, 123, 999))
        self.assertEqual([], due_messages(self.db, 1000 + 3 * 86400 - 1))
        self.assertEqual([(123, 999, 301)], due_messages(self.db, 1000 + 3 * 86400))
        dequeue_message(self.db, 123, 999, 301)
        self.assertEqual([], due_messages(self.db, 1000 + 4 * 86400))

    def test_off_cancels_pending_deletions_and_preserves_bets(self):
        set_retention_days(self.db, 123, 999, 3)
        enqueue_message(self.db, 123, 999, 301, 1000)
        self.assertEqual(1, disable_autoclean(self.db, 123, 999))
        self.assertIsNone(get_retention_days(self.db, 123, 999))
        self.assertEqual([], due_messages(self.db, 999999))
        self.assertEqual([(111, 4.5)], self.db.execute("SELECT * FROM bets").fetchall())
        self.assertEqual([(222,)], self.db.execute("SELECT * FROM pending").fetchall())

    def test_server_and_channel_settings_are_independent(self):
        set_retention_days(self.db, 1, 400, 3)
        set_retention_days(self.db, 2, 400, 7)
        set_retention_days(self.db, 1, 500, 1)
        for guild, channel, message in ((1, 400, 101), (2, 400, 102), (1, 500, 103)):
            enqueue_message(self.db, guild, channel, message, 1000)
        self.assertEqual([(1, 500, 103)], due_messages(self.db, 1000 + 86400))
        self.assertEqual({(1, 400, 101), (1, 500, 103)}, set(
            due_messages(self.db, 1000 + 3 * 86400)
        ))
        self.assertEqual(1, disable_autoclean(self.db, 1, 400))
        self.assertEqual(7, get_retention_days(self.db, 2, 400))
        self.assertEqual([(2, 400, 102)], [
            x for x in due_messages(self.db, 1000 + 7 * 86400) if x[0] == 2
        ])

    def test_missing_permissions_retries_later_without_blocking_other_guilds(self):
        set_retention_days(self.db, 1, 100, 3)
        set_retention_days(self.db, 2, 200, 3)
        enqueue_message(self.db, 1, 100, 101, 1000)
        enqueue_message(self.db, 2, 200, 202, 1000)
        due = 1000 + 3 * 86400
        defer_message(self.db, 1, 100, 101, due + 3600)
        self.assertEqual([(2, 200, 202)], due_messages(self.db, due))
        self.assertEqual({(1, 100, 101), (2, 200, 202)},
                         set(due_messages(self.db, due + 3600)))

    def test_duration_is_bounded(self):
        for days in (0, 31, 100):
            with self.assertRaises(ValueError):
                set_retention_days(self.db, 1, 2, days)


class AutocleanBotGuardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text())

    def function(self, name, scope):
        fn = next(
            x for x in self.tree.body
            if isinstance(x, ast.FunctionDef) and x.name == name
        )
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "<autoclean guard>", "exec"), scope)
        return scope[name]

    def test_protected_channels_are_never_cleanup_targets(self):
        f = self.function("is_autoclean_protected_channel", {
            "SUMMARY_CHANNEL_ID": 999,
            "is_trackable_channel": lambda x: x == 555,
        })
        def channel(id, name):
            return types.SimpleNamespace(id=id, name=name)
        self.assertTrue(f(channel(999, "bettingtracker")))
        self.assertTrue(f(channel(555, "picks")))
        self.assertTrue(f(channel(321, "vip-results")))
        self.assertTrue(f(channel(322, "monthly-recaps")))
        self.assertFalse(f(channel(333, "vipbot-commands")))

    def test_bot_only_queues_capper_commands_and_its_own_responses(self):
        captured = []
        scope = {
            "bot": types.SimpleNamespace(user=types.SimpleNamespace(id=10)),
            "is_autoclean_protected_channel": lambda x: False,
            "is_bot_command_message": lambda m: m.content.startswith("bt!"),
            "enqueue_message": lambda *args: captured.append(args),
            "conn": object(),
            "sqlite3": sqlite3,
        }
        fn = self.function("maybe_queue_command_message", scope)
        def msg(uid, text, *, is_bot=False):
            return types.SimpleNamespace(
                guild=types.SimpleNamespace(id=123),
                channel=types.SimpleNamespace(id=456),
                author=types.SimpleNamespace(id=uid, bot=is_bot),
                content=text, id=1000 + uid,
                created_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
            )
        fn(msg(11, "bt!capper gr8 October"))
        fn(msg(12, "bt!leaderboard NBA October"))
        fn(msg(10, "Your recap is ready", is_bot=True))
        fn(msg(13, "Normal chat should stay"))
        fn(msg(14, "1u Knicks ML (-110)"))
        fn(msg(15, "bt!fake", is_bot=True))
        self.assertEqual(3, len(captured))
        self.assertEqual({1011, 1012, 1010}, {args[3] for args in captured})

    def test_command_requires_admin_and_worker_checks_protected_channel(self):
        source = (Path(__file__).resolve().parents[1] / "bot.py").read_text()
        self.assertIn('@commands.has_permissions(manage_guild=True)\nasync def autoclean_cmd(', source.replace("\r\n", "\n"))
        self.assertIn('if channel is None or is_autoclean_protected_channel(channel):', source)
        self.assertIn('if not autoclean_loop.is_running():', source)


if __name__ == "__main__":
    unittest.main()
