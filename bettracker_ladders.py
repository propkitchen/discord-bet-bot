"""Safe parsing for independent wager ladders and day-by-day bankroll challenges.

This module never guesses a win/loss from a prop threshold. Each line must
carry its OWN stake and odds. An emoji alone never creates multiple wagers.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Optional, Tuple

STAKE = r"(?:\d+(?:\.\d+)?|\.\d+)"
PREFIX = r"(?:[🪜🏀🏈⚾⚽🏒🎾]\s*)*"
LABEL = r"(?:(?:main\s+play|base\s+play|main|base|rung\s*\d+)\s*:\s*)?"
LEADING_UNITS = re.compile(
    rf"(?i)^\s*{PREFIX}{LABEL}{PREFIX}(?P<risk>{STAKE})\s*u\b"
)
UNITS = re.compile(rf"(?i)(?<![\w.]){STAKE}\s*u\b")
ODDS = re.compile(r"(?i)(?<![\w.])(?P<american>[+-]\d{2,5})(?![\d.])")
MULTIPLIER = re.compile(r"(?i)(?<![\w.])(?P<multiplier>\d+(?:\.\d+)?x)\b")
LADDER_MARKER = re.compile(r"(?i)(?<![A-Z0-9])LADDER(?![A-Z0-9])")
DAY_MARKER = re.compile(r"(?i)\b(?:ladder\s*)?(?:challenge\s*)?day\s*#?\s*(\d{1,2})\b")
CHALLENGE_MARKER = re.compile(r"(?i)\b(?:ladder\s+challenge|bankroll\s+challenge|daily\s+ladder)\b")


@dataclass(frozen=True)
class LadderRung:
    number: int  # 1-based; rung 1 = main/base stake, number key 1️⃣ = rung 2
    line: str
    risk: float
    odds: str


@dataclass(frozen=True)
class LadderPost:
    preface: str
    rungs: Tuple[LadderRung, ...]
    explicitly_labeled: bool = True


@dataclass(frozen=True)
class LadderChallenge:
    day: int
    wager: str
    risk: float
    odds: str


def is_ladder_post(text: str) -> bool:
    return "🪜" in (text or "") or bool(LADDER_MARKER.search(text or ""))


def is_challenge_day(text: str) -> bool:
    """A day-by-day bankroll challenge is NOT a multi-rung single-event ladder."""
    if not text:
        return False
    day = DAY_MARKER.search(text)
    return bool(day and (is_ladder_post(text) or CHALLENGE_MARKER.search(text)))


def _priced_stake(line: str) -> Optional[Tuple[float, str]]:
    """Return a single priced wager. Never reinterpret .5u as 5u."""
    prefix = LEADING_UNITS.match(line)
    if not prefix:
        return None
    risk = float(prefix.group("risk"))
    if risk <= 0:
        return None
    odds = ODDS.findall(line)
    multipliers = MULTIPLIER.findall(line)
    if len(odds) + len(multipliers) != 1:
        return None
    price = odds[0] if odds else multipliers[0]
    if price.lower().endswith("x") and float(price[:-1]) <= 1:
        return None
    # Reject a stake-only stub and stray odds. The wager must have a market.
    remainder = line[prefix.end():].replace("🪜", "").strip(" |:-")
    remainder = ODDS.sub("", remainder)
    remainder = MULTIPLIER.sub("", remainder)
    remainder = re.sub(r"(?i)\b(?:Onyx|FanDuel|DraftKings|Fanatics|Novig|BetMGM|Bet365|PrizePicks)\b", "", remainder)
    if len(remainder.strip(" ()|-:")) < 3:
        return None
    return risk, price


def has_multiple_staked_lines(text: str) -> bool:
    """Conservative detection: two lines with independently stated stakes."""
    return sum(1 for line in (text or "").splitlines() if LEADING_UNITS.match(line)) >= 2


def is_multi_wager_post(text: str) -> bool:
    """Emoji is optional, but multiple *priced* independent lines are required."""
    if is_challenge_day(text):
        return False
    return sum(1 for line in (text or "").splitlines() if _priced_stake(line)) >= 2


def parse_ladder_post(text: str) -> LadderPost:
    if is_challenge_day(text):
        raise ValueError("This is a day-by-day challenge, not an event prop ladder.")
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) > 35:
        raise ValueError("Keep a multi-rung wager to 2–8 clear, separately priced lines.")
    if not (is_ladder_post(text) or is_multi_wager_post(text)):
        raise ValueError("A multi-rung ladder needs two or more individually staked and priced lines.")
    preface: List[str] = []
    rungs: List[LadderRung] = []
    seen_rung = False
    for line in lines:
        stake_prefix = LEADING_UNITS.match(line)
        if stake_prefix:
            parsed = _priced_stake(line)
            if parsed is None:
                raise ValueError("Every rung needs its own stake, market and odds (e.g. .5u Player 30+ Pts +120).")
            risk, odds = parsed
            clean = line.replace("🪜", "").strip()
            clean = re.sub(r"(?i)^(?:main\s+play|base\s+play|main|base|rung\s*\d+)\s*:\s*", "", clean).strip()
            rungs.append(LadderRung(len(rungs) + 1, clean, risk, odds))
            seen_rung = True
        elif not seen_rung:
            preface.append(line.replace("🪜", "").strip())
        else:
            # Unstaked prose (links, notes or alternatives) is NOT an extra bet.
            # A second standalone price without stake remains just commentary.
            pass
    if not 2 <= len(rungs) <= 8:
        raise ValueError("A prop ladder needs 2–8 individually priced wagers.")
    if len({r.line.casefold() for r in rungs}) != len(rungs):
        raise ValueError("Duplicate rungs found; correct the repeated wager first.")
    return LadderPost(" ".join(preface).strip(), tuple(rungs), is_ladder_post(text))


def parse_challenge_day(text: str) -> Optional[LadderChallenge]:
    """Exactly one *priced* wager per day; pocketing and rollovers are notes.

    A challenge marker alone never blocks a valid single bet. There are no
    units-to-dollars conversions: the capper's explicitly staked units win.
    """
    if not is_challenge_day(text):
        return None
    day = DAY_MARKER.search(text or "")
    if not day:
        return None
    valid = [(line.strip(), _priced_stake(line)) for line in text.splitlines()]
    matches = [(line, parsed) for line, parsed in valid if parsed is not None]
    if len(matches) > 1:
        raise ValueError("This challenge day contains multiple priced wagers. Post the day's bet separately.")
    if len(matches) != 1:
        return None
    line, priced = matches[0]
    assert priced is not None
    return LadderChallenge(int(day.group(1)), line.replace("🪜", "").strip(), priced[0], priced[1])
