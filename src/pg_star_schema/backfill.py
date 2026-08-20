from collections.abc import Callable

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


def fact_backfill_batch_sql(
    table: str,
    columns: list[Column],
    key_columns: list[Column],
    schema: str = "public",
    after: bool = False,
) -> sql.Composed:
    """SQL mirroring one key-ordered batch of source rows into the fact table.

    Walks the primary key in row-value order, so composite keys work the same
    as single-column ones. Parametrized: execute with `(batch_size,)` when
    `after` is false (the first batch), or `(*last_key, batch_size)` when it
    is true (only rows whose key row-value is greater than the previous
    batch's last key). Returns one row per non-empty batch - the last row's
    key values followed by the batch row count - and no row when the batch is
    empty. Rows already mirrored - by the sync trigger or an earlier run - are
    skipped via `on conflict do nothing` on the source key.
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
    key_aliases = [sql.Identifier(f"sk_{i}") for i in range(len(key_columns))]
    key_refs = [sql.SQL("s.{key}").format(key=sql.Identifier(key.name)) for key in key_columns]
    batch_exprs = [
        sql.SQL("{alias}.id as {out}").format(
            alias=sql.Identifier(f"d_{column.name}"),
            out=sql.Identifier(f"{column.name}_id"),
        )
        for column in columns
    ]
    batch_exprs.extend(
        sql.SQL("{ref} as {alias}").format(ref=ref, alias=alias)
        for ref, alias in zip(key_refs, key_aliases)
    )
    insert_columns = [sql.Identifier(f"{column.name}_id") for column in columns]
    insert_columns.extend(sql.Identifier(source_key_column_name(key.name)) for key in key_columns)
    batch_columns: list[sql.Composable] = [sql.Identifier(f"{column.name}_id") for column in columns]
    batch_columns.extend(key_aliases)
    where = sql.SQL("")
    if after:
        where = sql.SQL("where ({key_refs}) > ({placeholders}) ").format(
            key_refs=sql.SQL(", ").join(key_refs),
            placeholders=sql.SQL(", ").join(sql.SQL("%s") for _ in key_columns),
        )
    return sql.SQL(
        "with batch as ("
        "select {batch_exprs} from {schema}.{table} s {joins} "
        "{where}order by {key_refs} limit %s"
        "), mirrored as ("
        "insert into {schema}.{fact} ({insert_columns}) "
        "select {batch_columns} from batch "
        "on conflict ({source_keys}) do nothing"
        ") select {key_aliases}, (select count(*) from batch) from batch "
        "order by {desc_aliases} limit 1"
    ).format(
        batch_exprs=sql.SQL(", ").join(batch_exprs),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        joins=sql.SQL(" ").join(joins),
        where=where,
        key_refs=sql.SQL(", ").join(key_refs),
        fact=sql.Identifier(fact_table_name(table)),
        insert_columns=sql.SQL(", ").join(insert_columns),
        batch_columns=sql.SQL(", ").join(batch_columns),
        source_keys=sql.SQL(", ").join(
            sql.Identifier(source_key_column_name(key.name)) for key in key_columns
        ),
        key_aliases=sql.SQL(", ").join(key_aliases),
        desc_aliases=sql.SQL(", ").join(
            sql.SQL("{alias} desc").format(alias=alias) for alias in key_aliases
        ),
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


def backfill_star_schema_batched(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None = None,
    schema: str = "public",
    batch_size: int = 10_000,
    on_batch: Callable[[int, int], None] | None = None,
) -> int:
    """Backfill like backfill_star_schema, but mirror the fact rows in
    key-ordered batches, committing after each one.

    Meant for large source tables: no single long transaction, and because
    each batch is `on conflict do nothing` on the source key, an interrupted
    run can simply be re-run - already-mirrored rows are skipped and the walk
    continues to the end. Returns the number of source rows processed.

    `on_batch`, when given, is called after each committed batch with
    `(rows_in_batch, total_rows_so_far)` - progress reporting for runs long
    enough to want it.

    NOTE: requires a primary key on the source table (composite keys are
    walked in row-value order). Without one use backfill_star_schema.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    all_columns = get_columns(conn, table, schema)
    by_name = {column.name: column for column in all_columns}
    dimensioned = all_columns if columns is None else [by_name[name] for name in columns]
    key_names = get_primary_key(conn, table, schema)
    if not key_names:
        raise ValueError(
            f"batched backfill requires a primary key on {schema}.{table}; "
            "use backfill_star_schema instead"
        )
    key_columns = [by_name[name] for name in key_names]

    with conn.transaction(), conn.cursor() as cur:
        for column in dimensioned:
            cur.execute(dimension_backfill_sql(table, column.name, schema))

    processed = 0
    last_key: tuple | None = None
    while True:
        statement = fact_backfill_batch_sql(table, dimensioned, key_columns, schema, after=last_key is not None)
        params = (batch_size,) if last_key is None else (*last_key, batch_size)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(statement, params)
            row = cur.fetchone()
        if row is None:
            return processed
        *last_key_values, batch_rows = row
        last_key = tuple(last_key_values)
        processed += batch_rows
        if on_batch is not None:
            on_batch(batch_rows, processed)
        if batch_rows < batch_size:
            return processed
