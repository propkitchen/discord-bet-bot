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
import shlex
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
            channel_id INTEGER NOT NULL,
            capper TEXT NOT NULL,
            capper_user_id INTEGER NOT NULL,
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
            grade_reaction TEXT NOT NULL DEFAULT ''
        )
        """
    )

    # Safe migrations for existing Render database.
    pending_columns = {
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
    }
    for col, definition in pending_columns.items():
        add_column_if_missing("pending", col, definition)

    bet_columns = {
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
    }
    for col, definition in bet_columns.items():
        add_column_if_missing("bets", col, definition)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_created_utc ON bets(created_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_date ON bets(bet_date);")
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
    """Parse American, multiplier, or decimal odds from a play message."""
    m = RE_AMERICAN_PAREN.search(text)
    if m:
        return m.group(1)

    m = RE_MULT.search(text)
    if m:
        return f"{m.group(1)}x"

    # Signed American odds can appear with or without parentheses, including +715.
    m = RE_AMERICAN.search(text)
    if m:
        return m.group(1)

    # Decimal odds must be clearly labeled to avoid confusing a prop line with odds.
    # Supported examples: Odds: 2.50, Odds 2.50, @ 2.50, 2.50 decimal.
    for regex in (RE_DECIMAL_CONTEXT, RE_DECIMAL_SUFFIX):
        m = regex.search(text)
        if not m:
            continue
        try:
            decimal_odds = float(m.group(1))
        except Exception:
            continue
        if decimal_odds > 1.0:
            return m.group(1)

    return ""


def parse_manual_odds_text(value: str) -> str:
    """Normalize odds supplied directly to an admin correction command."""
    clean = normalize_space(value).replace(" ", "")

    if re.fullmatch(r"[+-]\d{2,5}", clean):
        return clean

    if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)x", clean, flags=re.IGNORECASE):
        try:
            mult = float(clean[:-1])
        except Exception:
            return ""
        return f"{mult:g}x" if mult > 1.0 else ""

    if re.fullmatch(r"\d{1,3}(?:\.\d+)?", clean):
        try:
            decimal_odds = float(clean)
        except Exception:
            return ""
        if decimal_odds > 1.0:
            # Keep at least two decimal places so it is visibly decimal odds.
            return f"{decimal_odds:.2f}"

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
    if re.fullmatch(r"[+-]\d{2,5}", odds_text):
        return "american"
    try:
        decimal_odds = float(odds_text)
        if decimal_odds > 1.0 and "." in odds_text:
            return "decimal"
    except Exception:
        pass
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
    # Multiplier is total return, so subtract the original stake for net profit.
    # Example: .25u at 3x returns .75u total and profits .50u.
    return risk * (mult - 1.0)


def profit_from_decimal(risk: float, decimal_odds: float) -> float:
    # Decimal odds are total return, so net profit is risk * (odds - 1).
    return risk * (decimal_odds - 1.0)


def compute_net_units(risk: float, odds_text: str, result: str) -> float:
    result = result.lower()
    if result == "push":
        return 0.0
    if result == "loss":
        return -risk

    clean_odds = str(odds_text or "").strip()
    if not clean_odds:
        # Preserve legacy behavior for a win when no odds were supplied.
        return risk

    if clean_odds.lower().endswith("x"):
        try:
            mult = float(clean_odds[:-1])
            return profit_from_multiplier(risk, mult) if mult > 1.0 else 0.0
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

def date_window_where(start_local: datetime, end_local: datetime) -> Tuple[str, Tuple[object, ...]]:
    """
    Filter by intended bet/result date.

    New rows use bets.bet_date, which can come from text like (7/10).
    Older rows without bet_date fall back to graded_utc.
    """
    start_date = start_local.date().isoformat()
    end_date = end_local.date().isoformat()
    start_utc = utc_iso(to_utc(start_local))
    end_utc = utc_iso(to_utc(end_local))
    return (
        "((bet_date >= ? AND bet_date < ?) OR ((bet_date IS NULL OR bet_date = '') AND graded_utc >= ? AND graded_utc < ?))",
        (start_date, end_date, start_utc, end_utc),
    )


def fetch_capper_rows(start_local: datetime, end_local: datetime) -> List[Tuple[str, float, int, int, int]]:
    where_sql, params = date_window_where(start_local, end_local)
    rows = cur.execute(
        f"""
        SELECT
            capper,
            COALESCE(SUM(net_units), 0) AS net_units_sum,
            SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE {where_sql}
        GROUP BY capper
        ORDER BY net_units_sum DESC
        """,
        params,
    ).fetchall()
    return [(str(c), float(u), int(w or 0), int(l or 0), int(p or 0)) for c, u, w, l, p in rows]


def fetch_vip_totals(start_local: datetime, end_local: datetime) -> Tuple[float, int, int, int]:
    where_sql, params = date_window_where(start_local, end_local)
    row = cur.execute(
        f"""
        SELECT
            COALESCE(SUM(net_units), 0) AS net_units_sum,
            SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
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
    where_sql, params = date_window_where(start_local, end_local)
    await post_leaderboard(
        channel,
        title,
        format_period_window(start_local, end_local),
        where_sql,
        params,
        include_chart=True,
    )


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


def tracked_capper_display_names() -> List[str]:
    """Return every configured capper once, preserving config order."""
    names: List[str] = []
    seen: set[str] = set()
    for capper in TRACKED_CHANNELS.values():
        key = capper.name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(capper.name)
    return names


def fetch_leaderboard_rows(
    where_sql: str,
    params: Tuple[object, ...],
    include_zero_cappers: bool = True,
) -> List[Tuple[str, int, float, float, int, int, int, float, float]]:
    """
    Return name, bets, risked, profit, wins, losses, pushes, win rate, and ROI.

    Active cappers rank by net units, then ROI, then number of bets.
    Configured cappers with no qualifying bets stay at the bottom.
    """
    db_rows = cur.execute(
        f"""
        SELECT
            LOWER(capper) AS capper_key,
            MAX(capper) AS capper_display,
            COUNT(*) AS total_bets,
            COALESCE(SUM(risk_units), 0) AS risk_sum,
            COALESCE(SUM(net_units), 0) AS net_sum,
            SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE {where_sql}
        GROUP BY LOWER(capper)
        """,
        params,
    ).fetchall()

    stats: Dict[str, Tuple[str, int, float, float, int, int, int]] = {}
    for key, display, total, risk, net, wins, losses, pushes in db_rows:
        stats[str(key)] = (
            str(display),
            int(total or 0),
            float(risk or 0.0),
            float(net or 0.0),
            int(wins or 0),
            int(losses or 0),
            int(pushes or 0),
        )

    ordered_names = tracked_capper_display_names()
    configured_keys = {name.lower() for name in ordered_names}
    for key, values in stats.items():
        if key not in configured_keys:
            ordered_names.append(values[0])

    rows: List[Tuple[str, int, float, float, int, int, int, float, float]] = []
    for name in ordered_names:
        values = stats.get(name.lower())
        if values is None:
            if not include_zero_cappers:
                continue
            total = 0
            risk = 0.0
            net = 0.0
            wins = losses = pushes = 0
        else:
            _display, total, risk, net, wins, losses, pushes = values

        graded = wins + losses
        win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
        roi = (net / risk * 100.0) if risk > 0 else 0.0
        rows.append((name, total, risk, net, wins, losses, pushes, win_pct, roi))

    rows.sort(
        key=lambda row: (
            row[1] == 0,
            -row[3],
            -row[8],
            -row[1],
            row[0].lower(),
        )
    )
    return rows


def format_period_window(start_local: datetime, end_local: datetime) -> str:
    """Create a compact user-facing label for a date window."""
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


def build_leaderboard_text(
    title: str,
    period_label: str,
    where_sql: str,
    params: Tuple[object, ...],
    sport_name: Optional[str] = None,
    league_name: Optional[str] = None,
) -> str:
    total, risk, net, wins, losses, pushes = fetch_filtered_totals(where_sql, params)
    graded = wins + losses
    win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
    roi = (net / risk * 100.0) if risk > 0 else 0.0
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")

    lines = [f"📊 **{title}**", f"Period: **{period_label}**"]
    if sport_name:
        lines.append(f"Sport: **{sport_name}**")
    if league_name:
        lines.append(f"League: **{league_name}**")
    lines.extend(
        [
            "",
            "**VIP TOTAL**",
            f"Record: **{record}** ({win_pct:.1f}%) | Bets: **{total}**",
            f"Risked: **{risk:.2f}u** | Net Units: **{net:+.2f}u** | ROI: **{roi:.1f}%**",
            "",
            "**Leaderboard — Sorted by Net Units**",
        ]
    )

    rows = fetch_leaderboard_rows(where_sql, params, include_zero_cappers=True)
    for rank, row in enumerate(rows, start=1):
        name, capper_total, capper_risk, capper_net, capper_w, capper_l, capper_p, capper_win_pct, capper_roi = row
        capper_record = f"{capper_w}-{capper_l}" + (f"-{capper_p}" if capper_p > 0 else "")
        if capper_total == 0:
            lines.append(f"{rank}. **{name}** — 0-0 | 0 bets | +0.00u")
            continue
        lines.append(f"{rank}. **{name}**")
        lines.append(f"Record: **{capper_record}** ({capper_win_pct:.1f}%) | Bets: **{capper_total}**")
        lines.append(
            f"Risked: **{capper_risk:.2f}u** | Net Units: **{capper_net:+.2f}u** | ROI: **{capper_roi:.1f}%**"
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
    include_chart: bool = False,
) -> None:
    text = build_leaderboard_text(
        title,
        period_label,
        where_sql,
        params,
        sport_name=sport_name,
        league_name=league_name,
    )
    for chunk in split_discord_text(text):
        await channel.send(chunk)

    if include_chart:
        rows = fetch_leaderboard_rows(where_sql, params, include_zero_cappers=False)
        chart_rows = [
            (name, net, wins, losses, pushes)
            for name, total, _risk, net, wins, losses, pushes, _wp, _roi in rows
            if total > 0
        ]
        if chart_rows:
            img = generate_units_chart(f"{title} Net Units", chart_rows)
            await channel.send(file=discord.File(img, filename="units.png"))


def fetch_recent_bets(
    where_sql: str,
    params: Tuple[object, ...],
    limit: int = 20,
) -> List[Tuple[str, str, str, float, float, str, str, str, str, str]]:
    rows = cur.execute(
        f"""
        SELECT graded_utc, capper, result, risk_units, net_units, content, market,
               odds_text, sport, jump_url
        FROM bets
        WHERE {where_sql}
        ORDER BY graded_utc ASC
        LIMIT ?
        """,
        (*params, int(limit)),
    ).fetchall()
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
        )
        for graded, capper, result, risk, net, content, market, odds, sport, jump in rows
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
    """Return only the wager description, not headers, dates, pings, or writeups."""
    raw = content or market or ""
    raw = re.sub(r"<@!?&?\d+>", "", raw)
    raw = re.sub(r"https?://\S+", "", raw)

    # The actual wager is normally the first line containing a stake.
    chosen = ""
    for line in raw.splitlines():
        if RE_UNITS.search(line):
            chosen = line
            break
    if not chosen:
        chosen = raw

    chosen = normalize_space(chosen)
    stake_match = RE_UNITS.search(chosen)
    if stake_match:
        chosen = chosen[stake_match.end():]

    odds_start = _find_odds_start(chosen)
    if odds_start is not None:
        chosen = chosen[:odds_start]

    chosen = RE_ISO_DATE.sub("", chosen)
    chosen = RE_SLASH_DATE.sub("", chosen)
    chosen = re.sub(r"(?i)\b(?:odds?|date)\s*[:=]?", "", chosen)
    chosen = normalize_space(chosen).strip(" -–—|:()")

    if not chosen:
        chosen = normalize_space(market) or "Bet details unavailable"
        stake_match = RE_UNITS.search(chosen)
        if stake_match:
            chosen = chosen[stake_match.end():]
        odds_start = _find_odds_start(chosen)
        if odds_start is not None:
            chosen = chosen[:odds_start]
        chosen = normalize_space(chosen).strip(" -–—|:()")

    if len(chosen) > max_len:
        chosen = chosen[: max_len - 3].rstrip() + "..."
    return chosen or "Bet details unavailable"


def clean_period_label(label: Optional[str]) -> str:
    if not label or label == "All-Time":
        return "All-Time"
    exact = re.search(r"(20\d{2}-\d{2}-\d{2})", label)
    if exact:
        return exact.group(1)
    if label.startswith("Month: "):
        return label.replace("Month: ", "", 1)
    if label.startswith("Year: "):
        return label.replace("Year: ", "", 1)
    if label.startswith("Range: "):
        return label.replace("Range: ", "", 1)
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


def build_filtered_summary_text(
    title: str,
    where_sql: str,
    params: Tuple[object, ...],
    capper_name: Optional[str] = None,
    period_label: str = "All-Time",
    sport_name: Optional[str] = None,
) -> str:
    total, risk, net, wins, losses, pushes = fetch_filtered_totals(where_sql, params)
    graded = wins + losses
    win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
    roi = (net / risk * 100.0) if risk > 0 else 0.0
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")

    if capper_name:
        lines = [f"📊 **Capper: {capper_name}**"]
        if sport_name:
            lines.append(f"Sport: **{sport_name}**")
        lines.extend(
            [
                f"Date: **{period_label}**",
                f"Record: **{record}** ({win_pct:.1f}%)",
                f"Risked: **{risk:.2f}u**",
                f"Net Units: **{net:+.2f}u**",
                f"ROI: **{roi:.1f}%**",
                f"Total Bets: **{total}**",
            ]
        )
    else:
        lines = [
            f"📊 **{title}**",
            f"Record: **{record}** ({win_pct:.1f}%)",
            f"Risked: **{risk:.2f}u**",
            f"Net Units: **{net:+.2f}u**",
            f"ROI: **{roi:.1f}%**",
            f"Total Bets: **{total}**",
        ]

    bets = fetch_recent_bets(where_sql, params, limit=25)
    if bets:
        lines.append("\n**Bets:**")
        for _graded, _capper, result, risk_units, net_units, content, market, odds_text, sport, jump_url in bets:
            icon = "✅" if result == "win" else ("❌" if result == "loss" else "➖")
            bet_text = display_bet_text(content, market)
            jump = f"[jump]({jump_url})" if jump_url else "jump unavailable"
            if capper_name:
                prefix = f"{icon} {format_compact_units(risk_units)}"
            else:
                prefix = f"{icon} **{_capper}** | {format_compact_units(risk_units)}"
            lines.append(
                f"{prefix} | {bet_text} | Odds: {format_odds_display(odds_text)} | "
                f"{sport} | {format_compact_units(net_units, signed=True)} | {jump}"
            )

    return "\n".join(lines)


async def post_filtered_summary(
    ctx: commands.Context,
    title: str,
    where_sql: str,
    params: Tuple[object, ...],
    capper_name: Optional[str] = None,
    period_label: str = "All-Time",
    sport_name: Optional[str] = None,
) -> None:
    text = build_filtered_summary_text(
        title,
        where_sql,
        params,
        capper_name=capper_name,
        period_label=period_label,
        sport_name=sport_name,
    )
    for chunk in split_discord_text(text):
        await ctx.send(chunk)


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

SPORT_CODES = {code for code, _keys in SPORT_KEYWORDS}


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def capper_aliases() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for capper in TRACKED_CHANNELS.values():
        aliases[normalize_key(capper.name)] = capper.name

    # Friendly shortcuts. These do not change how the capper is stored.
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
    return aliases


def split_args(raw: str) -> List[str]:
    try:
        return shlex.split(raw or "")
    except Exception:
        return (raw or "").split()


def resolve_capper_from_tokens(tokens: List[str]) -> Tuple[Optional[str], List[str]]:
    aliases = capper_aliases()
    best_name: Optional[str] = None
    best_i = 0

    # Try longest prefix first so "Matt Locks" can resolve to mattlocks.
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

    # Friendly league/sport words used often in Discord commands.
    aliases = {
        "BASEBALL": "MLB",
        "FOOTBALL": "NFL",
        "BASKETBALL": "NBA",
        "SOCCER": "SOCCER",
        "TENNIS": "TENNIS",
        "HOCKEY": "NHL",
    }
    key = first.replace("-", "")
    if key in aliases:
        return aliases[key], tokens[1:]

    return None, tokens


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


def parse_time_filter(raw: str) -> Tuple[Optional[str], Optional[datetime], Optional[datetime], Optional[str]]:
    """Return label/start/end/error. Blank means all-time."""
    text = normalize_space(raw).lower()
    text = text.replace("_", "-")

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

    # Custom inclusive ranges work anywhere a time filter is accepted.
    # Examples: 2026-07-01 2026-07-14 or 2026-07-01 to 2026-07-14.
    range_tokens = text.replace("→", " ").replace(" through ", " ").replace(" to ", " ").split()
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

    return None, None, None, f"Could not understand time filter `{raw}`. Try `today`, `yesterday`, `july`, `2026-07`, or `2026-07-09`."


def add_time_filter(where_parts: List[str], params: List[object], start_l: Optional[datetime], end_l: Optional[datetime]) -> None:
    if start_l is None or end_l is None:
        return
    where_sql, date_params = date_window_where(start_l, end_l)
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
) -> None:
    label, start_l, end_l, error = parse_time_filter(time_text)
    if error:
        await ctx.send(error)
        return
    add_time_filter(where_parts, params, start_l, end_l)
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
    )


async def post_leaderboard_query(
    ctx: commands.Context,
    time_text: str = "",
    sport_name: Optional[str] = None,
    league_name: Optional[str] = None,
    include_chart: bool = False,
) -> None:
    label, start_l, end_l, error = parse_time_filter(time_text)
    if error:
        await ctx.send(error)
        return

    where_parts: List[str] = []
    params: List[object] = []
    if sport_name:
        where_parts.append("UPPER(sport) = ?")
        params.append(sport_name.upper())
    if league_name:
        where_parts.append("LOWER(league) = ?")
        params.append(league_name.lower())
    add_time_filter(where_parts, params, start_l, end_l)
    where_sql, final_params = build_where(where_parts, params)

    title = "VIP CAPPER LEADERBOARD"
    if sport_name:
        title = f"VIP {sport_name.upper()} LEADERBOARD"
    elif league_name:
        title = f"VIP {league_name} LEADERBOARD"

    await post_leaderboard(
        ctx,
        title,
        clean_period_label(label),
        where_sql,
        final_params,
        sport_name=sport_name,
        league_name=league_name,
        include_chart=include_chart,
    )


def split_name_and_time_filter(query: str) -> Tuple[str, str]:
    """Split a free-form name from an optional trailing time filter."""
    tokens = split_args(query)
    if not tokens:
        return "", ""

    for suffix_len in range(min(3, len(tokens)), 0, -1):
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
    bet_date = parse_bet_date(content, created_utc)

    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (
            message_id, channel_id, capper, capper_user_id, content, created_utc, bet_date,
            sport, risk_units, odds_text, jump_url, league, event, player, team,
            opponent, bet_type, market, line, sportsbook, odds_format, multiplier
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            channel_id,
            capper.name,
            capper.user_id,
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
        ),
    )
    conn.commit()
    return True


def refresh_pending_from_message(message: discord.Message, capper: Capper) -> bool:
    """Re-parse a pending wager after the capper edits the Discord message."""
    if not pending_exists(message.id):
        return False

    content = message_to_text(message)
    risk = parse_risk_units(content)
    if risk is None:
        return False

    odds_text = parse_odds_text(content)
    fields = parse_analytics_fields(content, odds_text)
    created_utc = utc_iso(message.created_at)
    bet_date = parse_bet_date(content, created_utc)

    cur.execute(
        """
        UPDATE pending
        SET content = ?, bet_date = ?, sport = ?, risk_units = ?, odds_text = ?,
            jump_url = ?, league = ?, event = ?, player = ?, team = ?, opponent = ?,
            bet_type = ?, market = ?, line = ?, sportsbook = ?, odds_format = ?, multiplier = ?
        WHERE message_id = ?
        """,
        (
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
            int(message.id),
        ),
    )
    conn.commit()
    return True


def grade_pending(message_id: int, result: str, grade_reaction: str) -> bool:
    row = cur.execute(
        """
        SELECT
            channel_id, capper, capper_user_id, content, sport, risk_units, odds_text,
            created_utc, bet_date, jump_url, league, event, player, team, opponent, bet_type,
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

    # Safety check: older code could misread leading decimals like .5u as 5u.
    # Always trust the original message text when it contains a valid unit amount.
    parsed_risk = parse_risk_units(str(content or ""))
    if parsed_risk is not None:
        risk = float(parsed_risk)

    bet_date = bet_date_for_grade(str(content or ""), str(created_utc), str(bet_date or ""))
    net = compute_net_units(float(risk), str(odds_text), result)

    cur.execute(
        """
        INSERT OR REPLACE INTO bets
        (
            message_id, channel_id, capper, capper_user_id, sport, risk_units,
            net_units, result, odds_text, created_utc, graded_utc, bet_date, content,
            jump_url, league, event, player, team, opponent, bet_type, market,
            line, sportsbook, odds_format, multiplier, grade_reaction
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            str(grade_reaction),
        ),
    )
    cur.execute("DELETE FROM pending WHERE message_id = ?", (message_id,))
    conn.commit()
    return True


def regrade_bet(message_id: int, result: str, grade_reaction: str) -> bool:
    row = cur.execute(
        """
        SELECT risk_units, odds_text, content, created_utc, bet_date
        FROM bets
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return False

    risk, odds_text, content, created_utc, existing_bet_date = row
    bet_date = bet_date_for_grade(str(content or ""), str(created_utc or ""), str(existing_bet_date or ""))
    net = compute_net_units(float(risk), str(odds_text), result)

    cur.execute(
        """
        UPDATE bets
        SET result = ?, net_units = ?, graded_utc = ?, bet_date = ?, grade_reaction = ?
        WHERE message_id = ?
        """,
        (
            str(result),
            float(net),
            utc_iso(datetime.now(timezone.utc)),
            str(bet_date or ""),
            str(grade_reaction),
            int(message_id),
        ),
    )
    conn.commit()
    return True


def ungrade_bet(message_id: int) -> bool:
    row = cur.execute(
        """
        SELECT
            channel_id, capper, capper_user_id, content, sport, risk_units,
            odds_text, created_utc, bet_date, jump_url, league, event, player, team,
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
    ) = row

    cur.execute("DELETE FROM bets WHERE message_id = ?", (message_id,))
    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (
            message_id, channel_id, capper, capper_user_id, content, created_utc, bet_date,
            sport, risk_units, odds_text, jump_url, league, event, player, team,
            opponent, bet_type, market, line, sportsbook, odds_format, multiplier
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(channel_id),
            str(capper_name),
            int(capper_user_id),
            str(content or ""),
            str(created_utc),
            str(bet_date or ""),
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
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    """Refresh parsing when a capper edits an ungraded play."""
    if bot.user and after.author.id == bot.user.id:
        return

    capper = TRACKED_CHANNELS.get(after.channel.id)
    if not capper:
        return

    is_capper_user = after.author.id == capper.user_id
    is_webhook_post = after.webhook_id is not None
    if not (is_capper_user or is_webhook_post):
        return

    # Edits only update pending bets. Graded bets require bt!setodds or bt!setdate
    # so results never change silently after grading.
    refresh_pending_from_message(after, capper)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
    """Refresh an edited pending play even when the message was not in Discord's cache."""
    if not pending_exists(payload.message_id):
        return

    capper = TRACKED_CHANNELS.get(payload.channel_id)
    if not capper:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    is_capper_user = message.author.id == capper.user_id
    is_webhook_post = message.webhook_id is not None
    if not (is_capper_user or is_webhook_post):
        return

    refresh_pending_from_message(message, capper)


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


@bot.command(name="commands", aliases=["command", "cmds", "guide"])
async def commands_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "📌 **BetTracker Commands**\n"
        "Run normal lookups in `#vipbot-commands`.\n\n"
        "**VIP Leaderboards**\n"
        "`bt!today`\n"
        "`bt!yesterday`\n"
        "`bt!weekly`\n"
        "`bt!month 2026-07`\n"
        "`bt!year 2026`\n"
        "`bt!range 2026-07-01 2026-07-14`\n"
        "`bt!alltime`\n\n"
        "**Sport / League Leaderboards**\n"
        "`bt!leaderboard MLB july`\n"
        "`bt!leaderboard Tennis thisweek`\n"
        "`bt!leaderboard WNBA 2026`\n"
        "`bt!leaderboard Soccer 2026-07-01 2026-07-14`\n"
        "`bt!sport MLB july`\n"
        "`bt!league \"Premier League\" july`\n\n"
        "**Detailed Capper Bets**\n"
        "`bt!capper PropKitchen today`\n"
        "`bt!capper gr8 MLB july`\n"
        "`bt!capper pxs Tennis 2026-07-01 2026-07-14`\n"
        "`bt!bets PropKitchen 2026-07`\n\n"
        "**Player / Bet Type**\n"
        "`bt!player Paige Bueckers`\n"
        "`bt!bettype Rebounds`\n\n"
        "**Admin Quality / Corrections**\n"
        "`bt!data_issues today`\n"
        "`bt!data_issues 2026-07`\n"
        "Reply to the original bet: `bt!setdate 2026-07-12`\n"
        "Reply to the original bet: `bt!setodds +715` / `3x` / `2.50`\n\n"
        "**Admin Cleanup**\n"
        "`bt!fix_decimal_units`\n"
        "`bt!fix_bet_dates`\n"
        "`bt!recalc_multipliers`\n"
        "`bt!clear_pending_old`\n"
        "`bt!clear_pending_before today`"
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


async def clear_pending_rows(ctx: commands.Context, where_sql: str, params: Tuple[object, ...], label: str) -> None:
    rows = cur.execute(
        f"""
        SELECT message_id, channel_id, capper
        FROM pending
        WHERE {where_sql}
        """,
        params,
    ).fetchall()

    if not rows:
        await ctx.send(f"📝 No pending bets found for **{label}**.")
        return

    # Remove the 📝 reaction where possible. If Discord permissions fail, the DB cleanup still works.
    reactions_cleared = 0
    for message_id, channel_id, capper_name in rows:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            continue
        try:
            msg = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
            await msg.clear_reaction(PENDING_REACTION)
            reactions_cleared += 1
        except Exception:
            continue

    cur.execute(f"DELETE FROM pending WHERE {where_sql}", params)
    conn.commit()

    await ctx.send(
        f"✅ **Pending Cleared: {label}**\n"
        f"Removed pending rows: **{len(rows)}**\n"
        f"Removed 📝 reactions where possible: **{reactions_cleared}**\n"
        "Graded bet history was **not** deleted."
    )


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
        """
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
        """
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
        """
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
        """
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


@bot.command(name="backfill_content")
@commands.has_permissions(manage_guild=True)
async def backfill_content_cmd(ctx: commands.Context, limit: int = 200) -> None:
    limit = max(1, min(int(limit), 500))
    rows = cur.execute(
        """
        SELECT id, message_id, channel_id, risk_units, result, net_units
        FROM bets
        WHERE content = '' OR content IS NULL
        ORDER BY created_utc DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    updated = 0
    checked = 0
    corrected_units = 0
    for bet_id, message_id, channel_id, risk_units, result, old_net in rows:
        checked += 1
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            continue
        try:
            msg = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
        except Exception:
            continue

        content = message_to_text(msg)
        if not content:
            continue
        odds_text = parse_odds_text(content)
        fields = parse_analytics_fields(content, odds_text)
        bet_date = parse_bet_date(content, msg.created_at.isoformat())
        new_net = compute_net_units(float(risk_units), odds_text, str(result)) if odds_text else float(old_net)
        if abs(float(new_net) - float(old_net)) > 0.0001:
            corrected_units += 1
        cur.execute(
            """
            UPDATE bets
            SET content = ?, market = ?, player = ?, bet_type = ?, league = ?, sport = ?,
                line = ?, sportsbook = ?, odds_text = ?, odds_format = ?, multiplier = ?, net_units = ?,
                bet_date = COALESCE(NULLIF(?, ''), bet_date)
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


def referenced_message_id(ctx: commands.Context) -> Optional[int]:
    reference = ctx.message.reference
    if not reference or not reference.message_id:
        return None
    return int(reference.message_id)


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
        await ctx.send("Reply to the original bet and use `bt!setodds +715`, `bt!setodds 3x`, or `bt!setodds 2.50`.")
        return

    odds_text = parse_manual_odds_text(odds_arg)
    if not odds_text:
        await ctx.send("Odds not recognized. Use American `+715`/`-120`, multiplier `3x`, or decimal `2.50`.")
        return

    odds_format = infer_odds_format(odds_text)
    multiplier = parse_multiplier_value(odds_text)

    pending_row = cur.execute(
        "SELECT message_id FROM pending WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if pending_row:
        cur.execute(
            """
            UPDATE pending
            SET odds_text = ?, odds_format = ?, multiplier = ?
            WHERE message_id = ?
            """,
            (odds_text, odds_format, multiplier, message_id),
        )
        conn.commit()
        await ctx.send(f"✅ Pending bet odds updated to **{odds_text}**.")
        return

    bet_row = cur.execute(
        "SELECT risk_units, result, net_units FROM bets WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if not bet_row:
        await ctx.send("I could not find that replied-to message in pending or graded bets.")
        return

    risk_units, result, old_net = bet_row
    new_net = compute_net_units(float(risk_units), odds_text, str(result))
    cur.execute(
        """
        UPDATE bets
        SET odds_text = ?, odds_format = ?, multiplier = ?, net_units = ?
        WHERE message_id = ?
        """,
        (odds_text, odds_format, multiplier, float(new_net), message_id),
    )
    conn.commit()

    await ctx.send(
        f"✅ Graded bet odds updated to **{odds_text}**.\n"
        f"Net units corrected from **{float(old_net):+.2f}u** to **{new_net:+.2f}u**."
    )


@bot.command(name="leaderboard", aliases=["leaders", "lb"])
async def leaderboard_cmd(ctx: commands.Context, *, query: str = "") -> None:
    tokens = split_args(query)
    sport, remaining = resolve_sport_from_tokens(tokens)
    time_text = " ".join(remaining if sport else tokens)
    await post_leaderboard_query(ctx, time_text, sport_name=sport)


@bot.command(name="sport")
async def sport_cmd(ctx: commands.Context, *, query: str) -> None:
    tokens = split_args(query)
    sport, remaining = resolve_sport_from_tokens(tokens)
    if not sport:
        await ctx.send("Use format: `bt!sport WNBA`, `bt!sport WNBA today`, or `bt!sport MLB 2026-07`")
        return
    await post_leaderboard_query(ctx, " ".join(remaining), sport_name=sport)


@bot.command(name="league")
async def league_cmd(ctx: commands.Context, *, query: str) -> None:
    league_name, time_text = split_name_and_time_filter(query)
    if not league_name:
        await ctx.send('Use format: `bt!league "Premier League" july`')
        return
    await post_leaderboard_query(ctx, time_text, league_name=league_name)


@bot.command(name="capper", aliases=["bets"])
async def capper_cmd(ctx: commands.Context, *, query: str) -> None:
    tokens = split_args(query)
    capper, remaining = resolve_capper_from_tokens(tokens)
    if not capper:
        await ctx.send(
            "Use `bt!capper PropKitchen today`, `bt!capper gr8 MLB july`, "
            "`bt!capper pxs Tennis 2026-07-01 2026-07-14`, or `bt!bets PropKitchen 2026-07`."
        )
        return

    sport, remaining = resolve_sport_from_tokens(remaining)
    where_parts = ["LOWER(capper) = ?"]
    params: List[object] = [capper.lower()]
    title = f"Capper: {capper}"

    if sport:
        where_parts.append("UPPER(sport) = ?")
        params.append(sport)
        title += f" — {sport}"

    time_text = " ".join(remaining)
    await post_query_summary(
        ctx,
        title,
        where_parts,
        params,
        time_text,
        capper_name=capper,
        sport_name=sport,
    )


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


# =====================
# COMMAND ERRORS
# =====================

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the **Manage Server** permission to run that command.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing argument. Try `bt!month 2026-07`, `bt!leaderboard MLB july`, or `bt!capper PropKitchen today`.")
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
