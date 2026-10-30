"""Regression tests extracted from real bot functions without starting Discord."""
from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
MODULE = ast.parse(SOURCE)


def load_function(name: str, **dependencies):
    node = next(
        n for n in MODULE.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            node,
        ], type_ignores=[])
    )
    namespace = dict(dependencies)
    exec(compile(module, "<isolated bot function>", "exec"), namespace)
    return namespace[name]


class GuildIsolationTests(unittest.TestCase):
    def test_ungrade_preserves_guild_id(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        columns = [
            "channel_id", "capper", "capper_user_id", "author_user_id",
            "content", "sport", "risk_units", "odds_text", "created_utc",
            "bet_date", "jump_url", "league", "event", "player", "team",
            "opponent", "bet_type", "market", "line", "sportsbook",
            "odds_format", "multiplier", "wager_category", "platform",
            "platform_type", "duplicate_key", "guild_id",
        ]
        for table in ("bets", "pending"):
            cur.execute(
                f"CREATE TABLE {table} (message_id INTEGER PRIMARY KEY, " +
                ", ".join(f"{c} TEXT" for c in columns) + ")"
            )
        defaults = {name: "" for name in columns}
        defaults.update({
            "channel_id": 101, "capper": "test", "capper_user_id": 201,
            "author_user_id": 201, "sport": "NBA", "risk_units": 1.5,
            "odds_text": "-110", "created_utc": "2026-10-30T12:00:00+00:00",
            "guild_id": 303,
        })
        insert_columns = ["message_id", *columns]
        cur.execute(
            f"INSERT INTO bets ({','.join(insert_columns)}) " +
            f"VALUES ({','.join('?' for _ in insert_columns)})",
            (99, *(defaults[c] for c in columns)),
        )
        conn.commit()
        ungrade = load_function(
            "ungrade_bet", cur=cur, conn=conn, WAGER_STRAIGHT="STRAIGHT"
        )
        self.assertTrue(ungrade(99))
        self.assertIsNone(cur.execute("SELECT message_id FROM bets WHERE message_id=99").fetchone())
        self.assertEqual(
            303,
            int(cur.execute("SELECT guild_id FROM pending WHERE message_id=99").fetchone()[0]),
        )
        conn.close()

    def test_clear_pending_cannot_delete_another_guild(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE pending (message_id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, capper TEXT)")
        cur.executemany(
            "INSERT INTO pending VALUES (?, ?, ?, ?)",
            [(1, 303, 10, "one"), (2, 404, 20, "two")],
        )
        conn.commit()
        async def send(_message):
            return None
        ctx = SimpleNamespace(guild=SimpleNamespace(id=303), send=send)
        bot = SimpleNamespace(get_channel=lambda _id: None)
        clear_pending = load_function("clear_pending_rows", cur=cur, conn=conn, bot=bot)
        asyncio.run(clear_pending(ctx, "1 = 1", tuple(), "All Pending Bets"))
        self.assertEqual([(2, 404)], cur.execute("SELECT message_id, guild_id FROM pending").fetchall())
        conn.close()


class ReportingTests(unittest.TestCase):
    def test_recent_bets_includes_newest_not_oldest(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE bets (graded_utc TEXT, capper TEXT, result TEXT, "
            "risk_units REAL, net_units REAL, content TEXT, market TEXT, "
            "odds_text TEXT, sport TEXT, jump_url TEXT, wager_category TEXT, "
            "platform TEXT, sportsbook TEXT, guild_id INTEGER)"
        )
        cur.executemany(
            "INSERT INTO bets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (f"2026-10-{i // 24 + 1:02d}T{i % 24:02d}:00:00", "test", "win",
                 1.0, 1.0, str(i), "PTS", "-100", "NBA", "", "STRAIGHT", "", "", 303)
                for i in range(100)
            ],
        )
        fetch = load_function(
            "fetch_recent_bets",
            cur=cur, WAGER_CATEGORY_SQL="wager_category", WAGER_STRAIGHT="STRAIGHT"
        )
        result = fetch("guild_id = ?", (303,), limit=75)
        self.assertEqual(75, len(result))
        self.assertEqual("25", result[0][5])
        self.assertEqual("99", result[-1][5])
        self.assertFalse(any(row[5] == "0" for row in result))
        conn.close()


class WarningTests(unittest.TestCase):
    def test_reply_and_normal_odds_discussion_not_warned(self):
        fn = load_function(
            "looks_like_wager_message",
            is_bot_command_message=lambda message: False,
            RE_UNITS=re.compile(r"\b\d+(?:\.\d+)?u\b"),
            parse_odds_text=lambda text: "-120" if "-120" in text else "",
            infer_platform=lambda text: ("", ""),
        )
        reply = SimpleNamespace(reference=SimpleNamespace(message_id=5))
        normal = SimpleNamespace(reference=None)
        self.assertFalse(fn(reply, "Zay Flowers OVER 80 -120 1u"))
        self.assertFalse(fn(normal, "I saw the -120 odds earlier"))
        self.assertTrue(fn(normal, "Zay Flowers OVER 80 -120"))


if __name__ == "__main__":
    unittest.main()
