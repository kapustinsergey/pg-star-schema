"""Command line interface: build, backfill, or drop a star schema.

The connection string comes from `--dsn`; when it is empty (the default),
psycopg falls back to the libpq `PG*` environment variables (`PGHOST`,
`PGDATABASE`, `PGUSER`, `PGPASSWORD`, ...). `--dry-run` connects - the plan
comes from introspecting the source table - but only prints the SQL and
executes nothing.
"""

import argparse
import sys

import psycopg
from psycopg import sql

from pg_star_schema.backfill import backfill_star_schema, backfill_statements
from pg_star_schema.build import build_star_schema, build_statements
from pg_star_schema.introspect import Column, get_columns, get_primary_key
from pg_star_schema.teardown import drop_star_schema, drop_statements


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-star-schema",
        description="Build and maintain a star schema on top of an existing Postgres table.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {
        "build": "create the dimension tables, fact table, and sync triggers",
        "backfill": "mirror the source table's existing rows into the star schema",
        "drop": "remove everything build created; the source table is never touched",
    }
    for name, help_text in commands.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        sub.add_argument("table", help="source table name")
        sub.add_argument(
            "columns",
            nargs="*",
            help="columns to dimension; default: every column (build docs explain why to narrow this)",
        )
        sub.add_argument("--dsn", default="", help="libpq connection string; empty uses PG* environment variables")
        sub.add_argument("--schema", default="public", help="schema of the source table (default: public)")
        sub.add_argument("--dry-run", action="store_true", help="print the SQL without executing anything")
    return parser


def _introspect(
    conn: psycopg.Connection,
    table: str,
    columns: list[str] | None,
    schema: str,
) -> tuple[list[Column], list[Column]]:
    """The (dimensioned, key) columns a command would operate on."""
    all_columns = get_columns(conn, table, schema)
    if not all_columns:
        raise ValueError(f"table {schema}.{table} does not exist or has no columns")
    by_name = {column.name: column for column in all_columns}
    if columns:
        missing = [name for name in columns if name not in by_name]
        if missing:
            raise ValueError(f"no such column(s) on the source table: {', '.join(missing)}")
    dimensioned = [by_name[name] for name in columns] if columns else all_columns
    key_columns = [by_name[name] for name in get_primary_key(conn, table, schema)]
    return dimensioned, key_columns


def _plan(
    conn: psycopg.Connection,
    command: str,
    table: str,
    columns: list[str] | None,
    schema: str,
) -> list[sql.Composed]:
    if command == "drop":
        source_columns = get_columns(conn, table, schema)
        if columns is None:
            if not source_columns:
                raise ValueError(
                    f"table {schema}.{table} does not exist; pass the dimensioned columns to name the tables to drop"
                )
            columns = [column.name for column in source_columns]
        return drop_statements(table, columns, schema, source_exists=bool(source_columns))
    dimensioned, key_columns = _introspect(conn, table, columns, schema)
    if command == "build":
        return build_statements(table, dimensioned, key_columns, schema)
    return backfill_statements(table, dimensioned, key_columns, schema)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    columns = args.columns or None
    try:
        with psycopg.connect(args.dsn) as conn:
            if args.dry_run:
                for statement in _plan(conn, args.command, args.table, columns, args.schema):
                    print(statement.as_string(conn) + ";")
                return 0
            if args.command == "build":
                dimensioned = build_star_schema(conn, args.table, columns, args.schema)
                names = ", ".join(column.name for column in dimensioned)
                print(f"built star schema for {args.schema}.{args.table}; dimensions: {names}")
            elif args.command == "backfill":
                backfill_star_schema(conn, args.table, columns, args.schema)
                print(f"backfilled {args.schema}.{args.table}")
            else:
                drop_star_schema(conn, args.table, columns, args.schema)
                print(f"dropped star schema for {args.schema}.{args.table}")
    except (psycopg.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
