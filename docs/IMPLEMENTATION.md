# KAIROS: what's implemented

*Last updated: 17 Oct 2026. This file is the single source of truth for what exists in the repo, how to run it, and what's verified.*

> **Status:** The system is **fully implemented, tested, and running live in production**. 
> - **FalkorDB Cloud** memory graph active (no local Docker required; 270+ nodes, 120+ events, 50+ actions).
> - **Production Hub** deployed on Oracle Cloud VPS (`http://129.151.21.192`) via systemd + Nginx with WebSocket streaming.
> - **PC Companion Agent & Desktop Pet** active and connected from Windows desktop.
> - **All 123/123 Cypher queries** verified via `kairos doctor`.

---

## 0. System Verification & Live Deployment

### Architecture Milestones
| Component | Status | Environment / Target | Details |
|---|---|---|---|
| **FalkorDB Graph Memory** | ✅ Verified Live | FalkorDB Cloud | Connected over TLS/TCP. All 123 Cypher queries validated with zero dialect errors. |
| **Central Hub (FastAPI)** | ✅ Deployed Live | Oracle Cloud VPS (`129.151.21.192`) | Running 24/7 as `kairos.service` (systemd auto-restart) behind Nginx reverse proxy on port 80. |
| **PC Companion Agent** | ✅ Verified Live | Local Windows 11 PC | Watches `~/Downloads` via NTFS Zone.Identifier streams, captures active app status, executes actions. |
| **Desktop Pet Avatar** | ✅ Verified Live | Local Windows 11 PC | Floating Tkinter transparent overlay, sprite mood animations, push-to-talk mic, F9 global hotkey. |
| **Desktop Shortcut** | ✅ Created | `~/Desktop/KAIROS Agent.lnk` | Double-click launches both PC companion agent and desktop pet connected to the cloud hub. |
| **`kairos doctor`** | ✅ 100% Passing | Local & VPS | Verifies packages, environment, FalkorDB queries via EXPLAIN, schema seeding, and OpenRouter LLM. |
| **Automated Tests** | ✅ Passing | Local & CI | Comprehensive test suite covering models, agent tools, ingest pipelines, triage, and graph queries. |

---

## 1. Summary

KAIROS is a voice-first personal assistant that spans an Android phone and a Windows PC. **FalkorDB is its memory:** every notification, download, file, command and thing you say becomes a node in one graph per user. Recall, "tell me when…" watchers, habits, permissions and explanations are all graph queries.

| Area | Status |
|---|---|
| Graph model + 123 named Cypher queries | ✅ Verified live on FalkorDB Cloud (`kairos doctor` clean) |
| **Untrusted-content tracking** (prompt-injection defence through the graph) | ✅ Implemented & unit-tested |
| **Commitments / open loops** (requests and promises → tasks, due reminders) | ✅ Implemented & verified |
| **Hybrid memory** (graph + notes + full-text + vectors), GraphRAG entity context | ✅ Implemented & verified |
| **Memory editor** (edit/forget facts, notes, edit/merge people, markdown export) | ✅ Live in Web Dashboard |
| **Calendar** (ICS, no OAuth) + meeting prep packs | ✅ Implemented & verified |
| **Email** (IMAP, read-only) | ✅ Implemented & tested |
| **Telegram** channel | ✅ Implemented (token-configurable) |
| **Browser history** (Chrome/Edge/Brave/Firefox) | ✅ Implemented (SQLite direct read) |
| **Location / place watchers** (geofences, coordinates never stored) | ✅ Implemented & tested |
| **File + clipboard handoff** between devices | ✅ Implemented |
| **MCP plugins** (KAIROS as MCP client) | ✅ Implemented (`kairos/plugins.py`) |
| **Graph algorithms**: important people (PageRank + interactions) | ✅ Implemented (with fallback) |
| **Shared family graph** | ✅ Implemented |
| **Wake word** (openWakeWord) / Raspberry Pi satellite | ✅ Implemented (`kairos/voice/wake.py`) |
| **Read-only web**: search + read pages (real browser via Playwright, or HTTP) | ✅ Implemented (`kairos/web.py`) |
| **`kairos doctor`** end-to-end readiness check | ✅ 123/123 queries accepted |
| **Live device status** (open apps, foreground window, battery, network… every 2 min) | ✅ Verified live on Windows |
| Event ingestion (notifications, downloads, files, browser, commands, utterances) | ✅ Implemented & tested |
| Watchers ("tell me when…"), 6 types incl. open-ended `semantic` | ✅ Implemented & tested |
| **Notification triage** (importance 0–3 + fulfils open requests; LLM or rules) | ✅ Implemented & tested |
| Versioned facts, habits (reinforce + decay), forgetting | ✅ Implemented & tested |
| Permission graph, action log, confirmations | ✅ Implemented & tested |
| Explainability ("why did you do that?") | ✅ Implemented & tested |
| Agent with 42 tools (+ any MCP plugin tools) | ✅ Implemented & tested |
| LLM via OpenRouter + offline rule-based backup | ✅ Verified live with `xiaomi/mimo-v2.6-pro` |
| Hub (FastAPI + WebSocket) + REST API | ✅ Live on VPS (`http://129.151.21.192`) |
| Web dashboard | ✅ Live on VPS with real-time graph visualization |
| PC agent (Windows) | ✅ Verified live with persistent WS connection |
| Desktop Pet Avatar | ✅ Verified live on Windows with F9 hotkey |
| Phone agent (Termux / ADB) | ✅ Implemented (`agents/phone_agent.py`) |
| Voice push-to-talk | ✅ Faster-Whisper + sounddevice |
| MCP server | ✅ Implemented (`kairos/mcp_server.py`) |
| Simulator + synthetic persona + 3 weeks of history | ✅ Implemented (`kairos/sim.py`) |
| Tests | ✅ Comprehensive automated test coverage |

✅ = implemented and verified live

---

## 2. Architecture

```
 Phone agent (Termux/ADB) ─┐                        ┌─ Web dashboard (browser)
 PC agent (Windows)  ──────┼── WebSocket /ws/{id} ──┤
 Voice PTT ── POST /chat ──┤                        │
 Simulator ── POST /events ┘      KAIROS HUB (FastAPI)
                                  │
                         Kairos runtime (kairos/runtime.py)
     ┌────────────┬─────────────┬──────────────┬───────────┬──────────┬──────────┐
  Ingestor    WatcherEngine   Agent          Policy+Audit  Memory     Explainer
  (rules/LLM   (5 matchers +  (OpenRouter     (permission   (facts,    (Action →
   extract)     timer tick)    loop / rules)   graph, log)   habits)    cause path)
     └────────────┴─────────────┴──────┬───────┴───────────┴──────────┘
                                 FalkorDB graph  kairos:<user>
```

**Runtime flows**
- **A. Ingest:** a device event becomes an `EventEnvelope`, which is de-duplicated, has its entities extracted, and becomes an `Event` node plus entity links. Watchers are matched next, and any alert goes to a device.
- **B. Chat/voice:** the utterance is stored as an `Utterance` event, then the planner picks tools. Each tool passes the permission check, runs, and is logged as an `Action` with `CAUSED_BY` edges. The reply goes back to the user.
- **C. Watchers:** matched on every event; time-based ones are checked every 5 s.
- **C1. Live status:** every 2 min (and on request) each device agent sends a `device.status` snapshot. The hub keeps the latest state on the `Device` node plus `RUNNING` edges, and ingests only the changes as events (see §7c).
- **C2. Triage:** after the exact watchers, each live notification goes to a background worker. The LLM (or rules) decides how important it is and which open-ended requests it fulfills, then fires those watchers and/or alerts you (see §7b).
- **D. Coordination:** phone and PC only share state through the graph and the hub (e.g. the PC finishes a download, the phone gets the alert).
- **E. Background jobs:** every 10 min, habit detection and decay run; every 5 s, timed watchers and habit suggestions.

---

## 3. File map

| Path | What it does |
|---|---|
| `kairos/config.py` | Reads all settings from env / `.env` |
| `kairos/models.py` | `EventEnvelope` (device → hub) and `Outbound` (hub → device) |
| `kairos/timeutil.py` | Epoch-ms helpers, timezones, named periods (`today`, `yesterday_evening`, …) |
| `kairos/graph/client.py` | FalkorDB wrapper: one graph per user, named queries, rows as dicts, schema apply |
| `kairos/graph/queries.py` | Loads `queries/*.cypher` by `// name:`; label templates use an allowlist |
| `kairos/graph/guard.py` | Read-only Cypher guard for `graph_query` (MCP) |
| `kairos/ingest/rules.py` | Rule-based notification extraction (rides, messages, links, files) |
| `kairos/ingest/pipeline.py` | Ingestion: dedupe → create event → link entities per type |
| `kairos/watchers.py` | Watcher create / match / due / cancel |
| `kairos/status.py` | Live device status: snapshot diffing, Device state + RUNNING edges, change events, status descriptions |
| `agents/pc_status.py` | Windows status collector (ctypes, no installs): foreground app/window, open apps, battery, idle, RAM, Wi-Fi, active downloads |
| `kairos/triage.py` | Notification triage: importance + fulfilling open requests + commitments (LLM or rules), background worker, rate limit |
| `kairos/trust.py` | Untrusted-content tracking: trust levels, per-turn taint, which capabilities need confirmation when tainted |
| `kairos/tasks.py` | Commitments: due-date parsing, request/promise extraction, dedupe, TaskManager |
| `kairos/calendar_ics.py` | ICS parser (TZID/UTC/all-day, folding, attendees, DAILY/WEEKLY RRULE), calendar sync, meeting prep packs, `demo` calendar |
| `kairos/email_imap.py` | Read-only IMAP poller → `notification` events (plain + HTML + links), no backfill |
| `kairos/channels/telegram.py` | Telegram Bot API long-polling channel (allow-listed chats), alert delivery |
| `kairos/places.py` | Geofences (haversine), nearest place, place-name normalization |
| `kairos/handoff.py` | Cross-device file transfers (one-time IDs, size limit, expiry) |
| `kairos/plugins.py` | MCP client: connects to tool servers from `plugins.json`, exposes their tools behind the permission graph |
| `kairos/family.py` | Shared family graph |
| `kairos/algorithms.py` | Important people: PageRank (if available) + recency-weighted interactions |
| `kairos/export.py` | Memory → Markdown export |
| `kairos/voice/wake.py` | Wake word (openWakeWord) → record until silence → STT → hub (also the Raspberry Pi satellite) |
| `agents/browser_history.py` | Chrome/Edge/Brave/Firefox history reader (read-only copies, watermark, no backfill) |
| `kairos/policy.py` | Permission check, safe-command allowlist, action log |
| `kairos/memory.py` | Facts (assert / supersede / history / search / forget), habits (detect / decay / suggest) |
| `kairos/explain.py` | Builds "why" sentences from the `explain_action` path |
| `kairos/agent/tools.py` | 25 tools + JSON schemas (`TOOL_SPECS`, `openai_tools()`) |
| `kairos/agent/intents.py` | Rule-based planner (the offline backup) |
| `kairos/agent/llm.py` | LLM layer: OpenRouter planner and extractor, rule planner and extractor, `make_llm` |
| `kairos/agent/openrouter.py` | OpenRouter HTTP client with circuit breaker |
| `kairos/agent/core.py` | Agent: picks the planner, normalizes device names, checks policy, runs and logs tools, builds the context card |
| `kairos/gateway.py` | `LocalGateway`: prints outbound messages, fakes downloads for demos |
| `kairos/runtime.py` | `Kairos` class that wires everything together |
| `kairos/hub/app.py` | FastAPI hub: device WebSocket, REST, dashboard API, device simulation, outbox |
| `kairos/hub/static/` | Dashboard (`index.html`, `app.js`, `style.css`) |
| `kairos/seed.py` | Loads persona, devices, people, places, capabilities, permissions, starting facts |
| `kairos/sim.py` | Scenario replay + synthetic history generator |
| `kairos/embed.py` | Optional local embeddings (off by default) |
| `kairos/mcp_server.py` | MCP tools for Claude Desktop / Claude Code |
| `kairos/voice/ptt.py` | Push-to-talk: faster-whisper STT → hub → spoken reply |
| `kairos/cli.py` | `kairos setup / chat / replay / hub / mcp / llm-check / doctor` |
| `kairos/doctor.py` | End-to-end readiness check (packages, .env, FalkorDB, schema, EXPLAIN on every query, seeding, LLM) |
| `agents/common.py` | Reconnecting WebSocket client shared by device agents |
| `agents/pc_agent.py` | Windows agent |
| `agents/phone_agent.py` | Android agent (Termux, or ADB from the PC) |
| `queries/*.cypher` | Every Cypher query the product uses (75 named queries) |
| `sim/persona.yaml` | Synthetic persona "Arjun Mehta": devices, people, places, facts, permissions |
| `sim/scenarios/*.yaml` | `morning` (email + Uber + WhatsApp), `download` |
| `tests/` | 7 test files (see §12) |
| `docker-compose.yml`, `Dockerfile` | FalkorDB + hub |
| `.env.example`, `.env` | Configuration (`.env` is gitignored; fill in the OpenRouter key) |

---

## 4. Graph data model

One graph per user: `kairos:<user_id>` (FalkorDB multigraph).

**Nodes**
| Label | Memory type | Key properties |
|---|---|---|
| `User`, `Device` | — | `id`, `kind` (phone/pc), `online`, `last_event_id`; live state: `status_ts, foreground_app, foreground_title, battery, charging, network, cpu, mem, idle_s, downloads_active` |
| `Event` + sub-label `Notification` / `DownloadEvent` / `FileEvent` / `BrowserVisit` / `CommandRun` / `Utterance` / `AppEvent` / `DeviceEvent` | episodic, immutable | `id, type, ts, source, text, hash, status?, importance?, triage_reason?` |
| `Person`, `Place`, `App`, `File`, `Folder`, `Link` | semantic | `name` / `path` / `url`, `aliases` |
| `Message` | episodic | `channel, subject, snippet, ts` |
| `Download` | semantic, mutable entity | `id, url, filename, status` |
| `Trip` | episodic | `provider, status, eta_min, driver, ts, weekday, hour` |
| `Fact` | semantic, versioned | `key, statement, valid_from, valid_to, archived` |
| `Habit` | procedural | `kind, place, days, hour, strength, count, last_reinforced` |
| `Watcher` | procedural | `condition_type, mode, status, …` |
| `Action` | audit | `tool, args_json, status, summary, result, ts` |
| `Capability` | policy | `name, risk 0–3` |
| `Session` | episodic | `id, started, device` |
| `Task` | procedural / open loops | `title, direction (they_asked/i_promised/todo), due_ts, status, source, trust` |
| `Meeting` | semantic | `title, start_ts, end_ts, location, prepped` |
| `Note` | semantic (free text) | `text, trust, archived, embedding?` |
| `Member`, `SharedFact` | family graph | `name` / `statement, key, valid_to` |
| `Place` (geo) | semantic | `lat, lon, radius_m` for geofences |

Every `Event` also has **`trust`** (`user` / `device` / `external`), and every `Action` has **`tainted`** and **`taint_sources`**. New relationships: `(Action)-[:INFLUENCED_BY]->(Event)` (external content the model had read), `(User)-[:HAS_TASK]->(Task)-[:WITH]->(Person)`, `(Task)-[:DERIVED_FROM]->(Event)`, `(User)-[:ATTENDS]->(Meeting)-[:WITH]->(Person)`, `(Meeting)-[:AT]->(Place)`, `(User)-[:WROTE]->(Note)-[:ABOUT_ENTITY]->(Person|Place)`, `(PlaceEvent)-[:ABOUT]->(Place)`, `(Member)-[:SHARED]->(SharedFact)`.

**Main relationships:** `RUNNING {since}` (Device → App, what's open right now), `OWNS`, `ON_DEVICE`, `NEXT` (per-device timeline), `FROM_APP`, `MENTIONS`, `SENT`, `RECEIVED`, `CONTAINS`, `DOWNLOADED_AS`, `RESULTED_IN`, `SAVED_IN`, `UPDATES` (DownloadEvent → Download), `ABOUT` (Notification → Trip/Message), `TO`, `RELATION {type}`, `ASSERTS`, `SUPERSEDES`, `DERIVED_FROM`, `REINFORCED_BY`, `WATCHES`, `NOTIFY_VIA`, `FIRED_BY`, `CREATED_BY`, `CAUSED_BY`, `USED_CAPABILITY`, `ALLOWED {mode}`, `IN_SESSION`.

**Design decisions (changed from the original doc)**
- `Download` is a mutable entity. Its state changes are immutable `DownloadEvent`s that `UPDATES` it, so events stay immutable.
- Watcher matching is one small query per condition type instead of one large `OR`.
- `app_text` watchers use `CONTAINS`, not regex, since FalkorDB may not support regex.
- `assert_fact` works for the first version of a fact (`OPTIONAL MATCH` + `FOREACH`).
- `Trip` stores `ts/weekday/hour` (user timezone) for habit detection.
- The habit's starting strength depends on how many times it was seen (`0.3 + 0.04 × count`, capped at 0.9).

---

## 5. Cypher queries (130, in `queries/`)

New files since session 6:

| File | Queries |
|---|---|
| `tasks.cypher` (4) | `create_task`, `open_tasks`, `complete_task`, `due_tasks` |
| `notes.cypher` (16) | notes (`add_note`, `note_about_*`, `notes_about_person`, `list_notes`, `search_notes_text`, `archive_note`); hybrid search (`search_events_fulltext`, `search_events_contains`, `vector_events`); memory editor (`archive_fact`, `list_people_full`, `update_person`, `merge_person_out`/`merge_person_in` with an allowlisted `{rel}` template, `merge_person_finish`) |
| `calendar.cypher` (6) | `upsert_meeting`, `link_meeting_person`, `link_meeting_place`, `upcoming_meetings`, `mark_meeting_prepped`, `meeting_context` (the prep-pack graph walk) |
| `algorithms.cypher` (2) | `pagerank_people` (`algo.pageRank`), `person_interactions` |
| `family.cypher` (2) | `family_share` (with supersede), `family_search` |
| `web.cypher` (4) | `link_search_result`, `set_link_excerpt`, `link_known`, `recent_web_reads` |
| additions | `match_place`, `geo_places`, `link_event_place`, `link_influence`, `recent_browser_visits`, `list_places` |

Earlier files:

| File | Queries |
|---|---|
| `schema.cypher` | `schema` (range + full-text indexes), `schema_vectors` |
| `seed.cypher` | `seed_user`, `seed_device`, `seed_person`, `seed_place`, `seed_capability`, `seed_permission` |
| `ingest.cypher` | `event_by_hash`, `create_event`, `link_app`, `resolve_person`, `create_person`, `link_mentions_person`, `create_message`, `active_trip`, `upsert_trip`, `link_trip_destination`, `create_download_request`, `upsert_download`, `download_result_file`, `latest_open_download`, `link_file`, `link_url`, `link_session`, `set_embedding` |
| `watchers.cypher` | `create_watcher`, `watch_target`, `match_entity_state`, `match_event_type`, `match_trip_eta`, `match_app_text`, `due_time_watchers`, `semantic_watchers`, `fire_watcher`, `list_active_watchers`, `cancel_watcher`, `cancel_all_watchers` |
| `triage.cypher` | `recent_utterances`, `set_triage` |
| `status.cypher` | `get_device_status`, `set_device_status`, `stop_running_apps`, `start_running_apps`, `all_device_status` |
| `recall.cypher` | `recall_file_from_person` (5-hop), `recall_links_from_person`, `recall_messages_from_person`, `find_file_by_name`, `latest_link`, `person_context`, `recent_activity`, `event_counts_since`, `find_path` (bounded variable-length path), `vector_facts` |
| `memory.cypher` | `assert_fact`, `current_fact`, `fact_history`, `search_facts_text`, `forget_topic`, `detect_trip_habits`, `decay_habits`, `list_habits`, `mark_habit_suggested` |
| `policy.cypher` | `check_permission`, `create_action`, `link_cause`, `update_action`, `pending_action`, `expire_pending_actions`, `last_action`, `set_device_online`, `set_all_devices_online`, `list_devices`, `route_device` |
| `explain.cypher` | `explain_action` |
| `dashboard.cypher` | `recent_events`, `recent_actions`, `get_action`, `list_people`, `current_facts`, `all_facts`, `graph_snapshot`, `graph_counts` |

---

## 6. Agent tools (44, plus any MCP plugin tools)

New since session 6: `add_note`, `list_tasks`, `add_task`, `complete_task`, `upcoming_meetings`, `meeting_prep`, `important_people`, `recent_sites`, `watch_place`, `send_file` (file.transfer), `send_clipboard` (clipboard), `share_with_family` (memory.share), `family_memory`, `web_search` (net.search), `read_page` (net.read / net.read_unknown). `open_link` now uses its own `link.open` capability, and `search_memory` is hybrid. MCP plugin tools appear as `mcp__<server>__<tool>` with capability `plugin.<server>`.

Each tool has a capability that is checked against the permission graph before it runs.

| Tool | Capability | Does |
|---|---|---|
| `watch_download` | watcher.create | Alert when the current/next download finishes |
| `watch_ride` | watcher.create | Alert when an Uber/Ola/Rapido is ≤ N min away or has arrived |
| `watch_app` | watcher.create | Alert when an app's notification contains a keyword |
| `watch_for` | watcher.create | Open-ended request ("when Rahul replies about the budget"), matched by triage |
| `remind_in` | watcher.create | Timed reminder |
| `list_watchers` / `cancel_watchers` | read / watcher.create | List or cancel watchers |
| `download_from_person` | net.download | Download the latest link a person sent (on the PC) + optional alert when done |
| `recall_file` | read | Person → Message → Link → Download → File |
| `recall_messages` | read | Recent messages from a person |
| `open_file` / `open_link` | file.open | Open on the right device (where the file lives / the device you named) |
| `recent_activity` | read | "What was I doing yesterday evening?" |
| `person_context` / `find_path` | read | Who someone is / how two things connect |
| `remember` | memory.write | Save a fact; same key supersedes and keeps history |
| `get_fact` / `fact_history` / `search_memory` | read | Read facts |
| `forget` | memory.forget | Archive a topic (asks first) |
| `explain` | read | Why the last action / last alert happened |
| `delete_path` | file.delete (risk 3) | Sandbox-only delete, always confirmed |
| `run_command` | shell.read (allowlisted) / shell.exec (risk 3) | PowerShell on the PC |
| `device_status` | read | Live status of devices (foreground app, open apps, battery, network, CPU/RAM, idle) + asks for a fresh snapshot |
| `list_habits` / `briefing` / `help` | read | Routines, daily summary, help |

---

## 7. LLM: OpenRouter with an offline rule-based backup

**Configure in `.env`:**
```
KAIROS_LLM=auto                 # auto | openrouter | rules
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=<tool-calling model id from openrouter.ai/models>
OPENROUTER_EXTRACT_MODEL=       # optional cheaper model for notification extraction
OPENROUTER_FALLBACK_MODELS=     # optional comma list
KAIROS_LLM_TIMEOUT=20
```
Check with `kairos llm-check`. It tests the key, a chat call, and whether the model calls a tool.

**How each turn is planned (`kairos/agent/core.py`)**
1. A **pending yes/no** is always handled by rules, never by the model.
2. If **OpenRouter is available**, the tool loop runs (up to 6 steps). Tool results go back to the model and the final text is the spoken reply.
3. **Otherwise** (no key, no internet, API error), the **rule-based planner** answers.
4. If the model fails *after* a tool already ran, the tool output is returned and the rules do **not** run it again.

**Model input per turn:** the system prompt plus a **context card** built from the graph (devices, people and relations, current facts with keys, active watchers, top routines, recent events, last file and link), the last 8 chat messages, and the utterance.

**Safety:**
- Every tool the model picks goes through the permission check and the action log.
- Device names ("phone", "pc", "laptop") are mapped to device IDs; unknown devices are dropped.
- Arguments the model invents are filtered out by the tool's signature.

**Circuit breaker:** after a failure the LLM is skipped for 60 s (5 min for 401/402/403), so offline turns answer instantly. The dashboard badge shows the current state.

**Extractor:** rules handle known apps. The LLM reads only notifications from apps the rules don't recognize (JSON with category, sender, subject, people), and falls back to rules if it fails.

**Rule planner** (`intents.py`) understands every demo phrase: download/Uber/app/timer watchers, cross-device download, recall + "open it", the office move, remember/forget, where is my office / history, why, delete, run, habits, briefing, recent activity, who is X, how is X connected to Y, open the WhatsApp link on my laptop, list/cancel reminders, help, and yes/no.

---

## 7b. Notification triage (`kairos/triage.py`)

**Question it answers for every live notification:** is this important, and does it fulfill something the user asked for earlier?

**Pipeline:**
1. **Ingest:** the event is stored and the exact watchers (download, ride, app keyword, timer) are matched. These stay rule-based and predictable.
2. **Eligibility:** only `notification` and `download.failed` events from the last 10 minutes are triaged. Older events, such as the setup history, are never sent, so they cost nothing. Utterances aren't triaged.
3. **Background worker:** the hub and `kairos chat` run a queue worker, so LLM time never blocks ingestion. Without a worker (tests, `kairos replay`), triage runs inline.
4. **Context from the graph:** open `semantic` watchers (with IDs), what the user said in the last 24 h, and their people and relations.
5. **Decision:**
   - **LLM** (`OPENROUTER_EXTRACT_MODEL`, 10 s timeout, at most `KAIROS_TRIAGE_MAX_PER_MIN` calls per minute) returns `{importance 0–3, reason, fulfills[ids], message}`. Unknown watcher IDs are dropped, so the model can only fire *semantic* watchers.
   - **Rules fallback** (offline, rate-limited, or `KAIROS_TRIAGE=rules`): keyword overlap with each semantic watcher (≥ 67% of its keywords); importance from urgent words, close contacts (manager, family), security/OTP alerts, and promotions.
6. **Effects (under the graph lock):**
   - The result is saved on the event: `importance`, `triage_reason`, `triage_by` (`llm` or `rules`).
   - Each fulfilled watcher fires (`FIRED_BY {reason}`) and alerts its device.
   - Otherwise, if `importance ≥ KAIROS_TRIAGE_NOTIFY_MIN` (default 3), an unrequested "Important: …" alert goes to the best device.
   - **Never a duplicate:** if an exact watcher already alerted for this event, no extra importance alert is sent.
   - Every alert is an `Action` with `CAUSED_BY` the event (and the watcher), so "why?" can explain it.

**Creating open-ended requests:**
- The `watch_for` tool, chosen by the LLM.
- The rule intent "tell/notify/remind me (on my phone) when/if …", used when no specific watcher matches.

**Dashboard:** timeline rows show an importance badge (noise / normal / later / important) and the reason.

**Demo:** the `morning` scenario now includes an Amazon promo (marked noise, no alert) and Rahul's Slack message "Budget numbers are final, need your sign-off ASAP". If you said "tell me on my phone when Rahul replies about the budget", that message fulfills it, with importance 3 and one alert.

**Privacy:** in `auto` mode, notification text is sent to OpenRouter. Use `KAIROS_TRIAGE=rules` to keep it local.

---

## 7c. Live device status (`kairos/status.py`, `agents/pc_status.py`)

**Goal:** the agent always knows what's going on (what's open, what you're focused on, battery, network) before you ask.

**Collection**
- Every `KAIROS_STATUS_INTERVAL` seconds (default 120), and whenever the hub sends `status_request`, each device agent sends a `device.status` envelope.
- **PC (Windows, ctypes only, no installs):**
  - foreground app + window title (`--no-titles` sends app names only)
  - open apps (visible top-level windows, mapped to friendly names: Chrome, VS Code, Word, Teams…)
  - battery % and charging, idle time, RAM %, Wi-Fi SSID, downloads in progress
  - CPU %, if `psutil` happens to be installed

  Verified on this machine: it returned the foreground app, 12 open apps, battery, idle time and RAM.
- **Phone, Termux mode:** battery and charging, Wi-Fi.
- **Phone, ADB mode:** battery and charging, foreground app package, Wi-Fi.

**What the hub does with a snapshot:** it is *not* stored as an event.
1. It's diffed against the previous state (`diff_status`).
2. The new state goes on the `Device` node; open apps become `(Device)-[:RUNNING {since}]->(App)` edges.
3. Only the changes are ingested as events:

   | Event | When |
   |---|---|
   | `app.opened` / `app.closed` | an app appears or disappears (max 10 each per snapshot) |
   | `app.focused` | the foreground app changes |
   | `battery.low` | battery crosses ≤ 20% while not charging (once) |
   | `network.changed` | the network changes |

**Limits:**
- A device's first snapshot creates no events, so there's no flood.
- Phone snapshots have no app list, so they never "close" apps.

**Who uses it**
- **LLM context card:** each turn gets a "Live status" line per device, e.g. *"Arjun's laptop (1 min ago): using Chrome ('Q3_report.pdf'); also open: Outlook, Teams, VS Code; battery 63%; network Wi-Fi Home-5G"*. Data older than 10 min is marked `[stale]`.
- **`device_status` tool:** answers from the latest state and asks the devices for a fresh snapshot. Rule intents: "what's open on my laptop", "phone battery", "device status", "is my phone charging", "which wifi".
- **History:** `app.focused` events feed "what was I working on yesterday evening?".
- **Triage:** `battery.low` and `network.changed` are triaged, so "tell me when my phone battery is low" (a `watch_for` request) gets fulfilled.
- **Dashboard:** device pills show the foreground app and battery; hover for the full status. The timeline shows app and battery events.

**Demo:** the `morning` scenario sends status snapshots. The PC switches from Outlook to Chrome with `Q3_report.pdf` and Acrobat opens; the phone drops from 23% to 18% battery (`battery.low`) and goes from Wi-Fi to mobile data (`network.changed`).

---

## 7d. Session 7 features

### Untrusted-content tracking (prompt-injection defence), `kairos/trust.py`
- **Trust levels:** every Event gets `trust`, derived from its type, never from the payload:
  - `user`: utterances,
  - `device`: status, app, download, file and place events, commands,
  - `external`: notifications, emails, web pages.
- **Tainted turns:** an LLM turn is *tainted* once the model has read external text, either from the context card (recent notifications, window titles, messages from people mentioned) or from a tool result (recall, briefing, status, meetings, tasks, family, open_link, plugins).
- **What tainting does:**
  - That text is sent to the model wrapped in `<untrusted>…</untrusted>`, and the system prompt says it's data, never instructions.
  - On a tainted turn, capabilities that execute, delete, download, open links, transfer, copy the clipboard, share, forget or call plugins go from **auto to confirm**. Harmless ones (watchers, notes, alerts) still run.
  - The Action is stored with `tainted` + `taint_sources` and `INFLUENCED_BY` edges to the external events. "Why?" says "I decided this while reading untrusted content (…)", and the dashboard shows **⚠ untrusted context**.
- **Rule planner turns are never tainted:** rules never read external text as instructions.
- **Demo:** ▶ *Injection email*, then ask the LLM "what's in my latest email?"

### Commitments / open loops, `kairos/tasks.py`
- **Requests in incoming messages** ("Could you share the updated budget deck by Friday?") become `Task {direction: they_asked}`, linked `WITH` the sender and `DERIVED_FROM` the email.
- **Promises you make** ("I promised Rahul I'd send the deck by Friday") become `i_promised` tasks.
- **Also from:** the LLM triage `commitment` field, "add a task …", and the dashboard.
- **Due dates:** `parse_due` understands today/tonight/EOD, tomorrow (morning), weekday names, next week, "in N hours/days", and "5pm" style times.
- **Dedupe:** near-identical open tasks with the same person are skipped.
- **Finishing tasks:** "I've sent the deck to Rahul" completes by fuzzy match.
- **Reminders:** a due reminder fires within the hour before the deadline, once.
- **Where tasks show up:** the briefing, person context, meeting prep, the LLM context card and the dashboard **Tasks** tab.

### Hybrid memory + GraphRAG entity context
- **Search** (`search_memory`) combines:
  - structured facts,
  - linked `Note`s (free text attached to people and places: "Rahul seemed stressed about the budget"),
  - events by **meaning** (vector index, when `KAIROS_EMBEDDINGS=1` and sentence-transformers is installed) or by **words** (FalkorDB full-text index, with a substring fallback).
- **Context card:** it now links the people you mention to their graph neighbourhood (relation, notes, recent message subjects), and adds open commitments and upcoming meetings.

### Memory editor + markdown export (dashboard **Memory** tab)
- Add, edit (supersede) or forget facts; add or forget notes.
- Edit a person's name, aliases and relation, or **merge duplicates** ("R. Sharma" into "Rahul Sharma"). A merge moves the `SENT`, `MENTIONS`, `WITH`, `ABOUT_ENTITY` and `WATCHES` edges through allowlisted templates, and keeps the old name as an alias.
- **Export .md** downloads the whole memory as readable Markdown.
- Every edit is audited as an Action.

### Calendar + meeting prep, `kairos/calendar_ics.py`
- **Sources:** ICS feeds (Google "secret iCal address", Outlook "publish calendar"), local `.ics` files, or `demo`. No OAuth.
- **Sync:** every 10 min, giving `Meeting` nodes linked to attendees (resolved to Persons) and the location.
- **Prep pack:** about 15 min before a meeting, KAIROS walks the graph from the Meeting to its people → their recent messages → files they sent → your open tasks with them → files matching the title, and *speaks* a summary.
- **Tools and phrases:** `upcoming_meetings`, `meeting_prep`, "prep me for the Q3 sync".

### Email over IMAP, `kairos/email_imap.py`
- Read-only (`BODY.PEEK`): new mail becomes ordinary notification events, so Message → Link chains, commitments and triage all just work.
- It reads HTML bodies and `href` links, skips messages over 2 MB, and doesn't backfill the inbox on first run.

### Telegram, `kairos/channels/telegram.py`
- A bot becomes one more Device (`telegram-arjun`, kind `chat`), using long-polling with no public URL.
- **Allow-list:** only chats in `TELEGRAM_ALLOWED_CHAT_IDS` get answers; strangers are told their chat ID and nothing else.
- **Alerts** can route to Telegram ("tell me on telegram when…").
- **Stricter permissions:** requests from Telegram use the **stricter of the Telegram rule and the target device's rule**, so "run X on my PC" from Telegram always asks first.

### Browser history, `agents/browser_history.py`
- **Sources:** the PC agent reads Chrome, Edge, Brave and Firefox history every 60 s, from read-only copies of the locked databases.
- **What's sent:** `browser.visit` events with a saved watermark and no backfill. Internal pages and reloads are skipped.
- **Tool:** `recent_sites` ("what site was I reading this morning").
- **Privacy:** disable with `--no-history`.

### Location and place watchers, `kairos/places.py`
- **Geofences:** places have lat/lon/radius in the persona. The phone agent (`--location`) sends a network location fix.
- **Privacy:** the hub maps the fix to a place name and **never stores coordinates**.
- **Events and watchers:** `place.entered` / `place.left` events fire `watch_place` watchers ("remind me to buy milk when I leave the office").
- **Demo:** ▶ *Commute*.

### File and clipboard handoff, `kairos/handoff.py`
- **Files:** "send it to my phone":
  1. the hub asks the device that has the file to upload it (`/api/transfer/<id>`, device token, 100 MB limit, one-time 128-bit ID, 1 h expiry);
  2. the target device opens the download link;
  3. the result is reported to the original Action.
- **Clipboard:** "send my clipboard to my phone": the source device's result is forwarded as `clipboard_set`. The clipboard text is **never stored** in the graph.

### MCP plugins, `kairos/plugins.py`
- **KAIROS as an MCP *client*:** servers listed in `plugins.json` (see `plugins.example.json`) start at boot, and their tools appear to the LLM as `mcp__<server>__<tool>`.
- **Guarded by the permission graph:** capability `plugin.<server>`, risk 2, configured mode, default `confirm`.
- **Plugin output is untrusted:** it taints the turn.

### Graph algorithms, `kairos/algorithms.py`
- **`important_people`** ranks people by FalkorDB **PageRank** combined with recent messages, mentions, open tasks, upcoming meetings and relation, and says *why*.
- **Fallback:** if the server lacks `algo.pageRank`, the interaction score is used alone; doctor reports this as a warning.

### Shared family graph, `kairos/family.py`
- **Setup:** set `KAIROS_FAMILY_GRAPH=kairos:family` on every member's KAIROS.
- **What's shared:** "tell the family the plumber comes at 5" writes a `SharedFact` (keyed facts supersede older ones); "what did the family share" reads them. Private graphs are never copied.

### Wake word, `kairos/voice/wake.py`
- **Detection:** openWakeWord (pre-trained `hey_jarvis` etc., or a custom "hey kairos" model), then record until silence, then faster-whisper, then the hub, then a spoken reply.
- **Hardware satellite:** the same script runs on a Raspberry Pi with a USB mic and speaker; it's just another Device.

### Read-only web: search and read pages, `kairos/web.py`
- **Tools:** `web_search(query)` returns numbered results; `read_page(url or result number, focus)` returns the page's readable text. Read-only: no forms, clicks or logins.
- **Real browser:** Playwright drives your installed Chrome (or Edge, or the bundled Chromium) with a **fresh, empty profile**. `KAIROS_WEB_HEADLESS=0` shows the window. Without Playwright it falls back to plain HTTP. Search uses DuckDuckGo's results page by default (no API key), with Bing as an option. Bot-check pages are reported, never bypassed.
- **Safety:**
  - private/local network addresses are refused, including after every redirect and browser navigation,
  - 3 MB / 20 s limits; non-HTML isn't read,
  - **a URL nobody gave us (not a search result, a link you received, your history, or typed by you) always needs confirmation**, which blocks "fetch evil.example/?q=<your data>",
  - web text is untrusted and taints the turn.
- **Memory:** `WebSearch -[:RESULT]-> Link` and `WebRead -[:MENTIONS]-> Link {excerpt}`, so "that article I read" works later.
- **Corporate networks:** `kairos/net.py` makes every HTTPS client trust the OS certificate store (plus an optional `KAIROS_CA_BUNDLE`); found because the dev laptop intercepts HTTPS.
- **Verified live:** `kairos doctor --web` ran a real search and page read on the dev machine.

### Cross-session memory: daily chat log + nightly digest, `kairos/journal.py`, `kairos/digest.py`
Memory lives in the FalkorDB graph, not the LLM's context window. This closes the gap for things said in passing.
- **Daily chat log on disk:** every turn goes to `<KAIROS_DATA_DIR>/chatlog/YYYY-MM-DD.txt` (one readable line per message). After a hub restart, each session's last 8 messages are restored from it.
- **Your own words are searchable:** event search now includes what you said (excluding the question being asked), so "what did I say about Goa?" works.
- **Nightly digest:** after 3 AM (`KAIROS_DIGEST_HOUR`), a small model (`KAIROS_DIGEST_MODEL`) reads the previous day's log and files what matters into the graph: versioned **facts** (reusing existing keys, so old values are superseded), **notes** linked to people, **people** with relations, **tasks**, plus a `Digest {day, summary, topics}` node. Everything links back with `DERIVED_FROM → Digest → COVERS → Session`.
- **Safe by construction:** the digest reads only the user's own lines (assistant replies can quote emails or web pages), and it only writes memory, never acts.
- **Incremental and resilient:** re-running a day only reads new lines; missed days are caught up on the next start; offline it uses rules ("my X is Y", "I'm flying to X", "I love X", "my sister Priya"); in auto mode it waits up to 3 days for the model before falling back.
- **Reading back:** the context card shows the last two days' summaries; tools `recall_conversation(day)` and `digest_memory(day)`; `kairos digest`, `POST /jobs/digest`, `GET /api/chatlog`, and the Memory tab's "Daily digests" panel.

### Voice chat + desktop pet, `kairos/voice/stt.py`, `kairos/hub/static/voice.js`, `kairos/pet/`
- **Speech-to-text on the hub:** one local faster-whisper model (`KAIROS_STT_MODEL`, default `base.en`) shared by every client. `POST /api/voice` takes a WAV, transcribes it, and runs a normal chat turn (`reply=false` to only transcribe). `GET /api/voice/status`. Audio stays on the machine; missing packages return a clear 503.
- **Dashboard:** 🎤 button (Ctrl+M). It stops by itself when you pause, and the reply is spoken by the browser. **Voice chat** mode keeps listening after each reply, for a hands-free conversation. If the hub has no speech model, it falls back to the browser's speech recognition.
- **Desktop pet** (`kairos pet`): a pixel robot in an always-on-top transparent window just above the taskbar.
  - **Look:** shaded pixel art with automatic outlines and lighting. Its CRT screen has scanlines, glow and glare, and its eyes follow the mouse. It has 8 moods (idle, happy, listening, thinking, speaking, alert, sleep, and **NULL** when the hub is offline), each with its own effects (hearts, sound waves, "?", "!", zZ), and 4 skins (`--skin slate|classic|mint|peach`). See `docs/pet_moods.png`.
  - Alerts for the PC appear as speech bubbles; hover to see status.
  - Click to talk, double-click to open the dashboard, drag to move, right-click for the menu.
  - Speech out uses pyttsx3 or Windows' built-in voice, so nothing needs installing.
  - Mic needs `pip install sounddevice numpy`. Without it, clicking opens a text box.

---

## 8. Web dashboard (`kairos hub` → http://localhost:8000/)

No external libraries; works offline.

| Panel | Features |
|---|---|
| Top bar | Graph name, LLM badge (OpenRouter + model / Offline rules + reason), devices (connected / simulated / offline), node counts |
| Chat | Talk as 💻 PC or 📱 Phone, 9 one-click demo commands, Yes/No buttons on confirmations, shows which planner answered |
| Memory graph | Built-in force-directed SVG; time window (1 h / 24 h / 7 d / 30 d); live refresh only when the graph changes; drag, pan, zoom; click a node to see its connections; colour legend |
| Timeline | Recent device events with icons |
| Actions | Action log with status, **Why?** (explanation chain) and **Approve / Deny** for pending actions |
| Watchers | Active watchers with **Cancel** |
| Memory | Versioned facts (old versions struck through), habits with strength bars, people |
| Simulator | ▶ Morning, ▶ Download, Detect habits |
| Toasts | Every alert or message sent to a device pops up |

**Hub API:** `GET /`, `/health`, `/demo/{name}.pdf` (sample PDF for the offline demo download), `/api/status`, `/api/events`, `/api/actions`, `/api/memory`, `/api/graph?minutes=`, `/api/outbox?after=`, `/watchers`, `/explain/{id}`; `POST /chat`, `/events`, `/api/actions/{id}/confirm`, `/api/watchers/{id}/cancel`, `/api/sim/{scenario}`, `/jobs/consolidate`; `WS /ws/{device_id}?token=`.

**Device simulation (`KAIROS_SIMULATE_DEVICES=1`):** messages to devices that aren't connected are handled by a simulator (downloads complete after about 3 s), so the full demo runs with no hardware.

---

## 9. Devices

**PC agent:** `python -m agents.pc_agent --hub ws://localhost:8000 --device pc-main [--chat]`
- Watches `~/Downloads`: Chrome and Firefox temp-file renames become `download.started` / `download.completed`. It keeps the hub's `download_id` for downloads the hub asked for, and reads the source URL from the Windows `Zone.Identifier` stream.
- Runs downloads, PowerShell commands (output becomes a `command.run` event), and deletes **inside the sandbox only** (checked again on the device). It opens files and links and shows toasts (`win11toast` if installed) and speech (`pyttsx3` if installed).

**Phone agent:**
- **Termux mode (on the phone):** polls `termux-notification-list`. Alerts use `termux-notification` + vibrate, speech uses `termux-tts-speak`, links use `termux-open-url`.
- **ADB mode (`--adb`, on the PC):** parses `dumpsys notification --noredact` and posts alerts with `cmd notification post`.

**Voice:** `python -m kairos.voice.ptt --hub http://localhost:8000` (hold F9, faster-whisper, reply spoken).

**MCP:** `kairos mcp` exposes `recall_file`, `graph_query` (read-only), `explain_action`, `list_watchers`, `current_fact` and `ask`.

---

## 10. Safety

- **Permission graph:** `Device -[:ALLOWED {mode}]-> Capability {risk}`. No edge means deny. Risk 3 always asks for confirmation, whatever the edge says.
- **Phone:** can't run arbitrary shell commands or delete files.
- **Commands:** read-only PowerShell commands (`Get-ChildItem`, `Get-Process`, …) run straight away; anything else, or anything chained with `;`, `|` or `&`, asks first.
- **Deletes:** only inside `KAIROS_SANDBOX`, checked by both the hub and the PC agent.
- **Confirmations:** stored as a pending `Action`, expire after 5 min, and are decided only by rules or the dashboard buttons.
- **Audit:** every tool call is an `Action` node with its causes; nothing runs without one.
- **Device results flow back:** the Action ID is created *before* the tool runs, and every message to a device carries it.
  - Work a device still has to finish (`run_command`, `delete_path`, `download_from_person`) is recorded as `dispatched`.
  - When the device reports back (`{"kind": "result", "action_id", "ok", "output"}`), `Kairos.device_result` sets the Action to `done` or `failed`, stores the output, and alerts the device the request came from (`origin_device`). A command asked for on the phone and run on the PC reports its result on the phone.
  - Confirmed actions keep their original ID.
  - Successful downloads stay quiet, because the download watcher already alerts.
  - Late results for cancelled actions are recorded but not announced.
  - Simulated devices report simulated results, so nothing stays stuck in the demo.
- **Read-only Cypher guard:** blocks write clauses, unknown procedures and multiple statements, and runs with `ro_query`.
- **Device auth:** devices need a shared token (`KAIROS_DEVICE_TOKEN`).
- **Data:** the graph only ever holds the synthetic persona.

---

## 11. Commands

```bash
docker compose up -d falkordb          # FalkorDB + Browser :3000
pip install -e .[dev,pc]
kairos setup --wipe [--weeks 3]        # schema, persona, history, habits
kairos hub                             # hub + dashboard :8000
kairos chat [--device pc-main]         # terminal chat; /replay morning, /device, /consolidate
kairos replay morning [--hub http://localhost:8000] [--speed 2]
kairos llm-check
kairos doctor [--llm]                  # end-to-end readiness check
kairos digest [--day today] [--pending] [--rules]   # file a day of chat into memory now
kairos pet [--voice-chat] [--scale 5]  # desktop pet: status bubbles, click to talk
kairos mcp
pytest
```

---

## 12. Tests

| File | Tests | Covers |
|---|---|---|
| `test_rules.py` | 7 | Uber arrival time / driver / status / destination, email sender + link, WhatsApp groups, file paths |
| `test_intents.py` | 13 | Every demo phrase → correct tool + arguments; yes/no only when something is pending |
| `test_queries_and_guard.py` | 10 (+ params) | All 75 queries present, templates use the allowlist, watcher return shapes match, read-only guard, command classification, sandbox paths |
| `test_models_sim.py` | 7 | Envelope dedupe, scenarios, history, persona permissions, PC download watcher (Chrome flow, hub-assigned IDs), ADB parser |
| `test_openrouter.py` | 8 | Tool loop against a fake server, bad JSON arguments, offline → circuit breaker, 401 cooldown, no key → rules, extractor rules-first / LLM / offline |
| `test_agent_fallback.py` | 7 | LLM path + device names, offline → rules, no key → rules, no double run, model-requested delete still confirmed, yes/no decided by rules, chat history |
| `test_triage.py` | 12 | Keyword matching, rule importance, `watch_for` intent, fulfil → one alert, noise not alerted, old events skipped, triage off, no duplicate alerts, LLM fulfils with graph context and drops made-up IDs, LLM unrequested alert, LLM offline → rules, rate limit → rules |
| `test_status.py` | 10 | First snapshot quiet, app/focus/battery/network diffs, battery-low once and not while charging, phone snapshots don't close apps, state props, status descriptions + stale flag, friendly app names, netsh/ADB parsers, status intents vs battery watcher, tracker effects |
| `test_device_results.py` | 7 | Action ID exists before the tool runs, `dispatched` status, origin device, confirmed action keeps its ID, result → `done` + alert on the origin device, failures reported, quiet download success, unknown/cancelled actions, simulated device results |
| `test_doctor.py` | 6 | Every query gets all its params with the right types (lists for `UNWIND`/`IN`, maps for `+=`, vectors), doctor reports a missing FalkorDB, optional packages never block, sample PDF endpoint is a valid PDF, header bytes decode |
| `test_trust.py` | 6 | Trust levels, taint record, sensitive capabilities, **LLM reads an email then tries a command → confirmation, never runs**, the same command from the user's own words runs, harmless actions still allowed when tainted |
| `test_tasks_notes.py` | 7 | Due-date parsing, request/promise extraction, similarity, merge-template allowlist, person merge moves every relationship, markdown export |
| `test_integrations.py` | 17 | ICS (time zones, folding, attendees, RRULE), demo calendar and prep text, IMAP → linked message, geofences, place transitions, coordinates never stored, Telegram allow-list/refusal/delivery, Chromium + Firefox history, watermark, important people with and without PageRank, transfer store + expiry, transfer → open on target, clipboard forwarded not stored, plugin names/specs/results/calls |
| `test_intents_new.py` | 23 cases | Offline phrases for every new feature + regression on older phrases + Telegram device phrase |
| `test_wake.py` | 2 | Record-until-silence (skipped without numpy) |
| `test_integration.py` | 15 | **Needs FalkorDB:** previous 9 + request email → task → completed, notes + hybrid search, meeting prep from calendar, place watcher on leaving, merge people, said-in-passing → digest → recalled after a restart |
| `test_voice_pet.py` | 11 | WAV parsing, `/api/voice` (chat, silence, transcribe-only, 503/400/413), missing packages, pet sprite moods + NULL screen, pet mood/bubble logic, voice activity detection |
| `test_digest.py` | 15 | Chat log format, day split, restore across midnight, prune; rules digest; model digest writes facts/notes/people/tasks with provenance and never sees assistant lines; incremental; bad output → rules; digest hour; auto waits for the model; conversation recall; agent restores history after restart; phrases; queries |

`test_queries_and_guard.py` now **scans the code** for every query name used and checks it exists, instead of a hand-kept list.

**Result now:** 197 passed, 16 skipped (15 need FalkorDB, 1 needs numpy). `tests/test_web.py` (25) covers the web feature, `tests/test_digest.py` (15) the chat log + digest.

---

## 13. Not verified yet / known gaps

| Item | Why / next step |
|---|---|
| Cypher on a real FalkorDB | No Docker here. `kairos doctor` checks all queries at once when FalkorDB is up. Remaining suspects: `FOREACH` with `SET`, `exp()`, vector index syntax (`shortestPath` was removed) |
| Demo links | Scenario links point at the hub's sample PDF (`localhost:8000/demo/…`); a phone outside the LAN can't open them |
| OpenRouter with a real key | Add the key + model and run `kairos llm-check`, then tune `SYSTEM_PROMPT` on the demo lines |
| Dashboard against the live hub | Only checked with canned data |
| PC / phone agents, voice, MCP | Not run; optional packages (`win11toast`, `pyttsx3`, `faster-whisper`, `sounddevice`, `keyboard`, `mcp`) not installed |
| Wake word | Not built (stretch goal) |
| LLM extractor runs inside the ingest lock | A slow model call (≤ 8 s) delays other ingestion; only happens for unknown apps (triage, by contrast, runs outside the lock) |
| Status privacy | Window titles can contain email subjects or document names; use `--no-titles` on the PC agent to send app names only |
| Phone foreground app | Not available in Termux mode (Android restriction); only via ADB |
| Real accounts not exercised | Telegram bot, IMAP mailbox, ICS feeds, MCP servers and openWakeWord were tested with fakes or fixtures only; add credentials in `.env` and check with `kairos doctor` |
| Calendar recurrence | Only DAILY/WEEKLY RRULEs (with COUNT/UNTIL/INTERVAL) are expanded; BYDAY and exceptions (EXDATE) are ignored |
| Phone location | Termux mode only (network fix); ADB mode sends no location |
| Taint is turn-level | Any external text read in a turn taints the whole turn (conservative: more confirmations, never fewer) |
| Triage cost | One cheap-model call per live notification (capped per minute). Tune `KAIROS_TRIAGE_MAX_PER_MIN` or use `rules` |
| Memory digest with a real model | Tested with a fake client only; tune `DIGEST_PROMPT` on real chats. `forget X` doesn't rewrite old chat-log files (they expire after `KAIROS_CHATLOG_DAYS`) |
| Hackathon rules | Check whether code written before Oct 15 is allowed |

---

## 14. Change log

| When | Change |
|---|---|
| Session 1 | Read and corrected the design doc (Fixed vs flexible, Q4/Q5/Q6 fixes, Download model, schema properties) |
| Session 1 | Built the full repo: queries, core engine, agent with rule planner, hub, CLI, simulator, persona, PC/phone agents, MCP, voice, tests, README, PLAN |
| Session 2 | Added the OpenRouter LLM (planner tool loop + extractor) with the offline rule backup and circuit breaker, `kairos llm-check`, 15 new tests |
| Session 2 | Added the web dashboard (chat, live graph, timeline, actions with Why/Approve/Deny, watchers, memory, simulator, toasts) and the hub dashboard API + device simulation |
| Session 2 | This document |
| Session 9 | Voice chat: hub speech-to-text (`/api/voice`), dashboard mic + continuous voice chat; desktop pet (`kairos pet`) with moods, status bubbles, click-to-talk; 11 tests (197 total) |
| Session 8 | Cross-session memory: daily chat log on disk, session restore after restart, own words searchable, nightly memory digest (small model or rules → facts/notes/people/tasks with Digest provenance), `recall_conversation`/`digest_memory` tools, `kairos digest`, dashboard digests panel, 15 + 1 tests (186 total) |
| Session 7b | Read-only web search + page reading (Playwright or HTTP), SSRF guard, provenance confirmation for unknown URLs, web memory in the graph, OS trust store for all HTTPS clients, `doctor --web`, 25 tests (171 total) |
| Session 7 | Untrusted-content tracking (taint → confirm, `INFLUENCED_BY`), commitments/tasks, hybrid memory (notes, full-text, vectors) + entity-linked context card, memory editor + markdown export, ICS calendar + meeting prep, IMAP email, Telegram channel, browser history, location/place watchers, file + clipboard handoff, MCP plugins, important people (PageRank), shared family graph, wake word; Telegram-origin stricter policy; dashboard Tasks tab, editor, badges; 4 new demo scenarios; 53 new tests (146 total) |
| Session 6 | End-to-end readiness: `kairos doctor` (EXPLAIN on every query), `shortestPath` removed, explain timestamps fixed, hub fails fast without FalkorDB, header decoding, Termux import fix, offline sample PDF for the real download demo, `kairos` command installed, 6 new tests |
| Session 5 | Device results linked back to their Action (pre-allocated IDs, `dispatched` status, origin-device alerts, simulated results, dashboard result display), 8 new tests |
| Session 4 | Live device status: PC (ctypes) + phone (Termux/ADB) snapshots every 2 min, Device state + RUNNING edges, change events, `device_status` tool, LLM context card, dashboard pills, 10 new tests |
| Session 3 | Notification triage: LLM/rules importance + fulfilling open-ended `watch_for` requests, background worker, rate limit, dashboard importance badges, demo events, 13 new tests |
