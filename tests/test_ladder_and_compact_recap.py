"""Regression tests for prop ladders, multi-day challenges and existing recaps."""
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

from bettracker_ladders import (
    parse_ladder_post, parse_challenge_day, is_challenge_day, is_ladder_post,
    is_multi_wager_post, has_multiple_staked_lines,
)

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
    exec(compile(mod, "<isolated ladder>", "exec"), scope)
    return scope[name]


class TwoLadderTypesTests(unittest.TestCase):
    PROP = (
        "NBA Luka Doncic Points Ladder\n"
        "Main Play: 0.8u Luka Doncic 30+ Points (-115 Novig)\n"
        "🪜.4u Luka Doncic 35+ Points (+120 DraftKings)\n"
        "🪜.2u Luka Doncic 40+ Points (+400 FanDuel)\n"
        "🪜.1u Luka Doncic 50+ Points (+750 Fanatics)\n"
        "🪜.05u Luka Doncic 60+ Points (+1500 BetMGM)"
    )
    BILLS = (
        "NFL Bills Team Total Ladder\n"
        "1.7u Buffalo Bills Team Total over 20.5 (-170)\n"
        "🪜 .8u Bills TT over 24.5 (-120)\n"
        ".5u Bills TT over 27.5 +150\n"
        ".35u over 56.5 Game Total (+250)\n"
        ".25u Bills TT over 31.5 points (+450)\n"
        ".20u Bills TT 37.5 Points +800\n"
        ".10u TT over 40+ Points+1000\n"
        ".10u TT 50+ Points +2000"
    )
    CHALLENGE = (
        "The Balls Out Bets Challenge 🪜 Day 1\n"
        "Goal: $25 to $500 in 6 bets\n"
        "1️⃣ $25 -> $50\n"
        "2️⃣ $50 -> $100 pocket $25\n"
        "3️⃣ $75 -> $150 pocket $50\n"
        "4️⃣ $100 -> $200 pocket $50\n"
        "1U | NFL | Bears +10.5 Caleb 180 passing yards "
        "Darnold 200 passing yards | odds +100 | 11/2"
    )

    def test_luka_stakes_and_odds_and_no_visible_reply_requirement(self):
        item = parse_ladder_post(self.PROP)
        self.assertEqual([.8, .4, .2, .1, .05], [x.risk for x in item.rungs])
        self.assertEqual(["-115", "+120", "+400", "+750", "+1500"], [x.odds for x in item.rungs])
        self.assertTrue(is_multi_wager_post(self.PROP))
        self.assertFalse(is_challenge_day(self.PROP))

    def test_bills_eight_staked_rungs_and_compact_odds(self):
        item = parse_ladder_post(self.BILLS)
        self.assertEqual(8, len(item.rungs))
        self.assertAlmostEqual(sum(x.risk for x in item.rungs), 4, places=7)
        self.assertEqual("+1000", item.rungs[6].odds)
        self.assertEqual("+2000", item.rungs[7].odds)
        self.assertAlmostEqual(item.rungs[2].risk, 0.5)

    def test_unmarked_ladder_with_3_staked_wagers(self):
        s = ("NBA Tyrese Haliburton\n"
             "1u Tyrese Haliburton over 7.5 Assists -150\n"
             ".3u Tyrese Haliburton 9+ Assists (+120)\n"
             ".2u Tyrese Haliburton Double Double (+250)")
        self.assertFalse(is_ladder_post(s))
        self.assertTrue(is_multi_wager_post(s))
        self.assertEqual(3, len(parse_ladder_post(s).rungs))

    def test_one_play_with_alternatives_is_not_multiple_bets(self):
        s = ("NBA Luka Doncic\n"
             "1u Luka Doncic over 24.5 Points (-110)\n"
             "You can also play 27.5 PRA or 26.5 PR\n"
             "I am fine with up to 25.5 Points")
        self.assertFalse(is_multi_wager_post(s))
        self.assertFalse(has_multiple_staked_lines(s))

    def test_bad_stake_or_missing_odds_reject_entire_rung_set(self):
        with self.assertRaises(ValueError):
            parse_ladder_post("NFL Ladder\n.5u 225+ Pass Yds (-110)\n.3u 250+ Pass Yds")
        with self.assertRaises(ValueError):
            parse_ladder_post("NFL Ladder\n.5u 225+ Pass Yds (-110)")
        with self.assertRaises(ValueError):
            parse_ladder_post("NFL Ladder\n.5u 225+ Pass Yds (-110)\n.5u 225+ Pass Yds (-110)")

    def test_day_by_day_ladder_is_single_wager(self):
        self.assertTrue(is_challenge_day(self.CHALLENGE))
        self.assertFalse(is_multi_wager_post(self.CHALLENGE))
        day = parse_challenge_day(self.CHALLENGE)
        self.assertIsNotNone(day)
        self.assertEqual(1, day.day)
        self.assertEqual(1, day.risk)
        self.assertEqual("+100", day.odds)

    def test_first_day_launch_without_day_label(self):
        post = (
            "The Balls Out Bets Challenge\n"
            "Goal: $25 -> $500 in 6 bets\n"
            "1️⃣ $25 -> $50\n"
            "2️⃣ $50 -> $100 pocket $25\n"
            "3️⃣ $75 -> $150 pocket $50\n"
            "1U | NFL | Bears +10.5 Caleb 180 pass yards | odds +100"
        )
        self.assertTrue(is_challenge_day(post))
        day = parse_challenge_day(post)
        self.assertEqual(0, day.day)
        self.assertEqual(1, day.risk)
        self.assertEqual("+100", day.odds)

    def test_day_2_and_4_with_pocketing_notes(self):
        for n, units in [(2, 2), (4, 4)]:
            x = f"🪜 Ladder Challenge Day {n}\n{units}u Aaron Judge 2+ Total Bases (2x)\nPocketed $25"
            day = parse_challenge_day(x)
            self.assertEqual(n, day.day)
            self.assertEqual(units, day.risk)
            self.assertEqual("2x", day.odds)

    def test_challenge_status_only_not_a_wager(self):
        self.assertIsNone(parse_challenge_day("🪜 Ladder Challenge Day 3\n$75 -> $150 pocket $50"))
        with self.assertRaises(ValueError):
            parse_ladder_post(self.CHALLENGE)


class LadderLedgerTests(unittest.TestCase):
    PROP = TwoLadderTypesTests.PROP

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.cur = self.db.cursor()
        self.cur.executescript("""
        CREATE TABLE ladder_sources (
          source_message_id INTEGER PRIMARY KEY, guild_id INTEGER,
          channel_id INTEGER, capper_user_id INTEGER, rung_count INTEGER,
          created_utc TEXT);
        CREATE TABLE ladder_rungs (
          source_message_id INTEGER, rung_number INTEGER,
          tracked_message_id INTEGER UNIQUE,
          PRIMARY KEY(source_message_id,rung_number));
        CREATE TABLE pending(message_id INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE bets(
          message_id INTEGER PRIMARY KEY, result TEXT,
          grade_reaction TEXT, grader_user_id INTEGER);
        """)
        self.reactions = []
        self.alerts = []
        self.original = types.SimpleNamespace(
            id=500, guild=types.SimpleNamespace(id=10),
            channel=types.SimpleNamespace(id=20),
            author=types.SimpleNamespace(id=99),
            created_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
            content=self.PROP,
            jump_url="https://discord.com/channels/10/20/500",
        )
        self.channel = types.SimpleNamespace(fetch_message=AsyncMock(return_value=self.original))
        self.ctx = {
            "cur": self.cur, "conn": self.db, "sqlite3": sqlite3,
            "re": re,
            "discord": types.SimpleNamespace(DiscordException=RuntimeError),
            "bot": types.SimpleNamespace(get_channel=lambda x: self.channel),
            "message_to_text": lambda m: m.content,
            "parse_ladder_post": parse_ladder_post,
            "is_challenge_day": is_challenge_day,
            "is_ladder_post": is_ladder_post,
            "is_multi_wager_post": is_multi_wager_post,
            "ladder_source_exists": lambda mid: self.cur.execute(
                "SELECT 1 FROM ladder_sources WHERE source_message_id=?", (mid,)
            ).fetchone() is not None,
            "infer_sports": lambda text: ["NBA"] if "NBA" in text else [],
            "utc_iso": lambda value: value.isoformat(),
            "send_temporary_notice": AsyncMock(side_effect=lambda m, msg: self.alerts.append(msg)),
            "safe_add_reaction": AsyncMock(side_effect=lambda m, emoji: self.reactions.append(emoji)),
            "safe_clear_reaction": AsyncMock(),
            "PENDING_REACTION": "📝", "LOGGED_REACTION": "📌",
            "WIN_EMOJI": "✅", "LOSS_EMOJI": "❌", "DUPLICATE_REACTION": "⚠️",
            "LADDER_NUMBER_REACTIONS": ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣"),
            "LADDER_FINISH_REACTION": "🏁",
            "parse_odds_text": lambda content: (
                re.findall(r"[+-]\d{2,5}", content)[0] if re.findall(r"[+-]\d{2,5}", content) else ""
            ),
            "authorized_to_grade": lambda uid, owner: uid == owner or uid == 1,
            "SimpleNamespace": types.SimpleNamespace,
            "parse_risk_units": lambda content: float(re.search(
                r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)\s*u\b", content, re.I
            ).group(1)),
            "record_wager_edit": lambda *args: self.alerts.append("EDIT LOGGED"),
        }
        self.ctx["pending_exists"] = lambda mid: self.cur.execute(
            "SELECT 1 FROM pending WHERE message_id=?", (mid,)
        ).fetchone() is not None
        self.ctx["bet_exists"] = lambda mid: self.cur.execute(
            "SELECT 1 FROM bets WHERE message_id=?", (mid,)
        ).fetchone() is not None
        self.ctx["refresh_pending_from_message"] = self.refresh_pending
        self.ctx["insert_pending"] = self.insert_pending
        self.ctx["grade_pending"] = self.grade_pending
        self.ctx["regrade_bet"] = self.regrade_bet
        self.ctx["ungrade_bet"] = self.ungrade_bet
        for name in ("ladder_rung_ids", "next_ladder_internal_id", "update_ladder_indicator",
                     "process_ladder_reaction", "track_ladder_post",
                     "correction_proxy", "sync_ladder_edit"):
            self.ctx[name] = load(name, self.ctx)

    def tearDown(self):
        self.db.close()

    def insert_pending(self, mid, guild, channel, capper, author, content, created, jump):
        self.assertEqual(jump, self.original.jump_url)
        self.assertEqual((guild, channel, author), (10, 20, 99))
        self.cur.execute("INSERT INTO pending VALUES (?, ?)", (mid, content))
        self.db.commit()
        return True, None

    def refresh_pending(self, proxy, capper):
        self.cur.execute("UPDATE pending SET content=? WHERE message_id=?",
                         (proxy.content, proxy.id))
        self.db.commit()
        return True

    def grade_pending(self, mid, result, emoji, uid):
        self.cur.execute("DELETE FROM pending WHERE message_id=?", (mid,))
        self.cur.execute("INSERT INTO bets VALUES (?, ?, ?, ?)", (mid, result, emoji, uid))
        self.db.commit()
        return True

    def regrade_bet(self, mid, result, emoji, uid):
        self.cur.execute("UPDATE bets SET result=?,grade_reaction=?,grader_user_id=? WHERE message_id=?",
                         (result, emoji, uid, mid))
        self.db.commit()
        return True

    def ungrade_bet(self, mid):
        self.cur.execute("DELETE FROM bets WHERE message_id=?", (mid,))
        self.cur.execute("INSERT INTO pending VALUES (?, ?)", (mid, "restored"))
        self.db.commit()
        return True

    def create_ladder(self):
        capper = types.SimpleNamespace(name="Sample", user_id=99)
        asyncio.run(self.ctx["track_ladder_post"](self.original, capper))

    def fire(self, emoji, user=99, removed=False):
        payload = types.SimpleNamespace(
            emoji=emoji, user_id=user, message_id=500, guild_id=10, channel_id=20
        )
        return asyncio.run(self.ctx["process_ladder_reaction"](payload, removed=removed))

    def test_ladder_has_five_independent_hidden_records_and_no_bot_replies(self):
        self.create_ladder()
        rows = self.cur.execute("SELECT tracked_message_id FROM ladder_rungs ORDER BY rung_number").fetchall()
        self.assertEqual(5, len(rows))
        self.assertTrue(all(r[0] < 0 for r in rows))
        self.assertIn("📝", self.reactions)
        self.assertIn("1️⃣", self.reactions)
        self.assertIn("🏁", self.reactions)
        self.assertEqual(5, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
        self.create_ladder()
        self.assertEqual(5, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
        self.assertFalse(self.alerts)

    def test_main_and_numbered_rungs_independent_then_finish_only_ungraded_losses(self):
        self.create_ladder()
        self.assertTrue(self.fire("✅"))
        self.assertTrue(self.fire("1️⃣"))
        self.assertTrue(self.fire("2️⃣"))
        self.fire("🏁")
        results = [row[0] for row in self.cur.execute(
            "SELECT result FROM bets JOIN ladder_rungs USING (tracked_message_id) ORDER BY rung_number"
        )] if False else [self.cur.execute(
            "SELECT result FROM bets WHERE message_id=?", (mid,)
        ).fetchone()[0] for mid in self.ctx["ladder_rung_ids"](500)]
        self.assertEqual(["win", "win", "win", "loss", "loss"], results)
        self.assertEqual(0, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
        self.assertIn("📌", self.reactions)
        self.fire("3️⃣")  # Correct a previously finished rung.
        self.assertEqual("win", self.cur.execute(
            "SELECT result FROM bets WHERE message_id=?",
            (self.ctx["ladder_rung_ids"](500)[3],)
        ).fetchone()[0])

    def test_finish_reaction_removal_reopens_only_auto_closed_rungs(self):
        self.create_ladder()
        self.fire("✅")
        self.fire("1️⃣")
        self.fire("🏁")
        self.assertEqual(0, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
        self.fire("🏁", removed=True)
        self.assertEqual(3, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
        self.assertEqual(2, self.cur.execute("SELECT COUNT(*) FROM bets").fetchone()[0])

    def test_correction_before_grade_and_refuse_after_grade(self):
        self.create_ladder()
        self.original.content = self.PROP.replace("0.8u", "0.6u").replace(
            "(+400 FanDuel)", "(+425 FanDuel)"
        )
        capper = types.SimpleNamespace(name="Sample", user_id=99)
        asyncio.run(self.ctx["sync_ladder_edit"](self.original, capper))
        ids = self.ctx["ladder_rung_ids"](500)
        self.assertIn("0.6u", self.cur.execute(
            "SELECT content FROM pending WHERE message_id=?", (ids[0],)
        ).fetchone()[0])
        self.assertIn("+425", self.cur.execute(
            "SELECT content FROM pending WHERE message_id=?", (ids[2],)
        ).fetchone()[0])
        self.assertIn("EDIT LOGGED", self.alerts)
        self.fire("✅")
        self.original.content = self.original.content.replace("0.6u", "1.6u")
        asyncio.run(self.ctx["sync_ladder_edit"](self.original, capper))
        self.assertEqual("win", self.cur.execute(
            "SELECT result FROM bets WHERE message_id=?", (ids[0],)
        ).fetchone()[0])
        self.assertTrue(any("partly graded" in note for note in self.alerts))

    def test_unauthorized_reaction_cannot_grade(self):
        self.create_ladder()
        self.fire("1️⃣", user=1234)
        self.assertEqual(5, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def test_remove_number_undoes_only_that_grade(self):
        self.create_ladder()
        self.fire("✅")
        self.fire("1️⃣")
        self.fire("1️⃣", removed=True)
        self.assertEqual(4, self.cur.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
        self.assertEqual("win", self.cur.execute(
            "SELECT result FROM bets WHERE message_id=?",
            (self.ctx["ladder_rung_ids"](500)[0],)
        ).fetchone()[0])


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
            "VOID_EMOJI": "↩️", "WAGER_DFS": "DFS",
            "format_compact_units": lambda x, signed=False: f"{x:+g}u" if signed else f"{x:g}u",
            "format_odds_display": lambda odds: odds,
        })
        self.fn = load("build_bet_result_line", self.scope)

    def test_simplified_straight_and_dfs_line(self):
        straight = (
            "2026-10-30", "PropKitchen", "win", 1., .74,
            "Karl Anthony-Towns o14.5 Rebounds+Assists ( Onyx |", "",
            "-135", "NBA", "https://discord.com/channels/1/2/123",
            "STRAIGHT", "Onyx", "2026-10-30",
        )
        self.assertIn("Karl Anthony-Towns o14.5 Reb+Ast",
                      self.fn(straight))
        self.assertNotIn("Onyx", self.fn(straight))
        self.assertNotIn("10/30", self.fn(straight))
        dfs = list(straight)
        dfs[10] = "DFS"
        dfs[11] = "Chalkboard"
        dfs[7] = "1.92x"
        self.assertIn("1u | Chalkboard | 1.92x", self.fn(tuple(dfs)))

if __name__ == "__main__":
    unittest.main()
