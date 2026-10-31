"""
bot.py

Discord betting tracker using reaction grading, user-based capper ownership,
shared-channel tracking, DFS classification, leaderboards, and master reports.

Flow:
1) A registered capper posts in a dedicated capper channel or approved shared channel.
2) Bot reacts 📝 to mark it as pending (only if it contains units like 1u / .25u / 0.5u).
3) The capper grades by reacting:
   ✅ = Win
   ❌ = Loss
   ➖ = Push
   ↩️ = Refunded / Rebooted / Voided
4) Bot logs the result to SQLite and reacts 📌.
5) The original capper or PropKitchen admin can grade/regrade the play.
6) Removing the active grade returns the play to 📝 when no authorized grade remains.

Recaps:
- Auto-post Daily recap at 10:00 AM ET (yesterday) into SUMMARY_CHANNEL_ID.
- Commands for daily/weekly/monthly/yearly/all-time summaries.

Deployment:
- Use env var TOKEN (never commit token).
- Render persistent disk path: /var/data/bets.db
"""

from __future__ import annotations

import csv
import io
import os
import re
import shlex
import sqlite3
import time

from bettracker_autoclean import (
    DEFAULT_RETENTION_DAYS,
    defer_message,
    dequeue_message,
    disable_autoclean,
    due_messages,
    enqueue_message,
    ensure_autoclean_schema,
    get_retention_days,
    queued_count,
    set_retention_days,
)
from bettracker_excel import export_xlsx
from bettracker_ladders import is_ladder_post, parse_ladder_post
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from bettracker_sports import infer_sports_from_text
import matplotlib.pyplot as plt
from bettracker_visuals import generate_recap_card, generate_play_recap_card
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

DB_PATH = os.getenv("DB_PATH", "/var/data/bets.db")
REPORT_TZ = "America/New_York"

SUMMARY_CHANNEL_ID = 1473454134689796146  # #bettingtracker

AUTOPOST_ENABLED = True
AUTOPOST_HOUR_ET = 10
AUTOPOST_MINUTE_ET = 0
WEEKLY_POST_WEEKDAY = 0  # Monday
MONTHLY_POST_DAY = 3     # 3rd of month

PENDING_REACTION = "📝"
LOGGED_REACTION = "📌"

WIN_EMOJI = "✅"
LOSS_EMOJI = "❌"
PUSH_EMOJI = "➖"
VOID_EMOJI = "↩️"
GRADE_EMOJIS = {WIN_EMOJI, LOSS_EMOJI, PUSH_EMOJI, VOID_EMOJI}

DUPLICATE_REACTION = "⚠️"

ADMIN_USER_ID = 1230980936657535061

# Registered VIP cappers can be tracked in these shared channels by their Discord user ID.
SHARED_TRACKING_CHANNEL_IDS = {
    1387178991139553351,  # promo slips
    1279264580895510559,  # free plays
    1306857603598520352,  # giveaways
}

DUPLICATE_WINDOW_HOURS = 12
FORMAT_WARNING_DELETE_SECONDS = 60
DEFAULT_DFS_BACKFILL_DATE = "2026-07-01"

WAGER_STRAIGHT = "STRAIGHT"
WAGER_PARLAY = "SPORTSBOOK_PARLAY"
WAGER_DFS = "DFS"
WAGER_CATEGORIES = {WAGER_STRAIGHT, WAGER_PARLAY, WAGER_DFS}


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
    1492009669395480686: Capper("sgtprops", 495687263661850634),
}

# Preserve existing capper mappings and all historical records. Inactive
# accounts cannot create new tracked wagers, but past bets remain searchable
# and gradeable. Reactivate by removing the capper from this set.
# pxs = Picks x Stevie (departed); the others are on hiatus.
INACTIVE_CAPPER_NAMES = frozenset({"pxs", "daijonbets", "mikelocks", "mattlocks"})


# =====================
# DISCORD SETUP
# =====================

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True

bot = commands.Bot(
    command_prefix=("bt!", "Bt!", "bT!", "BT!"),
    case_insensitive=True,
    intents=intents,
)


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
    """Accept full ISO, US-slash, US-dash, and written calendar dates."""
    clean = (value or "").strip().replace(",", "")
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y",
                    "%m-%d-%Y", "%m-%d-%y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(clean, pattern).date()
        except ValueError:
            continue
    return None


def local_date_from_utc_iso(value: str) -> date:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_tz()).date()
    except Exception:
        return now_local().date()


def parse_bet_date(text: str, created_utc: str = "") -> str:
    """
    Return YYYY-MM-DD for the intended game/result date when the capper includes it.

    This fixes late-night posts like:
    MLB Prop #1 (7/10) posted at 11:42 PM on 7/9.

    If no clear date is written in the post, return blank and let grading time decide.
    """
    clean = text or ""
    base_day = local_date_from_utc_iso(created_utc) if created_utc else now_local().date()

    # Explicit ISO date wins, e.g. 2026-07-10.
    for m in RE_ISO_DATE.finditer(clean):
        d = parse_date_yyyy_mm_dd(m.group(1))
        if d:
            return d.isoformat()

    candidates: List[date] = []
    for m in RE_SLASH_DATE.finditer(clean):
        try:
            month = int(m.group(1))
            day = int(m.group(2))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue

            raw_year = m.group(3)
            if raw_year:
                year = int(raw_year)
                if year < 100:
                    year += 2000
                candidates.append(date(year, month, day))
                continue

            # No year written: choose the year closest to the message date.
            possible: List[date] = []
            for year in (base_day.year - 1, base_day.year, base_day.year + 1):
                try:
                    possible.append(date(year, month, day))
                except ValueError:
                    pass
            if possible:
                candidates.append(min(possible, key=lambda d: abs((d - base_day).days)))
        except Exception:
            continue

    # Avoid accidentally using stat text like 5/5 unless it is close to the posted date.
    close_candidates = [d for d in candidates if abs((d - base_day).days) <= 45]
    if close_candidates:
        return min(close_candidates, key=lambda d: abs((d - base_day).days)).isoformat()

    return ""


def bet_date_for_grade(content: str, created_utc: str, existing_bet_date: str = "") -> str:
    explicit = parse_bet_date(content, created_utc)
    if explicit:
        return explicit
    if existing_bet_date:
        return str(existing_bet_date)
    # No written game date: use the day it was graded/resulted in ET.
    return now_local().date().isoformat()


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
            guild_id INTEGER NOT NULL DEFAULT 0,
            channel_id INTEGER NOT NULL,
            capper TEXT NOT NULL,
            capper_user_id INTEGER NOT NULL,
            author_user_id INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            bet_date TEXT NOT NULL DEFAULT '',
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
            multiplier REAL,
            wager_category TEXT NOT NULL DEFAULT 'STRAIGHT',
            platform TEXT NOT NULL DEFAULT '',
            platform_type TEXT NOT NULL DEFAULT '',
            duplicate_key TEXT NOT NULL DEFAULT ''
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE NOT NULL,
            guild_id INTEGER NOT NULL DEFAULT 0,
            channel_id INTEGER NOT NULL,
            capper TEXT NOT NULL,
            capper_user_id INTEGER NOT NULL,
            author_user_id INTEGER NOT NULL DEFAULT 0,
            sport TEXT NOT NULL,
            risk_units REAL NOT NULL,
            net_units REAL NOT NULL,
            result TEXT NOT NULL,
            odds_text TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            graded_utc TEXT NOT NULL,
            bet_date TEXT NOT NULL DEFAULT '',
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
            wager_category TEXT NOT NULL DEFAULT 'STRAIGHT',
            platform TEXT NOT NULL DEFAULT '',
            platform_type TEXT NOT NULL DEFAULT '',
            duplicate_key TEXT NOT NULL DEFAULT '',
            grade_reaction TEXT NOT NULL DEFAULT '',
            grader_user_id INTEGER NOT NULL DEFAULT 0,
            admin_override INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Safe migrations for the existing Render database.
    pending_columns = {
        "guild_id": "INTEGER NOT NULL DEFAULT 0",
        "bet_date": "TEXT NOT NULL DEFAULT ''",
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
        "author_user_id": "INTEGER NOT NULL DEFAULT 0",
        "wager_category": "TEXT NOT NULL DEFAULT 'STRAIGHT'",
        "platform": "TEXT NOT NULL DEFAULT ''",
        "platform_type": "TEXT NOT NULL DEFAULT ''",
        "duplicate_key": "TEXT NOT NULL DEFAULT ''",
    }
    for col, definition in pending_columns.items():
        add_column_if_missing("pending", col, definition)

    bet_columns = {
        "guild_id": "INTEGER NOT NULL DEFAULT 0",
        "bet_date": "TEXT NOT NULL DEFAULT ''",
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
        "author_user_id": "INTEGER NOT NULL DEFAULT 0",
        "wager_category": "TEXT NOT NULL DEFAULT 'STRAIGHT'",
        "platform": "TEXT NOT NULL DEFAULT ''",
        "platform_type": "TEXT NOT NULL DEFAULT ''",
        "duplicate_key": "TEXT NOT NULL DEFAULT ''",
        "grader_user_id": "INTEGER NOT NULL DEFAULT 0",
        "admin_override": "INTEGER NOT NULL DEFAULT 0",
    }
    for col, definition in bet_columns.items():
        add_column_if_missing("bets", col, definition)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS correction_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL DEFAULT 0,
            table_name TEXT NOT NULL DEFAULT '',
            field_name TEXT NOT NULL DEFAULT '',
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            changed_by_user_id INTEGER NOT NULL DEFAULT 0,
            changed_utc TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_correction_message ON correction_audit(message_id);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recap_settings (
            guild_id INTEGER PRIMARY KEY,
            monthly_role_ids TEXT NOT NULL DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS autopost_history (
            cadence TEXT NOT NULL,
            period_key TEXT NOT NULL,
            posted_utc TEXT NOT NULL,
            PRIMARY KEY (cadence, period_key)
        )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_created_utc ON bets(created_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_date ON bets(bet_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_capper_time ON bets(capper, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport_time ON bets(sport, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_league_time ON bets(league, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_type_time ON bets(bet_type, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_player_time ON bets(player, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_wager_category ON bets(wager_category);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_platform ON bets(platform);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_duplicate_key ON bets(capper, duplicate_key, created_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_duplicate_key ON pending(capper, duplicate_key, created_utc);")
    conn.commit()
    # A separate lightweight queue; existing graded/pending wager data is unchanged.
    ensure_autoclean_schema(conn)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ladder_sources (
            source_message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            capper_user_id INTEGER NOT NULL,
            rung_count INTEGER NOT NULL,
            created_utc TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ladder_rungs (
            source_message_id INTEGER NOT NULL,
            rung_number INTEGER NOT NULL,
            tracked_message_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY (source_message_id, rung_number)
        )
    """)
    conn.commit()


ensure_schema()


# =====================
# PARSING PLAYS
# =====================

RE_UNITS = re.compile(r"(?i)(?<![\w.])((?:\d+(?:\.\d+)?|\.\d+))\s*u\b")
RE_AMERICAN_PAREN = re.compile(r"(?i)\(([+-]\d{2,5})(?:\s+[A-Za-z][A-Za-z .'-]*)?\)")
RE_AMERICAN = re.compile(r"(?i)(?<![\w.])([+-]\d{2,5})(?![\d.])")
RE_MULT = re.compile(r"(?i)(?<![\w.])((?:\d+(?:\.\d+)?|\.\d+))\s*x\b")
RE_DECIMAL_CONTEXT = re.compile(r"(?i)(?:\bodds?\s*[:=]?\s*|@\s*)(\d{1,3}(?:\.\d+)?)\b")
RE_DECIMAL_SUFFIX = re.compile(r"(?i)\b(\d{1,3}(?:\.\d+)?)\s*(?:decimal|dec)\b")
RE_LINE = re.compile(r"(?i)\b(?:over|under|o|u)\s*((?:\d+(?:\.\d+)?|\.\d+))\b")
RE_ISO_DATE = re.compile(r"(?<!\d)(20\d{2}-\d{1,2}-\d{1,2})(?!\d)")
RE_SLASH_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?!\d)")

SPORT_KEYWORDS = [
    ("NCAAB", ["NCAAB", "CBB", "COLLEGE BASKETBALL"]),
    ("WNBA", ["WNBA"]),
    ("NBA", ["NBA"]),
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

SPORT_EMOJIS = {
    "🏀": "BASKETBALL",
    "⚾": "MLB",
    "🏈": "FOOTBALL",
    "🏒": "NHL",
    "⚽": "SOCCER",
    "🎾": "TENNIS",
}

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
    ("Ligue 1", ["LIGUE 1"]),
    ("Liga MX", ["LIGA MX"]),
    ("Europa League", ["EUROPA LEAGUE"]),
    ("Conference League", ["CONFERENCE LEAGUE"]),
    ("Copa América", ["COPA AMERICA"]),
    ("Copa Libertadores", ["COPA LIBERTADORES"]),
    ("FA Cup", ["FA CUP"]),
    ("Carabao Cup", ["CARABAO CUP", "EFL CUP"]),
    ("ATP", ["ATP"]),
    ("WTA", ["WTA"]),
]

# Canonical DFS platform name followed by accepted aliases.
DFS_PLATFORM_RULES = [
    ("Chalkboard", ["CHALKBOARD"]),
    ("PrizePicks", ["PRIZEPICKS", "PRIZE PICKS"]),
    ("Underdog", ["UNDERDOG"]),
    ("Betr", ["BETR"]),
    ("Sleeper", ["SLEEPER"]),
    ("ParlayPlay", ["PARLAYPLAY", "PARLAY PLAY"]),
    ("Boom Sports", ["BOOM SPORTS", "BOOM"]),
    ("DK Pick6", ["DK PICK6", "DRAFTKINGS PICK6", "PICK6", "PICK 6"]),
    ("Dabble", ["DABBLE"]),
    ("Hotstreak", ["HOTSTREAK", "HOT STREAK"]),
    ("Smacktok", ["SMACKTOK", "SMACK TOK"]),
]

SPORTSBOOK_KEYWORDS = [
    ("Onyx", ["ONYX"]),
    ("Hard Rock", ["HARD ROCK"]),
    ("DraftKings", ["DRAFTKINGS"]),
    ("FanDuel", ["FANDUEL"]),
    ("BetMGM", ["BETMGM"]),
    ("Caesars", ["CAESARS"]),
    ("ESPN BET", ["ESPN BET"]),
]

BET_TYPE_RULES = [
    ("Parlay", ["PARLAY", "COMBO", "BUILDER", "SAME GAME PARLAY", "SGP"]),
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
    ("Spread", ["SPREAD"]),
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


def _contains_alias(text: str, alias: str) -> bool:
    pattern = r"(?<![A-Z0-9])" + re.escape(alias.upper()).replace(r"\ ", r"\s+") + r"(?![A-Z0-9])"
    return re.search(pattern, text.upper()) is not None


def infer_sports(text: str) -> List[str]:
    """Use specific tournaments/teams/players before generic emoji fallbacks.

    This is intentionally conservative: no sport is invented just because
    a capper posts an ambiguous team name or a lone 🏀/🏈 emoji.
    """
    return infer_sports_from_text(text)


def infer_sport(text: str) -> str:
    sports = infer_sports(text)
    return sports[0] if sports else "UNKNOWN"


def infer_league(text: str, sport: str) -> str:
    if sport == "MIXED":
        return "MIXED"
    upper = f" {text.upper()} "
    for league, keys in LEAGUE_KEYWORDS:
        if any(_contains_alias(upper, key) for key in keys):
            return league
    return sport if sport != "UNKNOWN" else ""


def infer_platform(text: str) -> Tuple[str, str]:
    for platform, aliases in DFS_PLATFORM_RULES:
        if any(_contains_alias(text, alias) for alias in aliases):
            return platform, "DFS_APP"

    for platform, aliases in SPORTSBOOK_KEYWORDS:
        if any(_contains_alias(text, alias) for alias in aliases):
            return platform, "SPORTSBOOK"

    return "", ""


def infer_sportsbook(text: str) -> str:
    platform, _platform_type = infer_platform(text)
    return platform


def infer_bet_type(text: str) -> str:
    upper = f" {text.upper()} "
    for bet_type, keys in BET_TYPE_RULES:
        if any(_contains_alias(upper, key) for key in keys):
            return bet_type
    return ""


def parse_risk_units(text: str) -> Optional[float]:
    match = RE_UNITS.search(text)
    if not match:
        return None
    try:
        units = float(match.group(1))
        return units if units > 0 else None
    except Exception:
        return None


def parse_odds_text(text: str) -> str:
    """Parse American, multiplier, or clearly labeled decimal odds."""
    match = RE_AMERICAN_PAREN.search(text)
    if match:
        return match.group(1)

    match = RE_MULT.search(text)
    if match:
        return f"{match.group(1)}x"

    match = RE_AMERICAN.search(text)
    if match:
        return match.group(1)

    for regex in (RE_DECIMAL_CONTEXT, RE_DECIMAL_SUFFIX):
        match = regex.search(text)
        if not match:
            continue
        try:
            decimal_odds = float(match.group(1))
        except Exception:
            continue
        if decimal_odds > 1.0:
            return match.group(1)

    return ""


def parse_manual_odds_text(value: str) -> str:
    clean = normalize_space(value).replace(" ", "")

    if re.fullmatch(r"[+-]\d{2,5}", clean):
        return clean

    if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)x", clean, flags=re.IGNORECASE):
        try:
            multiplier = float(clean[:-1])
        except Exception:
            return ""
        return f"{multiplier:g}x" if multiplier > 1.0 else ""

    if re.fullmatch(r"\d{1,3}(?:\.\d+)?", clean):
        try:
            decimal_odds = float(clean)
        except Exception:
            return ""
        if decimal_odds > 1.0:
            return f"{decimal_odds:.2f}"

    return ""


def parse_multiplier_value(odds_text: str) -> Optional[float]:
    if not str(odds_text or "").lower().endswith("x"):
        return None
    try:
        return float(str(odds_text)[:-1])
    except Exception:
        return None


def infer_odds_format(odds_text: str) -> str:
    clean = str(odds_text or "").strip()
    if not clean:
        return ""
    if clean.lower().endswith("x"):
        return "multiplier"
    if re.fullmatch(r"[+-]\d{2,5}", clean):
        return "american"
    try:
        decimal_odds = float(clean)
        if decimal_odds > 1.0 and "." in clean:
            return "decimal"
    except Exception:
        pass
    return ""


def infer_wager_category(text: str, odds_text: str, platform_type: str, bet_type: str) -> str:
    upper = text.upper()
    if (
        platform_type == "DFS_APP"
        or str(odds_text or "").lower().endswith("x")
        or re.search(r"(?<![A-Z0-9])DFS(?![A-Z0-9])", upper)
        or re.search(r"(?<![A-Z0-9])DFS\s+SLIP(?![A-Z0-9])", upper)
    ):
        return WAGER_DFS
    # A sportsbook/exchange "lotto" is a multi-leg parlay, even when
    # the capper does not write the word "parlay". A single-player
    # "double-double" prop alone is NOT evidence of multiple legs.
    if (
        bet_type == "Parlay"
        or re.search(r"(?<![A-Z0-9])(?:PARLAY|SGP|LOTTO)(?![A-Z0-9])", upper)
        or "SAME GAME" in upper and "BUILDER" in upper
    ):
        return WAGER_PARLAY
    return WAGER_STRAIGHT


def parse_line(text: str) -> str:
    match = RE_LINE.search(text)
    return match.group(1) if match else ""


def _remove_odds_and_dates(text: str) -> str:
    clean = RE_AMERICAN_PAREN.sub("", text)
    clean = RE_MULT.sub("", clean)
    clean = RE_AMERICAN.sub("", clean)
    clean = RE_DECIMAL_CONTEXT.sub("", clean)
    clean = RE_DECIMAL_SUFFIX.sub("", clean)
    clean = RE_ISO_DATE.sub("", clean)
    clean = RE_SLASH_DATE.sub("", clean)
    return clean


def parse_market(text: str) -> str:
    clean = normalize_space(text)
    clean = re.sub(r"<@!?&?\d+>", "", clean)
    clean = re.sub(r"https?://\S+", "", clean)
    clean = RE_UNITS.sub("", clean, count=1)
    clean = _remove_odds_and_dates(clean)
    clean = normalize_space(clean).strip(" -–—|:")
    return clean[:250]


def parse_player(text: str) -> str:
    clean = normalize_space(text)
    clean = RE_ISO_DATE.sub("", clean)
    clean = RE_SLASH_DATE.sub("", clean)
    clean = RE_UNITS.sub("", clean, count=1).strip(" -–—|:")
    upper = clean.upper()

    if "/" in clean or " VS " in f" {upper} " or "TEAM TOTAL" in upper or "TOTAL RUNS" in upper:
        return ""
    if "MONEYLINE" in upper or re.search(r"\bML\b", upper) or "SPREAD" in upper:
        return ""

    match = re.search(
        r"(?i)\b(?:over|under|o|u)\s*(?:\d|\.)|\b(?:to record|anytime|double double|triple double)\b",
        clean,
    )
    if not match:
        return ""

    candidate = clean[:match.start()].strip(" -–—|:")
    candidate = re.sub(
        r"(?i)\b(NBA|WNBA|MLB|NFL|NHL|NCAAB|CBB|SOCCER|TENNIS|DFS|MIXED)\b",
        "",
        candidate,
    )
    candidate = normalize_space(candidate).strip(" -–—|:")
    if not candidate or len(candidate) > 60 or len(candidate.split()) > 5:
        return ""
    return candidate


def _clean_description_fragment(value: str) -> str:
    clean = re.sub(r"<@!?&?\d+>", "", value or "")
    clean = re.sub(r"https?://\S+", "", clean)
    clean = RE_UNITS.sub("", clean)
    clean = _remove_odds_and_dates(clean)
    clean = re.sub(r"(?i)\b(?:odds?|date)\s*[:=]?", "", clean)
    clean = normalize_space(clean).strip(" -–—|:()")
    return clean


def _description_is_meaningful(value: str) -> bool:
    clean = normalize_space(value)
    if not clean:
        return False
    stripped = clean.upper()
    for token in ("DFS", "STRAIGHT", "MIXED"):
        stripped = re.sub(rf"\b{token}\b", "", stripped)
    stripped = re.sub(r"\b(?:MLB|WNBA|NBA|NFL|NCAAF|NCAAB|NHL|TENNIS|SOCCER)\b", "", stripped)
    stripped = stripped.replace("|", " ")
    return len(normalize_space(stripped)) >= 3


def _finalize_description(value: str) -> str:
    parts = [normalize_space(part).strip(" -–—|:()") for part in value.split("|")]
    cleaned_parts: List[str] = []
    removable = {
        "DFS", "STRAIGHT", "MIXED", "MLB", "WNBA", "NBA", "NFL", "NCAAF",
        "NCAAB", "NHL", "TENNIS", "SOCCER", "BASKETBALL", "FOOTBALL",
    }
    for part in parts:
        if not part:
            continue
        if part.upper() in removable:
            continue
        cleaned_parts.append(part)
    return " | ".join(cleaned_parts).strip(" -–—|:")


def extract_bet_description(content: str, market: str = "", max_len: int = 140) -> str:
    """
    Extract the human-readable wager description.

    It can use the line above the stake, which fixes posts such as:
    Sleeper Discount #1
    0.5u | 1.88x
    """
    raw = content or market or ""
    raw = re.sub(r"\r\n?", "\n", raw)
    lines = [line.strip() for line in raw.splitlines()]

    stake_index: Optional[int] = None
    for index, line in enumerate(lines):
        if RE_UNITS.search(line):
            stake_index = index
            break

    candidates: List[str] = []
    if stake_index is not None:
        same_line = _clean_description_fragment(lines[stake_index])
        if _description_is_meaningful(same_line):
            candidates.append(same_line)

        for index in range(stake_index - 1, -1, -1):
            previous = _clean_description_fragment(lines[index])
            if not previous:
                continue
            if re.fullmatch(r"(?:<@!?&?\d+>\s*)+", lines[index]):
                continue
            candidates.append(previous)
            break
    else:
        whole = _clean_description_fragment(raw)
        if whole:
            candidates.append(whole)

    if not candidates and market:
        candidates.append(_clean_description_fragment(market))

    chosen = _finalize_description(candidates[0]) if candidates else "Bet details unavailable"
    if len(chosen) > max_len:
        chosen = chosen[: max_len - 3].rstrip() + "..."
    return chosen or "Bet details unavailable"


def dfs_description_is_specific(description: str) -> bool:
    """Avoid blocking two different generic DFS slips that share the same stake/multiplier."""
    upper = description.upper()
    if any(signal in upper for signal in (" OVER ", " UNDER ", " O", " U", " MORE", " LESS", "+")):
        return True

    stripped = upper
    for _platform, aliases in DFS_PLATFORM_RULES:
        for alias in aliases:
            stripped = re.sub(
                r"(?<![A-Z0-9])" + re.escape(alias).replace(r"\ ", r"\s+") + r"(?![A-Z0-9])",
                " ",
                stripped,
            )
    stripped = re.sub(r"\b(?:DFS|SLIP|ENTRY|PICK|PICKS|DISCOUNT|FLEX|POWER|PLAY)\b", " ", stripped)
    stripped = re.sub(r"#?\d+(?:\.\d+)?", " ", stripped)
    stripped = re.sub(r"\b(?:MLB|WNBA|NBA|NFL|NHL|TENNIS|SOCCER|MIXED)\b", " ", stripped)
    meaningful_words = re.findall(r"[A-Z]{2,}", stripped)
    return len(meaningful_words) >= 2


def normalized_duplicate_description(description: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", description.lower()).strip()


def build_duplicate_key(
    content: str,
    created_utc: str,
    capper_name: str,
    risk_units: float,
    odds_text: str,
    fields: Dict[str, object],
) -> str:
    description = extract_bet_description(content, str(fields.get("market", "")))
    category = str(fields.get("wager_category", WAGER_STRAIGHT))
    if description == "Bet details unavailable":
        return ""
    if category == WAGER_DFS and not dfs_description_is_specific(description):
        return ""

    bet_day = parse_bet_date(content, created_utc)
    if not bet_day:
        bet_day = local_date_from_utc_iso(created_utc).isoformat()

    normalized_description = normalized_duplicate_description(description)
    if len(normalized_description) < 5:
        return ""

    return "|".join(
        [
            capper_name.lower(),
            bet_day,
            normalized_description,
            f"{float(risk_units):.4f}",
            str(odds_text or "").lower(),
            str(fields.get("line", "")).lower(),
            str(fields.get("sport", "")).lower(),
            str(fields.get("platform", "")).lower(),
        ]
    )


def parse_analytics_fields(text: str, odds_text: str) -> Dict[str, object]:
    platform, platform_type = infer_platform(text)
    bet_type = infer_bet_type(text)
    wager_category = infer_wager_category(text, odds_text, platform_type, bet_type)
    sports = infer_sports(text)

    # MIXED is an intentional multi-sport classification, not a missing sport.
    # Applies to both DFS slips and sportsbook parlays; never split a single
    # multi-sport payout across the sports in a performance recap.
    explicitly_mixed = re.search(r"(?<![A-Z0-9])MIXED(?![A-Z0-9])", text.upper()) is not None
    if explicitly_mixed or (len(sports) > 1 and wager_category in {WAGER_DFS, WAGER_PARLAY}):
        sport = "MIXED"
    elif len(sports) > 1:
        # A single straight bet cannot be both NFL and NBA. Ask the capper
        # instead of picking whichever clue happens to be listed first.
        sport = "UNKNOWN"
    else:
        sport = sports[0] if sports else "UNKNOWN"

    multiplier = parse_multiplier_value(odds_text)
    return {
        "sport": sport,
        "league": infer_league(text, sport),
        "event": "",
        "player": parse_player(text),
        "team": "",
        "opponent": "",
        "bet_type": bet_type,
        "market": parse_market(text),
        "line": parse_line(text),
        "sportsbook": platform,
        "odds_format": infer_odds_format(odds_text),
        "multiplier": multiplier,
        "wager_category": wager_category,
        "platform": platform,
        "platform_type": platform_type,
    }


def profit_from_american(risk: float, american: int) -> float:
    if american > 0:
        return risk * (american / 100.0)
    return risk * (100.0 / abs(american))


def profit_from_multiplier(risk: float, multiplier: float) -> float:
    return risk * (multiplier - 1.0)


def profit_from_decimal(risk: float, decimal_odds: float) -> float:
    return risk * (decimal_odds - 1.0)


def compute_net_units(risk: float, odds_text: str, result: str) -> float:
    result = result.lower()
    if result in {"push", "void"}:
        return 0.0
    if result == "loss":
        return -risk

    clean_odds = str(odds_text or "").strip()
    if not clean_odds:
        return risk

    if clean_odds.lower().endswith("x"):
        try:
            multiplier = float(clean_odds[:-1])
            return profit_from_multiplier(risk, multiplier) if multiplier > 1.0 else 0.0
        except Exception:
            return risk

    if re.fullmatch(r"[+-]\d{2,5}", clean_odds):
        try:
            return profit_from_american(risk, int(clean_odds))
        except Exception:
            return risk

    try:
        decimal_odds = float(clean_odds)
        if decimal_odds > 1.0:
            return profit_from_decimal(risk, decimal_odds)
    except Exception:
        pass

    return risk

def emoji_to_result(emoji: str) -> str:
    if emoji == WIN_EMOJI:
        return "win"
    if emoji == LOSS_EMOJI:
        return "loss"
    if emoji == VOID_EMOJI:
        return "void"
    return "push"

# =====================
# CHARTS
# =====================

def generate_units_chart(
    title: str,
    rows: List[Tuple[str, float, int, int, int]],
    overall: Optional[Dict[str, object]] = None,
    period_label: str = "",
) -> io.BytesIO:
    """Existing chart API now produces a reusable 4:5 PropKitchen social card."""
    return generate_recap_card(title, rows, summary=overall, period_label=period_label)


# =====================
# REPORTING
# =====================

WAGER_CATEGORY_SQL = """
CASE
    -- DFS is the highest-priority category, including old DFS slips.
    WHEN UPPER(COALESCE(wager_category, '')) = 'DFS'
      OR UPPER(COALESCE(platform_type, '')) = 'DFS_APP'
      OR LOWER(COALESCE(odds_text, '')) LIKE '%x'
        THEN 'DFS'
    -- A historical sportsbook lotto may have been stored as STRAIGHT.
    -- Correct its report classification without modifying any bet rows.
    WHEN UPPER(COALESCE(wager_category, '')) = 'SPORTSBOOK_PARLAY'
      OR LOWER(COALESCE(bet_type, '')) = 'parlay'
      OR LOWER(' ' || COALESCE(content, '') || ' ') LIKE '% lotto%'
      OR LOWER(' ' || COALESCE(content, '') || ' ') LIKE '% parlay%'
      OR LOWER(' ' || COALESCE(content, '') || ' ') LIKE '% sgp%'
        THEN 'SPORTSBOOK_PARLAY'
    ELSE 'STRAIGHT'
END
"""


def date_window_where(start_local: datetime, end_local: datetime) -> Tuple[str, Tuple[object, ...]]:
    start_date = start_local.date().isoformat()
    end_date = end_local.date().isoformat()
    start_utc = utc_iso(to_utc(start_local))
    end_utc = utc_iso(to_utc(end_local))
    return (
        "((bet_date >= ? AND bet_date < ?) OR "
        "((bet_date IS NULL OR bet_date = '') AND graded_utc >= ? AND graded_utc < ?))",
        (start_date, end_date, start_utc, end_utc),
    )


def fetch_capper_rows(start_local: datetime, end_local: datetime) -> List[Tuple[str, float, int, int, int]]:
    where_sql, params = date_window_where(start_local, end_local)
    rows = cur.execute(
        f"""
        SELECT
            capper,
            COALESCE(SUM(net_units), 0),
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END)
        FROM bets
        WHERE {where_sql}
        GROUP BY capper
        ORDER BY SUM(net_units) DESC
        """,
        params,
    ).fetchall()
    return [(str(c), float(u), int(w or 0), int(l or 0), int(p or 0)) for c, u, w, l, p in rows]


def fetch_vip_totals(start_local: datetime, end_local: datetime) -> Tuple[float, int, int, int]:
    where_sql, params = date_window_where(start_local, end_local)
    row = cur.execute(
        f"""
        SELECT
            COALESCE(SUM(net_units), 0),
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END)
        FROM bets
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    if not row:
        return 0.0, 0, 0, 0
    return float(row[0] or 0.0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)


def make_rows_text(rows: List[Tuple[str, float, int, int, int]]) -> str:
    if not rows:
        return "No graded bets found."
    output: List[str] = []
    for capper_name, net_units, wins, losses, pushes in rows:
        graded = wins + losses
        win_pct = wins / graded * 100.0 if graded else 0.0
        record = f"{wins}-{losses}" + (f"-{pushes}" if pushes else "")
        output.append(f"**{capper_name}**: {record} ({win_pct:.1f}%) | **{net_units:+.2f}u**")
    return "\n".join(output)


def make_vip_line(net_units: float, wins: int, losses: int, pushes: int) -> str:
    graded = wins + losses
    win_pct = wins / graded * 100.0 if graded else 0.0
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes else "")
    return f"**VIP TOTAL**: {record} ({win_pct:.1f}%) | **{net_units:+.2f}u**"


async def post_period_summary(
    channel: discord.abc.Messageable,
    title: str,
    start_local: datetime,
    end_local: datetime,
) -> None:
    where_sql, params = date_window_where(start_local, end_local)
    guild = getattr(channel, "guild", None)
    if guild is not None:
        where_sql = f"(guild_id = ?) AND ({where_sql})"
        params = (int(guild.id), *params)
    await post_leaderboard(
        channel,
        title,
        format_period_window(start_local, end_local),
        where_sql,
        params,
        include_chart=True,
    )


def format_record(wins: int, losses: int, pushes: int = 0) -> str:
    return f"{wins}-{losses}" + (f"-{pushes}" if pushes else "")


def format_void_suffix(voids: int) -> str:
    return f" | Voids: **{voids}**" if voids else ""


def fetch_filtered_totals(
    where_sql: str,
    params: Tuple[object, ...],
) -> Tuple[int, float, float, int, int, int, int]:
    row = cur.execute(
        f"""
        SELECT
            COUNT(*),
            COALESCE(SUM(CASE WHEN result = 'void' THEN 0 ELSE risk_units END), 0),
            COALESCE(SUM(net_units), 0),
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'void' THEN 1 ELSE 0 END)
        FROM bets
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    if not row:
        return 0, 0.0, 0.0, 0, 0, 0, 0
    return (
        int(row[0] or 0),
        float(row[1] or 0.0),
        float(row[2] or 0.0),
        int(row[3] or 0),
        int(row[4] or 0),
        int(row[5] or 0),
        int(row[6] or 0),
    )

def tracked_capper_display_names() -> List[str]:
    names: List[str] = []
    seen: set[str] = set()
    for capper in TRACKED_CHANNELS.values():
        key = capper.name.lower()
        if key in INACTIVE_CAPPER_NAMES:
            continue  # Hide zero-bet inactive cappers on current leaderboards.
        if key not in seen:
            seen.add(key)
            names.append(capper.name)
    return names


def fetch_leaderboard_rows(
    where_sql: str,
    params: Tuple[object, ...],
    include_zero_cappers: bool = True,
) -> List[Tuple[str, int, float, float, int, int, int, int, float, float]]:
    db_rows = cur.execute(
        f"""
        SELECT
            LOWER(capper),
            MAX(capper),
            COUNT(*),
            COALESCE(SUM(CASE WHEN result = 'void' THEN 0 ELSE risk_units END), 0),
            COALESCE(SUM(net_units), 0),
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'void' THEN 1 ELSE 0 END)
        FROM bets
        WHERE {where_sql}
        GROUP BY LOWER(capper)
        """,
        params,
    ).fetchall()

    stats: Dict[str, Tuple[str, int, float, float, int, int, int, int]] = {}
    for key, display, total, risk, net, wins, losses, pushes, voids in db_rows:
        stats[str(key)] = (
            str(display),
            int(total or 0),
            float(risk or 0.0),
            float(net or 0.0),
            int(wins or 0),
            int(losses or 0),
            int(pushes or 0),
            int(voids or 0),
        )

    ordered_names = tracked_capper_display_names()
    configured = {name.lower() for name in ordered_names}
    for key, values in stats.items():
        if key not in configured:
            ordered_names.append(values[0])

    rows: List[Tuple[str, int, float, float, int, int, int, int, float, float]] = []
    for name in ordered_names:
        values = stats.get(name.lower())
        if values is None:
            if not include_zero_cappers:
                continue
            total = 0
            risk = net = 0.0
            wins = losses = pushes = voids = 0
        else:
            _display, total, risk, net, wins, losses, pushes, voids = values

        graded = wins + losses
        win_pct = wins / graded * 100.0 if graded else 0.0
        roi = net / risk * 100.0 if risk else 0.0
        rows.append((name, total, risk, net, wins, losses, pushes, voids, win_pct, roi))

    rows.sort(
        key=lambda row: (
            row[1] == 0,
            -row[3],
            -row[9],
            -row[1],
            row[0].lower(),
        )
    )
    return rows

def format_period_window(start_local: datetime, end_local: datetime) -> str:
    inclusive_end = (end_local - timedelta(days=1)).date()
    start_day = start_local.date()
    if start_day == inclusive_end:
        return start_day.isoformat()
    if start_day.day == 1 and end_local.day == 1:
        if start_day.month == 1 and end_local.month == 1 and end_local.year == start_day.year + 1:
            return str(start_day.year)
        next_month = 1 if start_day.month == 12 else start_day.month + 1
        next_year = start_day.year + 1 if start_day.month == 12 else start_day.year
        if end_local.year == next_year and end_local.month == next_month:
            return f"{start_day.year}-{start_day.month:02d}"
    return f"{start_day.isoformat()} → {inclusive_end.isoformat()}"


def wager_category_label(category: str) -> str:
    normalized = str(category or "").upper()
    if normalized == WAGER_DFS:
        return "DFS Slips"
    if normalized == WAGER_PARLAY:
        return "Sportsbook / Exchange Parlays"
    return "Straight Bets"


def build_leaderboard_text(
    title: str,
    period_label: str,
    where_sql: str,
    params: Tuple[object, ...],
    sport_name: Optional[str] = None,
    league_name: Optional[str] = None,
    wager_category: Optional[str] = None,
) -> str:
    total, risk, net, wins, losses, pushes, voids = fetch_filtered_totals(where_sql, params)
    graded = wins + losses
    win_pct = wins / graded * 100.0 if graded else 0.0
    roi = net / risk * 100.0 if risk else 0.0
    record = format_record(wins, losses, pushes)

    lines = [f"📊 **{title}**", f"Period: **{period_label}**"]
    if sport_name:
        lines.append(f"Sport: **{sport_name}**")
    if league_name:
        lines.append(f"League: **{league_name}**")
    if wager_category:
        lines.append(f"Wager Type: **{wager_category_label(wager_category)}**")
    lines.extend(
        [
            "",
            "**VIP TOTAL**",
            f"Record: **{record}** ({win_pct:.1f}%) | Bets: **{total}**{format_void_suffix(voids)}",
            f"Risked: **{risk:.2f}u** | Net Units: **{net:+.2f}u** | ROI: **{roi:.1f}%**",
            "",
            "**Leaderboard — Sorted by Net Units**",
        ]
    )

    rows = fetch_leaderboard_rows(where_sql, params, include_zero_cappers=True)
    for rank, row in enumerate(rows, start=1):
        name, capper_total, capper_risk, capper_net, capper_w, capper_l, capper_p, capper_v, capper_wp, capper_roi = row
        capper_record = format_record(capper_w, capper_l, capper_p)
        if capper_total == 0:
            lines.append(f"{rank}. **{name}** — 0-0 | 0 bets | +0.00u")
            continue
        lines.append(f"{rank}. **{name}**")
        lines.append(
            f"Record: **{capper_record}** ({capper_wp:.1f}%) | Bets: **{capper_total}**"
            f"{format_void_suffix(capper_v)}"
        )
        lines.append(
            f"Risked: **{capper_risk:.2f}u** | Net Units: **{capper_net:+.2f}u** | "
            f"ROI: **{capper_roi:.1f}%**"
        )

    return "\n".join(lines)

async def post_leaderboard(
    channel: discord.abc.Messageable,
    title: str,
    period_label: str,
    where_sql: str,
    params: Tuple[object, ...],
    sport_name: Optional[str] = None,
    league_name: Optional[str] = None,
    wager_category: Optional[str] = None,
    include_chart: bool = False,
) -> None:
    text = build_leaderboard_text(
        title,
        period_label,
        where_sql,
        params,
        sport_name=sport_name,
        league_name=league_name,
        wager_category=wager_category,
    )
    await send_styled_report(channel, text, heading=f"VIP RESULTS  /  {period_label}")

    if include_chart:
        # No extra graphic for short single-day/single-sport VIP results.
        # Broader summaries use a real play list instead of empty leaderboard art.
        await send_optional_social_recap(
            channel, where_sql, params,
            title="PropKitchen VIP Results",
            filters=period_label + (f" · {sport_name}" if sport_name else "")
                    + (f" · {league_name}" if league_name else "")
                    + (f" · {wager_category_label(wager_category)}" if wager_category else ""),
            include_capper=True,
        )


def fetch_recent_bets(
    where_sql: str,
    params: Tuple[object, ...],
    limit: int = 75,
    offset: int = 0,
) -> List[Tuple[str, str, str, float, float, str, str, str, str, str, str, str]]:
    rows = cur.execute(
        f"""
        SELECT
            graded_utc, capper, result, risk_units, net_units, content, market,
            odds_text, sport,
            CASE
                WHEN jump_url LIKE 'https://discord.com/channels/%' THEN jump_url
                WHEN guild_id > 0 AND channel_id > 0 AND message_id > 0 THEN
                    'https://discord.com/channels/' || guild_id || '/' || channel_id || '/' || message_id
                ELSE COALESCE(jump_url, '')
            END AS jump_url,
            {WAGER_CATEGORY_SQL} AS normalized_category,
            COALESCE(platform, sportsbook, ''),
            COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) AS display_date
        FROM bets
        WHERE {where_sql}
        ORDER BY graded_utc DESC
        LIMIT ? OFFSET ?
        """,
        (*params, int(limit), int(offset)),
    ).fetchall()
    rows.reverse()  # Keep chronological display order for the newest matching bets.
    return [
        (
            str(graded),
            str(capper),
            str(result),
            float(risk),
            float(net),
            str(content or ""),
            str(market or ""),
            str(odds or ""),
            str(sport or "UNKNOWN"),
            str(jump or ""),
            str(category or WAGER_STRAIGHT),
            str(platform or ""),
            str(display_date or ""),
        )
        for graded, capper, result, risk, net, content, market, odds, sport, jump, category, platform, display_date in rows
    ]


def format_compact_number(value: float, max_decimals: int = 2) -> str:
    text = f"{float(value):.{max_decimals}f}".rstrip("0").rstrip(".")
    return text if text not in {"-0", ""} else "0"


def format_compact_units(value: float, signed: bool = False) -> str:
    number = format_compact_number(abs(value) if signed else value)
    if signed:
        sign = "+" if value > 0 else ("-" if value < 0 else "")
        return f"{sign}{number}u"
    return f"{number}u"


def format_odds_display(odds_text: str) -> str:
    return str(odds_text or "N/A")


def _find_odds_start(text: str) -> Optional[int]:
    matches: List[int] = []
    for regex in (RE_AMERICAN_PAREN, RE_MULT, RE_AMERICAN, RE_DECIMAL_CONTEXT, RE_DECIMAL_SUFFIX):
        match = regex.search(text)
        if match:
            matches.append(match.start())
    return min(matches) if matches else None


def display_bet_text(content: str, market: str, max_len: int = 95) -> str:
    return extract_bet_description(content, market, max_len=max_len)


def clean_period_label(label: Optional[str]) -> str:
    if not label or label == "All-Time":
        return "All-Time"
    exact = re.search(r"(20\d{2}-\d{2}-\d{2})", label)
    if exact and not label.startswith("Range:"):
        return exact.group(1)
    for prefix in ("Month: ", "Year: ", "Range: "):
        if label.startswith(prefix):
            return label.replace(prefix, "", 1)
    return label


def split_discord_text(text: str, limit: int = 1900) -> List[str]:
    chunks: List[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def make_report_embeds(text: str, heading: str = "BETTRACKER REPORT") -> List[discord.Embed]:
    """Split long reports into styled, legible Discord embeds."""
    reports: List[discord.Embed] = []
    for index, chunk in enumerate(split_discord_text(text, limit=3450)):
        embed = discord.Embed(
            title=(heading[:180] if index == 0 else f"{heading[:165]} — PAGE {index + 1}"),
            description=chunk,
            colour=discord.Colour.from_rgb(166, 230, 84),
        )
        embed.set_footer(text="PROPKITCHEN  •  BETTRACKER  |  Complete performance records")
        reports.append(embed)
    return reports


async def send_styled_report(
    destination: discord.abc.Messageable,
    text: str,
    heading: str,
) -> None:
    """Prefer styled embeds, but never lose results due to Embed Links permissions.

    Keep already-posted chunks when an individual embed fails; send the
    remaining chunks as plain text to avoid posting duplicate results.
    """
    chunks = split_discord_text(text, limit=3450)
    embeds = make_report_embeds(text, heading=heading)
    for index, embed in enumerate(embeds):
        try:
            await destination.send(embed=embed)
        except discord.HTTPException as exc:
            print(
                f"BetTracker embed unavailable; using plain text: "
                f"{type(exc).__name__} (status={getattr(exc, 'status', 'unknown')})"
            )
            for remaining in chunks[index:]:
                for plain_chunk in split_discord_text(remaining, limit=1800):
                    await destination.send(plain_chunk)
            return


def social_recap_eligible(where_sql: str, params: Tuple[object, ...]) -> bool:
    """Only create an extra image for substantive multi-day/multi-sport reports.

    A one-day, one-sport recap is complete in its Discord embed; attaching
    the old empty 4:5 sport-breakdown image just adds clutter.
    """
    row = cur.execute(
        f"""
        SELECT
            COUNT(*),
            COUNT(DISTINCT COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10))),
            COUNT(DISTINCT UPPER(COALESCE(NULLIF(sport, ''), 'UNKNOWN')))
        FROM bets
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    if not row:
        return False
    wagers, distinct_days, distinct_sports = (int(v or 0) for v in row)
    return wagers >= 6 and (distinct_days > 1 or distinct_sports > 1)


def social_recap_play_rows(
    where_sql: str, params: Tuple[object, ...], include_capper: bool = False,
) -> Tuple[List[Tuple[str, str]], int]:
    """Use existing graded data, without altering recorded wagers or ROI."""
    total = int(stats_values(where_sql, params)["total"])
    recent = fetch_recent_bets(where_sql, params, limit=16, offset=0)
    output: List[Tuple[str, str]] = []
    for row in recent:
        graded, capper, result, risk, net, content, market, odds, sport, jump, category, platform, bet_date = row
        if category == WAGER_DFS:
            description = f"{platform or 'DFS'} {odds or ''} slip".strip()
        else:
            description = compact_bet_description(content, market, max_len=125)
            # For shareable recaps, use complete bet language (not shorthand).
            description = re.sub(r"(?i)(?<![A-Z0-9])o(?=\d)", "over ", description)
            description = re.sub(r"(?i)(?<![A-Z0-9])u(?=\d)", "under ", description)
        if include_capper:
            description = f"{capper} · {description}"
        output.append((result, description))
    return output, total


async def send_optional_social_recap(
    destination: discord.abc.Messageable,
    where_sql: str,
    params: Tuple[object, ...],
    title: str,
    filters: str,
    include_capper: bool = False,
) -> None:
    """Optional visual enhancement only; the complete embed is always first."""
    try:
        if not social_recap_eligible(where_sql, params):
            return
        plays, total = social_recap_play_rows(
            where_sql, params, include_capper=include_capper,
        )
        if not plays:
            return
        image = generate_play_recap_card(
            title, stats_values(where_sql, params), plays, filters, total,
        )
        await destination.send(file=discord.File(
            image, filename="propkitchen_recap.png",
        ))
    except Exception as exc:
        print(f"Optional shareable recap graphic unavailable: {type(exc).__name__}: {exc}")


def stats_values(where_sql: str, params: Tuple[object, ...]) -> Dict[str, object]:
    total, risk, net, wins, losses, pushes, voids = fetch_filtered_totals(where_sql, params)
    graded = wins + losses
    return {
        "total": total,
        "risk": risk,
        "net": net,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "voids": voids,
        "win_pct": wins / graded * 100.0 if graded else 0.0,
        "roi": net / risk * 100.0 if risk else 0.0,
        "record": format_record(wins, losses, pushes),
    }

def append_stats(lines: List[str], stats: Dict[str, object]) -> None:
    voids = int(stats.get("voids", 0) or 0)
    lines.extend(
        [
            f"Record: **{stats['record']}** ({float(stats['win_pct']):.1f}%){format_void_suffix(voids)}",
            f"Risked: **{float(stats['risk']):.2f}u**",
            f"Net Units: **{float(stats['net']):+.2f}u**",
            f"ROI: **{float(stats['roi']):.1f}%**",
            f"Total Bets: **{int(stats['total'])}**",
        ]
    )

RECAP_MARKET_ABBREVIATIONS = (
    (r"(?i)\bpoints\b", "Pts"),
    (r"(?i)\brebounds\b", "Reb"),
    (r"(?i)\bassists\b", "Ast"),
    (r"(?i)\breceptions\b", "Rec"),
    (r"(?i)\breceiving\s+yards\b", "Rec Yds"),
    (r"(?i)\brushing\s+yards\b", "Rush Yds"),
    (r"(?i)\bpassing\s+yards\b", "Pass Yds"),
    (r"(?i)\bpassing\s+touchdowns\b", "Pass TDs"),
    (r"(?i)\brushing\s+touchdowns\b", "Rush TDs"),
    (r"(?i)\btotal\s+bases\b", "TB"),
    (r"(?i)\bstrikeouts\b", "Ks"),
    (r"(?i)\bshots\s+on\s+goal\b", "SOG"),
)


def compact_bet_description(content: str, market: str, max_len: int = 95) -> str:
    """Compact wager-only text. Remove odds, site labels and broken punctuation."""
    description = display_bet_text(content, market, max_len=max_len + 100)
    description = re.sub(r"<a?:[A-Za-z0-9_]+:\d+>", "", description)
    description = re.sub(r":[A-Za-z0-9_]+:", "", description)
    description = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", description)
    description = re.sub(r"\(\s*(?:\|)?\s*$", "", description)
    description = re.sub(r"\|\s*$", "", description)
    description = re.sub(
        r"(?i)\(\s*(?:Onyx|FanDuel|DraftKings|Hard Rock|BetMGM|"
        r"Caesars|Bet365|Fanatics|ESPN BET)\s*\)?\s*$", "", description
    )
    description = re.sub(
        r"(?i)(?:\s*\|\s*|\s+)(?:Onyx|FanDuel|DraftKings|Hard Rock|"
        r"BetMGM|Caesars|Bet365|Fanatics|ESPN BET)\s*$", "", description
    )
    description = re.sub(
        r"(?i)^(?:NBA|WNBA|NFL|NCAAF|CFB|CBB|NCAAB|MLB|NHL)\s+",
        "", description,
    )
    description = re.sub(r"\s*\|\s*", " ", description)
    description = re.sub(r"(?i)\bOVER\s*(?=\d|\.)", "o", description)
    description = re.sub(r"(?i)\bUNDER\s*(?=\d|\.)", "u", description)
    for pattern, replacement in RECAP_MARKET_ABBREVIATIONS:
        description = re.sub(pattern, replacement, description)
    description = normalize_space(description).strip(" |:()-")
    if len(description) > max_len:
        description = description[:max_len - 3].rstrip() + "..."
    return description or "Bet details unavailable"


def format_recap_wager_date(bet_date: str, graded_utc: str) -> str:
    """Short date label for DFS slips; prefer wager date over grading date."""
    day = str(bet_date or "").strip()
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", day):
        day = local_date_from_utc_iso(graded_utc).isoformat()
    try:
        parsed = date.fromisoformat(day)
        return f"{parsed.month}/{parsed.day}"
    except ValueError:
        return "Date N/A"


def build_bet_result_line(
    row: Tuple[str, ...],
    include_capper: bool = False,
) -> str:
    graded, capper, result, risk, net, content, market, odds, sport, jump_url, category, platform, bet_date = row
    icon = {"win": WIN_EMOJI, "loss": LOSS_EMOJI, "push": PUSH_EMOJI, "void": VOID_EMOJI}.get(result, PUSH_EMOJI)
    if category == WAGER_DFS:
        detail = [format_compact_units(risk), platform or "DFS Slip", format_odds_display(odds)]
    else:
        detail = [
            format_compact_units(risk),
            compact_bet_description(content, market),
            format_odds_display(odds),
        ]
    if include_capper:
        detail.insert(0, f"**{capper}**")
    detail.extend([
        format_compact_units(net, signed=True),
        f"[jump]({jump_url})"
        if str(jump_url or "").startswith("https://discord.com/channels/")
        else "jump unavailable",
    ])
    return f"{icon} " + " | ".join(detail)


SPORT_RECAP_ORDER = (
    "NBA", "WNBA", "NFL", "NCAAF", "MLB", "NHL", "NCAAB",
    "SOCCER", "TENNIS", "GOLF", "UFC", "MMA", "MIXED", "UNKNOWN",
)


SPORT_RECAP_EMOJI = {
    "NBA": "🏀", "WNBA": "🏀", "NCAAB": "🏀",
    "NFL": "🏈", "NCAAF": "🏈",
    "MLB": "⚾", "NHL": "🏒", "SOCCER": "⚽",
    "TENNIS": "🎾", "GOLF": "⛳", "UFC": "🥊",
    "MMA": "🥊", "BOXING": "🥊",
    "MIXED": "🧩", "UNKNOWN": "🔎",
}


def sport_recap_emoji(sport: str) -> str:
    return SPORT_RECAP_EMOJI.get(str(sport).upper(), "🎯")


def recap_sport_order(label: str) -> Tuple[int, str]:
    name = (label or "UNKNOWN").upper()
    return (
        SPORT_RECAP_ORDER.index(name) if name in SPORT_RECAP_ORDER else len(SPORT_RECAP_ORDER) - 2,
        name,
    )


def build_filtered_summary_text(
    title: str,
    where_sql: str,
    params: Tuple[object, ...],
    capper_name: Optional[str] = None,
    period_label: str = "All-Time",
    sport_name: Optional[str] = None,
    wager_category: Optional[str] = None,
    filter_label: Optional[str] = None,
    page: int = 1,
) -> str:
    """Sport-first recap; totals always include ALL bets, not just page rows."""
    overall = stats_values(where_sql, params)
    total = int(overall["total"])
    page_size = 75
    max_pages = max(1, (total + page_size - 1) // page_size)
    if page < 1 or page > max_pages:
        return f"That recap page does not exist. Use page 1 through {max_pages}."

    if capper_name:
        lines = [f"📊 **{capper_name} — {period_label}**"]
    else:
        lines = [f"📊 **{title}**"]
    if filter_label:
        lines.append(f"Filter: **{filter_label}**")
    if sport_name:
        lines.append(f"Sport: **{sport_name}**")
    if wager_category:
        lines.append(f"Wager Type: **{wager_category_label(wager_category)}**")

    lines.extend([
        "",
        f"**OVERALL** — **{overall['record']}** ({float(overall['win_pct']):.1f}%) | {total} bets",
        f"Risked: {float(overall['risk']):.2f}u | Net: **{float(overall['net']):+.2f}u** | ROI: **{float(overall['roi']):.1f}%**",
    ])
    if int(overall.get("voids", 0)):
        lines.append(f"Voids: {int(overall['voids'])}")
    if not total:
        return "\n".join(lines + ["No bets found for this selection."])

    bets = fetch_recent_bets(where_sql, params, limit=page_size, offset=(page - 1) * page_size)
    if max_pages == 1:
        lines.append("\n**PLAYS**")
    else:
        lines.append(f"\n**Wager details — page {page}/{max_pages}** ({len(bets)} of {total} bets)")
    if max_pages > 1:
        lines.append(f"To see other pages, repeat the command with `page 2`, `page 3`, etc. Complete export: `bt!export`.")

    sports = fetch_group_breakdown(
        where_sql, params, "UPPER(COALESCE(NULLIF(sport, ''), 'UNKNOWN'))",
    )
    for raw_sport, sport_stats in sorted(sports, key=lambda item: recap_sport_order(item[0])):
        sport = str(raw_sport or "UNKNOWN").upper()
        if sport == "MIXED":
            heading = "MIXED — Multi-Sport Slips"
        elif sport in {"UNKNOWN", "BASKETBALL", "FOOTBALL"}:
            heading = f"{sport} — Check classification"
        else:
            heading = sport
        lines.extend([
            "",
            f"{sport_recap_emoji(sport)} **{heading}**",
            f"Record: **{sport_stats['record']}** ({float(sport_stats['win_pct']):.1f}%)",
            f"Risked: {float(sport_stats['risk']):.2f}u | Net: **{float(sport_stats['net']):+.2f}u** | ROI: **{float(sport_stats['roi']):.1f}%**",
        ])
        # Full single-sport totals are counted here; a multi-sport slip belongs
        # to MIXED ONLY, never credited to two sport sections.
        category_where = f"({where_sql}) AND UPPER(COALESCE(NULLIF(sport, ''), 'UNKNOWN')) = ?"
        category_params = (*params, sport)
        for category in (WAGER_STRAIGHT, WAGER_DFS, WAGER_PARLAY):
            if wager_category is not None and category != wager_category:
                continue
            subset_where = f"({category_where}) AND ({WAGER_CATEGORY_SQL}) = ?"
            subset_params = (*category_params, category)
            stats = stats_values(subset_where, subset_params)
            if not int(stats["total"]):
                continue
            category_rows = [
                row for row in bets
                if str(row[8]).upper() == sport and str(row[10]).upper() == category
            ]
            lines.append(
                f"**{wager_category_label(category)}** — "
                f"{stats['record']} | {float(stats['net']):+.2f}u | "
                f"ROI {float(stats['roi']):.1f}%"
            )
            lines.extend(build_bet_result_line(row, include_capper=not bool(capper_name))
                         for row in category_rows)
            if not category_rows:
                lines.append("_Bets in this category appear on other pages._")

    return "\n".join(lines)


async def post_filtered_summary(
    ctx: commands.Context,
    title: str,
    where_sql: str,
    params: Tuple[object, ...],
    capper_name: Optional[str] = None,
    period_label: str = "All-Time",
    sport_name: Optional[str] = None,
    wager_category: Optional[str] = None,
    filter_label: Optional[str] = None,
    page: int = 1,
) -> None:
    text = build_filtered_summary_text(
        title,
        where_sql,
        params,
        capper_name=capper_name,
        period_label=period_label,
        sport_name=sport_name,
        wager_category=wager_category,
        filter_label=filter_label,
        page=page,
    )
    await send_styled_report(ctx, text, heading=f"{capper_name or 'VIP'}  /  RECAP")

    # Only substantive multi-day or multi-sport summaries get an extra image.
    # Images list actual plays, with complete odds/net/jump details above.
    if capper_name and page == 1:
        applied = [period_label]
        if sport_name:
            applied.append(sport_name)
        if wager_category:
            applied.append(wager_category_label(wager_category))
        if filter_label:
            applied.append(filter_label)
        await send_optional_social_recap(
            ctx, where_sql, params, capper_name, " · ".join(applied),
        )


def fetch_group_breakdown(
    where_sql: str,
    params: Tuple[object, ...],
    group_expression: str,
) -> List[Tuple[str, Dict[str, object]]]:
    rows = cur.execute(
        f"""
        SELECT
            {group_expression} AS group_name,
            COUNT(*),
            COALESCE(SUM(CASE WHEN result = 'void' THEN 0 ELSE risk_units END), 0),
            COALESCE(SUM(net_units), 0),
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END),
            SUM(CASE WHEN result = 'void' THEN 1 ELSE 0 END)
        FROM bets
        WHERE {where_sql}
        GROUP BY group_name
        ORDER BY COUNT(*) DESC, SUM(net_units) DESC
        """,
        params,
    ).fetchall()

    output: List[Tuple[str, Dict[str, object]]] = []
    for name, total, risk, net, wins, losses, pushes, voids in rows:
        total_i = int(total or 0)
        risk_f = float(risk or 0.0)
        net_f = float(net or 0.0)
        wins_i = int(wins or 0)
        losses_i = int(losses or 0)
        pushes_i = int(pushes or 0)
        voids_i = int(voids or 0)
        graded = wins_i + losses_i
        output.append(
            (
                str(name or "UNKNOWN"),
                {
                    "total": total_i,
                    "risk": risk_f,
                    "net": net_f,
                    "wins": wins_i,
                    "losses": losses_i,
                    "pushes": pushes_i,
                    "voids": voids_i,
                    "win_pct": wins_i / graded * 100.0 if graded else 0.0,
                    "roi": net_f / risk_f * 100.0 if risk_f else 0.0,
                    "record": format_record(wins_i, losses_i, pushes_i),
                },
            )
        )
    return output

def build_master_report_text(
    capper_name: str,
    period_label: str,
    where_sql: str,
    params: Tuple[object, ...],
) -> str:
    overall = stats_values(where_sql, params)
    lines = [
        "📊 **MASTER CAPPER REPORT**",
        f"Capper: **{capper_name}**",
        f"Period: **{period_label}**",
        "",
        "**OVERALL**",
    ]
    append_stats(lines, overall)

    lines.append("\n**SPORT BREAKDOWN**")
    sport_rows = fetch_group_breakdown(
        where_sql,
        params,
        "UPPER(COALESCE(NULLIF(sport, ''), 'UNKNOWN'))",
    )
    if not sport_rows:
        lines.append("No graded bets found.")
    for sport, stats in sport_rows:
        lines.append(f"\n**{sport}**")
        append_stats(lines, stats)

    lines.append("\n**WAGER TYPE BREAKDOWN**")
    category_rows = fetch_group_breakdown(where_sql, params, WAGER_CATEGORY_SQL)
    by_category = {name.upper(): stats for name, stats in category_rows}
    for category in (WAGER_STRAIGHT, WAGER_PARLAY, WAGER_DFS):
        stats = by_category.get(category)
        if not stats:
            continue
        lines.append(f"\n**{wager_category_label(category)}**")
        append_stats(lines, stats)

    return "\n".join(lines)


async def post_master_report(
    ctx: commands.Context,
    capper_name: str,
    time_text: str,
) -> None:
    label, start_local, end_local, error = parse_time_filter(time_text)
    if error:
        await ctx.send(error)
        return

    where_parts = ["LOWER(capper) = ?"]
    params: List[object] = [capper_name.lower()]
    if ctx.guild is not None:
        where_parts.append("guild_id = ?")
        params.append(int(ctx.guild.id))
    add_time_filter(where_parts, params, start_local, end_local)
    where_sql, final_params = build_where(where_parts, params)
    text = build_master_report_text(
        capper_name,
        clean_period_label(label),
        where_sql,
        final_params,
    )
    await send_styled_report(ctx, text, heading=f"{capper_name}  /  RECAP")


# =====================
# FILTER / DATE HELPERS
# =====================

MONTH_NAMES = {
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

SPORT_CODES = {code for code, _keys in SPORT_KEYWORDS} | {"MIXED", "BASKETBALL", "FOOTBALL"}

WAGER_CATEGORY_ALIASES = {
    "dfs": WAGER_DFS,
    "dfsslip": WAGER_DFS,
    "dfsslips": WAGER_DFS,
    "slip": WAGER_DFS,
    "slips": WAGER_DFS,
    "straight": WAGER_STRAIGHT,
    "straightbet": WAGER_STRAIGHT,
    "straightbets": WAGER_STRAIGHT,
    "sportsbookparlay": WAGER_PARLAY,
    "sportsbookparlays": WAGER_PARLAY,
    "parlay": WAGER_PARLAY,
    "parlays": WAGER_PARLAY,
}


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def capper_aliases() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for capper in TRACKED_CHANNELS.values():
        aliases[normalize_key(capper.name)] = capper.name

    aliases.setdefault("pk", "PropKitchen")
    aliases.setdefault("propkitchen", "PropKitchen")
    aliases.setdefault("matt", "mattlocks")
    aliases.setdefault("mattlocks", "mattlocks")
    aliases.setdefault("mike", "mikelocks")
    aliases.setdefault("mikelocks", "mikelocks")
    aliases.setdefault("bray", "betsbybray")
    aliases.setdefault("clip", "clipset")
    aliases.setdefault("clipset", "clipset")
    aliases.setdefault("daijon", "DaijonBets")
    aliases.setdefault("daijonbets", "DaijonBets")
    aliases.setdefault("stevie", "pxs")
    aliases.setdefault("picksxstevie", "pxs")
    aliases.setdefault("sgt", "sgtprops")
    return aliases


def capper_by_user_id(user_id: int) -> Optional[Capper]:
    for capper in TRACKED_CHANNELS.values():
        if capper.user_id == int(user_id) and capper.name.lower() not in INACTIVE_CAPPER_NAMES:
            return capper
    return None


def split_args(raw: str) -> List[str]:
    try:
        return shlex.split(raw or "")
    except Exception:
        return (raw or "").split()


def resolve_capper_from_tokens(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    aliases = capper_aliases()
    best_name: Optional[str] = None
    best_i = 0
    for i in range(1, min(len(tokens), 4) + 1):
        key = normalize_key(" ".join(tokens[:i]))
        if key in aliases:
            best_name = aliases[key]
            best_i = i

    if best_name is None:
        return None, tokens
    return best_name, tokens[best_i:]


def resolve_sport_from_tokens(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    if not tokens:
        return None, tokens

    first = tokens[0].upper()
    if first in SPORT_CODES:
        return first, tokens[1:]

    aliases = {
        "BASEBALL": "MLB",
        "FOOTBALL": "NFL",
        "BASKETBALL": "NBA",
        "SOCCER": "SOCCER",
        "TENNIS": "TENNIS",
        "HOCKEY": "NHL",
        "MIXED": "MIXED",
    }
    key = first.replace("-", "")
    if key in aliases:
        return aliases[key], tokens[1:]
    return None, tokens


def resolve_wager_category_from_tokens(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    if not tokens:
        return None, tokens
    key = normalize_key(tokens[0])
    category = WAGER_CATEGORY_ALIASES.get(key)
    if not category:
        return None, tokens
    return category, tokens[1:]


def resolve_optional_filters(
    tokens: List[str],
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Find one sport/category anywhere; preserve remaining date words."""
    remaining = []
    sport: Optional[str] = None
    category: Optional[str] = None
    for token in tokens:
        if sport is None:
            candidate, rest = resolve_sport_from_tokens([token])
            if candidate and not rest:
                sport = candidate
                continue
        if category is None:
            candidate, rest = resolve_wager_category_from_tokens([token])
            if candidate and not rest:
                category = candidate
                continue
        remaining.append(token)
    return sport, category, remaining


def local_day_bounds(day: date) -> Tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=_tz())
    return start, start + timedelta(days=1)


def local_month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=_tz())
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=_tz())
    return start, end


def parse_time_filter(raw: str, sport: Optional[str] = None) -> Tuple[Optional[str], Optional[datetime], Optional[datetime], Optional[str]]:
    text = normalize_space(raw).lower().replace("_", "-")

    if not text or text in {"all", "alltime", "all-time", "lifetime"}:
        return "All-Time", None, None, None

    today = now_local().date()

    if text in {"today", "todays"}:
        start, end = local_day_bounds(today)
        return f"Today ({today.isoformat()})", start, end, None

    if text in {"yesterday", "yday", "yst"}:
        day = today - timedelta(days=1)
        start, end = local_day_bounds(day)
        return f"Yesterday ({day.isoformat()})", start, end, None

    if text in {"thisweek", "this-week", "this week", "week"}:
        start, end = period_bounds_local("weekly", today)
        return f"This Week ({start.date()} → {(end - timedelta(days=1)).date()})", start, end, None

    if text in {"lastweek", "last-week", "last week"}:
        ref = today - timedelta(days=7)
        start, end = period_bounds_local("weekly", ref)
        return f"Last Week ({start.date()} → {(end - timedelta(days=1)).date()})", start, end, None

    if text in {"thismonth", "this-month", "this month", "month"}:
        start, end = period_bounds_local("monthly", today)
        return f"This Month ({start.year}-{start.month:02d})", start, end, None

    if text in {"lastmonth", "last-month", "last month"}:
        ref = today.replace(day=1) - timedelta(days=1)
        start, end = period_bounds_local("monthly", ref)
        return f"Last Month ({start.year}-{start.month:02d})", start, end, None

    # Sports seasons need an explicitly verified opening date. Never guess one.
    season_match = re.fullmatch(r"(?:season\s+)?(20\d{2})(?:-\d{2,4})?\s*(?:season)?", text)
    if season_match and "season" in text:
        year = int(season_match.group(1))
        starts = {("NBA", 2026): date(2026, 10, 20)}
        opening = starts.get(((sport or "").upper(), year))
        if opening is None:
            return None, None, None, (
                "Unknown sport/season. Try `bt!capper PropKitchen NBA 2026 season`."
            )
        start, _unused = local_day_bounds(opening)
        _unused, end = local_day_bounds(today)
        return f"{sport.upper()} {year}-{str(year+1)[-2:]} Season", start, end, None

    relative = re.fullmatch(r"(?:last|past)\s+(\d{1,3})\s*(?:days?|d)", text)
    if relative:
        count = int(relative.group(1))
        if not 1 <= count <= 365:
            return None, None, None, "Choose between 1 and 365 days."
        start, _unused = local_day_bounds(today - timedelta(days=count - 1))
        _unused, end = local_day_bounds(today)
        return f"Last {count} days", start, end, None

    named_month = re.fullmatch(r"([a-z]+)\s+(20\d{2})", text)
    if named_month and named_month.group(1) in MONTH_NAMES:
        year = int(named_month.group(2))
        month = MONTH_NAMES[named_month.group(1)]
        start, end = local_month_bounds(year, month)
        return f"Month: {year}-{month:02d}", start, end, None

    slash_month = re.fullmatch(r"(\d{1,2})/(20\d{2})", text)
    if slash_month and 1 <= int(slash_month.group(1)) <= 12:
        month, year = int(slash_month.group(1)), int(slash_month.group(2))
        start, end = local_month_bounds(year, month)
        return f"Month: {year}-{month:02d}", start, end, None

    range_text = re.sub(r"\s+(?:through|to|[-–])\s+|\s*→\s*", " ", text)
    range_tokens = range_text.split()
    if len(range_tokens) == 2:
        range_start = parse_date_yyyy_mm_dd(range_tokens[0])
        range_end = parse_date_yyyy_mm_dd(range_tokens[1])
        if range_start and range_end:
            if range_end < range_start:
                return None, None, None, "The ending date must be on or after the starting date."
            start, _ = local_day_bounds(range_start)
            _unused, end = local_day_bounds(range_end)
            return f"Range: {range_start.isoformat()} → {range_end.isoformat()}", start, end, None

    exact_day = parse_date_yyyy_mm_dd(text)
    if exact_day:
        start, end = local_day_bounds(exact_day)
        return f"Date: {exact_day.isoformat()}", start, end, None

    if re.fullmatch(r"\d{4}-\d{2}", text):
        try:
            month_start = datetime.strptime(text, "%Y-%m").date().replace(day=1)
            start, end = local_month_bounds(month_start.year, month_start.month)
            return f"Month: {text}", start, end, None
        except Exception:
            return None, None, None, "Use month format like `2026-07`."

    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        start = datetime(year, 1, 1, 0, 0, 0, tzinfo=_tz())
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=_tz())
        return f"Year: {year}", start, end, None

    if text in MONTH_NAMES:
        year = today.year
        month = MONTH_NAMES[text]
        start, end = local_month_bounds(year, month)
        return f"Month: {year}-{month:02d}", start, end, None

    return (
        None,
        None,
        None,
        f"Could not understand time filter `{raw}`. Try `today`, `yesterday`, "
        "`july`, `2026-07`, or `2026-07-09`.",
    )


def add_time_filter(
    where_parts: List[str],
    params: List[object],
    start_local: Optional[datetime],
    end_local: Optional[datetime],
) -> None:
    if start_local is None or end_local is None:
        return
    where_sql, date_params = date_window_where(start_local, end_local)
    where_parts.append(where_sql)
    params.extend(date_params)


def build_where(where_parts: List[str], params: List[object]) -> Tuple[str, Tuple[object, ...]]:
    if not where_parts:
        return "1 = 1", tuple()
    return " AND ".join(f"({part})" for part in where_parts), tuple(params)


async def post_query_summary(
    ctx: commands.Context,
    base_title: str,
    where_parts: List[str],
    params: List[object],
    time_text: str = "",
    capper_name: Optional[str] = None,
    sport_name: Optional[str] = None,
    wager_category: Optional[str] = None,
    filter_label: Optional[str] = None,
    page: int = 1,
) -> None:
    label, start_local, end_local, error = parse_time_filter(time_text, sport=sport_name)
    if error:
        await ctx.send(error)
        return

    if ctx.guild is not None:
        where_parts.append("guild_id = ?")
        params.append(int(ctx.guild.id))
    add_time_filter(where_parts, params, start_local, end_local)
    where_sql, final_params = build_where(where_parts, params)
    title = base_title if not label or label == "All-Time" else f"{base_title} — {label}"
    await post_filtered_summary(
        ctx,
        title,
        where_sql,
        final_params,
        capper_name=capper_name,
        period_label=clean_period_label(label),
        sport_name=sport_name,
        wager_category=wager_category,
        filter_label=filter_label,
        page=page,
    )


async def post_leaderboard_query(
    ctx: commands.Context,
    time_text: str = "",
    sport_name: Optional[str] = None,
    league_name: Optional[str] = None,
    wager_category: Optional[str] = None,
    include_chart: bool = False,
) -> None:
    label, start_local, end_local, error = parse_time_filter(time_text, sport=sport_name)
    if error:
        await ctx.send(error)
        return

    where_parts: List[str] = []
    params: List[object] = []
    if ctx.guild is not None:
        where_parts.append("guild_id = ?")
        params.append(int(ctx.guild.id))
    if sport_name:
        where_parts.append("UPPER(sport) = ?")
        params.append(sport_name.upper())
    if league_name:
        where_parts.append("LOWER(league) = ?")
        params.append(league_name.lower())
    if wager_category:
        where_parts.append(f"({WAGER_CATEGORY_SQL}) = ?")
        params.append(wager_category)

    add_time_filter(where_parts, params, start_local, end_local)
    where_sql, final_params = build_where(where_parts, params)

    title = "VIP CAPPER LEADERBOARD"
    if sport_name:
        title = f"VIP {sport_name.upper()} LEADERBOARD"
    elif league_name:
        title = f"VIP {league_name} LEADERBOARD"
    elif wager_category:
        title = f"VIP {wager_category_label(wager_category).upper()} LEADERBOARD"

    await post_leaderboard(
        ctx,
        title,
        clean_period_label(label),
        where_sql,
        final_params,
        sport_name=sport_name,
        league_name=league_name,
        wager_category=wager_category,
        include_chart=include_chart,
    )


def split_name_and_time_filter(query: str) -> Tuple[str, str]:
    tokens = split_args(query)
    if not tokens:
        return "", ""

    for suffix_len in range(min(5, len(tokens)), 0, -1):
        candidate = " ".join(tokens[-suffix_len:])
        label, _start, _end, error = parse_time_filter(candidate)
        if error is None and label != "All-Time":
            name = " ".join(tokens[:-suffix_len]).strip()
            if name:
                return name, candidate
    return " ".join(tokens).strip(), ""


# =====================
# PENDING + GRADING
# =====================

async def safe_add_reaction(message: discord.Message, emoji: str) -> bool:
    try:
        await message.add_reaction(emoji)
        return True
    except Exception:
        return False

async def safe_clear_reaction(message: discord.Message, emoji: str) -> bool:
    try:
        await message.clear_reaction(emoji)
        return True
    except Exception:
        return False

async def send_temporary_notice(message: discord.Message, text: str) -> None:
    try:
        await message.reply(
            text,
            mention_author=False,
            delete_after=FORMAT_WARNING_DELETE_SECONDS,
        )
    except Exception:
        return


def is_bot_command_message(message: discord.Message) -> bool:
    content = (message.content or "").lstrip().lower()
    return content.startswith(("bt!",))


def looks_like_wager_message(message: discord.Message, content: str) -> bool:
    if is_bot_command_message(message) or getattr(message, "reference", None) is not None:
        return False  # Replies and commands are not candidates for missing-units warnings.
    if RE_UNITS.search(content):
        return True
    upper = f" {content.upper()} "
    betting_signals = (
        " OVER ", " UNDER ", " PARLAY", " SGP ", " DFS ",
        " MONEYLINE", " SPREAD", " STRIKEOUT", " REBOUND", " ASSIST",
        " POINTS", " TOTAL BASE", " HITS ALLOWED", " PRA ", " MORE ", " LESS ",
    )
    # Odds alone, mentions of a sportsbook, or an unrelated image are not
    # sufficient evidence that this is a malformed wager.
    has_bet_context = any(signal in upper for signal in betting_signals) or bool(infer_platform(content)[0])
    return has_bet_context and bool(parse_odds_text(content))


def format_warnings_for_tracked_post(content: str, created_utc: str) -> List[str]:
    warnings: List[str] = []
    odds_text = parse_odds_text(content)
    fields = parse_analytics_fields(content, odds_text)
    category = str(fields.get("wager_category", WAGER_STRAIGHT))
    platform = str(fields.get("platform", ""))
    sport = str(fields.get("sport", "UNKNOWN"))
    description = extract_bet_description(content, str(fields.get("market", "")))

    if category == WAGER_DFS:
        if not platform:
            warnings.append("DFS app/platform is missing")
        if not str(odds_text).lower().endswith("x"):
            warnings.append("DFS multiplier must end in `x` (example: `2.4x`)")
    elif not odds_text:
        warnings.append("odds are missing, so a win would calculate as even money")

    if sport in {"", "UNKNOWN", "FOOTBALL", "BASKETBALL", "SOFTBALL"}:
        warnings.append(
            "couldn't identify the specific sport/league; add `NFL`, `CFB`, "
            "`NBA`, `WNBA`, `CBB`, etc. (a lone 🏈/🏀 can be ambiguous)"
        )
    if description == "Bet details unavailable":
        warnings.append("wager details are too vague")
    return warnings


async def warn_about_tracked_format(message: discord.Message, content: str) -> None:
    if getattr(message, "reference", None) is not None:
        return  # Do not clutter reply threads with automatic format feedback.
    warnings = format_warnings_for_tracked_post(content, utc_iso(message.created_at))
    if not warnings:
        return
    details = "\n".join(f"• {warning}" for warning in warnings)
    await send_temporary_notice(
        message,
        "⚠️ **Tracked, but please fix this post before grading:**\n"
        f"{details}\n"
        "Edit the original message. The pending bet will refresh automatically.",
    )


def pending_exists(message_id: int) -> bool:
    return cur.execute("SELECT 1 FROM pending WHERE message_id = ?", (message_id,)).fetchone() is not None


def bet_exists(message_id: int) -> bool:
    return cur.execute("SELECT 1 FROM bets WHERE message_id = ?", (message_id,)).fetchone() is not None


def is_trackable_channel(channel_id: int) -> bool:
    return int(channel_id) in TRACKED_CHANNELS or int(channel_id) in SHARED_TRACKING_CHANNEL_IDS


def resolve_capper_for_message(message: discord.Message) -> Optional[Capper]:
    """
    In approved channels, direct Discord posts belong to the registered capper user.
    Dedicated-channel webhooks keep the old channel-based fallback.
    """
    if not is_trackable_channel(message.channel.id):
        return None

    channel_capper = TRACKED_CHANNELS.get(int(message.channel.id))
    if channel_capper and channel_capper.name.lower() in INACTIVE_CAPPER_NAMES:
        return None  # Pause new posts in inactive capper dedicated channels.

    direct_capper = capper_by_user_id(message.author.id)
    if direct_capper:
        return direct_capper

    if message.webhook_id is not None and message.channel.id in TRACKED_CHANNELS:
        return TRACKED_CHANNELS[message.channel.id]

    return None


def find_duplicate_message_id(
    message_id: int,
    guild_id: int,
    capper_name: str,
    duplicate_key: str,
    created_utc: str,
) -> Optional[int]:
    if not duplicate_key:
        return None

    try:
        created = datetime.fromisoformat(created_utc.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except Exception:
        created = datetime.now(timezone.utc)

    cutoff = utc_iso(created.astimezone(timezone.utc) - timedelta(hours=DUPLICATE_WINDOW_HOURS))
    upper = utc_iso(created.astimezone(timezone.utc) + timedelta(seconds=1))

    for table in ("pending", "bets"):
        row = cur.execute(
            f"""
            SELECT message_id
            FROM {table}
            WHERE guild_id = ?
              AND LOWER(capper) = ?
              AND duplicate_key = ?
              AND created_utc >= ?
              AND created_utc <= ?
              AND message_id != ?
            ORDER BY created_utc DESC
            LIMIT 1
            """,
            (int(guild_id), capper_name.lower(), duplicate_key, cutoff, upper, int(message_id)),
        ).fetchone()
        if row:
            return int(row[0])
    return None


def insert_pending(
    message_id: int,
    guild_id: int,
    channel_id: int,
    capper: Capper,
    author_user_id: int,
    content: str,
    created_utc: str,
    jump_url: str,
) -> Tuple[bool, Optional[int]]:
    risk = parse_risk_units(content)
    if risk is None:
        return False, None

    odds_text = parse_odds_text(content)
    fields = parse_analytics_fields(content, odds_text)
    # When no future/event date is supplied, assume the posting day in ET.
    # Cappers posting advance picks should explicitly include the game date.
    bet_date = parse_bet_date(content, created_utc) or local_date_from_utc_iso(created_utc).isoformat()
    duplicate_key = build_duplicate_key(content, created_utc, capper.name, risk, odds_text, fields)
    duplicate_message_id = find_duplicate_message_id(
        message_id,
        guild_id,
        capper.name,
        duplicate_key,
        created_utc,
    )
    if duplicate_message_id is not None:
        return False, duplicate_message_id

    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (
            message_id, guild_id, channel_id, capper, capper_user_id, author_user_id,
            content, created_utc, bet_date, sport, risk_units, odds_text, jump_url,
            league, event, player, team, opponent, bet_type, market, line,
            sportsbook, odds_format, multiplier, wager_category, platform,
            platform_type, duplicate_key
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(guild_id),
            int(channel_id),
            capper.name,
            int(capper.user_id),
            int(author_user_id),
            content,
            created_utc,
            bet_date,
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
            str(fields["wager_category"]),
            str(fields["platform"]),
            str(fields["platform_type"]),
            duplicate_key,
        ),
    )
    conn.commit()
    return True, None


def refresh_pending_from_message(message: discord.Message, capper: Capper) -> bool:
    if not pending_exists(message.id):
        return False

    content = message_to_text(message)
    risk = parse_risk_units(content)
    if risk is None:
        return False

    odds_text = parse_odds_text(content)
    fields = parse_analytics_fields(content, odds_text)
    created_utc = utc_iso(message.created_at)
    bet_date = parse_bet_date(content, created_utc) or local_date_from_utc_iso(created_utc).isoformat()
    duplicate_key = build_duplicate_key(content, created_utc, capper.name, risk, odds_text, fields)

    cur.execute(
        """
        UPDATE pending
        SET guild_id = ?, capper = ?, capper_user_id = ?, author_user_id = ?, content = ?,
            bet_date = ?, sport = ?, risk_units = ?, odds_text = ?, jump_url = ?,
            league = ?, event = ?, player = ?, team = ?, opponent = ?, bet_type = ?,
            market = ?, line = ?, sportsbook = ?, odds_format = ?, multiplier = ?,
            wager_category = ?, platform = ?, platform_type = ?, duplicate_key = ?
        WHERE message_id = ?
        """,
        (
            int(message.guild.id if message.guild else 0),
            capper.name,
            int(capper.user_id),
            int(message.author.id),
            content,
            bet_date,
            str(fields["sport"]),
            float(risk),
            odds_text,
            message.jump_url,
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
            str(fields["wager_category"]),
            str(fields["platform"]),
            str(fields["platform_type"]),
            duplicate_key,
            int(message.id),
        ),
    )
    conn.commit()
    return True


def owner_user_id_for_message(message_id: int) -> Optional[int]:
    row = cur.execute(
        "SELECT capper_user_id FROM pending WHERE message_id = ?",
        (int(message_id),),
    ).fetchone()
    if not row:
        row = cur.execute(
            "SELECT capper_user_id FROM bets WHERE message_id = ?",
            (int(message_id),),
        ).fetchone()
    return int(row[0]) if row else None


def authorized_to_grade(user_id: int, owner_user_id: int) -> bool:
    return int(user_id) in {int(owner_user_id), int(ADMIN_USER_ID)}


def grade_pending(
    message_id: int,
    result: str,
    grade_reaction: str,
    grader_user_id: int,
) -> bool:
    row = cur.execute(
        """
        SELECT
            guild_id, channel_id, capper, capper_user_id, author_user_id, content, sport,
            risk_units, odds_text, created_utc, bet_date, jump_url, league, event,
            player, team, opponent, bet_type, market, line, sportsbook, odds_format,
            multiplier, wager_category, platform, platform_type, duplicate_key
        FROM pending
        WHERE message_id = ?
        """,
        (int(message_id),),
    ).fetchone()
    if not row:
        return False

    (
        guild_id,
        channel_id,
        capper_name,
        capper_user_id,
        author_user_id,
        content,
        sport,
        risk,
        odds_text,
        created_utc,
        bet_date,
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
        wager_category,
        platform,
        platform_type,
        duplicate_key,
    ) = row

    parsed_risk = parse_risk_units(str(content or ""))
    if parsed_risk is not None:
        risk = parsed_risk

    parsed_odds = parse_odds_text(str(content or ""))
    if parsed_odds:
        odds_text = parsed_odds

    fields = parse_analytics_fields(str(content or ""), str(odds_text or ""))
    if str(fields["sport"]) != "UNKNOWN":
        sport = fields["sport"]
    league = fields["league"] or league
    player = fields["player"] or player
    bet_type = fields["bet_type"] or bet_type
    market = fields["market"] or market
    line = fields["line"] or line
    sportsbook = fields["sportsbook"] or sportsbook
    odds_format = fields["odds_format"] or odds_format
    multiplier = fields["multiplier"] if fields["multiplier"] is not None else multiplier
    wager_category = fields["wager_category"]
    platform = fields["platform"] or platform
    platform_type = fields["platform_type"] or platform_type

    bet_date = bet_date_for_grade(str(content or ""), str(created_utc), str(bet_date or ""))
    net = compute_net_units(float(risk), str(odds_text or ""), result)
    admin_override = int(int(grader_user_id) == int(ADMIN_USER_ID) and int(grader_user_id) != int(capper_user_id))

    cur.execute(
        """
        INSERT OR REPLACE INTO bets
        (
    message_id, guild_id, channel_id, capper, capper_user_id, author_user_id, sport,
            risk_units, net_units, result, odds_text, created_utc, graded_utc,
            bet_date, content, jump_url, league, event, player, team, opponent,
            bet_type, market, line, sportsbook, odds_format, multiplier,
            wager_category, platform, platform_type, duplicate_key, grade_reaction,
            grader_user_id, admin_override
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(guild_id),
            int(channel_id),
            str(capper_name),
            int(capper_user_id),
            int(author_user_id or capper_user_id),
            str(sport or "UNKNOWN"),
            float(risk),
            float(net),
            str(result),
            str(odds_text or ""),
            str(created_utc),
            utc_iso(datetime.now(timezone.utc)),
            str(bet_date or ""),
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
            str(wager_category or WAGER_STRAIGHT),
            str(platform or ""),
            str(platform_type or ""),
            str(duplicate_key or ""),
            str(grade_reaction),
            int(grader_user_id),
            admin_override,
        ),
    )
    cur.execute("DELETE FROM pending WHERE message_id = ?", (int(message_id),))
    conn.commit()
    return True


def regrade_bet(
    message_id: int,
    result: str,
    grade_reaction: str,
    grader_user_id: int,
) -> bool:
    row = cur.execute(
        """
        SELECT risk_units, odds_text, content, created_utc, bet_date, capper_user_id
        FROM bets
        WHERE message_id = ?
        """,
        (int(message_id),),
    ).fetchone()
    if not row:
        return False

    risk, odds_text, content, created_utc, existing_bet_date, capper_user_id = row
    bet_date = bet_date_for_grade(
        str(content or ""),
        str(created_utc or ""),
        str(existing_bet_date or ""),
    )
    net = compute_net_units(float(risk), str(odds_text or ""), result)
    admin_override = int(int(grader_user_id) == int(ADMIN_USER_ID) and int(grader_user_id) != int(capper_user_id))

    cur.execute(
        """
        UPDATE bets
        SET result = ?, net_units = ?, graded_utc = ?, bet_date = ?,
            grade_reaction = ?, grader_user_id = ?, admin_override = ?
        WHERE message_id = ?
        """,
        (
            str(result),
            float(net),
            utc_iso(datetime.now(timezone.utc)),
            str(bet_date or ""),
            str(grade_reaction),
            int(grader_user_id),
            admin_override,
            int(message_id),
        ),
    )
    conn.commit()
    return True


def ungrade_bet(message_id: int) -> bool:
    row = cur.execute(
        """
        SELECT
            channel_id, capper, capper_user_id, author_user_id, content, sport,
            risk_units, odds_text, created_utc, bet_date, jump_url, league, event,
            player, team, opponent, bet_type, market, line, sportsbook, odds_format,
            multiplier, wager_category, platform, platform_type, duplicate_key, guild_id
        FROM bets
        WHERE message_id = ?
        """,
        (int(message_id),),
    ).fetchone()
    if not row:
        return False

    cur.execute("DELETE FROM bets WHERE message_id = ?", (int(message_id),))
    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (
            message_id, channel_id, capper, capper_user_id, author_user_id,
            content, created_utc, bet_date, sport, risk_units, odds_text, jump_url,
            league, event, player, team, opponent, bet_type, market, line,
            sportsbook, odds_format, multiplier, wager_category, platform,
            platform_type, duplicate_key, guild_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3] or row[2]),
            str(row[4] or ""),
            str(row[8] or ""),
            str(row[9] or ""),
            str(row[5] or "UNKNOWN"),
            float(row[6]),
            str(row[7] or ""),
            str(row[10] or ""),
            str(row[11] or ""),
            str(row[12] or ""),
            str(row[13] or ""),
            str(row[14] or ""),
            str(row[15] or ""),
            str(row[16] or ""),
            str(row[17] or ""),
            str(row[18] or ""),
            str(row[19] or ""),
            str(row[20] or ""),
            row[21],
            str(row[22] or WAGER_STRAIGHT),
            str(row[23] or ""),
            str(row[24] or ""),
            str(row[25] or ""),
            int(row[26]),
        ),
    )
    conn.commit()
    return True


async def find_remaining_authorized_grade(
    message: discord.Message,
    owner_user_id: int,
) -> Optional[Tuple[str, int]]:
    candidates: List[Tuple[str, int]] = []
    for reaction in message.reactions:
        emoji = str(reaction.emoji)
        if emoji not in GRADE_EMOJIS:
            continue
        try:
            async for user in reaction.users(limit=None):
                if user.id in {owner_user_id, ADMIN_USER_ID}:
                    candidates.append((emoji, int(user.id)))
        except Exception:
            continue

    # Prefer the owner when both still have valid reactions. This makes removing an
    # admin override naturally fall back to the capper's own grade.
    for emoji, user_id in candidates:
        if user_id == owner_user_id:
            return emoji, user_id
    return candidates[0] if candidates else None


# =====================
# COMMAND-CHANNEL AUTO-CLEAN (OPT-IN)
# =====================

def is_autoclean_protected_channel(channel: discord.abc.GuildChannel) -> bool:
    """Never delete betting posts or curated recap/results channels."""
    channel_id = int(channel.id)
    channel_name = str(getattr(channel, "name", "")).lower()
    return (
        channel_id == SUMMARY_CHANNEL_ID
        or is_trackable_channel(channel_id)
        or "recap" in channel_name
        or "result" in channel_name
    )


def maybe_queue_command_message(message: discord.Message) -> None:
    """Track only new command-channel messages, not old chat history."""
    if message.guild is None or is_autoclean_protected_channel(message.channel):
        return
    is_own_reply = bot.user is not None and message.author.id == bot.user.id
    is_user_command = (not message.author.bot) and is_bot_command_message(message)
    if not (is_own_reply or is_user_command):
        return
    try:
        enqueue_message(
            conn,
            int(message.guild.id),
            int(message.channel.id),
            int(message.id),
            int(message.created_at.timestamp()),
        )
    except sqlite3.Error as exc:
        print(f"Autoclean queue error: {type(exc).__name__}: {exc}")


@bot.command(name="autoclean", aliases=["commandcleanup", "cleancommands"])
@commands.has_permissions(manage_guild=True)
async def autoclean_cmd(
    ctx: commands.Context, action: str = "status", duration: str = "3d"
) -> None:
    """Manage this command channel's cleanup independently for each Discord server."""
    if ctx.guild is None:
        await ctx.send("Run this in the Discord server's command channel.")
        return

    gid, cid = int(ctx.guild.id), int(ctx.channel.id)
    mode = action.strip().lower()
    if mode in {"status", "check"}:
        days = get_retention_days(conn, gid, cid)
        queued = queued_count(conn, gid, cid)
        if days is None:
            await ctx.send(
                "🧹 **Command Auto-Clean: OFF** for this channel.\n"
                "Enable with `bt!autoclean on 3d` (Manage Server required).\n"
                "The recap and capper channels are never cleaned."
            )
        else:
            await ctx.send(
                f"🧹 **Command Auto-Clean: ON** for <#{cid}>\n"
                f"Retention: **{days} day(s)** | Scheduled: **{queued} messages**\n"
                "Removes new `bt!` commands and BetTracker replies only.\n"
                "Use `bt!autoclean off` to disable and cancel scheduled removals."
            )
        return

    if mode in {"off", "disable"}:
        queued_removed = disable_autoclean(conn, gid, cid)
        await ctx.send(
            f"✅ **Auto-Clean OFF** for <#{cid}>.\n"
            f"Cancelled **{queued_removed}** scheduled message deletions. "
            "Messages already deleted cannot be restored."
        )
        return

    if mode not in {"on", "enable"}:
        await ctx.send(
            "Use `bt!autoclean status`, `bt!autoclean on 3d`, "
            "or `bt!autoclean off`. Retention supports **1–30 days**."
        )
        return

    match = re.fullmatch(r"(\d{1,2})(?:d|days?)?", duration.strip().lower())
    if not match or not 1 <= int(match.group(1)) <= 30:
        await ctx.send("Choose **1–30 days**, for example `bt!autoclean on 3d`.")
        return
    if is_autoclean_protected_channel(ctx.channel):
        await ctx.send(
            "🚫 Auto-Clean cannot be enabled in a betting, recap, or results channel. "
            "Run it in your dedicated bot-commands channel instead."
        )
        return

    member = ctx.guild.me or (
        ctx.guild.get_member(bot.user.id) if bot.user is not None else None
    )
    perms = ctx.channel.permissions_for(member) if member is not None else None
    if not perms or not (
        perms.view_channel and perms.read_message_history
        and perms.manage_messages and perms.send_messages
    ):
        await ctx.send(
            "⚠️ Auto-Clean was **not enabled**. Give BetTracker "
            "**View Channel**, **Read Message History**, **Send Messages**, "
            "and **Manage Messages** permission in this channel."
        )
        return

    days = int(match.group(1))
    set_retention_days(conn, gid, cid, days)
    maybe_queue_command_message(ctx.message)
    await ctx.send(
        f"✅ **Auto-Clean ON** for <#{cid}> — **{days} day(s)**.\n"
        "New `bt!` commands and BetTracker replies/files posted **in this channel** "
        "will be removed after the selected period.\n"
        "Older messages, original bets, and posts in recap/results channels are safe. "
        "Use `bt!autoclean off` at any time."
    )


@tasks.loop(minutes=5)
async def autoclean_loop() -> None:
    """Best-effort cleanup of explicitly queued IDs; never sweep channel history."""
    now_epoch = int(time.time())
    for guild_id, channel_id, message_id in due_messages(
        conn, now_epoch, limit=30
    ):
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if channel is None:
            defer_message(conn, guild_id, channel_id, message_id, now_epoch + 3600)
            continue
        if is_autoclean_protected_channel(channel):
            # Channel was changed into a betting/recap channel after queuing.
            # Cancel the deletion instead of retaining a risky queued job.
            dequeue_message(conn, guild_id, channel_id, message_id)
            continue

        member = guild.me or (
            guild.get_member(bot.user.id) if bot.user is not None else None
        )
        perms = channel.permissions_for(member) if member is not None else None
        if not perms or not (
            perms.view_channel and perms.read_message_history and perms.manage_messages
        ):
            defer_message(conn, guild_id, channel_id, message_id, now_epoch + 3600)
            continue

        try:
            msg = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            await msg.delete()
        except discord.NotFound:
            # Message was already removed manually; no reason to retry.
            pass
        except discord.Forbidden:
            defer_message(conn, guild_id, channel_id, message_id, now_epoch + 3600)
            continue
        except discord.HTTPException as exc:
            print(f"Autoclean delete retry later: {type(exc).__name__}")
            defer_message(conn, guild_id, channel_id, message_id, now_epoch + 3600)
            continue
        dequeue_message(conn, guild_id, channel_id, message_id)


@autoclean_loop.before_loop
async def before_autoclean_loop() -> None:
    await bot.wait_until_ready()


# =====================
# MULTI-RUNG LADDER TRACKING
# =====================

def ladder_source_exists(message_id: int) -> bool:
    return cur.execute(
        "SELECT 1 FROM ladder_sources WHERE source_message_id = ?",
        (int(message_id),),
    ).fetchone() is not None


async def track_ladder_post(message: discord.Message, capper: Capper) -> bool:
    """One original capper post -> one separately gradeable bot reply per rung."""
    content = message_to_text(message)
    if not is_ladder_post(content):
        return False
    try:
        ladder = parse_ladder_post(content)
    except ValueError as exc:
        await send_temporary_notice(
            message,
            f"🪜 **Ladder not tracked:** {exc}\n"
            "Add NFL, CFB, NBA, etc. to the heading and keep 2–8 "
            "individually priced lines. Nothing was saved.",
        )
        return True

    if message.guild is None:
        return True

    # A lone football/basketball emoji is not an unambiguous league.
    context = ladder.preface
    specific_sports = [
        item for item in infer_sports(context)
        if item not in {"FOOTBALL", "BASKETBALL", "SOFTBALL"}
    ]
    if len(specific_sports) != 1:
        await send_temporary_notice(
            message,
            "🪜 **Ladder not tracked:** include one clear league or player "
            "in the heading (NFL, CFB, NBA, etc.). "
            "This avoids recording an ambiguous ladder in the wrong sport.",
        )
        return True

    # Persist source BEFORE replies to prevent duplicate event submissions.
    cursor = cur.execute(
        """
        INSERT OR IGNORE INTO ladder_sources
        (source_message_id, guild_id, channel_id, capper_user_id, rung_count, created_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(message.id), int(message.guild.id), int(message.channel.id),
            int(capper.user_id), len(ladder.rungs), utc_iso(message.created_at),
        ),
    )
    conn.commit()
    if cursor.rowcount != 1:
        return True

    successes = 0
    for rung in ladder.rungs:
        inherited = f"{ladder.preface}\n{rung.line}"
        reply_text = (
            f"🪜 **LADDER {rung.number}/{len(ladder.rungs)} — "
            f"{capper.name}**\n{inherited}"
        )
        try:
            child = await message.reply(
                reply_text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            print(f"Could not publish ladder rung: {type(exc).__name__}: {exc}")
            break

        inserted, duplicate_id = insert_pending(
            child.id, int(message.guild.id), int(message.channel.id),
            capper, message.author.id, inherited, utc_iso(message.created_at),
            child.jump_url,
        )
        if not inserted:
            try:
                await child.delete()
            except discord.HTTPException:
                pass
            print(f"Ladder rung duplicate/invalid: source={message.id} rung={rung.number}")
            continue

        cur.execute(
            """INSERT OR IGNORE INTO ladder_rungs
               (source_message_id, rung_number, tracked_message_id)
               VALUES (?, ?, ?)""",
            (int(message.id), rung.number, int(child.id)),
        )
        conn.commit()
        successes += 1
        await safe_add_reaction(child, PENDING_REACTION)

    if successes == len(ladder.rungs):
        await safe_add_reaction(message, "🪜")
    else:
        await send_temporary_notice(
            message,
            f"⚠️ **Ladder partially tracked:** {successes}/{len(ladder.rungs)} "
            "rungs were saved. Do not repost or grade the original ladder. "
            "Grade only the individual rung replies, and contact an admin "
            "to resolve any missing rungs.",
        )
    return True


# =====================
# EVENTS
# =====================

@bot.check
async def require_server_context(ctx: commands.Context) -> bool:
    # Reports and exports must never query all servers from a direct message.
    return ctx.guild is not None


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")
    if AUTOPOST_ENABLED and not autopost_loop.is_running():
        autopost_loop.start()
    if not autoclean_loop.is_running():
        autoclean_loop.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    # The same channel-specific retention policy applies to every capper's
    # bt! command and to BetTracker's own responses (including exported files).
    maybe_queue_command_message(message)
    if bot.user and message.author.id == bot.user.id:
        return

    if is_bot_command_message(message):
        await bot.process_commands(message)
        return

    if not is_trackable_channel(message.channel.id):
        await bot.process_commands(message)
        return

    capper = resolve_capper_for_message(message)
    if capper is None:
        await bot.process_commands(message)
        return

    is_webhook_post = message.webhook_id is not None
    if message.author.bot and not is_webhook_post:
        await bot.process_commands(message)
        return

    if pending_exists(message.id) or bet_exists(message.id):
        await bot.process_commands(message)
        return

    content = message_to_text(message)
    if is_ladder_post(content):
        await track_ladder_post(message, capper)
        await bot.process_commands(message)
        return

    if parse_risk_units(content) is None:
        if looks_like_wager_message(message, content):
            await send_temporary_notice(
                message,
                "❌ **Bet not tracked: units are missing.**\n"
                "Start the post with a stake such as `1u`, `0.5u`, or `0.25u`.",
            )
        await bot.process_commands(message)
        return

    inserted, duplicate_message_id = insert_pending(
        message.id,
        message.guild.id if message.guild else 0,
        message.channel.id,
        capper,
        message.author.id,
        content,
        utc_iso(message.created_at),
        message.jump_url,
    )
    if inserted:
        reaction_added = await safe_add_reaction(message, PENDING_REACTION)
        if not reaction_added:
            await send_temporary_notice(
                message,
                "⚠️ **The bet was saved, but I could not add 📝.**\n"
                "Give BetTracker the **Add Reactions** and **Read Message History** permissions in this channel.",
            )
        await warn_about_tracked_format(message, content)
    elif duplicate_message_id is not None:
        await safe_add_reaction(message, DUPLICATE_REACTION)

    await bot.process_commands(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if bot.user and after.author.id == bot.user.id:
        return
    # Ignore Discord embed/link-preview updates with unchanged wager text.
    if message_to_text(before) == message_to_text(after):
        return
    if is_bot_command_message(after):
        return
    if not is_trackable_channel(after.channel.id) or bet_exists(after.id):
        return
    if ladder_source_exists(after.id):
        await send_temporary_notice(
            after, "🪜 This ladder already created separately tracked replies. "
            "Editing the original does not update their odds or stakes. "
            "Contact an admin to correct a rung.",
        )
        return

    capper = resolve_capper_for_message(after)
    if capper is None:
        return

    content = message_to_text(after)
    if is_ladder_post(content):
        await track_ladder_post(after, capper)
        return
    if pending_exists(after.id):
        refresh_pending_from_message(after, capper)
        return

    # Corrected edits remain silent. Tracking happens only when units are supplied.
    if parse_risk_units(content) is None:
        return

    inserted, duplicate_message_id = insert_pending(
        after.id,
        after.guild.id if after.guild else 0,
        after.channel.id,
        capper,
        after.author.id,
        content,
        utc_iso(after.created_at),
        after.jump_url,
    )
    if inserted:
        await safe_clear_reaction(after, DUPLICATE_REACTION)
        await safe_add_reaction(after, PENDING_REACTION)
    elif duplicate_message_id is not None:
        await safe_add_reaction(after, DUPLICATE_REACTION)

@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
    # on_message_edit handles cached messages. Avoid processing the same
    # update twice and sending duplicate warning replies.
    if getattr(payload, "cached_message", None) is not None:
        return
    if not is_trackable_channel(payload.channel_id) or bet_exists(payload.message_id):
        return
    if ladder_source_exists(payload.message_id):
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    if is_bot_command_message(message):
        return

    capper = resolve_capper_for_message(message)
    if capper is None:
        return

    content = message_to_text(message)
    if is_ladder_post(content):
        await track_ladder_post(message, capper)
        return
    if pending_exists(payload.message_id):
        refresh_pending_from_message(message, capper)
        return

    # Avoid chat spam for raw/background Discord edit events.
    if parse_risk_units(content) is None:
        return

    inserted, duplicate_message_id = insert_pending(
        message.id,
        message.guild.id if message.guild else 0,
        message.channel.id,
        capper,
        message.author.id,
        content,
        utc_iso(message.created_at),
        message.jump_url,
    )
    if inserted:
        await safe_clear_reaction(message, DUPLICATE_REACTION)
        await safe_add_reaction(message, PENDING_REACTION)
    elif duplicate_message_id is not None:
        await safe_add_reaction(message, DUPLICATE_REACTION)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if not bot.user or payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    if emoji not in GRADE_EMOJIS or not is_trackable_channel(payload.channel_id):
        return

    owner_user_id = owner_user_id_for_message(payload.message_id)
    if owner_user_id is None or not authorized_to_grade(payload.user_id, owner_user_id):
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    result = emoji_to_result(emoji)
    if pending_exists(payload.message_id):
        if not grade_pending(payload.message_id, result, emoji, payload.user_id):
            return
    elif bet_exists(payload.message_id):
        if not regrade_bet(payload.message_id, result, emoji, payload.user_id):
            return
    else:
        return

    await safe_clear_reaction(message, PENDING_REACTION)
    await safe_add_reaction(message, LOGGED_REACTION)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    if not bot.user:
        return

    emoji = str(payload.emoji)
    if emoji not in GRADE_EMOJIS or not is_trackable_channel(payload.channel_id):
        return

    row = cur.execute(
        """
        SELECT capper_user_id, grader_user_id, grade_reaction
        FROM bets
        WHERE message_id = ?
        """,
        (int(payload.message_id),),
    ).fetchone()
    if not row:
        return

    owner_user_id = int(row[0])
    active_grader_user_id = int(row[1] or owner_user_id)
    active_reaction = str(row[2] or "")
    if not authorized_to_grade(payload.user_id, owner_user_id):
        return

    # Removing an old/non-active authorized reaction should not erase the current grade.
    if int(payload.user_id) != active_grader_user_id or emoji != active_reaction:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    remaining = await find_remaining_authorized_grade(message, owner_user_id)
    if remaining:
        remaining_emoji, remaining_user_id = remaining
        result = emoji_to_result(remaining_emoji)
        if regrade_bet(payload.message_id, result, remaining_emoji, remaining_user_id):
            await safe_add_reaction(message, LOGGED_REACTION)
        return

    if not ungrade_bet(payload.message_id):
        return

    await safe_clear_reaction(message, LOGGED_REACTION)
    await safe_add_reaction(message, PENDING_REACTION)


# =====================
# COMMANDS
# =====================

@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


@bot.command(name="version", aliases=["build", "botversion"])
async def version_cmd(ctx: commands.Context) -> None:
    """Report the actual Render branch and commit serving Discord requests."""
    branch = os.getenv("RENDER_GIT_BRANCH", "unavailable")
    commit = os.getenv("RENDER_GIT_COMMIT", "")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        commit = "unavailable"
    else:
        commit = commit[:12]
    await ctx.send(
        "**BetTracker Live Build**\n"
        f"Branch: `{branch}`\n"
        f"Commit: `{commit}`\n"
        "Release: **November 2026 + report diagnostics**"
    )


@bot.command(name="trackcheck", aliases=["checktracking", "channelcheck"])
async def trackcheck_cmd(ctx: commands.Context) -> None:
    if ctx.guild is None or bot.user is None:
        await ctx.send("`bt!trackcheck` must be run inside the Discord server.")
        return

    channel_id = int(ctx.channel.id)
    approved = is_trackable_channel(channel_id)
    dedicated_capper = TRACKED_CHANNELS.get(channel_id)
    shared = channel_id in SHARED_TRACKING_CHANNEL_IDS
    posting_capper = capper_by_user_id(ctx.author.id)

    bot_member = ctx.guild.get_member(bot.user.id) or ctx.guild.me
    if bot_member is None:
        await ctx.send("I could not resolve the bot's server permissions.")
        return
    perms = ctx.channel.permissions_for(bot_member)  # type: ignore[attr-defined]

    checks = {
        "View Channel": bool(getattr(perms, "view_channel", False)),
        "Read Message History": bool(getattr(perms, "read_message_history", False)),
        "Add Reactions": bool(getattr(perms, "add_reactions", False)),
        "Send Messages": bool(getattr(perms, "send_messages", False)),
        "Attach Files": bool(getattr(perms, "attach_files", False)),
    }
    permission_lines = "\n".join(
        f"{'✅' if allowed else '❌'} {name}" for name, allowed in checks.items()
    )

    channel_type = "Approved shared tracking channel" if shared else (
        f"Dedicated channel for {dedicated_capper.name}" if dedicated_capper else "Not an approved tracking channel"
    )
    direct_ready = approved and posting_capper is not None and checks["View Channel"] and checks["Read Message History"]
    reaction_ready = checks["Add Reactions"]

    lines = [
        "🔎 **BetTracker Channel Check**",
        f"Channel: **#{getattr(ctx.channel, 'name', 'unknown')}**",
        f"Channel ID: `{channel_id}`",
        f"Status: **{channel_type}**",
        f"Your capper account: **{posting_capper.name if posting_capper else 'Not registered'}**",
        "",
        "**Bot Permissions**",
        permission_lines,
        "",
        f"Direct post can be saved: **{'YES' if direct_ready else 'NO'}**",
        f"📝 reaction can be added: **{'YES' if reaction_ready else 'NO'}**",
    ]
    if direct_ready and not reaction_ready:
        lines.append("⚠️ Bets can enter the database, but cappers will not see 📝 until Add Reactions is enabled.")
    elif not approved:
        lines.append("Fix: add this channel ID to the approved tracking-channel configuration.")
    elif posting_capper is None:
        lines.append("Fix: this Discord account must be registered to a capper.")
    await ctx.send("\n".join(lines))


@bot.command(name="format", aliases=["template", "postformat"])
async def format_cmd(ctx: commands.Context, wager_type: str = "") -> None:
    today = now_local().date()
    short_date = f"{today.month}/{today.day}"
    key = normalize_key(wager_type)
    templates = {
        "straight": f"1u | MLB | Player o5.5 Strikeouts | Odds: -120 | {short_date}",
        "parlay": f"0.5u | MLB | Parlay: Leg 1 + Leg 2 | Odds: +180 | {short_date}",
        "dfs": f"0.5u | DFS | Sleeper | Leg 1 + Leg 2 | 1.88x | MLB | {short_date}",
    }
    if key in templates:
        await ctx.send(f"🧾 **{key.upper()} FORMAT**\n{templates[key]}")
        return
    await ctx.send(
        "🧾 **POSTING FORMATS**\n"
        f"Straight: {templates['straight']}\n"
        f"Parlay: {templates['parlay']}\n"
        f"DFS: {templates['dfs']}\n\n"
        "Use `bt!format straight`, `bt!format parlay`, or `bt!format dfs`."
    )


@bot.command(name="commands", aliases=["command", "cmds", "guide"])
async def commands_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "📌 **BetTracker Quick Commands**\n"
        "Commands are not case-sensitive.\n\n"
        "**Team Results**\n"
        "`bt!today` | `bt!yesterday` | `bt!date 2026-07-13`\n"
        "`bt!weekly` | `bt!month 2026-07` | `bt!year 2026` | `bt!alltime`\n"
        "`bt!range 2026-07-01 2026-07-31`\n\n"
        "**Capper Results / Searches**\n"
        "`bt!capper PropKitchen july`\n"
        "`bt!capper PropKitchen strikeouts july`\n"
        "`bt!capper PropKitchen player Zack Wheeler july`\n"
        "`bt!capper gr8 MLB dfs july`\n"
        "`bt!report PropKitchen july`\n\n"
        "**Leaderboards**\n"
        "`bt!leaderboard MLB july` | `bt!leaderboard dfs july`\n\n"
        "**Posting Help**\n"
        "`bt!format straight` | `bt!format parlay` | `bt!format dfs`\n"
        "Grades: ✅ win | ❌ loss | ➖ push | ↩️ void/refund\n"
        "`bt!trackcheck` | `bt!pending` | `bt!pending all` | `bt!pending gr8`\n\n"
        "**Admin**\n"
        "Reply: `bt!setdate`, `bt!setodds`, `bt!setunits`, `bt!setsport`\n"
        "Reply: `bt!settype`, `bt!setplatform`, `bt!setmarket`, `bt!setpayout 2x`\n"
        "`bt!force_sport NBA WNBA 2026-07-01 2026-07-31`\n"
        "`bt!backup` | `bt!export july` | `bt!export PropKitchen july`\n"
        "Command-channel cleanup: `bt!autoclean on 3d`, `off`, or `status` (admin only)."
    )

@bot.command()
async def daily(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, "today", include_chart=True)


@bot.command()
async def weekly(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, "thisweek", include_chart=True)


@bot.command()
async def monthly(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, "thismonth", include_chart=True)


@bot.command()
async def yearly(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, str(now_local().year), include_chart=True)


@bot.command()
async def alltime(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, "alltime", include_chart=True)


@bot.command(name="today")
async def today_cmd(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, "today")


@bot.command(name="yesterday", aliases=["yday"])
async def yesterday_cmd(ctx: commands.Context) -> None:
    await post_leaderboard_query(ctx, "yesterday")


@bot.command(name="date")
async def date_cmd(ctx: commands.Context, date_arg: str) -> None:
    await post_leaderboard_query(ctx, date_arg)


@bot.command(name="monthlyroles", aliases=["setmonthlyroles", "recaproles"])
@commands.has_permissions(manage_guild=True)
async def monthlyroles_cmd(ctx: commands.Context, role_one: discord.Role, role_two: discord.Role) -> None:
    """Configure exactly two server roles to tag in the monthly recap."""
    if (ctx.guild is None or role_one.guild.id != ctx.guild.id
            or role_two.guild.id != ctx.guild.id or role_one.is_default()
            or role_two.is_default() or role_one.id == role_two.id):
        await ctx.send("Mention two different VIP roles from this Discord server.")
        return
    cur.execute(
        """
        INSERT INTO recap_settings (guild_id, monthly_role_ids) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET monthly_role_ids = excluded.monthly_role_ids
        """,
        (int(ctx.guild.id), f"{int(role_one.id)},{int(role_two.id)}"),
    )
    conn.commit()
    await ctx.send(f"✅ The monthly recap will tag {role_one.name} and {role_two.name} on the 3rd of each month.")


@bot.command(name="pending", aliases=["ungraded", "openbets", "unsettled"])
async def pending_cmd(ctx: commands.Context, *, query: str = "") -> None:
    """Find ungraded wagers with direct links to react on the original post."""
    if ctx.guild is None:
        await ctx.send("Run this inside the Discord server.")
        return

    tokens = split_args(query)
    page = 1
    if tokens and tokens[-1].isdigit():
        page = int(tokens.pop())
    if not 1 <= page <= 100:
        await ctx.send("Use a page number from 1 to 100.")
        return

    filter_text = " ".join(tokens).strip()
    capper_name: Optional[str] = None
    if filter_text.lower() not in {"all", "everyone", "team"}:
        if filter_text:
            capper_name, extra = resolve_capper_from_tokens(tokens)
            if capper_name is None or extra:
                await ctx.send("Use `bt!pending`, `bt!pending all`, `bt!pending gr8`, or `bt!pending all 2`.")
                return
        else:
            registered = capper_by_user_id(ctx.author.id)
            if registered is not None:
                capper_name = registered.name

    where_sql = "guild_id = ?"
    params: List[object] = [int(ctx.guild.id)]
    if capper_name:
        where_sql += " AND LOWER(capper) = ?"
        params.append(capper_name.lower())

    count = int(cur.execute(
        f"SELECT COUNT(*) FROM pending WHERE {where_sql}", tuple(params)
    ).fetchone()[0])
    rows = cur.execute(
        f"""
        SELECT capper, risk_units, content, market, jump_url, channel_id, message_id
        FROM pending
        WHERE {where_sql}
        ORDER BY created_utc DESC, message_id DESC
        LIMIT 20 OFFSET ?
        """,
        (*params, (page - 1) * 20),
    ).fetchall()

    label = capper_name or "All Cappers"
    if count == 0:
        await ctx.send(f"📝 **Ungraded Bets — {label}: 0**")
        return
    if not rows:
        await ctx.send(f"No bets on page {page}. There are {count} ungraded bets.")
        return

    lines = [f"📝 **Ungraded Bets — {label}** | {count} pending | Page {page}"]
    for name, risk, content, market, jump_url, channel_id, message_id in rows:
        desc = compact_bet_description(str(content or ""), str(market or ""), max_len=82)
        url = str(jump_url or "").strip()
        if not url.startswith("https://discord.com/channels/"):
            url = f"https://discord.com/channels/{int(ctx.guild.id)}/{int(channel_id)}/{int(message_id)}"
        capper_label = f"**{name}** | " if not capper_name else ""
        lines.append(f"• {capper_label}{format_compact_units(float(risk))} | {desc} | [jump]({url})")
    if page * 20 < count:
        suffix = (capper_name or "all") + f" {page + 1}"
        lines.append(f"Next page: `bt!pending {suffix}`")
    for chunk in split_discord_text("\n".join(lines)):
        await ctx.send(chunk)


async def clear_pending_rows(ctx: commands.Context, where_sql: str, params: Tuple[object, ...], label: str) -> None:
    if ctx.guild is None:
        await ctx.send("Run this command inside a Discord server.")
        return

    # Always restrict bulk deletion to the invoking Discord server.
    scoped_where_sql = f"guild_id = ? AND ({where_sql})"
    scoped_params = (int(ctx.guild.id), *params)

    # Large backlogs should not require hundreds of Discord API fetches
    # before the database rows are deleted.
    count_row = cur.execute(
        f"SELECT COUNT(*) FROM pending WHERE {scoped_where_sql}",
        scoped_params,
    ).fetchone()
    pending_count = int(count_row[0] or 0) if count_row else 0
    if not pending_count:
        await ctx.send(f"📝 No pending bets found for **{label}**.")
        return

    recent_rows = cur.execute(
        f"""
        SELECT message_id, channel_id
        FROM pending
        WHERE {scoped_where_sql}
        ORDER BY created_utc DESC
        LIMIT 50
        """,
        scoped_params,
    ).fetchall()

    cur.execute(f"DELETE FROM pending WHERE {scoped_where_sql}", scoped_params)
    conn.commit()

    await ctx.send(
        f"✅ **Pending Cleared: {label}**\n"
        f"Removed pending rows: **{pending_count}**\n"
        "Graded bet history was **not** deleted.\n"
        "Cleaning 📝 reactions from up to the 50 most recent cleared messages where possible."
    )

    # Best-effort cosmetic cleanup. Older 📝 reactions may remain in Discord,
    # but those wagers are no longer pending in the database.
    for message_id, channel_id in recent_rows:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            continue
        try:
            msg = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
            await msg.clear_reaction(PENDING_REACTION)
        except Exception:
            continue


@bot.command(name="clear_pending")
@commands.has_permissions(manage_guild=True)
async def clear_pending_cmd(ctx: commands.Context, *, capper_name: str = "") -> None:
    if capper_name.strip():
        tokens = split_args(capper_name)
        capper, remaining = resolve_capper_from_tokens(tokens)
        if not capper:
            await ctx.send("Could not find that capper. Example: `bt!clear_pending gr8`")
            return
        await clear_pending_rows(ctx, "LOWER(capper) = ?", (capper.lower(),), capper)
        return

    await clear_pending_rows(ctx, "1 = 1", tuple(), "All Pending Bets")


@bot.command(name="clear_pending_old", aliases=["clearoldpending", "clear_stale_pending"])
@commands.has_permissions(manage_guild=True)
async def clear_pending_old_cmd(ctx: commands.Context) -> None:
    # Safe reset: clears stale pending plays before today, leaves today's active plays alone.
    today = now_local().date()
    start_today, _end_today = local_day_bounds(today)
    cutoff_utc = utc_iso(to_utc(start_today))
    await clear_pending_rows(
        ctx,
        "((bet_date IS NOT NULL AND bet_date != '' AND bet_date < ?) OR ((bet_date IS NULL OR bet_date = '') AND created_utc < ?))",
        (today.isoformat(), cutoff_utc),
        f"Before Today ({today.isoformat()})",
    )


@bot.command(name="clear_pending_before")
@commands.has_permissions(manage_guild=True)
async def clear_pending_before_cmd(ctx: commands.Context, *, cutoff: str) -> None:
    label, start_l, _end_l, error = parse_time_filter(cutoff)
    if error or start_l is None:
        await ctx.send("Use format: `bt!clear_pending_before today` or `bt!clear_pending_before 2026-07-10`")
        return

    cutoff_utc = utc_iso(to_utc(start_l))
    cutoff_date = start_l.date().isoformat()
    await clear_pending_rows(
        ctx,
        "((bet_date IS NOT NULL AND bet_date != '' AND bet_date < ?) OR ((bet_date IS NULL OR bet_date = '') AND created_utc < ?))",
        (cutoff_date, cutoff_utc),
        f"Before {label}",
    )


@bot.command(name="fix_decimal_units", aliases=["fixdecimals", "fix_decimal", "fixunits", "fix_units"])
@commands.has_permissions(manage_guild=True)
async def fix_decimal_units_cmd(ctx: commands.Context) -> None:
    """Repair rows created when leading decimals like .5u were parsed as 5u."""

    checked_bets = 0
    updated_bets = 0
    checked_pending = 0
    updated_pending = 0
    old_total = 0.0
    new_total = 0.0

    bet_rows = cur.execute(
        """
        SELECT id, content, risk_units, odds_text, result, net_units
        FROM bets
        WHERE guild_id = ?
        """,
        (int(ctx.guild.id),),
    ).fetchall()

    for bet_id, content, old_risk, odds_text, result, old_net in bet_rows:
        checked_bets += 1
        parsed_risk = parse_risk_units(str(content or ""))
        if parsed_risk is None:
            continue

        old_risk_f = float(old_risk or 0.0)
        if abs(parsed_risk - old_risk_f) <= 0.0001:
            continue

        new_net = compute_net_units(float(parsed_risk), str(odds_text or ""), str(result or ""))
        old_net_f = float(old_net or 0.0)

        cur.execute(
            "UPDATE bets SET risk_units = ?, net_units = ? WHERE id = ?",
            (float(parsed_risk), float(new_net), int(bet_id)),
        )
        updated_bets += 1
        old_total += old_net_f
        new_total += float(new_net)

    pending_rows = cur.execute(
        """
        SELECT message_id, content, risk_units
        FROM pending
        WHERE guild_id = ?
        """,
        (int(ctx.guild.id),),
    ).fetchall()

    for message_id, content, old_risk in pending_rows:
        checked_pending += 1
        parsed_risk = parse_risk_units(str(content or ""))
        if parsed_risk is None:
            continue

        old_risk_f = float(old_risk or 0.0)
        if abs(parsed_risk - old_risk_f) <= 0.0001:
            continue

        cur.execute(
            "UPDATE pending SET risk_units = ? WHERE message_id = ?",
            (float(parsed_risk), int(message_id)),
        )
        updated_pending += 1

    conn.commit()

    await ctx.send(
        "✅ **Decimal Unit Fix Complete**\n"
        "This fixes mistakes like `.5u` being read as `5u` or `.25u` being read as `25u`.\n"
        f"Checked graded bets: **{checked_bets}** | Updated: **{updated_bets}**\n"
        f"Checked pending bets: **{checked_pending}** | Updated: **{updated_pending}**\n"
        f"Corrected net change on updated graded bets: **{(new_total - old_total):+.2f}u**"
    )


@bot.command(name="fix_bet_dates", aliases=["fixbetdates", "backfill_bet_dates", "fix_dates"])
@commands.has_permissions(manage_guild=True)
async def fix_bet_dates_cmd(ctx: commands.Context) -> None:
    """Backfill intended bet dates from post text like (7/10), falling back to graded date."""

    checked_bets = 0
    updated_bets = 0
    explicit_bet_dates = 0
    fallback_bet_dates = 0

    bet_rows = cur.execute(
        """
        SELECT id, content, created_utc, graded_utc, bet_date
        FROM bets
        WHERE guild_id = ?
        """,
        (int(ctx.guild.id),),
    ).fetchall()

    for bet_id, content, created_utc, graded_utc, old_bet_date in bet_rows:
        checked_bets += 1
        explicit = parse_bet_date(str(content or ""), str(created_utc or ""))
        if explicit:
            new_bet_date = explicit
            explicit_bet_dates += 1
        else:
            new_bet_date = local_date_from_utc_iso(str(graded_utc or created_utc or "" )).isoformat()
            fallback_bet_dates += 1

        if str(old_bet_date or "") != new_bet_date:
            cur.execute("UPDATE bets SET bet_date = ? WHERE id = ?", (new_bet_date, int(bet_id)))
            updated_bets += 1

    checked_pending = 0
    updated_pending = 0
    pending_rows = cur.execute(
        """
        SELECT message_id, content, created_utc, bet_date
        FROM pending
        WHERE guild_id = ?
        """,
        (int(ctx.guild.id),),
    ).fetchall()

    for message_id, content, created_utc, old_bet_date in pending_rows:
        checked_pending += 1
        explicit = parse_bet_date(str(content or ""), str(created_utc or ""))
        if explicit and str(old_bet_date or "") != explicit:
            cur.execute("UPDATE pending SET bet_date = ? WHERE message_id = ?", (explicit, int(message_id)))
            updated_pending += 1

    conn.commit()

    await ctx.send(
        "✅ **Bet Date Fix Complete**\n"
        "This lets posts like `MLB Prop #1 (7/10)` count toward 7/10, even if posted the night before.\n"
        f"Checked graded bets: **{checked_bets}** | Updated: **{updated_bets}**\n"
        f"Explicit dates found: **{explicit_bet_dates}** | Fallback to graded date: **{fallback_bet_dates}**\n"
        f"Checked pending bets: **{checked_pending}** | Updated pending: **{updated_pending}**"
    )


@bot.command(name="recalc_multipliers", aliases=["recalcmultipliers", "fixmultipliers", "fix_multipliers"])
@commands.has_permissions(manage_guild=True)
async def recalc_multipliers_cmd(ctx: commands.Context) -> None:
    rows = cur.execute(
        """
        SELECT id, risk_units, odds_text, net_units
        FROM bets
        WHERE guild_id = ? AND result = 'win' AND LOWER(odds_text) LIKE '%x%'
        """,
        (int(ctx.guild.id),),
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


@bot.command(name="backfill_wager_types", aliases=["backfillwagers", "backfill_dfs", "backfilldfs"])
@commands.has_permissions(manage_guild=True)
async def backfill_wager_types_cmd(
    ctx: commands.Context,
    start_date: str = DEFAULT_DFS_BACKFILL_DATE,
) -> None:
    parsed_start = parse_date_yyyy_mm_dd(start_date)
    if not parsed_start:
        await ctx.send("Use format: `bt!backfill_wager_types 2026-07-01`.")
        return

    cutoff = parsed_start.isoformat()
    checked_bets = updated_bets = 0
    checked_pending = updated_pending = 0
    dfs_count = parlay_count = straight_count = 0

    bet_rows = cur.execute(
        """
        SELECT id, capper, content, created_utc, risk_units, odds_text, sport, league, market
        FROM bets
        WHERE guild_id = ?
          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?
        """,
        (int(ctx.guild.id), cutoff),
    ).fetchall()

    for bet_id, capper_name, content, created_utc, risk, odds_text, old_sport, old_league, old_market in bet_rows:
        checked_bets += 1
        fields = parse_analytics_fields(str(content or old_market or ""), str(odds_text or ""))
        category = str(fields["wager_category"])
        if category == WAGER_DFS:
            dfs_count += 1
        elif category == WAGER_PARLAY:
            parlay_count += 1
        else:
            straight_count += 1

        new_sport = str(fields["sport"])
        if new_sport == "UNKNOWN" and str(old_sport or "") not in {"", "UNKNOWN"}:
            new_sport = str(old_sport)
        new_league = str(fields["league"] or old_league or "")
        duplicate_key = build_duplicate_key(
            str(content or old_market or ""),
            str(created_utc or ""),
            str(capper_name),
            float(risk or 0.0),
            str(odds_text or ""),
            fields,
        )

        cur.execute(
            """
            UPDATE bets
            SET wager_category = ?, platform = ?, platform_type = ?, sportsbook = ?,
                odds_format = ?, multiplier = ?, sport = ?, league = ?,
                duplicate_key = COALESCE(NULLIF(?, ''), duplicate_key),
                author_user_id = CASE WHEN author_user_id = 0 THEN capper_user_id ELSE author_user_id END,
                grader_user_id = CASE WHEN grader_user_id = 0 THEN capper_user_id ELSE grader_user_id END
            WHERE id = ?
            """,
            (
                category,
                str(fields["platform"]),
                str(fields["platform_type"]),
                str(fields["sportsbook"]),
                str(fields["odds_format"]),
                fields["multiplier"],
                new_sport,
                new_league,
                duplicate_key,
                int(bet_id),
            ),
        )
        updated_bets += 1

    pending_rows = cur.execute(
        """
        SELECT message_id, capper, content, created_utc, risk_units, odds_text, sport, league, market
        FROM pending
        WHERE guild_id = ?
          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?
        """,
        (int(ctx.guild.id), cutoff),
    ).fetchall()

    for message_id, capper_name, content, created_utc, risk, odds_text, old_sport, old_league, old_market in pending_rows:
        checked_pending += 1
        fields = parse_analytics_fields(str(content or old_market or ""), str(odds_text or ""))
        new_sport = str(fields["sport"])
        if new_sport == "UNKNOWN" and str(old_sport or "") not in {"", "UNKNOWN"}:
            new_sport = str(old_sport)
        duplicate_key = build_duplicate_key(
            str(content or old_market or ""),
            str(created_utc or ""),
            str(capper_name),
            float(risk or 0.0),
            str(odds_text or ""),
            fields,
        )
        cur.execute(
            """
            UPDATE pending
            SET wager_category = ?, platform = ?, platform_type = ?, sportsbook = ?,
                odds_format = ?, multiplier = ?, sport = ?, league = ?,
                duplicate_key = COALESCE(NULLIF(?, ''), duplicate_key),
                author_user_id = CASE WHEN author_user_id = 0 THEN capper_user_id ELSE author_user_id END
            WHERE message_id = ?
            """,
            (
                str(fields["wager_category"]),
                str(fields["platform"]),
                str(fields["platform_type"]),
                str(fields["sportsbook"]),
                str(fields["odds_format"]),
                fields["multiplier"],
                new_sport,
                str(fields["league"] or old_league or ""),
                duplicate_key,
                int(message_id),
            ),
        )
        updated_pending += 1

    conn.commit()
    await ctx.send(
        "✅ **Wager-Type Backfill Complete**\n"
        f"Starting date: **{cutoff}**\n"
        f"Graded bets checked/updated: **{checked_bets}/{updated_bets}**\n"
        f"Pending bets checked/updated: **{checked_pending}/{updated_pending}**\n"
        f"Graded classifications: **{straight_count} straight**, "
        f"**{parlay_count} sportsbook parlays**, **{dfs_count} DFS slips**\n"
        "This reclassifies existing database rows only. It cannot create a bet that was never logged."
    )


@bot.command(name="fix_sports", aliases=["fixsports", "repair_sports", "backfill_sports"])
@commands.has_permissions(manage_guild=True)
async def fix_sports_cmd(
    ctx: commands.Context,
    start_date: str = DEFAULT_DFS_BACKFILL_DATE,
) -> None:
    """
    Re-parse sport and league fields from saved wager text.

    This repairs historical rows created by older versions that could classify WNBA
    as NBA, and it attempts to resolve UNKNOWN rows when the original saved message
    contains a recognizable sport or league. Rows without enough saved text remain
    UNKNOWN rather than being guessed.
    """
    parsed_start = parse_date_yyyy_mm_dd(start_date)
    if not parsed_start:
        await ctx.send("Use format: `bt!fix_sports 2026-07-01`.")
        return

    cutoff = parsed_start.isoformat()
    checked_bets = 0
    updated_bets = 0
    wnba_repairs = 0
    unknown_resolved = 0
    unresolved_unknown = 0

    bet_rows = cur.execute(
        """
        SELECT id, content, market, odds_text, sport, league
        FROM bets
        WHERE guild_id = ?
          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?
        """,
        (int(ctx.guild.id), cutoff),
    ).fetchall()

    for bet_id, content, market, odds_text, old_sport, old_league in bet_rows:
        checked_bets += 1
        source_text = str(content or market or "")
        fields = parse_analytics_fields(source_text, str(odds_text or ""))
        parsed_sport = str(fields["sport"] or "UNKNOWN")
        parsed_league = str(fields["league"] or "")
        previous_sport = str(old_sport or "UNKNOWN")
        previous_league = str(old_league or "")

        # Do not erase a known historical sport when an old row has no recoverable text.
        if parsed_sport == "UNKNOWN" and previous_sport not in {"", "UNKNOWN"}:
            new_sport = previous_sport
            new_league = previous_league
        else:
            new_sport = parsed_sport
            new_league = parsed_league or (new_sport if new_sport != "UNKNOWN" else "")

        if previous_sport == "NBA" and new_sport == "WNBA":
            wnba_repairs += 1
        if previous_sport in {"", "UNKNOWN"} and new_sport not in {"", "UNKNOWN"}:
            unknown_resolved += 1
        if new_sport in {"", "UNKNOWN"}:
            unresolved_unknown += 1

        if new_sport != previous_sport or new_league != previous_league:
            cur.execute(
                "UPDATE bets SET sport = ?, league = ? WHERE id = ?",
                (new_sport, new_league, int(bet_id)),
            )
            updated_bets += 1

    checked_pending = 0
    updated_pending = 0
    pending_rows = cur.execute(
        """
        SELECT message_id, content, market, odds_text, sport, league
        FROM pending
        WHERE guild_id = ?
          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?
        """,
        (int(ctx.guild.id), cutoff),
    ).fetchall()

    for message_id, content, market, odds_text, old_sport, old_league in pending_rows:
        checked_pending += 1
        source_text = str(content or market or "")
        fields = parse_analytics_fields(source_text, str(odds_text or ""))
        parsed_sport = str(fields["sport"] or "UNKNOWN")
        parsed_league = str(fields["league"] or "")
        previous_sport = str(old_sport or "UNKNOWN")
        previous_league = str(old_league or "")

        if parsed_sport == "UNKNOWN" and previous_sport not in {"", "UNKNOWN"}:
            new_sport = previous_sport
            new_league = previous_league
        else:
            new_sport = parsed_sport
            new_league = parsed_league or (new_sport if new_sport != "UNKNOWN" else "")

        if new_sport != previous_sport or new_league != previous_league:
            cur.execute(
                "UPDATE pending SET sport = ?, league = ? WHERE message_id = ?",
                (new_sport, new_league, int(message_id)),
            )
            updated_pending += 1

    conn.commit()
    await ctx.send(
        "✅ **Sport Repair Complete**\n"
        f"Starting date: **{cutoff}**\n"
        f"Graded bets checked: **{checked_bets}** | Updated: **{updated_bets}**\n"
        f"WNBA bets moved out of NBA: **{wnba_repairs}**\n"
        f"UNKNOWN sports resolved: **{unknown_resolved}**\n"
        f"Still UNKNOWN: **{unresolved_unknown}**\n"
        f"Pending bets checked: **{checked_pending}** | Updated: **{updated_pending}**\n"
        "UNKNOWN means the saved post did not contain enough recognizable sport text. "
        "Those rows are left unchanged instead of being guessed."
    )


@bot.command(name="backfill_content")
@commands.has_permissions(manage_guild=True)
async def backfill_content_cmd(ctx: commands.Context, limit: int = 200) -> None:
    limit = max(1, min(int(limit), 500))
    rows = cur.execute(
        """
        SELECT id, message_id, channel_id, capper, created_utc, risk_units, result, net_units
        FROM bets
        WHERE guild_id = ? AND (content = '' OR content IS NULL)
        ORDER BY created_utc DESC
        LIMIT ?
        """,
        (int(ctx.guild.id), limit),
    ).fetchall()

    updated = checked = corrected_units = 0
    for bet_id, message_id, channel_id, capper_name, created_utc, risk_units, result, old_net in rows:
        checked += 1
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            continue
        try:
            message = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
        except Exception:
            continue

        content = message_to_text(message)
        if not content:
            continue

        odds_text = parse_odds_text(content)
        fields = parse_analytics_fields(content, odds_text)
        bet_date = parse_bet_date(content, message.created_at.isoformat())
        new_net = (
            compute_net_units(float(risk_units), odds_text, str(result))
            if odds_text
            else float(old_net)
        )
        if abs(float(new_net) - float(old_net)) > 0.0001:
            corrected_units += 1

        duplicate_key = build_duplicate_key(
            content,
            str(created_utc or message.created_at.isoformat()),
            str(capper_name),
            float(risk_units),
            odds_text,
            fields,
        )
        cur.execute(
            """
            UPDATE bets
            SET content = ?, market = ?, player = ?, bet_type = ?, league = ?, sport = ?,
                line = ?, sportsbook = ?, odds_text = ?, odds_format = ?, multiplier = ?,
                net_units = ?, bet_date = COALESCE(NULLIF(?, ''), bet_date),
                wager_category = ?, platform = ?, platform_type = ?,
                duplicate_key = COALESCE(NULLIF(?, ''), duplicate_key),
                author_user_id = COALESCE(NULLIF(author_user_id, 0), capper_user_id)
            WHERE id = ?
            """,
            (
                content,
                str(fields["market"]),
                str(fields["player"]),
                str(fields["bet_type"]),
                str(fields["league"]),
                str(fields["sport"]),
                str(fields["line"]),
                str(fields["sportsbook"]),
                odds_text,
                str(fields["odds_format"]),
                fields["multiplier"],
                float(new_net),
                bet_date,
                str(fields["wager_category"]),
                str(fields["platform"]),
                str(fields["platform_type"]),
                duplicate_key,
                int(bet_id),
            ),
        )
        updated += 1

    conn.commit()
    await ctx.send(
        f"✅ **Backfill Complete**\n"
        f"Checked: **{checked}** rows\n"
        f"Updated with recovered bet text: **{updated}**\n"
        f"Rows with corrected unit calculations: **{corrected_units}**"
    )


def log_correction(
    message_id: int,
    table_name: str,
    field_name: str,
    old_value: object,
    new_value: object,
    changed_by_user_id: int,
    note: str = "",
) -> None:
    cur.execute(
        """
        INSERT INTO correction_audit
        (message_id, table_name, field_name, old_value, new_value, changed_by_user_id, changed_utc, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            str(table_name),
            str(field_name),
            str(old_value if old_value is not None else ""),
            str(new_value if new_value is not None else ""),
            int(changed_by_user_id),
            utc_iso(datetime.now(timezone.utc)),
            str(note),
        ),
    )


def parse_manual_units(value: str) -> Optional[float]:
    clean = normalize_space(value).lower().replace(" ", "")
    match = re.fullmatch(r"((?:\d+(?:\.\d+)?|\.\d+))u?", clean)
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except Exception:
        return None
    return amount if amount > 0 else None


def canonical_sport(value: str) -> Optional[str]:
    key = normalize_space(value).upper().replace("-", " ")
    aliases = {
        "BASEBALL": "MLB",
        "BASKETBALL": "BASKETBALL",
        "FOOTBALL": "FOOTBALL",
        "HOCKEY": "NHL",
        "COLLEGE BASKETBALL": "NCAAB",
        "COLLEGE FOOTBALL": "NCAAF",
        "MIXED SPORT": "MIXED",
        "MIXED SPORTS": "MIXED",
    }
    key = aliases.get(key, key)
    if key in SPORT_CODES or key == "UNKNOWN":
        return key
    return None


def canonical_wager_category(value: str) -> Optional[str]:
    key = normalize_key(value)
    return WAGER_CATEGORY_ALIASES.get(key)


def canonical_platform(value: str) -> Tuple[str, str]:
    clean = normalize_space(value)
    key = normalize_key(clean)
    for platform, aliases in DFS_PLATFORM_RULES:
        if key == normalize_key(platform) or any(key == normalize_key(alias) for alias in aliases):
            return platform, "DFS_APP"
    for platform, aliases in SPORTSBOOK_KEYWORDS:
        if key == normalize_key(platform) or any(key == normalize_key(alias) for alias in aliases):
            return platform, "SPORTSBOOK"
    return clean[:80], ""


def default_league_for_manual_sport(new_sport: str, old_league: str, old_sport: str) -> str:
    direct_leagues = {"WNBA", "NBA", "NCAAB", "NFL", "NCAAF", "NHL", "MLB", "MIXED"}
    if new_sport in direct_leagues:
        return new_sport
    if not old_league or old_league.upper() == old_sport.upper():
        return new_sport if new_sport not in {"UNKNOWN", "SOCCER", "TENNIS"} else ""
    return old_league


def referenced_message_id(ctx: commands.Context) -> Optional[int]:
    """Resolve a replied-to tracked message only within this Discord server."""
    reference = ctx.message.reference
    if ctx.guild is None or not reference or not reference.message_id:
        return None
    message_id = int(reference.message_id)
    guild_id = int(ctx.guild.id)
    row = cur.execute(
        """
        SELECT 1 FROM pending WHERE message_id = ? AND guild_id = ?
        UNION ALL
        SELECT 1 FROM bets WHERE message_id = ? AND guild_id = ?
        LIMIT 1
        """,
        (message_id, guild_id, message_id, guild_id),
    ).fetchone()
    return message_id if row else None


@bot.command(name="setdate", aliases=["set_bet_date", "fixdate"])
@commands.has_permissions(manage_guild=True)
async def setdate_cmd(ctx: commands.Context, date_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send("Reply to the original bet and use `bt!setdate 2026-07-12`.")
        return

    parsed = parse_date_yyyy_mm_dd(date_arg)
    if not parsed:
        await ctx.send("Use exact format: `bt!setdate 2026-07-12`.")
        return

    updated_pending = cur.execute(
        "UPDATE pending SET bet_date = ? WHERE message_id = ?",
        (parsed.isoformat(), message_id),
    ).rowcount
    updated_bet = cur.execute(
        "UPDATE bets SET bet_date = ? WHERE message_id = ?",
        (parsed.isoformat(), message_id),
    ).rowcount
    conn.commit()

    if not updated_pending and not updated_bet:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return

    await ctx.send(f"✅ Bet date updated to **{parsed.isoformat()}**.")


@bot.command(name="setodds", aliases=["set_odds", "fixodds"])
@commands.has_permissions(manage_guild=True)
async def setodds_cmd(ctx: commands.Context, *, odds_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send(
            "Reply to the original bet and use `bt!setodds +715`, "
            "`bt!setodds 3x`, or `bt!setodds 2.50`."
        )
        return

    odds_text = parse_manual_odds_text(odds_arg)
    if not odds_text:
        await ctx.send(
            "Odds not recognized. Use American `+715`/`-120`, "
            "multiplier `3x`, or decimal `2.50`."
        )
        return

    pending_row = cur.execute(
        "SELECT content, sport, league FROM pending WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if pending_row:
        content, old_sport, old_league = pending_row
        fields = parse_analytics_fields(str(content or ""), odds_text)
        new_sport = str(fields["sport"])
        if new_sport == "UNKNOWN":
            new_sport = str(old_sport or "UNKNOWN")
        cur.execute(
            """
            UPDATE pending
            SET odds_text = ?, odds_format = ?, multiplier = ?, wager_category = ?,
                platform = ?, platform_type = ?, sportsbook = ?, sport = ?, league = ?
            WHERE message_id = ?
            """,
            (
                odds_text,
                str(fields["odds_format"]),
                fields["multiplier"],
                str(fields["wager_category"]),
                str(fields["platform"]),
                str(fields["platform_type"]),
                str(fields["sportsbook"]),
                new_sport,
                str(fields["league"] or old_league or ""),
                message_id,
            ),
        )
        conn.commit()
        await ctx.send(
            f"✅ Pending bet odds updated to **{odds_text}** and wager type refreshed."
        )
        return

    bet_row = cur.execute(
        """
        SELECT risk_units, result, net_units, content, sport, league
        FROM bets
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()
    if not bet_row:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return

    risk_units, result, old_net, content, old_sport, old_league = bet_row
    fields = parse_analytics_fields(str(content or ""), odds_text)
    new_sport = str(fields["sport"])
    if new_sport == "UNKNOWN":
        new_sport = str(old_sport or "UNKNOWN")
    new_net = compute_net_units(float(risk_units), odds_text, str(result))

    cur.execute(
        """
        UPDATE bets
        SET odds_text = ?, odds_format = ?, multiplier = ?, net_units = ?,
            wager_category = ?, platform = ?, platform_type = ?, sportsbook = ?,
            sport = ?, league = ?
        WHERE message_id = ?
        """,
        (
            odds_text,
            str(fields["odds_format"]),
            fields["multiplier"],
            float(new_net),
            str(fields["wager_category"]),
            str(fields["platform"]),
            str(fields["platform_type"]),
            str(fields["sportsbook"]),
            new_sport,
            str(fields["league"] or old_league or ""),
            message_id,
        ),
    )
    conn.commit()

    await ctx.send(
        f"✅ Graded bet odds updated to **{odds_text}**.\n"
        f"Net units corrected from **{float(old_net):+.2f}u** to **{new_net:+.2f}u**.\n"
        f"Wager type: **{wager_category_label(str(fields['wager_category']))}**."
    )


@bot.command(name="setpayout", aliases=["flexpayout", "paidmultiplier"])
async def setpayout_cmd(ctx: commands.Context, multiplier_text: str, message_ref: str = "") -> None:
    """Correct a settled slip's actual total payout multiplier (e.g. 25x paid 2x)."""
    if ctx.guild is None:
        await ctx.send("Use this command inside the Discord server.")
        return
    message_id = referenced_message_id(ctx)
    if message_id is None and message_ref:
        match = re.fullmatch(
            r"(?:https://(?:discord\.com|discordapp\.com)/channels/(\d+)/(\d+)/(\d+)|([0-9]{15,20}))",
            message_ref.strip(),
        )
        if match:
            if match.group(1) and int(match.group(1)) != int(ctx.guild.id):
                await ctx.send("That bet link belongs to a different Discord server.")
                return
            message_id = int(match.group(3) or match.group(4))
    if message_id is None:
        await ctx.send("Reply to the graded slip with `bt!setpayout 2x`, or use `bt!setpayout 2x <Discord message link>`.")
        return
    multiplier_text = parse_manual_odds_text(multiplier_text)
    mult = parse_multiplier_value(multiplier_text)
    if mult is None or mult <= 1:
        await ctx.send("Enter the actual total payout multiplier, e.g. `bt!setpayout 2x`.")
        return
    row = cur.execute(
        """
        SELECT risk_units, result, odds_text, net_units, capper_user_id
        FROM bets WHERE message_id = ? AND guild_id = ?
        """, (int(message_id), int(ctx.guild.id)),
    ).fetchone()
    if not row:
        await ctx.send("Graded bet not found in this server. Grade the slip first.")
        return
    risk, result, old_odds, old_net, owner_user_id = row
    can_correct = (int(ctx.author.id) in {int(owner_user_id), int(ADMIN_USER_ID)}
                   or bool(ctx.author.guild_permissions.manage_guild))
    if not can_correct:
        await ctx.send("Only the original capper or a server manager can correct this payout.")
        return
    if str(result) != "win":
        await ctx.send("This command is for a graded slip that paid out. Use grading reactions for losses/voids.")
        return
    new_net = compute_net_units(float(risk), multiplier_text, "win")
    cur.execute(
        """
        UPDATE bets SET odds_text = ?, odds_format = 'multiplier', multiplier = ?, net_units = ?
        WHERE message_id = ? AND guild_id = ?
        """, (multiplier_text, float(mult), float(new_net), int(message_id), int(ctx.guild.id)),
    )
    log_correction(message_id, "bets", "settled_payout_multiplier",
                   str(old_odds or ""), multiplier_text, ctx.author.id,
                   "Actual flex/void-adjusted payout; original post retained")
    conn.commit()
    await ctx.send(
        f"✅ **Actual payout recorded: {multiplier_text}**\n"
        f"Risk: **{float(risk):g}u** | Net: **{new_net:+.2f}u** "
        f"(previously {float(old_net):+.2f}u). Original message unchanged."
    )


@bot.command(name="setunits", aliases=["set_units", "fixstake", "setstake"])
@commands.has_permissions(manage_guild=True)
async def setunits_cmd(ctx: commands.Context, *, units_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send("Reply to the original bet and use `bt!setunits 0.5u`.")
        return
    new_units = parse_manual_units(units_arg)
    if new_units is None:
        await ctx.send("Units not recognized. Use `bt!setunits 0.5u` or `bt!setunits .25u`.")
        return

    pending_row = cur.execute(
        "SELECT risk_units FROM pending WHERE message_id = ?", (message_id,)
    ).fetchone()
    if pending_row:
        old_units = float(pending_row[0] or 0.0)
        cur.execute("UPDATE pending SET risk_units = ? WHERE message_id = ?", (new_units, message_id))
        log_correction(message_id, "pending", "risk_units", old_units, new_units, ctx.author.id)
        conn.commit()
        await ctx.send(f"✅ Pending bet units updated from **{old_units:g}u** to **{new_units:g}u**.")
        return

    bet_row = cur.execute(
        "SELECT risk_units, odds_text, result, net_units FROM bets WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if not bet_row:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return
    old_units, odds_text, result, old_net = bet_row
    new_net = compute_net_units(new_units, str(odds_text or ""), str(result or ""))
    cur.execute(
        "UPDATE bets SET risk_units = ?, net_units = ? WHERE message_id = ?",
        (new_units, new_net, message_id),
    )
    log_correction(message_id, "bets", "risk_units", old_units, new_units, ctx.author.id)
    log_correction(message_id, "bets", "net_units", old_net, new_net, ctx.author.id, "Recalculated after unit correction")
    conn.commit()
    await ctx.send(
        f"✅ Graded bet units updated from **{float(old_units):g}u** to **{new_units:g}u**.\n"
        f"Net units corrected from **{float(old_net):+.2f}u** to **{new_net:+.2f}u**."
    )


@bot.command(name="setsport", aliases=["set_sport", "fixsport"])
@commands.has_permissions(manage_guild=True)
async def setsport_cmd(ctx: commands.Context, *, sport_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send("Reply to the original bet and use `bt!setsport WNBA`.")
        return
    new_sport = canonical_sport(sport_arg)
    if not new_sport:
        await ctx.send("Sport not recognized. Examples: `MLB`, `WNBA`, `NFL`, `Tennis`, `Soccer`, `Mixed`.")
        return

    found = False
    for table in ("pending", "bets"):
        row = cur.execute(
            f"SELECT sport, league FROM {table} WHERE message_id = ?", (message_id,)
        ).fetchone()
        if not row:
            continue
        found = True
        old_sport, old_league = str(row[0] or "UNKNOWN"), str(row[1] or "")
        new_league = default_league_for_manual_sport(new_sport, old_league, old_sport)
        cur.execute(
            f"UPDATE {table} SET sport = ?, league = ? WHERE message_id = ?",
            (new_sport, new_league, message_id),
        )
        log_correction(message_id, table, "sport", old_sport, new_sport, ctx.author.id)
        if new_league != old_league:
            log_correction(message_id, table, "league", old_league, new_league, ctx.author.id)
    conn.commit()
    if not found:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return
    await ctx.send(f"✅ Bet sport updated to **{new_sport}**.")


@bot.command(name="settype", aliases=["setcategory", "set_wager_type"])
@commands.has_permissions(manage_guild=True)
async def settype_cmd(ctx: commands.Context, *, type_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send("Reply to the original bet and use `bt!settype DFS`, `straight`, or `parlay`.")
        return
    category = canonical_wager_category(type_arg)
    if not category:
        await ctx.send("Wager type not recognized. Use `straight`, `parlay`, or `DFS`.")
        return

    found = False
    for table in ("pending", "bets"):
        row = cur.execute(
            f"SELECT wager_category FROM {table} WHERE message_id = ?", (message_id,)
        ).fetchone()
        if not row:
            continue
        found = True
        old = str(row[0] or WAGER_STRAIGHT)
        cur.execute(
            f"UPDATE {table} SET wager_category = ? WHERE message_id = ?",
            (category, message_id),
        )
        log_correction(message_id, table, "wager_category", old, category, ctx.author.id)
    conn.commit()
    if not found:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return
    await ctx.send(f"✅ Wager type updated to **{wager_category_label(category)}**.")


@bot.command(name="setplatform", aliases=["setbook", "setapp", "set_platform"])
@commands.has_permissions(manage_guild=True)
async def setplatform_cmd(ctx: commands.Context, *, platform_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send("Reply to the original bet and use `bt!setplatform Sleeper`.")
        return
    platform, platform_type = canonical_platform(platform_arg)
    if not platform:
        await ctx.send("Platform cannot be blank.")
        return

    found = False
    for table in ("pending", "bets"):
        row = cur.execute(
            f"SELECT platform, platform_type, sportsbook, wager_category FROM {table} WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if not row:
            continue
        found = True
        old_platform, old_platform_type, _old_sportsbook, old_category = row
        resolved_type = platform_type or ("DFS_APP" if str(old_category).upper() == WAGER_DFS else "SPORTSBOOK")
        new_category = WAGER_DFS if resolved_type == "DFS_APP" else str(old_category or WAGER_STRAIGHT)
        cur.execute(
            f"""
            UPDATE {table}
            SET platform = ?, sportsbook = ?, platform_type = ?, wager_category = ?
            WHERE message_id = ?
            """,
            (platform, platform, resolved_type, new_category, message_id),
        )
        log_correction(message_id, table, "platform", old_platform, platform, ctx.author.id)
        log_correction(message_id, table, "platform_type", old_platform_type, resolved_type, ctx.author.id)
        if new_category != str(old_category or WAGER_STRAIGHT):
            log_correction(message_id, table, "wager_category", old_category, new_category, ctx.author.id)
    conn.commit()
    if not found:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return
    await ctx.send(f"✅ Platform updated to **{platform}**.")


@bot.command(name="setmarket", aliases=["set_market", "setbet", "setdescription"])
@commands.has_permissions(manage_guild=True)
async def setmarket_cmd(ctx: commands.Context, *, market_arg: str) -> None:
    message_id = referenced_message_id(ctx)
    if message_id is None:
        await ctx.send("Reply to the original bet and use `bt!setmarket Zack Wheeler over 5.5 Strikeouts`.")
        return
    new_market = normalize_space(market_arg)[:250]
    if not new_market:
        await ctx.send("Market cannot be blank.")
        return
    inferred_type = infer_bet_type(new_market)
    inferred_line = parse_line(new_market)
    inferred_player = parse_player(new_market)

    found = False
    for table in ("pending", "bets"):
        row = cur.execute(
            f"SELECT market, bet_type, line, player FROM {table} WHERE message_id = ?", (message_id,)
        ).fetchone()
        if not row:
            continue
        found = True
        old_market, old_type, old_line, old_player = [str(x or "") for x in row]
        new_type = inferred_type or old_type
        new_line = inferred_line or old_line
        new_player = inferred_player or old_player
        cur.execute(
            f"UPDATE {table} SET market = ?, bet_type = ?, line = ?, player = ? WHERE message_id = ?",
            (new_market, new_type, new_line, new_player, message_id),
        )
        log_correction(message_id, table, "market", old_market, new_market, ctx.author.id)
        if new_type != old_type:
            log_correction(message_id, table, "bet_type", old_type, new_type, ctx.author.id)
        if new_line != old_line:
            log_correction(message_id, table, "line", old_line, new_line, ctx.author.id)
        if new_player != old_player:
            log_correction(message_id, table, "player", old_player, new_player, ctx.author.id)
    conn.commit()
    if not found:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return
    await ctx.send(f"✅ Bet market updated to **{new_market}**.")


@bot.command(name="force_sport", aliases=["forcesport", "bulk_sport"])
@commands.has_permissions(manage_guild=True)
async def force_sport_cmd(
    ctx: commands.Context,
    old_sport_arg: str,
    new_sport_arg: str,
    start_date_arg: str,
    end_date_arg: str,
) -> None:
    old_sport = canonical_sport(old_sport_arg)
    new_sport = canonical_sport(new_sport_arg)
    start_day = parse_date_yyyy_mm_dd(start_date_arg)
    end_day = parse_date_yyyy_mm_dd(end_date_arg)
    if not old_sport or not new_sport or not start_day or not end_day:
        await ctx.send("Use `bt!force_sport NBA WNBA 2026-07-01 2026-07-31`.")
        return
    if end_day < start_day:
        await ctx.send("The ending date must be on or after the starting date.")
        return
    if old_sport == new_sport:
        await ctx.send("The old and new sport are the same; nothing was changed.")
        return

    start_iso, end_iso = start_day.isoformat(), end_day.isoformat()
    bet_rows = cur.execute(
        """
        SELECT message_id, sport, league
        FROM bets
        WHERE guild_id = ? AND UPPER(sport) = ?
          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10), substr(created_utc, 1, 10)) BETWEEN ? AND ?
        """,
        (int(ctx.guild.id), old_sport, start_iso, end_iso),
    ).fetchall()
    pending_rows = cur.execute(
        """
        SELECT message_id, sport, league
        FROM pending
        WHERE guild_id = ? AND UPPER(sport) = ?
          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) BETWEEN ? AND ?
        """,
        (int(ctx.guild.id), old_sport, start_iso, end_iso),
    ).fetchall()

    for table, rows in (("bets", bet_rows), ("pending", pending_rows)):
        for message_id, previous_sport, previous_league in rows:
            new_league = default_league_for_manual_sport(new_sport, str(previous_league or ""), str(previous_sport or ""))
            cur.execute(
                f"UPDATE {table} SET sport = ?, league = ? WHERE message_id = ?",
                (new_sport, new_league, int(message_id)),
            )
            log_correction(message_id, table, "sport", previous_sport, new_sport, ctx.author.id, "Bulk sport correction")
            if str(previous_league or "") != new_league:
                log_correction(message_id, table, "league", previous_league, new_league, ctx.author.id, "Bulk sport correction")
    conn.commit()
    await ctx.send(
        "✅ **Bulk Sport Correction Complete**\n"
        f"Period: **{start_iso} → {end_iso}**\n"
        f"Changed: **{old_sport} → {new_sport}**\n"
        f"Graded bets updated: **{len(bet_rows)}**\n"
        f"Pending bets updated: **{len(pending_rows)}**\n"
        "Results, units, odds, cappers, and dates were not changed."
    )


@bot.command(name="migrateguild", aliases=["previewguildmigration"])
@commands.has_permissions(manage_guild=True)
async def migrateguild_cmd(ctx: commands.Context, mode: str = "preview") -> None:
    """Owner-only preview/confirm repair of legacy guild_id=0 rows."""
    if ctx.guild is None or int(ctx.author.id) != int(ADMIN_USER_ID):
        await ctx.send("Only the BetTracker platform owner may migrate historical records.")
        return
    mode = mode.lower().strip()
    if mode not in {"preview", "confirm"}:
        await ctx.send("Use `bt!migrateguild preview` or `bt!migrateguild CONFIRM`.")
        return

    # Prove that a legacy row belongs to this guild using a valid Discord
    # jump URL, or a configured tracker channel actually found in this guild.
    registered_ids = set(TRACKED_CHANNELS) | set(SHARED_TRACKING_CHANNEL_IDS)
    valid_channels = sorted(
        channel_id for channel_id in registered_ids
        if ctx.guild.get_channel(channel_id) is not None
    )
    filters = ["jump_url LIKE ?"]
    values: List[object] = [f"https://discord.com/channels/{int(ctx.guild.id)}/%"]
    if valid_channels:
        filters.append("channel_id IN (" + ",".join("?" for _ in valid_channels) + ")")
        values.extend(valid_channels)
    match_sql = "(" + " OR ".join(filters) + ")"

    counts = {}
    for table in ("bets", "pending"):
        counts[table] = int(cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE guild_id = 0 AND {match_sql}",
            tuple(values),
        ).fetchone()[0])

    if mode == "preview":
        await ctx.send(
            f"🔎 **Legacy Data Migration Preview**\n"
            f"Server: **{ctx.guild.name}** (`{ctx.guild.id}`)\n"
            f"Historical graded bets: **{counts['bets']}**\n"
            f"Historical pending bets: **{counts['pending']}**\n"
            f"Verified configured channels: **{len(valid_channels)}**\n"
            "No data changed. Back up the database first. Then run "
            "`bt!migrateguild CONFIRM` in this same server."
        )
        return

    if counts["bets"] + counts["pending"] == 0:
        await ctx.send("✅ No qualifying legacy records need migration.")
        return

    changes = {}
    for table in ("bets", "pending"):
        changes[table] = cur.execute(
            f"UPDATE {table} SET guild_id = ? WHERE guild_id = 0 AND {match_sql}",
            (int(ctx.guild.id), *values),
        ).rowcount
    conn.commit()
    await ctx.send(
        f"✅ **Historical server migration completed**\n"
        f"Graded bets: **{changes['bets']}** | Pending: **{changes['pending']}**\n"
        "Unverified legacy records were left untouched."
    )


@bot.command(name="backup", aliases=["backupdb", "databasebackup"])
@commands.has_permissions(manage_guild=True)
async def backup_cmd(ctx: commands.Context) -> None:
    if int(ctx.author.id) != int(ADMIN_USER_ID):
        await ctx.send("Full database backups are restricted to the BetTracker platform owner. Use `bt!export` for server data.")
        return

    timestamp = now_local().strftime("%Y%m%d_%H%M%S")
    filename = f"bettracker_backup_{timestamp}.db"
    temp_path = os.path.join("/tmp", filename)
    try:
        conn.commit()
        destination = sqlite3.connect(temp_path)
        try:
            conn.backup(destination)
        finally:
            destination.close()
        await ctx.send(
            "💾 **BetTracker Database Backup**\n"
            "Store this file somewhere secure. It contains the complete tracker database.",
            file=discord.File(temp_path, filename=filename),
        )
    except Exception as exc:
        await ctx.send(f"❌ Backup failed: `{type(exc).__name__}`")
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


@bot.command(name="export", aliases=["exportcsv", "csv"])
@commands.has_permissions(manage_guild=True)
async def export_cmd(ctx: commands.Context, *, query: str = "alltime") -> None:
    tokens = split_args(query)
    capper, remaining = resolve_capper_from_tokens(tokens)
    if capper is None:
        remaining = tokens
    sport, category, remaining = resolve_optional_filters(remaining)
    time_text = " ".join(remaining)
    label, start_local, end_local, error = parse_time_filter(time_text)
    if error:
        await ctx.send(
            "Could not understand the export. Try `bt!export july`, "
            "`bt!export PropKitchen july`, or `bt!export PropKitchen MLB dfs 2026-07`."
        )
        return

    where_parts: List[str] = []
    params: List[object] = []
    labels: List[str] = []
    if ctx.guild is not None:
        where_parts.append("guild_id = ?")
        params.append(int(ctx.guild.id))
    if capper:
        where_parts.append("LOWER(capper) = ?")
        params.append(capper.lower())
        labels.append(capper)
    if sport:
        where_parts.append("UPPER(sport) = ?")
        params.append(sport)
        labels.append(sport)
    if category:
        where_parts.append(f"({WAGER_CATEGORY_SQL}) = ?")
        params.append(category)
        labels.append(category.lower())
    add_time_filter(where_parts, params, start_local, end_local)
    where_sql, final_params = build_where(where_parts, params)

    export_cursor = cur.execute(
        f"""
        SELECT *, {WAGER_CATEGORY_SQL} AS normalized_wager_category
        FROM bets
        WHERE {where_sql}
        ORDER BY COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)), graded_utc
        """,
        final_params,
    )
    rows = export_cursor.fetchall()
    if not rows:
        await ctx.send("No graded bets matched that export.")
        return
    headers = [str(column[0]) for column in export_cursor.description]
    raw_csv = str(getattr(ctx, "invoked_with", "") or "").lower() in {"exportcsv", "csv"}
    if not raw_csv:
        payload = io.BytesIO(export_xlsx(headers, rows, clean_period_label(label)))
        payload.seek(0)
        safe_parts = [normalize_key(part) or "all" for part in labels]
        period_name = normalize_key(clean_period_label(label)) or "alltime"
        filename = "bettracker_" + "_".join([*safe_parts, period_name]) + ".xlsx"
        await ctx.send(
            f"📊 **BetTracker Excel Report**\n"
            f"Rows: **{len(rows)}** | Period: **{clean_period_label(label)}**\n"
            "Includes Summary and Bets worksheets.",
            file=discord.File(payload, filename=filename),
        )
        return

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    payload = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    payload.seek(0)

    safe_parts = [normalize_key(part) or "all" for part in labels]
    period_name = normalize_key(clean_period_label(label)) or "alltime"
    safe_parts.append(period_name)
    filename = "bettracker_" + "_".join(safe_parts) + ".csv"
    await ctx.send(
        f"📤 **BetTracker Export**\nRows: **{len(rows)}** | Period: **{clean_period_label(label)}**",
        file=discord.File(payload, filename=filename),
    )


@bot.command(name="leaderboard", aliases=["leaders", "lb"])
async def leaderboard_cmd(ctx: commands.Context, *, query: str = "") -> None:
    tokens = split_args(query)
    sport, category, remaining = resolve_optional_filters(tokens)
    await post_leaderboard_query(
        ctx,
        " ".join(remaining),
        sport_name=sport,
        wager_category=category,
    )


@bot.command(name="sport")
async def sport_cmd(ctx: commands.Context, *, query: str) -> None:
    tokens = split_args(query)
    sport, remaining = resolve_sport_from_tokens(tokens)
    if not sport:
        await ctx.send(
            "Use format: `bt!sport WNBA`, `bt!sport MLB july`, "
            "or `bt!sport Tennis thisweek`."
        )
        return
    category, remaining = resolve_wager_category_from_tokens(remaining)
    await post_leaderboard_query(
        ctx,
        " ".join(remaining),
        sport_name=sport,
        wager_category=category,
    )


@bot.command(name="league")
async def league_cmd(ctx: commands.Context, *, query: str) -> None:
    league_name, time_text = split_name_and_time_filter(query)
    if not league_name:
        await ctx.send('Use format: `bt!league "Premier League" july`.')
        return
    await post_leaderboard_query(ctx, time_text, league_name=league_name)


@bot.command(name="capper", aliases=["bets"])
async def capper_cmd(ctx: commands.Context, *, query: str) -> None:
    tokens = split_args(query)
    page = 1
    if len(tokens) >= 2 and normalize_key(tokens[-2]) == "page":
        try:
            page = int(tokens[-1])
        except ValueError:
            await ctx.send("Use a page number like `page 2`.")
            return
        tokens = tokens[:-2]
    if not 1 <= page <= 10000:
        await ctx.send("Choose a page between 1 and 10000.")
        return
    wants_report = any(normalize_key(token) == "report" for token in tokens)
    tokens = [token for token in tokens if normalize_key(token) not in {"report", "recap", "summary"}]

    capper, remaining = resolve_capper_from_tokens(tokens)
    if not capper:
        await ctx.send(
            "Use `bt!capper PropKitchen july`, `bt!capper PropKitchen strikeouts july`, "
            "`bt!capper PropKitchen player Zack Wheeler july`, or `bt!report PropKitchen july`."
        )
        return

    if wants_report:
        await post_master_report(ctx, capper, " ".join(remaining))
        return

    sport, category, remaining = resolve_optional_filters(remaining)
    remaining_text = " ".join(remaining).strip()
    _whole_label, _whole_start, _whole_end, whole_time_error = parse_time_filter(remaining_text, sport=sport)
    if whole_time_error is None:
        search_text, time_text = "", remaining_text
    else:
        search_text, time_text = split_name_and_time_filter(remaining_text)
    search_text = search_text.strip()

    where_parts = ["LOWER(capper) = ?"]
    params: List[object] = [capper.lower()]
    title = f"Capper: {capper}"
    filter_label: Optional[str] = None

    if sport:
        where_parts.append("UPPER(sport) = ?")
        params.append(sport)
        title += f" — {sport}"

    if category:
        where_parts.append(f"({WAGER_CATEGORY_SQL}) = ?")
        params.append(category)
        title += f" — {wager_category_label(category)}"

    if search_text:
        search_tokens = split_args(search_text)
        explicit = normalize_key(search_tokens[0]) if search_tokens else ""
        if explicit in {"player", "athlete"}:
            value_text = " ".join(search_tokens[1:]).strip()
            if not value_text:
                await ctx.send("Use `bt!capper PropKitchen player Zack Wheeler july`.")
                return
            value = f"%{value_text.lower()}%"
            where_parts.append("(LOWER(player) LIKE ? OR LOWER(content) LIKE ?)")
            params.extend([value, value])
            filter_label = f"Player: {value_text}"
        else:
            if explicit in {"bettype", "market", "type"}:
                value_text = " ".join(search_tokens[1:]).strip()
            else:
                value_text = search_text
            if not value_text:
                await ctx.send("Use `bt!capper PropKitchen bettype strikeouts july`.")
                return
            value = f"%{value_text.lower()}%"
            where_parts.append(
                "(LOWER(bet_type) LIKE ? OR LOWER(market) LIKE ? OR LOWER(content) LIKE ?)"
            )
            params.extend([value, value, value])
            filter_label = f"Bet Type: {value_text}"

    await post_query_summary(
        ctx,
        title,
        where_parts,
        params,
        time_text,
        capper_name=capper,
        sport_name=sport,
        wager_category=category,
        filter_label=filter_label,
        page=page,
    )

@bot.command(name="recap", aliases=["recaps"])
async def recap_cmd(ctx: commands.Context, *, query: str = "") -> None:
    tokens = split_args(query)
    capper, _remaining = resolve_capper_from_tokens(tokens)
    if capper is None:
        own_capper = capper_by_user_id(ctx.author.id)
        if own_capper is None:
            await ctx.send("Use `bt!recap PropKitchen October 2026` or `bt!recap gr8 last 14 days`.")
            return
        query = f"{own_capper.name} {query}".strip()
    await capper_cmd(ctx, query=query)


@bot.command(name="report", aliases=["masterreport", "capperreport"])
async def report_cmd(ctx: commands.Context, *, query: str) -> None:
    tokens = [token for token in split_args(query) if normalize_key(token) != "report"]
    capper, remaining = resolve_capper_from_tokens(tokens)
    if not capper:
        await ctx.send(
            "Use `bt!report PropKitchen july`, `bt!report gr8 2026`, "
            "or `bt!report DaijonBets thisweek`."
        )
        return
    await post_master_report(ctx, capper, " ".join(remaining))


@bot.command(name="player")
async def player_cmd(ctx: commands.Context, *, query: str) -> None:
    player_name, time_text = split_name_and_time_filter(query)
    if not player_name:
        await ctx.send("Use `bt!player Zack Wheeler july`.")
        return
    value = f"%{player_name.strip().lower()}%"
    where_parts = ["(LOWER(player) LIKE ? OR LOWER(content) LIKE ?)"]
    params: List[object] = [value, value]
    await post_query_summary(
        ctx,
        f"Player Search: {player_name.strip()}",
        where_parts,
        params,
        time_text,
        filter_label=f"Player: {player_name.strip()}",
    )

@bot.command(name="bettype")
async def bettype_cmd(ctx: commands.Context, *, query: str) -> None:
    bet_type, time_text = split_name_and_time_filter(query)
    if not bet_type:
        await ctx.send("Use `bt!bettype Strikeouts july`.")
        return
    value = f"%{bet_type.strip().lower()}%"
    where_parts = ["(LOWER(bet_type) LIKE ? OR LOWER(market) LIKE ? OR LOWER(content) LIKE ?)"]
    params: List[object] = [value, value, value]
    await post_query_summary(
        ctx,
        f"Bet Type: {bet_type.strip()}",
        where_parts,
        params,
        time_text,
        filter_label=f"Bet Type: {bet_type.strip()}",
    )

@bot.command(name="month")
async def month_cmd(ctx: commands.Context, ym: str) -> None:
    await post_leaderboard_query(ctx, ym)


@bot.command(name="year")
async def year_cmd(ctx: commands.Context, yyyy: str) -> None:
    await post_leaderboard_query(ctx, yyyy)


@bot.command(name="range")
async def range_cmd(ctx: commands.Context, start_date: str, end_date: str) -> None:
    await post_leaderboard_query(ctx, f"{start_date} {end_date}")


@bot.command(name="data_issues", aliases=["dataissues", "issues"])
@commands.has_permissions(manage_guild=True)
async def data_issues_cmd(ctx: commands.Context, *, time_filter: str = "thismonth") -> None:
    label, start_l, end_l, error = parse_time_filter(time_filter)
    if error:
        await ctx.send(error)
        return

    where_parts: List[str] = []
    params: List[object] = []
    if ctx.guild is not None:
        where_parts.append("guild_id = ?")
        params.append(int(ctx.guild.id))
    add_time_filter(where_parts, params, start_l, end_l)
    where_sql, final_params = build_where(where_parts, params)

    rows = cur.execute(
        f"""
        SELECT capper, content, market, odds_text, sport, bet_date, league, jump_url, graded_utc
        FROM bets
        WHERE {where_sql}
        ORDER BY graded_utc DESC
        """,
        final_params,
    ).fetchall()

    counts = {
        "missing_description": 0,
        "missing_odds": 0,
        "unknown_sport": 0,
        "missing_date": 0,
        "missing_league": 0,
        "missing_jump": 0,
    }
    affected: List[Tuple[str, List[str], str, str]] = []

    for capper_name, content, market, odds_text, sport, bet_date, league, jump_url, _graded in rows:
        issues: List[str] = []
        bet_text = display_bet_text(str(content or ""), str(market or ""))
        if bet_text == "Bet details unavailable":
            counts["missing_description"] += 1
            issues.append("description")
        if not str(odds_text or "").strip():
            counts["missing_odds"] += 1
            issues.append("odds")
        if str(sport or "").upper() in {"", "UNKNOWN"}:
            counts["unknown_sport"] += 1
            issues.append("sport")
        if not str(bet_date or "").strip():
            counts["missing_date"] += 1
            issues.append("date")
        if not str(league or "").strip():
            counts["missing_league"] += 1
            issues.append("league")
        if not str(jump_url or "").strip():
            counts["missing_jump"] += 1
            issues.append("jump")
        if issues and len(affected) < 10:
            affected.append((str(capper_name), issues, bet_text, str(jump_url or "")))

    total_issues = sum(counts.values())
    lines = [
        "🧹 **BetTracker Data Quality Check**",
        f"Period: **{clean_period_label(label)}**",
        f"Bets checked: **{len(rows)}**",
        f"Total field issues: **{total_issues}**",
        "",
        f"Missing exact description: **{counts['missing_description']}**",
        f"Missing odds: **{counts['missing_odds']}**",
        f"Unknown sport: **{counts['unknown_sport']}**",
        f"Missing bet date: **{counts['missing_date']}**",
        f"Missing league: **{counts['missing_league']}**",
        f"Missing jump link: **{counts['missing_jump']}**",
    ]

    if affected:
        lines.append("\n**Recent Bets Needing Attention**")
        for capper_name, issues, bet_text, jump_url in affected:
            jump = f"[jump]({jump_url})" if jump_url else "jump unavailable"
            lines.append(f"• **{capper_name}** — {', '.join(issues)} | {bet_text} | {jump}")
    else:
        lines.append("\n✅ No data issues found for this period.")

    for chunk in split_discord_text("\n".join(lines)):
        await ctx.send(chunk)


@bot.command(name="reportcheck", aliases=["checkreports"])
@commands.has_permissions(manage_guild=True)
async def reportcheck_cmd(ctx: commands.Context) -> None:
    """Read-only check for a failed capper results command."""
    if ctx.guild is None:
        await ctx.send("Run this inside the PropKitchen server.")
        return

    guild_id = int(ctx.guild.id)
    counts = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(net_units), 0) FROM bets WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    pending_count = int(cur.execute(
        "SELECT COUNT(*) FROM pending WHERE guild_id = ?", (guild_id,)
    ).fetchone()[0])

    me = ctx.guild.me
    permissions = ctx.channel.permissions_for(me) if me is not None else None
    embed_permission = (
        "Yes" if permissions and permissions.embed_links
        else "No" if permissions is not None else "Unknown"
    )

    yesterday = now_local().date() - timedelta(days=1)
    start_local, end_local = local_day_bounds(yesterday)
    date_sql, date_params = date_window_where(start_local, end_local)
    where = f"guild_id = ? AND LOWER(capper) = ? AND ({date_sql})"
    params = (guild_id, "propkitchen", *date_params)

    try:
        report = build_filtered_summary_text(
            "PropKitchen", where, params,
            capper_name="PropKitchen", period_label="Yesterday",
        )
        query_result = f"PASS ({len(report):,} characters)"
    except Exception as exc:
        print(f"BetTracker reportcheck query error: {type(exc).__name__}: {exc}")
        query_result = f"FAILED ({type(exc).__name__})"

    await ctx.send(
        "🔎 **BetTracker Report Diagnostics**\n"
        f"Server graded bets: **{int(counts[0]):,}**\n"
        f"Server pending bets: **{pending_count:,}**\n"
        f"Net units in database: **{float(counts[1]):+.2f}u**\n"
        f"Embed Links permission: **{embed_permission}**\n"
        f"PropKitchen yesterday recap query: **{query_result}**\n"
        "Read-only check — no betting data changed."
    )


# =====================
# COMMAND ERRORS
# =====================

async def notify_command_failure(ctx: commands.Context, error: Exception) -> None:
    """No tracking/reporting command should fail without telling the capper."""
    underlying = getattr(error, "original", error)
    print(
        f"BetTracker command failed: {type(underlying).__name__}: {underlying} "
        f"(command={getattr(ctx.message, 'content', '')[:100]!r})"
    )
    try:
        await ctx.send(
            "⚠️ **BetTracker couldn't finish that command.** "
            "No betting records were changed by this report. "
            "Please tell an admin which command failed so they can check the bot logs."
        )
    except discord.HTTPException:
        print("BetTracker could not send the command error notice.")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if ctx.guild is None:
        await ctx.send("BetTracker commands must be used inside a Discord server.")
        return
    if isinstance(error, commands.CommandNotFound):
        raw = ctx.message.content[len(ctx.prefix):].strip() if ctx.prefix else ""
        tokens = split_args(raw)
        if tokens:
            recognized, _rest = resolve_capper_from_tokens(tokens)
            if recognized:
                try:
                    await ctx.invoke(capper_cmd, query=raw)
                except Exception as exc:
                    await notify_command_failure(ctx, exc)
                return
            sport, category, remaining = resolve_optional_filters(tokens)
            time_text = " ".join(remaining)
            if sport or category:
                label, _start, _end, problem = parse_time_filter(time_text, sport=sport)
                if problem is None:
                    await post_leaderboard_query(ctx, time_text, sport_name=sport, wager_category=category)
                    return
            label, _start, _end, problem = parse_time_filter(raw)
            if problem is None and label != "All-Time":
                await post_leaderboard_query(ctx, raw)
                return
        await ctx.send("Unknown command. Try `bt!commands`, `bt!PropKitchen NBA October`, or `bt!last 14 days NBA`.")
        return
    if isinstance(error, commands.CheckFailure) and ctx.guild is None:
        await ctx.send("BetTracker commands must be used inside a Discord server.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the **Manage Server** permission to run that command.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing argument. Try `bt!month 2026-07`, `bt!leaderboard MLB july`, or `bt!capper PropKitchen today`.")
        return
    await notify_command_failure(ctx, error)


# =====================
# AUTOPOST
# =====================

async def ping_monthly_vip_roles(channel: discord.abc.Messageable, month_label: str) -> None:
    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    setting = cur.execute(
        "SELECT monthly_role_ids FROM recap_settings WHERE guild_id = ?", (int(guild.id),)
    ).fetchone()
    if not setting or not setting[0]:
        return
    role_ids = [int(v) for v in setting[0].split(",") if v.isdigit()]
    roles = [guild.get_role(role_id) for role_id in role_ids]
    roles = [role for role in roles if role is not None and not role.is_default()]
    if not roles:
        return
    await channel.send(
        f"📅 **{month_label} Monthly VIP Recap**\n"
        + " ".join(role.mention for role in roles)
        + "\nReview all the monthly results above, including losses and ROI.",
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=roles),
    )


def autopost_was_sent(cadence: str, period_key: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM autopost_history WHERE cadence = ? AND period_key = ?",
        (cadence, period_key),
    ).fetchone() is not None


def remember_autopost(cadence: str, period_key: str) -> None:
    cur.execute(
        """
        INSERT OR IGNORE INTO autopost_history (cadence, period_key, posted_utc)
        VALUES (?, ?, ?)
        """,
        (cadence, period_key, utc_iso(datetime.now(timezone.utc))),
    )
    conn.commit()


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
    if _last_post_key["daily"] != day_key and not autopost_was_sent("daily", day_key):
        _last_post_key["daily"] = day_key
        start_l, end_l = period_bounds_local("daily", yesterday)
        await post_period_summary(channel, banner(f"DAILY ({day_key})"), start_l, end_l)
        remember_autopost("daily", day_key)

    # Weekly: previous week on Monday
    if nl.weekday() == WEEKLY_POST_WEEKDAY:
        prev_week_ref = nl.date() - timedelta(days=7)
        start_l, end_l = period_bounds_local("weekly", prev_week_ref)
        week_start = start_l.date()
        week_end = (end_l - timedelta(days=1)).date()
        week_key = f"{week_start.isoformat()}_{week_end.isoformat()}"
        if _last_post_key["weekly"] != week_key and not autopost_was_sent("weekly", week_key):
            _last_post_key["weekly"] = week_key
            await post_period_summary(channel, banner(f"WEEKLY ({week_start} → {week_end})"), start_l, end_l)
            remember_autopost("weekly", week_key)

    # Monthly: previous month on the 3rd
    if nl.day == MONTHLY_POST_DAY:
        prev_month_ref = nl.date().replace(day=1) - timedelta(days=1)
        start_l, end_l = period_bounds_local("monthly", prev_month_ref)
        month_label = f"{start_l.year}-{start_l.month:02d}"
        if _last_post_key["monthly"] != month_label and not autopost_was_sent("monthly", month_label):
            _last_post_key["monthly"] = month_label
            await post_period_summary(channel, banner(f"MONTHLY ({month_label})"), start_l, end_l)
            try:
                await ping_monthly_vip_roles(channel, month_label)
            except discord.DiscordException as exc:
                print(f"Monthly VIP role ping failed: {type(exc).__name__}")
            remember_autopost("monthly", month_label)

    # Yearly: previous year on Jan 1
    if nl.month == 1 and nl.day == 1:
        prev_year_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("yearly", prev_year_ref)
        year_label = f"{start_l.year}"
        if _last_post_key["yearly"] != year_label and not autopost_was_sent("yearly", year_label):
            _last_post_key["yearly"] = year_label
            await post_period_summary(channel, banner(f"YEARLY ({year_label})"), start_l, end_l)
            remember_autopost("yearly", year_label)


if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable (set it in Render Environment).")

bot.run(TOKEN)
