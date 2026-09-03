from dataclasses import dataclass

import psycopg
from psycopg import sql

from pg_star_schema.naming import (
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


def star_schema_status(
    conn: psycopg.Connection,
    table: str,
    schema: str = "public",
    estimate: bool = False,
) -> StarSchemaStatus:
    """What of the star schema for `table` currently exists.

    Reports the fact table and every `<table>_dim_*` table found, each with an
    exact `count(*)`, plus whether each sync trigger is installed. Discovery
    goes by the naming scheme, so it works whether or not the source table
    still exists. NOTE: dimension tables are matched on the `<table>_dim_`
    prefix; a source table name longer than about 50 bytes pushes that prefix
    past the identifier limit (see `naming.bounded`), and its dimension tables
    are then not listed here - `drop` still finds them, from the column names.

    `estimate=True` reads the planner's row estimate (`pg_class.reltuples`,
    maintained by vacuum and analyze) instead of counting - instant on large
    tables, approximate. A table the planner has no estimate for yet falls
    back to the exact count.
    """
    fact_name = fact_table_name(table)
    with conn.cursor() as cur:
        cur.execute(
            """
            select table_name
            from information_schema.tables
            where table_schema = %s and (table_name = %s or table_name like %s)
            order by table_name
            """,
            (schema, fact_name, f"{_like_escape(table)}\\_dim\\_%"),
        )
        names = [name for (name,) in cur.fetchall()]

    fact = None
    dimensions = []
    for name in names:
        table_status = TableStatus(name=name, rows=_table_rows(conn, name, schema, estimate))
        if name == fact_name:
            fact = table_status
        else:
            dimensions.append(table_status)

    return StarSchemaStatus(
        fact=fact,
        dimensions=dimensions,
        insert_trigger=_trigger_installed(conn, table, sync_trigger_name(table), schema),
        update_trigger=_trigger_installed(conn, table, sync_update_trigger_name(table), schema),
        delete_trigger=_trigger_installed(conn, table, sync_delete_trigger_name(table), schema),
    )
