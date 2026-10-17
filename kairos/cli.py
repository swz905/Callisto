"""KAIROS command line.

  kairos setup [--wipe] [--weeks 3]      schema + persona + synthetic history + habit detection
  kairos chat [--device pc-main]          typed conversation (voice fallback); /replay morning runs a scenario live
  kairos replay morning [--hub URL]       push a scenario into the hub (or directly into the graph)
  kairos hub [--port 8000]                run the hub + web dashboard (http://localhost:8000/)
  kairos mcp                              run the MCP server (stdio)
  kairos llm-check                        test OpenRouter key/model and tool calling
  kairos doctor [--llm]                   end-to-end readiness: packages, .env, FalkorDB, all queries, seeding
  kairos digest [--day today] [--rules]   file a day of chat (daily log) into memory now; --pending = all due days
  kairos pet [--voice-chat]               desktop pet: status bubbles above the taskbar, click it to talk
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading


def _runtime(simulate: bool = True):
    from kairos.config import load_settings
    from kairos.gateway import LocalGateway
    from kairos.runtime import Kairos

    settings = load_settings()
    gw = LocalGateway(simulate=simulate, user_id=settings.user_id)
    k = Kairos(settings, gateway=gw)
    gw.emit = k.ingest
    gw.on_result = k.device_result
    return k


def cmd_setup(args: argparse.Namespace) -> None:
    from kairos.seed import seed
    from kairos.sim import history

    k = _runtime()
    if args.wipe:
        k.graph.wipe()
        print("wiped graph", k.settings.graph_name)
    k.graph.apply_schema()
    if args.fresh:
        from kairos.seed import load_persona

        persona = load_persona()
        if args.name:
            persona["user"]["name"] = args.name
            for d in persona["devices"]:
                d["name"] = f"{args.name}'s {d['kind']}"
        persona["people"], persona["places"], persona["facts"] = [], [], []
        persona["notes"] = []
        print("seeded (fresh, no demo data):", seed(k.graph, persona, memory=k.memory))
        k.refresh_devices()
        return
    print("seeded:", seed(k.graph, memory=k.memory))
    k.refresh_devices()
    events = history(k.settings.user_id, k.settings.tz, weeks=args.weeks)
    for env in events:
        k.ingest(env)
    print(f"ingested {len(events)} historical events")
    result = k.consolidate()
    for h in result["habits"]:
        print(f"habit: {h['id']} strength={h['strength']:.2f} count={h['count']}")


def cmd_chat(args: argparse.Namespace) -> None:
    from kairos.sim import load_scenario, replay

    k = _runtime()
    k.graph.run("set_all_devices_online", uid=k.settings.user_id, online=True)
    k.refresh_devices()
    k.start_background()
    device = args.device or k.devices.get("pc") or "pc-main"
    stop = threading.Event()

    def ticker() -> None:
        n = 0
        while not stop.wait(5):
            try:
                if n % 12 == 0:  # every minute: calendar / email pulls that are due
                    k.background_jobs()
                k.tick()
            except Exception as exc:
                print(f"(tick failed: {exc})")
            n += 1

    threading.Thread(target=ticker, daemon=True).start()
    print(f"KAIROS chat on {device} (llm: {k.llm_status()}). Commands: /replay <scenario> [speed], "
          f"/device <id>, /consolidate, /quit")
    while True:
        try:
            line = input(f"you@{device}> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/replay"):
            parts = line.split()
            name = parts[1] if len(parts) > 1 else "morning"
            speed = float(parts[2]) if len(parts) > 2 else 2.0
            scenario = load_scenario(name)
            threading.Thread(target=replay, args=(scenario, k.ingest, k.settings.user_id, speed), daemon=True).start()
            continue
        if line.startswith("/device"):
            device = line.split(maxsplit=1)[1] if " " in line else device
            continue
        if line == "/consolidate":
            print(json.dumps(k.consolidate(), default=str, indent=1))
            continue
        reply = k.chat(line, device)
        print(f"kairos> {reply.text}   [{reply.planner}]")
    stop.set()


def cmd_doctor(args: argparse.Namespace) -> None:
    from pathlib import Path

    from kairos import doctor
    from kairos.config import load_settings

    checks, ok = doctor.run(load_settings(), Path(__file__).resolve().parents[1], llm_call=args.llm,
                            web_call=args.web)
    print(doctor.report(checks, ok))
    sys.exit(0 if ok else 1)


def cmd_llm_check(args: argparse.Namespace) -> None:
    """Checks the OpenRouter config without touching the graph."""
    from kairos.agent.llm import OpenRouterPlanner, TurnContext
    from kairos.agent.openrouter import LLMUnavailable, OpenRouterClient
    from kairos.agent.tools import openai_tools
    from kairos.config import load_settings
    from kairos.timeutil import now_ms

    s = load_settings()
    client = OpenRouterClient(s)
    print(f"mode={s.llm} model={s.openrouter_model} extract_model={s.openrouter_extract_model} "
          f"fallbacks={list(s.openrouter_fallback_models)}")
    if not client.configured:
        sys.exit(f"Not configured: {client.status()['reason']}. Put OPENROUTER_API_KEY and OPENROUTER_MODEL in .env")
    try:
        msg = client.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=10)
        print("chat ok:", (msg.get("content") or "").strip())
        calls: list[tuple[str, dict]] = []
        planner = OpenRouterPlanner(client, openai_tools())
        ctx = TurnContext(device_id="pc-main", devices={"pc": "pc-main", "phone": "phone-arjun"}, now=now_ms(),
                          tz=s.tz, card="Devices: pc = pc-main (this device), phone = phone-arjun.")
        planner.run("Remind me when my Uber is 2 minutes away", ctx,
                    lambda name, a: calls.append((name, a)) or "Got it. I'll remind you.")
        print("tool calling:", calls or "model answered without calling a tool (pick a model with tool support)")
    except LLMUnavailable as exc:
        sys.exit(f"OpenRouter unavailable: {exc}")


def cmd_replay(args: argparse.Namespace) -> None:
    from kairos.config import load_settings
    from kairos.sim import load_scenario, replay

    scenario = load_scenario(args.scenario)
    settings = load_settings()
    if args.hub:
        import httpx

        def sink(env):
            r = httpx.post(f"{args.hub.rstrip('/')}/events", json=env.model_dump(), timeout=10)
            for alert in r.json().get("alerts", []):
                print(f"  -> alert on {alert['device']}: {alert['text']}")

        replay(scenario, sink, settings.user_id, args.speed)
    else:
        k = _runtime()
        replay(scenario, k.ingest, settings.user_id, args.speed)


def cmd_digest(args: argparse.Namespace) -> None:
    from kairos.digest import parse_day

    k = _runtime()
    if args.pending:
        days = k.digest.pending_days()
        print(f"{len(days)} day(s) due: {', '.join(d.isoformat() for d in days) or 'none'}")
    else:
        days = [parse_day(args.day, k.settings.tz)]
    for day in days:
        r = k.digest.run(day, force_rules=args.rules)
        print(f"{r['day']}: {r['new']} new message(s) via {r['method']} -> {r['facts']} facts, {r['notes']} notes, "
              f"{r['people']} people, {r['tasks']} tasks")
        if r["summary"]:
            print(f"  {r['summary']}")
    print(f"chat logs: {k.journal.dir}")


def cmd_hub(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("kairos.hub.app:app", host=args.host, port=args.port, log_level="info")


def cmd_mcp(args: argparse.Namespace) -> None:
    from kairos.mcp_server import main

    main()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["pet"]:  # the pet has its own options (kairos pet --help)
        from kairos.pet.app import main as pet_main

        return pet_main(argv[1:])
    p = argparse.ArgumentParser(prog="kairos")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--wipe", action="store_true")
    s.add_argument("--weeks", type=int, default=3)
    s.add_argument("--fresh", action="store_true", help="no demo people/places/facts/history")
    s.add_argument("--name", help="your display name (with --fresh)")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("chat")
    s.add_argument("--device")
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("replay")
    s.add_argument("scenario")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--hub", help="hub base URL, e.g. http://localhost:8000 (default: write to graph directly)")
    s.set_defaults(fn=cmd_replay)
    s = sub.add_parser("hub")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_hub)
    s = sub.add_parser("mcp")
    s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("llm-check", help="test the OpenRouter key/model (no graph needed)")
    s.set_defaults(fn=cmd_llm_check)
    s = sub.add_parser("doctor", help="check packages, .env, FalkorDB, every Cypher query, seeding")
    s.add_argument("--llm", action="store_true", help="also make one real OpenRouter call")
    s.add_argument("--web", action="store_true", help="also run one real web search + page read")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("digest", help="file a day of conversation (from the daily chat log) into the memory graph")
    s.add_argument("--day", default="today", help="today | yesterday | YYYY-MM-DD")
    s.add_argument("--pending", action="store_true", help="every finished day not digested yet")
    s.add_argument("--rules", action="store_true", help="offline rules instead of the digest model")
    s.set_defaults(fn=cmd_digest)
    sub.add_parser("pet", help="desktop pet: status bubbles above the taskbar, click it to talk (kairos pet --help)")
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Exception as exc:
        if "ConnectionError" not in type(exc).__name__:
            raise
        sys.exit(f"Cannot reach FalkorDB ({exc}). Start it with: docker compose up -d falkordb")


if __name__ == "__main__":
    main()
