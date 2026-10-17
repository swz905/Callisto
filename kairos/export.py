"""Memory as Markdown: a human-readable snapshot of what KAIROS knows (facts with history, people, notes,
open commitments, routines, meetings). The dashboard's "Export" button downloads it."""

from __future__ import annotations

from kairos.memory import describe_habit
from kairos.tasks import describe_task
from kairos.timeutil import fmt_time


def memory_markdown(user: str, facts: list[dict], people: list[dict], notes: list[dict], tasks: list[dict],
                    habits: list[dict], meetings: list[dict], tz: str) -> str:
    out = [f"# KAIROS memory: {user}", ""]
    out += ["## Facts", ""]
    by_key: dict[str, list[dict]] = {}
    for f in facts:
        by_key.setdefault(f["key"], []).append(f)
    for key, versions in sorted(by_key.items()):
        current = [v for v in versions if not v.get("valid_to")]
        out.append(f"- **{key}**: {current[0]['statement'] if current else '(no current value)'}")
        for v in versions:
            if v.get("valid_to"):
                out.append(f"  - ~~{v['statement']}~~ (until {fmt_time(v['valid_to'], tz)})")
    out += ["", "## People", ""]
    for p in people:
        aliases = f" (aka {', '.join(p['aliases'])})" if p.get("aliases") else ""
        rel = f": {p['relation']}" if p.get("relation") else ""
        out.append(f"- {p['name']}{aliases}{rel}")
    out += ["", "## Notes", ""]
    for n in notes:
        about = f" [{', '.join(a for a in n.get('about') or [] if a)}]" if n.get("about") else ""
        out.append(f"- {n['text']}{about}")
    out += ["", "## Open commitments", ""]
    out += [f"- [ ] {describe_task(t, tz)}" for t in tasks] or ["- none"]
    out += ["", "## Routines", ""]
    out += [f"- {describe_habit(h)} (strength {h['strength']:.2f}, seen {h['count']}x)" for h in habits] or ["- none"]
    out += ["", "## Upcoming meetings", ""]
    out += [f"- {m['title']}: {fmt_time(m['start_ts'], tz)}" + (f" with {', '.join(m['people'])}" if m.get("people")
                                                                 else "") for m in meetings] or ["- none"]
    return "\n".join(out) + "\n"
