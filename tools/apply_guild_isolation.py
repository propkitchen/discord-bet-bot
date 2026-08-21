from __future__ import annotations

from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "bot.py"
data = PATH.read_bytes()
newline = "\r\n" if b"\r\n" in data else "\n"
text = data.decode("utf-8")


def block(lines: list[str]) -> str:
    return newline.join(lines)


def replace_once(old: str, new: str, label: str, marker: str | None = None) -> None:
    global text
    if marker and marker in text:
        print(f"SKIP: {label} already applied")
        return
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly 1 target, found {count}")
    text = text.replace(old, new, 1)
    print(f"OK: {label}")


# 1) Scope general query summaries to the Discord server that invoked them.
replace_once(
    block([
        "    if error:",
        "        await ctx.send(error)",
        "        return",
        "    add_time_filter(where_parts, params, start_local, end_local)",
    ]),
    block([
        "    if error:",
        "        await ctx.send(error)",
        "        return",
        "",
        "    if ctx.guild is not None:",
        "        where_parts.append(\"guild_id = ?\")",
        "        params.append(int(ctx.guild.id))",
        "    add_time_filter(where_parts, params, start_local, end_local)",
    ]),
    "post_query_summary guild filter",
    marker='where_parts.append("guild_id = ?")' + newline + '        params.append(int(ctx.guild.id))' + newline + '    add_time_filter(where_parts, params, start_local, end_local)',
)

# 2) Scope leaderboards to the invoking Discord server.
replace_once(
    block([
        "    where_parts: List[str] = []",
        "    params: List[object] = []",
        "    if sport_name:",
    ]),
    block([
        "    where_parts: List[str] = []",
        "    params: List[object] = []",
        "    if ctx.guild is not None:",
        "        where_parts.append(\"guild_id = ?\")",
        "        params.append(int(ctx.guild.id))",
        "    if sport_name:",
    ]),
    "post_leaderboard_query guild filter",
    marker='params: List[object] = []' + newline + '    if ctx.guild is not None:' + newline + '        where_parts.append("guild_id = ?")' + newline + '        params.append(int(ctx.guild.id))' + newline + '    if sport_name:',
)

# 3) Scope master capper reports to the invoking Discord server.
replace_once(
    block([
        '    where_parts = ["LOWER(capper) = ?"]',
        "    params: List[object] = [capper_name.lower()]",
        "    add_time_filter(where_parts, params, start_local, end_local)",
    ]),
    block([
        '    where_parts = ["LOWER(capper) = ?"]',
        "    params: List[object] = [capper_name.lower()]",
        "    if ctx.guild is not None:",
        '        where_parts.append("guild_id = ?")',
        "        params.append(int(ctx.guild.id))",
        "    add_time_filter(where_parts, params, start_local, end_local)",
    ]),
    "post_master_report guild filter",
    marker='where_parts = ["LOWER(capper) = ?"]' + newline + '    params: List[object] = [capper_name.lower()]' + newline + '    if ctx.guild is not None:',
)

# 4) Scope scheduled period summaries using the guild attached to the target channel.
replace_once(
    block([
        "    where_sql, params = date_window_where(start_local, end_local)",
        "    await post_leaderboard(",
    ]),
    block([
        "    where_sql, params = date_window_where(start_local, end_local)",
        '    guild = getattr(channel, "guild", None)',
        "    if guild is not None:",
        '        where_sql = f"(guild_id = ?) AND ({where_sql})"',
        "        params = (int(guild.id), *params)",
        "    await post_leaderboard(",
    ]),
    "post_period_summary guild filter",
    marker='guild = getattr(channel, "guild", None)',
)

# 5) Scope exports to the invoking Discord server.
replace_once(
    block([
        "    where_parts: List[str] = []",
        "    params: List[object] = []",
        "    labels: List[str] = []",
        "    if capper:",
    ]),
    block([
        "    where_parts: List[str] = []",
        "    params: List[object] = []",
        "    labels: List[str] = []",
        "    if ctx.guild is not None:",
        '        where_parts.append("guild_id = ?")',
        "        params.append(int(ctx.guild.id))",
        "    if capper:",
    ]),
    "export guild filter",
    marker='labels: List[str] = []' + newline + '    if ctx.guild is not None:',
)

# 6) Scope the data-quality report to the invoking Discord server.
replace_once(
    block([
        "    where_parts: List[str] = []",
        "    params: List[object] = []",
        "    add_time_filter(where_parts, params, start_l, end_l)",
    ]),
    block([
        "    where_parts: List[str] = []",
        "    params: List[object] = []",
        "    if ctx.guild is not None:",
        '        where_parts.append("guild_id = ?")',
        "        params.append(int(ctx.guild.id))",
        "    add_time_filter(where_parts, params, start_l, end_l)",
    ]),
    "data_issues guild filter",
    marker='params: List[object] = []' + newline + '    if ctx.guild is not None:' + newline + '        where_parts.append("guild_id = ?")' + newline + '        params.append(int(ctx.guild.id))' + newline + '    add_time_filter(where_parts, params, start_l, end_l)',
)

# 7) Keep duplicate detection isolated by guild so two communities can post identical bets.
replace_once(
    block([
        "def find_duplicate_message_id(",
        "    message_id: int,",
        "    capper_name: str,",
    ]),
    block([
        "def find_duplicate_message_id(",
        "    message_id: int,",
        "    guild_id: int,",
        "    capper_name: str,",
    ]),
    "duplicate function guild argument",
    marker="def find_duplicate_message_id(" + newline + "    message_id: int," + newline + "    guild_id: int,",
)

replace_once(
    block([
        "            FROM {table}",
        "            WHERE LOWER(capper) = ?",
        "              AND duplicate_key = ?",
    ]),
    block([
        "            FROM {table}",
        "            WHERE guild_id = ?",
        "              AND LOWER(capper) = ?",
        "              AND duplicate_key = ?",
    ]),
    "duplicate SQL guild filter",
    marker="            WHERE guild_id = ?" + newline + "              AND LOWER(capper) = ?" + newline + "              AND duplicate_key = ?",
)

replace_once(
    "            (capper_name.lower(), duplicate_key, cutoff, upper, int(message_id)),",
    "            (int(guild_id), capper_name.lower(), duplicate_key, cutoff, upper, int(message_id)),",
    "duplicate SQL parameters",
    marker="(int(guild_id), capper_name.lower(), duplicate_key, cutoff, upper, int(message_id))",
)

replace_once(
    block([
        "    duplicate_message_id = find_duplicate_message_id(",
        "        message_id,",
        "        capper.name,",
    ]),
    block([
        "    duplicate_message_id = find_duplicate_message_id(",
        "        message_id,",
        "        guild_id,",
        "        capper.name,",
    ]),
    "duplicate call guild argument",
    marker="duplicate_message_id = find_duplicate_message_id(" + newline + "        message_id," + newline + "        guild_id,",
)

# 8) If an older pending row is edited, refresh its guild_id from Discord too.
replace_once(
    block([
        "        UPDATE pending",
        "        SET capper = ?, capper_user_id = ?, author_user_id = ?, content = ?,",
    ]),
    block([
        "        UPDATE pending",
        "        SET guild_id = ?, capper = ?, capper_user_id = ?, author_user_id = ?, content = ?,",
    ]),
    "refresh_pending guild column",
    marker="SET guild_id = ?, capper = ?, capper_user_id = ?, author_user_id = ?, content = ?",
)

replace_once(
    block([
        "        (",
        "            capper.name,",
        "            int(capper.user_id),",
        "            int(message.author.id),",
        "            content,",
    ]),
    block([
        "        (",
        "            int(message.guild.id if message.guild else 0),",
        "            capper.name,",
        "            int(capper.user_id),",
        "            int(message.author.id),",
        "            content,",
    ]),
    "refresh_pending guild value",
    marker="int(message.guild.id if message.guild else 0)," + newline + "            capper.name," + newline + "            int(capper.user_id),",
)

PATH.write_bytes(text.encode("utf-8"))
print("Guild isolation edits applied successfully.")
