from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Column:
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
    """Return the columns of an existing table, in ordinal position order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select column_name, data_type, is_nullable = 'YES'
            from information_schema.columns
            where table_schema = %s and table_name = %s
            order by ordinal_position
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
