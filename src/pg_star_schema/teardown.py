import psycopg
from psycopg import sql

from pg_star_schema.introspect import get_columns
from pg_star_schema.naming import (
    dimension_table_name,
    fact_table_name,
    sync_delete_function_name,
    sync_delete_trigger_name,
    sync_function_name,
    sync_trigger_name,
    sync_update_function_name,
    sync_update_trigger_name,
)


def drop_trigger_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the sync trigger from the source table, if it exists.

    NOTE: Postgres errors on `drop trigger` when the table itself is gone,
    even with `if exists` - only run this while the source table exists.
    """
    return sql.SQL("drop trigger if exists {name} on {schema}.{table}").format(
        name=sql.Identifier(sync_trigger_name(table)),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
    )


def drop_update_trigger_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the update-sync trigger from the source table, if it exists.

    NOTE: same as drop_trigger_ddl - only run while the source table exists.
    """
    return sql.SQL("drop trigger if exists {name} on {schema}.{table}").format(
        name=sql.Identifier(sync_update_trigger_name(table)),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
    )


def drop_delete_trigger_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the delete-sync trigger from the source table, if it exists.

    NOTE: same as drop_trigger_ddl - only run while the source table exists.
    """
    return sql.SQL("drop trigger if exists {name} on {schema}.{table}").format(
        name=sql.Identifier(sync_delete_trigger_name(table)),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
    )


def drop_function_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the sync trigger function, if it exists."""
    return sql.SQL("drop function if exists {schema}.{name}()").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(sync_function_name(table)),
    )


def drop_update_function_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the update-sync trigger function, if it exists."""
    return sql.SQL("drop function if exists {schema}.{name}()").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(sync_update_function_name(table)),
    )


def drop_delete_function_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the delete-sync trigger function, if it exists."""
    return sql.SQL("drop function if exists {schema}.{name}()").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(sync_delete_function_name(table)),
    )


def drop_fact_table_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the fact table, if it exists.

    Drop it before the dimension tables - it holds the foreign keys.
    """
    return sql.SQL("drop table if exists {schema}.{name}").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(fact_table_name(table)),
    )


def drop_dimension_table_ddl(table: str, column: str, schema: str = "public") -> sql.Composed:
    """DDL dropping the dimension table backing `column`, if it exists."""
    return sql.SQL("drop table if exists {schema}.{name}").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(dimension_table_name(table, column)),
    )


def drop_statements(
    table: str,
    columns: list[str],
    schema: str = "public",
    source_exists: bool = True,
) -> list[sql.Composed]:
    """The statements drop_star_schema executes, in order. Pure - no database.

    Pass `source_exists=False` when the source table is gone - it skips the
    trigger drops, which Postgres errors on once the table no longer exists.
    """
    statements = []
    if source_exists:
        statements.append(drop_trigger_ddl(table, schema))
        statements.append(drop_update_trigger_ddl(table, schema))
        statements.append(drop_delete_trigger_ddl(table, schema))
    statements.append(drop_function_ddl(table, schema))
    statements.append(drop_update_function_ddl(table, schema))
    statements.append(drop_delete_function_ddl(table, schema))
    statements.append(drop_fact_table_ddl(table, schema))
    statements.extend(drop_dimension_table_ddl(table, name, schema) for name in columns)
    return statements


def drop_star_schema(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None = None,
    schema: str = "public",
) -> None:
    """Drop everything build_star_schema created for `table`.

    Removes the trigger, the sync function, the fact table, and the dimension
    tables, in dependency order, inside one transaction. Every statement is
    `if exists`, so a partially built or already torn-down star schema is fine.
    The source table itself is never touched.

    `columns` names the dimensioned columns whose dimension tables should be
    dropped; the default introspects the source table and covers every column.
    If the source table no longer exists there is nothing to introspect - pass
    `columns` explicitly in that case.
    """
    source_columns = get_columns(conn, table, schema)
    if columns is None:
        if not source_columns:
            raise ValueError(
                f"table {schema}.{table} does not exist; pass columns= to name the dimension tables to drop"
            )
        columns = [column.name for column in source_columns]

    with conn.transaction(), conn.cursor() as cur:
        for statement in drop_statements(table, columns, schema, source_exists=bool(source_columns)):
            cur.execute(statement)
