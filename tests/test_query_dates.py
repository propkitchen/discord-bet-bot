"""Natural date and command parsing regressions (no Discord client startup)."""
from __future__ import annotations
import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def load(name, **globals_dict):
    fn = next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node = ast.fix_missing_locations(ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn],
        type_ignores=[]))
    env = dict(globals_dict)
    exec(compile(node, "<isolated date parser>", "exec"), env)
    return env[name]


class DateParserTests(unittest.TestCase):
    def setUp(self):
        parse = load("parse_date_yyyy_mm_dd", datetime=datetime)
        self.parse = parse
        def day_bounds(day):
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            return start, start + timedelta(days=1)
        def month_bounds(y, m):
            start = datetime(y, m, 1, tzinfo=timezone.utc)
            end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
            return start, end
        def weekly(_period, _ref):
            return day_bounds(date(2026, 10, 26))[0], day_bounds(date(2026, 11, 1))[1]
        names = {"october": 10, "oct": 10, "july": 7, "jul": 7}
        self.time = load("parse_time_filter",
                         normalize_space=lambda x: " ".join(x.split()),
                         now_local=lambda: datetime(2026, 10, 30, tzinfo=timezone.utc),
                         local_day_bounds=day_bounds, local_month_bounds=month_bounds,
                         period_bounds_local=weekly,
                         parse_date_yyyy_mm_dd=parse,
                         MONTH_NAMES=names, re=re, date=date, timedelta=timedelta,
                         datetime=datetime, _tz=lambda: timezone.utc)

    def test_slash_and_iso_dates(self):
        for s in ("10/18/2026", "10-18-2026", "2026-10-18", "Oct 18 2026", "10/18/26"):
            with self.subTest(date=s):
                self.assertEqual(self.parse(s), date(2026, 10, 18))
                label, start, end, err = self.time(s)
                self.assertIsNone(err)
                self.assertEqual(start.date(), date(2026, 10, 18))
                self.assertEqual(end.date(), date(2026, 10, 19))

    def test_relative_14_days(self):
        label, start, end, error = self.time("last 14 days")
        self.assertIsNone(error)
        self.assertEqual(label, "Last 14 days")
        self.assertEqual(start.date(), date(2026, 10, 17))
        self.assertEqual(end.date(), date(2026, 10, 31))

    def test_nba_2026_season_starts_october_20(self):
        label, start, end, err = self.time("2026 season", sport="NBA")
        self.assertIsNone(err)
        self.assertIn("NBA", label)
        self.assertEqual(start.date(), date(2026, 10, 20))
        self.assertEqual(end.date(), date(2026, 10, 31))

    def test_unknown_season_is_rejected_not_guessed(self):
        label, start, end, err = self.time("2026 season", sport="MLB")
        self.assertIsNone(start)
        self.assertIsNotNone(err)

    def test_month_year_and_slash_date_range(self):
        for value in ("October 2026", "Oct 2026", "10/2026"):
            _label, start, end, error = self.time(value)
            self.assertIsNone(error)
            self.assertEqual(start.date(), date(2026, 10, 1))
            self.assertEqual(end.date(), date(2026, 11, 1))
        _label, start, end, error = self.time("10/17/2026 - 10/19/2026")
        self.assertIsNone(error)
        self.assertEqual(start.date(), date(2026, 10, 17))
        self.assertEqual(end.date(), date(2026, 10, 20))


if __name__ == "__main__":
    unittest.main()
