import csv
import io
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

Discord betting tracker (channel-based cappers) with:
- Odds support: flat units, multiplier (3x), American odds (+250/-120), "1u to win 4u"
- Result support: Win/Loss/Push
- Storage: SQLite (bets.db)
- Reporting: Daily/Weekly/Monthly/Yearly/All-time (calendar-based in ET)
- Filters: by sport, by capper
- VIP combined totals + charts
- Export: CSV
- Auto-post at 10:00 AM ET for previous day/week/month/year
"""

# =====================
# CONFIG (EDIT TOKEN ONLY)
# =====================

import os
TOKEN = os.getenv("TOKEN")

# Private capper channels (channel_id -> capper_name)
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

# Where auto-post summaries go
SUMMARY_CHANNEL_ID = 1473454134689796146  # #bettingtracker

PREFIX = "bt!"
DB_PATH = "bets.db"

# Calendar timezone for reporting/autopost
REPORT_TZ = "America/New_York"  # ET

# Auto-post schedule (ET)
AUTOPOST_ENABLED = True
AUTOPOST_HOUR_ET = 10
AUTOPOST_MINUTE_ET = 0
WEEKLY_POST_WEEKDAY = 0  # Monday (0=Mon ... 6=Sun)
MONTHLY_POST_DAY = 1     # 1st day of month


# =====================
# DISCORD SETUP
# =====================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)


# =====================
# DATABASE
# =====================

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_time ON bets(timestamp_utc)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_capper_time ON bets(capper, timestamp_utc)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport_time ON bets(sport, timestamp_utc)")
conn.commit()


def _ensure_schema() -> None:
    cols = cursor.execute("PRAGMA table_info(bets)").fetchall()
    colnames = {c[1].lower() for c in cols}
    if "timestamp" in colnames and "timestamp_utc" not in colnames:
        cursor.execute("ALTER TABLE bets RENAME COLUMN timestamp TO timestamp_utc")
    if "timestamp_utc" not in colnames:
        cursor.execute("ALTER TABLE bets ADD COLUMN timestamp_utc TEXT")
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
    """
    Calendar periods in ET:
    - daily: ref day 00:00 -> next day 00:00
    - weekly: Mon 00:00 -> next Mon 00:00 (week containing ref)
    - monthly: 1st 00:00 -> next month 1st 00:00
    - yearly: Jan 1 00:00 -> next Jan 1 00:00
    """
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
# PARSING + MATH
# =====================

@dataclass(frozen=True)
class ParsedBet:
    sport: str
    risk_units: float
    net_units: float
    result: str
    odds_text: str


_RE_UNITS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*u\s*$", re.IGNORECASE)
_RE_TO_WIN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*u\s*to\s*win\s*(\d+(?:\.\d+)?)\s*u\s*$", re.IGNORECASE)

# Odds can be "Odds: +250" OR "+250", same for "3x"
_RE_AMERICAN = re.compile(r"^\s*(?:odds\s*:\s*)?([+-]\d+)\s*$", re.IGNORECASE)
_RE_MULT = re.compile(r"^\s*(?:odds\s*:\s*)?(\d+(?:\.\d+)?)\s*x\s*$", re.IGNORECASE)


def _profit_from_american(risk: float, american: int) -> float:
    if american > 0:
        return risk * (american / 100.0)
    return risk * (100.0 / abs(american))


def _profit_from_multiplier(risk: float, mult: float) -> float:
    # "3x" means total return 3*risk => profit = (3-1)*risk
    return risk * (mult - 1.0)


def parse_bet_message(content: str) -> Optional[ParsedBet]:
    """
    Required structure:
      Bet: ... | <stake> | <sport> | <optional odds> | Result: Win/Loss/Push

    Examples:
      Bet: Lakers -3.5 | 1u | NBA | Result: Win
      Bet: Parlay | 1u | NBA | 3x | Result: Win
      Bet: ML | 1u | TENNIS | -140 | Result: Win
      Bet: Dog | 1u | ESPORTS | +250 | Result: Win
      Bet: Parlay | 1u to win 4u | NCAAB | Result: Win
    """
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

    # "1u to win 4u"
    m_to_win = _RE_TO_WIN.match(stake_part)
    if m_to_win:
        risk = float(m_to_win.group(1))
        to_win = float(m_to_win.group(2))
        if risk <= 0 or to_win < 0:
            return None
        net = to_win if result == "win" else (-risk if result == "loss" else 0.0)
        return ParsedBet(sport=sport, risk_units=risk, net_units=net, result=result, odds_text=f"to_win:{to_win}")

    # "1u"
    m_units = _RE_UNITS.match(stake_part)
    if not m_units:
        return None
    risk = float(m_units.group(1))
    if risk <= 0:
        return None

    # Default: flat (+risk on win, -risk on loss)
    odds_text = ""
    win_profit = risk
    loss_profit = -risk

    # Optional odds slot: only if message has 5+ parts
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


# =====================
# QUERIES / REPORTING
# =====================

def list_cappers() -> List[str]:
    return sorted(set(TRACKED_CHANNELS.values()), key=lambda s: s.lower())


def capper_match(name: str) -> Optional[str]:
    return next((c for c in list_cappers() if c.lower() == name.lower()), None)


def list_sports() -> List[str]:
    rows = cursor.execute(
        """
        SELECT DISTINCT sport
        FROM bets
        WHERE sport IS NOT NULL AND TRIM(sport) <> ''
        ORDER BY LOWER(sport) ASC
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


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


# =====================
# EXPORT
# =====================

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


# =====================
# POST HELPERS
# =====================

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

    # Ignore TEST bets (do not store in DB)
    if "test" in message.content.lower():
        await message.channel.send("🧪 Test bet detected — not counted.")
        await bot.process_commands(message)
        return

    # Only log bets from tracked capper channels
    if message.channel.id not in TRACKED_CHANNELS:
        await bot.process_commands(message)
        return

    parsed = parse_bet_message(message.content)
    if not parsed:
        await bot.process_commands(message)
        return

    capper = TRACKED_CHANNELS[message.channel.id]
    cursor.execute(
        """
        INSERT INTO bets (capper, sport, risk_units, net_units, result, odds_text, timestamp_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            capper,
            parsed.sport,
            parsed.risk_units,
            parsed.net_units,
            parsed.result,
            parsed.odds_text,
            utc_iso(datetime.now(timezone.utc)),
        ),
    )
    conn.commit()

    await message.channel.send("✅ Bet logged")
    await bot.process_commands(message)


# =====================
# COMMANDS
# =====================

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong")


@bot.command()
async def sports(ctx: commands.Context):
    s = list_sports()
    if not s:
        await ctx.send("No sports recorded yet.")
        return
    await ctx.send("**Sports recorded:** " + ", ".join(f"`{x}`" for x in s))


@bot.command()
async def daily(ctx: commands.Context, yyyy_mm_dd: str = ""):
    if yyyy_mm_dd:
        d = parse_yyyy_mm_dd(yyyy_mm_dd)
        if not d:
            await ctx.send("Use: `bt!daily YYYY-MM-DD` (example: `bt!daily 2026-02-17`)")
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
async def month(ctx: commands.Context, yyyy_mm: str):
    parsed = parse_yyyy_mm(yyyy_mm)
    if not parsed:
        await ctx.send("Use: `bt!month YYYY-MM` (example: `bt!month 2026-02`)")
        return
    y, m = parsed
    start_l = datetime(y, m, 1, 0, 0, 0, tzinfo=tzinfo())
    end_l = datetime(y + 1, 1, 1, 0, 0, 0, tzinfo=tzinfo()) if m == 12 else datetime(y, m + 1, 1, 0, 0, 0, tzinfo=tzinfo())
    await post_period_summary(ctx, f"Month ({yyyy_mm})", start_l, end_l)


@bot.command()
async def year(ctx: commands.Context, yyyy: str):
    y = parse_year(yyyy)
    if y is None:
        await ctx.send("Use: `bt!year YYYY` (example: `bt!year 2026`)")
        return
    start_l = datetime(y, 1, 1, 0, 0, 0, tzinfo=tzinfo())
    end_l = datetime(y + 1, 1, 1, 0, 0, 0, tzinfo=tzinfo())
    await post_period_summary(ctx, f"Year ({y})", start_l, end_l)


@bot.command()
async def sport(ctx: commands.Context, sport_name: str, period: str = "daily"):
    period = period.lower()
    if period not in {"daily", "weekly", "monthly", "yearly"}:
        await ctx.send("Use: `bt!sport NBA daily|weekly|monthly|yearly`")
        return
    start_l, end_l = period_bounds_local(period)
    await post_period_summary(ctx, period.capitalize(), start_l, end_l, sport=sport_name)


@bot.command()
async def capper(ctx: commands.Context, capper_name: str, period: str = "daily", sport_name: str = ""):
    capper_ok = capper_match(capper_name)
    if not capper_ok:
        await ctx.send("Unknown capper. Valid: " + ", ".join(f"`{c}`" for c in list_cappers()))
        return

    period = period.lower()
    if period not in {"daily", "weekly", "monthly", "yearly"}:
        await ctx.send("Use: `bt!capper PropKitchen daily|weekly|monthly|yearly [SPORT]`")
        return

    sport_filter = sport_name.strip() or None
    start_l, end_l = period_bounds_local(period)
    await post_period_summary(ctx, period.capitalize(), start_l, end_l, sport=sport_filter, capper=capper_ok)


@bot.command()
async def export(ctx: commands.Context, scope: str, value: str, sport_name: str = "", capper_name: str = ""):
    """
    Usage:
      bt!export day 2026-02-17
      bt!export month 2026-02
      bt!export year 2026
    Optional filters:
      bt!export month 2026-02 NBA
      bt!export month 2026-02 NBA PropKitchen
    """
    scope = scope.lower().strip()
    sport_filter = sport_name.strip() or None
    capper_filter = capper_name.strip() or None

    if capper_filter:
        cm = capper_match(capper_filter)
        if not cm:
            await ctx.send("Unknown capper. Valid: " + ", ".join(f"`{c}`" for c in list_cappers()))
            return
        capper_filter = cm

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
# AUTOPOST (10AM ET, previous periods)
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

    # DAILY: post yesterday
    yesterday = nl.date() - timedelta(days=1)
    day_key = yesterday.isoformat()
    if _last_post_key["daily"] != day_key:
        _last_post_key["daily"] = day_key
        start_l, end_l = period_bounds_local("daily", yesterday)
        await post_period_summary(channel, banner(f"DAILY ({yesterday.isoformat()})"), start_l, end_l)

    # WEEKLY: post previous Mon–Sun (run only on Monday)
    if nl.weekday() == WEEKLY_POST_WEEKDAY:
        prev_week_ref = nl.date() - timedelta(days=7)
        start_l, end_l = period_bounds_local("weekly", prev_week_ref)
        week_start = start_l.date()
        week_end = (end_l - timedelta(days=1)).date()
        week_key = f"{week_start.isoformat()}_{week_end.isoformat()}"
        if _last_post_key["weekly"] != week_key:
            _last_post_key["weekly"] = week_key
            await post_period_summary(channel, banner(f"WEEKLY ({week_start} → {week_end})"), start_l, end_l)

    # MONTHLY: post previous month (run only on the 1st)
    if nl.day == MONTHLY_POST_DAY:
        prev_month_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("monthly", prev_month_ref)
        month_label = f"{start_l.year}-{start_l.month:02d}"
        if _last_post_key["monthly"] != month_label:
            _last_post_key["monthly"] = month_label
            await post_period_summary(channel, banner(f"MONTHLY ({month_label})"), start_l, end_l)

    # YEARLY: post previous year (run only on Jan 1)
    if nl.month == 1 and nl.day == 1:
        prev_year_ref = nl.date() - timedelta(days=1)
        start_l, end_l = period_bounds_local("yearly", prev_year_ref)
        year_label = f"{start_l.year}"
        if _last_post_key["yearly"] != year_label:
            _last_post_key["yearly"] = year_label
            await post_period_summary(channel, banner(f"YEARLY ({year_label})"), start_l, end_l)


bot.run(TOKEN)
