"""Loads named Cypher queries from the top-level `queries/` folder.

Format: a line `// name: <query_name>` starts a query; other `//` lines are comments and are dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

QUERY_DIR = Path(__file__).resolve().parents[2] / "queries"
_NAME_RE = re.compile(r"^\s*//\s*name:\s*(\w+)\s*$")

# Labels that may be substituted into `{label}` templates. Never user-controlled.
ALLOWED_LABELS = {
    "Event", "Notification", "DownloadEvent", "FileEvent", "BrowserVisit", "CommandRun", "Utterance",
    "AppEvent", "DeviceEvent", "PlaceEvent", "WebSearch", "WebRead",
    "Watcher", "Habit", "Fact", "Action", "Download", "Trip", "Person", "App", "File", "Place",
    "Task", "Meeting", "Note",
}
# Relationship types that may be substituted into `{rel}` templates (person merge).
PERSON_RELS_OUT = ("SENT",)
PERSON_RELS_IN = ("MENTIONS", "WITH", "ABOUT_ENTITY", "WATCHES")
ALLOWED_RELS = set(PERSON_RELS_OUT + PERSON_RELS_IN)


def load_queries(directory: Path = QUERY_DIR) -> dict[str, str]:
    queries: dict[str, str] = {}
    for path in sorted(directory.glob("*.cypher")):
        name: str | None = None
        buf: list[str] = []

        def flush() -> None:
            if name is not None:
                body = "\n".join(buf).strip()
                if name in queries:
                    raise ValueError(f"duplicate query name {name!r} in {path.name}")
                queries[name] = body

        for line in path.read_text(encoding="utf-8").splitlines():
            m = _NAME_RE.match(line)
            if m:
                flush()
                name, buf = m.group(1), []
            elif line.strip().startswith("//"):
                continue
            else:
                buf.append(line)
        flush()
    return queries


def render(template: str, label: str | None = None, rel: str | None = None) -> str:
    if label is not None:
        if label not in ALLOWED_LABELS:
            raise ValueError(f"label {label!r} not allowed in query templates")
        template = template.replace("{label}", label)
    if rel is not None:
        if rel not in ALLOWED_RELS:
            raise ValueError(f"relationship {rel!r} not allowed in query templates")
        template = template.replace("{rel}", rel)
    return template


def split_statements(body: str) -> list[str]:
    return [s.strip() for s in body.split(";") if s.strip()]


QUERIES = load_queries()
