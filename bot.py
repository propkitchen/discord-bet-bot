"""
bot.py

Discord betting tracker (channel-based cappers) using reaction grading.

Flow:
1) A play is posted in a tracked capper channel (can be capper user OR webhook).
2) Bot reacts 📝 to mark it as pending (only if it contains units like 1u / .25u / 0.5u).
3) The capper grades by reacting:
   ✅ = Win
   ❌ = Loss
   ➖ = Push
4) Bot logs the result to SQLite and reacts 📌.
5) If capper removes ✅/❌/➖, bot ungrades and switches back to 📝.
6) If capper adds a different grading reaction later, bot can regrade the bet.

Recaps:
- Auto-post Daily recap at 10:00 AM ET (yesterday) into SUMMARY_CHANNEL_ID.
- Commands for daily/weekly/monthly/yearly/all-time summaries.

Deployment:
- Use env var TOKEN (never commit token).
- Render persistent disk path: /var/data/bets.db
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
import matplotlib.pyplot as plt
from discord.ext import commands, tasks

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# =====================
# CONFIG
# =====================

PREFIX = "bt!"
TOKEN = os.getenv("TOKEN")

DB_PATH = "/var/data/bets.db"
REPORT_TZ = "America/New_York"

SUMMARY_CHANNEL_ID = 1473454134689796146  # #bettingtracker

AUTOPOST_ENABLED = True
AUTOPOST_HOUR_ET = 10
AUTOPOST_MINUTE_ET = 0
WEEKLY_POST_WEEKDAY = 0  # Monday
MONTHLY_POST_DAY = 1     # 1st of month

PENDING_REACTION = "📝"
LOGGED_REACTION = "📌"

WIN_EMOJI = "✅"
LOSS_EMOJI = "❌"
PUSH_EMOJI = "➖"
GRADE_EMOJIS = {WIN_EMOJI, LOSS_EMOJI, PUSH_EMOJI}


@dataclass(frozen=True)
class Capper:
    name: str
    user_id: int


# Add new cappers here only.
# Format:
# CHANNEL_ID: Capper("DisplayName", DISCORD_USER_ID),
TRACKED_CHANNELS: Dict[int, Capper] = {
    1257081246509563944: Capper("PropKitchen", 1230980936657535061),
    1258244563726893106: Capper("hotshot", 475659527337934849),
    1281388388569579608: Capper("clipset", 684940092665757696),
    1278486906169987226: Capper("pxs", 933024893992329286),
    1356017581558857796: Capper("mattlocks", 1242294328253218878),
    1344526479366688808: Capper("ballsout", 1345160333261668362),
    1409640332295147570: Capper("gr8", 1109269360037601411),
    1430746272192659569: Capper("mikelocks", 1430751846125010970),
    1424256774692667422: Capper("betsbybray", 865284268745949194),
    1515610298696863885: Capper("DaijonBets", 1168899954660614155),
}


# =====================
# DISCORD SETUP
# =====================

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)


# =====================
# TIME HELPERS
# =====================

def _tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(REPORT_TZ)
    except Exception:
        return timezone.utc


def now_local() -> datetime:
    return datetime.now(_tz())


def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=_tz())
    return dt_local.astimezone(timezone.utc)


def utc_iso(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone.utc).isoformat()


def period_bounds_local(period: str, ref: Optional[date] = None) -> Tuple[datetime, datetime]:
    period = period.lower()
    ref = ref or now_local().date()

    if period == "daily":
        start = datetime(ref.year, ref.month, ref.day, 0, 0, 0, tzinfo=_tz())
        return start, start + timedelta(days=1)

    if period == "weekly":
        start_day = ref - timedelta(days=ref.weekday())  # Monday
        start = datetime(start_day.year, start_day.month, start_day.day, 0, 0, 0, tzinfo=_tz())
        return start, start + timedelta(days=7)

    if period == "monthly":
        start = datetime(ref.year, ref.month, 1, 0, 0, 0, tzinfo=_tz())
        if ref.month == 12:
            end = datetime(ref.year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
        else:
            end = datetime(ref.year, ref.month + 1, 1, 0, 0, 0, tzinfo=_tz())
        return start, end

    if period == "yearly":
        start = datetime(ref.year, 1, 1, 0, 0, 0, tzinfo=_tz())
        end = datetime(ref.year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
        return start, end

    start = datetime(ref.year, ref.month, ref.day, 0, 0, 0, tzinfo=_tz())
    return start, start + timedelta(days=1)


def parse_date_yyyy_mm_dd(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


MONTH_NAME_TO_NUMBER = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def resolve_capper_name(raw: str) -> str:
    target = normalize_lookup(raw)
    seen: Dict[str, str] = {}
    for capper in TRACKED_CHANNELS.values():
        seen[normalize_lookup(capper.name)] = capper.name
    return seen.get(target, raw.strip())


def known_sport_codes() -> set[str]:
    return {code.upper() for code, _ in SPORT_KEYWORDS}


def extract_sport_token(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    remaining: List[str] = []
    found: Optional[str] = None
    sport_codes = known_sport_codes()
    for token in tokens:
        clean = token.strip().upper()
        if found is None and clean in sport_codes:
            found = clean
        else:
            remaining.append(token)
    return found, remaining


def month_bounds_local(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=_tz())
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=_tz())
    return start, end


def period_filter_from_tokens(tokens: List[str]) -> Tuple[str, str, Tuple[object, ...]]:
    """Parse friendly date tokens like july, 2026-07, thisweek, lastmonth."""
    cleaned = [t.strip().lower() for t in tokens if t.strip()]
    joined = " ".join(cleaned).replace("_", "").replace("-", "")
    today = now_local().date()

    if not cleaned:
        return "All-Time", "", ()

    def bounds_to_filter(label: str, start_l: datetime, end_l: datetime) -> Tuple[str, str, Tuple[object, ...]]:
        return label, "created_utc >= ? AND created_utc < ?", (utc_iso(to_utc(start_l)), utc_iso(to_utc(end_l)))

    # Exact month: 2026-07
    for token in cleaned:
        if re.fullmatch(r"\d{4}-\d{2}", token):
            start_d = datetime.strptime(token, "%Y-%m").date().replace(day=1)
            start_l, end_l = month_bounds_local(start_d.year, start_d.month)
            return bounds_to_filter(f"{start_d.strftime('%B')} {start_d.year}", start_l, end_l)

    # Exact year: 2026
    for token in cleaned:
        if re.fullmatch(r"\d{4}", token):
            year = int(token)
            start_l = datetime(year, 1, 1, 0, 0, 0, tzinfo=_tz())
            end_l = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
            return bounds_to_filter(str(year), start_l, end_l)

    if joined in {"today"}:
        start_l, end_l = period_bounds_local("daily", today)
        return bounds_to_filter("Today", start_l, end_l)

    if joined in {"yesterday"}:
        start_l, end_l = period_bounds_local("daily", today - timedelta(days=1))
        return bounds_to_filter("Yesterday", start_l, end_l)

    if joined in {"thisweek", "week", "weekly"}:
        start_l, end_l = period_bounds_local("weekly", today)
        return bounds_to_filter("This Week", start_l, end_l)

    if joined in {"lastweek", "previousweek"}:
        start_l, end_l = period_bounds_local("weekly", today - timedelta(days=7))
        return bounds_to_filter("Last Week", start_l, end_l)

    if joined in {"thismonth", "month", "monthly"}:
        start_l, end_l = period_bounds_local("monthly", today)
        return bounds_to_filter("This Month", start_l, end_l)

    if joined in {"lastmonth", "previousmonth"}:
        first_this_month = today.replace(day=1)
        prev_ref = first_this_month - timedelta(days=1)
        start_l, end_l = period_bounds_local("monthly", prev_ref)
        return bounds_to_filter(f"{start_l.strftime('%B')} {start_l.year}", start_l, end_l)

    if joined in {"thisyear", "year", "yearly"}:
        start_l, end_l = period_bounds_local("yearly", today)
        return bounds_to_filter(str(today.year), start_l, end_l)

    if joined in {"lastyear", "previousyear"}:
        year = today.year - 1
        start_l = datetime(year, 1, 1, 0, 0, 0, tzinfo=_tz())
        end_l = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
        return bounds_to_filter(str(year), start_l, end_l)

    # Friendly month name: july / Jul / September. Uses current year.
    for token in cleaned:
        if token in MONTH_NAME_TO_NUMBER:
            month = MONTH_NAME_TO_NUMBER[token]
            start_l, end_l = month_bounds_local(today.year, month)
            return bounds_to_filter(f"{start_l.strftime('%B')} {today.year}", start_l, end_l)

    return "All-Time", "", ()


def combine_where(parts: List[str], params: List[object]) -> Tuple[str, Tuple[object, ...]]:
    return " AND ".join(parts) if parts else "1 = 1", tuple(params)


# =====================
# DATABASE
# =====================

def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


conn = db_connect()
cur = conn.cursor()


def table_columns(table_name: str) -> set[str]:
    rows = cur.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def add_column_if_missing(table_name: str, column_name: str, column_definition: str) -> None:
    if column_name not in table_columns(table_name):
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def ensure_schema() -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            capper TEXT NOT NULL,
            capper_user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            sport TEXT NOT NULL,
            risk_units REAL NOT NULL,
            odds_text TEXT NOT NULL,
            jump_url TEXT NOT NULL DEFAULT '',
            league TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            player TEXT NOT NULL DEFAULT '',
            team TEXT NOT NULL DEFAULT '',
            opponent TEXT NOT NULL DEFAULT '',
            bet_type TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT '',
            line TEXT NOT NULL DEFAULT '',
            sportsbook TEXT NOT NULL DEFAULT '',
            odds_format TEXT NOT NULL DEFAULT '',
            multiplier REAL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE NOT NULL,
            channel_id INTEGER NOT NULL,
            capper TEXT NOT NULL,
            capper_user_id INTEGER NOT NULL,
            sport TEXT NOT NULL,
            risk_units REAL NOT NULL,
            net_units REAL NOT NULL,
            result TEXT NOT NULL,
            odds_text TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            graded_utc TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            jump_url TEXT NOT NULL DEFAULT '',
            league TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            player TEXT NOT NULL DEFAULT '',
            team TEXT NOT NULL DEFAULT '',
            opponent TEXT NOT NULL DEFAULT '',
            bet_type TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT '',
            line TEXT NOT NULL DEFAULT '',
            sportsbook TEXT NOT NULL DEFAULT '',
            odds_format TEXT NOT NULL DEFAULT '',
            multiplier REAL,
            grade_reaction TEXT NOT NULL DEFAULT ''
        )
        """
    )

    # Safe migrations for existing Render database.
    pending_columns = {
        "jump_url": "TEXT NOT NULL DEFAULT ''",
        "league": "TEXT NOT NULL DEFAULT ''",
        "event": "TEXT NOT NULL DEFAULT ''",
        "player": "TEXT NOT NULL DEFAULT ''",
        "team": "TEXT NOT NULL DEFAULT ''",
        "opponent": "TEXT NOT NULL DEFAULT ''",
        "bet_type": "TEXT NOT NULL DEFAULT ''",
        "market": "TEXT NOT NULL DEFAULT ''",
        "line": "TEXT NOT NULL DEFAULT ''",
        "sportsbook": "TEXT NOT NULL DEFAULT ''",
        "odds_format": "TEXT NOT NULL DEFAULT ''",
        "multiplier": "REAL",
    }
    for col, definition in pending_columns.items():
        add_column_if_missing("pending", col, definition)

    bet_columns = {
        "content": "TEXT NOT NULL DEFAULT ''",
        "jump_url": "TEXT NOT NULL DEFAULT ''",
        "league": "TEXT NOT NULL DEFAULT ''",
        "event": "TEXT NOT NULL DEFAULT ''",
        "player": "TEXT NOT NULL DEFAULT ''",
        "team": "TEXT NOT NULL DEFAULT ''",
        "opponent": "TEXT NOT NULL DEFAULT ''",
        "bet_type": "TEXT NOT NULL DEFAULT ''",
        "market": "TEXT NOT NULL DEFAULT ''",
        "line": "TEXT NOT NULL DEFAULT ''",
        "sportsbook": "TEXT NOT NULL DEFAULT ''",
        "odds_format": "TEXT NOT NULL DEFAULT ''",
        "multiplier": "REAL",
        "grade_reaction": "TEXT NOT NULL DEFAULT ''",
    }
    for col, definition in bet_columns.items():
        add_column_if_missing("bets", col, definition)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_created_utc ON bets(created_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_capper_time ON bets(capper, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport_time ON bets(sport, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_league_time ON bets(league, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_type_time ON bets(bet_type, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_player_time ON bets(player, graded_utc);")
    conn.commit()


ensure_schema()


# =====================
# PARSING PLAYS
# =====================

RE_UNITS = re.compile(r"(?i)\b((?:\d+(?:\.\d+)?|\.\d+))\s*u\b")
RE_AMERICAN_PAREN = re.compile(r"(?i)\(([-+]\d{2,5})(?:\s+[A-Za-z ]+)?\)")
RE_AMERICAN = re.compile(r"(?i)\b([-+]\d{2,5})\b")
RE_MULT = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*x\b")
RE_LINE = re.compile(r"(?i)\b(?:over|under|o|u)\s*((?:\d+(?:\.\d+)?|\.\d+))\b")

SPORT_KEYWORDS = [
    ("NCAAB", ["NCAAB", "CBB", "COLLEGE BASKETBALL"]),
    ("NBA", ["NBA"]),
    ("WNBA", ["WNBA"]),
    ("NFL", ["NFL"]),
    ("NCAAF", ["NCAAF", "CFB", "COLLEGE FOOTBALL"]),
    ("NHL", ["NHL", "HOCKEY"]),
    ("MLB", ["MLB", "BASEBALL"]),
    ("SOCCER", ["SOCCER", "EPL", "UCL", "MLS", "LA LIGA", "SERIE A", "BUNDESLIGA", "WORLD CUP"]),
    ("TENNIS", ["TENNIS", "ATP", "WTA"]),
    ("ESPORTS", ["ESPORTS", "E-SPORTS", "CS2", "CSGO", "VALORANT", "LOL", "DOTA"]),
    ("UFC", ["UFC"]),
    ("MMA", ["MMA"]),
    ("BOXING", ["BOXING"]),
]

LEAGUE_KEYWORDS = [
    ("WNBA", ["WNBA"]),
    ("NBA", ["NBA"]),
    ("NBA Summer League", ["SUMMER LEAGUE", "NBA SUMMER"]),
    ("NCAAB", ["NCAAB", "CBB", "COLLEGE BASKETBALL"]),
    ("NFL", ["NFL"]),
    ("NCAAF", ["NCAAF", "CFB", "COLLEGE FOOTBALL"]),
    ("MLB", ["MLB", "BASEBALL"]),
    ("NHL", ["NHL", "HOCKEY"]),
    ("World Cup", ["WORLD CUP"]),
    ("Premier League", ["PREMIER LEAGUE", "EPL"]),
    ("Champions League", ["CHAMPIONS LEAGUE", "UCL"]),
    ("MLS", ["MLS"]),
    ("La Liga", ["LA LIGA"]),
    ("Serie A", ["SERIE A"]),
    ("Bundesliga", ["BUNDESLIGA"]),
    ("ATP", ["ATP"]),
    ("WTA", ["WTA"]),
]

SPORTSBOOK_KEYWORDS = [
    ("PrizePicks", ["PRIZEPICKS", "PRIZE PICKS", " PP "]),
    ("Underdog", ["UNDERDOG", " UD "]),
    ("Onyx", ["ONYX"]),
    ("Hard Rock", ["HARD ROCK", " HR)", " HR "]),
    ("DraftKings", ["DRAFTKINGS", " DK "]),
    ("FanDuel", ["FANDUEL", " FD "]),
    ("BetMGM", ["BETMGM", " MGM "]),
    ("Caesars", ["CAESARS"]),
    ("ESPN BET", ["ESPN BET"]),
]

BET_TYPE_RULES = [
    ("Parlay", ["PARLAY", "COMBO", "BUILDER", "SLIP"]),
    ("PRA", ["PRA", "PTS+REB+AST", "POINTS+REBOUNDS+ASSISTS", "PTS REB AST"]),
    ("PR", [" PR ", "PTS+REB", "POINTS+REBOUNDS", "PTS REB"]),
    ("PA", [" PA ", "PTS+AST", "POINTS+ASSISTS", "PTS AST"]),
    ("RA", [" RA ", "REB+AST", "REBOUNDS+ASSISTS", "REB AST"]),
    ("Points", ["POINTS", " PTS "]),
    ("Rebounds", ["REBOUNDS", " REB "]),
    ("Assists", ["ASSISTS", " AST "]),
    ("3PM", ["3PM", "3 POINTER", "3-POINTER", "THREES"]),
    ("Strikeouts", ["STRIKEOUTS", " KS", " K'S", " K "]),
    ("Outs", ["OUTS", "OUTS RECORDED"]),
    ("Hits Allowed", ["HITS ALLOWED", "HA"]),
    ("Hits", [" HITS", " HIT "]),
    ("Total Bases", ["TOTAL BASES", " TB"]),
    ("Home Runs", ["HOME RUNS", " HR", "HOMER"]),
    ("Earned Runs", ["EARNED RUNS", " ER"]),
    ("HRR", [" HRR", "HITS RUNS RBI", "HITS+RUNS+RBI"]),
    ("Moneyline", ["MONEYLINE", " ML"]),
    ("Spread", ["SPREAD", " +", " -"]),
    ("Team Total", ["TEAM TOTAL", " TT"]),
    ("Total", ["TOTAL RUNS", "TOTAL POINTS", " OVER ", " UNDER "]),
    ("SOG", ["SOG", "SHOTS ON GOAL"]),
    ("SOT", ["SOT", "SHOT ON TARGET", "SHOTS ON TARGET"]),
    ("Goals", ["GOALS", " GOAL"]),
]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def message_to_text(message: discord.Message) -> str:
    parts: List[str] = []
    if message.content:
        parts.append(message.content)

    # Webhook play posters often put the play in embeds, not message.content.
    for embed in message.embeds:
        if embed.title:
            parts.append(str(embed.title))
        if embed.description:
            parts.append(str(embed.description))
        for field in embed.fields:
            if field.name:
                parts.append(str(field.name))
            if field.value:
                parts.append(str(field.value))

    return "\n".join(parts).strip()


def infer_sport(text: str) -> str:
    up = f" {text.upper()} "
    for code, keys in SPORT_KEYWORDS:
        for k in keys:
            if k in up:
                return code
    return "UNKNOWN"


def infer_league(text: str, sport: str) -> str:
    up = f" {text.upper()} "
    for league, keys in LEAGUE_KEYWORDS:
        for k in keys:
            if k in up:
                return league
    return sport if sport != "UNKNOWN" else ""


def infer_sportsbook(text: str) -> str:
    up = f" {text.upper()} "
    for book, keys in SPORTSBOOK_KEYWORDS:
        for k in keys:
            if k in up:
                return book
    return ""


def infer_bet_type(text: str) -> str:
    up = f" {text.upper()} "
    for bet_type, keys in BET_TYPE_RULES:
        for k in keys:
            if k in up:
                return bet_type
    return ""


def parse_risk_units(text: str) -> Optional[float]:
    m = RE_UNITS.search(text)
    if not m:
        return None
    try:
        u = float(m.group(1))
        return u if u > 0 else None
    except Exception:
        return None


def parse_odds_text(text: str) -> str:
    m = RE_AMERICAN_PAREN.search(text)
    if m:
        return m.group(1)
    m = RE_MULT.search(text)
    if m:
        return f"{m.group(1)}x"
    m = RE_AMERICAN.search(text)
    if m:
        return m.group(1)
    return ""


def parse_multiplier_value(odds_text: str) -> Optional[float]:
    if not odds_text.lower().endswith("x"):
        return None
    try:
        return float(odds_text[:-1])
    except Exception:
        return None


def infer_odds_format(odds_text: str) -> str:
    if not odds_text:
        return ""
    if odds_text.lower().endswith("x"):
        return "multiplier"
    try:
        int(odds_text)
        return "american"
    except Exception:
        return ""


def parse_line(text: str) -> str:
    m = RE_LINE.search(text)
    if not m:
        return ""
    return m.group(1)


def parse_market(text: str) -> str:
    clean = normalize_space(text)
    # Remove leading stake so market text is cleaner.
    clean = RE_UNITS.sub("", clean, count=1).strip(" -–—|:")
    # Remove odds/multiplier from the market display.
    clean = RE_AMERICAN_PAREN.sub("", clean)
    clean = RE_MULT.sub("", clean)
    clean = normalize_space(clean)
    return clean[:250]


def parse_player(text: str) -> str:
    clean = normalize_space(text)
    clean = RE_UNITS.sub("", clean, count=1).strip(" -–—|:")

    # If this looks like a game/team total, avoid pretending the team/event is a player.
    up = clean.upper()
    if "/" in clean or " VS " in f" {up} " or "TEAM TOTAL" in up or "TOTAL RUNS" in up:
        return ""
    if "MONEYLINE" in up or " ML" in up or "SPREAD" in up:
        return ""

    m = re.search(
        r"(?i)\b(?:over|under|o|u)\s*(?:\d|\.)|\b(?:to record|anytime|double double|triple double)\b",
        clean,
    )
    if not m:
        return ""

    candidate = clean[:m.start()].strip(" -–—|:")
    candidate = re.sub(r"(?i)\b(NBA|WNBA|MLB|NFL|NHL|NCAAB|CBB|SOCCER|TENNIS)\b", "", candidate)
    candidate = normalize_space(candidate)

    # Avoid storing huge text as player name.
    if not candidate or len(candidate) > 60:
        return ""
    if len(candidate.split()) > 5:
        return ""
    return candidate


def parse_analytics_fields(text: str, odds_text: str) -> Dict[str, object]:
    sport = infer_sport(text)
    multiplier = parse_multiplier_value(odds_text)
    return {
        "sport": sport,
        "league": infer_league(text, sport),
        "event": "",
        "player": parse_player(text),
        "team": "",
        "opponent": "",
        "bet_type": infer_bet_type(text),
        "market": parse_market(text),
        "line": parse_line(text),
        "sportsbook": infer_sportsbook(text),
        "odds_format": infer_odds_format(odds_text),
        "multiplier": multiplier,
    }


def profit_from_american(risk: float, american: int) -> float:
    if american > 0:
        return risk * (american / 100.0)
    return risk * (100.0 / abs(american))


def profit_from_multiplier(risk: float, mult: float) -> float:
    # Correct multiplier math:
    # .25u at 3x returns .75u total, but profit is .50u.
    # Net profit = risk * (multiplier - 1)
    return risk * (mult - 1.0)


def compute_net_units(risk: float, odds_text: str, result: str) -> float:
    result = result.lower()
    if result == "push":
        return 0.0
    if result == "loss":
        return -risk

    if not odds_text:
        return risk

    if odds_text.lower().endswith("x"):
        try:
            mult = float(odds_text[:-1])
            if mult <= 1:
                return 0.0
            return profit_from_multiplier(risk, mult)
        except Exception:
            return risk

    try:
        american = int(odds_text)
        return profit_from_american(risk, american)
    except Exception:
        return risk


def emoji_to_result(emoji: str) -> str:
    if emoji == WIN_EMOJI:
        return "win"
    if emoji == LOSS_EMOJI:
        return "loss"
    return "push"


# =====================
# CHARTS
# =====================

def generate_units_chart(title: str, rows: List[Tuple[str, float, int, int, int]]) -> io.BytesIO:
    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]

    fig, ax = plt.subplots()
    ax.bar(labels, values)
    ax.set_title(title)
    ax.set_ylabel("Net Units")
    ax.tick_params(axis="x", rotation=45)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


# =====================
# REPORTING
# =====================

def fetch_capper_rows(start_utc: datetime, end_utc: datetime) -> List[Tuple[str, float, int, int, int]]:
    rows = cur.execute(
        """
        SELECT
            capper,
            COALESCE(SUM(net_units), 0) AS net_units_sum,
            SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE graded_utc >= ? AND graded_utc < ?
        GROUP BY capper
        ORDER BY net_units_sum DESC
        """,
        (utc_iso(start_utc), utc_iso(end_utc)),
    ).fetchall()
    return [(str(c), float(u), int(w or 0), int(l or 0), int(p or 0)) for c, u, w, l, p in rows]


def fetch_vip_totals(start_utc: datetime, end_utc: datetime) -> Tuple[float, int, int, int]:
    row = cur.execute(
        """
        SELECT
            COALESCE(SUM(net_units), 0) AS net_units_sum,
            SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE graded_utc >= ? AND graded_utc < ?
        """,
        (utc_iso(start_utc), utc_iso(end_utc)),
    ).fetchone()
    if not row:
        return 0.0, 0, 0, 0
    return float(row[0] or 0.0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)


def make_rows_text(rows: List[Tuple[str, float, int, int, int]]) -> str:
    if not rows:
        return "No graded bets found."
    lines: List[str] = []
    for capper_name, net_units, wins, losses, pushes in rows:
        graded = wins + losses
        win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
        record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")
        lines.append(f"**{capper_name}**: {record} ({win_pct:.1f}%) | **{net_units:+.2f}u**")
    return "\n".join(lines)


def make_vip_line(net_units: float, wins: int, losses: int, pushes: int) -> str:
    graded = wins + losses
    win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")
    return f"**VIP TOTAL**: {record} ({win_pct:.1f}%) | **{net_units:+.2f}u**"


async def post_period_summary(channel: discord.abc.Messageable, title: str, start_local: datetime, end_local: datetime) -> None:
    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)

    rows = fetch_capper_rows(start_utc, end_utc)
    vip_net, vip_w, vip_l, vip_p = fetch_vip_totals(start_utc, end_utc)

    await channel.send(
        f"📊 **{title}**\n{make_vip_line(vip_net, vip_w, vip_l, vip_p)}\n\n{make_rows_text(rows)}"
    )

    if rows:
        img = generate_units_chart(f"{title} Net Units", rows)
        await channel.send(file=discord.File(img, filename="units.png"))


def fetch_filtered_totals(where_sql: str, params: Tuple[object, ...]) -> Tuple[int, float, float, int, int, int]:
    row = cur.execute(
        f"""
        SELECT
            COUNT(*) AS total_bets,
            COALESCE(SUM(risk_units), 0) AS risk_sum,
            COALESCE(SUM(net_units), 0) AS net_sum,
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    if not row:
        return 0, 0.0, 0.0, 0, 0, 0
    return int(row[0] or 0), float(row[1] or 0.0), float(row[2] or 0.0), int(row[3] or 0), int(row[4] or 0), int(row[5] or 0)


def clean_bet_display_text(content: str, market: str, max_len: int = 140) -> str:
    """Return the best human-readable bet text for command output."""
    text = normalize_space(content) or normalize_space(market)
    if not text:
        return "Bet details unavailable (old row before tracking upgrade)"
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def fetch_recent_bets(where_sql: str, params: Tuple[object, ...], limit: int = 5) -> List[Tuple[str, str, str, float, float, str, str, str]]:
    rows = cur.execute(
        f"""
        SELECT created_utc, capper, result, risk_units, net_units, content, market, jump_url
        FROM bets
        WHERE {where_sql}
        ORDER BY created_utc DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()
    return [
        (
            str(created),
            str(capper),
            str(result),
            float(risk),
            float(net),
            str(content or ""),
            str(market or ""),
            str(jump or ""),
        )
        for created, capper, result, risk, net, content, market, jump in rows
    ]


def build_filtered_summary_text(title: str, where_sql: str, params: Tuple[object, ...]) -> str:
    total, risk, net, wins, losses, pushes = fetch_filtered_totals(where_sql, params)
    graded = wins + losses
    win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
    roi = (net / risk * 100.0) if risk > 0 else 0.0
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")

    lines = [
        f"📊 **{title}**",
        f"Record: **{record}** ({win_pct:.1f}%)",
        f"Net Units: **{net:+.2f}u**",
        f"Risked: **{risk:.2f}u** | ROI: **{roi:.1f}%**",
        f"Total Bets: **{total}**",
    ]

    recent = fetch_recent_bets(where_sql, params)
    if recent:
        lines.append("\n**Recent Bets**")
        for created, capper, result, risk_units, net_units, content, market, jump_url in recent:
            icon = "✅" if result == "win" else ("❌" if result == "loss" else "➖")
            bet_text = clean_bet_display_text(content, market)
            if jump_url:
                lines.append(f"{icon} **{capper}** | {bet_text} | {net_units:+.2f}u | [jump]({jump_url})")
            else:
                lines.append(f"{icon} **{capper}** | {bet_text} | {net_units:+.2f}u")

    return "\n".join(lines)


async def post_filtered_summary(ctx: commands.Context, title: str, where_sql: str, params: Tuple[object, ...]) -> None:
    await ctx.send(build_filtered_summary_text(title, where_sql, params))


# =====================
# PENDING + GRADING
# =====================

async def safe_add_reaction(msg: discord.Message, emoji: str) -> None:
    try:
        await msg.add_reaction(emoji)
    except Exception:
        return


async def safe_clear_reaction(msg: discord.Message, emoji: str) -> None:
    try:
        await msg.clear_reaction(emoji)
    except Exception:
        return


def pending_exists(message_id: int) -> bool:
    return cur.execute("SELECT 1 FROM pending WHERE message_id = ?", (message_id,)).fetchone() is not None


def bet_exists(message_id: int) -> bool:
    return cur.execute("SELECT 1 FROM bets WHERE message_id = ?", (message_id,)).fetchone() is not None


def insert_pending(
    message_id: int,
    channel_id: int,
    capper: Capper,
    content: str,
    created_utc: str,
    jump_url: str,
) -> bool:
    risk = parse_risk_units(content)
    if risk is None:
        return False

    odds_text = parse_odds_text(content)
    fields = parse_analytics_fields(content, odds_text)

    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (
            message_id, channel_id, capper, capper_user_id, content, created_utc,
            sport, risk_units, odds_text, jump_url, league, event, player, team,
            opponent, bet_type, market, line, sportsbook, odds_format, multiplier
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            channel_id,
            capper.name,
            capper.user_id,
            content,
            created_utc,
            str(fields["sport"]),
            float(risk),
            odds_text,
            jump_url,
            str(fields["league"]),
            str(fields["event"]),
            str(fields["player"]),
            str(fields["team"]),
            str(fields["opponent"]),
            str(fields["bet_type"]),
            str(fields["market"]),
            str(fields["line"]),
            str(fields["sportsbook"]),
            str(fields["odds_format"]),
            fields["multiplier"],
        ),
    )
    conn.commit()
    return True


def grade_pending(message_id: int, result: str, grade_reaction: str) -> bool:
    row = cur.execute(
        """
        SELECT
            channel_id, capper, capper_user_id, content, sport, risk_units, odds_text,
            created_utc, jump_url, league, event, player, team, opponent, bet_type,
            market, line, sportsbook, odds_format, multiplier
        FROM pending
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return False

    (
        channel_id,
        capper_name,
        capper_user_id,
        content,
        sport,
        risk,
        odds_text,
        created_utc,
        jump_url,
        league,
        event,
        player,
        team,
        opponent,
        bet_type,
        market,
        line,
        sportsbook,
        odds_format,
        multiplier,
    ) = row

    # Older pending rows may not have parsed fields yet. Re-parse from content if needed.
    if content and (not sport or sport == "UNKNOWN" or not bet_type or not market):
        fields = parse_analytics_fields(str(content), str(odds_text))
        sport = sport if sport and sport != "UNKNOWN" else str(fields["sport"])
        league = league or str(fields["league"])
        player = player or str(fields["player"])
        bet_type = bet_type or str(fields["bet_type"])
        market = market or str(fields["market"])
        line = line or str(fields["line"])
        sportsbook = sportsbook or str(fields["sportsbook"])
        odds_format = odds_format or str(fields["odds_format"])
        multiplier = multiplier if multiplier is not None else fields["multiplier"]

    net = compute_net_units(float(risk), str(odds_text), result)

    cur.execute(
        """
        INSERT OR REPLACE INTO bets
        (
            message_id, channel_id, capper, capper_user_id, sport, risk_units,
            net_units, result, odds_text, created_utc, graded_utc, content,
            jump_url, league, event, player, team, opponent, bet_type, market,
            line, sportsbook, odds_format, multiplier, grade_reaction
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(channel_id),
            str(capper_name),
            int(capper_user_id),
            str(sport),
            float(risk),
            float(net),
            str(result),
            str(odds_text),
            str(created_utc),
            utc_iso(datetime.now(timezone.utc)),
            str(content or ""),
            str(jump_url or ""),
            str(league or ""),
            str(event or ""),
            str(player or ""),
            str(team or ""),
            str(opponent or ""),
            str(bet_type or ""),
            str(market or ""),
            str(line or ""),
            str(sportsbook or ""),
            str(odds_format or ""),
            multiplier,
            str(grade_reaction),
        ),
    )
    cur.execute("DELETE FROM pending WHERE message_id = ?", (message_id,))
    conn.commit()
    return True


def regrade_bet(message_id: int, result: str, grade_reaction: str) -> bool:
    row = cur.execute(
        """
        SELECT risk_units, odds_text
        FROM bets
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return False

    risk, odds_text = row
    net = compute_net_units(float(risk), str(odds_text), result)

    cur.execute(
        """
        UPDATE bets
        SET result = ?, net_units = ?, graded_utc = ?, grade_reaction = ?
        WHERE message_id = ?
        """,
        (str(result), float(net), utc_iso(datetime.now(timezone.utc)), str(grade_reaction), int(message_id)),
    )
    conn.commit()
    return True


def ungrade_bet(message_id: int) -> bool:
    row = cur.execute(
        """
        SELECT
            channel_id, capper, capper_user_id, content, sport, risk_units,
            odds_text, created_utc, jump_url, league, event, player, team,
            opponent, bet_type, market, line, sportsbook, odds_format, multiplier
        FROM bets
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return False

    (
        channel_id,
        capper_name,
        capper_user_id,
        content,
        sport,
        risk,
        odds_text,
        created_utc,
        jump_url,
        league,
        event,
        player,
        team,
        opponent,
        bet_type,
        market,
        line,
        sportsbook,
        odds_format,
        multiplier,
    ) = row

    cur.execute("DELETE FROM bets WHERE message_id = ?", (message_id,))
    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (
            message_id, channel_id, capper, capper_user_id, content, created_utc,
            sport, risk_units, odds_text, jump_url, league, event, player, team,
            opponent, bet_type, market, line, sportsbook, odds_format, multiplier
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(channel_id),
            str(capper_name),
            int(capper_user_id),
            str(content or ""),
            str(created_utc),
            str(sport),
            float(risk),
            str(odds_text),
            str(jump_url or ""),
            str(league or ""),
            str(event or ""),
            str(player or ""),
            str(team or ""),
            str(opponent or ""),
            str(bet_type or ""),
            str(market or ""),
            str(line or ""),
            str(sportsbook or ""),
            str(odds_format or ""),
            multiplier,
        ),
    )
    conn.commit()
    return True


async def find_remaining_capper_grade(msg: discord.Message, capper_user_id: int) -> Optional[str]:
    """After a reaction is removed, check whether the capper still has another grade reaction on the message."""
    for reaction in msg.reactions:
        emoji = str(reaction.emoji)
        if emoji not in GRADE_EMOJIS:
            continue
        try:
            async for user in reaction.users(limit=None):
                if user.id == capper_user_id:
                    return emoji
        except Exception:
            continue
    return None


# =====================
# EVENTS
# =====================

@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")
    if AUTOPOST_ENABLED and not autopost_loop.is_running():
        autopost_loop.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore our own bot messages
    if bot.user and message.author.id == bot.user.id:
        return

    capper = TRACKED_CHANNELS.get(message.channel.id)
    if not capper:
        await bot.process_commands(message)
        return

    # Allow either:
    # - capper user posting, OR
    # - a webhook posting (common for rich embed play posters)
    is_capper_user = (message.author.id == capper.user_id)
    is_webhook_post = (message.webhook_id is not None)

    # If it's some other bot (not webhook), ignore
    if message.author.bot and not is_webhook_post:
        await bot.process_commands(message)
        return

    if not (is_capper_user or is_webhook_post):
        await bot.process_commands(message)
        return

    if pending_exists(message.id) or bet_exists(message.id):
        await bot.process_commands(message)
        return

    content = message_to_text(message)
    ok = insert_pending(
        message.id,
        message.channel.id,
        capper,
        content,
        utc_iso(message.created_at),
        message.jump_url,
    )
    if ok:
        await safe_add_reaction(message, PENDING_REACTION)

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if not bot.user:
        return

    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    if emoji not in GRADE_EMOJIS:
        return

    capper = TRACKED_CHANNELS.get(payload.channel_id)
    if not capper:
        return

    # Only that channel's capper can grade
    if payload.user_id != capper.user_id:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        msg = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    result = emoji_to_result(emoji)

    if pending_exists(payload.message_id):
        if not grade_pending(payload.message_id, result, emoji):
            return
    elif bet_exists(payload.message_id):
        # Allows direct regrading if the capper adds a different grade reaction later.
        if not regrade_bet(payload.message_id, result, emoji):
            return
    else:
        return

    await safe_clear_reaction(msg, PENDING_REACTION)
    await safe_add_reaction(msg, LOGGED_REACTION)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if not bot.user:
        return

    emoji = str(payload.emoji)
    if emoji not in GRADE_EMOJIS:
        return

    capper = TRACKED_CHANNELS.get(payload.channel_id)
    if not capper:
        return

    if payload.user_id != capper.user_id:
        return

    if not bet_exists(payload.message_id):
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        msg = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    remaining_grade = await find_remaining_capper_grade(msg, capper.user_id)
    if remaining_grade:
        # If another grade reaction is still present, treat this as a regrade instead of an ungrade.
        result = emoji_to_result(remaining_grade)
        if regrade_bet(payload.message_id, result, remaining_grade):
            await safe_add_reaction(msg, LOGGED_REACTION)
        return

    if not ungrade_bet(payload.message_id):
        return

    await safe_clear_reaction(msg, LOGGED_REACTION)
    await safe_add_reaction(msg, PENDING_REACTION)


# =====================
# COMMANDS
# =====================

@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


@bot.command(name="commands", aliases=["guide", "howto"])
async def commands_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "📌 **Bet Tracker Commands**\n\n"
        "**Basic Recaps**\n"
        "`bt!daily` — today\n"
        "`bt!weekly` — current week\n"
        "`bt!monthly` — current month\n"
        "`bt!yearly` — current year\n"
        "`bt!alltime` — all tracked bets\n\n"
        "**Cleaner Filters**\n"
        "`bt!capper PropKitchen`\n"
        "`bt!capper gr8 july`\n"
        "`bt!capper gr8 WNBA july`\n"
        "`bt!sport WNBA july`\n"
        "`bt!player Paige Bueckers`\n"
        "`bt!bettype Rebounds`\n"
        "`bt!month 2026-07`\n"
        "`bt!year 2026`\n"
        "`bt!range 2026-07-01 2026-07-31`\n\n"
        "**Pending / Admin**\n"
        "`bt!pending` — pending count by capper\n"
        "`bt!clear_pending` — clear all pending rows/reset pending board\n"
        "`bt!clear_pending gr8` — clear pending for one capper\n"
        "`bt!backfill_content 200` — recover exact bet text for old rows where possible\n"
        "`bt!recalc_multipliers` — fix old multiplier math rows"
    )


@bot.command()
async def daily(ctx: commands.Context) -> None:
    start_l, end_l = period_bounds_local("daily")
    await post_period_summary(ctx, "Daily", start_l, end_l)


@bot.command()
async def weekly(ctx: commands.Context) -> None:
    start_l, end_l = period_bounds_local("weekly")
    await post_period_summary(ctx, "Weekly", start_l, end_l)


@bot.command()
async def monthly(ctx: commands.Context) -> None:
    start_l, end_l = period_bounds_local("monthly")
    await post_period_summary(ctx, "Monthly", start_l, end_l)


@bot.command()
async def yearly(ctx: commands.Context) -> None:
    start_l, end_l = period_bounds_local("yearly")
    await post_period_summary(ctx, "Yearly", start_l, end_l)


@bot.command()
async def alltime(ctx: commands.Context) -> None:
    start_l = datetime(2000, 1, 1, 0, 0, 0, tzinfo=_tz())
    end_l = now_local() + timedelta(days=1)
    await post_period_summary(ctx, "All-Time", start_l, end_l)


@bot.command(name="pending")
async def pending_cmd(ctx: commands.Context) -> None:
    rows = cur.execute(
        """
        SELECT capper, COUNT(*)
        FROM pending
        GROUP BY capper
        ORDER BY COUNT(*) DESC
        """
    ).fetchall()

    total = sum(int(count or 0) for _, count in rows)
    if not rows:
        await ctx.send("📝 **Pending Bets:** 0")
        return

    lines = [f"📝 **Pending Bets:** {total}"]
    for capper_name, count in rows:
        lines.append(f"**{capper_name}**: {int(count)}")
    await ctx.send("\n".join(lines))


@bot.command(name="clear_pending", aliases=["clearpending", "reset_pending", "resetpending"])
@commands.has_permissions(manage_guild=True)
async def clear_pending_cmd(ctx: commands.Context, *, capper_name: str = "") -> None:
    """Clear pending bets without deleting graded betting history."""
    capper_name = capper_name.strip()

    if capper_name:
        resolved = resolve_capper_name(capper_name)
        rows = cur.execute(
            """
            SELECT message_id, channel_id, capper
            FROM pending
            WHERE LOWER(capper) = ?
            """,
            (resolved.lower(),),
        ).fetchall()
    else:
        resolved = "ALL"
        rows = cur.execute(
            """
            SELECT message_id, channel_id, capper
            FROM pending
            """
        ).fetchall()

    if not rows:
        await ctx.send("🧹 **Pending Reset:** No pending bets found.")
        return

    cleared_reactions = 0
    for message_id, channel_id, _capper in rows:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            continue
        try:
            msg = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
        except Exception:
            continue
        await safe_clear_reaction(msg, PENDING_REACTION)
        cleared_reactions += 1

    if capper_name:
        cur.execute("DELETE FROM pending WHERE LOWER(capper) = ?", (resolved.lower(),))
    else:
        cur.execute("DELETE FROM pending")
    conn.commit()

    target = resolved if capper_name else "all cappers"
    await ctx.send(
        "🧹 **Pending Reset Complete**\n"
        f"Target: **{target}**\n"
        f"Deleted pending rows: **{len(rows)}**\n"
        f"Cleared 📝 reactions where possible: **{cleared_reactions}**\n"
        "Graded bets/history were **not** deleted."
    )


@bot.command(name="backfill_content", aliases=["backfill", "backfill_recent"])
@commands.has_permissions(manage_guild=True)
async def backfill_content_cmd(ctx: commands.Context, limit: int = 100) -> None:
    """Try to recover exact bet text for old rows by fetching original Discord messages."""
    limit = max(1, min(int(limit), 500))
    rows = cur.execute(
        """
        SELECT message_id, channel_id, odds_text, sport
        FROM bets
        WHERE COALESCE(content, '') = '' OR COALESCE(market, '') = ''
        ORDER BY created_utc DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        await ctx.send("✅ No old rows needed content backfill.")
        return

    checked = 0
    updated = 0
    missing = 0

    for message_id, channel_id, old_odds_text, old_sport in rows:
        checked += 1
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            missing += 1
            continue
        try:
            msg = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
        except Exception:
            missing += 1
            continue

        content = message_to_text(msg)
        if not content:
            missing += 1
            continue

        odds_text = str(old_odds_text or "") or parse_odds_text(content)
        fields = parse_analytics_fields(content, odds_text)
        sport = str(fields["sport"]) if str(fields["sport"]) != "UNKNOWN" else str(old_sport or "UNKNOWN")

        cur.execute(
            """
            UPDATE bets
            SET
                content = ?,
                jump_url = ?,
                sport = ?,
                league = ?,
                player = ?,
                bet_type = ?,
                market = ?,
                line = ?,
                sportsbook = ?,
                odds_text = ?,
                odds_format = ?,
                multiplier = ?
            WHERE message_id = ?
            """,
            (
                content,
                msg.jump_url,
                sport,
                str(fields["league"]),
                str(fields["player"]),
                str(fields["bet_type"]),
                str(fields["market"]),
                str(fields["line"]),
                str(fields["sportsbook"]),
                odds_text,
                str(fields["odds_format"]),
                fields["multiplier"],
                int(message_id),
            ),
        )
        updated += 1

    conn.commit()
    await ctx.send(
        "✅ **Backfill Complete**\n"
        f"Checked: **{checked}** old rows\n"
        f"Updated with exact bet text: **{updated}**\n"
        f"Could not fetch/fill: **{missing}**\n"
        "Now try your command again and recent bets should show more exact text where Discord messages were recoverable."
    )


@bot.command(name="recalc_multipliers", aliases=["recalcmultipliers", "fixmultipliers", "fix_multipliers"])
@commands.has_permissions(manage_guild=True)
async def recalc_multipliers_cmd(ctx: commands.Context) -> None:
    rows = cur.execute(
        """
        SELECT id, risk_units, odds_text, net_units
        FROM bets
        WHERE result = 'win' AND LOWER(odds_text) LIKE '%x%'
        """
    ).fetchall()

    checked = 0
    updated = 0
    old_total = 0.0
    new_total = 0.0

    for bet_id, risk_units, odds_text, old_net in rows:
        checked += 1
        mult = parse_multiplier_value(str(odds_text))
        if mult is None:
            continue
        new_net = compute_net_units(float(risk_units), str(odds_text), "win")
        old_net_f = float(old_net or 0.0)
        old_total += old_net_f
        new_total += new_net
        if abs(new_net - old_net_f) > 0.0001:
            cur.execute("UPDATE bets SET net_units = ? WHERE id = ?", (float(new_net), int(bet_id)))
            updated += 1

    conn.commit()
    await ctx.send(
        "✅ **Multiplier Recalc Complete**\n"
        f"Checked: **{checked}** multiplier wins\n"
        f"Updated: **{updated}** rows\n"
        f"Old total: **{old_total:+.2f}u**\n"
        f"New total: **{new_total:+.2f}u**"
    )


@bot.command(name="sport")
async def sport_cmd(ctx: commands.Context, *, sport_name: str) -> None:
    tokens = sport_name.strip().split()
    if not tokens:
        await ctx.send("Use format: `bt!sport WNBA` or `bt!sport WNBA july`")
        return

    sport, remaining = extract_sport_token(tokens)
    if sport is None:
        sport = tokens[0].strip().upper()
        remaining = tokens[1:]

    where_parts = ["UPPER(sport) = ?"]
    params: List[object] = [sport]

    period_label, period_sql, period_params = period_filter_from_tokens(remaining)
    if period_sql:
        where_parts.append(period_sql)
        params.extend(period_params)

    title = f"Sport: {sport}"
    if period_label != "All-Time":
        title += f" — {period_label}"

    where_sql, final_params = combine_where(where_parts, params)
    await post_filtered_summary(ctx, title, where_sql, final_params)


@bot.command(name="league")
async def league_cmd(ctx: commands.Context, *, league_name: str) -> None:
    value = league_name.strip().lower()
    await post_filtered_summary(ctx, f"League: {league_name.strip()}", "LOWER(league) = ?", (value,))


@bot.command(name="capper")
async def capper_cmd(ctx: commands.Context, *, capper_name: str) -> None:
    tokens = capper_name.strip().split()
    if not tokens:
        await ctx.send("Use format: `bt!capper PropKitchen`, `bt!capper gr8 july`, or `bt!capper gr8 WNBA july`")
        return

    capper_display = resolve_capper_name(tokens[0])
    remaining = tokens[1:]

    sport, remaining = extract_sport_token(remaining)
    period_label, period_sql, period_params = period_filter_from_tokens(remaining)

    where_parts = ["LOWER(capper) = ?"]
    params: List[object] = [capper_display.lower()]

    if sport:
        where_parts.append("UPPER(sport) = ?")
        params.append(sport)

    if period_sql:
        where_parts.append(period_sql)
        params.extend(period_params)

    title = f"Capper: {capper_display}"
    if sport:
        title += f" — {sport}"
    if period_label != "All-Time":
        title += f" — {period_label}"

    where_sql, final_params = combine_where(where_parts, params)
    await post_filtered_summary(ctx, title, where_sql, final_params)


@bot.command(name="player")
async def player_cmd(ctx: commands.Context, *, player_name: str) -> None:
    value = f"%{player_name.strip().lower()}%"
    await post_filtered_summary(
        ctx,
        f"Player Search: {player_name.strip()}",
        "LOWER(player) LIKE ? OR LOWER(content) LIKE ?",
        (value, value),
    )


@bot.command(name="bettype")
async def bettype_cmd(ctx: commands.Context, *, bet_type: str) -> None:
    value = f"%{bet_type.strip().lower()}%"
    await post_filtered_summary(
        ctx,
        f"Bet Type: {bet_type.strip()}",
        "LOWER(bet_type) LIKE ? OR LOWER(content) LIKE ?",
        (value, value),
    )


@bot.command(name="month")
async def month_cmd(ctx: commands.Context, ym: str) -> None:
    try:
        start = datetime.strptime(ym.strip(), "%Y-%m").date().replace(day=1)
    except Exception:
        await ctx.send("Use format: `bt!month 2026-07`")
        return

    start_l = datetime(start.year, start.month, 1, 0, 0, 0, tzinfo=_tz())
    if start.month == 12:
        end_l = datetime(start.year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
    else:
        end_l = datetime(start.year, start.month + 1, 1, 0, 0, 0, tzinfo=_tz())

    await post_filtered_summary(
        ctx,
        f"Month: {ym.strip()}",
        "created_utc >= ? AND created_utc < ?",
        (utc_iso(to_utc(start_l)), utc_iso(to_utc(end_l))),
    )


@bot.command(name="year")
async def year_cmd(ctx: commands.Context, yyyy: str) -> None:
    try:
        year = int(yyyy.strip())
    except Exception:
        await ctx.send("Use format: `bt!year 2026`")
        return

    start_l = datetime(year, 1, 1, 0, 0, 0, tzinfo=_tz())
    end_l = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())

    await post_filtered_summary(
        ctx,
        f"Year: {year}",
        "created_utc >= ? AND created_utc < ?",
        (utc_iso(to_utc(start_l)), utc_iso(to_utc(end_l))),
    )


@bot.command(name="range")
async def range_cmd(ctx: commands.Context, start_date: str, end_date: str) -> None:
    start_d = parse_date_yyyy_mm_dd(start_date)
    end_d = parse_date_yyyy_mm_dd(end_date)
    if not start_d or not end_d:
        await ctx.send("Use format: `bt!range 2026-07-01 2026-07-31`")
        return

    start_l = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=_tz())
    # Inclusive end date for user convenience.
    end_l = datetime(end_d.year, end_d.month, end_d.day, 0, 0, 0, tzinfo=_tz()) + timedelta(days=1)

    await post_filtered_summary(
        ctx,
        f"Range: {start_date} → {end_date}",
        "created_utc >= ? AND created_utc < ?",
        (utc_iso(to_utc(start_l)), utc_iso(to_utc(end_l))),
    )


# =====================
# COMMAND ERRORS
# =====================

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the **Manage Server** permission to run that command.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing argument. Example commands: `bt!sport WNBA`, `bt!player Paige Bueckers`, `bt!month 2026-07`")
        return
    raise error


# =====================
# AUTOPOST
# =====================

_last_post_key: Dict[str, Optional[str]] = {"daily": None, "weekly": None, "monthly": None, "yearly": None}


@tasks.loop(minutes=1)
async def autopost_loop() -> None:
    if not AUTOPOST_ENABLED:
        return

    nl = now_local()
    if nl.hour != AUTOPOST_HOUR_ET or nl.minute != AUTOPOST_MINUTE_ET:
        return

    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if channel is None:
        return

    def banner(t: str) -> str:
        return f"🔥 VIP RECAP — {t} 🔥"

    # Daily: yesterday
    yesterday = nl.date() - timedelta(days=1)
    day_key = yesterday.isoformat()
    if _last_post_key["daily"] != day_key:
        _last_post_key["daily"] = day_key
        start_l, end_l = period_bounds_local("daily", yesterday)
        await post_period_summary(channel, banner(f"DAILY ({day_key})"), start_l, end_l)

    # Weekly: previous week on Monday
    if nl.weekday() == WEEKLY_POST_WEEKDAY:
        prev_week_ref = nl.date() - timedelta(days=7)
        start_l, end_l = period_bounds_local("weekly", prev_week_ref)
        week_start = start_l.date()
        week_end = (end_l - timedelta(days=1)).date()
        week_key = f"{week_start.isoformat()}_{week_end.isoformat()}"
        if _last_post_key["weekly"] != week_key:
            _last_post_key["weekly"] = week_key
            await post_period_summary(channel, banner(f"WEEKLY ({week_start} → {week_end})"), start_l, end_l)

    # Monthly: previous month on the 1st
    if nl.day == MONTHLY_POST_DAY:
        prev_month_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("monthly", prev_month_ref)
        month_label = f"{start_l.year}-{start_l.month:02d}"
        if _last_post_key["monthly"] != month_label:
            _last_post_key["monthly"] = month_label
            await post_period_summary(channel, banner(f"MONTHLY ({month_label})"), start_l, end_l)

    # Yearly: previous year on Jan 1
    if nl.month == 1 and nl.day == 1:
        prev_year_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("yearly", prev_year_ref)
        year_label = f"{start_l.year}"
        if _last_post_key["yearly"] != year_label:
            _last_post_key["yearly"] = year_label
            await post_period_summary(channel, banner(f"YEARLY ({year_label})"), start_l, end_l)


if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable (set it in Render Environment).")

bot.run(TOKEN)
