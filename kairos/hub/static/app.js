// KAIROS dashboard: vanilla JS, no external dependencies (works offline).
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (ts) => {
  if (!ts) return "";
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ts).toLocaleDateString(undefined, { day: "numeric", month: "short" });
};

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

const state = { devices: {}, kind: "pc", outboxSeq: 0, firstOutbox: true };

// ---------------------------------------------------------------- status
async function refreshStatus() {
  const s = await api("/api/status");
  $("#graphName").textContent = s.graph;
  const llm = s.llm;
  const badge = $("#llmBadge");
  if (llm.online) {
    badge.className = "badge ok";
    badge.textContent = `● OpenRouter · ${llm.model}`;
    badge.title = "LLM planner online; rules are the offline backup";
  } else {
    badge.className = "badge warn";
    badge.textContent = "● Offline rules";
    badge.title = llm.reason || "rule-based planner";
  }
  state.devices = {};
  $("#devices").innerHTML = s.devices.map((d) => {
    state.devices[d.kind] = d.id;
    const cls = d.connected ? "on" : s.simulate_devices ? "sim" : "";
    const st = d.connected ? "connected" : s.simulate_devices ? "simulated" : "offline";
    const live = [d.foreground_app, d.battery != null && d.battery >= 0 ? `${d.battery}%${d.charging ? "⚡" : ""}` : null]
      .filter(Boolean).join(" · ");
    return `<span class="pill ${cls}" title="${esc(d.summary || d.id)}">${d.kind === "phone" ? "📱" : "💻"} ${esc(d.name || d.id)} · ${st}${live ? " · " + esc(live) : ""}</span>`;
  }).join("");
  const c = s.counts || {};
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  $("#counts").textContent = `${total} nodes · ${c.Event || 0} events · ${c.Action || 0} actions`;
}

// ---------------------------------------------------------------- chat
const CHIPS = [
  "remind me when my Uber is 2 minutes away",
  "download the report Rahul sent and tell me on my phone when it's done",
  "what was that PDF Rahul sent me today? open it",
  "I've moved to the Cyber City office",
  "where is my office",
  "why did you remind me",
  "delete my temp folder",
  "what are my habits",
  "brief me",
  "what's open on my laptop",
  "tell me on my phone when Rahul replies about the budget",
  "what do I owe Rahul",
  "prep me for my next meeting",
  "remind me to buy milk when I leave the office",
  "send it to my phone",
  "who matters most to me right now",
  "that thing about the budget",
  "search the web for FalkorDB vector index",
  "read result 2",
];

function addMsg(role, text, meta, confirm) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = esc(text) + (meta ? `<span class="meta">${esc(meta)}</span>` : "");
  if (confirm) {
    const box = document.createElement("div");
    box.className = "confirm";
    box.innerHTML = `<button class="btn ok">Yes</button><button class="btn bad">No</button>`;
    box.children[0].onclick = () => { box.remove(); send("yes"); };
    box.children[1].onclick = () => { box.remove(); send("no"); };
    el.appendChild(box);
  }
  $("#messages").appendChild(el);
  $("#messages").scrollTop = 1e9;
}

async function send(text) {
  if (!text.trim()) return;
  const device = state.devices[state.kind] || (state.kind === "phone" ? "phone-arjun" : "pc-main");
  addMsg("user", text, `${state.kind === "phone" ? "📱" : "💻"} ${device}`);
  try {
    const r = await post("/chat", { device_id: device, text });
    addMsg("bot", r.reply, `via ${r.planner}${r.actions.length ? ` · ${r.actions.length} action(s)` : ""}`,
      r.reply.startsWith("Just to confirm"));
  } catch (e) {
    addMsg("bot", `Error: ${e.message}`);
  }
  refreshSide();
}

function initChat() {
  $("#chips").innerHTML = CHIPS.map((c) => `<span class="chip">${esc(c)}</span>`).join("");
  $$("#chips .chip").forEach((el) => (el.onclick = () => send(el.textContent)));
  $("#chatForm").onsubmit = (e) => {
    e.preventDefault();
    const t = $("#chatText").value;
    $("#chatText").value = "";
    send(t);
  };
  $$("#deviceSeg button").forEach((b) => (b.onclick = () => {
    $$("#deviceSeg button").forEach((x) => x.classList.toggle("on", x === b));
    state.kind = b.dataset.kind;
  }));
  addMsg("bot", "Hi Arjun. Ask me anything below, or play a simulator scenario to feed me events.");
}

// ---------------------------------------------------------------- side panels
const ICON = { notification: "🔔", "download.started": "⬇️", "download.completed": "✅", "download.failed": "⚠️",
  "file.created": "📄", "file.modified": "📝", "file.moved": "📁", "file.deleted": "🗑️", "browser.visit": "🌐",
  "command.run": "⌨️", utterance: "🗣️", "app.opened": "🟢", "app.closed": "⚪", "app.focused": "🪟",
  "battery.low": "🪫", "network.changed": "📶", "place.entered": "📍", "place.left": "🚶", "web.search": "🔎", "web.read": "📖" };

const DIR = { they_asked: "asked you", i_promised: "you promised", todo: "to do" };
const when = (ts) => (ts ? new Date(ts).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" }) : "");

async function refreshSide() {
  const [events, actions, watchers, memory] = await Promise.all([
    api("/api/events"), api("/api/actions"), api("/watchers"), api("/api/memory"),
  ]);

  const IMP = ["noise", "normal", "later", "important"];
  $("#tab-timeline").innerHTML = events.map((e) => `
    <div class="row"><div class="top"><span>${ICON[e.type] || "•"} ${esc(e.text)}</span>
    <span class="when">${ago(e.ts)}</span></div><span class="muted small">${esc(e.device || "")} · ${esc(e.type)}</span>
    ${e.importance != null ? `<span class="imp imp-${e.importance}" title="triaged by ${esc(e.triage_by)}">${IMP[e.importance]}${e.triage_reason ? " · " + esc(e.triage_reason) : ""}</span>` : ""}</div>`
  ).join("") || `<p class="muted">No events yet. Try the simulator.</p>`;

  $("#tab-actions").innerHTML = actions.map((a) => `
    <div class="row"><div class="top"><span><span class="tag">${esc(a.tool)}</span>${esc(a.summary)}</span>
    <span class="when">${ago(a.ts)}</span></div>
    <span class="small st-${esc(a.status)}">${esc(a.status === "dispatched" ? "waiting for device…" : a.status.replace("_", " "))}</span>
    ${a.tainted ? `<span class="imp imp-3" title="${esc(a.taint_sources || "")}">⚠ untrusted context</span>` : ""}
    ${["run_command", "delete_path", "download_from_person"].includes(a.tool) && a.result && a.status !== "dispatched"
      ? `<div class="result small muted">${esc(a.result.slice(0, 200))}</div>` : ""}
    <div class="actions">
      <button class="link" data-why="${esc(a.id)}">Why?</button>
      ${a.status === "pending_confirmation" ? `<button class="btn ok" data-approve="${esc(a.id)}">Approve</button>
        <button class="btn bad" data-deny="${esc(a.id)}">Deny</button>` : ""}
    </div></div>`).join("") || `<p class="muted">No actions yet.</p>`;

  $("#tab-watchers").innerHTML = watchers.map((w) => `
    <div class="row"><div class="top"><span>👁️ ${esc(w.description)}</span><span class="when">${ago(w.created_at)}</span></div>
    <span class="muted small">${esc(w.condition_type)} → ${esc(w.notify_device || "auto-route")}</span>
    <div class="actions"><button class="link" data-cancel="${esc(w.id)}">Cancel</button></div></div>`
  ).join("") || `<p class="muted">Not watching for anything. Say “tell me when…”.</p>`;

  $("#tab-tasks").innerHTML = `
    <div class="actions"><button class="btn" id="addTask">+ Add task</button></div>
    <h3>Open commitments</h3>
    ${(memory.tasks || []).map((t) => `<div class="row"><div class="top"><span>${t.person ? `<b>${esc(t.person)}</b> ` : ""}${esc(DIR[t.direction] || "")}: ${esc(t.title)}</span>
      <span class="when">${t.due_ts ? "due " + when(t.due_ts) : ""}</span></div>
      <span class="muted small">from ${esc(t.source)}${t.trust === "external" ? " · came from someone else's message" : ""}</span>
      <div class="actions"><button class="link" data-done="${esc(t.id)}">Mark done</button></div></div>`).join("")
      || `<p class="muted">Nothing open. Requests like “can you send me the deck by Friday?” show up here.</p>`}
    <h3>Meetings</h3>
    ${(memory.meetings || []).map((m) => `<div class="row"><div class="top"><span>📅 ${esc(m.title)}</span><span class="when">${when(m.start_ts)}</span></div>
      <span class="muted small">${esc((m.people || []).join(", ") || "no attendees")}${m.location ? " · " + esc(m.location) : ""}${m.prepped ? " · prepped ✓" : ""}</span></div>`).join("")
      || `<p class="muted">No meetings (set KAIROS_CALENDAR_URLS, or <code>demo</code>).</p>`}`;

  const byKey = {};
  memory.facts.forEach((f) => (byKey[f.key] = byKey[f.key] || []).push(f));
  const people = memory.people || [];
  $("#tab-memory").innerHTML = `
    <div class="actions">
      <button class="btn" id="addFact">+ Fact</button><button class="btn" id="addNote">+ Note</button>
      <button class="btn" id="digestNow" title="File today's conversation into memory now (runs by itself every night)">Digest today's chat</button>
      <a class="ghost btnlink" href="/api/memory/export.md" download>Export .md</a>
    </div>
    <h3>Facts (versioned)</h3>
    ${Object.entries(byKey).map(([k, fs]) => `<div class="row"><span class="tag">${esc(k)}</span>
      ${fs.map((f) => `<div class="${f.valid_to ? "fact-old" : ""}">${esc(f.statement)}
        <span class="when">${f.valid_to ? "until " + ago(f.valid_to) : "since " + ago(f.valid_from)}</span>
        ${f.valid_to ? "" : `<button class="link" data-editfact="${esc(k)}">edit</button> <button class="link" data-forgetfact="${esc(f.id)}">forget</button>`}
        </div>`).join("")}
      </div>`).join("") || `<p class="muted">No facts yet.</p>`}
    <h3>Daily digests <span class="muted small">(your chats, filed into memory each night)</span></h3>
    ${(memory.digests || []).map((d) => `<div class="row"><div class="top"><span>🗓 <b>${esc(d.day)}</b></span>
      <span class="when">${esc(d.method || "")} · ${d.turns || 0} msgs · ${d.items || 0} items</span></div>
      ${esc(d.summary || "")}${(d.topics || []).length ? `<div class="muted small">${esc(d.topics.join(" · "))}</div>` : ""}</div>`).join("")
      || `<p class="muted">Nothing digested yet. Yesterday's chat is filed away after 3 AM, or click “Digest today's chat”.</p>`}
    <h3>Notes</h3>
    ${(memory.notes || []).map((n) => `<div class="row">📝 ${esc(n.text)}
      <span class="muted small">${(n.about || []).filter(Boolean).length ? "· about " + esc(n.about.filter(Boolean).join(", ")) : ""}</span>
      <button class="link" data-forgetnote="${esc(n.id)}">forget</button></div>`).join("") || `<p class="muted">No notes yet.</p>`}
    <h3>Habits (procedural)</h3>
    ${(memory.habits || []).map((h) => `<div class="row">${esc(h.provider || "Cab")} to ${esc(h.place)} on ${esc(h.days)} ~${h.hour}:00
      <span class="muted small">· seen ${h.count}×</span>
      <div class="bar"><i style="width:${Math.round(h.strength * 100)}%"></i></div></div>`).join("")
      || `<p class="muted">No habits yet. Click “Detect habits” after setup.</p>`}
    <h3>People</h3>
    ${people.map((p) => `<div class="row">${esc(p.name)} <span class="muted small">· ${esc(p.relation || "")}${(p.aliases || []).length ? " · aka " + esc(p.aliases.join(", ")) : ""} · ${p.messages} msg</span>
      <button class="link" data-editperson="${esc(p.id)}">edit</button>
      <button class="link" data-mergeperson="${esc(p.id)}">merge into…</button></div>`).join("")}`;

  $("#addTask").onclick = async () => {
    const title = prompt("Task (e.g. 'send Rahul the deck by Friday')");
    if (!title) return;
    const r = await post("/api/tasks", { title, person: prompt("With whom? (optional)") || null, due: title });
    toast("tasks", r.reply, "speak"); refreshSide();
  };
  $("#addFact").onclick = async () => {
    const statement = prompt("Fact (e.g. 'My gym is Cult Sector 29')");
    if (statement) { await post("/api/facts", { statement }); refreshSide(); }
  };
  $("#digestNow").onclick = async () => {
    const r = await post("/jobs/digest?day=today");
    toast("memory", r.new ? `Filed ${r.new} messages: ${r.facts} facts, ${r.notes} notes, ${r.people} people, ${r.tasks} tasks`
      : "Nothing new to file away today.", "speak");
    refreshSide();
  };
  $("#addNote").onclick = async () => {
    const text = prompt("Note (e.g. 'Rahul seemed stressed about the budget')");
    if (text) { await post("/api/notes", { text }); refreshSide(); }
  };
  $$("[data-editfact]").forEach((b) => (b.onclick = async () => {
    const statement = prompt(`New value for ${b.dataset.editfact} (the old one is kept as history)`);
    if (statement) { await post("/api/facts", { statement, key: b.dataset.editfact }); refreshSide(); }
  }));
  $$("[data-forgetfact]").forEach((b) => (b.onclick = async () => {
    if (confirm("Forget this fact?")) { await post(`/api/facts/${b.dataset.forgetfact}/archive`); refreshSide(); }
  }));
  $$("[data-forgetnote]").forEach((b) => (b.onclick = async () => {
    if (confirm("Forget this note?")) { await post(`/api/notes/${b.dataset.forgetnote}/archive`); refreshSide(); }
  }));
  $$("[data-editperson]").forEach((b) => (b.onclick = async () => {
    const p = people.find((x) => x.id === b.dataset.editperson);
    const name = prompt("Name", p.name);
    if (name === null) return;
    const aliases = prompt("Aliases (comma-separated)", (p.aliases || []).join(", "));
    const relation = prompt("Relation (manager, sister, friend…)", p.relation || "");
    await post(`/api/people/${p.id}`, { name, aliases: aliases === null ? null : aliases.split(",").map((s) => s.trim()).filter(Boolean),
      relation: relation || null });
    refreshSide();
  }));
  $$("[data-mergeperson]").forEach((b) => (b.onclick = async () => {
    const src = people.find((x) => x.id === b.dataset.mergeperson);
    const others = people.filter((x) => x.id !== src.id);
    const pick = prompt(`Merge “${src.name}” into which person? Type the name:\n${others.map((o) => "• " + o.name).join("\n")}`);
    const keep = others.find((o) => pick && o.name.toLowerCase() === pick.trim().toLowerCase());
    if (keep && confirm(`Merge ${src.name} into ${keep.name}? Their messages, mentions and tasks move over.`)) {
      await post("/api/people/merge", { keep: keep.id, merge: src.id }); refreshSide();
    }
  }));
  $$("[data-done]").forEach((b) => (b.onclick = async () => { await post(`/api/tasks/${b.dataset.done}/done`); refreshSide(); }));

  $$("[data-why]").forEach((b) => (b.onclick = () => showWhy(b.dataset.why)));
  $$("[data-approve]").forEach((b) => (b.onclick = () => decide(b.dataset.approve, true)));
  $$("[data-deny]").forEach((b) => (b.onclick = () => decide(b.dataset.deny, false)));
  $$("[data-cancel]").forEach((b) => (b.onclick = async () => { await post(`/api/watchers/${b.dataset.cancel}/cancel`); refreshSide(); }));
}

async function decide(id, approve) {
  const r = await post(`/api/actions/${id}/confirm`, { approve });
  addMsg("bot", r.reply, "from dashboard");
  refreshSide();
}

async function showWhy(id) {
  const r = await api(`/explain/${id}`);
  $("#modalBody").innerHTML = `<h2>Why?</h2><p>${esc(r.text)}</p>
    <ul class="chain">${r.chain.map((c) => `<li><b>${esc(c.kind)}</b>${esc(c.text)}
      <span class="muted small"> ${esc((c.rels || []).join(" → "))} ${c.ts ? "· " + ago(c.ts) : ""}</span></li>`).join("")}</ul>`;
  $("#modal").classList.remove("hidden");
}

function initSide() {
  $$("#tabs button").forEach((b) => (b.onclick = () => {
    $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
    $$(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${b.dataset.tab}`));
  }));
  $$("[data-sim]").forEach((b) => (b.onclick = async () => {
    await post(`/api/sim/${b.dataset.sim}?speed=2`);
    toast("simulator", `Playing “${b.dataset.sim}” scenario`);
  }));
  $("#consolidate").onclick = async () => {
    const r = await post("/jobs/consolidate");
    toast("memory", `${r.habits.length} habit(s) reinforced, ${r.archived} archived`);
    refreshSide();
  };
  $("#modalClose").onclick = () => $("#modal").classList.add("hidden");
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };
}

// ---------------------------------------------------------------- outbox -> toasts
function toast(device, text, kind = "alert") {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<div class="dev">${kind === "speak" ? "🔊" : "🔔"} → ${esc(device)}</div>${esc(text)}`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

async function refreshOutbox() {
  const msgs = await api(`/api/outbox?after=${state.outboxSeq}`);
  for (const m of msgs) {
    state.outboxSeq = Math.max(state.outboxSeq, m.seq);
    if (state.firstOutbox || m.kind === "status_request") continue;  // no history replay, no internal pings
    if (m.kind === "alert" || m.kind === "speak") toast(m.device_id, m.text, m.kind);
    else toast(m.device_id, `${m.kind}: ${m.text}${m.simulated ? " (simulated)" : ""}`, m.kind);
  }
  if (msgs.length) refreshSide();
  state.firstOutbox = false;
}

// ---------------------------------------------------------------- graph (tiny force layout, SVG)
const COLORS = { User: "#f0f6fc", Device: "#a371f7", Person: "#ff7b72", Place: "#3fb950", App: "#79c0ff",
  Notification: "#d29922", DownloadEvent: "#e3b341", FileEvent: "#c9d1d9", BrowserVisit: "#56d4dd",
  CommandRun: "#8b949e", Utterance: "#ffa657", Message: "#f778ba", Link: "#58a6ff", Download: "#2ea043",
  File: "#7ee787", Folder: "#6e7681", Trip: "#ffa198", Fact: "#d2a8ff", Habit: "#bc8cff", Watcher: "#ffdf5d",
  Action: "#8b949e", Capability: "#484f58", Session: "#6e7681", Event: "#9e6a03", Digest: "#a371f7" };
const G = { nodes: [], edges: [], pos: new Map(), scale: 1, tx: 0, ty: 0, alpha: 0, drag: null, sel: null };

async function refreshGraph(force = true) {
  const data = await api(`/api/graph?minutes=${$("#graphWindow").value}`);
  const sig = `${$("#graphWindow").value}:${data.nodes.length}:${data.edges.length}`;
  if (!force && sig === G.sig) return;  // unchanged: keep the settled layout
  const warm = G.nodes.length > 0 && !force;
  G.sig = sig;
  const svg = $("#graphSvg");
  const w = svg.clientWidth || 800, h = svg.clientHeight || 600;
  G.nodes = data.nodes.map((n) => {
    const p = G.pos.get(n.id) || { x: w / 2 + (Math.random() - 0.5) * w * 0.6, y: h / 2 + (Math.random() - 0.5) * h * 0.6, vx: 0, vy: 0 };
    G.pos.set(n.id, p);
    return Object.assign(n, { p });
  });
  const idx = new Map(G.nodes.map((n) => [n.id, n]));
  G.edges = data.edges.filter((e) => idx.has(e.source) && idx.has(e.target)).map((e) => ({ ...e, s: idx.get(e.source), t: idx.get(e.target) }));
  G.deg = new Map();
  G.edges.forEach((e) => { G.deg.set(e.source, (G.deg.get(e.source) || 0) + 1); G.deg.set(e.target, (G.deg.get(e.target) || 0) + 1); });
  const labels = [...new Set(G.nodes.map((n) => n.label))].sort();
  $("#legend").innerHTML = labels.map((l) => `<span><i style="background:${COLORS[l] || "#888"}"></i>${esc(l)}</span>`).join("");
  G.alpha = warm ? 0.4 : 1;
  draw(true);
  if (!G.running) { G.running = true; requestAnimationFrame(step); }
}

function step() {
  const svg = $("#graphSvg");
  const w = svg.clientWidth, h = svg.clientHeight, N = G.nodes;
  if (G.alpha > 0.02) {
    for (let i = 0; i < N.length; i++) {
      const a = N[i].p;
      for (let j = i + 1; j < N.length; j++) {
        const b = N[j].p;
        let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 160000) continue;
        const f = (3200 / d2) * G.alpha;
        const d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
      }
    }
    for (const e of G.edges) {
      const a = e.s.p, b = e.t.p;
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = ((d - 105) / d) * 0.04 * G.alpha;
      a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
    }
    for (const n of N) {
      const p = n.p;
      if (G.drag === n) continue;
      p.vx += (w / 2 - p.x) * 0.004 * G.alpha;
      p.vy += (h / 2 - p.y) * 0.004 * G.alpha;
      p.vx *= 0.82; p.vy *= 0.82;
      p.x += Math.max(-20, Math.min(20, p.vx));
      p.y += Math.max(-20, Math.min(20, p.vy));
    }
    G.alpha *= 0.985;
    draw(false);
  }
  requestAnimationFrame(step);
}

function draw(rebuild) {
  const svg = $("#graphSvg");
  if (rebuild || !G.g) {
    svg.innerHTML = "";
    G.g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    svg.appendChild(G.g);
    G.lineEls = G.edges.map((e) => {
      const l = document.createElementNS("http://www.w3.org/2000/svg", "line");
      l.innerHTML = `<title>${esc(e.rel)}</title>`;
      G.g.appendChild(l);
      return l;
    });
    G.nodeEls = G.nodes.map((n) => {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("r", Math.min(14, 4 + Math.sqrt(G.deg.get(n.id) || 1) * 2));
      c.setAttribute("fill", COLORS[n.label] || "#888");
      c.innerHTML = `<title>${esc(n.label)}: ${esc(n.name)}</title>`;
      c.addEventListener("pointerdown", (ev) => { ev.stopPropagation(); G.drag = n; G.alpha = Math.max(G.alpha, 0.3); select(n); });
      G.g.appendChild(c);
      return c;
    });
    G.textEls = G.nodes.map((n) => {
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      const important = ["Person", "Place", "File", "Device", "Watcher", "Habit", "Fact", "User", "Trip"].includes(n.label);
      t.textContent = important ? n.name.slice(0, 22) : "";
      G.g.appendChild(t);
      return t;
    });
  }
  G.g.setAttribute("transform", `translate(${G.tx},${G.ty}) scale(${G.scale})`);
  G.edges.forEach((e, i) => {
    const l = G.lineEls[i];
    l.setAttribute("x1", e.s.p.x); l.setAttribute("y1", e.s.p.y);
    l.setAttribute("x2", e.t.p.x); l.setAttribute("y2", e.t.p.y);
    l.classList.toggle("hl", G.sel && (e.s === G.sel || e.t === G.sel));
  });
  G.nodes.forEach((n, i) => {
    G.nodeEls[i].setAttribute("cx", n.p.x); G.nodeEls[i].setAttribute("cy", n.p.y);
    G.textEls[i].setAttribute("x", n.p.x + 8); G.textEls[i].setAttribute("y", n.p.y + 3);
  });
}

function select(n) {
  G.sel = n;
  const rels = G.edges.filter((e) => e.s === n || e.t === n)
    .map((e) => e.s === n ? `→ <b>${esc(e.rel)}</b> → ${esc(e.t.label)}: ${esc(e.t.name)}` : `← <b>${esc(e.rel)}</b> ← ${esc(e.s.label)}: ${esc(e.s.name)}`);
  const box = $("#nodeInfo");
  box.innerHTML = `<b style="color:${COLORS[n.label] || "#888"}">${esc(n.label)}</b><div>${esc(n.name)}</div>
    <h3>${rels.length} connection(s)</h3><div class="small">${rels.slice(0, 25).join("<br>")}</div>`;
  box.classList.remove("hidden");
  draw(false);
}

function initGraph() {
  const svg = $("#graphSvg");
  const toLocal = (ev) => {
    const r = svg.getBoundingClientRect();
    return { x: (ev.clientX - r.left - G.tx) / G.scale, y: (ev.clientY - r.top - G.ty) / G.scale };
  };
  let pan = null;
  svg.addEventListener("pointerdown", (ev) => { pan = { x: ev.clientX - G.tx, y: ev.clientY - G.ty }; G.sel = null; $("#nodeInfo").classList.add("hidden"); draw(false); });
  window.addEventListener("pointermove", (ev) => {
    if (G.drag) { const p = toLocal(ev); G.drag.p.x = p.x; G.drag.p.y = p.y; G.drag.p.vx = G.drag.p.vy = 0; draw(false); }
    else if (pan) { G.tx = ev.clientX - pan.x; G.ty = ev.clientY - pan.y; draw(false); }
  });
  window.addEventListener("pointerup", () => { G.drag = null; pan = null; });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = svg.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    const k = ev.deltaY < 0 ? 1.1 : 1 / 1.1;
    G.tx = mx - (mx - G.tx) * k; G.ty = my - (my - G.ty) * k; G.scale *= k;
    draw(false);
  }, { passive: false });
  $("#graphRefresh").onclick = () => refreshGraph(true);
  $("#graphWindow").onchange = () => refreshGraph(true);
}

// ---------------------------------------------------------------- F9 Assistant & Messages
let f9SequenceRunning = false;
let f9Timer = null;

async function triggerF9Sequence() {
  if (f9SequenceRunning) {
    clearTimeout(f9Timer);
    f9SequenceRunning = false;
  }
  // 1. Tell backend to launch/focus pet avatar if Windows
  fetch("/api/launch_pet").catch(() => {});

  // 2. Fetch recent messages
  let msgs = [];
  try {
    msgs = await api("/api/recent_messages?limit=5");
  } catch (e) {
    console.warn("could not fetch recent messages:", e);
  }

  if (!msgs || !msgs.length) {
    appendMsg({ kind: "speak", text: "✨ You're all caught up! No recent messages. Click 🎤 or the avatar to talk." }, true);
    return;
  }

  f9SequenceRunning = true;
  appendMsg({ kind: "alert", text: `⚡ F9 Activated: Reviewing ${msgs.length} recent update(s)...` }, true);

  let idx = 0;
  function showNext() {
    if (!f9SequenceRunning || idx >= msgs.length) {
      appendMsg({ kind: "speak", text: "✨ All updates shown! Click the 🎤 mic or avatar to speak." }, true);
      f9SequenceRunning = false;
      return;
    }
    const m = msgs[idx];
    const text = `[${idx + 1}/${msgs.length}] ${m.sender || m.type}:\n${m.text}`;
    appendMsg({ kind: m.type === "notification" ? "alert" : "speak", text }, true);
    idx++;
    f9Timer = setTimeout(showNext, 3500);
  }
  f9Timer = setTimeout(showNext, 1200);
}

// Global hotkeys & button
window.addEventListener("keydown", (e) => {
  if (e.key === "F9" || (e.ctrlKey && e.shiftKey && (e.key === "K" || e.key === "k"))) {
    e.preventDefault();
    triggerF9Sequence();
  }
});
const f9Btn = $("#f9Btn");
if (f9Btn) f9Btn.onclick = triggerF9Sequence;

// ---------------------------------------------------------------- boot
const safe = (fn) => () => fn().catch((e) => console.warn(e));
initChat(); initSide(); initGraph();
safe(refreshStatus)(); safe(refreshSide)(); safe(refreshGraph)(); safe(refreshOutbox)();
setInterval(safe(refreshStatus), 5000);
setInterval(safe(refreshOutbox), 1500);
setInterval(safe(refreshSide), 4000);
setInterval(() => { if ($("#graphAuto").checked) refreshGraph(false).catch((e) => console.warn(e)); }, 8000);
