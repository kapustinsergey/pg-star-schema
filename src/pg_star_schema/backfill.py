import psycopg
from psycopg import sql

from pg_star_schema.introspect import Column, get_columns
from pg_star_schema.naming import dimension_table_name, fact_table_name


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


def fact_backfill_sql(table: str, columns: list[Column], schema: str = "public") -> sql.Composed:
    """SQL mirroring every existing source row into the fact table.

    Left-joins each dimension on the source value, so a NULL source value
    becomes a NULL surrogate key - same rows the insert trigger would have
    produced had it been active from the start.

    NOTE: the fact table has no natural key, so running this twice duplicates
    rows. Call it exactly once, right after build_star_schema.
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
    return sql.SQL("insert into {schema}.{fact} ({fk_columns}) select {ids} from {schema}.{table} s {joins}").format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        fk_columns=sql.SQL(", ").join(sql.Identifier(f"{column.name}_id") for column in columns),
        ids=sql.SQL(", ").join(
            sql.SQL("{alias}.id").format(alias=sql.Identifier(f"d_{column.name}")) for column in columns
        ),
        table=sql.Identifier(table),
        joins=sql.SQL(" ").join(joins),
    )


def backfill_star_schema(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None = None,
    schema: str = "public",
) -> None:
    """Mirror the source table's existing rows into an already-built star schema.

    Pass the same `columns` selection that was given to build_star_schema.
    Runs in one transaction: dimensions first, then the fact rows. One-shot -
    see fact_backfill_sql for why running it twice duplicates fact rows.
    """
    all_columns = get_columns(conn, table, schema)
    if columns is None:
        dimensioned = all_columns
    else:
        by_name = {column.name: column for column in all_columns}
        dimensioned = [by_name[name] for name in columns]

    with conn.transaction(), conn.cursor() as cur:
        for column in dimensioned:
            cur.execute(dimension_backfill_sql(table, column.name, schema))
        cur.execute(fact_backfill_sql(table, dimensioned, schema))
