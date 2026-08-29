from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Column:
    """One source column. `data_type` is the type as DDL spells it - `format_type`
    output such as `character varying(50)`, `numeric(12,2)`, `integer[]` or an
    enum's (schema-qualified where needed) name - so it can be reused verbatim
    for the dimension table's `value` column."""

    name: str
    data_type: str
    is_nullable: bool


def resolve_columns(all_columns: list[Column], names: list[str] | None) -> list[Column]:
    """The Column values for `names`, in the given order; all of them for None.

    Raises ValueError naming the missing columns instead of a bare KeyError.
    """
    if names is None:
        return all_columns
    by_name = {column.name: column for column in all_columns}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ValueError(f"no such column(s) on the source table: {', '.join(missing)}")
    return [by_name[name] for name in names]


def get_columns(conn: psycopg.Connection, table: str, schema: str = "public") -> list[Column]:
    """Return the columns of an existing table, in ordinal position order.

    Reads pg_attribute rather than information_schema.columns: the latter's
    data_type collapses enums to `USER-DEFINED`, arrays to `ARRAY` and drops
    lengths and precisions, none of which survives being pasted into DDL.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.attname, format_type(a.atttypid, a.atttypmod), not a.attnotnull
            from pg_attribute a
            join pg_class c on c.oid = a.attrelid
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relname = %s
              and a.attnum > 0 and not a.attisdropped
            order by a.attnum
            """,
            (schema, table),
        )
        return [Column(name=name, data_type=data_type, is_nullable=nullable) for name, data_type, nullable in cur.fetchall()]


def get_primary_key(conn: psycopg.Connection, table: str, schema: str = "public") -> list[str]:
    """Return the primary key column names of an existing table, in key order.

    A composite key returns several names. Empty when the table has no primary
    key or does not exist - the caller decides whether that is an error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select kcu.column_name
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu
              on kcu.constraint_schema = tc.constraint_schema
             and kcu.constraint_name = tc.constraint_name
             and kcu.table_schema = tc.table_schema
             and kcu.table_name = tc.table_name
            where tc.table_schema = %s and tc.table_name = %s
              and tc.constraint_type = 'PRIMARY KEY'
            order by kcu.ordinal_position
            """,
            (schema, table),
        )
        return [name for (name,) in cur.fetchall()]
