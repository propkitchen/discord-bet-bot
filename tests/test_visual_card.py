"""Render actual 4:5 recap PNGs without running the Discord bot."""
from __future__ import annotations

import io
import struct
import unittest

from bettracker_visuals import generate_recap_card


class SocialCardTests(unittest.TestCase):
    def test_vip_card_is_valid_portrait_png(self):
        rows = [
            ("PropKitchen", 4.25, 8, 3, 1),
            ("hotshot", -1.75, 3, 5, 0),
            ("gr8", 0.65, 4, 3, 0),
        ]
        totals = {
            "total": 24, "wins": 15, "losses": 11, "pushes": 1,
            "net": 3.15, "roi": 9.4,
        }
        png = generate_recap_card("VIP RECAP - DAILY", rows, totals, "October 30, 2026")
        data = png.getvalue()
        self.assertGreater(len(data), 10000)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", data[16:24])
        self.assertLess(width, height)
        self.assertAlmostEqual(width / height, .8, delta=.01)

    def test_sport_breakdown_and_empty_result_render(self):
        image = generate_recap_card(
            "PropKitchen", [("NBA", 3.2, 6, 2, 1), ("MIXED", -0.5, 2, 3, 0)],
            {"total": 14, "wins": 8, "losses": 5, "pushes": 1,
             "net": 2.7, "roi": 14.5},
            "October 2026",
            section_title="SPORT BREAKDOWN",
        )
        self.assertGreater(len(image.getvalue()), 10000)
        empty = generate_recap_card("No bets", [], {"total": 0})
        self.assertGreater(len(empty.getvalue()), 10000)


if __name__ == "__main__":
    unittest.main()
