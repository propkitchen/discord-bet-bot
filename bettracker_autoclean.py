"""Per-server Discord command-channel retention, stored in the existing SQLite DB.

This module has no Discord dependency and never deletes betting records.
It queues only message IDs selected by the Discord event handler, and
does not backfill any historical channel messages.
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional, Tuple

MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 30
DEFAULT_RETENTION_DAYS = 3


def ensure_autoclean_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autoclean_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            retention_days INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autoclean_queue (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            delete_after_epoch INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_autoclean_due "
        "ON autoclean_queue(delete_after_epoch)"
    )
    conn.commit()


def get_retention_days(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> Optional[int]:
    row = conn.execute(
        "SELECT retention_days FROM autoclean_channels "
        "WHERE guild_id = ? AND channel_id = ?",
        (int(guild_id), int(channel_id)),
    ).fetchone()
    return int(row[0]) if row else None


def set_retention_days(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, days: int
) -> None:
    if not MIN_RETENTION_DAYS <= int(days) <= MAX_RETENTION_DAYS:
        raise ValueError("Retention must be between 1 and 30 days.")
    conn.execute(
        """
        INSERT INTO autoclean_channels (guild_id, channel_id, retention_days)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, channel_id)
        DO UPDATE SET retention_days = excluded.retention_days
        """,
        (int(guild_id), int(channel_id), int(days)),
    )
    conn.commit()


def disable_autoclean(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> int:
    """Stop cleanup and cancel queued deletions for this channel."""
    args = (int(guild_id), int(channel_id))
    with conn:
        conn.execute(
            "DELETE FROM autoclean_channels "
            "WHERE guild_id = ? AND channel_id = ?", args
        )
        removed = conn.execute(
            "DELETE FROM autoclean_queue "
            "WHERE guild_id = ? AND channel_id = ?", args
        ).rowcount
    return int(removed)


def enqueue_message(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    created_epoch: int,
) -> bool:
    """Queue a newly posted command or bot reply, only when enabled.

    Message content, authors and attachments are never persisted here.
    """
    days = get_retention_days(conn, guild_id, channel_id)
    if days is None:
        return False
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO autoclean_queue
            (guild_id, channel_id, message_id, delete_after_epoch)
            VALUES (?, ?, ?, ?)
            """,
            (
                int(guild_id), int(channel_id), int(message_id),
                int(created_epoch) + days * 86400,
            ),
        )
    return True


def due_messages(
    conn: sqlite3.Connection, now_epoch: int, limit: int = 30
) -> List[Tuple[int, int, int]]:
    """Only return queued messages in still-enabled server/channel settings."""
    rows = conn.execute(
        """
        SELECT q.guild_id, q.channel_id, q.message_id
        FROM autoclean_queue AS q
        INNER JOIN autoclean_channels AS c
          ON q.guild_id = c.guild_id AND q.channel_id = c.channel_id
        WHERE q.delete_after_epoch <= ?
        ORDER BY q.delete_after_epoch ASC
        LIMIT ?
        """,
        (int(now_epoch), max(1, min(100, int(limit)))),
    ).fetchall()
    return [(int(g), int(c), int(m)) for g, c, m in rows]


def dequeue_message(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, message_id: int
) -> None:
    with conn:
        conn.execute(
            "DELETE FROM autoclean_queue "
            "WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
            (int(guild_id), int(channel_id), int(message_id)),
        )


def defer_message(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    retry_epoch: int,
) -> None:
    """Retry later without letting one inaccessible channel starve others."""
    with conn:
        conn.execute(
            "UPDATE autoclean_queue SET delete_after_epoch = ? "
            "WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
            (int(retry_epoch), int(guild_id), int(channel_id), int(message_id)),
        )


def queued_count(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM autoclean_queue "
        "WHERE guild_id = ? AND channel_id = ?",
        (int(guild_id), int(channel_id)),
    ).fetchone()
    return int(row[0] or 0)
