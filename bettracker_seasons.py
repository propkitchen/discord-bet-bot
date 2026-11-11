"""Canonical read-only seasons and competition parser for Discord and future API.

Phase 1 does NOT run database migrations, query the bot, or classify wagers.
Metadata is taken from config/season_registry.json only.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Dict, Optional, Sequence, Tuple

REGISTRY_PATH = Path(__file__).resolve().parent / "config" / "season_registry.json"
PHASE_ALIASES = {
    "preseason": ("preseason", "pre season"),
    "spring_training": ("spring training", "springtraining"),
    "regular": ("regular season", "regular"),
    "play_in": ("play in", "play-in", "playin"),
    "playoffs": ("playoffs", "playoff"),
    "postseason": ("postseason", "post season"),
    "conference_championships": ("conference championships",),
    "conference_tournaments": ("conference tournaments",),
    "bowls": ("bowl games", "bowls"),
    "cfp": ("college football playoff", "cfp"),
    "march_madness": ("march madness", "ncaa tournament"),
    "knockout": ("knockout stage", "knockout phase", "knockouts"),
    "league": ("league phase",),
    "qualifying": ("qualifiers", "qualifying stage", "qualifying rounds", "qualifying"),
    "group": ("group stage", "group phase"),
    "main_draw": ("main draw",),
    "final": ("final",),
}
GROUP_MEMBERS = {
    "GRAND_SLAMS": ("AUSTRALIAN_OPEN", "ROLAND_GARROS", "WIMBLEDON", "US_OPEN_TENNIS"),
    "ATP_TOUR": ("ATP_M1000", "ATP_500", "ATP_250", "ATP_FINALS", "ATP_NEXT_GEN"),
    "WTA_TOUR": ("WTA_1000", "WTA_500", "WTA_250", "WTA_125", "WTA_FINALS"),
    "GOLF_MAJORS": ("MASTERS_GOLF", "PGA_CHAMPIONSHIP", "US_OPEN_GOLF", "OPEN_CHAMPIONSHIP"),
}
SEASON_PATTERN = re.compile(
    r"(?<!\d)(?P<first>20\d{2}|\d{2})(?:\s*[-–]\s*(?P<second>20\d{2}|\d{2}))?(?!\d)"
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def _alias_re(alias: str) -> re.Pattern[str]:
    words = re.split(r"\s+", _norm(alias))
    return re.compile(r"(?<![\w])" + r"\s+".join(re.escape(w) for w in words)
                      + r"(?![\w])", re.I)


@lru_cache(maxsize=1)
def registry() -> Dict[str, Any]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported season registry version")
    ids = [entry["id"] for entry in payload["competitions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate competition identifier")
    for entry in payload["windows"]:
        if entry["competition_id"] not in ids:
            raise ValueError("Unknown competition in a calendar window")
        if entry["status"] == "verified" and not (entry["start_date"] and entry["end_date"]):
            raise ValueError("Verified window requires both dates")
        if entry["start_date"] and entry["end_date"]:
            if date.fromisoformat(entry["start_date"]) > date.fromisoformat(entry["end_date"]):
                raise ValueError("Calendar range is reversed")
        if not entry.get("source_url", "").startswith("https://"):
            raise ValueError("Every calendar window must cite a source")
    return payload


@dataclass(frozen=True)
class SeasonQuery:
    sport: str
    competition_id: str
    season_id: str
    phase: str
    included_phases: Tuple[str, ...]
    competition_ids: Tuple[str, ...]
    date_start: Optional[date]
    date_end: Optional[date]
    calendar_complete: bool
    verified_sources: Tuple[str, ...]
    breakdown: bool = False

    @property
    def title(self) -> str:
        label = self.competition_id.replace("_", " ")
        phase = {"full": "Full Season", "all": "Season", "play_in": "Play-In",
                 "knockout": "Knockout Stage", "main_draw": "Main Draw"}.get(
                     self.phase, self.phase.replace("_", " ").title())
        return f"{label} {self.season_id} {phase}"

    @property
    def date_range_display(self) -> str:
        if not self.date_start:
            return "Unverified event dates — competition classification required"
        end = self.date_end.isoformat() if self.date_end else "end pending verification"
        return f"{self.date_start.isoformat()} – {end}"


def _parse_year(text: str) -> Optional[Tuple[int, Optional[int], Tuple[int, int]]]:
    match = SEASON_PATTERN.search(text)
    if not match:
        return None
    first = int(match.group("first"))
    if first < 100:
        first += 2000
    raw_second = match.group("second")
    second = int(raw_second) if raw_second else None
    if second is not None and second < 100:
        second += 2000
    if second is not None and second != first + 1:
        return None
    return first, second, match.span()


def _find_competition(text: str, items: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    matches = []
    for item in items:
        for alias in item["aliases"]:
            for m in _alias_re(alias).finditer(text):
                matches.append((m.start(), m.end(), len(alias), item))
    if not matches:
        return None
    matches.sort(key=lambda x: (-x[2], x[0]))
    best = matches[0]
    for start, end, _length, item in matches[1:]:
        if start < best[1] and end > best[0]:
            continue
        if item["id"] != best[3]["id"]:
            return None
    return best[3]


def _phase(text: str, competition: Dict[str, Any]) -> Optional[str]:
    matched = [
        phase for phase, aliases in PHASE_ALIASES.items()
        if phase in competition["phases"]
        and any(_alias_re(alias).search(text) for alias in aliases)
    ]
    if len(set(matched)) > 1:
        return None
    if matched:
        return matched[0]
    return "full" if competition.get("full_phases") else "all"


def _canonical_year(competition: Dict[str, Any], year: int,
                    second: Optional[int], raw: str) -> Optional[str]:
    style = competition["season_style"]
    if style == "calendar":
        if second is not None and competition["id"] not in {"NFL", "NCAAF"}:
            return None
        return str(year)
    if style == "split":
        return f"{year}-{str(year + 1)[-2:]}"
    if style == "special" and competition["id"] == "MLS":
        if "sprint" in _norm(raw):
            return "2027-sprint" if year == 2027 and second is None else None
        if second is not None:
            return f"{year}-{str(year + 1)[-2:]}" if year >= 2027 else None
        return str(year) if year <= 2026 else None
    return str(year) if second is None else None


def parse_season_query(raw: str) -> Optional[SeasonQuery]:
    """Safely recognize season filters without hijacking legacy date commands.

    Requires a named competition or league AND a real season year.
    Unknown, partial or conflicting phrases return None.
    """
    original = _norm(raw)
    parsed = _parse_year(original)
    if parsed is None:
        return None
    year, second, span = parsed
    text = (original[:span[0]] + " " + original[span[1]:]).strip()
    breakdown = bool(_alias_re("breakdown").search(text))
    text = _alias_re("breakdown").sub(" ", text).strip()
    comp = _find_competition(text, registry()["competitions"])
    if comp is None:
        return None
    matched_aliases = [
        (len(alias), _alias_re(alias).search(text))
        for alias in comp["aliases"] if _alias_re(alias).search(text)
    ]
    match = max(matched_aliases, key=lambda pair: pair[0])[1]
    assert match is not None
    remainder = (text[:match.start()] + " " + text[match.end():]).strip()
    phase = _phase(remainder, comp)
    if phase is None:
        return None
    terms = [alias for aliases in PHASE_ALIASES.values() for alias in aliases]
    terms += ["full season", "season", "sprint season", "sprint", "breakdown"]
    for term in sorted(terms, key=len, reverse=True):
        remainder = _alias_re(term).sub(" ", remainder)
    if _norm(remainder):
        return None
    sid = _canonical_year(comp, year, second, original)
    if sid is None:
        return None
    phases = tuple(comp["full_phases"] if phase == "full" else
                   comp["phases"] if phase == "all" else (phase,))
    aggregate_sports = {
        "TENNIS_ALL": "TENNIS", "GOLF_ALL": "GOLF",
        "ESPORTS_ALL": "ESPORTS", "MOTORSPORTS_ALL": "MOTORSPORTS",
    }
    if comp["id"] in aggregate_sports:
        ids = tuple(c["id"] for c in registry()["competitions"]
                    if c["sport"] == aggregate_sports[comp["id"]])
    else:
        ids = GROUP_MEMBERS.get(comp["id"], (comp["id"],))
    # A tour/group view inherits phase types from its actual tournaments.
    # Do not require the synthetic parent "tour" tag on each member.
    if comp["id"] in {*aggregate_sports, "ATP_TOUR", "WTA_TOUR", "GOLF_MAJORS"}:
        phases = tuple(sorted({
            phase for child in registry()["competitions"]
            if child["id"] in ids for phase in child["phases"]
        }))
    windows = [w for w in registry()["windows"] if
               w["competition_id"] in ids and w["season_id"] == sid
               and w["phase"] in phases]
    # FIFA publishes complete tournament boundaries but not every group /
    # knockout phase in this registry. An explicit all-phase window is valid
    # for displaying the tournament span, not for guessing match phases.
    overall = [w for w in registry()["windows"] if
               w["competition_id"] in ids and w["season_id"] == sid
               and w["phase"] == "all"] if phase in {"full", "all"} else []
    available = windows + overall
    start = min((date.fromisoformat(w["start_date"]) for w in available
                 if w.get("start_date")), default=None)
    complete = bool(
        (windows and len(windows) >= len(phases)
         and all(w["status"] == "verified" and w.get("end_date") for w in windows))
        or (overall and all(w["status"] == "verified" and w.get("end_date")
                            for w in overall))
    )
    end = max((date.fromisoformat(w["end_date"]) for w in available
               if w.get("end_date")), default=None) if complete else None
    return SeasonQuery(
        comp["sport"], comp["id"], sid, phase, phases, ids, start, end,
        complete, tuple(sorted({w["source_url"] for w in available})),
        breakdown,
    )


def membership_exists_sql(
    query: SeasonQuery, wager_alias: str = "b",
) -> Tuple[str, Tuple[object, ...]]:
    """Stage 2 SQL contract: EXISTS prevents overlapping membership double count.

    Not wired to live bot. Requires validated wager_season_memberships table.
    """
    if not re.fullmatch(r"[A-Za-z_]\w*", wager_alias):
        raise ValueError("Unsafe SQL alias")
    comps = ",".join("?" for _ in query.competition_ids)
    phases = ",".join("?" for _ in query.included_phases)
    sql = (
        "EXISTS (SELECT 1 FROM wager_season_memberships AS sm "
        f"WHERE sm.guild_id = {wager_alias}.guild_id "
        f"AND sm.message_id = {wager_alias}.message_id "
        "AND sm.season_id = ? "
        f"AND sm.competition_id IN ({comps}) "
        f"AND sm.phase IN ({phases}))"
    )
    return sql, (query.season_id, *query.competition_ids, *query.included_phases)


def as_api_metadata(query: SeasonQuery) -> Dict[str, Any]:
    """One JSON-compatible contract for future FastAPI and Next.js UI."""
    return {
        "sport": query.sport, "competition_id": query.competition_id,
        "season_id": query.season_id, "phase": query.phase,
        "included_phases": list(query.included_phases),
        "date_start": query.date_start.isoformat() if query.date_start else None,
        "date_end": query.date_end.isoformat() if query.date_end else None,
        "calendar_complete": query.calendar_complete,
        "verified_sources": list(query.verified_sources),
        "breakdown": query.breakdown,
    }
