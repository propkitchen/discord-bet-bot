"""Read-only ladder post parser.

A ladder is explicitly marked with LADDER or 🪜, has 2–8 distinct lines
with independent units and American odds. Reject partial/malformed ladders;
never silently count the first rung as a whole ladder.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Tuple

UNITS = re.compile(r"(?i)(?<![A-Z0-9.])(?:\d+(?:\.\d+)?|\.\d+)\s*u\b")
LEADING_UNITS = re.compile(
    r"(?i)^\s*(?:[🏀🏈⚾⚽🏒🎾]\s*)?"
    r"(?P<risk>(?:\d+(?:\.\d+)?|\.\d+))\s*u\b"
)
AMERICAN = re.compile(r"(?<![A-Z0-9])([+-]\d{2,5})(?!\d)", flags=re.I)
MARKER = re.compile(r"(?i)(?<![A-Z0-9])LADDER(?![A-Z0-9])")


@dataclass(frozen=True)
class LadderRung:
    number: int
    line: str
    risk: float
    odds: str


@dataclass(frozen=True)
class LadderPost:
    preface: str
    rungs: Tuple[LadderRung, ...]


def is_ladder_post(text: str) -> bool:
    return "🪜" in text or bool(MARKER.search(text))


def has_multiple_staked_lines(text: str) -> bool:
    """Prevent silently saving just the first wager of an unmarked ladder."""
    lines = re.split(r"\r?\n", text or "")
    return sum(1 for line in lines if LEADING_UNITS.search(line)) >= 2


def parse_ladder_post(text: str) -> LadderPost:
    if not is_ladder_post(text):
        raise ValueError("Not a marked ladder.")
    lines = [line.strip() for line in re.split(r"\r?\n", text or "") if line.strip()]
    if not lines or len(lines) > 18:
        raise ValueError("Keep ladders to one short post with 2–8 rungs.")

    preface: List[str] = []
    rungs: List[LadderRung] = []
    started = False
    for line in lines:
        units = UNITS.findall(line)
        if not units:
            if started:
                raise ValueError("Place one complete wager on each rung; no extra lines between rungs.")
            preface.append(line.replace("🪜", "").strip())
            continue

        started = True
        leading = LEADING_UNITS.search(line)
        matches = AMERICAN.findall(line)
        if not leading or len(units) != 1 or len(matches) != 1:
            raise ValueError("Every rung needs its own stake and American odds, e.g. .5u 250+ Pass Yds (+120).")
        risk = float(leading.group("risk"))
        if risk <= 0:
            raise ValueError("Each rung must have a positive unit stake.")
        clean = line.replace("🪜", "").strip()
        if len(clean) < 12:
            raise ValueError("Add the actual player/market/threshold to every rung.")
        rungs.append(LadderRung(len(rungs) + 1, clean, risk, matches[0]))

    if not 2 <= len(rungs) <= 8:
        raise ValueError("A ladder needs 2–8 individually priced rungs.")
    if len({(x.line.lower(), x.risk, x.odds) for x in rungs}) != len(rungs):
        raise ValueError("Duplicate ladder rungs found; each rung must be unique.")
    return LadderPost(" ".join(preface).strip(), tuple(rungs))
