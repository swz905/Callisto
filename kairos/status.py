"""Live device status: periodic snapshots -> current state on the Device node + change events.

Device agents send a `device.status` envelope every few minutes (and on request). The hub:
  1. diffs it against the previous state (apps opened/closed, foreground switch, battery low, network change),
  2. stores the new state on the Device node and as (Device)-[:RUNNING]->(App) edges,
  3. ingests only the *changes* as normal events (so they show in the timeline, history and triage).
The agent's context card and the `device_status` tool read this state, so it can answer
"what's open on my PC?" or "is my phone charging?" without asking the device.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.models import EventEnvelope
from kairos.places import nearest_place
from kairos.timeutil import MINUTE_MS, now_ms

if TYPE_CHECKING:
    from kairos.runtime import IngestOutcome, Kairos

BATTERY_LOW = 20
STALE_MS = 10 * MINUTE_MS
MAX_APP_CHANGES = 10
STRING_FIELDS = ("foreground_app", "foreground_title", "network")
NUMBER_FIELDS = ("battery", "cpu", "mem", "idle_s", "downloads_active")


def diff_status(prev: dict | None, new: dict) -> list[tuple[str, dict]]:
    """Change events between two snapshots. The first snapshot of a device produces none (no flood)."""
    if not prev or not prev.get("status_ts"):
        return []
    out: list[tuple[str, dict]] = []
    if "apps" in new:
        old_apps, new_apps = set(prev.get("apps") or []), set(new.get("apps") or [])
        out += [("app.opened", {"app": a}) for a in sorted(new_apps - old_apps)[:MAX_APP_CHANGES]]
        out += [("app.closed", {"app": a}) for a in sorted(old_apps - new_apps)[:MAX_APP_CHANGES]]
    fg = new.get("foreground_app")
    if fg and fg != prev.get("foreground_app"):
        out.append(("app.focused", {"app": fg, "title": new.get("foreground_title") or ""}))
    b_new, b_old = new.get("battery"), prev.get("battery")
    if b_new is not None and b_new <= BATTERY_LOW and not new.get("charging") and (b_old is None or b_old > BATTERY_LOW):
        out.append(("battery.low", {"battery": b_new}))
    net_new, net_old = new.get("network"), prev.get("network")
    if net_new is not None and net_old and net_new != net_old:
        out.append(("network.changed", {"from": net_old, "to": net_new}))
    if "place_name" in new:  # only when the snapshot carried a location
        p_new, p_old = new.get("place_name") or "", prev.get("place_name") or ""
        if p_new != p_old:
            if p_old:
                out.append(("place.left", {"place": p_old}))
            if p_new:
                out.append(("place.entered", {"place": p_new}))
    return out


def state_props(payload: dict, ts: int) -> dict:
    props: dict = {"status_ts": ts}
    for k in STRING_FIELDS:
        if k in payload:
            props[k] = (payload.get(k) or "")[:120]
    for k in NUMBER_FIELDS:
        if payload.get(k) is not None:
            props[k] = payload[k]
    if payload.get("charging") is not None:
        props["charging"] = bool(payload["charging"])
    if "place_name" in payload:  # coordinates are never stored, only the matched place
        props["place_name"] = payload.get("place_name") or ""
    return props


def _age(ts: int | None, now: int) -> str:
    if not ts:
        return "never"
    mins = int((now - ts) / MINUTE_MS)
    return "just now" if mins < 1 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago"


def describe_status(r: dict, now: int | None = None) -> str:
    """One line per device, used in the agent context card and the device_status tool."""
    now = now or now_ms()
    name = r.get("name") or r.get("id")
    if not r.get("status_ts"):
        return f"{name}: no status received yet"
    parts = []
    if r.get("foreground_app"):
        title = f" (“{r['foreground_title'][:60]}”)" if r.get("foreground_title") else ""
        parts.append(f"using {r['foreground_app']}{title}")
    apps = [a for a in r.get("apps") or [] if a != r.get("foreground_app")]
    if apps:
        more = f" +{len(apps) - 8} more" if len(apps) > 8 else ""
        parts.append("also open: " + ", ".join(apps[:8]) + more)
    if r.get("battery") is not None and r["battery"] >= 0:
        parts.append(f"battery {r['battery']}%{' charging' if r.get('charging') else ''}")
    if r.get("network"):
        parts.append(f"network {r['network']}")
    if r.get("idle_s") and r["idle_s"] >= 120:
        parts.append(f"idle {r['idle_s'] // 60} min")
    if r.get("cpu") is not None or r.get("mem") is not None:
        parts.append("CPU " + (f"{r['cpu']:.0f}%" if r.get("cpu") is not None else "?") +
                     ", RAM " + (f"{r['mem']:.0f}%" if r.get("mem") is not None else "?"))
    if r.get("downloads_active"):
        parts.append(f"{r['downloads_active']} download(s) in progress")
    if r.get("place_name"):
        parts.append(f"at {r['place_name']}")
    stale = " [stale]" if now - r["status_ts"] > STALE_MS else ""
    return f"{name} ({_age(r['status_ts'], now)}{stale}): " + "; ".join(parts or ["no details"])


class StatusTracker:
    def __init__(self, k: Kairos):
        self.k = k

    def apply(self, env: EventEnvelope) -> IngestOutcome:
        from kairos.runtime import IngestOutcome

        g, p = self.k.graph, dict(env.payload)
        with self.k.lock:
            loc = p.pop("location", None)
            if isinstance(loc, dict) and loc.get("lat") is not None and loc.get("lon") is not None:
                p["place_name"] = nearest_place(float(loc["lat"]), float(loc["lon"]), g.run("geo_places")) or ""
            prev = g.one("get_device_status", device_id=env.device_id)
            changes = diff_status(prev, p)
            g.run("set_device_status", device_id=env.device_id, props=state_props(p, env.ts))
            if "apps" in p:
                apps = [str(a)[:60] for a in (p.get("apps") or [])][:40]
                g.run("stop_running_apps", device_id=env.device_id, apps=apps)
                g.run("start_running_apps", device_id=env.device_id, apps=apps, now=env.ts)
        alerts: list[dict] = []
        for type_, payload in changes:
            out = self.k.ingest(EventEnvelope(user_id=env.user_id, device_id=env.device_id, type=type_,
                                              source="status", payload=payload, ts=env.ts))
            alerts += out.alerts
        return IngestOutcome(event_id=f"status-{env.device_id}-{env.ts}", duplicate=False, alerts=alerts)

    def all(self) -> list[dict]:
        return self.k.graph.run("all_device_status", uid=self.k.settings.user_id)
