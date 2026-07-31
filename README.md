# pg-star-schema

Automatically build and maintain a star schema on top of existing Postgres tables.

Point it at a table you already have; it introspects the columns and generates the
fact/dimension tables and the trigger needed to keep a star schema in sync as rows are
inserted - no manual ETL.

## Status

Early, built incrementally. `build_star_schema` runs end to end against a live database,
`backfill_star_schema` mirrors the rows that existed before it ran, and
`drop_star_schema` removes everything again.

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
`orders_fact` table holding the surrogate keys, and an after-insert trigger on `orders`
that resolves each new row into it. When `orders` has a primary key, each fact row also
carries it in `source_<column>` columns (unique together), linking it to the source row
it mirrors.

Pick the columns you actually group by. Omitting `columns` dimensions every column, which
gives a surrogate key or a timestamp one dimension row per fact row - pure overhead.

To undo it all, `drop_star_schema(conn, "orders")` drops the trigger, the sync function,
the fact table, and the dimension tables - the source table is never touched. If the
source table is already gone, pass `columns=` to name the dimension tables to drop.

The lower-level pieces are exported too, if you'd rather generate the SQL and run it
yourself: `get_columns`, `get_primary_key`, `dimension_table_ddl`, `fact_table_ddl`, `dimension_upsert_sql`,
`sync_function_ddl`, `sync_trigger_ddl`, and the `drop_*_ddl` counterparts.
