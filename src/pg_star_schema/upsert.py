from psycopg import sql

from pg_star_schema.naming import dimension_table_name


def dimension_upsert_sql(table: str, column: str, schema: str = "public") -> sql.Composed:
    """SQL to resolve a value to its surrogate key in one round trip.

    Inserts the value if it's new, otherwise leaves the existing row alone; either
    way returns its id. This is what a per-row insert trigger will call once per
    dimensioned column, with the raw value as the single query parameter.
    """
    name = dimension_table_name(table, column)
    return sql.SQL(
        "insert into {schema}.{name} (value) values (%s) "
        "on conflict (value) do update set value = excluded.value "
        "returning id"
    ).format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(name),
    )
