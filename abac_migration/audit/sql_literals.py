"""Tiny helper shared by audit_repository.py and inventory_repository.py for
turning Python values into SQL literals when building INSERT statements
against the append-only audit tables (§4). Deliberately minimal - these
repositories only ever write a small, known set of column shapes."""
from __future__ import annotations

import datetime as dt


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], dict):
            structs = ", ".join(_struct_literal(v) for v in value)
            return f"ARRAY({structs})" if value else "ARRAY()"
        return "ARRAY(" + ", ".join(sql_literal(v) for v in value) + ")" if value else "ARRAY()"
    if isinstance(value, dict):
        return _struct_literal(value)
    return "'" + str(value).replace("'", "''") + "'"


def _struct_literal(d: dict) -> str:
    fields = ", ".join(f"{sql_literal(v)} AS {k}" for k, v in d.items())
    return f"STRUCT({fields})"
