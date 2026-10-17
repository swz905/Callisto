"""Shared family memory: a second FalkorDB graph (KAIROS_FAMILY_GRAPH, e.g. `kairos:family`) next to each
member's private graph (`kairos:<user>`). Members choose what to share ("tell the family the plumber comes at 5");
nothing private is ever copied automatically. Each member runs their own KAIROS with their own KAIROS_USER and
the same family graph. Shared facts with a key supersede older ones, like private facts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.models import new_id
from kairos.timeutil import now_ms

if TYPE_CHECKING:
    from kairos.graph.client import Graph
    from kairos.runtime import Kairos


class FamilyMemory:
    def __init__(self, k: Kairos):
        self.k = k
        self.graph_name = k.settings.family_graph
        self._g: Graph | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.graph_name)

    @property
    def g(self) -> Graph:
        if self._g is None:
            from kairos.graph.client import Graph

            self._g = Graph(self.k.settings, graph_name=self.graph_name)
        return self._g

    def share(self, statement: str, key: str | None = None) -> str:
        fid = f"shared-{new_id()[:8]}"
        self.g.run("family_share", member=self.k.settings.user_id,
                   name=self.k.settings.user_display_name or self.k.settings.user_id,
                   id=fid, statement=statement[:500], key=key, now=now_ms())
        return fid

    def search(self, q: str | None = None) -> list[dict]:
        return self.g.run("family_search", q=q or "")
