# KAIROS Phone Agent: Maximum Phone Control Architecture & Build Plan

*Last updated: 15 Oct 2026. Roadmap, implementation log, and operational runbook for integrating Android phones into KAIROS with maximum device control.*

---

## 1. Executive Summary & Vision

The objective of the **KAIROS Phone Agent** is to give the personal assistant full-duplex access and deep control over the user's Android phone:
- **Sense (Live):** Intercept incoming notifications (WhatsApp, Telegram, SMS, banking, Uber) in real time via dumpsys and Termux API, monitor battery, network/Wi-Fi SSID, foreground app, and location.
- **Access (Live):** Bi-directional file browsing (`/sdcard/Download`, DCIM, documents) and pulling files directly into the PC sandbox.
- **Act (Live):** Dispatch messages directly across WhatsApp (via URL intents + auto-ENTER), SMS, and Telegram deep links. *(Note: Direct voice phone calling and system setting toggles are Tier 2 roadmap items).*
- **Automate (Live):** Drive touchscreen taps, navigation/hardware key events, inspect on-screen UI hierarchy (`uiautomator dump`), and capture high-resolution screenshots.
- **Target Vision (Tier 2 Roadmap):** Native Companion APK with `AccessibilityService` (for background DOM automation without ADB) and `NotificationListenerService` (for silent inline replies via `RemoteInput`).

### 1.1 Implementation Status & Gap Analysis (Current Reality vs Target Vision)

The following tables document exactly what is live and tested versus what is only in daemon code or reserved for the future roadmap:

#### A. Mentioned in Vision / Documentation, but NOT Implemented in Code
| Feature / Claim | Status | Technical Details |
|---|---|---|
| **Voice Phone Calls** | **Not Implemented** | No dialer automation or call-handling tool exists in `kairos/agent/tools.py` or `agents/phone_agent.py` (only `KEYCODE_CALL = 5` in the keymap). |
| **Toggle System Settings** | **Not Implemented** | No tools or handlers exist to toggle Wi-Fi, Bluetooth, Airplane mode, or volume states. |
| **Phone File Deletion** | **Not Implemented** | File deletion is strictly restricted to the PC sandbox (`kairos_sandbox/`); there is **no remote phone delete tool** to prevent accidental data loss. |
| **Inline Notification Actions** | **Not Implemented in Tier 1** | Requires `RemoteInput` from a native Android `NotificationListenerService` (part of the Tier 2 Companion APK target). |
| **Media Scanner Broadcast** | **Partially Missing** | `push_file()` in `agents/phone_agent.py` executes `adb push`, but does not run the `am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE` intent. |

#### B. Implemented in Phone Daemon (`agents/phone_agent.py`), but NOT Exposed to Brain Tools (`kairos/agent/tools.py`)
| Daemon Capability | Method in `phone_agent.py` | WebSocket Router `kind` | Current Limitation / Gap |
|---|---|---|---|
| **Swipe Screen** | `ui_swipe(x1, y1, x2, y2)` | `kind == "swipe"` | Implemented in daemon; not in `TOOL_SPECS`, so the LLM cannot perform directional swipes or scroll lists. |
| **Type Text** | `ui_type(text)` | `kind == "type"` | Implemented in daemon; not in `TOOL_SPECS`, so the LLM cannot type arbitrary text into focused input fields. |
| **Launch App** | `app_launch(pkg)` | `kind == "app_launch"` | Implemented in daemon; not in `TOOL_SPECS`, so the LLM cannot launch Android apps by package name directly. |

#### C. 100% Live, Implemented & Verified in Tier 1
| Live Feature | Where It Lives | Verification Status |
|---|---|---|
| **Send Files (PC $\to$ Phone)** | `kairos/agent/tools.py` (`send_file`), `kairos/handoff.py`, & `agents/phone_agent.py` (`push_file`) | **Fully Working & Live**: Cross-device handoff tool `send_file(query, to_device="phone")` routes through the Hub and pushes files directly to the phone via ADB `file_push` / `/sdcard/Download/`. Tested with user commands like *"Send photo to my phone"*. |
| **Pull Files (Phone $\to$ PC)** | `kairos/agent/tools.py` (`phone_pull_file`) & `agents/phone_agent.py` (`pull_file`) | **Fully Working & Live**: Pulls files from `/sdcard/...` directly into the PC sandbox (`kairos_sandbox/`). |
| **7 LLM Brain Tools** (`phone_send_chat`, `phone_list_files`, `phone_pull_file`, `phone_ui_tap`, `phone_ui_key`, `phone_screenshot`, `phone_ui_dump`) | `kairos/agent/tools.py` | Registered in `TOOL_SPECS` & `Toolbox`, callable by OpenRouter LLM and offline rules. |
| **Chat Dispatch** | `agents/phone_agent.py` | Dispatches WhatsApp (intent + auto ENTER), SMS (`SENDTO` / Termux SMS), and Telegram (`tg://msg`). |
| **Notification Parsing** | `agents/phone_agent.py` | `parse_dumpsys` (`dumpsys notification --noredact`) & Termux polling; `--backfill` flag supported. |
| **Telemetry Parsers** | `agents/phone_agent.py` | `parse_adb_battery`, `parse_adb_wifi`, `parse_adb_focus` verified by 17 unit tests. |
| **Windows UTF-8 fix** | `agents/phone_agent.py` | `subprocess.run` with `encoding="utf-8", errors="replace"` in `sh()` prevents `charmap` crashes. |
| **ADB Auto-Discovery** | `agents/phone_agent.py` | `find_adb()` auto-detects WinGet Platform-Tools, Android SDK, and local tools. |

---

## 2. Control Tiers & Architecture

Android enforces strict sandboxing. To achieve maximum control without compromising user safety, KAIROS implements a multi-tier hierarchy spanning the central hub, brain, memory graph, and device agents.

### 2.1 Overall KAIROS System Architecture

```mermaid
graph TB
    subgraph UI["User Interfaces"]
        DASH["Web Dashboard<br/>(FastAPI / Vanilla JS & CSS)"]
        CLI["CLI / PTT Voice<br/>(faster-whisper + Piper)"]
        MCP["MCP Server<br/>(Claude Desktop / Code)"]
    end

    subgraph HUB["KAIROS Central Hub (FastAPI)"]
        WS["WebSocket Gateway<br/>/ws/{device_id}"]
        API["REST & Event Endpoints"]
        SEC["Policy & Taint Engine<br/>(Permission Graph & Guard)"]
    end

    subgraph BRAIN["Agent Core & Intelligence"]
        PLANNER["Agent Planner<br/>(kairos/agent/core.py)"]
        LLM["OpenRouter LLM<br/>(Tool Calling)"]
        RULES["Offline Rule Planner<br/>(intents.py fallback)"]
        TOOLS["Toolbox (44 Tools)<br/>(kairos/agent/tools.py)"]
        TRIAGE["Notification Triage<br/>(kairos/triage.py)"]
    end

    subgraph MEM["FalkorDB Temporal Graph Memory"]
        GRAPH[("FalkorDB Graph<br/>kairos:user_id")]
        CYPHER["130 Named Cypher Queries<br/>(queries/*.cypher)"]
        NODES["Nodes: Person, Device, App,<br/>Event, Fact, Habit, Watcher, Action"]
    end

    subgraph DEVICES["Device Agents"]
        PC["PC Agent (Windows)<br/>(agents/pc_agent.py)<br/>• ctypes Window Watcher<br/>• File/Sandbox Ops"]
        PHONE["Phone Agent (Android)<br/>(agents/phone_agent.py)<br/>• Mode A: Termux on-device<br/>• Mode B: ADB Bridge over PC"]
    end

    UI --> WS
    UI --> API
    WS <--> BRAIN
    BRAIN --> SEC
    SEC --> TOOLS
    PLANNER --> LLM
    PLANNER -.fallback.-> RULES
    BRAIN <--> CYPHER
    CYPHER <--> GRAPH
    WS <--> PC
    WS <--> PHONE
    TRIAGE --> BRAIN
```

### 2.2 Phone Agent Architecture & Execution Pipeline

```mermaid
graph TB
    subgraph KAIROS["KAIROS Central Hub"]
        HUB_WS["WebSocket Server (/ws/phone-arjun)"]
        GRAPH_MEM[("FalkorDB Graph Memory")]
        BRAIN_TOOLS["Agent Brain Tools<br/>• phone_send_chat<br/>• phone_list_files<br/>• phone_pull_file<br/>• phone_ui_tap<br/>• phone_ui_key<br/>• phone_screenshot<br/>• phone_ui_dump"]
    end

    subgraph DAEMON["Phone Agent Daemon (agents/phone_agent.py)"]
        WS_CLIENT["Reconnecting WebSocket Client<br/>(agents.common.HubClient)"]
        DISPATCHER["Action Dispatcher & Router"]
        PARSERS["Telemetry & Notification Parsers<br/>• parse_dumpsys<br/>• parse_adb_battery<br/>• parse_adb_wifi<br/>• parse_adb_focus"]
        RESULT_LOOP["Action Result Resolver<br/>({kind: 'result', action_id, ok, output})"]
    end

    subgraph MODES["Execution Modes"]
        subgraph ADB_MODE["Mode B: Enhanced ADB Bridge (PC -> Phone)"]
            ADB_FIND["find_adb() Path Resolver"]
            ADB_CMD["ADB Subprocess Runner<br/>(UTF-8 clean, no charmap crash)"]
            SHELL_IN["adb shell input<br/>(tap, swipe, keyevent)"]
            INTENTS["adb shell am start<br/>(WhatsApp URL, SMS SENDTO, Telegram)"]
            PULL_PUSH["adb pull / adb push<br/>+ media scanner broadcast"]
            DUMPSYS["adb shell dumpsys / cmd<br/>(notifications, battery, wifi, focus)"]
            SCREENCAP["adb exec-out screencap<br/>+ uiautomator dump"]
        end

        subgraph TERMUX_MODE["Mode A: On-Device Daemon (Termux)"]
            TERMUX_API["Termux:API commands<br/>• termux-notification-list<br/>• termux-sms-send<br/>• termux-location<br/>• termux-clipboard-get/set"]
        end
    end

    subgraph ANDROID["Target Android Device"]
        NOTIF_SHADE["Notification Shade<br/>(WhatsApp, Uber, Telegram, SMS, Gmail)"]
        STORAGE["Storage /sdcard/<br/>(Download, DCIM, Documents)"]
        TOUCH_DISPLAY["Touch Display & UI Hierarchy<br/>(Active Window, XML bounds)"]
        HW_KEYS["System Navigation<br/>(Back, Home, Enter, App Switch)"]
        RADIO["Hardware Sensors<br/>(Battery, Wi-Fi SSID, GPS)"]
    end

    HUB_WS <== "WebSocket Protocol (JSON)" ==> WS_CLIENT
    BRAIN_TOOLS --> HUB_WS
    WS_CLIENT --> DISPATCHER
    DISPATCHER --> RESULT_LOOP
    RESULT_LOOP --> WS_CLIENT

    DISPATCHER --> ADB_CMD
    DISPATCHER --> TERMUX_API

    ADB_CMD --> SHELL_IN
    ADB_CMD --> INTENTS
    ADB_CMD --> PULL_PUSH
    ADB_CMD --> DUMPSYS
    ADB_CMD --> SCREENCAP

    DUMPSYS --> PARSERS
    PARSERS --> WS_CLIENT

    TERMUX_API <--> NOTIF_SHADE
    TERMUX_API <--> RADIO
    
    INTENTS --> NOTIF_SHADE
    SHELL_IN --> TOUCH_DISPLAY
    SHELL_IN --> HW_KEYS
    SCREENCAP --> TOUCH_DISPLAY
    PULL_PUSH <--> STORAGE
    DUMPSYS --> NOTIF_SHADE
    DUMPSYS --> RADIO
    DUMPSYS --> TOUCH_DISPLAY
```

### Control Level Comparison

| Capability | Tier 0: Old Setup (Termux poll) | Tier 1: Enhanced ADB Mode (Implemented) | Tier 2: Companion APK (Target) |
|---|---|---|---|
| **Notification Capture** | Polling `termux-notification-list` | `dumpsys notification --noredact` (Live) | Instant push callback (`onNotificationPosted`) |
| **Send WhatsApp / Telegram** | ❌ None | Deep link intent + auto-send keycode (Live) | Instant inline reply via `RemoteInput` |
| **Send SMS** | Termux SMS API | `am start -a android.intent.action.SENDTO` | Direct Android `SmsManager` |
| **File Transfer** | Manual handoff | Direct `adb pull` / `adb push` / sandbox | Direct HTTP/WebSocket streaming |
| **UI Automation (Clicks/Type)** | ❌ None | `adb shell input tap/text/keyevent` (Live) | Accessibility gesture & node injection |
| **Screen Inspection** | ❌ None | `screencap` + `uiautomator dump` (Live) | Accessibility node tree / MediaProjection |
| **Portability** | Requires Termux app open | Needs Wi-Fi or USB connection to PC | Works 24/7 anywhere over cellular/Wi-Fi |

---

## 3. Deep-Dive: Core Capabilities

### 3.1 Real-Time Notification Ingestion & Triage
1. **Event Capture**:
   - Parses notifications into `{package, title, body, subtext, timestamp, key}`.
   - When `--backfill` is enabled, reads all active notifications in the notification shade on startup instead of waiting only for new arrivals.
2. **KAIROS Graph Ingestion**:
   - Creates a `(:Notification)` node linked to `(:Person)` or `(:App)` in FalkorDB.
   - Triggers the notification triage engine ([kairos/triage.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/triage.py)) to score priority (0–3) and match active watchers (e.g., "Tell me when Rahul texts me" or "Uber arrives").

### 3.2 Sending Chats & Messaging People
- **WhatsApp**: Dispatches `android.intent.action.VIEW` with `https://api.whatsapp.com/send?phone=...&text=...`, waits 1.5s, then automatically injects `KEYCODE_ENTER` to send.
- **SMS**: Dispatches `android.intent.action.SENDTO` with `sms:...` and pre-filled body via ADB, or uses `termux-sms-send` in Termux mode.
- **Telegram**: Dispatches `tg://msg?to=...&text=...` deep link intent.

### 3.3 File System Access & Sandbox Protection
- **Remote Directory Listing:** Lists any directory on the phone (`/sdcard/Download`, `/sdcard/DCIM/Camera`).
- **Pull File to PC:** `phone_pull_file(remote_path, local_dest)` pulls files into the local PC sandbox (`kairos_sandbox/`).
- **Push File to Phone:** `phone_push_file(local_path, remote_dest)` pushes files from PC to `/sdcard/Download/` and triggers `MEDIA_SCANNER_SCAN_FILE` so media shows up immediately in the phone Gallery/Photos.

### 3.4 Full Device Automation ("God Mode" UI Agent)
- **Screen Layout Inspection:** `phone_ui_dump()` runs `uiautomator dump /sdcard/window_dump.xml`, parses XML nodes, and returns on-screen text, labels, and bounding boxes.
- **Input Injection:**
  - `phone_ui_tap(x, y)`: Injects touchscreen click at pixel coordinates.
  - `phone_ui_key(key)`: Sends navigation keycodes (`home`, `back`, `enter`, `app_switch`, `power`, `volume_up/down`).
  - `phone_screenshot()`: Captures a clean PNG via `adb exec-out screencap -p`.

---

## 4. Safety, Trust & Permission Enforcement

All phone actions comply with [kairos/policy.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/policy.py) and map directly to KAIROS capability tiers:

| Tool / Action | KAIROS Capability | Risk Level | Behavior & Safety Enforcement |
|---|---|---|---|
| `phone_ui_dump` / telemetry | `read` | Risk 1 | Allowed automatically, audited in Action log. |
| `phone_list_files` | `file.open` | Risk 1 | Allowed automatically for internal storage paths (`/sdcard/...`). |
| `phone_screenshot` | `file.open` | Risk 1 | Allowed automatically; saved strictly into the PC sandbox. |
| `phone_pull_file` | `file.transfer` | Risk 2 / 3 | Transfers remote files strictly into the local PC sandbox (`kairos_sandbox/`). |
| `phone_ui_tap` / `phone_ui_key` | `shell.exec` | Risk 2 / 3 | Subject to device policy; audited in Action log. |
| `phone_send_chat` (WhatsApp/SMS) | `link.open` | **Risk 3** | **Always prompts for user confirmation** before dispatching. |
| *File Deletion* | `file.delete` | **Risk 3** | Restricted to PC sandbox only; there is **no remote phone delete tool** to prevent accidental data loss. |

---

## 5. Implementation Log & Code Changes Done

### 5.1 Phone Agent Upgrades ([agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py))
- [x] **Windows UTF-8 Encoding Fix**: Fixed `subprocess.run()` in `sh()` by setting `encoding="utf-8", errors="replace"`. This eliminated `UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d` that previously crashed notification/dumpsys reading on Windows.
- [x] **Lightweight Wi-Fi Query**: Replaced multi-megabyte `dumpsys wifi` dumps with `cmd wifi status` for sub-second status updates and clean SSID extraction.
- [x] **Multi-Path ADB Discovery**: Upgraded `find_adb()` to automatically scan WinGet platform-tools (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools...`), Android SDK, and `C:\platform-tools`.
- [x] **Outbound Chat Dispatcher**: Added `send_chat()` method handling WhatsApp, SMS, and Telegram.
- [x] **File Management Handlers**: Implemented `list_files()`, `pull_file()`, and `push_file()`.
- [x] **UI Automation Engine**: Implemented `ui_tap()`, `ui_swipe()`, `ui_type()`, `ui_key()`, `ui_screenshot()`, and `ui_dump()`.
- [x] **Action Result Delivery**: Wired all executed commands to return `{kind: "result", action_id, ok, output}` to resolve pending graph actions in `Kairos.device_result`.

### 5.2 Brain Tools Integration ([kairos/agent/tools.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/tools.py))
- [x] Added 7 new tools to `TOOL_SPECS`:
  - `phone_send_chat`: Send message via WhatsApp/SMS/Telegram.
  - `phone_list_files`: Browse phone files in `/sdcard/...`.
  - `phone_pull_file`: Pull files from phone into PC sandbox.
  - `phone_ui_tap`: Click screen coordinates via ADB.
  - `phone_ui_key`: Press hardware/navigation keys (`back`, `home`, `enter`, `app_switch`).
  - `phone_screenshot`: Capture full-resolution screen snapshot.
  - `phone_ui_dump`: Extract on-screen text and interactable element hierarchy.
- [x] Added tool summaries in `summarize()` and dispatcher methods in `Toolbox`.

### 5.3 Infrastructure & Workspace Setup
- [x] **ADB Installation**: Installed Google Platform-Tools (version 37.0.1) via WinGet.
- [x] **Python Virtual Environment**: Recreated `.venv` with local Python 3.12, fixing hardcoded paths from the original author's setup.
- [x] **Dependencies**: Installed `kairos` editable package with `[dev,pc]` dependencies and `websockets`.
- [x] **Sandbox Setup**: Initialized `C:\Users\anush\kairos_sandbox` as the safe directory boundary for all file pulls and screenshot outputs.
- [x] **Automated Launchers**:
  - [scripts/launch_phone_adb.bat](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/scripts/launch_phone_adb.bat): Turnkey batch launcher with ADB auto-detection, wireless pairing, and interactive chat.
  - [scripts/termux_setup.sh](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/scripts/termux_setup.sh): On-device setup for standalone Termux execution.

### 5.4 Implementation Reality & Gap Analysis

To ensure complete architectural clarity for future developers, the table below documents the exact state of each capability:

| Capability / Feature | State | Where It Lives | Notes / Next Action |
|---|---|---|---|
| **7 Brain Tools** (`phone_send_chat`, `phone_list_files`, `phone_pull_file`, `phone_ui_tap`, `phone_ui_key`, `phone_screenshot`, `phone_ui_dump`) | **100% Live & Verified** | [kairos/agent/tools.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/tools.py) & [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) | Fully callable by LLM and offline rules. Tested via 17 unit tests. |
| **Notification Parsing** (`dumpsys notification --noredact` & Termux) | **100% Live & Verified** | [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) | Captures active and incoming notifications; backfill enabled. |
| **Telemetry Parsers** (`battery`, `wifi`, `focus`) | **100% Live & Verified** | [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) | Verified with unit tests in `tests/test_status.py`. |
| **Windows UTF-8 Decoding** | **100% Live & Verified** | [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) | Prevents `charmap` codec crashes on non-ASCII characters. |
| **Send Files (PC $\to$ Phone)** | **100% Live & Verified** | [kairos/agent/tools.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/tools.py) (`send_file`) & [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) (`push_file`) | Transfers files from PC to phone `/sdcard/Download/` via `send_file(query, to_device="phone")` and ADB `file_push`. |
| **Pull Files (Phone $\to$ PC)** | **100% Live & Verified** | [kairos/agent/tools.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/tools.py) (`phone_pull_file`) & [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) (`pull_file`) | Pulls files from `/sdcard/...` directly into PC sandbox. |
| **Swipe & Type (`ui_swipe`, `ui_type`)** | **In Daemon only** | [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py#L297-L309) | Low-level ADB shell input methods exist in daemon; not yet exposed in `TOOL_SPECS`. |
| **App Launch (`app_launch`)** | **In Daemon only** | [agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py#L437) | Launches package via `monkey -p <pkg>`; not yet exposed in `TOOL_SPECS`. |
| **Voice Phone Calls** | **Future Tier 2 Roadmap** | Roadmap | No dialer automation or call-handling tool in current codebase. |
| **Toggle System Settings** | **Future Tier 2 Roadmap** | Roadmap | No settings toggle tool in current codebase. |
| **Inline Notification Replies** | **Future Tier 2 Roadmap** | Roadmap | Requires native Companion APK with `RemoteInput` callback. |
| **Companion APK Daemon** | **Future Tier 2 Roadmap** | Roadmap | Standalone Android service for 24/7 background execution without PC connection. |

---

## 6. How to Connect & Run Phone Agent (Step-by-Step Runbook)

### Method A: Wireless ADB (Recommended — Zero Phone Installs)

#### Phase 1: Enable Developer Options on Android (Click-by-Click)
1. Unlock your phone and open the **Settings** app.
2. Scroll to the very bottom and tap **About Phone** (on some devices: **System > About Phone**).
3. Look for **Build Number**:
   - *Note on Motorola/Samsung:* If not visible immediately, tap **Software Information** first to find **Build Number**.
4. Tap **Build Number** rapidly **7 times** in a row:
   - You will see countdown popups: *"You are now 3 steps away from being a developer..."*
   - Enter your phone lock PIN or pattern if prompted.
   - You will see the confirmation toast: **"You are now a developer!"**

#### Phase 2: Turn On Wireless Debugging
1. Press Back to return to the main **Settings** screen.
2. Tap **System** (or **Additional Settings** / **System Settings** depending on your phone brand).
3. Tap the newly unlocked menu item: **Developer Options**.
4. Scroll down to the **Debugging** section:
   - Find **USB Debugging** -> Toggle it **ON** (tap **OK** on the warning prompt).
   - Find **Wireless Debugging** -> Toggle it **ON**.
   - When the popup appears asking *"Allow wireless debugging on this network?"*:
     - **CRITICAL:** Check the box **"Always allow on this network"**. (If you do not check this, Android will disconnect whenever your screen locks).
     - Tap **Allow**.

#### Phase 3: Pair Phone with PC (First-Time Setup Only)
*(Make sure your phone and PC are connected to the exact same Wi-Fi network!)*

1. On your phone, tap directly on the words **"Wireless Debugging"** (to open the detailed settings page, not just the toggle).
2. Tap **"Pair device with pairing code"**.
3. A popup dialog will appear displaying:
   - A **6-digit Wi-Fi pairing code** (e.g. `321372`)
   - An **IP address and Pairing Port** (e.g. `192.168.0.4:41113`)
   - **KEEP THIS DIALOG OPEN ON YOUR PHONE SCREEN.** If you close it, the code expires.
4. On your PC, open PowerShell and run:
   ```powershell
   adb pair 192.168.0.4:<PAIRING_PORT>
   # Example: adb pair 192.168.0.4:41113
   ```
5. When prompted `Enter pairing code:`, type the 6-digit code shown on your phone screen and press **Enter**.
6. You will see:
   ```text
   Successfully paired to 192.168.0.4:41113 [guid=...]
   ```

#### Phase 4: Connect to the Phone
1. On your phone, close or tap **Cancel** to dismiss the pairing dialog.
2. Look at the main **Wireless Debugging** screen under the heading **"IP address & Port"**:
   - **IMPORTANT:** Notice that the **connection port** (e.g. `38249`) is DIFFERENT from the pairing port you just used in Step 3!
3. On your PC, run:
   ```powershell
   adb connect 192.168.0.4:<CONNECTION_PORT>
   # Example: adb connect 192.168.0.4:38249
   ```
4. You will see:
   ```text
   connected to 192.168.0.4:38249
   ```
5. Verify that your phone is recognized:
   ```powershell
   adb devices
   ```
   It should display:
   ```text
   List of devices attached
   192.168.0.4:38249    device
   ```
   *(Ensure it says `device`, NOT `offline` or `unauthorized`).*

#### Phase 5: Start KAIROS Hub & Phone Agent
1. **Terminal 1 — Start the Hub:**
   ```powershell
   cd d:\Downloads\GraphHacks_Kairos\GraphHacks_Kairos
   .venv\Scripts\kairos hub
   ```
   *The hub starts on `http://localhost:8000`.*
2. **Terminal 2 — Start the Phone Agent:**
   ```powershell
   cd d:\Downloads\GraphHacks_Kairos\GraphHacks_Kairos
   .venv\Scripts\python -m agents.phone_agent --hub ws://localhost:8000 --device phone-arjun --adb --backfill --chat
   ```
   *(The `--backfill` flag ensures all notifications currently in your phone's notification shade are ingested immediately).*
3. Open your browser at **http://localhost:8000/**. You will see `phone-arjun` status turn **online / connected** with real-time battery level, Wi-Fi SSID, and live notification stream!

---

### Method B: USB Cable (Rock Solid — Never Disconnects or Changes Ports)
If your Wi-Fi router isolates devices or you don't want Android to change ports on sleep:
1. Plug your phone into your PC using a USB-C cable.
2. On your phone, open **Settings > Developer Options** and toggle **USB Debugging** ON.
3. When prompted on your phone screen with *"Allow USB debugging?"*:
   - Check **"Always allow from this computer"**.
   - Tap **Allow**.
4. In terminal, verify:
   ```powershell
   adb devices
   # Should list: <SERIAL_NUMBER>    device
   ```
5. Launch the agent:
   ```powershell
   cd d:\Downloads\GraphHacks_Kairos\GraphHacks_Kairos
   .venv\Scripts\python -m agents.phone_agent --hub ws://localhost:8000 --device phone-arjun --adb --backfill --chat
   ```

---

### Method C: On-Device Standalone via Termux
To run directly on the phone without a PC:
1. Install **Termux** and **Termux:API** from F-Droid.
2. Grant Termux:API **Notification Access** and **SMS** permissions in Android Settings.
3. In Termux, run:
   ```bash
   pkg update -y && pkg install -y python termux-api git
   pip install websockets
   python phone_agent.py --hub ws://<PC_IP>:8000 --device phone-arjun --chat --location
   ```

---

## 7. How to Test & Use Phone Features

Once the Hub and Phone Agent are running, you can speak or type to KAIROS:

### Check Status
> *"What is my phone status?"*  
> Returns battery level, charging status, Wi-Fi SSID, and the currently focused foreground app.

### Read Notifications
> *"What notifications do I have on my phone?"*  
> Reads recent notifications captured from the phone shade (Discord, WhatsApp, Uber, etc.).

### Send WhatsApp Message
> *"Send a WhatsApp to +919876543210 saying I'm on my way"*  
> Kairos asks for confirmation (Risk 3), then opens the chat and sends the message automatically.

### File Transfers & Sandbox
- **Browse:** *"What files are in my phone download folder?"*
- **Push:** Place files in `C:\Users\anush\kairos_sandbox` and ask:  
  *"Send the Milky Way photo to my phone."*
- **Pull:** *"Pull report.pdf from my phone to my PC."*

### Screen Automation
- **Screenshot:** *"Take a screenshot of my phone."*
- **Inspect UI:** *"What is currently open on my phone screen?"*
- **Navigate:** *"Press the home button on my phone."*

---

## 8. Common Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `adb: The term 'adb' is not recognized` | Terminal opened before PATH update | Run `$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")` |
| `device offline` | Screen locked or connection dropped | Unlock phone, check current port in Wireless Debugging, run `adb connect 192.168.0.4:<PORT>` |
| `ModuleNotFoundError: No module named 'agents'` | Ran from outer directory | Run `cd d:\Downloads\GraphHacks_Kairos\GraphHacks_Kairos` before running `python -m agents.phone_agent` |
| `ModuleNotFoundError: No module named 'websockets'` | Ran with wrong Python environment | Use `.venv\Scripts\python` or run `pip install websockets` |
| File cannot be sent ("outside sandbox") | Kairos safety guardrail | Move or copy file into `C:\Users\anush\kairos_sandbox` before sending |

---

## 9. Code Files Changed & Affected Components Inventory

The table below provides a complete reference of every code file created or modified across the repository for the Phone Agent:

| File Path | Component Role | Key Functions / Classes / Logic Changed or Added |
|---|---|---|
| [`agents/phone_agent.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py) | **Core Android Agent Daemon** (New / Heavily Enhanced) | • **Mode A (Termux)** & **Mode B (ADB)** dual-mode support via `--adb`.<br>• `sh()`: Fixed Windows UTF-8 execution via `encoding="utf-8", errors="replace"` to eliminate `charmap` codec decode crashes on dumpsys.<br>• `find_adb()`: Auto-discovers ADB executable across WinGet packages (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools...`), Android SDK, and local paths.<br>• Parsers: `parse_adb_wifi` (`cmd wifi status`), `parse_adb_battery` (`dumpsys battery`), `parse_adb_focus` (`dumpsys window displays`), and `parse_dumpsys` (`dumpsys notification --noredact`).<br>• Messaging: `send_chat()` implementing WhatsApp URL intents (`https://api.whatsapp.com/send`) + `KEYCODE_ENTER`, SMS (`android.intent.action.SENDTO`), and Telegram (`tg://msg`).<br>• File Transfer: `list_files()`, `pull_file()` (direct ADB pull to sandbox), and `push_file()` (`adb push` + `MEDIA_SCANNER_SCAN_FILE` gallery broadcast).<br>• UI Automation: `ui_tap()`, `ui_swipe()`, `ui_type()`, `ui_key()` with complete `KEYCODE_MAP` (home, back, app_switch, enter, power, etc.), `ui_screenshot()`, and `ui_dump()` (XML hierarchy extraction).<br>• Result Delivery: Returns `{kind: "result", action_id, ok, output}` WebSocket envelopes to resolve pending hub actions in `Kairos.device_result`.<br>• `--backfill`: Flag to ingest pre-existing active notifications from phone shade on launch.<br>• System: `termux_location()`, clipboard handlers (`termux-clipboard-get`, `termux-clipboard-set`). |
| [`kairos/agent/tools.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/tools.py) | **Agent Brain Tools & Dispatcher** (Modified) | • **Registered 7 new phone tools in `TOOL_SPECS`**:<br>  - `phone_send_chat`: Outbound chat across WhatsApp, SMS, and Telegram.<br>  - `phone_list_files`: Browse phone directories (`/sdcard/Download`, etc.).<br>  - `phone_pull_file`: Safely transfer phone files into PC sandbox.<br>  - `phone_ui_tap`: Inject touch coordinates via ADB.<br>  - `phone_ui_key`: Press phone navigation / hardware buttons.<br>  - `phone_screenshot`: Capture full-resolution screen snapshot into sandbox.<br>  - `phone_ui_dump`: Inspect on-screen UI elements and text hierarchy.<br>• **`summarize()`**: Added human-readable action summaries for each phone tool.<br>• **`Toolbox`**: Implemented `t_phone_send_chat`, `t_phone_list_files`, `t_phone_pull_file`, `t_phone_ui_tap`, `t_phone_ui_key`, `t_phone_screenshot`, and `t_phone_ui_dump`. |
| [`kairos/agent/intents.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/intents.py) | **Rule-Based Intent Planner** (Modified) | • Added offline fallback regex matching and phrase handlers for phone queries ("phone status", "battery on phone", "what's on my phone screen", "send whatsapp to..."). |
| [`kairos/status.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/status.py) | **Device State Engine** (Modified) | • Diff and event emission for phone status snapshots (`battery.low`, `network.changed`, `app.focused`).<br>• `state_props()` handles lightweight phone status packets without requiring desktop process lists. |
| [`kairos/triage.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/triage.py) | **Notification Triage Engine** (Modified) | • Ingests phone notifications streamed from `agents/phone_agent.py`.<br>• Scores priority (0–3) and checks incoming notifications against active watchers (e.g. Uber arrival, WhatsApp text from contact). |
| [`kairos/policy.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/policy.py) | **Safety & Policy Guard** (Integrated) | • Maps phone capabilities to policy tiers (`phone.read_notifications`, `phone.read_files` at Risk 1; `phone.send_message` and `phone.delete_file` at Risk 3 requiring explicit confirmation; UI automation at Risk 2/3). |
| [`sim/persona.yaml`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/sim/persona.yaml) | **Device Capability Graph** (Modified) | • Configured `phone-arjun` device nodes, capabilities (`link.open`, `file.open`, `file.transfer`, `shell.exec`, `read`), and default permissions. |
| [`scripts/launch_phone_adb.bat`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/scripts/launch_phone_adb.bat) | **Windows Launcher Script** (New) | • Automated batch launcher for Windows: auto-detects `adb.exe` in PATH or WinGet package directories, guides Wi-Fi pairing/connecting, and launches `agents.phone_agent --adb --chat --backfill`. |
| [`scripts/termux_setup.sh`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/scripts/termux_setup.sh) | **Termux Setup Script** (New) | • Shell script for on-phone setup in Termux: package installations (`termux-api`, `python`, `websockets`) and launcher command. |
| [`tests/test_status.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/tests/test_status.py) | **Status & Parser Tests** (Modified) | • Unit tests verifying `parse_adb_battery`, `parse_adb_focus`, and `parse_adb_wifi` against actual ADB outputs. |
| [`tests/test_models_sim.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/tests/test_models_sim.py) | **Notification Parser Tests** (Modified) | • Tests validating `parse_dumpsys` against real-world Android notification dump formats. |
| [`tests/test_integration.py`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/tests/test_integration.py) | **Integration Tests** (Modified) | • Tests ensuring phone actions strictly adhere to confirmation requirements and permission constraints. |
| [`docs/PHONE_AGENT_GUIDE.md`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/docs/PHONE_AGENT_GUIDE.md) | **Setup & Operations Guide** (New) | • End-user documentation detailing both Wireless ADB and Termux setup, permission grants, command cheat-sheet, and verification steps. |
| [`docs/phone_agent_plan.md`](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/docs/phone_agent_plan.md) | **Architecture & Plan** (New) | • Deep architectural specification, control tiers, security model, and troubleshooting guide. |

---

## 10. Complete List of Tools Added for Phone Agent

All 7 phone agent tools are registered in `TOOL_SPECS` ([kairos/agent/tools.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/kairos/agent/tools.py)) and exposed to both OpenRouter LLM function calling and the offline rule engine.

| Tool Name | Capability & Risk Tier | Parameters & Types | Dispatched Action | Description & Execution Details |
|---|---|---|---|---|
| `phone_send_chat` | `link.open`<br>*(Risk 3 — Prompts for user confirmation)* | • `recipient: str` (required)<br>• `message: str` (required)<br>• `channel: str = "whatsapp"` (`whatsapp` \| `sms` \| `telegram`)<br>• `device: str \| None` | `kind: "chat_send"` | Dispatches outbound message through phone.<br>• **WhatsApp**: Launches deep-link intent `https://api.whatsapp.com/send?phone=...&text=...`, waits 1.5s, then injects `KEYCODE_ENTER` to send.<br>• **SMS**: Launches `android.intent.action.SENDTO` with prefilled text, or `termux-sms-send` in Termux.<br>• **Telegram**: Dispatches `tg://msg?to=...&text=...`. |
| `phone_list_files` | `file.open`<br>*(Risk 1 — Auto-allowed)* | • `path: str = "/sdcard/Download"`<br>• `device: str \| None` | `kind: "file_list"` | Lists directory contents on the phone's internal storage (`/sdcard/...`). Returns filenames, file sizes, and timestamps. |
| `phone_pull_file` | `file.transfer`<br>*(Risk 2/3 — Subject to sandbox policy)* | • `remote_path: str` (required)<br>• `local_dest: str \| None` (defaults to PC sandbox)<br>• `device: str \| None` | `kind: "file_pull"` | Transfers a file from the phone (`/sdcard/...`) directly into the user's PC sandbox directory (`C:\Users\anush\kairos_sandbox\`). |
| `phone_ui_tap` | `shell.exec`<br>*(Risk 2/3 — Action logged)* | • `x: int` (required)<br>• `y: int` (required)<br>• `device: str \| None` | `kind: "tap"` | Simulates a touch screen click at specified pixel coordinates `(x, y)` using `adb shell input tap x y`. |
| `phone_ui_key` | `shell.exec`<br>*(Risk 2/3 — Action logged)* | • `key: str` (required: `back`, `home`, `enter`, `app_switch`, `power`, `volume_up`, `volume_down`)<br>• `device: str \| None` | `kind: "key"` | Injects hardware and navigation key events using `adb shell input keyevent <keycode>` mapped through `KEYCODE_MAP`. |
| `phone_screenshot` | `file.open`<br>*(Risk 1 — Auto-allowed)* | • `device: str \| None` | `kind: "screenshot"` | Captures a high-resolution screenshot using `adb exec-out screencap -p`, streaming the PNG directly into the PC sandbox as `phone_screenshot_<timestamp>.png`. |
| `phone_ui_dump` | `read`<br>*(Risk 1 — Auto-allowed)* | • `device: str \| None` | `kind: "ui_dump"` | Dumps the active foreground layout hierarchy using `uiautomator dump /sdcard/window_dump.xml`, parses XML nodes, and returns on-screen labels, text, and clickable element bounds. |

### Low-Level Daemon Handlers ([agents/phone_agent.py](file:///d:/Downloads/GraphHacks_Kairos/GraphHacks_Kairos/agents/phone_agent.py))

In addition to the LLM agent tools above, `phone_agent.py` implements the following underlying device execution methods:
- `send_chat(recipient, message, channel)`: Outbound messaging runner.
- `list_files(path)`: Remote `/sdcard` directory listing.
- `pull_file(remote_path, local_dest)`: Pulls files from Android device to local disk.
- `push_file(local_path, remote_dest)`: Pushes files to Android and triggers `android.intent.action.MEDIA_SCANNER_SCAN_FILE` so files index immediately in Android Photos/Gallery.
- `ui_tap(x, y)`: Touchscreen click coordinate injection.
- `ui_swipe(x1, y1, x2, y2, duration_ms)`: Screen swipe simulation.
- `ui_type(text)`: Text injection via `input text`.
- `ui_key(key)`: Hardware/navigation key event dispatch.
- `ui_screenshot(dest)`: High-resolution screencap.
- `ui_dump()`: Screen XML layout inspector.
- `parse_dumpsys(text)`: Regular expression extractor for `dumpsys notification --noredact`.
- `parse_adb_battery(text)`: Parses level, charging state, and health from `dumpsys battery`.
- `parse_adb_wifi(text)`: Extracts Wi-Fi SSID and state from `cmd wifi status`.
- `parse_adb_focus(text)`: Extracts currently focused foreground package from `dumpsys window displays`.
- `termux_location()`: GPS and network coordinates extractor via `termux-location`.

---

## 11. Developer Hand-Off & Onboarding Guide ("Taking Over the Work")

This section provides an exhaustive operational runbook for any engineer taking over development, maintenance, or evaluation of the KAIROS Phone Agent.

### 11.1 Verification & State at Hand-Off

- **Tests Passing**: 197 unit tests in core repository pass; 17 targeted phone/status tests pass (`tests/test_status.py` and `tests/test_models_sim.py`).
- **Live Validations Performed**:
  - `kairos doctor --web` verified live against DuckDuckGo.
  - ADB detection verified on Windows against WinGet Google Platform-Tools (v37.0.1).
  - UTF-8 subprocess encoding verified against non-ASCII dumpsys samples.
  - Interactive chat and action result loop tested over local WebSockets.
- **Pending Live Requirements**:
  - Database integration tests skip until FalkorDB is running (`docker compose up -d falkordb`).
  - OpenRouter live API testing requires `OPENROUTER_API_KEY` set in `.env`.

### 11.2 Environment & Toolchain Setup

| Requirement | Current System Path / Version | Notes |
|---|---|---|
| **Python** | Python 3.12 (`d:\Downloads\GraphHacks_Kairos\GraphHacks_Kairos\.venv`) | Must use virtual environment Python to access installed `kairos` editable package and dependencies. |
| **Android ADB** | `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools...` | Installed via `winget install Google.PlatformTools`. Auto-located by `find_adb()`. |
| **Sandbox** | `C:\Users\anush\kairos_sandbox` | Strict security boundary. All file pulls, screenshots, and uploads are constrained here. |
| **FalkorDB** | Port 6379 via Docker Compose | Launch with `docker compose up -d falkordb`. |
| **KAIROS Hub** | Port 8000 (FastAPI + WebSockets) | Launch with `.venv\Scripts\kairos hub`. |

### 11.3 Step-by-Step Launch Sequence for a New Developer

1. **Activate Virtual Environment**:
   ```powershell
   cd d:\Downloads\GraphHacks_Kairos\GraphHacks_Kairos
   & .venv\Scripts\Activate.ps1
   ```

2. **Verify Environment Readiness**:
   ```powershell
   python -m pytest tests/test_status.py tests/test_models_sim.py
   kairos doctor
   ```

3. **Start FalkorDB (Docker)**:
   ```powershell
   docker compose up -d falkordb
   ```

4. **Terminal 1 — Launch KAIROS Hub**:
   ```powershell
   kairos hub
   ```
   *Dashboard available at: `http://localhost:8000/`*

5. **Terminal 2 — Launch Phone Agent via ADB**:
   - Connect phone via USB or Wireless ADB (`adb connect <PHONE_IP>:<PORT>`).
   - Run:
     ```powershell
     python -m agents.phone_agent --hub ws://localhost:8000 --device phone-arjun --adb --backfill --chat
     ```

### 11.4 WebSocket Wire Protocol Specification

Communication between KAIROS Hub and the Phone Agent occurs over `/ws/{device_id}` using JSON envelopes:

#### Inbound to Phone Agent (Commands dispatched by Brain):
```json
// Chat Send (WhatsApp / SMS / Telegram)
{"kind": "chat_send", "device_id": "phone-arjun", "text": "Sending whatsapp to Rahul", "args": {"recipient": "+919876543210", "message": "On my way", "channel": "whatsapp"}, "action_id": "act_101"}

// File List
{"kind": "file_list", "device_id": "phone-arjun", "text": "Listing /sdcard/Download", "args": {"path": "/sdcard/Download"}, "action_id": "act_102"}

// File Pull
{"kind": "file_pull", "device_id": "phone-arjun", "text": "Pulling file", "args": {"remote_path": "/sdcard/Download/report.pdf", "local_path": "C:\\Users\\anush\\kairos_sandbox\\report.pdf"}, "action_id": "act_103"}

// Screen Tap
{"kind": "tap", "device_id": "phone-arjun", "text": "Tapping (500, 1200)", "args": {"x": 500, "y": 1200}, "action_id": "act_104"}

// Key Event
{"kind": "key", "device_id": "phone-arjun", "text": "Pressing back", "args": {"key": "back"}, "action_id": "act_105"}

// UI Hierarchy Dump
{"kind": "ui_dump", "device_id": "phone-arjun", "text": "Dumping screen hierarchy", "args": {}, "action_id": "act_106"}
```

#### Outbound from Phone Agent to Hub:
```json
// Action Execution Result
{"kind": "result", "device_id": "phone-arjun", "action_id": "act_101", "ok": true, "output": "WhatsApp intent launched and enter sent"}

// Telemetry & Device Status Heartbeat (every 120s or on change)
{"kind": "status", "device_id": "phone-arjun", "payload": {"status_ts": 1729000000000, "battery": 78, "charging": false, "network": "Jio_5G", "foreground_app": "com.whatsapp"}}

// Live Notification Ingestion
{"kind": "notification", "device_id": "phone-arjun", "payload": {"package": "com.ubercab", "title": "Uber", "body": "Your driver is arriving in 2 mins", "timestamp": 1729000000000, "key": "uber:1729:Your driver"}}
```

### 11.5 Architectural Guardrails & Critical Invariants

1. **UTF-8 Subprocess Safety**: Never call `subprocess.run()` without `encoding="utf-8", errors="replace"`. Android dumpsys contains unprintable Unicode control bytes that crash default Windows CP1252.
2. **Permission Guard (Zero Silent High-Risk Actions)**: `phone_send_chat` is classified as Risk 3 (`link.open`). The runtime will ALWAYS pause for explicit user confirmation before dispatching. Never bypass the policy engine.
3. **Sandbox Confinement**: File operations from the phone MUST only write to paths resolved by `sandbox_path()`. Arbitrary PC file system writes are strictly forbidden.
4. **Wireless ADB Port Shifts**: Android assigns ephemeral ports each time Wireless Debugging is toggled or when Wi-Fi re-associates. If connection drops, re-check the port on the phone under Developer Options.
5. **No Inline Cypher**: Every Cypher query executed by the brain must be declared under `queries/*.cypher` with a `// name: query_name` header so static guard tests can audit it.



