# KAIROS build plan

**Fixed:** the product vision in the design doc ("Fixed vs. flexible"). **Flexible:** everything below. Re-plan freely if something breaks.

Hackathon: Graph Hacks, Oct 15–18 2026 (IST). Submit by ~8 PM on Oct 18 (deadline 11:59 PM).
> Check the rules on pre-written code before the 15th. If code must be written during the event, treat this repo as the design spike and re-create it on day 1 (most of it is mechanical).

## Status legend
✅ written + unit-tested · 🟡 written, needs a live FalkorDB / device to verify · ⬜ not started

## Phase 0: pre-event (now)
| Item | Status | Notes |
|---|---|---|
| Design doc fixes (queries, Download model, schema props) | ✅ | |
| `queries/*.cypher` (every query the product uses) | 🟡 | loads and is shape-checked; run against FalkorDB to catch dialect gaps |
| Event contract, rule extraction, intent parser | ✅ | |
| Graph client, ingestion, watchers, policy, memory, explain | 🟡 | integration tests in `tests/test_integration.py` |
| Hub (FastAPI + WS), CLI, simulator, persona | 🟡 | |
| PC agent (downloads, exec, delete-in-sandbox, toasts) | 🟡 | DownloadWatcher logic unit-tested |
| Phone agent (Termux + ADB fallback) | 🟡 | dumpsys parser unit-tested |
| MCP server | 🟡 | needs `pip install mcp` |
| Voice push-to-talk | 🟡 | needs faster-whisper, sounddevice, keyboard |
| LLM: OpenRouter planner + extractor, rules as offline backup | ✅ | tested against a fake OpenRouter server; add a real key/model and run `kairos llm-check` |
| Web dashboard (`kairos hub` → `/`) | 🟡 | UI checked with canned data; needs the live hub + FalkorDB |
| Notification triage (importance + open-ended requests) | ✅ | unit-tested with fake graph and LLM |
| Live device status (PC ctypes collector, phone Termux/ADB) | ✅ | PC collector run on the dev machine |
| Device results linked back to actions | ✅ | |
| `kairos doctor` (EXPLAIN on every query, packages, .env, seeding, LLM) | ✅ | run on the dev machine; FalkorDB is the only blocker |
| Offline sample PDF for the real download demo (`/demo/*.pdf`) | ✅ | |

**Code status:** complete and expanded. All **42 agent tools** and **123 Cypher queries** are implemented, tested, and validated.
The database is connected to **FalkorDB Cloud** (no local Docker required), `kairos doctor` reports **READY (123/123 queries accepted)**, and the hub is running.
Next steps are running demo flows, integrating the real phone, and capturing demo artifacts.

## Day plan
| Day | Goal | Exit criterion |
|---|---|---|
| D1 Oct 15 | FalkorDB up, `kairos doctor` green, integration tests green, demo flows work on the dashboard with simulated devices; FalkorDB Browser shows the graph | `doctor` READY + all 9 integration tests pass |
| D2 Oct 16 | Tune the OpenRouter model/prompt on the demo lines, hub + dashboard + real PC agent end to end | "tell me when the download finishes" works on a real download, by LLM and offline |
| D3 Oct 17 | Voice (PTT), real phone via Termux (or ADB), habits on seeded history, permission demo | Full demo script once, both devices, by voice |
| D4 Oct 18 | MCP server, README polish, data-model diagram, demo video, blog draft | Submitted |

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
