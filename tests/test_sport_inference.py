"""Tests for sport clues from real Discord betting language.

No Discord token, database, or network required.
"""
import ast
from pathlib import Path
import re
import unittest

from bettracker_sports import infer_sports_from_text


class SportRecognitionTests(unittest.TestCase):
    def test_nfl_broadcasts_and_championships(self):
        for phrase in (
            "TNF Prop #1 🏈", "MNF 1u Brock Purdy o18.5 (-116)",
            "SNF Prop #2", "Sunday Night Football", "Monday Night Football",
            "Thursday Night Football", "Thuraday Night Football",
            "Super Bowl MVP", "NFC Championship ML", "AFC Championship",
            "AFC/NFC Championship", "AFC East futures", "NFC", "AFC",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(["NFL"], infer_sports_from_text(phrase))

    def test_nba_and_wnba_tournament_terms(self):
        for phrase in (
            "NBA Cup Lakers ML", "In-Season Tournament", "ECF MVP",
            "WCF Celtics", "Eastern Conference Finals",
            "Western Conference Final", "🏀 1.2u Knicks win ECF (-120)",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(["NBA"], infer_sports_from_text(phrase))
        self.assertEqual(["WNBA"], infer_sports_from_text("WNBA Caitlin Clark 25+ 🏀"))

    def test_tennis_mlb_soccer_golf_and_cbb_cfb(self):
        examples = {
            "🎾 WTA Prop #1": "TENNIS",
            "ATP Carlos Alcaraz": "TENNIS",
            "🏀 1u Iowa ML (-110)": "NCAAB",
            "🏈 1u Iowa ML (-110)": "NCAAF",
            "🏈 LSU vs Alabama": "NCAAF",
            "🏀 Duke ML": "NCAAB",
            "CFB Prop #2": "NCAAF",
            "CBB Duke 1u": "NCAAB",
            "ALCS Game 5": "MLB",
            "World Series Dodgers": "MLB",
            "Erling Haaland to score 1u -110": "SOCCER",
            "Haaland Anytime Goal": "SOCCER",
            "Premier League Arsenal vs Chelsea": "SOCCER",
            "AFC Wimbledon ML": "SOCCER",
            "AFC Asian Cup": "SOCCER",
            "⚽ World Cup": "SOCCER",
            "Toronto Tempo ML": "WNBA",
            "Portland Fire ML": "WNBA",
            "UEFA Champions League": "SOCCER",
            "PGA golfer prop": "GOLF",
        }
        for phrase, expected in examples.items():
            with self.subTest(phrase=phrase):
                self.assertEqual([expected], infer_sports_from_text(phrase))

    def test_pro_teams_and_shared_names(self):
        examples = {
            "Knicks ML": "NBA",
            "Celtics spread": "NBA",
            "49ers ML": "NFL",
            "Commanders spread": "NFL",
            "Ravens receptions": "NFL",
            "Yankees ML": "MLB",
            "Mets ML": "MLB",
            "Golden Knights ML": "NHL",
            "Indiana Fever to win": "WNBA",
            "Liverpool vs Manchester City": "SOCCER",
            "🏀 Kings ML": "NBA",
            "🏒 Kings ML": "NHL",
            "🏈 Cardinals ML": "NFL",
            "⚾ Cardinals ML": "MLB",
            "🏈 Giants ML": "NFL",
            "⚾ Giants ML": "MLB",
            "⚾ 1u Texas Rangers ML": "MLB",
            "🏈 1u Texas Rangers ML": "MLB",
            "⚽ Spurs to win": "SOCCER",
            "🏀 Spurs to win": "NBA",
        }
        for phrase, expected in examples.items():
            with self.subTest(phrase=phrase):
                self.assertEqual([expected], infer_sports_from_text(phrase))

    def test_ambiguous_cases_are_not_guessed(self):
        for phrase in (
            "🏈", "🏀", "Iowa ML (-110)", "Cardinals -115",
            "Jets o2.5", "Kings spread", "Spurs ML",
            "Eastern Conference ML", "Western Conference", "FIBA World Cup",
            "1u ⛱️ My best bet",
        ):
            with self.subTest(phrase=phrase):
                result = infer_sports_from_text(phrase)
                self.assertFalse(any(x in {
                    "NFL", "NCAAF", "NBA", "WNBA", "NCAAB",
                    "SOCCER", "MLB", "NHL",
                } for x in result), result)

    def test_explicit_league_overrides_decorative_emoji(self):
        self.assertEqual(["NBA"], infer_sports_from_text("NBA 1u Knicks 🏈"))
        self.assertEqual(["WNBA"], infer_sports_from_text("WNBA 1u Aja Wilson 🏈"))
        self.assertEqual(["NFL"], infer_sports_from_text("NFL 1u Cardinals 🏀"))

    def test_multisport_inferred_without_double_accounting(self):
        self.assertEqual({"NBA", "NFL"}, set(
            infer_sports_from_text("NBA Celtics + NFL Chiefs 2x DFS")
        ))
        self.assertEqual({"NBA", "NFL"}, set(
            infer_sports_from_text("Knicks + Ravens 2x DFS")
        ))


class ParserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
        cls.syntax = ast.parse(source)

    def test_parser_calls_sport_inference(self):
        parser = next(n for n in self.syntax.body
                      if isinstance(n, ast.FunctionDef) and n.name == "infer_sports")
        self.assertIn("infer_sports_from_text", ast.unparse(parser))

    def test_unknown_or_generic_emoji_prompts_capper(self):
        target = next(n for n in self.syntax.body
                      if isinstance(n, ast.FunctionDef) and n.name == "format_warnings_for_tracked_post")
        module = ast.fix_missing_locations(ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), target],
            type_ignores=[]))
        def fields(sport):
            return {
                "sport": sport, "wager_category": "STRAIGHT",
                "platform": "", "market": "ML",
            }
        for sport, needs_help in [
            ("UNKNOWN", True), ("FOOTBALL", True), ("BASKETBALL", True),
            ("NFL", False), ("NBA", False), ("SOCCER", False),
        ]:
            with self.subTest(sport=sport):
                g = {
                    "parse_odds_text": lambda content: "-110",
                    "parse_analytics_fields": lambda content, odds: fields(sport),
                    "extract_bet_description": lambda c, market: "Iowa ML",
                    "WAGER_DFS": "DFS", "WAGER_STRAIGHT": "STRAIGHT",
                }
                exec(compile(module, "<warning format>", "exec"), g)
                messages = g["format_warnings_for_tracked_post"]("1u Iowa ML (-110)", "")
                self.assertEqual(needs_help, any("specific sport" in m for m in messages))

    def test_explicit_mixed_and_conflicting_straight_bets(self):
        target = next(n for n in self.syntax.body
                      if isinstance(n, ast.FunctionDef) and n.name == "parse_analytics_fields")
        module = ast.fix_missing_locations(ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), target],
            type_ignores=[]))
        scope = {
            "infer_platform": lambda t: ("", ""),
            "infer_bet_type": lambda t: "",
            "infer_wager_category": lambda *args: "DFS" if "2x" in args[0] else "STRAIGHT",
            "infer_sports": infer_sports_from_text,
            "infer_league": lambda text, sport: sport,
            "parse_multiplier_value": lambda t: None,
            "parse_player": lambda t: "", "parse_market": lambda t: "",
            "parse_line": lambda t: "", "infer_odds_format": lambda t: "american",
            "WAGER_DFS": "DFS", "WAGER_PARLAY": "SPORTSBOOK_PARLAY",
            "re": re,
        }
        exec(compile(module, "<analytics>", "exec"), scope)
        parser = scope["parse_analytics_fields"]
        self.assertEqual("MIXED", parser("NBA NFL PrizePicks 2x", "2x")["sport"])
        self.assertEqual("UNKNOWN", parser("Knicks Ravens straight -110", "-110")["sport"])
        self.assertEqual("NCAAF", parser("🏈 1u Iowa ML -110", "-110")["sport"])


if __name__ == "__main__":
    unittest.main()
