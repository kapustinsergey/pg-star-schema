from psycopg import sql

from pg_star_schema.introspect import Column
from pg_star_schema.naming import dimension_table_name, fact_table_name, source_key_column_name


def dimension_table_ddl(table: str, column: Column, schema: str = "public") -> sql.Composed:
    """DDL to create the dimension table backing `column`, if it doesn't exist yet.

    id is the surrogate key the fact table stores; value is the distinct original value.
    """
    name = dimension_table_name(table, column.name)
    return sql.SQL(
        "create table if not exists {schema}.{name} ("
        "id bigserial primary key, "
        "value {data_type} not null unique"
        ")"
    ).format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(name),
        data_type=sql.SQL(column.data_type),
    )


def fact_table_ddl(
    table: str,
    columns: list[Column],
    schema: str = "public",
    key_columns: list[Column] | None = None,
) -> sql.Composed:
    """DDL to create the fact table, with one FK column per dimensioned column.

    Each column becomes `<column>_id bigint references <table>_dim_<column>(id)`;
    the original value never lives in the fact table, only the surrogate key.

    `key_columns` is the source table's primary key. Each key column becomes a
    `source_<column>` column, unique together, linking every fact row back to
    the source row it mirrors. `create table if not exists` never alters an
    existing fact table, so adding the key later requires a rebuild.
    """
    name = fact_table_name(table)
    parts = [
        sql.SQL("{fk_name} bigint references {schema}.{dim_name}(id)").format(
            fk_name=sql.Identifier(f"{column.name}_id"),
            schema=sql.Identifier(schema),
            dim_name=sql.Identifier(dimension_table_name(table, column.name)),
        )
        for column in columns
    ]
    for key in key_columns or []:
        parts.append(
            sql.SQL("{name} {data_type} not null").format(
                name=sql.Identifier(source_key_column_name(key.name)),
                data_type=sql.SQL(key.data_type),
            )
        )
    if key_columns:
        parts.append(
            sql.SQL("unique ({key_names})").format(
                key_names=sql.SQL(", ").join(
                    sql.Identifier(source_key_column_name(key.name)) for key in key_columns
                )
            )
        )
    return sql.SQL("create table if not exists {schema}.{name} (id bigserial primary key, {parts})").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(name),
        parts=sql.SQL(", ").join(parts),
    )


def fact_index_ddl(table: str, column: Column, schema: str = "public") -> sql.Composed:
    """DDL for the index on one dimension FK column of the fact table.

    Star queries join the fact table to a dimension and filter or group on it;
    the index lets those joins seek instead of scanning the fact table.
    """
    fact = fact_table_name(table)
    return sql.SQL("create index if not exists {index} on {schema}.{fact} ({fk_name})").format(
        index=sql.Identifier(f"{fact}_{column.name}_id_idx"),
        schema=sql.Identifier(schema),
        fact=sql.Identifier(fact),
        fk_name=sql.Identifier(f"{column.name}_id"),
    )
