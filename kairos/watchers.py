"""Flow C: "tell me when..." watchers, stored as graph patterns and matched on every event."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from kairos.graph.client import Graph
from kairos.models import new_id
from kairos.timeutil import now_ms

EVENT_MATCHERS = ("match_entity_state", "match_event_type", "match_trip_eta", "match_app_text", "match_place")


@dataclass
class WatcherSpec:
    description: str
    condition_type: str  # entity_state | event_type | trip_eta | app_text | time
    mode: str = "once"
    target_label: str | None = None  # entity_state: label + id of the watched node
    target_id: str | None = None
    target_status: str | None = None
    event_type: str | None = None
    provider: str | None = None
    eta_threshold: int | None = None
    app: str | None = None
    keyword: str | None = None
    due_ts: int | None = None
    message: str | None = None
    place: str | None = None  # place watchers: Place name + transition ('entered' | 'left')
    transition: str | None = None


@dataclass
class Fired:
    watcher_id: str
    description: str
    notify_device: str | None
    event_text: str | None
    event_id: str | None = None


class WatcherEngine:
    def __init__(self, graph: Graph):
        self.g = graph

    def create(self, spec: WatcherSpec, utterance_id: str | None, notify_device: str | None) -> str:
        wid = f"watch-{new_id()[:8]}"
        props = {k: v for k, v in asdict(spec).items() if k not in ("target_label", "target_id")}
        props.update(id=wid, status="active", created_at=now_ms())
        self.g.run("create_watcher", props=props, utterance_id=utterance_id, notify_device=notify_device)
        if spec.target_label and spec.target_id:
            self.g.run_template("watch_target", spec.target_label, watcher_id=wid, target_id=spec.target_id)
        return wid

    def on_event(self, event_id: str) -> list[Fired]:
        fired: list[Fired] = []
        seen: set[str] = set()
        for name in EVENT_MATCHERS:
            for row in self.g.run(name, event_id=event_id):
                if row["watcher_id"] not in seen:
                    seen.add(row["watcher_id"])
                    fired.append(Fired(**row, event_id=event_id))
        return fired

    def due(self, now: int) -> list[Fired]:
        return [Fired(**row) for row in self.g.run("due_time_watchers", now=now)]

    def list_active(self) -> list[dict]:
        return self.g.run("list_active_watchers")

    def cancel(self, watcher_id: str | None = None) -> int:
        if watcher_id:
            return len(self.g.run("cancel_watcher", id=watcher_id, now=now_ms()))
        row = self.g.one("cancel_all_watchers", now=now_ms())
        return row["n"] if row else 0
