"""PropKitchen-branded, screenshot-ready performance cards.

Presentation only. Takes already calculated results and never changes bet data.
"""
from __future__ import annotations

import io
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


def generate_recap_card(
    title: str,
    rows: List[Tuple[str, float, int, int, int]],
    summary: Optional[Dict[str, object]] = None,
    period_label: str = "",
    section_title: str = "CAPPER LEADERBOARD",
) -> io.BytesIO:
    """Create a dark, readable 4:5 PNG suitable for sharing on X."""
    totals = summary or {}
    bg, panel, subdued, white = "#0D1116", "#1C242D", "#AAB5BE", "#F6F8FA"
    lime, green, red = "#BCF34D", "#55E197", "#FF7582"
    fig = plt.figure(figsize=(8, 10), dpi=150, facecolor=bg)
    ax = fig.add_axes((0, 0, 1, 1), frameon=False)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    def box(x, y, w, h):
        ax.add_patch(FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.005,rounding_size=0.018",
            facecolor=panel, edgecolor="none"
        ))

    def put(x, y, value, size=12, color=white, bold=False, align="left"):
        ax.text(x, y, str(value), color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                ha=align, va="center", family="DejaVu Sans")

    put(.07, .951, "PROPKITCHEN  /  BETTRACKER", 15, lime, True)
    put(.07, .905, "VIP PERFORMANCE", 29, white, True)
    subtitle = str(title).replace("🔥", "").replace("📊", "").strip()
    put(.07, .868, subtitle[:60], 13, subdued)
    if period_label:
        put(.07, .838, str(period_label)[:55], 12, subdued)

    wins = int(totals.get("wins", sum(int(r[2]) for r in rows)))
    losses = int(totals.get("losses", sum(int(r[3]) for r in rows)))
    pushes = int(totals.get("pushes", sum(int(r[4]) for r in rows)))
    net = float(totals.get("net", sum(float(r[1]) for r in rows)))
    roi = float(totals.get("roi", 0.0))
    count = int(totals.get("total", wins + losses + pushes))
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes else "")
    cards = [
        ("RECORD", record, white),
        ("NET UNITS", f"{net:+.2f}u", green if net >= 0 else red),
        ("ROI", f"{roi:+.1f}%", green if roi >= 0 else red),
    ]
    for idx, (label, value, color) in enumerate(cards):
        x = .07 + idx * .295
        box(x, .683, .273, .113)
        put(x + .018, .759, label, 10, subdued, True)
        put(x + .018, .715, value, 21 if len(value) < 10 else 16, color, True)

    put(.07, .641, section_title[:24], 18, white, True)
    put(.93, .641, f"{count} BETS", 11, subdued, True, "right")
    featured = rows[:8]
    if not featured:
        box(.07, .51, .86, .1)
        put(.10, .56, "No settled wagers for this period", 14, subdued)
    for idx, (capper, units, w, l, p) in enumerate(featured):
        y = .580 - idx * .060
        box(.07, y, .86, .053)
        put(.092, y + .027, f"{idx+1:02d}", 12, lime, True)
        put(.155, y + .027, str(capper)[:22], 14, white, True)
        small_record = f"{w}-{l}" + (f"-{p}" if p else "")
        put(.693, y + .027, small_record, 11, subdued, False, "right")
        put(.907, y + .027, f"{units:+.2f}u", 14,
            green if units >= 0 else red, True, "right")

    if len(rows) > 8:
        put(.07, .075, f"+ {len(rows) - 8} more cappers in the full report", 10, subdued)
    ax.plot([.07, .93], [.062, .062], color="#3C4650", linewidth=1)
    put(.07, .039, "Every result counts. Transparent tracking.", 10, subdued)
    put(.93, .039, "PK  /  VIP RECAP", 9, lime, True, "right")

    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=bg, dpi=150)
    plt.close(fig)
    out.seek(0)
    return out
