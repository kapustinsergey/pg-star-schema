import psycopg
from psycopg import sql

from pg_star_schema.ddl import dimension_table_ddl, fact_index_ddl, fact_table_ddl
from pg_star_schema.introspect import Column, get_columns, get_primary_key, resolve_columns
from pg_star_schema.naming import dimension_table_name, fact_table_name, source_key_column_name
from pg_star_schema.trigger import (
    sync_delete_function_ddl,
    sync_delete_trigger_ddl,
    sync_function_ddl,
    sync_trigger_ddl,
    sync_update_function_ddl,
    sync_update_trigger_ddl,
)


def build_statements(
    table: str,
    dimensioned: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> list[sql.Composed]:
    """The statements build_star_schema executes, in order. Pure - no database.

    Introspect `dimensioned` and `key_columns` yourself (get_columns,
    get_primary_key) or construct the Column values directly.
    """
    statements = [dimension_table_ddl(table, column, schema) for column in dimensioned]
    statements.append(fact_table_ddl(table, dimensioned, schema, key_columns=key_columns))
    statements.extend(fact_index_ddl(table, column, schema) for column in dimensioned)
    statements.append(sync_function_ddl(table, dimensioned, schema, key_columns=key_columns))
    statements.append(sync_trigger_ddl(table, schema))
    if key_columns:
        statements.append(sync_update_function_ddl(table, dimensioned, key_columns, schema))
        statements.append(sync_update_trigger_ddl(table, schema))
        statements.append(sync_delete_function_ddl(table, key_columns, schema))
        statements.append(sync_delete_trigger_ddl(table, schema))
    return statements


def configuration_drift(
    conn: psycopg.Connection,
    table: str,
    dimensioned: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> list[str]:
    """How the star schema already in the database differs from the one a
    build with these columns would create. Empty when they agree, or when no
    star schema exists yet.

    `create table if not exists` leaves an existing fact or dimension table
    exactly as it is, so a build with a different column set, a different
    source primary key, or changed source types would silently keep the old
    tables while installing triggers written for the new layout. This is the
    check that turns that into an error. It compares the fact table's columns
    (one `<column>_id` per dimension, one `source_<key>` per key column, with
    the key's type) and each dimension table's `value` type against the
    source table as it is now.
    """
    drift = []
    fact_columns = {column.name: column for column in get_columns(conn, fact_table_name(table), schema)}
    if fact_columns:
        expected = {"id": "bigint"}
        expected.update({f"{column.name}_id": "bigint" for column in dimensioned})
        expected.update({source_key_column_name(key.name): key.data_type for key in key_columns})
        missing = sorted(set(expected) - set(fact_columns))
        extra = sorted(set(fact_columns) - set(expected))
        if missing:
            drift.append(f"fact table lacks column(s) {', '.join(missing)}")
        if extra:
            drift.append(f"fact table has extra column(s) {', '.join(extra)}")
        for name, data_type in expected.items():
            existing = fact_columns.get(name)
            if existing is not None and existing.data_type != data_type:
                drift.append(f"fact column {name} is {existing.data_type}, source now needs {data_type}")
    for column in dimensioned:
        dimension = dimension_table_name(table, column.name)
        value = next((c for c in get_columns(conn, dimension, schema) if c.name == "value"), None)
        if value is not None and value.data_type != column.data_type:
            drift.append(f"{dimension}.value is {value.data_type}, source column is {column.data_type}")
    return drift


def check_configuration(
    conn: psycopg.Connection,
    table: str,
    dimensioned: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> None:
    """Raise ValueError, naming every difference, when `configuration_drift`
    finds any - with the way out: drop the star schema and build it again."""
    drift = configuration_drift(conn, table, dimensioned, key_columns, schema)
    if drift:
        raise ValueError(
            f"star schema for {schema}.{table} already exists with a different configuration: "
            + "; ".join(drift)
            + ". Drop it (drop_star_schema / pg-star-schema drop) and build again"
        )


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
    `source_<column>` columns, linking it to the source row it mirrors; an
    after-update trigger re-points the fact row when its source row changes and
    an after-delete trigger removes it when the source row is deleted.

    `columns` selects which columns become dimensions; the default is every column,
    which is rarely what you want on a real table - a surrogate key or a timestamp
    gets one dimension row per fact row, which is pure overhead. Pass the columns
    you actually group by.

    Re-running with the same columns is a no-op (`create table if not exists`,
    `create or replace`). Re-running with a different set of columns, a different
    source primary key, or changed source column types stops with a ValueError
    naming the difference - `create table if not exists` would keep the old
    tables under triggers written for the new layout - and the way out is
    `drop_star_schema` followed by a fresh build. It does NOT backfill: only rows
    inserted after this runs reach the fact table.

    NOTE: requires Postgres 14 or newer, for `create or replace trigger`.
    """
    all_columns = get_columns(conn, table, schema)
    if not all_columns:
        raise ValueError(f"table {schema}.{table} does not exist or has no columns")

    dimensioned = resolve_columns(all_columns, columns)
    if not dimensioned:
        raise ValueError("at least one column must be dimensioned")
    by_name = {column.name: column for column in all_columns}
    key_columns = [by_name[name] for name in get_primary_key(conn, table, schema)]
    check_configuration(conn, table, dimensioned, key_columns, schema)

    with conn.transaction(), conn.cursor() as cur:
        for statement in build_statements(table, dimensioned, key_columns, schema):
            cur.execute(statement)

    return dimensioned
