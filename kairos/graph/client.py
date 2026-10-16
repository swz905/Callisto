"""Thin FalkorDB wrapper: one graph per user, named queries, rows as dicts."""

from __future__ import annotations

import logging
from typing import Any

from kairos.config import Settings
from kairos.graph.guard import ensure_read_only
from kairos.graph.queries import QUERIES, render, split_statements

log = logging.getLogger(__name__)


def _columns(header: list) -> list[str]:
    """Header entries are [column_type, name] pairs; names may arrive as bytes."""
    names = [h[1] if isinstance(h, (list, tuple)) and len(h) > 1 else h for h in header]
    return [n.decode() if isinstance(n, bytes) else str(n) for n in names]


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    """Drop None values from property maps so `SET n += $props` never writes nulls."""
    out = {}
    for k, v in params.items():
        out[k] = {pk: pv for pk, pv in v.items() if pv is not None} if k == "props" and isinstance(v, dict) else v
    return out


class Graph:
    def __init__(self, settings: Settings, graph_name: str | None = None):
        from falkordb import FalkorDB  # imported lazily so pure-logic code/tests don't need it

        self.settings = settings
        self.graph_name = graph_name or settings.graph_name
        self.db = FalkorDB(**settings.falkordb_kwargs())
        self.g = self.db.select_graph(self.graph_name)

    # --- execution -------------------------------------------------------
    def _exec(self, cypher: str, params: dict[str, Any] | None, read_only: bool = False) -> list[dict]:
        params = _clean(params or {})
        res = (self.g.ro_query if read_only else self.g.query)(cypher, params)
        if not res.header:
            return []
        cols = _columns(res.header)
        return [dict(zip(cols, row)) for row in res.result_set]

    def run(self, name: str, /, **params: Any) -> list[dict]:
        return self._exec(QUERIES[name], params)

    def run_template(self, name: str, /, label: str | None = None, rel: str | None = None, **params: Any) -> list[dict]:
        return self._exec(render(QUERIES[name], label, rel), params)

    def one(self, name: str, /, **params: Any) -> dict | None:
        rows = self.run(name, **params)
        return rows[0] if rows else None

    def read_only(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        return self._exec(ensure_read_only(cypher), params, read_only=True)

    # --- admin -----------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self.db.connection.ping())
        except Exception:
            return False

    def apply_schema(self, vectors: bool = True) -> None:
        names = ["schema"] + (["schema_vectors"] if vectors else [])
        for name in names:
            for stmt in split_statements(QUERIES[name]):
                try:
                    self.g.query(stmt)
                except Exception as exc:  # index already exists, or syntax differs across versions
                    log.debug("schema statement skipped (%s): %s", exc, stmt)

    def wipe(self) -> None:
        try:
            self.g.delete()
        except Exception:
            pass
        self.g = self.db.select_graph(self.graph_name)
