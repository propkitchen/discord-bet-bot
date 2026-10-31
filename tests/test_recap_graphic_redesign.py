"""Regression coverage for clean multi-day social cards and true parlay categories."""
from __future__ import annotations

import ast
import copy
import re
import sqlite3
from pathlib import Path
import struct
import unittest

from bettracker_visuals import generate_play_recap_card

SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text()
TREE = ast.parse(SOURCE)


def load(name, values):
    fn = copy.deepcopy(next(
        n for n in TREE.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    ))
    fn.decorator_list = []
    mod = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn,
    ], type_ignores=[]))
    exec(compile(mod, "<recap regression>", "exec"), values)
    return values[name]


class CategoryTest(unittest.TestCase):
    def test_new_lotto_is_parlay_but_single_double_double_is_straight(self):
        scope = {
            "WAGER_DFS": "DFS",
            "WAGER_STRAIGHT": "STRAIGHT",
            "WAGER_PARLAY": "SPORTSBOOK_PARLAY",
            "re": re,
        }
        fn = load("infer_wager_category", scope)
        self.assertEqual("SPORTSBOOK_PARLAY", fn(
            ".1u NBA Double Double Lotto +1224", "+1224", "SPORTSBOOK", ""
        ))
        self.assertEqual("SPORTSBOOK_PARLAY", fn(
            ".1u NBA Double Double Lotto +1224", "+1224", "", ""
        ))
        self.assertEqual("STRAIGHT", fn(
            "1u Jokic to record a double double (-120)", "-120", "SPORTSBOOK", "Points"
        ))
        self.assertEqual("DFS", fn(
            "PrizePicks Lotto 2x", "2x", "DFS_APP", ""
        ))

    def test_old_straight_lotto_is_reclassified_at_query_time_without_db_changes(self):
        assignment = next(n for n in TREE.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "WAGER_CATEGORY_SQL" for t in n.targets
        ))
        sql = ast.literal_eval(assignment.value)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE bets (wager_category TEXT, platform_type TEXT, odds_text TEXT, bet_type TEXT, content TEXT)")
        cases = [
            ("STRAIGHT", "SPORTSBOOK", "+1224", "", ".1u NBA Double Double Lotto"),
            ("STRAIGHT", "SPORTSBOOK", "-120", "Points", "Nikola Jokic double double"),
            ("STRAIGHT", "DFS_APP", "2.1x", "", "Chalkboard Lotto"),
            ("SPORTSBOOK_PARLAY", "SPORTSBOOK", "+245", "Parlay", "2 leg SGP"),
        ]
        db.executemany("INSERT INTO bets VALUES (?, ?, ?, ?, ?)", cases)
        self.assertEqual(
            ["SPORTSBOOK_PARLAY", "STRAIGHT", "DFS", "SPORTSBOOK_PARLAY"],
            [r[0] for r in db.execute(f"SELECT {sql} FROM bets ORDER BY rowid")],
        )
        self.assertEqual("STRAIGHT", db.execute("SELECT wager_category FROM bets LIMIT 1").fetchone()[0])
        db.close()


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE bets (guild_id INTEGER, bet_date TEXT, graded_utc TEXT, sport TEXT)")
        self.scope = {"cur": self.conn.cursor()}
        self.fn = load("social_recap_eligible", self.scope)

    def tearDown(self):
        self.conn.close()

    def insert(self, guild, day, sport, count):
        self.conn.executemany(
            "INSERT INTO bets VALUES (?, ?, ?, ?)",
            [(guild, day, day + "T12:00:00", sport)] * count
        )

    def test_single_day_nba_report_has_no_extra_image(self):
        self.insert(1, "2026-10-30", "NBA", 8)
        self.assertFalse(self.fn("guild_id = ?", (1,)))

    def test_six_bets_multi_sport_or_multi_day_get_image(self):
        self.insert(1, "2026-10-30", "NBA", 4)
        self.insert(1, "2026-10-30", "NFL", 2)
        self.assertTrue(self.fn("guild_id = ?", (1,)))
        self.conn.execute("DELETE FROM bets")
        self.insert(1, "2026-10-29", "NBA", 3)
        self.insert(1, "2026-10-30", "NBA", 3)
        self.assertTrue(self.fn("guild_id = ?", (1,)))

    def test_other_guild_cannot_make_a_single_sport_daily_report_qualify(self):
        self.insert(1, "2026-10-30", "NBA", 8)
        self.insert(2, "2026-10-31", "NFL", 100)
        self.assertFalse(self.fn("guild_id = ?", (1,)))
        self.assertTrue(self.fn("guild_id = ?", (2,)) is False)  # still only one day/sport

    def test_sport_emojis_are_specific(self):
        scope = {"SPORT_RECAP_EMOJI": {
            "NBA": "🏀", "WNBA": "🏀", "NCAAB": "🏀",
            "NFL": "🏈", "NCAAF": "🏈", "MLB": "⚾",
        }}
        fn = load("sport_recap_emoji", scope)
        self.assertEqual("🏀", fn("NBA"))
        self.assertEqual("🏈", fn("NFL"))
        self.assertEqual("⚾", fn("MLB"))
        self.assertNotEqual("🏟️", fn("NBA"))


class ShareableImageTests(unittest.TestCase):
    def test_card_is_4_by_5_png_with_real_plays(self):
        summary = {"wins": 5, "losses": 3, "net": 1.99, "roi": 34.0, "total": 8}
        plays = [
            ("win", "Scottie Barnes over 20.5 Points"),
            ("loss", "Brandon Miller over 33.5 Points+Rebounds"),
            ("win", "Karl-Anthony Towns over 14.5 Rebounds+Assists"),
        ]
        png = generate_play_recap_card("PropKitchen", summary, plays, "October 2026 · NBA", 8)
        data = png.getvalue()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", data[16:24])
        self.assertAlmostEqual(width / height, .8, delta=.01)
        self.assertGreater(len(data), 10000)

    def test_eighteen_plays_fit_with_honest_showing_count(self):
        summary = {"wins": 15, "losses": 5, "net": 6.0, "roi": 25.0, "total": 20}
        lines = [("win", f"Player {i} over 20.5 Points") for i in range(20)]
        png = generate_play_recap_card("PropKitchen", summary, lines, "October 2026 · NBA", 20)
        self.assertGreater(len(png.getvalue()), 10000)

    def test_simple_daily_report_is_not_forced_to_generate_image(self):
        fn = next(x for x in TREE.body
                  if isinstance(x, ast.AsyncFunctionDef) and x.name == "post_filtered_summary")
        body = ast.unparse(fn)
        self.assertIn("if capper_name and page == 1", body)
        self.assertIn("send_optional_social_recap", body)
        self.assertNotIn("generate_recap_card(", body)


if __name__ == "__main__":
    unittest.main()
