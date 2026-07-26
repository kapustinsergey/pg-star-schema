# pg-star-schema

Automatically build and maintain a star schema on top of existing Postgres tables.

Point it at a table you already have; it introspects the columns and (eventually) generates
the fact/dimension tables and triggers needed to keep a star schema in sync as rows are
inserted - no manual ETL.

## Status

Early, built incrementally. Currently: table introspection, fact/dimension DDL generation,
and the insert trigger that keeps the star schema in sync. Not yet wired into a single
entry point that runs all of it against a live database.

## Install

```bash
uv add pg-star-schema
```

## Usage

```python
import psycopg
from pg_star_schema import get_columns

conn = psycopg.connect("postgresql://localhost/mydb")
columns = get_columns(conn, "orders")
```
