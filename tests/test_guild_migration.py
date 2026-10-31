"""Safety tests for owner-approved migration and monthly VIP role configuration."""
from __future__ import annotations

import ast
import asyncio
import copy
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def isolate(name, **deps):
    node = copy.deepcopy(next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name))
    node.decorator_list = []
    tree = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node,
    ], type_ignores=[]))
    scope = dict(deps)
    exec(compile(tree, "<isolated migration>", "exec"), scope)
    return scope[name]


class MigrationTests(unittest.TestCase):
    def test_preview_does_nothing_and_confirm_scopes_to_guild(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        for table in ("bets", "pending"):
            cur.execute(
                f"CREATE TABLE {table} (message_id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, jump_url TEXT)"
            )
        cur.executemany("INSERT INTO bets VALUES (?, ?, ?, ?)", [
            (1, 0, 11, ""),  # Verified channel in this server
            (2, 0, 77, "https://discord.com/channels/100/77/2"),  # Verified jump link
            (3, 0, 77, "https://discord.com/channels/200/77/3"),  # Different server
            (4, 0, 99, ""),  # Unverified legacy record
            (5, 100, 11, ""),  # Already migrated
        ])
        cur.execute("INSERT INTO pending VALUES (6, 0, 11, '')")
        messages = []
        async def send(m): messages.append(m)
        guild = SimpleNamespace(id=100, name="Test",
                                get_channel=lambda cid: object() if cid == 11 else None)
        ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(id=42), send=send)
        run = isolate("migrateguild_cmd", ctx=None, cur=cur, conn=conn, ADMIN_USER_ID=42,
                      TRACKED_CHANNELS={11: None},
                      SHARED_TRACKING_CHANNEL_IDS=set())
        asyncio.run(run(ctx, "preview"))
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM bets WHERE guild_id=0").fetchone()[0], 4)
        self.assertIn("No data changed", messages[-1])
        asyncio.run(run(ctx, "CONFIRM"))
        self.assertEqual(cur.execute("SELECT guild_id FROM bets WHERE message_id=1").fetchone()[0], 100)
        self.assertEqual(cur.execute("SELECT guild_id FROM bets WHERE message_id=2").fetchone()[0], 100)
        self.assertEqual(cur.execute("SELECT guild_id FROM bets WHERE message_id=3").fetchone()[0], 0)
        self.assertEqual(cur.execute("SELECT guild_id FROM bets WHERE message_id=4").fetchone()[0], 0)
        self.assertEqual(cur.execute("SELECT guild_id FROM pending WHERE message_id=6").fetchone()[0], 100)
        conn.close()

    def test_manual_corrections_only_accept_same_guild_messages(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE bets (message_id INTEGER PRIMARY KEY, guild_id INTEGER)")
        cur.execute("CREATE TABLE pending (message_id INTEGER PRIMARY KEY, guild_id INTEGER)")
        cur.execute("INSERT INTO bets VALUES (201, 1)")
        cur.execute("INSERT INTO bets VALUES (202, 2)")
        cur.execute("INSERT INTO pending VALUES (203, 1)")
        fn = isolate("referenced_message_id", cur=cur)
        for message_id, expected in ((201, 201), (202, None), (203, 203), (999, None)):
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=1),
                message=SimpleNamespace(reference=SimpleNamespace(message_id=message_id)),
            )
            self.assertEqual(fn(ctx), expected)
        conn.close()

    def test_monthly_roles_persist_exactly_two_roles(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE recap_settings (guild_id INTEGER PRIMARY KEY, monthly_role_ids TEXT NOT NULL)")
        guild = SimpleNamespace(id=100)
        one = SimpleNamespace(id=401, name="VIP Members", guild=guild, is_default=lambda: False)
        two = SimpleNamespace(id=402, name="VIP Members Winible", guild=guild, is_default=lambda: False)
        messages = []
        async def send(m): messages.append(m)
        ctx = SimpleNamespace(guild=guild, send=send)
        run = isolate("monthlyroles_cmd", cur=cur, conn=conn)
        asyncio.run(run(ctx, one, two))
        self.assertEqual(cur.execute("SELECT monthly_role_ids FROM recap_settings").fetchone()[0], "401,402")
        self.assertIn("3rd", messages[-1])
        conn.close()

    def test_monthly_post_previous_month_on_day_three(self):
        self.assertIn("MONTHLY_POST_DAY = 3", SOURCE)
        self.assertIn("nl.date().replace(day=1) - timedelta(days=1)", SOURCE)


if __name__ == "__main__":
    unittest.main()
