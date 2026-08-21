from __future__ import annotations

from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "bot.py"
data = PATH.read_bytes()
newline = "\r\n" if b"\r\n" in data else "\n"
text = data.decode("utf-8")


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


# Finish the clear-pending function where the first helper stopped.
CLEAR_START = "async def clear_pending_rows("
CLEAR_END = '@bot.command(name="clear_pending")'
replace_in_section(
    CLEAR_START,
    CLEAR_END,
    "        params," + newline + "    ).fetchall()",
    "        scoped_params," + newline + "    ).fetchall()",
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


# Scope decimal-unit repair to the invoking guild.
FIX_DEC_START = "async def fix_decimal_units_cmd(ctx: commands.Context) -> None:"
FIX_DEC_END = '@bot.command(name="fix_bet_dates"'
replace_in_section(
    FIX_DEC_START,
    FIX_DEC_END,
    "        FROM bets" + newline + '        """' + newline + "    ).fetchall()",
    "        FROM bets" + newline + "        WHERE guild_id = ?" + newline + '        """,' + newline + "        (int(ctx.guild.id),)," + newline + "    ).fetchall()",
    "decimal repair bets guild filter",
    already="        FROM bets" + newline + "        WHERE guild_id = ?",
)
replace_in_section(
    FIX_DEC_START,
    FIX_DEC_END,
    "        FROM pending" + newline + '        """' + newline + "    ).fetchall()",
    "        FROM pending" + newline + "        WHERE guild_id = ?" + newline + '        """,' + newline + "        (int(ctx.guild.id),)," + newline + "    ).fetchall()",
    "decimal repair pending guild filter",
    already="        FROM pending" + newline + "        WHERE guild_id = ?",
)


# Scope bet-date repair to the invoking guild.
FIX_DATE_START = "async def fix_bet_dates_cmd(ctx: commands.Context) -> None:"
FIX_DATE_END = '@bot.command(name="recalc_multipliers"'
replace_in_section(
    FIX_DATE_START,
    FIX_DATE_END,
    "        FROM bets" + newline + '        """' + newline + "    ).fetchall()",
    "        FROM bets" + newline + "        WHERE guild_id = ?" + newline + '        """,' + newline + "        (int(ctx.guild.id),)," + newline + "    ).fetchall()",
    "bet-date repair bets guild filter",
    already="        FROM bets" + newline + "        WHERE guild_id = ?",
)
replace_in_section(
    FIX_DATE_START,
    FIX_DATE_END,
    "        FROM pending" + newline + '        """' + newline + "    ).fetchall()",
    "        FROM pending" + newline + "        WHERE guild_id = ?" + newline + '        """,' + newline + "        (int(ctx.guild.id),)," + newline + "    ).fetchall()",
    "bet-date repair pending guild filter",
    already="        FROM pending" + newline + "        WHERE guild_id = ?",
)


# Scope multiplier recalculation to the invoking guild.
RECALC_START = "async def recalc_multipliers_cmd(ctx: commands.Context) -> None:"
RECALC_END = '@bot.command(name="backfill_wager_types"'
replace_in_section(
    RECALC_START,
    RECALC_END,
    "        WHERE result = 'win' AND LOWER(odds_text) LIKE '%x%'" + newline + '        """' + newline + "    ).fetchall()",
    "        WHERE guild_id = ? AND result = 'win' AND LOWER(odds_text) LIKE '%x%'" + newline + '        """,' + newline + "        (int(ctx.guild.id),)," + newline + "    ).fetchall()",
    "multiplier recalc guild filter",
    already="WHERE guild_id = ? AND result = 'win'",
)


# Scope wager-type backfill to the invoking guild.
WAGER_START = "async def backfill_wager_types_cmd("
WAGER_END = '@bot.command(name="fix_sports"'
replace_in_section(
    WAGER_START,
    WAGER_END,
    "        WHERE COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (cutoff,),",
    "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), cutoff),",
    "wager backfill bets guild filter",
    already="        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
)
replace_in_section(
    WAGER_START,
    WAGER_END,
    "        WHERE COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (cutoff,),",
    "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), cutoff),",
    "wager backfill pending guild filter",
    already="        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
)


# Scope sport repair to the invoking guild.
SPORT_START = "async def fix_sports_cmd("
SPORT_END = '@bot.command(name="backfill_content")'
replace_in_section(
    SPORT_START,
    SPORT_END,
    "        WHERE COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (cutoff,),",
    "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), cutoff),",
    "sport repair bets guild filter",
    already="        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10)) >= ?",
)
replace_in_section(
    SPORT_START,
    SPORT_END,
    "        WHERE COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (cutoff,),",
    "        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), cutoff),",
    "sport repair pending guild filter",
    already="        WHERE guild_id = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) >= ?",
)


# Scope content backfill to the invoking guild.
CONTENT_START = "async def backfill_content_cmd(ctx: commands.Context, limit: int = 200) -> None:"
CONTENT_END = "def log_correction("
replace_in_section(
    CONTENT_START,
    CONTENT_END,
    "        WHERE content = '' OR content IS NULL" + newline + "        ORDER BY created_utc DESC" + newline + "        LIMIT ?" + newline + '        """,' + newline + "        (limit,),",
    "        WHERE guild_id = ? AND (content = '' OR content IS NULL)" + newline + "        ORDER BY created_utc DESC" + newline + "        LIMIT ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), limit),",
    "content backfill guild filter",
    already="WHERE guild_id = ? AND (content = '' OR content IS NULL)",
)


# Scope forced sport corrections to the invoking guild.
FORCE_START = "async def force_sport_cmd("
FORCE_END = '@bot.command(name="backup"'
replace_in_section(
    FORCE_START,
    FORCE_END,
    "        WHERE UPPER(sport) = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10), substr(created_utc, 1, 10)) BETWEEN ? AND ?" + newline + '        """,' + newline + "        (old_sport, start_iso, end_iso),",
    "        WHERE guild_id = ?" + newline + "          AND UPPER(sport) = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(graded_utc, 1, 10), substr(created_utc, 1, 10)) BETWEEN ? AND ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), old_sport, start_iso, end_iso),",
    "force sport bets guild filter",
    already="        WHERE guild_id = ?" + newline + "          AND UPPER(sport) = ?",
)
replace_in_section(
    FORCE_START,
    FORCE_END,
    "        WHERE UPPER(sport) = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) BETWEEN ? AND ?" + newline + '        """,' + newline + "        (old_sport, start_iso, end_iso),",
    "        WHERE guild_id = ?" + newline + "          AND UPPER(sport) = ?" + newline + "          AND COALESCE(NULLIF(bet_date, ''), substr(created_utc, 1, 10)) BETWEEN ? AND ?" + newline + '        """,' + newline + "        (int(ctx.guild.id), old_sport, start_iso, end_iso),",
    "force sport pending guild filter",
    already="        WHERE guild_id = ?" + newline + "          AND UPPER(sport) = ?",
)


# Shared raw database backups are platform-owner only.
BACKUP_START = "async def backup_cmd(ctx: commands.Context) -> None:"
BACKUP_END = '@bot.command(name="export"'
replace_in_section(
    BACKUP_START,
    BACKUP_END,
    "async def backup_cmd(ctx: commands.Context) -> None:" + newline + "    timestamp =",
    "async def backup_cmd(ctx: commands.Context) -> None:" + newline + "    if int(ctx.author.id) != int(ADMIN_USER_ID):" + newline + "        await ctx.send(\"Full database backups are restricted to the BetTracker platform owner. Use `bt!export` for server data.\")" + newline + "        return" + newline + newline + "    timestamp =",
    "restrict raw database backup",
    already="Full database backups are restricted to the BetTracker platform owner.",
)


# Add indexes optimized for tenant-filtered reads.
replace_once(
    '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")',
    '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_guild_graded ON bets(guild_id, graded_utc);")' + newline +
    '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_guild_capper ON bets(guild_id, capper, graded_utc);")' + newline +
    '    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_guild_created ON pending(guild_id, created_utc);")' + newline +
    '    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_graded_utc ON bets(graded_utc);")',
    "guild database indexes",
    already="idx_bets_guild_graded",
)

PATH.write_bytes(text.encode("utf-8"))
print("Remaining admin and maintenance guild isolation edits applied successfully.")
