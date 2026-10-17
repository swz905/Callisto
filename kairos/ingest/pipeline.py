"""Flow A: device event -> dedupe -> extract -> one Event node + entity links (design doc §3.2)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kairos.agent.llm import Extractor
from kairos.config import Settings
from kairos.embed import Embedder, NullEmbedder
from kairos.graph.client import Graph
from kairos.ingest import rules
from kairos.models import EventEnvelope, new_id
from kairos.timeutil import HOUR_MS, weekday_hour
from kairos.trust import event_trust

log = logging.getLogger(__name__)

LABELS = {
    "notification": "Notification",
    "download.started": "DownloadEvent",
    "download.completed": "DownloadEvent",
    "download.failed": "DownloadEvent",
    "file.created": "FileEvent",
    "file.modified": "FileEvent",
    "file.moved": "FileEvent",
    "file.deleted": "FileEvent",
    "browser.visit": "BrowserVisit",
    "command.run": "CommandRun",
    "utterance": "Utterance",
    "app.opened": "AppEvent",
    "app.closed": "AppEvent",
    "app.focused": "AppEvent",
    "battery.low": "DeviceEvent",
    "network.changed": "DeviceEvent",
    "place.entered": "PlaceEvent",
    "place.left": "PlaceEvent",
    "web.search": "WebSearch",
    "web.read": "WebRead",
}
DOWNLOAD_STATUS = {"download.started": "in_progress", "download.completed": "completed", "download.failed": "failed"}


@dataclass
class IngestResult:
    event_id: str
    duplicate: bool = False
    linked: dict = field(default_factory=dict)


class Ingestor:
    def __init__(self, graph: Graph, settings: Settings, extractor: Extractor, embedder: Embedder | None = None):
        self.g = graph
        self.s = settings
        self.extractor = extractor
        self.embedder = embedder or NullEmbedder()

    def ingest(self, env: EventEnvelope) -> IngestResult:
        h = env.content_hash()
        if existing := self.g.one("event_by_hash", hash=h):
            return IngestResult(event_id=existing["id"], duplicate=True)

        extraction = None
        props = {"id": env.event_id, "type": env.type, "ts": env.ts, "source": env.source,
                 "text": env.text(), "hash": h, "user_id": env.user_id,
                 "trust": event_trust(env.type, env.source)}  # never taken from the payload
        p = env.payload
        if env.type.startswith("place."):
            props["place"] = p.get("place")

        if env.type == "notification":
            extraction = self.extractor.extract_notification(env.source, p)
            props.update(app=extraction.app, title=p.get("title"), body=p.get("body"), category=extraction.category)
            if extraction.trip:
                props["status"] = extraction.trip["status"]
        elif env.type in DOWNLOAD_STATUS:
            props["status"] = DOWNLOAD_STATUS[env.type]
        elif env.type == "utterance":
            props.update(speaker=p.get("speaker", "user"), session_id=p.get("session_id"))
        elif env.type == "command.run":
            props.update(cmd=p.get("cmd"), exit_code=p.get("exit_code"), stdout_excerpt=(p.get("stdout") or "")[:500])

        self.g.run_template("create_event", LABELS[env.type], device_id=env.device_id, props=props)
        linked = self._link(env, extraction)
        self._embed(env)
        return IngestResult(event_id=env.event_id, linked=linked)

    # --- linking per event type -------------------------------------------
    def _link(self, env: EventEnvelope, ex: rules.Extraction | None) -> dict:
        eid, p = env.event_id, env.payload
        linked: dict = {}
        if ex is not None:
            self.g.run("link_app", event_id=eid, app=ex.app, category=ex.category)
            if ex.message:
                linked["message"] = self._link_message(env, ex.message)
                linked["sender"] = ex.message.get("sender")
                linked["text"] = f"{ex.message.get('subject') or ''}. {ex.message.get('snippet') or ''}"
            elif ex.trip:
                linked["trip"] = self._link_trip(env, ex.trip)
            for name in ex.people if not ex.message else []:
                self.g.run("link_mentions_person", event_id=eid, person_id=self.resolve_person(name))
        elif env.type in DOWNLOAD_STATUS:
            linked["download"] = self._link_download(env)
        elif env.type.startswith("file.") and p.get("path"):
            name, ext, _ = rules.file_parts(p["path"])
            self.g.run("link_file", event_id=eid, path=p["path"], name=name, ext=ext)
        elif env.type == "browser.visit" and p.get("url"):
            self.g.run("link_url", event_id=eid, url=p["url"], domain=rules.domain_of(p["url"]), title=p.get("title"))
        elif env.type.startswith("app.") and p.get("app"):
            self.g.run("link_app", event_id=eid, app=p["app"], category="device_app")
        elif env.type == "web.search":
            for r in (p.get("results") or [])[:10]:
                self.g.run("link_search_result", event_id=eid, url=r["url"], domain=rules.domain_of(r["url"]),
                           title=r.get("title"), rank=r.get("rank"), snippet=(r.get("snippet") or "")[:300])
        elif env.type == "web.read" and p.get("url"):
            self.g.run("link_url", event_id=eid, url=p["url"], domain=rules.domain_of(p["url"]), title=p.get("title"))
            self.g.run("set_link_excerpt", url=p["url"], excerpt=(p.get("excerpt") or "")[:1500],
                       title=p.get("title"), now=env.ts)
        elif env.type.startswith("place.") and p.get("place"):
            self.g.run("link_event_place", event_id=eid, place=p["place"],
                       place_id=f"place-{p['place'].lower().replace(' ', '-')}")
        elif env.type == "utterance" and p.get("session_id"):
            self.g.run("link_session", event_id=eid, session_id=p["session_id"], ts=env.ts, device_id=env.device_id)
        return linked

    def resolve_person(self, name: str) -> str:
        if row := self.g.one("resolve_person", name=name):
            return row["id"]
        return self.g.one("create_person", name=name, id=f"person-{new_id()[:8]}")["id"]

    def _link_message(self, env: EventEnvelope, msg: dict) -> str:
        mid = f"msg-{env.event_id}"
        person_id = self.resolve_person(msg["sender"] or "Unknown")
        self.g.run("link_mentions_person", event_id=env.event_id, person_id=person_id)
        self.g.run("create_message", event_id=env.event_id, person_id=person_id, uid=env.user_id, id=mid,
                   channel=msg["channel"], subject=msg["subject"], snippet=msg["snippet"], ts=env.ts,
                   links=[{"url": u, "domain": rules.domain_of(u)} for u in msg["urls"]])
        return mid

    def _link_trip(self, env: EventEnvelope, trip: dict) -> str:
        trip_id = trip.get("trip_id")
        if not trip_id:
            row = self.g.one("active_trip", provider=trip["provider"], since=env.ts - 90 * 60_000)
            trip_id = row["id"] if row else f"trip-{env.event_id[:8]}"
        weekday, hour = weekday_hour(env.ts, self.s.tz)
        self.g.run("upsert_trip", event_id=env.event_id, trip_id=trip_id, provider=trip["provider"], ts=env.ts,
                   weekday=weekday, hour=hour, status=trip["status"], eta_min=trip["eta_min"], driver=trip["driver"])
        if trip.get("destination"):
            self.g.run("link_trip_destination", trip_id=trip_id, place=trip["destination"],
                       place_id=f"place-{trip['destination'].lower().replace(' ', '-')}")
        return trip_id

    def _link_download(self, env: EventEnvelope) -> str:
        p = env.payload
        path = p.get("path")
        download_id = p.get("download_id")
        if not download_id:  # agents should send one; otherwise attach to the latest open download
            row = self.g.one("latest_open_download", since=env.ts - 6 * HOUR_MS)
            download_id = row["id"] if row and env.type != "download.started" else f"dl-{env.event_id[:8]}"
        filename = p.get("filename") or (rules.file_parts(path)[0] if path else None)
        url = p.get("url")
        self.g.run("upsert_download", event_id=env.event_id, download_id=download_id, ts=env.ts,
                   status=DOWNLOAD_STATUS[env.type], filename=filename, url=url, bytes=p.get("bytes"),
                   domain=rules.domain_of(url) if url else None)
        if env.type == "download.completed" and path:
            name, ext, folder = rules.file_parts(path)
            self.g.run("download_result_file", download_id=download_id, path=path, name=name, ext=ext,
                       size=p.get("bytes"), folder=folder)
        return download_id

    def _embed(self, env: EventEnvelope) -> None:
        if env.type in ("notification", "utterance"):
            vec = self.embedder.embed(env.text())
            if vec:
                self.g.run_template("set_embedding", "Event", id=env.event_id, embedding=vec)
