# bot.py
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
Discord betting tracker (channel-based cappers) with:

- Auto-detect pending plays from natural posts (must include units like: 1u, 0.5u, .5u)
- Grade by reaction from ONLY the channel’s capper (User ID):
    ✅ = win
    ❌ = loss
    🟡 = push
- Bot confirms grading by adding: 📌
- Regrade allowed:
    capper removes 📌 -> bot unlogs bet and restores it to pending
- Storage: SQLite at /var/data/bets.db (Render persistent disk)
- Reporting: Daily/Weekly/Monthly/Yearly/All-time (ET)
- Auto-post daily recap at 10:00 AM ET (yesterday) to SUMMARY_CHANNEL_ID
"""

# =====================
# CONFIG
# =====================

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN env var is not set. Set TOKEN in Render -> Environment.")

PREFIX = "bt!"
DB_PATH = "/var/data/bets.db"
REPORT_TZ = "America/New_York"

# Capper channels (channel_id -> capper_name)
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

# Only THIS user can grade in THIS channel
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

# Where summaries / autopost recaps go
SUMMARY_CHANNEL_ID = 1473454134689796146

# Emojis
EMOJI_WIN = "✅"
EMOJI_LOSS = "❌"
EMOJI_PUSH = "🟡"
EMOJI_LOGGED = "📌"
EMOJI_PENDING = "📝"

# Pending behavior
REACT_ON_PENDING = True
IGNORE_TEST_BETS = True  # if message contains "test", ignore it completely

# Auto-post schedule (ET)
AUTOPOST_ENABLED = True
AUTOPOST_HOUR_ET = 10
AUTOPOST_MINUTE_ET = 0


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


def period_bounds_local(period: str, ref: Optional[date] = None) -> Tuple[datetime, datetime]:
    """
    Calendar periods in ET:
    - daily: ref day 00:00 -> next day 00:00
    - weekly: Mon 00:00 -> next Mon 00:00
    - monthly: 1st 00:00 -> next month 1st 00:00
    - yearly: Jan 1 00:00 -> next Jan 1 00:00
    """
    period = period.lower()
    ref = ref or now_local().date()

    if period == "daily":
        start = datetime(ref.year, ref.month, ref.day, 0, 0, 0, tzinfo=tzinfo())
        return start, start + timedelta(days=1)

    if period == "weekly":
        start_day = ref - timedelta(days=ref.weekday())
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
# DB SETUP + MIGRATION
# =====================

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()


def _ensure_tables_and_columns() -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capper TEXT NOT NULL,
            sport TEXT NOT NULL,
            risk_units REAL NOT NULL,
            net_units REAL NOT NULL,
            result TEXT NOT NULL,
            odds_text TEXT NOT NULL,
            message_id INTEGER,
            channel_id INTEGER,
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

    # Add missing columns safely for older DBs
    cols = cursor.execute("PRAGMA table_info(bets)").fetchall()
    colnames = {c[1].lower() for c in cols}

    if "message_id" not in colnames:
        cursor.execute("ALTER TABLE bets ADD COLUMN message_id INTEGER")
    if "channel_id" not in colnames:
        cursor.execute("ALTER TABLE bets ADD COLUMN channel_id INTEGER")

    conn.commit()

    # Indexes (AFTER columns exist)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_time ON bets(timestamp_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_capper_time ON bets(capper, timestamp_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport_time ON bets(sport, timestamp_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bets_message ON bets(message_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_channel ON pending_bets(channel_id)")
    conn.commit()


_ensure_tables_and_columns()


# =====================
# PARSING (pending detection)
# =====================

# Must include units like "1u", "0.5u", ".5u"
_RE_ANY_UNITS = re.compile(r"(?i)(^|\s)(\d+(?:\.\d+)?|\.\d+)\s*u\b")
# Odds like "(-115)" or "+120" or "-110"
_RE_ANY_AMERICAN_PARENS = re.compile(r"\(\s*([+-]\d+)\s*\)")
_RE_ANY_AMERICAN = re.compile(r"(^|\s)([+-]\d+)(\s|$)")
# Multiplier odds like "3.15x"
_RE_ANY_MULT = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*x\b")

# American odds full match
_RE_AMERICAN_FULL = re.compile(r"^\s*([+-]\d+)\s*$", re.IGNORECASE)
# Mult full match like "3.15x"
_RE_MULT_FULL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*x\s*$", re.IGNORECASE)

AUTO_DETECT_SPORTS = [
    "NBA", "NCAAB", "NFL", "NCAAF", "MLB", "NHL",
    "TENNIS", "ESPORTS", "UFC",
    "SOCCER", "EPL", "MLS", "UCL", "LA LIGA", "SERIE A", "BUNDESLIGA",
]


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


def detect_sport(content: str) -> str:
    upper = content.upper()
    for s in AUTO_DETECT_SPORTS:
        if s.upper() in upper:
            return s
    return "UNSPECIFIED"


def _profit_from_american(risk: float, american: int) -> float:
    if american > 0:
        return risk * (american / 100.0)
    return risk * (100.0 / abs(american))


def _profit_from_multiplier(risk: float, mult: float) -> float:
    # "3x" total return => profit = (3-1)*risk
    return risk * (mult - 1.0)


def compute_net_units(risk: float, odds_text: str, result: str) -> float:
    result = result.lower()
    if result == "push":
        return 0.0

    win_profit = risk
    if odds_text:
        am = _RE_AMERICAN_FULL.match(odds_text)
        if am:
            win_profit = _profit_from_american(risk, int(am.group(1)))
        else:
            mm = _RE_MULT_FULL.match(odds_text)
            if mm:
                win_profit = _profit_from_multiplier(risk, float(mm.group(1)))

    if result == "win":
        return win_profit
    return -risk


# =====================
# DB HELPERS
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


def insert_graded(
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
        INSERT INTO bets (capper, sport, risk_units, net_units, result, odds_text, message_id, channel_id, timestamp_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (capper, sport, risk, net, result, odds_text, message_id, channel_id, ts_utc),
    )
    conn.commit()


def delete_graded_by_message(message_id: int) -> Optional[Tuple[int, int, str, str, float, str, str]]:
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
# REPORTING
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

_last_daily_post = None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if AUTOPOST_ENABLED and not autopost_loop.is_running():
        autopost_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id not in TRACKED_CHANNELS:
        await bot.process_commands(message)
        return

    content = message.content or ""
    if IGNORE_TEST_BETS and "test" in content.lower():
        await bot.process_commands(message)
        return

    # Only detect pending if it includes units
    risk = detect_units(content)
    if risk is None or risk <= 0:
        await bot.process_commands(message)
        return

    capper = TRACKED_CHANNELS[message.channel.id]
    sport = detect_sport(content)
    odds_text = detect_odds_text(content)
    ts_utc = utc_iso(message.created_at.replace(tzinfo=timezone.utc))

    upsert_pending(message.id, message.channel.id, capper, sport, risk, odds_text, ts_utc)

    if REACT_ON_PENDING:
        try:
            await message.add_reaction(EMOJI_PENDING)
        except Exception:
            pass

    await bot.process_commands(message)


async def _try_remove_own_reaction(message: discord.Message, emoji: str) -> None:
    try:
        if bot.user:
            await message.remove_reaction(emoji, bot.user)
    except Exception:
        pass


async def _grade_message(message: discord.Message, result: str) -> None:
    pending = fetch_pending(message.id)
    if not pending:
        # Backfill: detect from message content if pending row wasn't created
        content = message.content or ""
        risk = detect_units(content)
        if risk is None or risk <= 0:
            return
        capper = TRACKED_CHANNELS.get(message.channel.id)
        if not capper:
            return
        sport = detect_sport(content)
        odds_text = detect_odds_text(content)
        ts_utc = utc_iso(message.created_at.replace(tzinfo=timezone.utc))
        upsert_pending(message.id, message.channel.id, capper, sport, risk, odds_text, ts_utc)
        pending = fetch_pending(message.id)

    if not pending:
        return

    _, channel_id, capper, sport, risk, odds_text, ts_utc = pending
    net = compute_net_units(risk, odds_text, result)

    insert_graded(
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

    # Clean: remove 📝 if we added it
    await _try_remove_own_reaction(message, EMOJI_PENDING)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not bot.user:
        return
    if payload.user_id == bot.user.id:
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

    result = "win" if emoji == EMOJI_WIN else ("loss" if emoji == EMOJI_LOSS else "push")
    await _grade_message(msg, result)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    # Regrade workflow: capper removes 📌 -> unlog and restore pending
    if not bot.user:
        return
    if payload.user_id == bot.user.id:
        return

    channel_id = payload.channel_id
    if channel_id not in TRACKED_CHANNELS:
        return

    owner_id = CAPPER_OWNERS.get(channel_id)
    if owner_id is None or payload.user_id != owner_id:
        return

    emoji = str(payload.emoji)
    if emoji != EMOJI_LOGGED:
        return

    restored = delete_graded_by_message(payload.message_id)
    if not restored:
        return

    message_id, ch_id, capper, sport, risk, odds_text, ts_utc = restored
    upsert_pending(message_id, ch_id, capper, sport, risk, odds_text, ts_utc)

    # Re-add pending marker
    channel = bot.get_channel(ch_id)
    if channel is None:
        return
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
async def daily(ctx: commands.Context):
    start_l, end_l = period_bounds_local("daily")
    await post_period_summary(ctx, "Daily", start_l, end_l)


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


# =====================
# AUTOPOST (10AM ET, yesterday)
# =====================

@tasks.loop(minutes=1)
async def autopost_loop():
    global _last_daily_post

    if not AUTOPOST_ENABLED:
        return

    nl = now_local()
    if nl.hour != AUTOPOST_HOUR_ET or nl.minute != AUTOPOST_MINUTE_ET:
        return

    # Prevent double-posting within same day
    yday = nl.date() - timedelta(days=1)
    key = yday.isoformat()
    if _last_daily_post == key:
        return
    _last_daily_post = key

    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if channel is None:
        return

    start_l, end_l = period_bounds_local("daily", yday)
    await post_period_summary(channel, f"🔥 VIP RECAP — DAILY ({yday.isoformat()}) 🔥", start_l, end_l)


bot.run(TOKEN)
