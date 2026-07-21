from psycopg import sql

from pg_star_schema.introspect import Column
from pg_star_schema.naming import dimension_table_name, fact_table_name


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


def fact_table_ddl(table: str, columns: list[Column], schema: str = "public") -> sql.Composed:
    """DDL to create the fact table, with one FK column per dimensioned column.

    Each column becomes `<column>_id bigint references <table>_dim_<column>(id)`;
    the original value never lives in the fact table, only the surrogate key.
    """
    name = fact_table_name(table)
    fk_columns = [
        sql.SQL("{fk_name} bigint references {schema}.{dim_name}(id)").format(
            fk_name=sql.Identifier(f"{column.name}_id"),
            schema=sql.Identifier(schema),
            dim_name=sql.Identifier(dimension_table_name(table, column.name)),
        )
        for column in columns
    ]
    return sql.SQL("create table if not exists {schema}.{name} (id bigserial primary key, {fk_columns})").format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(name),
        fk_columns=sql.SQL(", ").join(fk_columns),
    )
