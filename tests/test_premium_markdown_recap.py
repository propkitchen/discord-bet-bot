"""Isolated regression tests for premium Discord wager text and old recaps."""
from __future__ import annotations

import ast
import copy
import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def load_parsers():
    names = [
        "RE_UNITS", "RE_AMERICAN_PAREN", "RE_AMERICAN", "RE_MULT",
        "RE_DECIMAL_CONTEXT", "RE_DECIMAL_SUFFIX", "RE_ISO_DATE",
        "RE_SLASH_DATE", "RECAP_MARKET_ABBREVIATIONS",
        "normalize_space", "_remove_odds_and_dates", "_clean_description_fragment",
        "_description_is_meaningful", "_finalize_description",
        "extract_bet_description", "display_bet_text", "compact_bet_description",
    ]
    nodes = []
    for name in names:
        match = next(
            n for n in TREE.body
            if (
                isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
            ) or (
                isinstance(n, ast.FunctionDef) and n.name == name
            )
        )
        nodes.append(copy.deepcopy(match))
    module = ast.fix_missing_locations(
        ast.Module(body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *nodes,
        ], type_ignores=[])
    )
    scope = {"re": re}
    exec(compile(module, "<isolated premium parser>", "exec"), scope)
    return scope


class PremiumFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = load_parsers()

    def test_cedric_coward_recap_shows_player_not_fanatics(self):
        content = (
            "## NBA Prop #5 :basketball: (11/4)\n\n"
            ":MEMGrizzlies: **Cedric Coward OVER 6.5 Rebounds**\n"
            "1u | -130 Fanatics :fanatics:\n\n"
            "@VIP Members @VIP MEMBERS (WINIBLE) @PK Notification"
        )
        report = self.parser["compact_bet_description"](content, "")
        self.assertIn("Cedric Coward o6.5 Reb", report)
        self.assertNotIn("Fanatics", report)
        self.assertNotIn("*", report)
        self.assertNotIn(":MEMGrizzlies:", report)

    def test_anthony_davis_recap_shows_player_not_draftkings(self):
        content = (
            "## NBA Prop #6 :basketball: (11/4)\n\n"
            ":WashingtonWizards: **ANTHONY DAVIS OVER 23.5 POINTS**\n\n"
            "***1u*** | **-107 DraftKings :draftkings:**\n\n"
            "@VIP Members @VIP MEMBERS (WINIBLE) @PK Notification"
        )
        report = self.parser["compact_bet_description"](content, "")
        self.assertIn("ANTHONY DAVIS o23.5 Pts", report)
        self.assertNotIn("DraftKings", report)
        self.assertNotIn("*", report)

    def test_jordan_love_alternate_passing_yards_not_bookmaker(self):
        # Real-world Discord format: selection above a separate stake line.
        content = (
            "NFL Sunday Prop #1 🏈 (11/15)\\n\\n"
            "🏈 Jordan Love 250+ Passing Yards\\n\\n"
            "Collab w/ @TheeGr8 🤝\\n\\n"
            "1.25u | -123 DraftKings\\n\\n"
            "Love has exceeded 250 passing yards in six games."
        )
        description = self.parser["compact_bet_description"](content, "")
        self.assertEqual("Jordan Love 250+ Pass Yds", description)
        self.assertNotIn("DraftKings", description)

    def test_brock_purdy_alternate_rushing_yards_not_bookmaker(self):
        content = (
            "NFL Sunday Prop #2 🏈 (11/15)\\n\\n"
            "Brock Purdy 20+ Rushing Yards\\n\\n"
            "1u | -110 DraftKings\\n\\n"
            "Purdy has reached 20 rushing yards repeatedly."
        )
        description = self.parser["compact_bet_description"](content, "")
        self.assertEqual("Brock Purdy 20+ Rush Yds", description)
        self.assertNotIn("DraftKings", description)

    def test_alternate_receiving_yards_and_abbreviated_markets(self):
        examples = (
            ("CeeDee Lamb 35+ Receiving Yards", "CeeDee Lamb 35+ Rec Yds"),
            ("Jordan Love 250+ Pass Yds", "Jordan Love 250+ Pass Yds"),
            ("Brock Purdy 20+ Rush Yds", "Brock Purdy 20+ Rush Yds"),
        )
        for selection, expected in examples:
            with self.subTest(selection=selection):
                content = f"NFL Sunday Prop\\n{selection}\\n1u | -115 DraftKings"
                self.assertEqual(
                    expected, self.parser["compact_bet_description"](content, "")
                )

    def test_existing_over_under_formats_remain_intact(self):
        examples = (
            ("Cameron Boozer OVER 14.5 Rebounds+Assists", "Cameron Boozer o14.5 Reb+Ast"),
            ("Dylan Harper UNDER 16.5 Points", "Dylan Harper u16.5 Pts"),
            ("Jaylen Warren o19.5 Receiving Yards", "Jaylen Warren o19.5 Rec Yds"),
        )
        for selection, expected in examples:
            with self.subTest(selection=selection):
                content = f"NBA Prop #1\\n{selection}\\n1u | -111 DraftKings"
                self.assertEqual(
                    expected, self.parser["compact_bet_description"](content, "")
                )

    def test_same_line_wager_still_wins_over_heading(self):
        content = (
            "## NBA Prop #3\n"
            "1u | Jalen Brunson OVER 28.5 Points (-110 FanDuel)\n"
        )
        description = self.parser["extract_bet_description"](content)
        self.assertIn("Jalen Brunson OVER 28.5 Points", description)
        self.assertNotIn("NBA Prop", description)

    def test_non_player_bet_falls_back_without_losing_details(self):
        content = "## NFL Parlay\n1u | -110 FanDuel"
        description = self.parser["extract_bet_description"](content)
        self.assertTrue(description)


if __name__ == "__main__":
    unittest.main()
