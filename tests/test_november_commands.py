"""November command behavior tests without importing the running bot."""
from __future__ import annotations

import ast
import asyncio
import copy
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def isolate(name, **values):
    node = copy.deepcopy(next(n for n in TREE.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and n.name == name))
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        node,
    ], type_ignores=[]))
    ns = dict(values)
    exec(compile(module, "<isolated November command>", "exec"), ns)
    return ns[name]


class NovemberCommandTests(unittest.TestCase):
    def test_compact_recap_line(self):
        clean = isolate(
            "compact_bet_description",
            display_bet_text=lambda content, market, max_len=95: content,
            normalize_space=lambda s: " ".join(s.split()), re=re,
        )
        row_fn = isolate(
            "build_bet_result_line",
            compact_bet_description=clean,
            WIN_EMOJI="✅", LOSS_EMOJI="❌",
            PUSH_EMOJI="➖", VOID_EMOJI="↩️",
            format_compact_units=lambda v, signed=False: (f"{v:+.2f}u" if signed else f"{v:g}u"),
            format_odds_display=lambda x: x, WAGER_DFS="DFS",
        )
        row = ("", "PropKitchen", "win", 1, .86,
               "🏈 :49ers: Brock Purdy OVER 18.5 Rush Yards",
               "", "-116", "NFL", "https://discord.com/channels/1/2/3", "STRAIGHT", "")
        text = row_fn(row)
        self.assertIn("Brock Purdy o18.5 Rush Yards", text)
        self.assertNotIn(":49ers:", text)
        self.assertNotIn("NFL |", text)
        self.assertIn("1u", text)
        self.assertIn("-116", text)
        self.assertIn("+0.86u", text)
        self.assertIn("[jump]", text)

    def test_pending_links_only_for_own_guild(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE pending (guild_id INTEGER, capper TEXT, risk_units REAL, "
            "content TEXT, market TEXT, jump_url TEXT, channel_id INTEGER, "
            "message_id INTEGER, created_utc TEXT)")
        cur.executemany("INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (1, "PropKitchen", 1.0, "Brock Purdy OVER 18.5", "", "", 11, 101, "2026-10-30"),
            (2, "Other", 20.0, "PRIVATE BET", "", "", 21, 201, "2026-10-30"),
        ])
        output = []
        async def send(msg): output.append(msg)
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=42), send=send)
        pending = isolate(
            "pending_cmd",
            cur=cur,
            split_args=str.split,
            capper_by_user_id=lambda uid: None,
            resolve_capper_from_tokens=lambda tokens: (None, tokens),
            compact_bet_description=lambda content, market, max_len=82: content,
            format_compact_units=lambda risk: f"{risk:g}u",
            split_discord_text=lambda s: [s],
        )
        asyncio.run(pending(ctx, query="all"))
        joined = "\n".join(output)
        self.assertIn("Brock Purdy", joined)
        self.assertIn("https://discord.com/channels/1/11/101", joined)
        self.assertNotIn("PRIVATE BET", joined)
        conn.close()

    def test_partial_flex_payout_updates_only_scoped_bet(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE bets (message_id INTEGER, guild_id INTEGER, risk_units REAL, "
            "result TEXT, odds_text TEXT, net_units REAL, odds_format TEXT, multiplier REAL)")
        cur.executemany("INSERT INTO bets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            (101, 1, .5, "win", "25x", 12.0, "multiplier", 25.0),
            (102, 2, .5, "win", "25x", 12.0, "multiplier", 25.0),
        ])
        events, messages = [], []
        async def send(msg): messages.append(msg)
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1),
                              author=SimpleNamespace(id=42), send=send)
        payout = isolate(
            "setpayout_cmd", cur=cur, conn=conn,
            referenced_message_id=lambda _ctx: 101,
            parse_manual_odds_text=lambda x: x,
            parse_multiplier_value=lambda x: float(x[:-1]) if x.endswith("x") else None,
            compute_net_units=lambda units, odds, result: units * (float(odds[:-1]) - 1),
            log_correction=lambda *args: events.append(args),
        )
        asyncio.run(payout(ctx, "2x"))
        row = cur.execute("SELECT odds_text, net_units FROM bets WHERE message_id=101").fetchone()
        untouched = cur.execute("SELECT odds_text, net_units FROM bets WHERE message_id=102").fetchone()
        self.assertEqual(row, ("2x", .5))
        self.assertEqual(untouched, ("25x", 12.0))
        self.assertTrue(events)
        self.assertIn("2x", messages[-1])
        conn.close()


if __name__ == "__main__":
    unittest.main()
