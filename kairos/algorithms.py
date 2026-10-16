"""Graph algorithms as agent tools (hackathon Track 01).

important_people: blends FalkorDB's built-in PageRank (centrality across the whole graph) with
recency-weighted interaction signals (messages, mentions, open tasks, upcoming meetings, relationship).
If the algorithm procedure isn't available on the server, the interaction score alone is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.timeutil import DAY_MS

if TYPE_CHECKING:
    from kairos.graph.client import Graph

CLOSE = {"manager", "boss", "sister", "brother", "mother", "father", "wife", "husband", "partner", "family"}


def interaction_score(r: dict, now: int) -> float:
    score = 2.0 * (r.get("msgs") or 0) + 1.0 * (r.get("mentions") or 0) + 3.0 * (r.get("open_tasks") or 0)
    score += 2.5 * (r.get("meetings") or 0)
    if r.get("last_ts") and now - r["last_ts"] < 2 * DAY_MS:
        score += 3.0
    if (r.get("relation") or "").lower() in CLOSE:
        score += 2.0
    return score


def reasons(r: dict) -> str:
    out = []
    if r.get("open_tasks"):
        out.append(f"{r['open_tasks']} open task(s)")
    if r.get("meetings"):
        out.append(f"{r['meetings']} upcoming meeting(s)")
    if r.get("msgs"):
        out.append(f"{r['msgs']} recent message(s)")
    if r.get("relation"):
        out.append(r["relation"])
    return ", ".join(out) or "connected in your graph"


def pagerank_people(g: Graph) -> dict[str, float]:
    try:
        rows = g.run("pagerank_people")
    except Exception:  # procedure missing or different signature on this FalkorDB version
        return {}
    return {r["id"]: float(r["score"] or 0) for r in rows if r.get("id")}


def important_people(g: Graph, uid: str, now: int, days: int = 14, top: int = 5) -> list[dict]:
    rows = g.run("person_interactions", uid=uid, since=now - days * DAY_MS, now=now)
    pr = pagerank_people(g)
    max_pr = max(pr.values()) if pr else 0.0
    out = []
    for r in rows:
        s = interaction_score(r, now)
        if max_pr:
            s += 5.0 * pr.get(r["id"], 0.0) / max_pr
        if s > 0:
            out.append({**r, "score": round(s, 2), "why": reasons(r), "pagerank": pr.get(r["id"])})
    return sorted(out, key=lambda x: -x["score"])[:top]
