import csv
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
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # type: ignore


"""
bot.py

Discord betting tracker with:
- Channel-based cappers
- Pending bet detection from natural play posts (e.g. "1U ... (-115)")
- Grading via capper reactions only:
    ✅ win, ❌ loss, 🟡 push
- Bot confirmation reaction:
    📌 means "logged"
- Regrade workflow:
    capper removes 📌 -> bet is unlogged and restored to pending
- SQLite storage (persistent disk recommended: /var/data/bets.db)
- Summaries + charts + CSV export
- Auto-post at 10:00 AM ET for previous day/week/month/year
"""

# =====================
# CONFIG
# =====================

TOKEN = os.getenv("TOKEN")
PREFIX = "bt!"
DB_PATH = "/var/data/bets.db"

REPORT_TZ = "America/New_York"  # ET

SUMMARY_CHANNEL_ID = 1473454134689796146  # #bettingtracker

TRACKED_CHANNELS: Dict[int, str] = {
    1257081246509563944: "PropKitchen",
    1258244563726893106: "hotshot",
    1281388388569579608: "clipset",
    1278486906169987226: "pxs",
    1356017581558857796: "mattlocks",
    1344526479366688808: "ballsout",
    1409640332295147570: "gr8",
    1430746272192659569: "mikelocks",
    1424256774692667422: "betsbybray",
}

# Channel ID -> Capper User ID (ONLY this user can grade in that channel)
CAPPER_OWNERS: Dict[int, int] = {
    1257081246509563944: 1230980936657535061,  # PropKitchen
    1356017581558857796: 1242294328253218878,  # Matt Locks
    1344526479366688808: 1345160333261668362,  # Balls Out
    1430746272192659569: 1430751846125010970,  # Mike Locks
    1258244563726893106: 475659527337934849,   # Hotshot
    1409640332295147570: 1109269360037601411,  # Gr8
    1278486906169987226: 933024893992329286,   # PXS
    1424256774692667422: 865284268745949194,   # Bray
    1281388388569579608: 684940092665757696,   # Clipset
}

# Emoji grading + confirmation
EMOJI_WIN = "✅"
EMOJI_LOSS = "❌"
EMOJI_PUSH = "🟡"
EMOJI_LOGGED = "📌"
EMOJI_PENDING = "📝"

REACT_ON_PENDING = True  # adds 📝 when a pending bet is detected

AUTOPOST_ENABLED = True
AUTOPOST_HOUR_ET = 10
AUTOPOST_MINUTE_ET = 0
WEEKLY_POST_WEEKDAY = 0
MONTHLY_POST_DAY = 1

# Optional: default sport per channel (if you want clean "SOCCER" etc)
# If empty, the bot tries to detect sport from message text; if none, uses "UNSPECIFIED".
DEFAULT_SPORT_BY_CHANNEL: Dict[int, str] = {
    # 1257081246509563944: "NBA",
}

AUTO_DETECT_SPORTS = [
    "NBA", "NCAAB", "NFL", "NCAAF", "MLB", "NHL",
    "TENNIS", "ESPORTS", "UFC",
    "SOCCER", "EPL", "MLS", "UCL", "LA LIGA", "SERIE A", "BUNDESLIGA",
]

IGNORE_TEST_BETS = True  # if message contains "test", don't store anything


# =====================
# DISCORD SETUP
# =====================

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    partials=(discord.PartialMessage, discord.PartialMessageable, discord.PartialEmoji),
)


# =====================
# DB SETUP
# =====================

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        channel_id INTEGER,
        capper TEXT NOT NULL,
        sport TEXT NOT NULL,
        risk_units REAL NOT NULL,
        net_units REAL NOT NULL,
        result TEXT NOT NULL,          -- win/loss/push
        odds_text TEXT NOT NULL,       -- "", "+250", "-120", "3x", "to_win:4"
        timestamp_utc TEXT NOT NULL
    )
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS pending_bets (
        message_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        capper TEXT NOT NULL,
        sport TEXT NOT NULL,
        risk_units REAL NOT NULL,
        odds_text TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL
    )
    """
)

cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_time ON bets(timestamp_utc)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_capper_time ON bets(capper, timestamp_utc)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport_time ON bets(sport, timestamp_utc)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_message ON bets(message_id)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_channel ON pending_bets(channel_id)")
conn.commit()


def _ensure_schema() -> None:
    cols = cursor.execute("PRAGMA table_info(bets)").fetchall()
    colnames = {c[1].lower() for c in cols}

    # Backward-compat rename (if needed)
    if "timestamp" in colnames and "timestamp_utc" not in colnames:
        cursor.execute("ALTER TABLE bets RENAME COLUMN timestamp TO timestamp_utc")

    # Add missing columns if an older table exists
    for col, ddl in [
        ("message_id", "ALTER TABLE bets ADD COLUMN message_id INTEGER"),
        ("channel_id", "ALTER TABLE bets ADD COLUMN channel_id INTEGER"),
    ]:
        if col not in colnames:
            cursor.execute(ddl)

    conn.commit()


_ensure_schema()


# =====================
# TIME HELPERS
# =====================

def tzinfo():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(REPORT_TZ)
    except Exception:
        return timezone.utc


def now_local() -> datetime:
    return datetime.now(tzinfo())


def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tzinfo())
    return dt_local.astimezone(timezone.utc)


def utc_iso(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone.utc).isoformat()


def parse_yyyy_mm_dd(s: str) -> Optional[date]:
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def parse_yyyy_mm(s: str) -> Optional[Tuple[int, int]]:
    try:
        y, m = s.split("-")
        return int(y), int(m)
    except Exception:
        return None


def parse_year(s: str) -> Optional[int]:
    try:
        y = int(s)
        return y if 1900 <= y <= 3000 else None
    except Exception:
        return None


def period_bounds_local(period: str, ref: Optional[date] = None) -> Tuple[datetime, datetime]:
    period = period.lower()
    ref = ref or now_local().date()

    if period == "daily":
        start = datetime(ref.year, ref.month, ref.day, 0, 0, 0, tzinfo=tzinfo())
        return start, start + timedelta(days=1)

    if period == "weekly":
        start_day = ref - timedelta(days=ref.weekday())  # Monday
        start = datetime(start_day.year, start_day.month, start_day.day, 0, 0, 0, tzinfo=tzinfo())
        return start, start + timedelta(days=7)

    if period == "monthly":
        start = datetime(ref.year, ref.month, 1, 0, 0, 0, tzinfo=tzinfo())
        if ref.month == 12:
            end = datetime(ref.year + 1, 1, 1, 0, 0, 0, tzinfo=tzinfo())
        else:
            end = datetime(ref.year, ref.month + 1, 1, 0, 0, 0, tzinfo=tzinfo())
        return start, end

    if period == "yearly":
        start = datetime(ref.year, 1, 1, 0, 0, 0, tzinfo=tzinfo())
        end = datetime(ref.year + 1, 1, 1, 0, 0, 0, tzinfo=tzinfo())
        return start, end

    start = datetime(ref.year, ref.month, ref.day, 0, 0, 0, tzinfo=tzinfo())
    return start, start + timedelta(days=1)


# =====================
# PARSING
# =====================

@dataclass(frozen=True)
class ParsedBet:
    sport: str
    risk_units: float
    net_units: float
    result: str
    odds_text: str


# Supports "1u", "0.5u", ".5u"
_RE_UNITS = re.compile(r"^\s*((?:\d+(?:\.\d+)?|\.\d+))\s*u\s*$", re.IGNORECASE)
_RE_TO_WIN = re.compile(
    r"^\s*((?:\d+(?:\.\d+)?|\.\d+))\s*u\s*to\s*win\s*((?:\d+(?:\.\d+)?|\.\d+))\s*u\s*$",
    re.IGNORECASE,
)

_RE_AMERICAN = re.compile(r"^\s*(?:odds\s*:\s*)?([+-]\d+)\s*$", re.IGNORECASE)
_RE_MULT = re.compile(r"^\s*(?:odds\s*:\s*)?(\d+(?:\.\d+)?)\s*x\s*$", re.IGNORECASE)

# Natural post patterns:
# - "1U ..." or ".5U ..." anywhere
_RE_ANY_UNITS = re.compile(r"(?i)(^|\s)(\d+(?:\.\d+)?|\.\d+)\s*u\b")
# - "(-115)" or "+120" or "-110" or "3.15x"
_RE_ANY_AMERICAN_PARENS = re.compile(r"\(\s*([+-]\d+)\s*\)")
_RE_ANY_AMERICAN = re.compile(r"(^|\s)([+-]\d+)(\s|$)")
_RE_ANY_MULT = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*x\b")


def _profit_from_american(risk: float, american: int) -> float:
    if american > 0:
        return risk * (american / 100.0)
    return risk * (100.0 / abs(american))


def _profit_from_multiplier(risk: float, mult: float) -> float:
    return risk * (mult - 1.0)


def parse_strict_bet_message(content: str) -> Optional[ParsedBet]:
    low = content.lower()
    if "bet:" not in low or "result:" not in low:
        return None

    parts = [p.strip() for p in content.split("|")]
    if len(parts) < 4:
        return None

    stake_part = parts[1]
    sport = parts[2].strip()
    if not sport:
        return None

    result_part = parts[-1]
    if ":" not in result_part:
        return None
    result = result_part.split(":", 1)[1].strip().lower()
    if result not in {"win", "loss", "push"}:
        return None

    m_to_win = _RE_TO_WIN.match(stake_part)
    if m_to_win:
        risk = float(m_to_win.group(1))
        to_win = float(m_to_win.group(2))
        if risk <= 0 or to_win < 0:
            return None
        net = to_win if result == "win" else (-risk if result == "loss" else 0.0)
        return ParsedBet(sport=sport, risk_units=risk, net_units=net, result=result, odds_text=f"to_win:{to_win}")

    m_units = _RE_UNITS.match(stake_part)
    if not m_units:
        return None
    risk = float(m_units.group(1))
    if risk <= 0:
        return None

    odds_text = ""
    win_profit = risk
    loss_profit = -risk

    odds_part = parts[3] if len(parts) >= 5 else ""
    if odds_part:
        m_am = _RE_AMERICAN.match(odds_part)
        if m_am:
            american = int(m_am.group(1))
            odds_text = str(american)
            win_profit = _profit_from_american(risk, american)
        else:
            m_mult = _RE_MULT.match(odds_part)
            if not m_mult:
                return None
            mult = float(m_mult.group(1))
            if mult <= 1.0:
                return None
            odds_text = f"{mult}x"
            win_profit = _profit_from_multiplier(risk, mult)

    net = win_profit if result == "win" else (loss_profit if result == "loss" else 0.0)
    return ParsedBet(sport=sport, risk_units=risk, net_units=net, result=result, odds_text=odds_text)


def detect_sport(channel_id: int, content: str) -> str:
    if channel_id in DEFAULT_SPORT_BY_CHANNEL:
        return DEFAULT_SPORT_BY_CHANNEL[channel_id]
    upper = content.upper()
    for s in AUTO_DETECT_SPORTS:
        if s.upper() in upper:
            return s
    return "UNSPECIFIED"


def detect_units(content: str) -> Optional[float]:
    m = _RE_ANY_UNITS.search(content)
    if not m:
        return None
    try:
        return float(m.group(2))
    except Exception:
        return None


def detect_odds_text(content: str) -> str:
    m = _RE_ANY_AMERICAN_PARENS.search(content)
    if m:
        return m.group(1).strip()

    m = _RE_ANY_MULT.search(content)
    if m:
        return f"{m.group(1)}x"

    m = _RE_ANY_AMERICAN.search(content)
    if m:
        return m.group(2).strip()

    return ""


def compute_net_units(risk: float, odds_text: str, result: str) -> float:
    result = result.lower()
    if result == "push":
        return 0.0

    win_profit = risk
    if odds_text:
        am = _RE_AMERICAN.match(odds_text)
        if am:
            win_profit = _profit_from_american(risk, int(am.group(1)))
        else:
            mult = _RE_MULT.match(odds_text)
            if mult:
                win_profit = _profit_from_multiplier(risk, float(mult.group(1)))

    if result == "win":
        return win_profit
    return -risk  # loss


# =====================
# STORAGE HELPERS
# =====================

def upsert_pending(message_id: int, channel_id: int, capper: str, sport: str, risk: float, odds_text: str, ts_utc: str) -> None:
    cursor.execute(
        """
        INSERT INTO pending_bets (message_id, channel_id, capper, sport, risk_units, odds_text, timestamp_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            channel_id=excluded.channel_id,
            capper=excluded.capper,
            sport=excluded.sport,
            risk_units=excluded.risk_units,
            odds_text=excluded.odds_text,
            timestamp_utc=excluded.timestamp_utc
        """,
        (message_id, channel_id, capper, sport, risk, odds_text, ts_utc),
    )
    conn.commit()


def fetch_pending(message_id: int) -> Optional[Tuple[int, int, str, str, float, str, str]]:
    row = cursor.execute(
        """
        SELECT message_id, channel_id, capper, sport, risk_units, odds_text, timestamp_utc
        FROM pending_bets
        WHERE message_id=?
        """,
        (message_id,),
    ).fetchone()
    if not row:
        return None
    return int(row[0]), int(row[1]), str(row[2]), str(row[3]), float(row[4]), str(row[5]), str(row[6])


def delete_pending(message_id: int) -> None:
    cursor.execute("DELETE FROM pending_bets WHERE message_id=?", (message_id,))
    conn.commit()


def insert_graded_bet(
    message_id: int,
    channel_id: int,
    capper: str,
    sport: str,
    risk: float,
    odds_text: str,
    result: str,
    net: float,
    ts_utc: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO bets (message_id, channel_id, capper, sport, risk_units, net_units, result, odds_text, timestamp_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, channel_id, capper, sport, risk, net, result, odds_text, ts_utc),
    )
    conn.commit()


def delete_graded_bet(message_id: int) -> Optional[Tuple[int, int, str, str, float, str, str]]:
    row = cursor.execute(
        """
        SELECT message_id, channel_id, capper, sport, risk_units, odds_text, timestamp_utc
        FROM bets
        WHERE message_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (message_id,),
    ).fetchone()
    if not row:
        return None

    cursor.execute("DELETE FROM bets WHERE message_id=?", (message_id,))
    conn.commit()
    return int(row[0]), int(row[1]), str(row[2]), str(row[3]), float(row[4]), str(row[5]), str(row[6])


# =====================
# REPORTING (same as before)
# =====================

def fetch_capper_rows(
    start_utc: datetime,
    end_utc: datetime,
    sport: Optional[str] = None,
    capper: Optional[str] = None,
) -> List[Tuple[str, float, int, int, int]]:
    where = ["timestamp_utc >= ?", "timestamp_utc < ?"]
    params: List[object] = [utc_iso(start_utc), utc_iso(end_utc)]

    if sport:
        where.append("LOWER(sport) = LOWER(?)")
        params.append(sport)

    if capper:
        where.append("LOWER(capper) = LOWER(?)")
        params.append(capper)

    rows = cursor.execute(
        f"""
        SELECT
            capper,
            COALESCE(SUM(net_units), 0) AS net_units_sum,
            SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE {" AND ".join(where)}
        GROUP BY capper
        ORDER BY net_units_sum DESC
        """,
        tuple(params),
    ).fetchall()

    return [(str(c), float(u), int(w), int(l), int(p)) for c, u, w, l, p in rows]


def fetch_vip_totals(
    start_utc: datetime,
    end_utc: datetime,
    sport: Optional[str] = None,
    capper: Optional[str] = None,
) -> Tuple[float, int, int, int]:
    where = ["timestamp_utc >= ?", "timestamp_utc < ?"]
    params: List[object] = [utc_iso(start_utc), utc_iso(end_utc)]

    if sport:
        where.append("LOWER(sport) = LOWER(?)")
        params.append(sport)

    if capper:
        where.append("LOWER(capper) = LOWER(?)")
        params.append(capper)

    row = cursor.execute(
        f"""
        SELECT
            COALESCE(SUM(net_units), 0) AS net_units_sum,
            SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN result = 'push' THEN 1 ELSE 0 END) AS pushes
        FROM bets
        WHERE {" AND ".join(where)}
        """,
        tuple(params),
    ).fetchone()

    if not row:
        return 0.0, 0, 0, 0
    return float(row[0] or 0.0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)


def make_vip_line(net_units: float, wins: int, losses: int, pushes: int) -> str:
    graded = wins + losses
    win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")
    return f"**VIP TOTAL**: {record} ({win_pct:.1f}%) | **{net_units:+.2f}u**"


def make_rows_text(rows: List[Tuple[str, float, int, int, int]]) -> str:
    if not rows:
        return "No bets found."
    lines = []
    for capper_name, net_units, wins, losses, pushes in rows:
        graded = wins + losses
        win_pct = (wins / graded * 100.0) if graded > 0 else 0.0
        record = f"{wins}-{losses}" + (f"-{pushes}" if pushes > 0 else "")
        lines.append(f"**{capper_name}**: {record} ({win_pct:.1f}%) | **{net_units:+.2f}u**")
    return "\n".join(lines)


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


def generate_vip_combined_chart(title: str, vip_net: float, rows: List[Tuple[str, float, int, int, int]]) -> io.BytesIO:
    labels = ["VIP"] + [r[0] for r in rows]
    values = [vip_net] + [r[1] for r in rows]

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


def export_bets_csv(
    start_utc: datetime,
    end_utc: datetime,
    sport: Optional[str] = None,
    capper: Optional[str] = None,
) -> io.BytesIO:
    where = ["timestamp_utc >= ?", "timestamp_utc < ?"]
    params: List[object] = [utc_iso(start_utc), utc_iso(end_utc)]

    if sport:
        where.append("LOWER(sport) = LOWER(?)")
        params.append(sport)

    if capper:
        where.append("LOWER(capper) = LOWER(?)")
        params.append(capper)

    rows = cursor.execute(
        f"""
        SELECT capper, sport, risk_units, net_units, result, odds_text, timestamp_utc
        FROM bets
        WHERE {" AND ".join(where)}
        ORDER BY timestamp_utc ASC
        """,
        tuple(params),
    ).fetchall()

    buf = io.BytesIO()
    text = io.TextIOWrapper(buf, encoding="utf-8", newline="")
    w = csv.writer(text)
    w.writerow(["capper", "sport", "risk_units", "net_units", "result", "odds_text", "timestamp_utc"])
    for r in rows:
        w.writerow(r)
    text.flush()
    buf.seek(0)
    return buf


async def post_period_summary(
    channel: discord.abc.Messageable,
    title: str,
    start_local: datetime,
    end_local: datetime,
    sport: Optional[str] = None,
    capper: Optional[str] = None,
) -> None:
    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)

    rows = fetch_capper_rows(start_utc, end_utc, sport=sport, capper=capper)
    vip_net, vip_w, vip_l, vip_p = fetch_vip_totals(start_utc, end_utc, sport=sport, capper=capper)

    header = f"📊 **{title}**"
    if sport:
        header += f" — **{sport}**"
    if capper:
        header += f" — **{capper}**"

    await channel.send(f"{header}\n{make_vip_line(vip_net, vip_w, vip_l, vip_p)}\n\n{make_rows_text(rows)}")

    if rows:
        img = generate_units_chart(f"{title} Net Units", rows)
        await channel.send(file=discord.File(img, filename="units.png"))

    vip_img = generate_vip_combined_chart(f"{title} — VIP + Cappers", vip_net, rows)
    await channel.send(file=discord.File(vip_img, filename="vip_combined.png"))


# =====================
# EVENTS
# =====================

_last_post_key = {"daily": None, "weekly": None, "monthly": None, "yearly": None}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Prefix: {PREFIX}")
    if AUTOPOST_ENABLED and not autopost_loop.is_running():
        autopost_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content or ""
    if IGNORE_TEST_BETS and "test" in content.lower():
        await bot.process_commands(message)
        return

    if message.channel.id not in TRACKED_CHANNELS:
        await bot.process_commands(message)
        return

    capper = TRACKED_CHANNELS[message.channel.id]
    ts_utc = utc_iso(message.created_at.replace(tzinfo=timezone.utc))

    # 1) Keep strict format logging (optional)
    strict = parse_strict_bet_message(content)
    if strict:
        cursor.execute(
            """
            INSERT INTO bets (message_id, channel_id, capper, sport, risk_units, net_units, result, odds_text, timestamp_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.channel.id,
                capper,
                strict.sport,
                strict.risk_units,
                strict.net_units,
                strict.result,
                strict.odds_text,
                ts_utc,
            ),
        )
        conn.commit()
        try:
            await message.add_reaction(EMOJI_LOGGED)
        except Exception:
            pass
        await bot.process_commands(message)
        return

    # 2) Detect natural play -> pending
    risk = detect_units(content)
    if risk is None or risk <= 0:
        await bot.process_commands(message)
        return

    sport = detect_sport(message.channel.id, content)
    odds_text = detect_odds_text(content)

    upsert_pending(message.id, message.channel.id, capper, sport, risk, odds_text, ts_utc)

    if REACT_ON_PENDING:
        try:
            await message.add_reaction(EMOJI_PENDING)
        except Exception:
            pass

    await bot.process_commands(message)


async def _grade_message(message: discord.Message, result: str) -> None:
    pending = fetch_pending(message.id)
    if not pending:
        # Try backfill for old messages (if capper reacts before we saw the message)
        risk = detect_units(message.content or "")
        if risk is None or risk <= 0:
            return
        capper = TRACKED_CHANNELS.get(message.channel.id)
        if not capper:
            return
        sport = detect_sport(message.channel.id, message.content or "")
        odds_text = detect_odds_text(message.content or "")
        ts_utc = utc_iso(message.created_at.replace(tzinfo=timezone.utc))
        upsert_pending(message.id, message.channel.id, capper, sport, risk, odds_text, ts_utc)
        pending = fetch_pending(message.id)

    if not pending:
        return

    _, channel_id, capper, sport, risk, odds_text, ts_utc = pending
    net = compute_net_units(risk, odds_text, result)

    insert_graded_bet(
        message_id=message.id,
        channel_id=channel_id,
        capper=capper,
        sport=sport,
        risk=risk,
        odds_text=odds_text,
        result=result,
        net=net,
        ts_utc=ts_utc,
    )
    delete_pending(message.id)

    try:
        await message.add_reaction(EMOJI_LOGGED)
    except Exception:
        pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == (bot.user.id if bot.user else None):
        return

    channel_id = payload.channel_id
    if channel_id not in TRACKED_CHANNELS:
        return

    owner_id = CAPPER_OWNERS.get(channel_id)
    if owner_id is None or payload.user_id != owner_id:
        return

    emoji = str(payload.emoji)
    if emoji not in {EMOJI_WIN, EMOJI_LOSS, EMOJI_PUSH}:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    try:
        msg = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    # If already logged, force unlog first (capper should remove 📌 to regrade)
    if emoji in {EMOJI_WIN, EMOJI_LOSS, EMOJI_PUSH}:
        await _grade_message(
            msg,
            "win" if emoji == EMOJI_WIN else ("loss" if emoji == EMOJI_LOSS else "push"),
        )


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    print("REACTION_ADD", payload.channel_id, payload.message_id, payload.user_id, str(payload.emoji))
owner_id = CAPPER_OWNERS.get(channel_id)
print("OWNER_CHECK", channel_id, owner_id)

    # Regrade trigger: capper removes 📌
    channel_id = payload.channel_id
    if channel_id not in TRACKED_CHANNELS:
        return

    owner_id = CAPPER_OWNERS.get(channel_id)
    if owner_id is None or payload.user_id != owner_id:
        return

    emoji = str(payload.emoji)
    if emoji != EMOJI_LOGGED:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    # Unlog bet -> restore pending
    restored = delete_graded_bet(payload.message_id)
    if not restored:
        return

    message_id, ch_id, capper, sport, risk, odds_text, ts_utc = restored
    upsert_pending(message_id, ch_id, capper, sport, risk, odds_text, ts_utc)

    try:
        msg = await channel.fetch_message(message_id)
        if REACT_ON_PENDING:
            await msg.add_reaction(EMOJI_PENDING)
    except Exception:
        pass


# =====================
# COMMANDS
# =====================

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong")


@bot.command()
async def daily(ctx: commands.Context, yyyy_mm_dd: str = ""):
    if yyyy_mm_dd:
        d = parse_yyyy_mm_dd(yyyy_mm_dd)
        if not d:
            await ctx.send("Use: `bt!daily YYYY-MM-DD`")
            return
        start_l, end_l = period_bounds_local("daily", d)
        title = f"Daily ({d.isoformat()})"
    else:
        start_l, end_l = period_bounds_local("daily")
        title = "Daily"
    await post_period_summary(ctx, title, start_l, end_l)


@bot.command()
async def weekly(ctx: commands.Context):
    start_l, end_l = period_bounds_local("weekly")
    await post_period_summary(ctx, "Weekly", start_l, end_l)


@bot.command()
async def monthly(ctx: commands.Context):
    start_l, end_l = period_bounds_local("monthly")
    await post_period_summary(ctx, "Monthly", start_l, end_l)


@bot.command()
async def yearly(ctx: commands.Context):
    start_l, end_l = period_bounds_local("yearly")
    await post_period_summary(ctx, "Yearly", start_l, end_l)


@bot.command()
async def alltime(ctx: commands.Context):
    start_l = datetime(2000, 1, 1, 0, 0, 0, tzinfo=tzinfo())
    end_l = now_local() + timedelta(days=1)
    await post_period_summary(ctx, "All-Time", start_l, end_l)


@bot.command()
async def export(ctx: commands.Context, scope: str, value: str, sport_name: str = "", capper_name: str = ""):
    scope = scope.lower().strip()
    sport_filter = sport_name.strip() or None
    capper_filter = capper_name.strip() or None

    if scope == "day":
        d = parse_yyyy_mm_dd(value)
        if not d:
            await ctx.send("Use: `bt!export day YYYY-MM-DD`")
            return
        start_l, end_l = period_bounds_local("daily", d)
        label = f"day_{d.isoformat()}"
    elif scope == "month":
        ym = parse_yyyy_mm(value)
        if not ym:
            await ctx.send("Use: `bt!export month YYYY-MM`")
            return
        y, m = ym
        start_l = datetime(y, m, 1, 0, 0, 0, tzinfo=tzinfo())
        end_l = datetime(y + 1, 1, 1, 0, 0, 0, tzinfo=tzinfo()) if m == 12 else datetime(y, m + 1, 1, 0, 0, 0, tzinfo=tzinfo())
        label = f"month_{value}"
    elif scope == "year":
        y = parse_year(value)
        if y is None:
            await ctx.send("Use: `bt!export year YYYY`")
            return
        start_l = datetime(y, 1, 1, 0, 0, 0, tzinfo=tzinfo())
        end_l = datetime(y + 1, 1, 1, 0, 0, 0, tzinfo=tzinfo())
        label = f"year_{y}"
    else:
        await ctx.send("Use: `bt!export day|month|year <value> [SPORT] [CAPPER]`")
        return

    start_utc = to_utc(start_l)
    end_utc = to_utc(end_l)

    buf = export_bets_csv(start_utc, end_utc, sport=sport_filter, capper=capper_filter)
    await ctx.send(file=discord.File(buf, filename=f"bets_export_{label}.csv"))


# =====================
# AUTOPOST
# =====================

@tasks.loop(minutes=1)
async def autopost_loop():
    if not AUTOPOST_ENABLED:
        return

    nl = now_local()
    if nl.hour != AUTOPOST_HOUR_ET or nl.minute != AUTOPOST_MINUTE_ET:
        return

    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if channel is None:
        return

    def banner(title: str) -> str:
        return f"🔥 VIP RECAP — {title} 🔥"

    # DAILY: yesterday
    yesterday = nl.date() - timedelta(days=1)
    day_key = yesterday.isoformat()
    if _last_post_key["daily"] != day_key:
        _last_post_key["daily"] = day_key
        start_l, end_l = period_bounds_local("daily", yesterday)
        await post_period_summary(channel, banner(f"DAILY ({yesterday.isoformat()})"), start_l, end_l)

    # WEEKLY: previous week Mon–Sun (run Monday)
    if nl.weekday() == WEEKLY_POST_WEEKDAY:
        prev_week_ref = nl.date() - timedelta(days=7)
        start_l, end_l = period_bounds_local("weekly", prev_week_ref)
        week_start = start_l.date()
        week_end = (end_l - timedelta(days=1)).date()
        week_key = f"{week_start.isoformat()}_{week_end.isoformat()}"
        if _last_post_key["weekly"] != week_key:
            _last_post_key["weekly"] = week_key
            await post_period_summary(channel, banner(f"WEEKLY ({week_start} → {week_end})"), start_l, end_l)

    # MONTHLY: previous month (run 1st)
    if nl.day == MONTHLY_POST_DAY:
        prev_month_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("monthly", prev_month_ref)
        month_label = f"{start_l.year}-{start_l.month:02d}"
        if _last_post_key["monthly"] != month_label:
            _last_post_key["monthly"] = month_label
            await post_period_summary(channel, banner(f"MONTHLY ({month_label})"), start_l, end_l)

    # YEARLY: previous year (run Jan 1)
    if nl.month == 1 and nl.day == 1:
        prev_year_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("yearly", prev_year_ref)
        year_label = f"{start_l.year}"
        if _last_post_key["yearly"] != year_label:
            _last_post_key["yearly"] = year_label
            await post_period_summary(channel, banner(f"YEARLY ({year_label})"), start_l, end_l)


bot.run(TOKEN)

