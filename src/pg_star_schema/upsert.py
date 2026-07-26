from psycopg import sql

from pg_star_schema.naming import dimension_table_name


def dimension_upsert_sql(
    table: str,
    column: str,
    schema: str = "public",
    *,
    value_expr: sql.SQL | sql.Composed | None = None,
    into: str | None = None,
) -> sql.Composed:
    """SQL to resolve a value to its surrogate key in one round trip.

    Inserts the value if it's new, otherwise leaves the existing row alone; either
    way returns its id. This is what a per-row insert trigger calls once per
    dimensioned column, with the raw value as the single query parameter.

    `value_expr` overrides that query parameter with an SQL expression - inside a
    trigger body it is `new.<column>` rather than `%s`. `into` names a plpgsql
    variable to assign the returned id to, which is only valid inside a function body.
    """
    name = dimension_table_name(table, column)
    returning = sql.SQL("returning id") if into is None else sql.SQL("returning id into {var}").format(var=sql.Identifier(into))
    return sql.SQL(
        "insert into {schema}.{name} (value) values ({value_expr}) "
        "on conflict (value) do update set value = excluded.value "
        "{returning}"
    ).format(
        schema=sql.Identifier(schema),
        name=sql.Identifier(name),
        value_expr=value_expr if value_expr is not None else sql.SQL("%s"),
        returning=returning,
    )
