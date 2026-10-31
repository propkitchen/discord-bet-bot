"""No token or Discord login: validate multi-rung independence and compact recaps."""
from __future__ import annotations

import ast
import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import types
import unittest
from unittest.mock import AsyncMock

from bettracker_ladders import parse_ladder_post, is_ladder_post

SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text()
TREE = ast.parse(SOURCE)


def load(name, scope):
    fn = copy.deepcopy(next(
        node for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ))
    fn.decorator_list = []
    mod = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        fn,
    ], type_ignores=[]))
    exec(compile(mod, "<isolated ladder/recap>", "exec"), scope)
    return scope[name]


class LadderParserTests(unittest.TestCase):
    EXAMPLE = (
        "NFL Drake Maye Pass Yards Ladder 🪜\n"
        ".75u 225+ Pass Yards (-110)\n"
        ".5u 250+ Pass Yards (+120) 🪜\n"
        ".25u 300+ Pass Yards (+240) 🪜\n"
        ".1u 350+ Pass Yards (+500) 🪜"
    )

    def test_four_rungs_and_american_odds_each_independent(self):
        ladder = parse_ladder_post(self.EXAMPLE)
        self.assertEqual(4, len(ladder.rungs))
        self.assertEqual([.75, .5, .25, .1], [r.risk for r in ladder.rungs])
        self.assertEqual(["-110", "+120", "+240", "+500"], [r.odds for r in ladder.rungs])
        self.assertIn("NFL", ladder.preface)
        self.assertNotIn("🪜", ladder.rungs[1].line)

    def test_invalid_or_unmarked_ladder_not_silently_recorded(self):
        examples = [
            ".5u 225+ Pass Yards (-110)\n.2u 250+ Pass Yards (+120)",
            "NFL LADDER\n.5u 225+ Pass Yards (-110)\n.2u 250+ Pass Yards",
            "NFL LADDER\n.5u 225+ Pass Yards (-110)",
            "NFL LADDER\n.5u 225+ Pass Yards (-110)\n.5u 225+ Pass Yards (-110)",
        ]
        for text in examples:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_ladder_post(text)

    def test_ladder_indicator_does_not_confuse_plain_wager(self):
        self.assertFalse(is_ladder_post("1u Scottie Barnes over 20.5 Pts (-110)"))
        self.assertTrue(is_ladder_post("NFL Ladder 🪜\n.5u 250+ Pass Yds (+130)"))


class GradingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""
            CREATE TABLE ladder_sources (
                source_message_id INTEGER PRIMARY KEY, guild_id INTEGER,
                channel_id INTEGER, capper_user_id INTEGER, rung_count INTEGER,
                created_utc TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE ladder_rungs (
                source_message_id INTEGER, rung_number INTEGER,
                tracked_message_id INTEGER UNIQUE,
                PRIMARY KEY (source_message_id, rung_number)
            )
        """)
        self.cur = self.db.cursor()

    def tearDown(self):
        self.db.close()

    def test_one_original_message_fans_out_to_four_gradeable_messages_once(self):
        outputs = []
        warnings = []
        reactions = []
        counter = [2100]

        class FakeMessage:
            id = 1001
            content = LadderParserTests.EXAMPLE
            guild = types.SimpleNamespace(id=10)
            channel = types.SimpleNamespace(id=20)
            author = types.SimpleNamespace(id=99)
            created_at = datetime(2026, 10, 31, tzinfo=timezone.utc)
            async def reply(self, text, **kwargs):
                counter[0] += 1
                outputs.append(text)
                return types.SimpleNamespace(
                    id=counter[0],
                    jump_url=f"https://discord.com/channels/10/20/{counter[0]}",
                )

        def insert_pending(mid, gid, cid, capper, author, text, created, jump):
            self.assertEqual(gid, 10)
            self.assertEqual(cid, 20)
            self.assertEqual(capper.name, "PropKitchen")
            self.assertIn("NFL", text)
            self.assertIn("Pass Yards", text)
            self.assertEqual(author, 99)
            # Real insert_pending records a separate message_id per rung.
            reactions.append((mid, text))
            return True, None

        ctx = {
            "cur": self.cur, "conn": self.db, "discord": types.SimpleNamespace(
                HTTPException=RuntimeError,
                AllowedMentions=types.SimpleNamespace(none=lambda: None),
            ),
            "message_to_text": lambda m: m.content,
            "is_ladder_post": is_ladder_post,
            "parse_ladder_post": parse_ladder_post,
            "send_temporary_notice": AsyncMock(side_effect=lambda m, t: warnings.append(t)),
            "infer_sports": lambda x: ["NFL"] if "NFL" in x else [],
            "utc_iso": lambda dt: dt.isoformat(),
            "insert_pending": insert_pending,
            "safe_add_reaction": AsyncMock(return_value=True),
            "PENDING_REACTION": "📝",
        }
        fn = load("track_ladder_post", ctx)
        capper = types.SimpleNamespace(name="PropKitchen", user_id=99)
        asyncio.run(fn(FakeMessage(), capper))
        asyncio.run(fn(FakeMessage(), capper))
        self.assertEqual(4, len(outputs))
        self.assertEqual(4, len(reactions))
        self.assertEqual(4, self.cur.execute("SELECT COUNT(*) FROM ladder_rungs").fetchone()[0])
        self.assertEqual(1, self.cur.execute("SELECT COUNT(*) FROM ladder_sources").fetchone()[0])
        self.assertFalse(warnings)

    def test_legacy_grading_path_kept_for_every_bot_rung(self):
        fn = next(node for node in TREE.body
                  if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "on_raw_reaction_add")
        body = ast.unparse(fn)
        self.assertIn("owner_user_id_for_message", body)
        self.assertIn("grade_pending", body)
        self.assertIn("regrade_bet", body)
        self.assertNotIn("ladder_source_exists", body)


class CompactRecapTests(unittest.TestCase):
    def setUp(self):
        node = next(x for x in TREE.body if isinstance(x, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "RECAP_MARKET_ABBREVIATIONS" for t in x.targets
        ))
        self.scope = {
            "re": re,
            "RECAP_MARKET_ABBREVIATIONS": ast.literal_eval(node.value),
            "normalize_space": lambda x: " ".join(x.split()),
            "display_bet_text": lambda content, market, max_len=190: content,
        }
        self.clean = load("compact_bet_description", self.scope)
        self.scope.update({
            "WIN_EMOJI": "✅", "LOSS_EMOJI": "❌", "PUSH_EMOJI": "➖",
            "VOID_EMOJI": "↩️",
            "WAGER_DFS": "DFS",
            "format_compact_units": lambda units, signed=False: (
                f"{units:+g}u" if signed else f"{units:g}u"
            ),
            "format_odds_display": lambda odds: odds,
        })
        self.fn = load("build_bet_result_line", self.scope)

    def test_strips_platform_broken_punctuation_and_abbreviates_market(self):
        content = "Karl Anthony-Towns o14.5 Rebounds+Assists ( Onyx |"
        self.assertEqual(
            "Karl Anthony-Towns o14.5 Reb+Ast", self.clean(content, "")
        )

    def test_simplified_straight_and_dfs_line(self):
        straight = (
            "2026-10-30", "PropKitchen", "win", 1., .74,
            "Karl Anthony-Towns o14.5 Rebounds+Assists ( Onyx |", "",
            "-135", "NBA", "https://discord.com/channels/1/2/123",
            "STRAIGHT", "Onyx", "2026-10-30",
        )
        line = self.fn(straight)
        self.assertIn("✅ 1u | Karl Anthony-Towns o14.5 Reb+Ast | -135 | +0.74u", line)
        self.assertNotIn("Onyx", line)
        self.assertNotIn("10/30", line)
        self.assertNotIn("( |", line)

        dfs = list(straight)
        dfs[10] = "DFS"
        dfs[11] = "Chalkboard"
        dfs[7] = "1.92x"
        result = self.fn(tuple(dfs))
        self.assertIn("1u | Chalkboard | 1.92x", result)
        self.assertNotIn("10/30", result)

    def test_format_title_uses_recap(self):
        s = SOURCE
        self.assertIn(" /  RECAP", s)
        self.assertNotIn('heading=f"{capper_name or \'VIP\'}  /  BET RESULTS"', s)


if __name__ == "__main__":
    unittest.main()
