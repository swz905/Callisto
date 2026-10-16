"""Guard for the agent/MCP `graph_query` tool: only read-only Cypher gets through.

Defence in depth: this keyword check, plus the query is executed with `ro_query`.
"""

from __future__ import annotations

import re

_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b", re.I)
_CALL = re.compile(r"\bCALL\s+([\w.]+)", re.I)
_ALLOWED_PROCS = ("db.idx.vector.querynodes", "db.idx.fulltext.querynodes", "algo.", "db.labels",
                  "db.relationshiptypes", "db.propertykeys")


class UnsafeQuery(ValueError):
    pass


def ensure_read_only(cypher: str) -> str:
    stripped = _STRINGS.sub("''", cypher)
    if m := _WRITE.search(stripped):
        raise UnsafeQuery(f"write clause not allowed: {m.group(1).upper()}")
    for proc in _CALL.findall(stripped):
        if not proc.lower().startswith(_ALLOWED_PROCS):
            raise UnsafeQuery(f"procedure not allowed: {proc}")
    if ";" in stripped.strip().rstrip(";"):
        raise UnsafeQuery("multiple statements not allowed")
    return cypher
