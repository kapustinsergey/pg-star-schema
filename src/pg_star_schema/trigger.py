from psycopg import sql

from pg_star_schema.introspect import Column
from pg_star_schema.naming import (
    fact_table_name,
    source_key_column_name,
    sync_delete_function_name,
    sync_delete_trigger_name,
    sync_function_name,
    sync_trigger_name,
    sync_update_function_name,
    sync_update_trigger_name,
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


def sync_update_function_ddl(
    table: str,
    columns: list[Column],
    key_columns: list[Column],
    schema: str = "public",
) -> sql.Composed:
    """DDL for the plpgsql function that re-points a fact row after a source update.

    Resolves each dimensioned column of the new row to a surrogate key via the
    dimension upsert (new values get a dimension row), then updates the fact row
    matched through the old `source_<column>` key. The source key columns are
    written from the new row too, so a primary key change carries through. Pass
    the same `columns` and `key_columns` the fact table was created with.

    A source row without a fact row (inserted before the star schema existed and
    never backfilled) matches nothing and stays unmirrored. Dimension rows the
    old value pointed to stay in place.
    """
    if not key_columns:
        raise ValueError("key_columns must name the source table's primary key")
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
    assignments = [
        sql.SQL("{fk_name} = {var}").format(
            fk_name=sql.Identifier(f"{column.name}_id"),
            var=sql.Identifier(_variable_name(column)),
        )
        for column in columns
    ] + [
        sql.SQL("{source_column} = new.{key}").format(
            source_column=sql.Identifier(source_key_column_name(key.name)),
            key=sql.Identifier(key.name),
        )
        for key in key_columns
    ]
    conditions = sql.SQL(" and ").join(
        sql.SQL("{source_column} = old.{key}").format(
            source_column=sql.Identifier(source_key_column_name(key.name)),
            key=sql.Identifier(key.name),
        )
        for key in key_columns
    )
    fact_update = sql.SQL("update {schema}.{fact} set {assignments} where {conditions}").format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        assignments=sql.SQL(", ").join(assignments),
        conditions=conditions,
    )
    body = sql.SQL("declare {declarations} begin {statements}; return new; end;").format(
        declarations=sql.SQL(" ").join(declarations),
        statements=sql.SQL("; ").join([*upserts, fact_update]),
    )
    return sql.SQL("create or replace function {schema}.{name}() returns trigger language plpgsql as $$ {body} $$").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(sync_update_function_name(table)),
        body=body,
    )


def sync_update_trigger_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL binding the update-sync function to the source table.

    After-update, so the fact row is re-pointed in the same transaction as the
    source change.

    NOTE: `create or replace trigger` requires Postgres 14 or newer.
    """
    return sql.SQL(
        "create or replace trigger {trigger} after update on {schema}.{table} "
        "for each row execute function {schema}.{function}()"
    ).format(
        trigger=sql.Identifier(sync_update_trigger_name(table)),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        function=sql.Identifier(sync_update_function_name(table)),
    )


def sync_delete_function_ddl(
    table: str,
    key_columns: list[Column],
    schema: str = "public",
) -> sql.Composed:
    """DDL for the plpgsql function that removes a deleted source row's fact row.

    Matches the fact row through its `source_<column>` key, so the fact table
    must carry the source primary key - pass the same `key_columns` the fact
    table was created with. Dimension rows stay in place: other fact rows may
    reference them.
    """
    if not key_columns:
        raise ValueError("key_columns must name the source table's primary key")
    conditions = sql.SQL(" and ").join(
        sql.SQL("{source_column} = old.{key}").format(
            source_column=sql.Identifier(source_key_column_name(key.name)),
            key=sql.Identifier(key.name),
        )
        for key in key_columns
    )
    body = sql.SQL("begin delete from {schema}.{fact} where {conditions}; return old; end;").format(
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact_table_name(table)),
        conditions=conditions,
    )
    return sql.SQL("create or replace function {schema}.{name}() returns trigger language plpgsql as $$ {body} $$").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(sync_delete_function_name(table)),
        body=body,
    )


def sync_delete_trigger_ddl(table: str, schema: str = "public") -> sql.Composed:
    """DDL binding the delete-sync function to the source table.

    After-delete, so the fact row disappears in the same transaction as its
    source row.

    NOTE: `create or replace trigger` requires Postgres 14 or newer.
    """
    return sql.SQL(
        "create or replace trigger {trigger} after delete on {schema}.{table} "
        "for each row execute function {schema}.{function}()"
    ).format(
        trigger=sql.Identifier(sync_delete_trigger_name(table)),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        function=sql.Identifier(sync_delete_function_name(table)),
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
