"""`kairos doctor`: end-to-end readiness check, safe to run any time.

1. Python packages present (required + optional, by feature)
2. .env present and LLM configured
3. FalkorDB reachable
4. On a throwaway graph (`kairos:_doctor`, deleted afterwards): the schema applies, and FalkorDB accepts every
   named query in `queries/` via GRAPH.EXPLAIN (parses + plans, touches no data). This catches Cypher dialect
   problems for all ~85 queries at once instead of one at a time mid-demo.
5. The user's graph is seeded (persona, devices, permissions)
6. Optional: a real OpenRouter call (--llm)
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

from kairos.config import Settings
from kairos.graph.queries import QUERIES, render, split_statements

SCRATCH_GRAPH = "kairos:_doctor"
PARAM_RE = re.compile(r"\$(\w+)")
SKIP_EXPLAIN = {"schema", "schema_vectors"}  # applied directly instead
TEMPLATE_LABEL = {"watch_target": "Download", "link_cause": "Event", "set_embedding": "Event",
                  "create_event": "Notification", "derived_from_digest": "Fact"}
TEMPLATE_REL = {"merge_person_out": "SENT", "merge_person_in": "MENTIONS"}
# Server-feature dependent: the code falls back gracefully, so a failure here is a warning, not a blocker.
OPTIONAL_QUERIES = {"pagerank_people", "vector_facts", "vector_events", "search_events_fulltext"}

LIST_PARAMS = {"links", "apps", "tools", "aliases", "people", "embedding", "topics"}
NUMBER_PARAMS_EXTRA = {"start", "end", "due_ts", "lat", "lon", "radius_m"}
MAP_PARAMS = {"props"}
NUMBER_PARAMS = {"ts", "now", "since", "until", "min_count", "threshold", "lambda", "eta_min", "weekday", "hour",
                 "bytes", "size", "risk", "conf", "importance", "min_strength", "limit", "turns"}
BOOL_PARAMS = {"online"}

PACKAGES = [  # (import name, pip name, needed for)
    ("falkordb", "falkordb", "core"), ("fastapi", "fastapi", "core"), ("uvicorn", "uvicorn", "core"),
    ("pydantic", "pydantic", "core"), ("yaml", "pyyaml", "core"), ("httpx", "httpx", "core"),
    ("websockets", "websockets", "device agents"), ("dotenv", "python-dotenv", "core (.env loading)"),
    ("watchdog", "watchdog", "PC agent downloads"), ("win11toast", "win11toast", "PC toasts (optional)"),
    ("pyttsx3", "pyttsx3", "PC speech (optional)"), ("psutil", "psutil", "CPU % in status (optional)"),
    ("faster_whisper", "faster-whisper", "speech-to-text for dashboard mic + pet (optional)"),
    ("numpy", "numpy", "voice audio (optional)"), ("sounddevice", "sounddevice", "mic for the pet / voice (optional)"),
    ("keyboard", "keyboard", "voice push-to-talk (optional)"), ("mcp", "mcp", "MCP server (optional)"),
    ("sentence_transformers", "sentence-transformers", "embeddings (optional)"),
    ("openwakeword", "openwakeword", "wake word (optional)"),
    ("playwright", "playwright", "real browser for web search, else plain HTTP (optional)"),
]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


def dummy_value(name: str):
    if name == "embedding":
        return [0.1] * 384
    if name in LIST_PARAMS:
        return []
    if name in MAP_PARAMS:
        return {"id": "doctor"}
    if name in NUMBER_PARAMS or name in NUMBER_PARAMS_EXTRA:
        return 0
    if name in BOOL_PARAMS:
        return True
    return "doctor"


def dummy_params(cypher: str) -> dict:
    return {name: dummy_value(name) for name in dict.fromkeys(PARAM_RE.findall(cypher))}


def query_for(name: str) -> str:
    q = QUERIES[name]
    return render(q, TEMPLATE_LABEL.get(name, "Event") if "{label}" in q else None,
                  TEMPLATE_REL.get(name, "MENTIONS") if "{rel}" in q else None)


def check_packages() -> list[Check]:
    out = []
    for mod, pip, purpose in PACKAGES:
        found = importlib.util.find_spec(mod) is not None
        required = not purpose.endswith("(optional)") and purpose != "PC agent downloads"
        out.append(Check(f"package {pip}", found, purpose if found else f"missing: pip install {pip}  ({purpose})",
                         required))
    return out


def check_env(settings: Settings, root: Path) -> list[Check]:
    from kairos.agent.openrouter import OpenRouterClient

    out = [Check(".env file", (root / ".env").exists(), "copy .env.example to .env" if not (root / ".env").exists()
                 else "found", required=False)]
    if settings.llm == "rules":
        out.append(Check("LLM", True, "rules-only mode (KAIROS_LLM=rules)", required=False))
    else:
        st = OpenRouterClient(settings).status()
        out.append(Check("LLM config", bool(settings.openrouter_api_key and settings.openrouter_model),
                         f"model {settings.openrouter_model}" if st.get("online") else
                         f"{st.get('reason')} -> offline rule planner will be used", required=False))
    sandbox = Path(settings.sandbox_dir)
    out.append(Check("sandbox folder", True, f"{sandbox} ({'exists' if sandbox.exists() else 'created on first PC agent run'})",
                     required=False))
    # optional integrations: report what's on, never block
    tg = settings.telegram_bot_token
    out.append(Check("Telegram", bool(tg and settings.telegram_allowed_chat_ids) or not tg,
                     "off" if not tg else (f"{len(settings.telegram_allowed_chat_ids)} allowed chat(s)"
                                           if settings.telegram_allowed_chat_ids else
                                           "token set but TELEGRAM_ALLOWED_CHAT_IDS empty: nobody can talk to it"),
                     required=False))
    out.append(Check("calendar", True, ", ".join("demo" if c == "demo" else "ICS feed" for c in settings.calendar_urls)
                     or "off (KAIROS_CALENDAR_URLS)", required=False))
    out.append(Check("email (IMAP)", True, f"{settings.imap_user}@{settings.imap_host}" if settings.imap_host
                     else "off (KAIROS_IMAP_*)", required=False))
    plugins_file = Path(settings.plugins_file or root / "plugins.json")
    out.append(Check("MCP plugins", True, str(plugins_file) if plugins_file.exists() else "off (no plugins.json)",
                     required=False))
    out.append(Check("family graph", True, settings.family_graph or "off (KAIROS_FAMILY_GRAPH)", required=False))
    from kairos.web import WebService

    st = WebService(settings).status()
    out.append(Check("web (read-only)", True, "off (KAIROS_WEB=off)" if st["backend"] == "off" else
                     f"{st['backend']} backend, {st['engine']}"
                     + (f", browser {st['channel']}, {'headless' if st['headless'] else 'visible window'}"
                        if st["backend"] == "playwright" else
                        " (pip install playwright for a real browser)" if not st["playwright_installed"] else ""),
                     required=False))
    if "localhost" in settings.hub_public_url:
        out.append(Check("hub public URL", True, f"{settings.hub_public_url}: fine on one machine; set "
                         "KAIROS_HUB_PUBLIC_URL to the PC's LAN address so the phone can open handoff links",
                         required=False))
    return out


def check_graph(settings: Settings) -> list[Check]:
    try:
        from falkordb import FalkorDB

        db = FalkorDB(**settings.falkordb_kwargs())
        db.connection.ping()
    except Exception as exc:
        return [Check("FalkorDB reachable", False,
                      f"{settings.falkordb_host}:{settings.falkordb_port} -> {exc}. Start it with "
                      f"`docker compose up -d falkordb`, or point FALKORDB_HOST/PORT/USERNAME/PASSWORD/SSL "
                      f"in .env at a hosted FalkorDB (no Docker needed)")]
    out = [Check("FalkorDB reachable", True, f"{settings.falkordb_host}:{settings.falkordb_port}")]

    scratch = db.select_graph(SCRATCH_GRAPH)
    try:
        scratch.query("RETURN 1")
        failed_schema = []
        for name in ("schema", "schema_vectors"):
            for stmt in split_statements(QUERIES[name]):
                try:
                    scratch.query(stmt)
                except Exception as exc:
                    if "already" not in str(exc).lower():
                        failed_schema.append(f"{stmt[:60]}... -> {exc}")
        out.append(Check("schema / indexes", not failed_schema,
                         "all applied" if not failed_schema else "; ".join(failed_schema), required=False))

        bad, optional_bad = [], []
        names = [n for n in QUERIES if n not in SKIP_EXPLAIN]
        for name in names:
            q = query_for(name)
            try:
                scratch.explain(q, dummy_params(q))
            except Exception as exc:
                (optional_bad if name in OPTIONAL_QUERIES else bad).append(
                    f"{name}: {str(exc).splitlines()[0][:160]}")
        ok_count = len(names) - len(bad) - len(optional_bad)
        out.append(Check(f"Cypher queries accepted ({ok_count}/{len(names)})", not bad,
                         "all parse and plan" if not bad else "\n      " + "\n      ".join(bad)))
        if optional_bad:
            out.append(Check("optional server features (fallbacks are used)", False,
                             "\n      " + "\n      ".join(optional_bad), required=False))
    finally:
        try:
            scratch.delete()
        except Exception:
            pass

    g = db.select_graph(settings.graph_name)
    try:
        rows = g.ro_query("MATCH (u:User {id: $uid})-[:OWNS]->(d:Device) RETURN count(d)", {"uid": settings.user_id})
        n = rows.result_set[0][0] if rows.result_set else 0
        caps = g.ro_query("MATCH (:Device)-[a:ALLOWED]->(:Capability) RETURN count(a)").result_set[0][0]
    except Exception:
        n, caps = 0, 0
    out.append(Check(f"graph {settings.graph_name} seeded", n > 0 and caps > 0,
                     f"{n} devices, {caps} permissions" if n else "run: kairos setup --wipe", required=False))
    return out


def check_llm_call(settings: Settings) -> Check:
    from kairos.agent.openrouter import LLMUnavailable, OpenRouterClient

    client = OpenRouterClient(settings)
    if not client.configured:
        return Check("OpenRouter call", False, client.status()["reason"], required=False)
    try:
        msg = client.chat([{"role": "user", "content": "Reply with: pong"}], max_tokens=5)
        return Check("OpenRouter call", True, f"replied: {(msg.get('content') or '').strip()[:30]}", required=False)
    except LLMUnavailable as exc:
        return Check("OpenRouter call", False, str(exc), required=False)


def check_web_live(settings: Settings) -> Check:
    from kairos.web import WebError, WebService

    try:
        web = WebService(settings)
        results = web.search("FalkorDB graph database", 3)
        if not results:
            return Check("web search (live)", False, "no results parsed", required=False)
        page = web.read(results[0].url)
        return Check("web search (live)", True, f"{len(results)} results; read {page.domain} via {page.backend} "
                     f"({len(page.text)} chars)", required=False)
    except WebError as exc:
        return Check("web search (live)", False, str(exc), required=False)
    except Exception as exc:
        return Check("web search (live)", False, f"{type(exc).__name__}: {str(exc)[:150]} (offline? proxy? "
                     "set KAIROS_CA_BUNDLE if your network uses its own certificate)", required=False)


def run(settings: Settings, root: Path, llm_call: bool = False, web_call: bool = False) -> tuple[list[Check], bool]:
    checks = check_packages() + check_env(settings, root) + check_graph(settings)
    if llm_call:
        checks.append(check_llm_call(settings))
    if web_call:
        checks.append(check_web_live(settings))
    ok = all(c.ok for c in checks if c.required)
    return checks, ok


def report(checks: list[Check], ok: bool) -> str:
    lines = []
    for c in checks:
        mark = "PASS" if c.ok else ("FAIL" if c.required else "warn")
        lines.append(f"[{mark}] {c.name}: {c.detail}")
    lines.append("")
    lines.append("READY: run `kairos hub` and open http://localhost:8000/" if ok else
                 "NOT READY: fix the FAIL lines above (warn lines are optional features)")
    return "\n".join(lines)
