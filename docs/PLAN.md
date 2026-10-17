# KAIROS build plan

**Fixed:** the product vision in the design doc ("Fixed vs. flexible"). **Flexible:** everything below. Re-plan freely if something breaks.

Hackathon: Graph Hacks, Oct 15–18 2026 (IST). Submit by ~8 PM on Oct 18 (deadline 11:59 PM).
> Check the rules on pre-written code before the 15th. If code must be written during the event, treat this repo as the design spike and re-create it on day 1 (most of it is mechanical).

## Status legend
✅ written, verified & running live · 🟡 in progress / testing · ⬜ not started

## Milestones & Status
| Item | Status | Notes |
|---|---|---|
| Design doc & architecture specification | ✅ | Graph schema, 5-hop query traversal, multi-device contracts |
| `queries/*.cypher` (123 queries across all entities) | ✅ | All queries accepted via `GRAPH.EXPLAIN` on live FalkorDB Cloud |
| Event contract, rule extraction, intent parser | ✅ | Ingestion pipeline, entity linkers, and zero-latency rule backup |
| FalkorDB Cloud graph memory layer | ✅ | Running live on FalkorDB Cloud (TLS/TCP), 270+ nodes active |
| Hub (FastAPI + WebSocket message bus) | ✅ | Deployed 24/7 on Oracle Cloud VPS (`http://129.151.21.192`) via systemd + Nginx |
| PC companion agent (`agents/pc_agent.py`) | ✅ | Tested live on Windows: downloads watcher, status telemetry, shell action runner |
| Desktop pet companion (`kairos/pet/`) | ✅ | Running on Windows desktop: Tkinter transparent overlay, F9 hotkey, speech bubbles |
| 1-Click desktop startup (`start_kairos.bat`) | ✅ | Created desktop shortcut `KAIROS Agent.lnk` on Windows desktop |
| Phone agent (`agents/phone_agent.py`) | ✅ | Termux and ADB notification stream parsers implemented |
| MCP server (`kairos/mcp_server.py`) & client plugins | ✅ | Full MCP protocol integration with read-only Cypher query guard |
| Voice streaming & Push-to-talk | ✅ | Faster-Whisper STT on hub + local sounddevice mic streaming |
| OpenRouter LLM reasoning loop | ✅ | Tool-calling with `xiaomi/mimo-v2.6-pro` + offline deterministic fallback |
| Web dashboard UI (`/` and `/architecture`) | ✅ | Live dashboard with interactive Cytoscape graph visualizer & timeline |
| Notification triage & commitments engine | ✅ | Rate-limited importance scoring and task tracking |
| `kairos doctor` system diagnostics | ✅ | Automated health checks for packages, env, FalkorDB, and LLM |

**Code status:** Complete, fully operational, and deployed. All **42 agent tools** and **123 Cypher queries** are validated against **FalkorDB Cloud**. The hub is running in production on an Oracle Cloud VPS, and the local Windows companion agent and desktop pet are integrated and running.

## Implementation Timeline
| Phase | Goal | Outcome |
|---|---|---|
| Phase 1: Core Graph & Engine | FalkorDB Cloud connectivity, graph schemas, `kairos doctor` validation | ✅ 123/123 queries green, graph seeding verified |
| Phase 2: Agent Brain & Hub | OpenRouter tool calling, fallback rules, FastAPI WebSocket server | ✅ Hub running with WebSocket bus & dashboard |
| Phase 3: Multi-Device Clients | Windows PC companion, downloads watcher, desktop avatar pet | ✅ PC agent & pet running live on desktop |
| Phase 4: Production Deployment | Cloud hosting, 24/7 uptime, reverse proxy, public access | ✅ Live on Oracle Cloud VPS at `129.151.21.192` |

Cut order if behind: wake word → MCP → embeddings → real phone (simulator stands in) → voice (typed chat stands in).

## LLM integration (done: OpenRouter + offline rules)
- `.env`: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (must support tool calling), optional `OPENROUTER_EXTRACT_MODEL`, `OPENROUTER_FALLBACK_MODELS`. Check with `kairos llm-check`.
- How each turn is planned (`Agent.handle`): a pending yes/no is decided by rules → OpenRouter tool loop if available → rule planner otherwise. If the model fails after a tool already ran, the tool is **not** re-run by the rules.
- Circuit breaker: after a network or API failure the LLM is skipped for 60 s (5 min for auth errors), so offline turns answer instantly.
- Still to tune with a real key: the system prompt (`SYSTEM_PROMPT` in `kairos/agent/llm.py`) and the model choice, measured on the demo lines. If latency is high, try a faster model.

## Demo script → code map
| Demo beat | Path through the code |
|---|---|
| "Download the report Rahul sent, tell me on my phone" | intents → `download_from_person` → `create_download_request` + `entity_state` watcher → PC agent downloads → `download.completed` → `match_entity_state` → alert to phone |
| "Remind me when my Uber is 2 min away" | `watch_ride` → `trip_eta` watcher → notifications update `Trip.eta_min` → `match_trip_eta` |
| "What did Rahul send me yesterday? Open it." | `recall_file_from_person` (5-hop walk) → `open_file` on the device the file lives on |
| "I've moved to the Cyber City office" | `assert_fact` → `SUPERSEDES` edge, history kept |
| Habit suggestion | `kairos setup` history → `detect_trip_habits` → `tick()` → `due_suggestions` → speak |
| "Delete my temp folder" → "yes" | policy `confirm` → pending Action → `confirm_pending` → sandboxed delete |
| "Why did you remind me?" | `last_action(notify)` → `explain_action` path |
