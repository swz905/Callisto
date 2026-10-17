"""Load the synthetic persona, permission graph and starting facts into the user's graph."""

from __future__ import annotations

from pathlib import Path

import yaml

from kairos.graph.client import Graph

PERSONA = Path(__file__).resolve().parents[1] / "sim" / "persona.yaml"


def load_persona(path: Path = PERSONA) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def seed(graph: Graph, persona: dict | None = None, memory=None) -> dict:
    p = persona or load_persona()
    u = p["user"]
    graph.run("seed_user", uid=u["id"], name=u["name"], tz=u["tz"])
    for d in p["devices"]:
        graph.run("seed_device", uid=u["id"], **d)
    for person in p["people"]:
        graph.run("seed_person", uid=u["id"], id=person["id"], name=person["name"],
                  aliases=person.get("aliases", []), relation=person.get("relation"))
    for place in p["places"]:
        graph.run("seed_place", id=place["id"], name=place["name"], kind=place.get("kind", "other"),
                  lat=place.get("lat"), lon=place.get("lon"), radius_m=place.get("radius_m"))
    for cap in p["capabilities"]:
        graph.run("seed_capability", name=cap["name"], risk=cap["risk"], description=cap.get("description", ""))
    for device_id, perms in p["permissions"].items():
        for capability, mode in perms.items():
            graph.run("seed_permission", device_id=device_id, capability=capability, mode=mode)
    if memory is not None:
        for fact in p.get("facts", []):
            if not memory.get_fact(fact["key"]):
                memory.remember(fact["statement"], fact["key"], confidence=1.0)
        people = {person["name"]: person["id"] for person in p["people"]}
        existing = {n["text"] for n in graph.run("list_notes")}
        for note in p.get("notes", []):
            if note["text"] not in existing:
                memory.add_note(note["text"], [people[n] for n in note.get("about", []) if n in people])
    return {"devices": len(p["devices"]), "people": len(p["people"]), "capabilities": len(p["capabilities"])}
