"""Season registry and financial reconciliation regressions; no bot credentials."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bettracker_seasons import (
    registry, parse_season_query, membership_exists_sql, as_api_metadata,
)
from tools.season_financial_audit import (
    analyze_rows, audit_database, signature, exact_totals,
)


class CompetitionRegistryTests(unittest.TestCase):
    def test_every_sport_has_a_canonical_competition(self):
        sports = {c["sport"] for c in registry()["competitions"]}
        required = {"NBA","WNBA","NFL","NCAAF","NCAAB","NCAAW","MLB",
                    "NHL","SOCCER","TENNIS","NCAA_TENNIS","GOLF","UFC",
                    "MMA","BOXING","MOTORSPORTS","ESPORTS"}
        self.assertTrue(required.issubset(sports))
        self.assertGreaterEqual(len(registry()["competitions"]), 90)

    def test_nba_aliases_preseason_separation_and_incomplete_final(self):
        for text in ("26-27 NBA Season","2026-27 NBA Season",
                     "2026-2027 NBA Season","NBA 2026 season"):
            with self.subTest(text=text):
                q = parse_season_query(text)
                self.assertIsNotNone(q)
                self.assertEqual("2026-27", q.season_id)
                self.assertEqual(("regular", "play_in", "playoffs"), q.included_phases)
                self.assertNotIn("preseason", q.included_phases)
                self.assertEqual(date(2026, 10, 20), q.date_start)
                self.assertIsNone(q.date_end)
                self.assertFalse(q.calendar_complete)
        q = parse_season_query("26-27 NBA Preseason")
        self.assertEqual(("preseason",), q.included_phases)
        self.assertIsNone(q.date_start)
        self.assertEqual(("regular",), parse_season_query(
            "26-27 NBA Regular Season").included_phases)
        self.assertEqual(("play_in",), parse_season_query(
            "26-27 NBA Play-In").included_phases)
        self.assertEqual(("playoffs",), parse_season_query(
            "26-27 NBA Playoffs").included_phases)

    def test_nfl_canonical_2026_and_verified_postseason_boundaries(self):
        for text in ("26-27 NFL Season","2026 NFL Season",
                     "2026-2027 NFL Season"):
            q = parse_season_query(text)
            self.assertEqual("2026", q.season_id)
            self.assertEqual(date(2026,9,9), q.date_start)
            self.assertEqual(date(2027,2,14), q.date_end)
            self.assertTrue(q.calendar_complete)
            self.assertNotIn("preseason", q.included_phases)
        q = parse_season_query("26-27 NFL Playoffs")
        self.assertEqual("playoffs", q.phase)
        self.assertEqual(date(2027,1,16), q.date_start)
        self.assertEqual(date(2027,2,14), q.date_end)
        self.assertEqual(date(2027,1,10), parse_season_query(
            "2026 NFL Regular Season").date_end)

    def test_soccer_competitions_do_not_collapse(self):
        queries = [
            ("26-27 EPL", "EPL"),
            ("26-27 UCL", "UCL"),
            ("26-27 UCL Knockout Stage", "UCL"),
            ("2026 World Cup", "FIFA_WORLD_CUP"),
            ("2027 Womens World Cup", "FIFA_WOMENS_WORLD_CUP"),
            ("2026 World Cup Qualifiers", "WORLD_CUP_QUALIFIERS"),
            ("26-27 FA Cup", "FA_CUP"),
            ("26-27 Europa League", "UEL"),
            ("26-27 Copa del Rey", "COPA_DEL_REY"),
        ]
        for text, expected in queries:
            with self.subTest(text=text):
                self.assertEqual(expected, parse_season_query(text).competition_id)
        ko = parse_season_query("26-27 UCL Knockout Stage")
        self.assertEqual("knockout", ko.phase)
        self.assertEqual(date(2027, 2, 16), ko.date_start)
        self.assertEqual(date(2027, 6, 5), ko.date_end)
        self.assertNotEqual(parse_season_query("2026 World Cup Qualifiers").competition_id,
                            parse_season_query("2026 World Cup").competition_id)

    def test_mls_sprint_separate_and_ambiguous_2027_rejected(self):
        sprint = parse_season_query("2027 MLS Sprint Season")
        self.assertEqual("2027-sprint", sprint.season_id)
        self.assertEqual(date(2027,2,19), sprint.date_start)
        self.assertFalse(sprint.calendar_complete)
        self.assertIsNone(parse_season_query("2027 MLS"))
        self.assertEqual("2027-28", parse_season_query("2027-28 MLS").season_id)
        self.assertNotEqual(sprint.season_id, parse_season_query("2027-28 MLS").season_id)

    def test_tennis_grand_slams_tours_and_lower_tiers(self):
        for value, expected in (
            ("gr8 Tennis 2026", None),
            ("Tennis 2026", "TENNIS_ALL"),
            ("ATP 2026", "ATP_TOUR"),
            ("WTA 2026", "WTA_TOUR"),
            ("Wimbledon 2026", "WIMBLEDON"),
            ("ATP Challenger 2026", "ATP_CHALLENGER"),
            ("Tennis Grand Slams 2026", "GRAND_SLAMS"),
            ("ITF M15 2026", "ITF_M15"),
            ("Billie Jean King Cup 2026", "BILLIE_JEAN_KING_CUP"),
            ("NCAATennis 2026", "NCAA_TENNIS"),
        ):
            with self.subTest(value=value):
                q = parse_season_query(value)
                self.assertEqual(expected, q.competition_id if q else None)
        grand = parse_season_query("Tennis Grand Slams 2026")
        self.assertEqual(4, len(grand.competition_ids))
        self.assertIn("WIMBLEDON", grand.competition_ids)
        all_tennis = parse_season_query("Tennis 2026")
        self.assertIn("ATP_CHALLENGER", all_tennis.competition_ids)
        self.assertIn("ITF_M25", all_tennis.competition_ids)
        self.assertIn("WIMBLEDON", all_tennis.competition_ids)

    def test_other_sports_and_conservative_legacy_paths(self):
        self.assertEqual("spring_training", parse_season_query(
            "MLB 2026 Spring Training").phase)
        self.assertEqual("postseason", parse_season_query("MLB 2026 Postseason").phase)
        self.assertEqual("NCAAW", parse_season_query("NCAAW 26-27").sport)
        self.assertEqual("NCAAF", parse_season_query("2026 CFB").sport)
        self.assertEqual("NHL", parse_season_query("26-27 NHL Preseason").sport)
        self.assertEqual("F1", parse_season_query("2026 Formula 1").competition_id)
        self.assertEqual("VALORANT", parse_season_query("Valorant 2026").competition_id)
        self.assertEqual("ESPORTS_ALL", parse_season_query("Esports 2026").competition_id)
        self.assertIn("VALORANT", parse_season_query("Esports 2026").competition_ids)
        self.assertEqual("MOTORSPORTS_ALL", parse_season_query("Motorsports 2026").competition_id)
        self.assertEqual("GOLF_ALL", parse_season_query("Golf 2026").competition_id)
        for legacy in ("yesterday", "last 14 days", "2026-07", "2026-07-09",
                       "NBA yesterday", "July 2026"):
            self.assertIsNone(parse_season_query(legacy), legacy)
        q = parse_season_query("26-27 NFL Season breakdown")
        self.assertTrue(q.breakdown)
        self.assertEqual("NFL", as_api_metadata(q)["competition_id"])

    def test_future_calendar_not_silently_fabricated(self):
        self.assertIsNone(parse_season_query("2027 MLB").date_start)
        worldcup = parse_season_query("2026 World Cup")
        self.assertEqual(date(2026, 6, 11), worldcup.date_start)
        self.assertEqual(date(2026, 7, 19), worldcup.date_end)
        womens = parse_season_query("2027 Womens World Cup")
        self.assertEqual(date(2027, 6, 24), womens.date_start)
        self.assertEqual(date(2027, 7, 25), womens.date_end)
        self.assertIsNone(parse_season_query("2027 World Cup Qualifiers").date_start)
        self.assertIsNone(parse_season_query("2027 NHL 2027-28"))
        for window in registry()["windows"]:
            self.assertTrue(window["source_url"].startswith("https://"))


class MembershipPartitionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
        CREATE TABLE bets (guild_id INTEGER, message_id INTEGER, sport TEXT,
                           risk_units REAL, net_units REAL);
        CREATE TABLE wager_season_memberships (
            guild_id INTEGER, message_id INTEGER, competition_id TEXT,
            season_id TEXT, phase TEXT);
        INSERT INTO bets VALUES (1, 101, 'NBA', 1, 1);
        INSERT INTO bets VALUES (1, 102, 'NBA', 1, -1);
        INSERT INTO bets VALUES (1, 103, 'NBA', 2, 1);
        INSERT INTO bets VALUES (2, 201, 'NBA', 100, 100);
        INSERT INTO wager_season_memberships VALUES (1, 101, 'NBA', '2026-27', 'regular');
        INSERT INTO wager_season_memberships VALUES (1, 101, 'NBA_CUP', '2026-27', 'group');
        INSERT INTO wager_season_memberships VALUES (1, 102, 'NBA', '2026-27', 'playoffs');
        INSERT INTO wager_season_memberships VALUES (2, 201, 'NBA', '2026-27', 'regular');
        """)

    def tearDown(self):
        self.db.close()

    def test_full_season_membership_dedup_and_unclassified_partition(self):
        q = parse_season_query("2026-27 NBA Season")
        where, args = membership_exists_sql(q, "b")
        full = self.db.execute(
            f"SELECT COUNT(*), SUM(net_units) FROM bets b WHERE guild_id=1 AND {where}",
            args,
        ).fetchone()
        unc = self.db.execute(
            f"SELECT COUNT(*), SUM(net_units) FROM bets b WHERE guild_id=1 AND NOT ({where})",
            args,
        ).fetchone()
        self.assertEqual((2, 0.0), full)
        self.assertEqual((1, 1.0), unc)
        self.assertEqual(3, full[0] + unc[0])
        self.assertEqual(1.0, full[1] + unc[1])
        # Secondary NBA Cup membership does not duplicate financial records.
        joined = self.db.execute(
            "SELECT COUNT(*) FROM bets b JOIN wager_season_memberships sm "
            "ON b.guild_id=sm.guild_id AND b.message_id=sm.message_id "
            "WHERE b.guild_id=1 AND b.message_id=101"
        ).fetchone()[0]
        self.assertEqual(2, joined)
        self.assertEqual(1, self.db.execute(
            f"SELECT COUNT(*) FROM bets b WHERE guild_id=1 AND b.message_id=101 AND {where}", args
        ).fetchone()[0])

    def test_preseason_excluded_and_cross_guild_protected(self):
        q = parse_season_query("2026-27 NBA Preseason")
        where, args = membership_exists_sql(q)
        self.assertEqual(0, self.db.execute(
            f"SELECT COUNT(*) FROM bets b WHERE b.guild_id=1 AND {where}", args
        ).fetchone()[0])
        all_q = parse_season_query("2026-27 NBA Season")
        sql, args = membership_exists_sql(all_q)
        self.assertEqual(2, self.db.execute(
            f"SELECT COUNT(*) FROM bets b WHERE b.guild_id=1 AND {sql}", args
        ).fetchone()[0])
        with self.assertRaises(ValueError):
            membership_exists_sql(all_q, "b;DROP TABLE bets")


def row(mid, net, risk=1, category="STRAIGHT", sport="NBA", result="win",
        odds="-110", content="NBA Example player 20+ Points", guild=1):
    return {
        "id": mid, "message_id": mid, "guild_id": guild,
        "capper": "PropKitchen", "sport": sport,
        "risk_units": risk, "net_units": net,
        "odds_text": odds, "result": result,
        "wager_category": category, "platform_type": "SPORTSBOOK",
        "platform": "DraftKings", "bet_type": "Straight",
        "created_utc": "2026-11-10T15:00:00Z",
        "graded_utc": "2026-11-10T23:00:00Z",
        "bet_date": "2026-11-10",
        "content": content, "jump_url": f"https://discord.com/channels/{guild}/2/{mid}",
    }


class FinancialAuditTests(unittest.TestCase):
    def test_exact_totals_and_issues_are_nonmutating(self):
        rows = [
            row(1, .91),
            row(2, -1, result="loss"),
            row(3, 20, risk=10, odds="+1000"),
            row(4, 1, category="STRAIGHT", odds="2x",
                content="NBA Chalkboard Power", result="win"),
        ]
        saved = json.dumps(rows, sort_keys=True)
        before = signature(rows)
        result = analyze_rows(rows)
        self.assertEqual("audit_only_no_changes", result["status"])
        self.assertEqual(4, result["totals"]["bets"])
        self.assertTrue(any(i["message_id"] == "3" and
                            i["code"] == "large_risk_review" for i in result["issues"]))
        self.assertTrue(any(i["message_id"] == "4" and
                            i["code"] == "wager_category_mismatch" for i in result["issues"]))
        self.assertEqual(saved, json.dumps(rows, sort_keys=True))
        self.assertEqual(before, signature(rows))

    def test_duplicate_and_cross_sport_are_flagged_without_changes(self):
        a = row(11, 1, content="NFL Patrick Mahomes 2+ TD", sport="NBA")
        b = row(12, 1, content="NFL Patrick Mahomes 2+ TD", sport="NBA")
        report = analyze_rows([a, b])
        self.assertEqual(2, report["totals"]["bets"])
        self.assertEqual(2, report["issue_counts"]["potential_duplicate"])
        self.assertEqual(2, report["issue_counts"]["explicit_sport_conflict"])

    def test_read_only_sqlite_report_and_baseline_signature(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "copy.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE bets (
                    id INTEGER, message_id INTEGER, guild_id INTEGER,
                    capper TEXT, sport TEXT, risk_units REAL, net_units REAL,
                    odds_text TEXT, result TEXT, wager_category TEXT,
                    platform_type TEXT, platform TEXT, bet_type TEXT,
                    created_utc TEXT, graded_utc TEXT, bet_date TEXT,
                    content TEXT, jump_url TEXT
                );
            """)
            r = row(1, .91)
            db.execute(
                "INSERT INTO bets VALUES (" + ",".join("?" for _ in r) + ")",
                tuple(r.values()),
            )
            db.commit()
            db.close()
            first = audit_database(path)
            second = audit_database(path)
            self.assertEqual(first["signature"], second["signature"])
            self.assertEqual("ok", first["sqlite_integrity_check"])
            self.assertEqual(1, first["totals"]["bets"])
            self.assertEqual(1, first["breakdown"]["by_sport"]["NBA"]["bets"])


if __name__ == "__main__":
    unittest.main()
