# pg-star-schema

Automatically build and maintain a star schema on top of existing Postgres tables.

Point it at a table you already have; it introspects the columns and generates the
fact/dimension tables and the triggers needed to keep a star schema in sync as rows are
inserted, updated and deleted - no manual ETL.

## Status

Early, built incrementally. `build_star_schema` runs end to end against a live database,
`backfill_star_schema` mirrors the rows that existed before it ran, and
`drop_star_schema` removes everything again. All three are also available as a CLI.

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

That creates `orders_dim_customer`, `orders_dim_status`, `orders_dim_country`, the
`orders_fact` table holding the surrogate keys (each one indexed), and an after-insert
trigger on `orders` that resolves each new row into it. When `orders` has a primary
key, each fact row also carries it in `source_<column>` columns (unique together),
linking it to the source row it mirrors; an after-update trigger re-points the fact row
when its source row changes, and an after-delete trigger removes it when the source row
is deleted.

Pick the columns you actually group by. Omitting `columns` dimensions every column, which
gives a surrogate key or a timestamp one dimension row per fact row - pure overhead.

To undo it all, `drop_star_schema(conn, "orders")` drops the triggers, the sync
functions, the fact table, and the dimension tables - the source table is never touched.
If the source table is already gone, pass `columns=` to name the dimension tables to drop.

## CLI

The same three operations, as a command:

```bash
pg-star-schema build orders customer status country
pg-star-schema backfill orders customer status country
pg-star-schema drop orders

pg-star-schema build orders --dsn postgresql://localhost/mydb
pg-star-schema build orders customer --dry-run
```

The connection comes from `--dsn`; left empty, psycopg falls back to the libpq `PG*`
environment variables (`PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, ...). Column
arguments select what to dimension, exactly like the `columns=` parameter. `--schema`
picks a schema other than `public`. `--dry-run` prints the SQL a command would run and
executes nothing - it still connects, because the plan comes from introspecting the
source table.

The lower-level pieces are exported too, if you'd rather generate the SQL and run it
yourself: `build_statements`, `backfill_statements`, `drop_statements` (the per-command
plans), `get_columns`, `get_primary_key`, `dimension_table_ddl`, `fact_table_ddl`,
`fact_index_ddl`, `dimension_upsert_sql`,
`sync_function_ddl`, `sync_trigger_ddl`, `sync_update_function_ddl`,
`sync_update_trigger_ddl`, `sync_delete_function_ddl`, `sync_delete_trigger_ddl`, and
the `drop_*_ddl` counterparts.
