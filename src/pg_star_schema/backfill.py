import psycopg
from psycopg import sql

from pg_star_schema.introspect import Column, get_columns, get_primary_key
from pg_star_schema.naming import dimension_table_name, fact_table_name, source_key_column_name


def dimension_backfill_sql(table: str, column: str, schema: str = "public") -> sql.Composed:
    """SQL filling the dimension with every distinct existing value of `column`.

    NULLs are skipped - a NULL has no dimension row, and the fact backfill
    carries it through as a NULL foreign key instead.
    """
    return sql.SQL(
        "insert into {schema}.{dim} (value) "
        "select distinct {column} from {schema}.{table} where {column} is not null "
        "on conflict (value) do nothing"
    ).format(
        schema=sql.Identifier(schema),
        dim=sql.Identifier(dimension_table_name(table, column)),
        table=sql.Identifier(table),
        column=sql.Identifier(column),
    )


def fact_backfill_sql(
    table: str,
    columns: list[Column],
    schema: str = "public",
    key_columns: list[Column] | None = None,
) -> sql.Composed:
    """SQL mirroring every existing source row into the fact table.

    Left-joins each dimension on the source value, so a NULL source value
    becomes a NULL surrogate key - same rows the insert trigger would have
    produced had it been active from the start.

    `key_columns` is the source table's primary key; when given, each fact row
    also stores it in the `source_<column>` columns and the insert becomes
    `on conflict do nothing` on that key - re-running skips rows already
    mirrored instead of duplicating them.

    NOTE: without `key_columns` the fact table has no natural key, so running
    this twice duplicates rows - call it exactly once, right after
    build_star_schema.
    """
    joins = [
        sql.SQL("left join {schema}.{dim} {alias} on {alias}.value = s.{column}").format(
            schema=sql.Identifier(schema),
            dim=sql.Identifier(dimension_table_name(table, column.name)),
            alias=sql.Identifier(f"d_{column.name}"),
            column=sql.Identifier(column.name),
        )
        for column in columns
    ]
    insert_columns = [sql.Identifier(f"{column.name}_id") for column in columns]
    select_exprs: list[sql.Composable] = [
        sql.SQL("{alias}.id").format(alias=sql.Identifier(f"d_{column.name}")) for column in columns
    ]
    for key in key_columns or []:
        insert_columns.append(sql.Identifier(source_key_column_name(key.name)))
        select_exprs.append(sql.SQL("s.{column}").format(column=sql.Identifier(key.name)))
    conflict = sql.SQL("")
    if key_columns:
        conflict = sql.SQL(" on conflict ({key_names}) do nothing").format(
            key_names=sql.SQL(", ").join(
                sql.Identifier(source_key_column_name(key.name)) for key in key_columns
            )
        )
    return sql.SQL(
        "insert into {schema}.{fact} ({insert_columns}) select {select_exprs} from {schema}.{table} s {joins}{conflict}"
    ).format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        insert_columns=sql.SQL(", ").join(insert_columns),
        select_exprs=sql.SQL(", ").join(select_exprs),
        table=sql.Identifier(table),
        joins=sql.SQL(" ").join(joins),
        conflict=conflict,
    )


def backfill_statements(
    table: str,
    dimensioned: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> list[sql.Composed]:
    """The statements backfill_star_schema executes, in order. Pure - no database."""
    statements = [dimension_backfill_sql(table, column.name, schema) for column in dimensioned]
    statements.append(fact_backfill_sql(table, dimensioned, schema, key_columns=key_columns))
    return statements


def backfill_star_schema(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None = None,
    schema: str = "public",
) -> None:
    """Mirror the source table's existing rows into an already-built star schema.

    Pass the same `columns` selection that was given to build_star_schema.
    Runs in one transaction: dimensions first, then the fact rows. When the
    source table has a primary key this is idempotent - already-mirrored rows
    are skipped; without one it is one-shot, see fact_backfill_sql.
    """
    all_columns = get_columns(conn, table, schema)
    by_name = {column.name: column for column in all_columns}
    if columns is None:
        dimensioned = all_columns
    else:
        dimensioned = [by_name[name] for name in columns]
    key_columns = [by_name[name] for name in get_primary_key(conn, table, schema)]

    with conn.transaction(), conn.cursor() as cur:
        for statement in backfill_statements(table, dimensioned, key_columns, schema):
            cur.execute(statement)
