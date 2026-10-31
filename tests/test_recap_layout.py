"""Regression checks for sport-first monthly recaps and multi-sport accounting.

These tests never import or start the Discord client.
"""
from __future__ import annotations

import ast
import copy
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sqlite3
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def load(name, scope):
    node = copy.deepcopy(next(
        node for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ))
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        node,
    ], type_ignores=[]))
    exec(compile(module, "<isolated recap function>", "exec"), scope)
    return scope[name]


class RecapLayoutTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cur = self.conn.cursor()
        self.cur.execute(
            """CREATE TABLE bets (
                graded_utc TEXT, bet_date TEXT, capper TEXT, result TEXT,
                risk_units REAL, net_units REAL, content TEXT, market TEXT,
                odds_text TEXT, sport TEXT, jump_url TEXT, wager_category TEXT,
                platform TEXT, sportsbook TEXT, guild_id INTEGER,
                channel_id INTEGER, message_id INTEGER
            )"""
        )
        examples = [
            ("2026-10-30T10:00:00", "2026-10-30", "PropKitchen", "win",
             1.0, 0.86, "🏈 :49ers: Brock Purdy OVER 18.5 Rush Yards",
             "", "-116", "NFL", "https://discord.com/channels/1/2/101",
             "STRAIGHT", "", "", 1, 2, 101),
            ("2026-10-30T11:00:00", "2026-10-04", "PropKitchen", "win",
             0.3, 0.33, "🥣 PrizePicks taco Tuesday slip :49ers:",
             "", "2.1x", "NBA", "https://discord.com/channels/1/2/102",
             "DFS", "PrizePicks", "", 1, 2, 102),
            ("2026-10-30T12:00:00", "2026-10-30", "PropKitchen", "loss",
             1.0, -1.0, "Scottie Barnes OVER 20.5 Points",
             "", "-110", "NBA", "https://discord.com/channels/1/2/103",
             "STRAIGHT", "", "", 1, 2, 103),
            ("2026-10-30T13:00:00", "2026-10-30", "PropKitchen", "win",
             0.25, 0.25, "Chalkboard NBA NFL combo", "",
             "2x", "MIXED", "https://discord.com/channels/1/2/104",
             "DFS", "Chalkboard", "", 1, 2, 104),
            ("2026-10-30T14:00:00", "2026-10-30", "Other", "win",
             100.0, 200.0, "SECRET OTHER SERVER", "",
             "+200", "NFL", "https://discord.com/channels/2/2/105",
             "STRAIGHT", "", "", 2, 2, 105),
        ]
        self.cur.executemany("INSERT INTO bets VALUES (" + ",".join("?" for _ in range(17)) + ")", examples)
        self.conn.commit()
        scope = {
            "cur": self.cur,
            "WAGER_CATEGORY_SQL": "wager_category",
            "WAGER_STRAIGHT": "STRAIGHT",
            "WAGER_DFS": "DFS",
            "WAGER_PARLAY": "SPORTSBOOK_PARLAY",
            "WIN_EMOJI": "✅", "LOSS_EMOJI": "❌",
            "PUSH_EMOJI": "➖", "VOID_EMOJI": "↩️",
            "re": re, "date": date,
            "normalize_space": lambda x: " ".join(x.split()),
            "display_bet_text": lambda content, market, max_len=175: content or market,
            "local_date_from_utc_iso": lambda x: datetime.fromisoformat(x).date(),
        }
        for name in (
            "format_compact_number", "format_compact_units", "format_odds_display",
            "format_recap_wager_date", "compact_bet_description", "build_bet_result_line",
            "format_record", "fetch_filtered_totals", "stats_values",
            "fetch_group_breakdown", "fetch_recent_bets", "wager_category_label",
            "recap_sport_order", "build_filtered_summary_text",
        ):
            load(name, scope)
        scope["SPORT_RECAP_ORDER"] = (
            "NBA", "WNBA", "NFL", "NCAAF", "MLB", "NHL", "NCAAB",
            "SOCCER", "TENNIS", "GOLF", "UFC", "MMA", "MIXED", "UNKNOWN",
        )
        self.fn = scope["build_filtered_summary_text"]

    def tearDown(self):
        self.conn.close()

    def test_sport_first_layout_and_clean_dfs_lines(self):
        report = self.fn(
            "Capper: PropKitchen", "guild_id = ? AND LOWER(capper) = ?",
            (1, "propkitchen"), capper_name="PropKitchen", period_label="October 2026"
        )
        self.assertIn("**NBA**", report)
        self.assertIn("**NFL**", report)
        self.assertIn("**MIXED — Multi-Sport Slips**", report)
        self.assertLess(report.index("**NBA**"), report.index("**NFL**"))
        self.assertLess(report.index("**NFL**"), report.index("**MIXED — Multi-Sport Slips**"))
        self.assertIn("10/4 | 0.3u | PrizePicks | 2.1x | +0.33u | [jump]", report)
        self.assertIn("Brock Purdy o18.5 Rush Yards", report)
        self.assertNotIn("🥣", report)
        self.assertNotIn(":49ers:", report)
        self.assertNotIn("SECRET OTHER SERVER", report)

    def test_mixed_payout_counted_once(self):
        report = self.fn(
            "Capper", "guild_id = ? AND LOWER(capper) = ?",
            (1, "propkitchen"), capper_name="PropKitchen", period_label="October 2026"
        )
        self.assertIn("4 bets", report)
        self.assertIn("Net: **+0.44u**", report)
        mixed = report.split("**MIXED — Multi-Sport Slips**", 1)[1]
        self.assertIn("0.25u | Chalkboard | 2x | +0.25u", mixed)
        nba = report.split("**NBA**", 1)[1].split("**NFL**", 1)[0]
        nfl = report.split("**NFL**", 1)[1].split("**MIXED", 1)[0]
        self.assertNotIn("Chalkboard", nba)
        self.assertNotIn("Chalkboard", nfl)

    def test_second_page_uses_full_totals(self):
        for i in range(80):
            self.cur.execute(
                "INSERT INTO bets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"2026-10-31T12:{i % 60:02d}:00", "2026-10-31", "PropKitchen",
                 "win", 1.0, 1.0, f"Additional wager #{i}", "", "+100",
                 "NBA", "", "STRAIGHT", "", "", 1, 2, 1000 + i),
            )
        self.conn.commit()
        report = self.fn(
            "Capper", "guild_id = ? AND LOWER(capper) = ?",
            (1, "propkitchen"), capper_name="PropKitchen",
            period_label="October 2026", page=2
        )
        self.assertIn("page 2/2", report)
        self.assertIn("84 bets", report)
        self.assertIn("Net: **+80.44u**", report)
        self.assertNotIn("SECRET OTHER SERVER", report)


class ExplicitMixedTests(unittest.TestCase):
    def test_written_mixed_dfs_is_not_unknown(self):
        scope = {
            "infer_platform": lambda x: ("PrizePicks", "DFS_APP"),
            "infer_bet_type": lambda x: "",
            "infer_wager_category": lambda *args: "DFS",
            "infer_sports": lambda x: [],
            "infer_league": lambda text, sport: sport,
            "parse_multiplier_value": lambda odds: 2.1,
            "parse_player": lambda text: "",
            "parse_market": lambda text: "",
            "parse_line": lambda text: "",
            "infer_odds_format": lambda x: "multiplier",
            "WAGER_DFS": "DFS", "WAGER_PARLAY": "SPORTSBOOK_PARLAY",
            "re": re,
        }
        parser = load("parse_analytics_fields", scope)
        fields = parser("PrizePicks | MIXED | 0.3u | 2.1x", "2.1x")
        self.assertEqual("MIXED", fields["sport"])
        self.assertEqual("MIXED", fields["league"])

    def test_written_mixed_parlay_is_not_unknown(self):
        scope = {
            "infer_platform": lambda x: ("", ""),
            "infer_bet_type": lambda x: "Parlay",
            "infer_wager_category": lambda *args: "SPORTSBOOK_PARLAY",
            "infer_sports": lambda x: [],
            "infer_league": lambda text, sport: sport,
            "parse_multiplier_value": lambda odds: None,
            "parse_player": lambda text: "",
            "parse_market": lambda text: "",
            "parse_line": lambda text: "",
            "infer_odds_format": lambda x: "american",
            "WAGER_DFS": "DFS", "WAGER_PARLAY": "SPORTSBOOK_PARLAY",
            "re": re,
        }
        parser = load("parse_analytics_fields", scope)
        self.assertEqual("MIXED", parser("MIXED parlay +225", "+225")["sport"])


if __name__ == "__main__":
    unittest.main()
