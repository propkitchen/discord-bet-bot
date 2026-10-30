"""Capper lifecycle regression tests; do not import or launch the live bot."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import re
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def fixture_roster():
    roster = {}
    inactive = None
    for node in TREE.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "TRACKED_CHANNELS":
                for channel_id, capper in zip(node.value.keys, node.value.values):
                    roster[ast.literal_eval(channel_id)] = SimpleNamespace(
                        name=ast.literal_eval(capper.args[0]),
                        user_id=ast.literal_eval(capper.args[1]),
                    )
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "INACTIVE_CAPPER_NAMES" for target in node.targets):
                inactive = frozenset(ast.literal_eval(node.value.args[0]))
    if not roster or inactive is None:
        raise AssertionError("Capper roster or inactive-capper configuration missing")
    return roster, inactive


def function(name, **globals_dict):
    node = next(n for n in TREE.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    block = ast.fix_missing_locations(ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    ))
    scope = dict(globals_dict)
    exec(compile(block, "<isolated roster function>", "exec"), scope)
    return scope[name]


class CapperStatusTests(unittest.TestCase):
    def setUp(self):
        self.roster, self.inactive = fixture_roster()
        self.by_id = function("capper_by_user_id", TRACKED_CHANNELS=self.roster, INACTIVE_CAPPER_NAMES=self.inactive)
        self.is_trackable_channel = function(
            "is_trackable_channel",
            TRACKED_CHANNELS=self.roster,
            SHARED_TRACKING_CHANNEL_IDS={1387178991139553351},
        )
        self.resolve = function(
            "resolve_capper_for_message",
            is_trackable_channel=self.is_trackable_channel,
            capper_by_user_id=self.by_id,
            TRACKED_CHANNELS=self.roster,
            INACTIVE_CAPPER_NAMES=self.inactive,
        )

    def test_only_named_cappers_deactivated(self):
        self.assertEqual(self.inactive, {"pxs", "daijonbets", "mikelocks", "mattlocks"})
        self.assertEqual(len(self.roster), 10)
        for capper in self.roster.values():
            if capper.name.lower() in self.inactive:
                self.assertIsNone(self.by_id(capper.user_id))
            else:
                self.assertIsNotNone(self.by_id(capper.user_id))

    def test_inactive_cappers_cannot_track_in_shared_channels(self):
        channel = SimpleNamespace(id=1387178991139553351)
        for capper in self.roster.values():
            message = SimpleNamespace(channel=channel, author=SimpleNamespace(id=capper.user_id), webhook_id=None)
            got = self.resolve(message)
            self.assertEqual(got is None, capper.name.lower() in self.inactive)

    def test_inactive_dedicated_channels_are_disabled_for_new_posts(self):
        active_id = next(c.user_id for c in self.roster.values() if c.name == "PropKitchen")
        for channel_id, capper in self.roster.items():
            if capper.name.lower() not in self.inactive:
                continue
            message = SimpleNamespace(channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(id=active_id), webhook_id=None)
            self.assertIsNone(self.resolve(message))
            webhook = SimpleNamespace(channel=SimpleNamespace(id=channel_id), author=SimpleNamespace(id=999), webhook_id=1)
            self.assertIsNone(self.resolve(webhook))

    def test_older_pending_bets_remain_grading_eligible(self):
        # Reaction handlers use the unchanged trackable-channel registry.
        for channel_id, capper in self.roster.items():
            if capper.name.lower() in self.inactive:
                self.assertTrue(self.is_trackable_channel(channel_id))

    def test_active_leaderboard_excludes_idle_zero_bet_cappers(self):
        names = function("tracked_capper_display_names", TRACKED_CHANNELS=self.roster, INACTIVE_CAPPER_NAMES=self.inactive)()
        self.assertTrue(all(name.lower() not in self.inactive for name in names))
        self.assertIn("PropKitchen", names)
        # Historic capper records remain available to fetch_leaderboard_rows separately.

    def test_historical_stevie_alias(self):
        fn = function("capper_aliases", TRACKED_CHANNELS=self.roster, normalize_key=lambda x: re.sub("[^a-z0-9]", "", x.lower()))
        self.assertEqual(fn()["stevie"], "pxs")
        self.assertEqual(fn()["picksxstevie"], "pxs")


if __name__ == "__main__":
    unittest.main()
