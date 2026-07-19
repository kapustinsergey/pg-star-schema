from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    is_nullable: bool


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
