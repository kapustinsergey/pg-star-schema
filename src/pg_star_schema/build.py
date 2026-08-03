import psycopg

from pg_star_schema.ddl import dimension_table_ddl, fact_index_ddl, fact_table_ddl
from pg_star_schema.introspect import Column, get_columns, get_primary_key
from pg_star_schema.trigger import (
    sync_delete_function_ddl,
    sync_delete_trigger_ddl,
    sync_function_ddl,
    sync_trigger_ddl,
)


def _resolve(all_columns: list[Column], names: list[str] | None) -> list[Column]:
    if names is None:
        return all_columns
    by_name = {column.name: column for column in all_columns}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ValueError(f"no such column(s) on the source table: {', '.join(missing)}")
    return [by_name[name] for name in names]


def build_star_schema(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None = None,
    schema: str = "public",
) -> list[Column]:
    """Create the star schema for `table` and start keeping it in sync.

    Introspects the source table, then creates one dimension table per dimensioned
    column, the fact table referencing them, and the after-insert trigger that
    mirrors each new row. Returns the columns that were dimensioned. When the
    source table has a primary key, each fact row also carries it in
    `source_<column>` columns, linking it to the source row it mirrors, and an
    after-delete trigger removes the fact row when its source row is deleted.

    `columns` selects which columns become dimensions; the default is every column,
    which is rarely what you want on a real table - a surrogate key or a timestamp
    gets one dimension row per fact row, which is pure overhead. Pass the columns
    you actually group by.

    Every statement is idempotent (`create table if not exists`, `create or replace`),
    so re-running against an existing star schema is safe. It does NOT backfill: only
    rows inserted after this runs reach the fact table.

    NOTE: requires Postgres 14 or newer, for `create or replace trigger`.
    """
    all_columns = get_columns(conn, table, schema)
    if not all_columns:
        raise ValueError(f"table {schema}.{table} does not exist or has no columns")

    dimensioned = _resolve(all_columns, columns)
    if not dimensioned:
        raise ValueError("at least one column must be dimensioned")
    by_name = {column.name: column for column in all_columns}
    key_columns = [by_name[name] for name in get_primary_key(conn, table, schema)]

    with conn.transaction(), conn.cursor() as cur:
        for column in dimensioned:
            cur.execute(dimension_table_ddl(table, column, schema))
        cur.execute(fact_table_ddl(table, dimensioned, schema, key_columns=key_columns))
        for column in dimensioned:
            cur.execute(fact_index_ddl(table, column, schema))
        cur.execute(sync_function_ddl(table, dimensioned, schema, key_columns=key_columns))
        cur.execute(sync_trigger_ddl(table, schema))
        if key_columns:
            cur.execute(sync_delete_function_ddl(table, key_columns, schema))
            cur.execute(sync_delete_trigger_ddl(table, schema))

    return dimensioned
