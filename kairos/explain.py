"""Q9: answer "why did you do that?" from the graph path behind an Action."""

from __future__ import annotations

from dataclasses import dataclass, field

from kairos.graph.client import Graph
from kairos.timeutil import fmt_time


@dataclass
class Explanation:
    text: str
    chain: list[dict] = field(default_factory=list)


def _kind(labels: list[str] | None) -> str:
    labels = [lbl for lbl in (labels or []) if lbl != "Event"]
    return labels[0] if labels else "Event"


class Explainer:
    def __init__(self, graph: Graph, tz: str):
        self.g = graph
        self.tz = tz

    def explain(self, action_id: str) -> Explanation:
        rows = self.g.run("explain_action", action_id=action_id)
        if not rows:
            return Explanation("I have no record of that action.")
        head = rows[0]
        chain = [{"kind": _kind(r["labels"]), "text": r["text"], "ts": r["node_ts"], "hops": r["hops"],
                  "rels": r["rels"]} for r in rows if r.get("labels")]

        influenced = [c for c in chain if (c.get("rels") or [None])[0] == "INFLUENCED_BY"]
        chain_causal = [c for c in chain if c not in influenced]
        watcher = next((c for c in chain_causal if c["kind"] == "Watcher"), None)
        utterance = next((c for c in chain_causal if c["kind"] == "Utterance"), None)
        habit = next((c for c in chain_causal if c["kind"] == "Habit"), None)
        trigger = next((c for c in chain_causal if c["kind"] not in ("Watcher", "Utterance", "Habit")
                        and c["hops"] <= 2), None)

        parts = [f"I did that ({head['summary'] or head['tool']}, {fmt_time(head['ts'], self.tz)})"]
        if watcher:
            parts.append(f"because of your request “{watcher['text']}”")
            if utterance:
                parts.append(f"which you made {fmt_time(utterance['ts'], self.tz)}")
        elif utterance:
            parts.append(f"because you said “{utterance['text']}”")
        if habit:
            parts.append(f"based on your habit {habit['text']}")
        if trigger:
            parts.append(f"and it was triggered by: {trigger['text']} ({fmt_time(trigger['ts'], self.tz)})")
        text = " ".join(parts) + "."
        if head.get("tainted"):
            src = "; ".join(c["text"][:80] for c in influenced[:2]) or head.get("taint_sources") or "external content"
            text += f" Note: I decided this while reading untrusted content ({src}), so risky steps needed your OK."
        return Explanation(text, chain)
