"""Event simulator: replays scenario scripts and generates weeks of history so habits exist on demo day."""

from __future__ import annotations

import random
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterator

import yaml

from kairos.models import EventEnvelope
from kairos.timeutil import get_tz, now_ms, to_local

SCENARIOS = Path(__file__).resolve().parents[1] / "sim" / "scenarios"

Sink = Callable[[EventEnvelope], object]


def load_scenario(name_or_path: str) -> dict:
    path = Path(name_or_path)
    if not path.exists():
        path = SCENARIOS / f"{name_or_path}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def scenario_events(scenario: dict, user_id: str, start_ms: int | None = None) -> Iterator[tuple[float, EventEnvelope]]:
    """(offset_seconds, envelope) pairs; envelope ts = start + offset."""
    start = start_ms if start_ms is not None else now_ms()
    for step in scenario["steps"]:
        at = float(step.get("at", 0))
        yield at, EventEnvelope(user_id=user_id, device_id=step["device"], type=step["type"],
                                source=step.get("source", ""), payload=dict(step.get("payload", {})),
                                ts=start + int(at * 1000))


def replay(scenario: dict, sink: Sink, user_id: str, speed: float = 1.0,
           log: Callable[[str], None] = print) -> int:
    start_wall = time.time()
    n = 0
    for at, env in scenario_events(scenario, user_id):
        delay = at / speed - (time.time() - start_wall)
        if delay > 0:
            time.sleep(delay)
        env.ts = now_ms()  # live replay: stamp with real time so watchers/routing behave as live
        log(f"  [sim {env.device_id}] {env.text()}")
        sink(env)
        n += 1
    return n


def history(user_id: str, tz: str, weeks: int = 3, seed: int = 7, now: int | None = None) -> list[EventEnvelope]:
    """Past weekday Uber rides to the Office around 9 AM, plus some background chatter."""
    rng = random.Random(seed)
    now_dt = to_local(now or now_ms(), tz)
    events: list[EventEnvelope] = []

    def at(day, hour: int, minute: int) -> int:
        dt = day.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=get_tz(tz))
        return int(dt.timestamp() * 1000)

    for days_ago in range(weeks * 7, 0, -1):
        day = now_dt - timedelta(days=days_ago)
        if day.weekday() < 5:
            trip = f"trip-hist-{day:%Y%m%d}"
            minute = rng.randint(0, 20)
            for offset, body in [(0, "Ramesh is 6 min away"), (4, "Ramesh is 2 min away"),
                                 (6, "Ramesh has arrived at your pickup"), (40, "Trip completed. Rate your driver")]:
                events.append(EventEnvelope(
                    user_id=user_id, device_id="phone-arjun", type="notification", source="com.ubercab",
                    ts=at(day, 9, minute) + offset * 60_000,
                    payload={"app": "Uber", "title": "Uber", "body": body, "trip_id": trip, "destination": "Office"}))
        if rng.random() < 0.4:
            events.append(EventEnvelope(
                user_id=user_id, device_id="phone-arjun", type="notification", source="com.whatsapp",
                ts=at(day, 20, rng.randint(0, 59)),
                payload={"app": "WhatsApp", "title": "Priya Mehta", "body": rng.choice(
                    ["Call mom tonight", "Are you coming home this weekend?", "Sent you the recipe"])}))
    return events
