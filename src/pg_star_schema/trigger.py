from psycopg import sql

from pg_star_schema.introspect import Column
from pg_star_schema.naming import (
    fact_table_name,
    source_key_column_name,
    sync_function_name,
    sync_trigger_name,
)
from pg_star_schema.upsert import dimension_upsert_sql


def _variable_name(column: Column) -> str:
    return f"v_{column.name}_id"


def sync_function_ddl(
    table: str,
    columns: list[Column],
    schema: str = "public",
    key_columns: list[Column] | None = None,
) -> sql.Composed:
    """DDL for the plpgsql function that mirrors one source row into the star schema.

    For each dimensioned column it resolves `new.<column>` to a surrogate key via the
    dimension upsert, then writes a single fact row holding only those keys. Declared
    variables are prefixed `v_` so they can't be captured by a same-named column.

    `key_columns` is the source table's primary key; when given, the fact row also
    stores it in the `source_<column>` columns. Pass the same value the fact table
    was created with.
    """
    declarations = [
        sql.SQL("{var} bigint;").format(var=sql.Identifier(_variable_name(column)))
        for column in columns
    ]
    upserts = [
        dimension_upsert_sql(
            table,
            column.name,
            schema,
            value_expr=sql.SQL("new.{column}").format(column=sql.Identifier(column.name)),
            into=_variable_name(column),
        )
        for column in columns
    ]
    insert_columns = [sql.Identifier(f"{column.name}_id") for column in columns]
    insert_values: list[sql.Composable] = [sql.Identifier(_variable_name(column)) for column in columns]
    for key in key_columns or []:
        insert_columns.append(sql.Identifier(source_key_column_name(key.name)))
        insert_values.append(sql.SQL("new.{column}").format(column=sql.Identifier(key.name)))
    fact_insert = sql.SQL("insert into {schema}.{fact} ({insert_columns}) values ({insert_values})").format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        insert_columns=sql.SQL(", ").join(insert_columns),
        insert_values=sql.SQL(", ").join(insert_values),
    )
    body = sql.SQL("declare {declarations} begin {statements}; return new; end;").format(
        declarations=sql.SQL(" ").join(declarations),
        statements=sql.SQL("; ").join([*upserts, fact_insert]),
    )
    return sql.SQL("create or replace function {schema}.{name}() returns trigger language plpgsql as $$ {body} $$").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(sync_function_name(table)),
        body=body,
    )


def sync_trigger_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL binding the sync function to the source table, one call per inserted row.

    After-insert, so the source row is already durable before the fact row is written
    and a failure here rolls both back together.

    NOTE: `create or replace trigger` requires Postgres 14 or newer.
    """
    return sql.SQL(
        "create or replace trigger {trigger} after insert on {schema}.{table} "
        "for each row execute function {schema}.{function}()"
    ).format(
        trigger=sql.Identifier(sync_trigger_name(table)),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        function=sql.Identifier(sync_function_name(table)),
    )
