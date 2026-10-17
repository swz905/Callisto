"""Location awareness: the phone's position is mapped to a known Place (geofence) - only the place *name*
is stored, never raw coordinates. Transitions become `place.entered` / `place.left` events, which can fire
"remind me to buy milk when I leave the office" watchers.
"""

from __future__ import annotations

import math

DEFAULT_RADIUS_M = 250
PLACE_ALIASES = {"work": "Office", "office": "Office", "the office": "Office", "home": "Home", "house": "Home"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_place(lat: float, lon: float, places: list[dict]) -> str | None:
    """Name of the closest place whose geofence contains the point, else None."""
    best, best_d = None, float("inf")
    for p in places:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        d = haversine_m(lat, lon, float(p["lat"]), float(p["lon"]))
        if d <= float(p.get("radius_m") or DEFAULT_RADIUS_M) and d < best_d:
            best, best_d = p["name"], d
    return best


def normalize_place(spoken: str, known: list[str] | None = None) -> str:
    s = spoken.strip().lower().rstrip(".!?")
    s = s[4:] if s.startswith("the ") else s
    for name in known or []:
        if name.lower() == s:
            return name
    return PLACE_ALIASES.get(s) or PLACE_ALIASES.get(f"the {s}") or s.title()
