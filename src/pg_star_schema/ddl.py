from psycopg import sql

from pg_star_schema.introspect import Column
from pg_star_schema.naming import dimension_table_name


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
