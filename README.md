# pg-star-schema

Automatically build and maintain a star schema on top of existing Postgres tables.

Point it at a table you already have; it introspects the columns and generates the
fact/dimension tables and the triggers needed to keep a star schema in sync as rows are
inserted, updated and deleted - no manual ETL.

It turns an operational Postgres table into a dimensional model for analytics and OLAP
queries in place: the fact table, one dimension table per chosen column, surrogate keys,
indexes, and the sync triggers, all inside the same database - a small data warehouse
without moving the data anywhere. Python 3.11+, psycopg 3, plain SQL underneath.

## Status

Early, built incrementally. `build_star_schema` runs end to end against a live database,
`backfill_star_schema` mirrors the rows that existed before it ran,
`drop_star_schema` removes everything again, `star_schema_status` reports what
exists, and `check_star_schema` counts the rows the fact table and the source table
disagree on. All of it is also available as a CLI.

Requires Postgres 14 or newer.

## Install

```bash
uv add pg-star-schema
```

## Usage

```python
import psycopg
from pg_star_schema import backfill_star_schema, build_star_schema

conn = psycopg.connect("postgresql://localhost/mydb")
build_star_schema(conn, "orders", columns=["customer", "status", "country"])
backfill_star_schema(conn, "orders", columns=["customer", "status", "country"])
```

`backfill_star_schema` fills the dimensions from the existing distinct values and mirrors
every existing source row into the fact table (NULL values become NULL surrogate keys).
When the source table has a primary key the backfill is idempotent - already-mirrored
rows are skipped on a re-run. Without one, call it exactly once, right after
`build_star_schema` - a second backfill would duplicate rows.

For a large table, `backfill_star_schema_batched(conn, "orders", batch_size=50_000)`
mirrors the fact rows in key-ordered batches instead, committing after each one - no
single long transaction, and an interrupted run can simply be re-run and continues where
it stopped. It requires a primary key - composite keys are walked in row-value order -
and returns the number of source rows processed; an optional `on_batch` callback
receives `(rows_in_batch, total_so_far)` after each committed batch.

That creates `orders_dim_customer`, `orders_dim_status`, `orders_dim_country`, the
`orders_fact` table holding the surrogate keys (each one indexed), and an after-insert
trigger on `orders` that resolves each new row into it. When `orders` has a primary
key, each fact row also carries it in `source_<column>` columns (unique together),
linking it to the source row it mirrors; an after-update trigger re-points the fact row
when its source row changes, and an after-delete trigger removes it when the source row
is deleted.

Pick the columns you actually group by. Omitting `columns` dimensions every column, which
gives a surrogate key or a timestamp one dimension row per fact row - pure overhead.

`check_star_schema(conn, "orders")` compares the two sides by primary key and returns
how many source rows have no fact row (`unmirrored`) and how many fact rows have no
source row left (`orphaned`) - both zero means the star schema is in sync. Read-only
and exact, so it is two full scans; run it when drift is suspected, such as after a
trigger was dropped or a backfill was interrupted. Requires a primary key.

To undo it all, `drop_star_schema(conn, "orders")` drops the triggers, the sync
functions, the fact table, and the dimension tables - the source table is never touched.
If the source table is already gone, pass `columns=` to name the dimension tables to drop.

## How it works

For `build_star_schema(conn, "orders", columns=["customer", "status"])` on a table
with primary key `id`, one transaction creates:

- `orders_dim_customer`, `orders_dim_status` - one row per distinct value:
  `id bigserial primary key`, `value` typed like the source column, unique.
- `orders_fact` - one row per source row: `customer_id` and `status_id` referencing
  the dimensions (each with its own index), plus `source_id` mirroring the primary
  key.
- `orders_sync` and `orders_sync_trigger` - after insert, resolves each value to its
  dimension row (a lookup; the value is inserted only when it is new) and writes the
  fact row. A NULL value skips its dimension and becomes a NULL surrogate key, same
  as the backfill.
- `orders_sync_update` / `orders_sync_delete` and their triggers - after update and
  after delete, re-point or remove the fact row via `source_id`. Created only when
  the source table has a primary key; without one, fact rows are insert-only.

Object names all come from `pg_star_schema.naming`, which is how `status` and `drop`
find every object later from the table name alone. All SQL is composed with
`psycopg.sql` - identifiers are always quoted, never string-formatted.

## CLI

The library operations, as commands:

```bash
pg-star-schema build orders customer status country
pg-star-schema backfill orders customer status country
pg-star-schema drop orders

pg-star-schema status orders
pg-star-schema check orders

pg-star-schema build orders --dsn postgresql://localhost/mydb
pg-star-schema build orders customer --dry-run
pg-star-schema backfill orders --batch-size 50000
```

The connection comes from `--dsn`; left empty, psycopg falls back to the libpq `PG*`
environment variables (`PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, ...). Column
arguments select what to dimension, exactly like the `columns=` parameter. `--schema`
picks a schema other than `public`. `--dry-run` prints the SQL a command would run and
executes nothing - it still connects, because the plan comes from introspecting the
source table. `backfill --batch-size N` mirrors the fact rows in key-ordered batches of
N, committing after each - the batched form of `backfill_star_schema_batched`, for large
tables. `status` reports which star schema objects exist for the table - fact and
dimension tables with row counts, and whether each sync trigger is installed (also
exported as `star_schema_status`); `status --estimate` uses the planner's row
estimates instead of exact counts, instant on large tables. `check` prints the
unmirrored and orphaned row counts and exits 0 when both are zero, 2 when they are
not (1 stays the error exit), so a cron job can alert on drift.

The lower-level pieces are exported too, if you'd rather generate the SQL and run it
yourself: `build_statements`, `backfill_statements`, `drop_statements` (the per-command
plans), `unmirrored_rows_sql`, `orphaned_rows_sql` (the two `check` queries),
`get_columns`, `get_primary_key`, `dimension_table_ddl`, `fact_table_ddl`,
`fact_index_ddl`, `dimension_upsert_sql`,
`sync_function_ddl`, `sync_trigger_ddl`, `sync_update_function_ddl`,
`sync_update_trigger_ddl`, `sync_delete_function_ddl`, `sync_delete_trigger_ddl`, and
the `drop_*_ddl` counterparts.

## License

MIT.
