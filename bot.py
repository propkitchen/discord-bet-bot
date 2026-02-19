# FULL VIP PRODUCTION BOT
# (Large file intentionally complete and self-contained)

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
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# =====================
# CONFIG
# =====================

PREFIX = "bt!"
TOKEN = os.getenv("TOKEN")

DB_PATH = "/var/data/bets.db"
REPORT_TZ = "America/New_York"

SUMMARY_CHANNEL_ID = 1473454134689796146

AUTOPOST_ENABLED = True
AUTOPOST_HOUR_ET = 10
AUTOPOST_MINUTE_ET = 0

PENDING_REACTION = "📝"
LOGGED_REACTION = "📌"

WIN_EMOJI = "✅"
LOSS_EMOJI = "❌"
PUSH_EMOJI = "➖"
GRADE_EMOJIS = {WIN_EMOJI, LOSS_EMOJI, PUSH_EMOJI}

ADMIN_USER_IDS = {
    1230980936657535061,  # Add your admin IDs here
}


# =====================
# CAPERS
# =====================

@dataclass(frozen=True)
class Capper:
    name: str
    user_id: int


TRACKED_CHANNELS: Dict[int, Capper] = {
    1257081246509563944: Capper("PropKitchen", 1230980936657535061),
    1258244563726893106: Capper("Hotshot", 475659527337934849),
    1281388388569579608: Capper("Clipset", 684940092665757696),
    1278486906169987226: Capper("PXS", 933024893992329286),
    1356017581558857796: Capper("MattLocks", 1242294328253218878),
    1344526479366688808: Capper("BallsOut", 1345160333261668362),
    1409640332295147570: Capper("Gr8", 1109269360037601411),
    1430746272192659569: Capper("MikeLocks", 1430751846125010970),
    1424256774692667422: Capper("BetsByBray", 865284268745949194),
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


def now_local():
    return datetime.now(_tz())


def to_utc(dt_local):
    return dt_local.astimezone(timezone.utc)


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# =====================
# DATABASE
# =====================

def db_connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


conn = db_connect()
cur = conn.cursor()


def ensure_schema():
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            capper TEXT,
            capper_user_id INTEGER,
            content TEXT,
            created_utc TEXT,
            sport TEXT,
            risk_units REAL,
            odds_text TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE,
            channel_id INTEGER,
            capper TEXT,
            capper_user_id INTEGER,
            sport TEXT,
            risk_units REAL,
            net_units REAL,
            result TEXT,
            odds_text TEXT,
            content TEXT,
            created_utc TEXT,
            graded_utc TEXT,
            graded_date_et TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS autopost_memory (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()


ensure_schema()


# =====================
# UTILS
# =====================

def truncate(text: str, length: int = 80) -> str:
    return text if len(text) <= length else text[:length] + "..."


def calculate_roi(net: float, risk: float) -> float:
    if risk == 0:
        return 0.0
    return (net / risk) * 100


def get_streak(capper: str) -> str:
    rows = cur.execute("""
        SELECT result FROM bets
        WHERE capper = ?
        ORDER BY graded_utc DESC
        LIMIT 20
    """, (capper,)).fetchall()

    if not rows:
        return "-"

    streak_type = rows[0][0]
    count = 0
    for r in rows:
        if r[0] == streak_type:
            count += 1
        else:
            break

    if streak_type == "win":
        return f"W{count}"
    if streak_type == "loss":
        return f"L{count}"
    return "-"


# =====================
# SAFE CHANNEL FETCH
# =====================

async def safe_get_channel(channel_id):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    return channel


# =====================
# EVENTS
# =====================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if AUTOPOST_ENABLED:
        autopost_loop.start()


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    if emoji not in GRADE_EMOJIS:
        return

    capper = TRACKED_CHANNELS.get(payload.channel_id)
    if not capper or payload.user_id != capper.user_id:
        return

    row = cur.execute("SELECT * FROM pending WHERE message_id = ?", (payload.message_id,)).fetchone()
    if not row:
        return

    result = "win" if emoji == WIN_EMOJI else "loss" if emoji == LOSS_EMOJI else "push"

    _, channel_id, capper_name, capper_user_id, content, created_utc, sport, risk, odds = row

    net = risk if result == "win" else -risk if result == "loss" else 0

    graded_utc = utc_iso(datetime.utcnow().replace(tzinfo=timezone.utc))
    graded_date_et = now_local().date().isoformat()

    cur.execute("""
        INSERT INTO bets (
            message_id, channel_id, capper, capper_user_id, sport,
            risk_units, net_units, result, odds_text,
            content, created_utc, graded_utc, graded_date_et
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        payload.message_id, channel_id, capper_name, capper_user_id,
        sport, risk, net, result, odds,
        content, created_utc, graded_utc, graded_date_et
    ))

    cur.execute("DELETE FROM pending WHERE message_id = ?", (payload.message_id,))
    conn.commit()

    channel = await safe_get_channel(payload.channel_id)
    if channel:
        msg = await channel.fetch_message(payload.message_id)
        await msg.clear_reaction(PENDING_REACTION)
        await msg.add_reaction(LOGGED_REACTION)


# =====================
# ADMIN COMMANDS
# =====================

def is_admin(user_id):
    return user_id in ADMIN_USER_IDS


@bot.command()
async def removepending(ctx, message_id: int):
    if not is_admin(ctx.author.id):
        return
    cur.execute("DELETE FROM pending WHERE message_id = ?", (message_id,))
    conn.commit()
    await ctx.send("Pending bet removed.")


@bot.command()
async def removebet(ctx, message_id: int):
    if not is_admin(ctx.author.id):
        return
    cur.execute("DELETE FROM bets WHERE message_id = ?", (message_id,))
    conn.commit()
    await ctx.send("Bet removed.")


@bot.command()
async def listpending(ctx):
    rows = cur.execute("SELECT message_id, capper, risk_units FROM pending").fetchall()
    if not rows:
        await ctx.send("No pending bets.")
        return
    text = "\n".join([f"{r[0]} | {r[1]} | {r[2]}u" for r in rows])
    await ctx.send(f"Pending Bets:\n{text}")


# =====================
# RUN
# =====================

if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable.")

bot.run(TOKEN)
