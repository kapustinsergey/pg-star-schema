from dataclasses import dataclass

import psycopg
from psycopg import sql

from pg_star_schema.introspect import get_columns
from pg_star_schema.naming import (
    dimension_table_name,
    fact_table_name,
    sync_delete_trigger_name,
    sync_trigger_name,
    sync_update_trigger_name,
)


@dataclass(frozen=True)
class TableStatus:
    name: str
    rows: int


@dataclass(frozen=True)
class StarSchemaStatus:
    fact: TableStatus | None
    dimensions: list[TableStatus]
    insert_trigger: bool
    update_trigger: bool
    delete_trigger: bool


def _like_escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


def _count_rows(conn: psycopg.Connection, name: str, schema: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("select count(*) from {schema}.{name}").format(
                schema=sql.Identifier(schema),
                name=sql.Identifier(name),
            )
        )
        return cur.fetchone()[0]


def _estimate_rows(conn: psycopg.Connection, name: str, schema: str) -> int:
    """The planner's row estimate for a table; -1 when it has none yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.reltuples::bigint
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relname = %s
            """,
            (schema, name),
        )
        row = cur.fetchone()
        return row[0] if row else -1


def _table_rows(conn: psycopg.Connection, name: str, schema: str, estimate: bool) -> int:
    if not estimate:
        return _count_rows(conn, name, schema)
    estimated = _estimate_rows(conn, name, schema)
    if estimated < 0:
        return _count_rows(conn, name, schema)
    return estimated


def _trigger_installed(conn: psycopg.Connection, table: str, name: str, schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            select 1
            from information_schema.triggers
            where trigger_schema = %s and event_object_table = %s and trigger_name = %s
            limit 1
            """,
            (schema, table, name),
        )
        return cur.fetchone() is not None


def _dimension_names(conn: psycopg.Connection, table: str, schema: str) -> list[str]:
    """The dimension tables that exist for `table`, in fact-column order.

    Every `<column>_id` column of the fact table (other than `id`) names a
    dimension; the ones whose table exists are returned. With no fact table
    to read, falls back to every `<table>_dim_*` table by name prefix.
    """
    fact_columns = get_columns(conn, fact_table_name(table), schema)
    with conn.cursor() as cur:
        if fact_columns:
            candidates = [
                dimension_table_name(table, column.name[:-3])
                for column in fact_columns
                if column.name != "id" and column.name.endswith("_id")
            ]
            cur.execute(
                "select table_name from information_schema.tables "
                "where table_schema = %s and table_name = any(%s)",
                (schema, candidates),
            )
            existing = {name for (name,) in cur.fetchall()}
            return [name for name in candidates if name in existing]
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = %s and table_name like %s order by table_name",
            (schema, f"{_like_escape(table)}\\_dim\\_%"),
        )
        return [name for (name,) in cur.fetchall()]


def star_schema_status(
    conn: psycopg.Connection,
    table: str,
    schema: str = "public",
    estimate: bool = False,
) -> StarSchemaStatus:
    """What of the star schema for `table` currently exists.

    Reports the fact table and every dimension table found, each with an
    exact `count(*)`, plus whether each sync trigger is installed. Discovery
    goes by the naming scheme, so it works whether or not the source table
    still exists: the fact table's `<column>_id` columns name the dimensions
    (through `naming.dimension_table_name`, so bounded long names are found
    too); without a fact table, any `<table>_dim_*` table left behind is
    listed instead.

    `estimate=True` reads the planner's row estimate (`pg_class.reltuples`,
    maintained by vacuum and analyze) instead of counting - instant on large
    tables, approximate. A table the planner has no estimate for yet falls
    back to the exact count.
    """
    fact_name = fact_table_name(table)
    fact = None
    if get_columns(conn, fact_name, schema):
        fact = TableStatus(name=fact_name, rows=_table_rows(conn, fact_name, schema, estimate))
    dimensions = [
        TableStatus(name=name, rows=_table_rows(conn, name, schema, estimate))
        for name in _dimension_names(conn, table, schema)
    ]

    return StarSchemaStatus(
        fact=fact,
        dimensions=dimensions,
        insert_trigger=_trigger_installed(conn, table, sync_trigger_name(table), schema),
        update_trigger=_trigger_installed(conn, table, sync_update_trigger_name(table), schema),
        delete_trigger=_trigger_installed(conn, table, sync_delete_trigger_name(table), schema),
    )
