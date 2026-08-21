from __future__ import annotations

from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "bot.py"
data = PATH.read_bytes()
newline = "\r\n" if b"\r\n" in data else "\n"
text = data.decode("utf-8")


def block(lines: list[str]) -> str:
    return newline.join(lines)


def section_bounds(start_marker: str, end_marker: str) -> tuple[int, int]:
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(f"Could not find section start: {start_marker!r}")
    end = text.find(end_marker, start)
    if end < 0:
        raise RuntimeError(f"Could not find section end: {end_marker!r}")
    return start, end


def replace_in_section(
    start_marker: str,
    end_marker: str,
    old: str,
    new: str,
    label: str,
    already: str | None = None,
) -> None:
    global text
    start, end = section_bounds(start_marker, end_marker)
    section = text[start:end]
    if already and already in section:
        print(f"SKIP: {label} already applied")
        return
    count = section.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly 1 target in section, found {count}")
    section = section.replace(old, new, 1)
    text = text[:start] + section + text[end:]
    print(f"OK: {label}")


def replace_once(old: str, new: str, label: str, already: str | None = None) -> None:
    global text
    if already and already in text:
        print(f"SKIP: {label} already applied")
        return
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly 1 target, found {count}")
    text = text.replace(old, new, 1)
    print(f"OK: {label}")


# ---------------------------------------------------------------------------
# Preserve guild ownership when a graded bet is moved back to pending.
# ---------------------------------------------------------------------------
UNGRADE_START = "def ungrade_bet(message_id: int) -> bool:"
UNGRADE_END = "async def find_remaining_authorized_grade("

replace_in_section(
    UNGRADE_START,
    UNGRADE_END,
    "            multiplier, wager_category, platform, platform_type, duplicate_key",
    "            multiplier, wager_category, platform, platform_type, duplicate_key, guild_id",
    "ungrade select guild_id",
    already="platform, platform_type, duplicate_key, guild_id",
)
replace_in_section(
    UNGRADE_START,
    UNGRADE_END,
    "            message_id, channel_id, capper, capper_user_id, author_user_id,",
    "            message_id, guild_id, channel_id, capper, capper_user_id, author_user_id,",
    "ungrade pending guild column",
    already="message_id, guild_id, channel_id, capper, capper_user_id, author_user_id,",
)
replace_in_section(
    UNGRADE_START,
    UNGRADE_END,
    "        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "ungrade pending placeholder",
    already="VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
)
replace_in_section(
    UNGRADE_START,
    UNGRADE_END,
    block([
        "        (",
        "            int(message_id),",
        "            int(row[0]),",
    ]),
    block([
        "        (",
        "            int(message_id),",
        "            int(row[26] or 0),",
        "            int(row[0]),",
    ]),
    "ungrade pending guild value",
    already="            int(row[26] or 0),",
)


# ---------------------------------------------------------------------------
# Pending summaries and destructive pending cleanup must stay in one guild.
# ---------------------------------------------------------------------------
PENDING_START = "async def pending_cmd(ctx: commands.Context) -> None:"
PENDING_END = "async def clear_pending_rows("
replace_in_section(
    PENDING_START,
    PENDING_END,
    block([
        "        FROM pending",
        "        GROUP BY capper",
        "        ORDER BY COUNT(*) DESC",
        '        """',
        "    ).fetchall()",
    ]),
    block([
        "        FROM pending",
        "        WHERE guild_id = ?",
        "        GROUP BY capper",
        "        ORDER BY COUNT(*) DESC",
        '        """',
        "        , (int(ctx.guild.id),)",
        "    ).fetchall()",
    ]),
    "pending command guild filter",
    already="        WHERE guild_id = ?" + newline + "        GROUP BY capper",
)

CLEAR_START = "async def clear_pending_rows("
CLEAR_END = '@bot.command(name="clear_pending")'
replace_in_section(
    CLEAR_START,
    CLEAR_END,
    ") -> None:" + newline + "    rows = cur.execute(",
    ") -> None:" + newline + "    guild_id = int(ctx.guild.id)" + newline + "    scoped_where_sql = f\"(guild_id = ?) AND ({where_sql})\"" + newline + "    scoped_params = (guild_id, *params)" + newline + "" + newline + "    rows = cur.execute(",
    "clear pending scoped parameters",
    already="scoped_where_sql = f\"(guild_id = ?) AND ({where_sql})\"",
)
replace_in_section(
    CLEAR_START,
    CLEAR_END,
    "        WHERE {where_sql}",
    "        WHERE {scoped_where_sql}",
    "clear pending select guild filter",
    already="        WHERE {scoped_where_sql}",
)
replace_in_section(
    CLEAR_START,
    CLEAR_END,
    block([
        '        """',
        "        params,",
        "    ).fetchall()",
    ]),
    block([
        '        """',
        "        scoped_params,",
        "    ).fetchall()",
    ]),
    "clear pending select guild params",
    already="        scoped_params," + newline + "    ).fetchall()",
)
replace_in_section(
    CLEAR_START,
    CLEAR_END,
    '    cur.execute(f"DELETE FROM pending WHERE {where_sql}", params)',
    '    cur.execute(f"DELETE FROM pending WHERE {scoped_where_sql}", scoped_params)',
    "clear pending delete guild filter",
    already='DELETE FROM pending WHERE {scoped_where_sql}',
)


# ---------------------------------------------------------------------------
# Bulk repair commands: each server may repair only its own rows.
# ---------------------------------------------------------------------------
FIX_DEC_START = "async def fix_decimal_units_cmd(ctx: commands.Context) -> None:"
FIX_DEC_END = '@bot.command(name="fix_bet_dates"'
replace_in_section(
    FIX_DEC_START,
    FIX_DEC_END,
    block([
        "        SELECT id, content, risk_units, odds_text, result, net_units",
        "        FROM bets",
        '        """',
        "    ).fetchall()",
    ]),
    block([
        "        SELECT id, content, risk_units, odds_text, result, net_units",
        "        FROM bets",
        "        WHERE guild_id = ?",
        '        """',
        "        , (int(ctx.guild.id),)",
        "    ).fetchall()",
    ]),
    "decimal repair bets guild filter",
    already="SELECT id, content, risk_units, odds_text, result, net_units" + newline + "        FROM bets" + newline + "        WHERE guild_id = ?",
)
replace_in_section(
    FIX_DEC_START,
    FIX_DEC_END,
    block([
        "        SELECT message_id, content, risk_units",
        "        FROM pending",
        '        """',
        "    ).fetchall()",
    ]),
    block([
        "        SELECT message_id, content, risk_units",
        "        FROM pending",
        "        WHERE guild_id = ?",
        '        """',
        "        , (int(ctx.guild.id),)",
        "    ).fetchall()",
    ]),
    "decimal repair pending guild filter",
    already="SELECT message_id, content, risk_units" + newline + "        FROM pending" + newline + "        WHERE guild_id = ?",
)

FIX_DATE_START = "async def fix_bet_dates_cmd(ctx: commands.Context) -> None:"
FIX_DATE_END = '@bot.command(name="recalc_multipliers"'
replace_in_section(
    FIX_DATE_START,
    FIX_DATE_END,
    block([
        "        SELECT id, content, created_utc, graded_utc, bet_date",
        "        FROM bets",
        '        """',
        "    ).fetchall()",
    ]),
    block([
        "        SELECT id, content, created_utc, graded_utc, bet_date",
        "        FROM bets",
        "        WHERE guild_id = ?",
        '        """',
        "        , (int(ctx.guild.id),)",
        "    ).fetchall()",
    ]),
    "bet-date repair bets guild filter",
    already="SELECT id, content, created_utc, graded_utc, bet_date" + newline + "        FROM bets" + newline + "        WHERE guild_id = ?",
)
replace_in_section(
    FIX_DATE_START,
    FIX_DATE_END,
    block([
        "        SELECT message_id, content, created_utc, bet_date",
        "        FROM pending",
        '        """',
        "    ).fetchall()",
    ]),
    block([
        "        SELECT message_id, content, created_utc, bet_date",
        "        FROM pending",
        "        WHERE guild_id = ?",
        '        """',
        "        , (int(ctx.guild.id),)",
        "    ).fetchall()",
    ]),
    "bet-date repair pending guild filter",
    already="SELECT message_id, content, created_utc, bet_date" + newline + "        FROM pending" + newline + "        WHERE guild_id = ?",
)

RECALC_START = "async def recalc_multipliers_cmd(ctx: commands.Context) -> None:"
RECALC_END = '@bot.command(name="backfill_wager_types"'
replace_in_section(
    RECALC_START,
    RECALC_END,
    block([
        "        FROM bets",
        "        WHERE result = 'win' AND LOWER(odds_text) LIKE '%x%'",
        '        """',
        "    ).fetchall()",
    ]),
    block([
        "        FROM bets",
        "        WHERE guild_id = ? AND result = 'win' AND LOWER(odds_text) LIKE '%x%'",
        '        """',
        "        , (int(ctx.guild.id),)",
        "    ).fetchall()",
    ]),
    "multiplier recalc guild filter",
    already="WHERE guild_id = ? AND result = 'win'",
)

WAGER_START = "async def backfill_wager_types_cmd("
WAGER_END = '@bot.command(name="fix_sports"'
replace_in_section(
    WAGER_START,
    WAGER_END,
    block([
        "        FROM bets",
        "        WHERE COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
        '        """',
        "        (cutoff,),",
    ]),
    block([
        "        FROM bets",
        "        WHERE guild_id = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
        '        """',
        "        (int(ctx.guild.id), cutoff),",
    ]),
    "wager backfill bets guild filter",
    already="FROM bets" + newline + "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
)
replace_in_section(
    WAGER_START,
    WAGER_END,
    block([
        "        FROM pending",
        "        WHERE COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
        '        """',
        "        (cutoff,),",
    ]),
    block([
        "        FROM pending",
        "        WHERE guild_id = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
        '        """',
        "        (int(ctx.guild.id), cutoff),",
    ]),
    "wager backfill pending guild filter",
    already="FROM pending" + newline + "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
)

SPORT_START = "async def fix_sports_cmd("
SPORT_END = '@bot.command(name="backfill_content")'
replace_in_section(
    SPORT_START,
    SPORT_END,
    block([
        "        FROM bets",
        "        WHERE COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
        '        """',
        "        (cutoff,),",
    ]),
    block([
        "        FROM bets",
        "        WHERE guild_id = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
        '        """',
        "        (int(ctx.guild.id), cutoff),",
    ]),
    "sport repair bets guild filter",
    already="FROM bets" + newline + "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
)
replace_in_section(
    SPORT_START,
    SPORT_END,
    block([
        "        FROM pending",
        "        WHERE COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
        '        """',
        "        (cutoff,),",
    ]),
    block([
        "        FROM pending",
        "        WHERE guild_id = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
        '        """',
        "        (int(ctx.guild.id), cutoff),",
    ]),
    "sport repair pending guild filter",
    already="FROM pending" + newline + "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
)

CONTENT_START = "async def backfill_content_cmd(ctx: commands.Context, limit: int = 200) -> None:"
CONTENT_END = "def log_correction("
replace_in_section(
    CONTENT_START,
    CONTENT_END,
    block([
        "        FROM bets",
        "        WHERE content = '' OR content IS NULL",
        "        ORDER BY created_utc DESC",
        "        LIMIT ?",
        '        """',
        "        (limit,),",
    ]),
    block([
        "        FROM bets",
        "        WHERE guild_id = ? AND (content = '' OR content IS NULL)",
        "        ORDER BY created_utc DESC",
        "        LIMIT ?",
        '        """',
        "        (int(ctx.guild.id), limit),",
    ]),
    "content backfill guild filter",
    already="WHERE guild_id = ? AND (content = '' OR content IS NULL)",
)

FORCE_START = "async def force_sport_cmd("
FORCE_END = '@bot.command(name="backup"'
replace_in_section(
    FORCE_START,
    FORCE_END,
    block([
        "        FROM bets",
        "        WHERE UPPER(sport) = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10), substr(created_utc, 1, 10)) BETWEEN ? AND ?",
        '        """',
        "        (old_sport, start_iso, end_iso),",
    ]),
    block([
        "        FROM bets",
        "        WHERE guild_id = ?",
        "          AND UPPER(sport) = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10), substr(created_utc, 1, 10)) BETWEEN ? AND ?",
        '        """',
        "        (int(ctx.guild.id), old_sport, start_iso, end_iso),",
    ]),
    "force sport bets guild filter",
    already="FROM bets" + newline + "        WHERE guild_id = ?" + newline + "          AND UPPER(sport) = ?",
)
replace_in_section(
    FORCE_START,
    FORCE_END,
    block([
        "        FROM pending",
        "        WHERE UPPER(sport) = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) BETWEEN ? AND ?",
        '        """',
        "        (old_sport, start_iso, end_iso),",
    ]),
    block([
        "        FROM pending",
        "        WHERE guild_id = ?",
        "          AND UPPER(sport) = ?",
        "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) BETWEEN ? AND ?",
        '        """',
        "        (int(ctx.guild.id), old_sport, start_iso, end_iso),",
    ]),
    "force sport pending guild filter",
    already="FROM pending" + newline + "        WHERE guild_id = ?" + newline + "          AND UPPER(sport) = ?",
)


# ---------------------------------------------------------------------------
# A shared SaaS database must never be downloadable by arbitrary server admins.
# Keep the raw database backup restricted to the platform owner for now.
# ---------------------------------------------------------------------------
BACKUP_START = "async def backup_cmd(ctx: commands.Context) -> None:"
BACKUP_END = '@bot.command(name="export"'
replace_in_section(
    BACKUP_START,
    BACKUP_END,
    "async def backup_cmd(ctx: commands.Context) -> None:" + newline + "    timestamp =",
    "async def backup_cmd(ctx: commands.Context) -> None:" + newline + "    if int(ctx.author.id) != int(ADMIN_USER_ID):" + newline + "        await ctx.send(\"Full database backups are restricted to the BetTracker platform owner. Use `bt!export` for server data.\")" + newline + "        return" + newline + "" + newline + "    timestamp =",
    "restrict raw database backup",
    already="Full database backups are restricted to the BetTracker platform owner.",
)


# ---------------------------------------------------------------------------
# Add guild-first indexes for tenant-filtered reads and duplicate checks.
# ---------------------------------------------------------------------------
replace_once(
    '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")',
    block([
        '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_guild_graded ON bets(guild_id, graded_utc);")',
        '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_guild_capper ON bets(guild_id, capper, graded_utc);")',
        '    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_guild_created ON pending(guild_id, created_utc);")',
        '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")',
    ]),
    "guild database indexes",
    already="idx_bets_guild_graded",
)

PATH.write_bytes(text.encode("utf-8"))
print("Admin and maintenance guild isolation edits applied successfully.")
