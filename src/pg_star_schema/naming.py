def fact_table_name(table: str) -> str:
    return f"{table}_fact"


def dimension_table_name(table: str, column: str) -> str:
    return f"{table}_dim_{column}"


def sync_function_name(table: str) -> str:
    return f"{table}_sync"


def sync_trigger_name(table: str) -> str:
    return f"{table}_sync_trigger"
