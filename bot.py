"""
bot.py

Discord betting tracker (channel-based cappers) using reaction grading.

Flow (Option B):
1) A play is posted in a tracked capper channel (can be capper user OR webhook).
2) Bot reacts 📝 to mark it as "pending" (only if it contains units like 1u / 0.5u).
3) The capper grades by reacting:
   ✅ = Win
   ❌ = Loss
   ➖ = Push
4) Bot logs the result to SQLite and reacts 📌 (no extra messages).
5) If capper removes ✅/❌/➖, bot ungrades (removes from DB), switches back to 📝.

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
            odds_text TEXT NOT NULL
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
            graded_utc TEXT NOT NULL
        )
        """
    )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_capper_time ON bets(capper, graded_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_sport_time ON bets(sport, graded_utc);")
    conn.commit()


ensure_schema()


# =====================
# PARSING PLAYS
# =====================

RE_UNITS = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*u\b")
RE_AMERICAN_PAREN = re.compile(r"(?i)\(([-+]\d{2,5})\)")
RE_AMERICAN = re.compile(r"(?i)\b([-+]\d{2,5})\b")
RE_MULT = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*x\b")

SPORT_KEYWORDS = [
    ("NCAAB", ["NCAAB", "CBB", "COLLEGE BASKETBALL"]),
    ("NBA", ["NBA"]),
    ("WNBA", ["WNBA"]),
    ("NFL", ["NFL"]),
    ("NCAAF", ["NCAAF", "CFB", "COLLEGE FOOTBALL"]),
    ("NHL", ["NHL", "HOCKEY"]),
    ("MLB", ["MLB", "BASEBALL"]),
    ("SOCCER", ["SOCCER", "EPL", "UCL", "MLS", "LA LIGA", "SERIE A", "BUNDESLIGA"]),
    ("TENNIS", ["TENNIS", "ATP", "WTA"]),
    ("ESPORTS", ["ESPORTS", "E-SPORTS", "CS2", "CSGO", "VALORANT", "LOL", "DOTA"]),
    ("UFC", ["UFC"]),
    ("MMA", ["MMA"]),
    ("BOXING", ["BOXING"]),
]


def infer_sport(text: str) -> str:
    up = text.upper()
    for code, keys in SPORT_KEYWORDS:
        for k in keys:
            if k in up:
                return code
    return "UNKNOWN"


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


def profit_from_american(risk: float, american: int) -> float:
    if american > 0:
        return risk * (american / 100.0)
    return risk * (100.0 / abs(american))


def profit_from_multiplier(risk: float, mult: float) -> float:
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
                return risk
            return profit_from_multiplier(risk, mult)
        except Exception:
            return risk

    try:
        american = int(odds_text)
        return profit_from_american(risk, american)
    except Exception:
        return risk


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
    return [(str(c), float(u), int(w), int(l), int(p)) for c, u, w, l, p in rows]


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


# =====================
# PENDING + GRADING
# =====================

async def safe_add_reaction(msg: discord.Message, emoji: str) -> None:
    try:
        await msg.add_reaction(emoji)
    except Exception:
        return


async def safe_remove_reaction(msg: discord.Message, emoji: str) -> None:
    try:
        await msg.clear_reaction(emoji)
    except Exception:
        return


def pending_exists(message_id: int) -> bool:
    return cur.execute("SELECT 1 FROM pending WHERE message_id = ?", (message_id,)).fetchone() is not None


def bet_exists(message_id: int) -> bool:
    return cur.execute("SELECT 1 FROM bets WHERE message_id = ?", (message_id,)).fetchone() is not None


def insert_pending(message_id: int, channel_id: int, capper: Capper, content: str) -> bool:
    risk = parse_risk_units(content)
    if risk is None:
        return False

    sport = infer_sport(content)
    odds_text = parse_odds_text(content)

    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (message_id, channel_id, capper, capper_user_id, content, created_utc, sport, risk_units, odds_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            channel_id,
            capper.name,
            capper.user_id,
            content,
            utc_iso(datetime.now(timezone.utc)),
            sport,
            float(risk),
            odds_text,
        ),
    )
    conn.commit()
    return True


def grade_pending(message_id: int, result: str) -> bool:
    row = cur.execute(
        """
        SELECT channel_id, capper, capper_user_id, content, sport, risk_units, odds_text, created_utc
        FROM pending
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return False

    channel_id, capper_name, capper_user_id, content, sport, risk, odds_text, created_utc = row
    net = compute_net_units(float(risk), str(odds_text), result)

    cur.execute(
        """
        INSERT OR REPLACE INTO bets
        (message_id, channel_id, capper, capper_user_id, sport, risk_units, net_units, result, odds_text, created_utc, graded_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    cur.execute("DELETE FROM pending WHERE message_id = ?", (message_id,))
    conn.commit()
    return True


def ungrade_bet(message_id: int) -> bool:
    row = cur.execute(
        """
        SELECT channel_id, capper, capper_user_id, sport, risk_units, odds_text
        FROM bets
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return False

    channel_id, capper_name, capper_user_id, sport, risk, odds_text = row

    placeholder = f"{sport} {risk}u {odds_text}".strip()

    cur.execute("DELETE FROM bets WHERE message_id = ?", (message_id,))
    cur.execute(
        """
        INSERT OR REPLACE INTO pending
        (message_id, channel_id, capper, capper_user_id, content, created_utc, sport, risk_units, odds_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(message_id),
            int(channel_id),
            str(capper_name),
            int(capper_user_id),
            placeholder,
            utc_iso(datetime.now(timezone.utc)),
            str(sport),
            float(risk),
            str(odds_text),
        ),
    )
    conn.commit()
    return True


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

    ok = insert_pending(message.id, message.channel.id, capper, message.content or "")
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

    if not pending_exists(payload.message_id):
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        msg = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception:
        return

    result = "win" if emoji == WIN_EMOJI else ("loss" if emoji == LOSS_EMOJI else "push")
    if not grade_pending(payload.message_id, result):
        return

    await safe_remove_reaction(msg, PENDING_REACTION)
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

    if not ungrade_bet(payload.message_id):
        return

    await safe_remove_reaction(msg, LOGGED_REACTION)
    await safe_add_reaction(msg, PENDING_REACTION)


# =====================
# COMMANDS
# =====================

@bot.command()
async def ping(ctx: commands.Context) -> None:
    await ctx.send("pong")


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
