"""Names of every object a star schema is made of, derived from the source
table name alone - which is how `status` and `drop` find them later.

Postgres truncates identifiers to 63 bytes (NAMEDATALEN - 1) and does so
silently, so two long names that share their first 63 bytes would collide:
`<table>_dim_<column>` for two columns whose names differ only past the cut,
or the fact index for those columns. Every name here therefore goes through
`bounded`, which keeps a name under the limit by replacing its tail with a
short hash of the full name - deterministic, so the same inputs always give
the same object name.
"""

from hashlib import sha1

MAX_IDENTIFIER_BYTES = 63
_HASH_CHARS = 8


def bounded(name: str) -> str:
    """`name` if it fits Postgres's 63-byte identifier limit, otherwise a
    truncated prefix plus `_` and the first 8 hex chars of its SHA-1.

    Truncation is byte-based (identifiers may hold multibyte characters) and
    never splits a character.
    """
    if len(name.encode()) <= MAX_IDENTIFIER_BYTES:
        return name
    suffix = "_" + sha1(name.encode()).hexdigest()[:_HASH_CHARS]
    budget = MAX_IDENTIFIER_BYTES - len(suffix)
    prefix = name.encode()[:budget].decode(errors="ignore")
    return prefix + suffix


def fact_table_name(table: str) -> str:
    return bounded(f"{table}_fact")


def dimension_table_name(table: str, column: str) -> str:
    return bounded(f"{table}_dim_{column}")


def fact_index_name(table: str, column: str) -> str:
    return bounded(f"{fact_table_name(table)}_{column}_id_idx")


def source_key_column_name(column: str) -> str:
    return bounded(f"source_{column}")


def sync_function_name(table: str) -> str:
    return bounded(f"{table}_sync")


def sync_trigger_name(table: str) -> str:
    return bounded(f"{table}_sync_trigger")


def sync_update_function_name(table: str) -> str:
    return bounded(f"{table}_sync_update")


def sync_update_trigger_name(table: str) -> str:
    return bounded(f"{table}_sync_update_trigger")


def sync_delete_function_name(table: str) -> str:
    return bounded(f"{table}_sync_delete")


def sync_delete_trigger_name(table: str) -> str:
    return bounded(f"{table}_sync_delete_trigger")
