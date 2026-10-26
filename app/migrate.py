"""Lightweight schema migrations.

SQLAlchemy's `create_all` only creates missing tables; it never alters an
existing one. This module adds columns that were introduced after the first
release, so an existing deployment can be upgraded in place.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

# table -> {column: DDL type}
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "accounts": {
        "frozen_balance": "FLOAT DEFAULT 0.0",
        # Existing rows get 0. An old proxy pin could have been automatic or
        # manual, and there is no way to tell after the fact, so treat it as
        # manual: never clear it silently. The account row lets the operator
        # switch it to direct egress explicitly.
        "proxy_auto_assigned": "BOOLEAN DEFAULT 0",
        "user_agent": "VARCHAR(512)",
    },
    "video_jobs": {
        "input_reference_end_url": "TEXT",
    },
}


def _columns_sync(connection, table: str) -> set[str]:
    """Column names of a table, read with a plain DBAPI cursor."""
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table})")
    return {row[1] for row in rows}


def _table_names_sync(connection) -> set[str]:
    rows = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {row[0] for row in rows}


async def run_migrations(conn: AsyncConnection) -> list[str]:
    """Add any missing columns. Returns a log of what was applied."""
    applied: list[str] = []

    def apply(connection) -> list[str]:
        done: list[str] = []
        tables = _table_names_sync(connection)
        for table, columns in ADDED_COLUMNS.items():
            if table not in tables:
                continue
            present = _columns_sync(connection, table)
            for column, ddl in columns.items():
                if column in present:
                    continue
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
                done.append(f"{table}.{column}")
        return done

    applied = await conn.run_sync(apply)
    return applied
