// KAIROS dashboard voice: mic -> 16 kHz WAV -> hub /api/voice (local faster-whisper) -> reply spoken aloud.
// "Voice chat" mode keeps the conversation going: after KAIROS finishes speaking it listens again.
// No libraries. Browsers allow the mic on http://localhost; from another machine use HTTPS.
"use strict";

const VOICE = {
  conversation: false,  // keep listening after each reply
  state: "idle",        // idle | listening | thinking | speaking
  ctx: null, stream: null, node: null, chunks: [], rate: 48000,
  speechMs: 0, quietMs: 0, totalMs: 0, browserSTT: false,
};
const V_THRESHOLD = 0.018;   // RMS that counts as speech
const V_SILENCE_MS = 900;    // stop after this much quiet once you've spoken
const V_NO_SPEECH_MS = 7000; // give up if you say nothing
const V_MAX_MS = 20000;

function updateTaskbarVoice(s, hint) {
  const pill = $("#taskbarVoicePill");
  const text = $("#taskbarVoiceText");
  if (!pill || !text) return;
  pill.classList.remove("listening", "thinking", "speaking", "active-mode");

  if (s === "listening") {
    pill.classList.add("listening");
    text.textContent = "Listening...";
  } else if (s === "thinking") {
    pill.classList.add("thinking");
    text.textContent = "Transcribing...";
  } else if (s === "speaking") {
    pill.classList.add("speaking");
    text.textContent = "Speaking...";
  } else {
    if (VOICE.conversation) {
      pill.classList.add("active-mode");
      text.textContent = "Voice: Active";
    } else {
      text.textContent = "Voice Agent";
    }
  }
}

function setVoiceState(s, hint) {
  VOICE.state = s;
  const b = $("#micBtn");
  if (b) {
    b.className = `mic ${s}`;
    b.textContent = { idle: "🎤", listening: "●", thinking: "…", speaking: "🔊" }[s];
    b.title = hint || { idle: "Talk (Ctrl+M)", listening: "Listening… click to stop", thinking: "Thinking…",
      speaking: "Speaking… click to interrupt" }[s];
  }
  $("#voiceHint").textContent = hint || (s === "listening" ? "Listening…" : s === "thinking" ? "Transcribing…" : "");
  updateTaskbarVoice(s, hint);
}

function encodeWav(chunks, inRate) {
  // float32 chunks at the mic's rate -> mono 16-bit PCM WAV at 16 kHz
  const len = chunks.reduce((n, c) => n + c.length, 0);
  const all = new Float32Array(len);
  let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  const ratio = inRate / 16000, n = Math.floor(len / ratio);
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (off, s) => [...s].forEach((ch, i) => v.setUint8(off + i, ch.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, all[Math.floor(i * ratio)]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

async function startListening() {
  if (VOICE.state === "listening") return stopListening(true);
  if (VOICE.browserSTT) return browserListen();
  window.speechSynthesis?.cancel();
  try {
    VOICE.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    setVoiceState("idle", "Browser mic blocked on HTTP: Use Desktop Avatar on PC or enable in chrome://flags");
    return;
  }
  VOICE.ctx = new (window.AudioContext || window.webkitAudioContext)();
  VOICE.rate = VOICE.ctx.sampleRate;
  const src = VOICE.ctx.createMediaStreamSource(VOICE.stream);
  VOICE.node = VOICE.ctx.createScriptProcessor(4096, 1, 1);
  VOICE.chunks = []; VOICE.speechMs = 0; VOICE.quietMs = 0; VOICE.totalMs = 0;
  VOICE.node.onaudioprocess = (e) => {
    const d = e.inputBuffer.getChannelData(0);
    VOICE.chunks.push(new Float32Array(d));
    let sum = 0;
    for (let i = 0; i < d.length; i++) sum += d[i] * d[i];
    const rms = Math.sqrt(sum / d.length), ms = (d.length / VOICE.rate) * 1000;
    VOICE.totalMs += ms;
    $("#micLevel").style.width = `${Math.min(100, rms * 900)}%`;
    if (rms > V_THRESHOLD) { VOICE.speechMs += ms; VOICE.quietMs = 0; } else if (VOICE.speechMs > 250) VOICE.quietMs += ms;
    if ((VOICE.speechMs > 250 && VOICE.quietMs > V_SILENCE_MS) || VOICE.totalMs > V_MAX_MS) stopListening(true);
    else if (VOICE.speechMs < 250 && VOICE.totalMs > V_NO_SPEECH_MS) stopListening(false);
  };
  src.connect(VOICE.node); VOICE.node.connect(VOICE.ctx.destination);
  setVoiceState("listening");
}

function releaseMic() {
  try { VOICE.node && VOICE.node.disconnect(); } catch (_) { /* already */ }
  VOICE.stream && VOICE.stream.getTracks().forEach((t) => t.stop());
  VOICE.ctx && VOICE.ctx.close().catch(() => {});
  VOICE.node = VOICE.stream = VOICE.ctx = null;
  $("#micLevel").style.width = "0%";
}

async function stopListening(send) {
  if (VOICE.state !== "listening") return;
  const chunks = VOICE.chunks, rate = VOICE.rate, spoke = VOICE.speechMs > 250;
  releaseMic();
  if (!send || !spoke) {
    setVoiceState("idle", VOICE.conversation ? "Didn't hear anything. Voice chat paused." : "");
    VOICE.conversation = false; $("#voiceMode").checked = false;
    return;
  }
  setVoiceState("thinking");
  const device = state.devices[state.kind] || (state.kind === "phone" ? "phone-arjun" : "pc-main");
  try {
    const r = await fetch(`/api/voice?device_id=${encodeURIComponent(device)}`, { method: "POST", body: encodeWav(chunks, rate) });
    if (r.status === 503 && ("webkitSpeechRecognition" in window || "SpeechRecognition" in window)) {
      VOICE.browserSTT = true;  // hub has no speech model: use the browser's recogniser from now on
      addMsg("bot", "Server speech-to-text isn't installed (pip install faster-whisper numpy). Using the browser's speech recognition for now: say it again.");
      setVoiceState("idle");
      return;
    }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    const j = await r.json();
    if (!j.text) { setVoiceState("idle", "Couldn't make that out."); return maybeContinue(); }
    showVoiceTurn(device, j.text, j);
  } catch (e) {
    addMsg("bot", `Voice error: ${e.message}`);
    setVoiceState("idle");
  }
}

function showVoiceTurn(device, said, j) {
  addMsg("user", said, `🎤 ${device}`);
  addMsg("bot", j.reply, `via ${j.planner}${(j.actions || []).length ? ` · ${j.actions.length} action(s)` : ""}`,
    (j.reply || "").startsWith("Just to confirm"));
  refreshSide();
  speak(j.reply, maybeContinue);
}

async function browserListen() {
  // Fallback when the hub has no STT model. Note: Chrome's recogniser sends audio to Google.
  const R = window.SpeechRecognition || window.webkitSpeechRecognition;
  const rec = new R();
  rec.lang = "en-IN"; rec.interimResults = false; rec.maxAlternatives = 1;
  setVoiceState("listening", "Listening (browser speech recognition)…");
  rec.onresult = async (e) => {
    const text = e.results[0][0].transcript;
    setVoiceState("thinking");
    const device = state.devices[state.kind] || "pc-main";
    try {
      const j = await post("/chat", { device_id: device, text });
      showVoiceTurn(device, text, j);
    } catch (err) { addMsg("bot", `Error: ${err.message}`); setVoiceState("idle"); }
  };
  rec.onerror = () => setVoiceState("idle", "Didn't catch that.");
  rec.onend = () => { if (VOICE.state === "listening") setVoiceState("idle"); };
  rec.start();
}

function speak(text, done) {
  if (!text || !window.speechSynthesis || $("#voiceMute").checked) { setVoiceState("idle"); return done && done(); }
  const u = new SpeechSynthesisUtterance(text.replace(/[*_`#]/g, ""));
  u.rate = 1.05;
  const pick = speechSynthesis.getVoices().find((v) => /en[-_](IN|GB|US)/i.test(v.lang) && /natural|neural|online/i.test(v.name))
    || speechSynthesis.getVoices().find((v) => /^en/i.test(v.lang));
  if (pick) u.voice = pick;
  u.onend = u.onerror = () => { setVoiceState("idle"); done && done(); };
  setVoiceState("speaking");
  speechSynthesis.speak(u);
}

function maybeContinue() {
  if (VOICE.conversation) setTimeout(() => VOICE.conversation && VOICE.state === "idle" && startListening(), 350);
}

function initVoice() {
  const taskbarPill = $("#taskbarVoicePill");
  if (taskbarPill) {
    taskbarPill.onclick = () => {
      if (VOICE.state === "speaking") { speechSynthesis.cancel(); setVoiceState("idle"); return; }
      if (VOICE.state === "listening") { stopListening(true); return; }
      if (VOICE.state === "thinking") return;
      const chk = $("#voiceMode");
      if (chk) chk.checked = true;
      VOICE.conversation = true;
      startListening();
    };
  }

  $("#micBtn").onclick = () => {
    if (VOICE.state === "speaking") { speechSynthesis.cancel(); setVoiceState("idle"); return; }
    if (VOICE.state === "thinking") return;
    startListening();
  };
  $("#voiceMode").onchange = (e) => {
    VOICE.conversation = e.target.checked;
    if (VOICE.conversation && VOICE.state === "idle") {
      startListening();
    } else {
      updateTaskbarVoice(VOICE.state);
    }
  };
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key.toLowerCase() === "m") { e.preventDefault(); $("#micBtn").click(); }
  });
  api("/api/voice/status").then((s) => {
    if (!s.available) {
      VOICE.browserSTT = "webkitSpeechRecognition" in window || "SpeechRecognition" in window;
      $("#voiceHint").textContent = VOICE.browserSTT ? "Using browser speech recognition (hub: pip install faster-whisper numpy)"
        : "Voice needs: pip install faster-whisper numpy (on the hub)";
    }
  }).catch(() => {});
  setVoiceState("idle");
}
initVoice();
