from dataclasses import dataclass

import psycopg
from psycopg import sql

from pg_star_schema.introspect import Column, get_columns, get_primary_key
from pg_star_schema.naming import fact_table_name, source_key_column_name


@dataclass(frozen=True)
class SyncCheck:
    """How far the fact table has drifted from its source table.

    `unmirrored` counts source rows with no fact row; `orphaned` counts fact
    rows whose source row is gone.
    """

    unmirrored: int
    orphaned: int

    @property
    def in_sync(self) -> bool:
        return self.unmirrored == 0 and self.orphaned == 0


def _key_match(key_columns: list[Column]) -> sql.Composed:
    return sql.SQL(" and ").join(
        sql.SQL("f.{source_column} = s.{key}").format(
            source_column=sql.Identifier(source_key_column_name(key.name)),
            key=sql.Identifier(key.name),
        )
        for key in key_columns
    )


def unmirrored_rows_sql(table: str, key_columns: list[Column], schema: str = "public") -> sql.Composed:
    """SQL counting source rows that have no fact row.

    Rows inserted before the star schema existed and never backfilled, or
    inserted while the insert trigger was missing. Matches on the source
    primary key carried in the fact table's `source_<column>` columns.
    """
    return sql.SQL(
        "select count(*) from {schema}.{table} s "
        "where not exists (select 1 from {schema}.{fact} f where {key_match})"
    ).format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        fact=sql.Identifier(fact_table_name(table)),
        key_match=_key_match(key_columns),
    )


def orphaned_rows_sql(table: str, key_columns: list[Column], schema: str = "public") -> sql.Composed:
    """SQL counting fact rows whose source row no longer exists.

    Rows deleted from the source while the delete trigger was missing. The
    backfill never produces these: it only mirrors rows that exist.
    """
    return sql.SQL(
        "select count(*) from {schema}.{fact} f "
        "where not exists (select 1 from {schema}.{table} s where {key_match})"
    ).format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        fact=sql.Identifier(fact_table_name(table)),
        key_match=_key_match(key_columns),
    )


def check_star_schema(conn: psycopg.Connection, table: str, schema: str = "public") -> SyncCheck:
    """Compare the fact table against its source table, row by row, by primary key.

    Read-only. Both counts are exact, so on a large table this is two full
    scans - run it when drift is suspected (a trigger was dropped, a backfill
    was interrupted), not on every request. Values are not compared: a fact
    row that exists but points at a stale dimension is counted as mirrored.

    NOTE: requires a primary key on the source table - without one the fact
    rows carry no source key and there is nothing to match on. The fact table
    must exist; `status` tells you whether it does.
    """
    key_names = get_primary_key(conn, table, schema)
    if not key_names:
        raise ValueError(
            f"check requires a primary key on {schema}.{table}; "
            "without one fact rows are not linked to source rows"
        )
    by_name = {column.name: column for column in get_columns(conn, table, schema)}
    key_columns = [by_name[name] for name in key_names]

    with conn.cursor() as cur:
        cur.execute(unmirrored_rows_sql(table, key_columns, schema))
        unmirrored = cur.fetchone()[0]
        cur.execute(orphaned_rows_sql(table, key_columns, schema))
        orphaned = cur.fetchone()[0]
    return SyncCheck(unmirrored=unmirrored, orphaned=orphaned)
