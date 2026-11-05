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
        aliases = next(n for n in TREE.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RECAP_MARKET_ABBREVIATIONS"
            for t in n.targets
        ))
        clean = isolate(
            "compact_bet_description",
            display_bet_text=lambda content, market, max_len=95: content,
            normalize_space=lambda s: " ".join(s.split()), re=re,
            RECAP_MARKET_ABBREVIATIONS=ast.literal_eval(aliases.value),
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
               "", "-116", "NFL", "https://discord.com/channels/1/2/3", "STRAIGHT", "", "2026-10-30")
        text = row_fn(row)
        self.assertIn("Brock Purdy o18.5 Rush Yards", text)
        self.assertNotIn(":49ers:", text)
        self.assertNotIn("NFL |", text)
        self.assertIn("1u", text)
        self.assertIn("-116", text)
        self.assertIn("+0.86u", text)
        self.assertIn("[jump]", text)


    def test_recaps_recover_player_when_stake_line_only_has_sportsbook(self):
        aliases = next(n for n in TREE.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RECAP_MARKET_ABBREVIATIONS"
            for t in n.targets
        ))
        abbrevs = ast.literal_eval(aliases.value)
        def compact(content, market=""):
            # The underlying generic extractor currently picks the platform on
            # these old multiline posts. The recap must find the actual wager.
            return isolate(
                "compact_bet_description",
                display_bet_text=lambda content, market, max_len=195: "Fanatics",
                normalize_space=lambda s: " ".join(s.split()),
                RECAP_MARKET_ABBREVIATIONS=abbrevs, re=re,
            )(content, market)

        old_post = (
            "## NBA Prop #5 🏀 (11/4)\n\n"
            "<:MEMGrizzlies:1429488742921670820> : **Cedric Coward OVER 6.5 Rebounds**\n"
            "1u | -130 Fanatics <:fanatics:1504956348411023380>\n"
            "<@&1387012502050701423>"
        )
        actual = compact(old_post)
        self.assertIn("Cedric Coward o6.5 Reb", actual)
        self.assertNotIn("Fanatics", actual)
        self.assertNotIn("MEMGrizzlies", actual)
        self.assertNotIn("**", actual)

        draftkings_post = (
            "NBA Prop #4\n\n:Wizards: **Player Example UNDER 4.5 Assists**\n"
            "1u | -107 DraftKings\n<@&123456789012345678>"
        )
        draft = compact(draftkings_post)
        self.assertIn("Player Example u4.5 Ast", draft)
        self.assertNotIn("DraftKings", draft)

    def test_recap_keeps_regular_single_line_wagers(self):
        aliases = next(n for n in TREE.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RECAP_MARKET_ABBREVIATIONS"
            for t in n.targets
        ))
        compact = isolate(
            "compact_bet_description",
            display_bet_text=lambda content, market, max_len=195: content,
            normalize_space=lambda s: " ".join(s.split()),
            RECAP_MARKET_ABBREVIATIONS=ast.literal_eval(aliases.value),
            re=re,
        )
        self.assertIn(
            "Jaime Jaquez Jr o8.5 Reb+Ast",
            compact("1u Jaime Jaquez Jr OVER 8.5 Reb+Ast", ""),
        )


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
            "result TEXT, odds_text TEXT, net_units REAL, odds_format TEXT, multiplier REAL, capper_user_id INTEGER)")
        cur.executemany("INSERT INTO bets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (101, 1, .5, "win", "25x", 12.0, "multiplier", 25.0, 42),
            (102, 2, .5, "win", "25x", 12.0, "multiplier", 25.0, 999),
        ])
        events, messages = [], []
        async def send(msg): messages.append(msg)
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1),
                              author=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(manage_guild=False)), send=send)
        payout = isolate(
            "setpayout_cmd", cur=cur, conn=conn,
            referenced_message_id=lambda _ctx: 101,
            ADMIN_USER_ID=9, re=re,
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
