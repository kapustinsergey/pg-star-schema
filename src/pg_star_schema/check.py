from dataclasses import dataclass

import psycopg
from psycopg import sql

from pg_star_schema.backfill import backfill_star_schema, dimension_backfill_sql
from pg_star_schema.introspect import Column, get_columns, get_primary_key
from pg_star_schema.naming import dimension_table_name, fact_table_name, source_key_column_name


@dataclass(frozen=True)
class SyncCheck:
    """How far the fact table has drifted from its source table.

    `unmirrored` counts source rows with no fact row; `orphaned` counts fact
    rows whose source row is gone; `stale` counts fact rows that exist but
    point at a dimension value the source row no longer has - None when the
    values were not compared (the default, see check_star_schema).
    """

    unmirrored: int
    orphaned: int
    stale: int | None = None

    @property
    def in_sync(self) -> bool:
        return self.unmirrored == 0 and self.orphaned == 0 and not self.stale


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


def _dimension_joins(table: str, columns: list[Column], schema: str, on_id: bool) -> sql.Composed:
    """One left join per dimension, aliased `d_<column>` - on the fact row's
    surrogate key (`on_id`) or on the source row's current value."""
    return sql.SQL(" ").join(
        sql.SQL("left join {schema}.{dim} {alias} on {condition}").format(
            schema=sql.Identifier(schema),
            dim=sql.Identifier(dimension_table_name(table, column.name)),
            alias=sql.Identifier(f"d_{column.name}"),
            condition=(
                sql.SQL("{alias}.id = f.{fk}") if on_id else sql.SQL("{alias}.value = s.{column}")
            ).format(
                alias=sql.Identifier(f"d_{column.name}"),
                fk=sql.Identifier(f"{column.name}_id"),
                column=sql.Identifier(column.name),
            ),
        )
        for column in columns
    )


def stale_rows_sql(
    table: str,
    columns: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> sql.Composed:
    """SQL counting fact rows whose dimension pointers no longer match their
    source row's values, for the dimensioned `columns`.

    Rows updated in the source while the update trigger was missing. Each
    fact row is joined to its source row by primary key and each `<column>_id`
    is followed to the dimension value it points at; a row counts once when
    any of those values `is distinct from` the source's current value - so a
    NULL pointer against a NULL source value is in sync, and a NULL pointer
    against a value is not.
    """
    return sql.SQL(
        "select count(*) from {schema}.{fact} f "
        "join {schema}.{table} s on {key_match} {joins} where {mismatch}"
    ).format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        table=sql.Identifier(table),
        key_match=_key_match(key_columns),
        joins=_dimension_joins(table, columns, schema, on_id=True),
        mismatch=sql.SQL(" or ").join(
            sql.SQL("{alias}.value is distinct from s.{column}").format(
                alias=sql.Identifier(f"d_{column.name}"),
                column=sql.Identifier(column.name),
            )
            for column in columns
        ),
    )


def stale_rows_update_sql(
    table: str,
    columns: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> sql.Composed:
    """SQL re-pointing the stale fact rows at the dimension rows for their
    source row's current values - what the update trigger would have done.

    Looks each current value up in its dimension, so run the dimension
    backfill first: a value that has no dimension row yet would otherwise
    become a NULL pointer. Only rows where some pointer differs are written.
    """
    return sql.SQL(
        "update {schema}.{fact} f set {assignments} from {schema}.{table} s {joins} "
        "where {key_match} and ({changed})"
    ).format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        table=sql.Identifier(table),
        assignments=sql.SQL(", ").join(
            sql.SQL("{fk} = {alias}.id").format(
                fk=sql.Identifier(f"{column.name}_id"),
                alias=sql.Identifier(f"d_{column.name}"),
            )
            for column in columns
        ),
        joins=_dimension_joins(table, columns, schema, on_id=False),
        key_match=_key_match(key_columns),
        changed=sql.SQL(" or ").join(
            sql.SQL("f.{fk} is distinct from {alias}.id").format(
                fk=sql.Identifier(f"{column.name}_id"),
                alias=sql.Identifier(f"d_{column.name}"),
            )
            for column in columns
        ),
    )


def dimensioned_columns(conn: psycopg.Connection, table: str, schema: str = "public") -> list[Column]:
    """The source columns the existing star schema dimensions, read from the
    fact table: every `<column>_id` column whose `<column>` is a source column.
    Empty when there is no fact table."""
    source = {column.name: column for column in get_columns(conn, table, schema)}
    dimensioned = []
    for fact_column in get_columns(conn, fact_table_name(table), schema):
        name = fact_column.name
        if name != "id" and name.endswith("_id") and name[:-3] in source:
            dimensioned.append(source[name[:-3]])
    return dimensioned


def _key_columns(conn: psycopg.Connection, table: str, schema: str, command: str) -> list[Column]:
    key_names = get_primary_key(conn, table, schema)
    if not key_names:
        raise ValueError(
            f"{command} requires a primary key on {schema}.{table}; "
            "without one fact rows are not linked to source rows"
        )
    by_name = {column.name: column for column in get_columns(conn, table, schema)}
    return [by_name[name] for name in key_names]


def check_star_schema(
    conn: psycopg.Connection,
    table: str,
    schema: str = "public",
    values: bool = False,
) -> SyncCheck:
    """Compare the fact table against its source table, row by row, by primary key.

    Read-only. Both counts are exact, so on a large table this is two full
    scans - run it when drift is suspected (a trigger was dropped, a backfill
    was interrupted), not on every request. By default values are not
    compared: a fact row that exists but points at a stale dimension is
    counted as mirrored. `values=True` adds that comparison (`stale` on the
    result) for every column the star schema dimensions - a third pass that
    joins every dimension, so it costs the most of the three.

    NOTE: requires a primary key on the source table - without one the fact
    rows carry no source key and there is nothing to match on. The fact table
    must exist; `status` tells you whether it does.
    """
    key_columns = _key_columns(conn, table, schema, "check")
    with conn.cursor() as cur:
        cur.execute(unmirrored_rows_sql(table, key_columns, schema))
        unmirrored = cur.fetchone()[0]
        cur.execute(orphaned_rows_sql(table, key_columns, schema))
        orphaned = cur.fetchone()[0]
        stale = None
        if values:
            columns = dimensioned_columns(conn, table, schema)
            stale = 0
            if columns:
                cur.execute(stale_rows_sql(table, columns, key_columns, schema))
                stale = cur.fetchone()[0]
    return SyncCheck(unmirrored=unmirrored, orphaned=orphaned, stale=stale)


def orphaned_rows_delete_sql(table: str, key_columns: list[Column], schema: str = "public") -> sql.Composed:
    """SQL removing the fact rows whose source row no longer exists.

    The delete counterpart of orphaned_rows_sql - what the delete trigger
    would have done had it been installed when the source rows went away.
    Dimension rows stay in place, as everywhere else.
    """
    return sql.SQL(
        "delete from {schema}.{fact} f "
        "where not exists (select 1 from {schema}.{table} s where {key_match})"
    ).format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        fact=sql.Identifier(fact_table_name(table)),
        key_match=_key_match(key_columns),
    )


def stale_rows_repair_statements(
    table: str,
    columns: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> list[sql.Composed]:
    """The statements that re-point stale fact rows, in order: every
    dimension's backfill (so each current source value has a row), then the
    update. Pure - no database."""
    statements = [dimension_backfill_sql(table, column.name, schema) for column in columns]
    statements.append(stale_rows_update_sql(table, columns, key_columns, schema))
    return statements


def repair_star_schema(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None = None,
    schema: str = "public",
    values: bool = False,
) -> SyncCheck:
    """Bring a drifted star schema back in line with its source table.

    Measures the drift the way check_star_schema does, then mirrors the
    unmirrored source rows (the backfill, which skips rows already present)
    and deletes the orphaned fact rows - all in one transaction, so the fact
    table moves from one consistent state to another. Returns the drift as it
    was measured, i.e. what the repair fixed; `in_sync` on the result means
    there was nothing to do.

    `values=True` also compares the dimension values (check_star_schema's
    `stale`) and re-points the stale fact rows at the rows for their source
    row's current values, adding those values to the dimensions first.

    Pass the same `columns` selection the star schema was built with - the
    backfill fills dimensions for it; the value comparison goes by the
    dimensions that exist. Requires a primary key on the source table, same as
    check_star_schema.
    """
    with conn.transaction():
        drift = check_star_schema(conn, table, schema, values=values)
        if drift.unmirrored:
            backfill_star_schema(conn, table, columns, schema)
        if drift.orphaned or drift.stale:
            key_columns = _key_columns(conn, table, schema, "repair")
            with conn.cursor() as cur:
                if drift.orphaned:
                    cur.execute(orphaned_rows_delete_sql(table, key_columns, schema))
                if drift.stale:
                    dimensioned = dimensioned_columns(conn, table, schema)
                    for statement in stale_rows_repair_statements(table, dimensioned, key_columns, schema):
                        cur.execute(statement)
    return drift
