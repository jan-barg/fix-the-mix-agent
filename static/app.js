// Fix the Mix front end: upload → sections (with the Lead's brief) → analysis → results (DESIGN.md §9).
// The run ID lives in the URL hash, so a reload returns to the same run and the view it was in.
import { SectionEditor, TRACK_COLOURS, fmtTime, errorText, esc } from "/static/editor.js";
import { PROGRESS_MESSAGES } from "/static/progress-messages.js";

const MAX_BYTES = 30 * 1024 * 1024;
const TOO_LARGE = "That file is larger than 30 MB. Please export an MP3 (320 kbps works well) and upload that instead.";
const SLOTS = [
  { id: "user", role: "user", title: "Your track", required: true,
    hint: "The mix or master you want feedback on.",
    placeholder: "e.g. Unmastered mix. The bass feels disconnected from the kick." },
  { id: "ref1", role: "reference", slot: 1, title: "Reference 1", required: true,
    hint: "A finished track you want to compare against.",
    placeholder: "What to reference here, e.g. I want the bass from this song." },
  { id: "ref2", role: "reference", slot: 2, title: "Reference 2", required: false,
    hint: "Optional.", placeholder: "e.g. Use this for the build-to-drop loudness jump." },
  { id: "ref3", role: "reference", slot: 3, title: "Reference 3", required: false,
    hint: "Optional.", placeholder: "e.g. Only the stereo width of the pads." },
];

const $ = (sel) => document.querySelector(sel);
const state = {
  runId: null, run: null, files: {}, uploaded: {}, busy: false, editors: [],
  leadBusy: false, chatInFlight: false, events: [], next: 0, watching: false, openRows: new Set(),
};
// States in which the server is working on a /chat request (app/runs.py WORKING).
const WORKING = ["Researching", "FollowingUp", "Writing", "Humanizing", "Validating", "Rendering"];
const AUTO_START = "Sections confirmed — run the analysis.";

// --- Helpers ---
async function api(path, options = {}) {
  const r = await fetch(path, options);
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new Error(errorText(body?.detail) || `Request failed (${r.status}).`);
  return body;
}
function overlay(text) {
  $("#overlay").hidden = !text;
  if (text) $("#overlay-text").textContent = text;
}
function show(view) {
  for (const v of ["upload", "sections", "analysis", "results"]) $(`#view-${v}`).hidden = v !== view;
  const order = ["upload", "sections", "analysis", "report"];
  const current = view === "results" ? "report" : view;
  document.querySelectorAll(".step").forEach((el) => {
    const i = order.indexOf(el.dataset.step), c = order.indexOf(current);
    el.classList.toggle("active", i === c);
    el.classList.toggle("done", i < c);
  });
  window.scrollTo({ top: 0 });
}
function setRun(id) {
  state.runId = id;
  history.replaceState(null, "", id ? `#run=${id}` : location.pathname);
}
const mb = (n) => `${(n / 1024 / 1024).toFixed(1)} MB`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function refreshRun() { state.run = await api(`/api/runs/${state.runId}`); return state.run; }

// A Lead reply as HTML: escaped, with paragraphs, bullet lists and **bold**.
function md(text) {
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const bullet = /^\s*([-*]|\d+\.)\s+/;
  const out = [];
  let para = [], items = [];
  const flush = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    if (items.length) out.push(`<ul>${items.map((l) => `<li>${inline(l)}</li>`).join("")}</ul>`);
    para = [];
    items = [];
  };
  for (const line of String(text || "").trim().split("\n")) {
    if (!line.trim()) flush();
    else if (bullet.test(line)) { if (para.length) flush(); items.push(line.replace(bullet, "")); }
    else { if (items.length) flush(); para.push(line); }
  }
  flush();
  return out.join("");
}

// One message to the Lead (POST /chat, the starter's shape). The reply also lands in run.transcript.
async function sendChat(message) {
  state.chatInFlight = true;
  try {
    return await api("/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: state.runId }),
    });
  } finally {
    state.chatInFlight = false;
  }
}

// --- Upload view ---
function renderCards() {
  $("#cards").innerHTML = SLOTS.map((s, i) => `
    <article class="card" data-slot="${s.id}" style="--c:${TRACK_COLOURS[s.id]}">
      <div class="card-head">
        <span class="card-n">0${i + 1}</span><span class="dot"></span><span class="card-title">${s.title}</span>
        <span class="tag ${s.required ? "req" : ""}">${s.required ? "Required" : "Optional"}</span>
      </div>
      <label class="drop">
        <span class="drop-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></span>
        <span class="drop-text"><span class="drop-main">Drop audio or choose a file</span><span class="drop-sub">${s.hint}</span></span>
        <button class="remove" type="button" title="Remove" aria-label="Remove ${s.title}" hidden><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>
        <input type="file" accept=".wav,.mp3,audio/wav,audio/mpeg" />
      </label>
      <div class="status"></div>
      <div class="field">
        <label class="field-label" for="note-${s.id}">Note <span class="field-hint">${s.id === "user" ? "What should the Lead know?" : "What is this reference for?"}</span></label>
        <textarea id="note-${s.id}" rows="2" placeholder="${s.placeholder}"></textarea>
      </div>
    </article>`).join("");

  for (const card of document.querySelectorAll(".card")) {
    const id = card.dataset.slot, drop = card.querySelector(".drop"), input = card.querySelector("input");
    input.addEventListener("change", () => input.files[0] && pick(id, input.files[0]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      if (e.dataTransfer.files[0]) pick(id, e.dataTransfer.files[0]);
    });
    card.querySelector(".remove").addEventListener("click", (e) => { e.preventDefault(); unpick(id); });
  }
}

function card(id) { return document.querySelector(`.card[data-slot="${id}"]`); }

function setStatus(id, kind, html) {
  const el = card(id).querySelector(".status");
  el.className = `status ${kind || ""}`;
  el.innerHTML = html || "";
}

function pick(id, file) {
  const ext = file.name.toLowerCase().split(".").pop();
  const c = card(id);
  c.querySelector(".drop-main").textContent = file.name;
  c.querySelector(".drop-sub").textContent = mb(file.size);
  c.querySelector(".drop").classList.add("has-file");
  c.querySelector(".remove").hidden = false;
  delete state.uploaded[id];
  if (!["wav", "mp3"].includes(ext)) { state.files[id] = null; setStatus(id, "err", "Please choose a WAV or MP3 file."); }
  else if (file.size > MAX_BYTES) { state.files[id] = null; setStatus(id, "err", TOO_LARGE); }
  else { state.files[id] = file; setStatus(id, "", "Ready to upload"); }
  updatePrepare();
}

async function unpick(id) {
  const slot = SLOTS.find((s) => s.id === id), c = card(id);
  delete state.files[id];
  c.querySelector(".drop-main").textContent = "Drop audio or choose a file";
  c.querySelector(".drop-sub").textContent = slot.hint;
  c.querySelector(".drop").classList.remove("has-file");
  c.querySelector(".remove").hidden = true;
  c.querySelector("input").value = "";
  setStatus(id, "", "");
  if (state.uploaded[id] && state.runId) {
    delete state.uploaded[id];
    await api(`/api/runs/${state.runId}/tracks/${id}`, { method: "DELETE" }).catch(() => {});
  }
  updatePrepare();
}

function updatePrepare() {
  const has = (id) => state.files[id] || state.uploaded[id];
  const refs = ["ref1", "ref2", "ref3"].some(has);
  const invalid = Object.values(state.files).some((f) => f === null);
  $("#prepare").disabled = state.busy || !has("user") || !refs || invalid;
  $("#prepare-hint").textContent = state.busy ? "" : invalid ? "Remove or replace the files marked in red."
    : !has("user") ? "Add your track to continue." : !refs ? "Add at least one reference to continue." : "";
}

function upload(id, file) {
  const slot = SLOTS.find((s) => s.id === id);
  const form = new FormData();
  form.append("file", file);
  form.append("role", slot.role);
  if (slot.slot) form.append("slot", slot.slot);
  form.append("comment", card(id).querySelector("textarea").value);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/runs/${state.runId}/tracks`);
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      setStatus(id, "", `<span>Uploading ${pct}%</span><span class="bar"><span style="width:${pct}%"></span></span>`);
    };
    xhr.upload.onload = () => setStatus(id, "", '<span class="spinner"></span><span>Analysing tempo, loudness and spectrum…</span>');
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 200) resolve(body);
      else reject(new Error(errorText(body?.detail) || (xhr.status === 413 ? TOO_LARGE : `Upload failed (${xhr.status}).`)));
    };
    xhr.onerror = () => reject(new Error("The upload was interrupted. Check your connection and try again."));
    xhr.send(form);
  });
}

async function prepare() {
  state.busy = true;
  updatePrepare();
  $("#upload-error").textContent = "";
  try {
    if (!state.runId) setRun((await api("/api/runs", { method: "POST" })).run_id);
    await api(`/api/runs/${state.runId}/comments`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ global: $("#global-comment").value, tracks: {} }),
    });
    const pending = Object.entries(state.files).filter(([id, f]) => f && !state.uploaded[id]);
    const results = await Promise.allSettled(pending.map(async ([id, f]) => {
      try {
        const res = await upload(id, f);
        state.uploaded[id] = true;
        const g = res.track.grid;
        setStatus(id, "ok", `✓ Ready · ${fmtTime(res.track.source.duration_s)} · ${g.bpm.toFixed(g.bpm % 1 ? 2 : 0)} BPM`);
      } catch (err) {
        setStatus(id, "err", esc(err.message));
        throw err;
      }
    }));
    if (results.every((r) => r.status === "fulfilled")) await openSections();
    else $("#upload-error").textContent = "Some files could not be prepared. Fix them above and try again.";
  } catch (err) {
    $("#upload-error").textContent = err.message;
  } finally {
    state.busy = false;
    updatePrepare();
  }
}

async function demo() {
  overlay("Loading the demo tracks…");
  try {
    const run = await api("/api/demo", { method: "POST" });
    setRun(run.id);
    state.run = run;
    await openSections(run);
  } catch (err) {
    $("#upload-error").textContent = err.message;
  } finally {
    overlay(null);
  }
}

// --- Sections view, with the Lead's brief ---
async function openSections(run) {
  state.run = run || await refreshRun();
  overlay("Drawing waveforms…");
  try {
    const box = $("#editors");
    state.editors.forEach((e) => e.audio.pause());
    box.innerHTML = "";
    state.editors = [];
    const peaks = await Promise.all(state.run.tracks.map((t) => api(`/api/runs/${state.runId}/tracks/${t.id}/peaks`)));
    show("sections");
    state.run.tracks.forEach((t, i) => {
      const el = document.createElement("article");
      box.appendChild(el);
      const maps = state.run.section_maps[t.id];
      state.editors.push(new SectionEditor(el, {
        runId: state.runId, track: t, map: maps[maps.length - 1], peaks: peaks[i], onChange: updateConfirm,
      }));
    });
    renderLead();
    updateConfirm();
  } finally {
    overlay(null);
  }
  // The Lead reads the comments while the producer checks the sections.
  if (!state.run.transcript.length && !state.run.brief) await askLead(state.run.comments?.global?.trim() || "Please compare my track with the references.");
}

async function askLead(message) {
  state.leadBusy = true;
  renderLead();
  updateConfirm();
  try {
    await sendChat(message);
  } catch (err) {
    $("#lead-msg").innerHTML = `<p class="form-error">${esc(err.message)}</p>`;
  } finally {
    state.leadBusy = false;
  }
  await refreshRun();
  renderLead();
  updateConfirm();
}

function lastReply(run) {
  return [...(run.transcript || [])].reverse().find((m) => m.role === "assistant")?.text || "";
}

const CHECK = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>';

function renderLead() {
  const run = state.run, badge = $("#brief-badge");
  const asking = run.state === "NeedsClarification";
  badge.className = `badge ${state.leadBusy ? "live" : run.brief ? "ok" : asking ? "warn" : ""}`;
  badge.innerHTML = state.leadBusy ? '<span class="pulse"></span>Reading your notes'
    : run.brief ? `${CHECK}Brief recorded` : asking ? "Needs your answer" : "No brief yet";
  // While the Lead reads, a few skeleton lines stand in for its reply.
  $("#lead-msg").innerHTML = state.leadBusy
    ? `<div class="skel" role="status" aria-label="Reading your comments and the tracks">${[34, 92, 84, 88, 58].map((w) => `<i style="width:${w}%"></i>`).join("")}</div>`
    : md(lastReply(run)) || '<p class="muted">The Lead turns your comments into a brief for the analysis.</p>';
  $("#brief").innerHTML = run.brief ? renderBrief(run) : '<p class="brief-empty">The brief appears here once the Lead has read your notes.</p>';
  $("#lead-form").hidden = state.leadBusy;
  $("#lead-input").placeholder = asking ? "Your answer…" : "Tell the Lead what to change in the brief (optional)…";
}

function renderBrief(run) {
  const b = run.brief, name = (id) => run.tracks.find((t) => t.id === id)?.display_name || id;
  const list = (items) => `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  const row = (k, v, colour) => `<div><dt>${colour ? `<span class="dot" style="--c:${colour}"></span>` : ""}${esc(k)}</dt><dd>${v}</dd></div>`;
  const stage = b.production_stage ? b.production_stage[0].toUpperCase() + b.production_stage.slice(1) : "";
  const rows = [
    b.goals.length && row("Goals", list(b.goals)),
    b.preserve.length && row("Keep", list(b.preserve)),
    ...b.reference_roles.map((r) => row(name(r.track_id).replace(/^Reference\b/, "Ref"),
      esc(r.aspects.join(", ") || "general comparison")
        + (r.exclusions.length ? `<span class="not">not: ${esc(r.exclusions.join(", "))}</span>` : ""),
      TRACK_COLOURS[r.track_id])),
    stage && row("Stage", esc(stage)),
  ].filter(Boolean);
  return `<dl>${rows.join("")}</dl>`;
}

function updateConfirm() {
  const accepted = state.editors.filter((e) => e.accepted).length;
  const ready = state.run?.brief && !state.leadBusy;
  $("#confirm").disabled = !ready;
  $("#confirm-status").innerHTML = state.leadBusy ? "Waiting for the Lead to read your notes…"
    : !state.run?.brief ? "Answer the Lead above to continue."
    : `<b>${accepted} of ${state.editors.length}</b> accepted · confirming accepts the rest as they are`;
}

async function confirmAll() {
  const btn = $("#confirm");
  btn.disabled = true;
  overlay("Saving sections…");
  try {
    for (const e of state.editors) if (!e.accepted || e.dirty) await e.save(true);
    state.run = await api(`/api/runs/${state.runId}/sections/confirm`, { method: "POST" });
    state.editors.forEach((e) => e.audio.pause());
  } catch (err) {
    alert(err.message);
    btn.disabled = false;
    return;
  } finally {
    overlay(null);
  }
  startAnalysis(AUTO_START);
}

// --- Analysis: the research run and its trace ---
function startAnalysis(message) {
  show("analysis");
  $("#analysis-notice").hidden = true;
  sendChat(message).catch((err) => notice(`The request failed: ${err.message}`, "Try again", () => startAnalysis(message)));
  watch();
}

function notice(text, action, onClick) {
  $("#analysis-notice").hidden = false;
  $("#analysis-notice-text").innerHTML = text;
  $("#resume").textContent = action;
  $("#resume").onclick = onClick;
}

// Poll progress until no request is running for this run, then show whatever the run reached.
async function watch() {
  if (state.watching) return;
  state.watching = true;
  try {
    while (true) {
      const p = await pollOnce().catch(() => null);
      if (p && !p.busy && !state.chatInFlight) break;
      await sleep(1500);
    }
  } finally {
    state.watching = false;
  }
  route(await refreshRun());
}

async function pollOnce() {
  const p = await api(`/api/runs/${state.runId}/progress?since=${state.next}`);
  p.events.forEach((e, i) => { state.events[p.from + i] = e; });
  state.next = p.next;
  $("#stage").textContent = p.stage;
  $("#stage-meta").textContent = `${p.rounds_used ? `Round ${p.rounds_used} · ` : ""}${p.usage.calls} model calls · $${p.usage.cost_usd.toFixed(2)}`;
  funFor(p.busy ? state.events.filter((e) => e?.status === "running") : []);
  // This view shows the current request: from the Lead's first turn of the latest message.
  const first = state.events.findLastIndex((e) => e?.agent === "lead" && e.kind === "model_call" && e.args?.turn === 1);
  const current = state.events.slice(Math.max(first, 0)).filter(Boolean);
  renderTrace($("#trace"), current);
  state.dial = dialState(current, p.busy);
  runDial();
  return p;
}

let funTimer = null, funPool = [];
function funFor(running) {
  const keys = running.filter((e) => e.name.startsWith("researcher:")).map((e) => e.name.split(":")[1]);
  for (const agent of ["writer", "humanizer"]) if (running.some((e) => e.agent === agent)) keys.push(agent);
  if (!keys.length && running.some((e) => e.agent === "lead" && e.kind === "model_call")) keys.push("lead");
  const pool = (keys.length ? keys : ["general"]).flatMap((k) => PROGRESS_MESSAGES[k] || []);
  if (!running.length) { clearInterval(funTimer); funTimer = null; $("#fun").textContent = ""; return; }
  if (pool.join() === funPool.join() && funTimer) return;
  funPool = pool;
  const next = () => { $("#fun").textContent = funPool[Math.floor(Math.random() * funPool.length)]; };
  next();
  clearInterval(funTimer);
  funTimer = setInterval(next, 4000);
}

const SPECIALISTS = { kick: "Kick", low_end: "Low end", tonal_loudness: "Tone & loudness", stereo: "Stereo" };
// One slate ramp, so the trace and the dial read as one system; an error is the only red.
const SPECIALIST_COLOURS = { kick: "#334a66", low_end: "#56708f", tonal_loudness: "#7d93ad", stereo: "#a3b4c8" };

// --- The dial: one ring per researcher, showing its latest call (a full turn is 60 s) ---
const RINGS = [["kick", 166], ["low_end", 144], ["tonal_loudness", 122], ["stereo", 100]];
const TURN_S = 60;
const polar = (r, a) => [220 + r * Math.sin(a), 220 - r * Math.cos(a)];
const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
function arcPath(r, frac) {
  const f = Math.min(0.9999, Math.max(0.0001, frac)), [x, y] = polar(r, f * Math.PI * 2);
  return `M220 ${220 - r}A${r} ${r} 0 ${f > 0.5 ? 1 : 0} 1 ${x.toFixed(2)} ${y.toFixed(2)}`;
}

function buildDial() {
  if ($("#dial svg")) return;
  let s = '<svg class="dial" viewBox="0 0 440 440" role="img" aria-label="Time each researcher has spent on its latest call">';
  for (let i = 0; i < 60; i++) {
    const a = (i * Math.PI) / 30, long = i % 5 === 0, [x1, y1] = polar(long ? 192 : 197, a), [x2, y2] = polar(204, a);
    s += `<line class="tick ${long ? "l" : ""}" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`;
  }
  for (const sec of [15, 30, 45]) {
    const [x, y] = polar(180, (sec * Math.PI) / 30);
    s += `<text class="rlab" x="${x.toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="middle">${sec}s</text>`;
  }
  s += '<g class="sweep"><circle cx="220" cy="16" r="3.2"/></g>';
  s += [0, -2.4, -4.8].map((d) => `<circle class="rp" cx="220" cy="220" r="62" style="animation-delay:${d}s"/>`).join("");
  for (const [k, r] of RINGS) {
    s += `<circle class="ring" cx="220" cy="220" r="${r}"/><path class="arc" data-k="${k}"/>`;
    s += `<g class="head" data-k="${k}"><circle class="halo" cx="220" cy="${220 - r}" r="9"/><circle class="knob" cx="220" cy="${220 - r}" r="4.5"/></g>`;
    s += `<text class="rlab" x="212" y="${220 - r + 3}" text-anchor="end">${SPECIALISTS[k].replace(" & loudness", "")}</text>`;
  }
  s += '<text class="clock" x="220" y="226" text-anchor="middle">0:00</text><text class="clock-l" x="220" y="250" text-anchor="middle">LEAD</text></svg>';
  $("#dial").innerHTML = s;
}

// Each specialist's latest researcher call in this request, the latest round, and the request's span.
function dialState(events, busy) {
  const calls = {};
  for (const e of events) if (e.name?.startsWith("researcher:")) calls[e.name.split(":")[1]] = e;
  const starts = events.map((e) => Date.parse(e.started_at));
  const ends = events.map((e) => (e.ended_at ? Date.parse(e.ended_at) : Date.now()));
  return {
    calls, busy, round: Math.max(0, ...Object.values(calls).map((e) => e.round || 0)),
    t0: starts.length ? Math.min(...starts) : null, t1: busy || !ends.length ? null : Math.max(...ends),
  };
}

function callTiming(e, d, now) {
  const running = e.status === "running" && d.busy;
  const end = e.ended_at ? Date.parse(e.ended_at) : running ? now : d.t1 ?? now;
  return { running, secs: Math.max(0, (end - Date.parse(e.started_at)) / 1000) };
}

function updateDial() {
  const d = state.dial, svg = $("#dial svg");
  if (!d || !svg) return;
  const now = Date.now();
  svg.classList.toggle("idle", !d.busy);
  for (const [k, r] of RINGS) {
    const e = d.calls[k], arc = svg.querySelector(`.arc[data-k="${k}"]`), head = svg.querySelector(`.head[data-k="${k}"]`);
    if (!e) { arc.removeAttribute("d"); head.style.display = "none"; continue; }
    const { running, secs } = callTiming(e, d, now), frac = Math.min(1, secs / TURN_S);
    const colour = e.status === "error" ? "var(--err)" : SPECIALIST_COLOURS[k];
    arc.setAttribute("d", arcPath(r, frac));
    arc.style.setProperty("--c", colour);
    head.style.display = "";
    head.style.setProperty("--c", colour);
    head.classList.toggle("live", running);
    head.setAttribute("transform", `rotate(${(frac * 360).toFixed(2)} 220 220)`);
  }
  svg.querySelector(".clock").textContent = clock(d.t0 ? Math.max(0, ((d.t1 ?? now) - d.t0) / 1000) : 0);
  svg.querySelector(".clock-l").textContent = d.round ? `ROUND ${d.round}` : "LEAD";
  // The legend only changes when a shown time does, so write it at most every tenth of a second.
  const rows = RINGS.map(([k]) => {
    const e = d.calls[k];
    if (!e) return { k, cls: "off", t: "–", note: "Not run yet" };
    const { running, secs } = callTiming(e, d, now);
    const how = e.status === "error" ? "stopped with an error" : running ? "working" : e.status === "running" ? "stopped" : "done";
    return { k, cls: e.status === "error" ? "err" : running ? "live" : "", t: `${secs.toFixed(1)} s`, note: `Round ${e.round || 1} · ${how}` };
  });
  const key = JSON.stringify(rows);
  if (key === updateDial.key) return;
  updateDial.key = key;
  $("#dial-legend").innerHTML = rows.map(({ k, cls, t, note }) => `<li class="${cls}" style="--c:${SPECIALIST_COLOURS[k]}">
    <span class="k"></span><b>${SPECIALISTS[k]}</b><span class="t">${t}</span><span class="s">${note}</span></li>`).join("");
}

// Between polls, keep the running arcs and the clock moving.
let dialFrame = 0;
function runDial() {
  buildDial();
  cancelAnimationFrame(dialFrame);
  const step = () => {
    updateDial();
    if (state.dial?.busy && !$("#view-analysis").hidden) dialFrame = requestAnimationFrame(step);
  };
  step();
}

function traceLabel(e) {
  if (e.kind === "model_call" && e.agent === "lead") {
    return { text: `Lead · ${e.args?.phase?.replace("_", "-") ?? ""} turn ${e.args?.turn ?? ""}`, colour: "var(--text)" };
  }
  if (e.name.startsWith("researcher:")) {
    const k = e.name.split(":")[1];
    return { text: `${SPECIALISTS[k] || k} researcher`, colour: SPECIALIST_COLOURS[k] };
  }
  if (e.name === "run_researcher") {
    const k = e.args?.specialist;
    return { text: `run_researcher · ${SPECIALISTS[k] || k}`, colour: `color-mix(in srgb, ${SPECIALIST_COLOURS[k] || "#8a9099"} 38%, transparent)` };
  }
  if (e.kind === "helper") return { text: e.name + (e.cached ? " (cached)" : ""), colour: "#cdd2d9" };
  return { text: e.name, colour: "var(--faint)" };
}

const CHEVRON = '<svg class="chev" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3.5l4.5 4.5L6 12.5"/></svg>';

// Events in tree order: each event followed by what it started (a researcher call by its
// measurements and its model call), roots in the order they started.
function treeOrder(events) {
  const ids = new Set(events.map((e) => e.task_id)), children = {}, roots = [];
  for (const e of events) (e.parent_task_id && ids.has(e.parent_task_id) ? (children[e.parent_task_id] ||= []) : roots).push(e);
  const out = [], depth = {};
  const visit = (e, d) => { depth[e.task_id] = d; out.push(e); (children[e.task_id] || []).forEach((c) => visit(c, d + 1)); };
  roots.forEach((e) => visit(e, 0));
  return { list: out, depth };
}

function renderTrace(el, events) {
  const { list, depth } = treeOrder(events.filter(Boolean));
  if (!list.length) { el.innerHTML = '<div class="muted">Nothing has run yet.</div>'; return; }
  const t = (s) => Date.parse(s), now = Date.now();
  const t0 = Math.min(...list.map((e) => t(e.started_at)));
  const t1 = Math.max(t0 + 1000, ...list.map((e) => (e.ended_at ? t(e.ended_at) : now)));
  // A time axis with about six ticks across the request.
  const span = (t1 - t0) / 1000, step = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300].find((s) => span / s <= 6) || 600;
  const ticks = [];
  for (let s = 0; s <= span; s += step) ticks.push(`<span style="left:${(s / span) * 100}%">${s}${s ? "" : " s"}</span>`);
  el.innerHTML = `<div class="tr-axis" aria-hidden="true"><span>Task</span><span class="ax">${ticks.join("")}</span><span class="r">Duration</span></div>`
    + list.map((e) => {
      const { text, colour } = traceLabel(e), d = depth[e.task_id];
      const start = t(e.started_at), end = e.ended_at ? t(e.ended_at) : now;
      const left = ((start - t0) / (t1 - t0)) * 100, width = ((end - start) / (t1 - t0)) * 100;
      const secs = (end - start) / 1000, running = e.status === "running";
      const c = e.status === "error" ? "var(--err)" : e.status === "interrupted" ? "var(--warn)" : colour;
      const time = running ? `<span class="live"></span>${secs.toFixed(1)} s` : secs < 0.05 ? "&lt;0.1 s" : `${secs.toFixed(1)} s`;
      const args = typeof e.args === "string" ? e.args : JSON.stringify(e.args, null, 1);
      return `<details class="tr ${e.kind} ${e.status}" data-id="${esc(e.task_id)}" style="--d:${d}" ${state.openRows.has(e.task_id) ? "open" : ""}>
      <summary>
        <span class="tr-l">${d ? '<span class="elbow"></span>' : ""}${CHEVRON}${e.round ? `<b class="rd">R${e.round}</b>` : ""}<span class="nm">${esc(text)}</span>${e.status === "error" ? '<span class="badge err">error</span>' : ""}</span>
        <span class="tr-lane"><span class="tr-bar ${running ? "run" : ""} ${width < 0.9 ? "dot" : ""}" style="left:${left}%;width:${Math.max(width, 0.5)}%;--c:${c}"></span></span>
        <span class="tr-time">${time}</span>
      </summary>
      <div class="tr-body">
        <div class="tr-meta">${esc(e.kind)} · ${esc(e.agent)} · ${esc(e.task_id)}${e.parent_task_id ? ` ← ${esc(e.parent_task_id)}` : ""}${e.cost_usd ? ` · $${e.cost_usd.toFixed(3)}` : ""}</div>
        <div><span class="tr-k">Arguments</span><pre>${esc(args)}</pre></div>
        <div><span class="tr-k">Result</span><pre class="res">${esc(e.result || (running ? "Running…" : ""))}</pre></div>
      </div>
    </details>`;
    }).join("");
  el.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => {
    if (d.open) state.openRows.add(d.dataset.id); else state.openRows.delete(d.dataset.id);
  }));
}

// Show the view that matches where the run is.
async function route(run) {
  const confirmed = run.tracks.length && run.tracks.every((t) => run.section_maps[t.id].at(-1).confirmed);
  if (run.research?.finished && !WORKING.includes(run.state) && run.state !== "Interrupted") return showResults(run);
  if (run.state === "Interrupted") {
    show("analysis");
    await pollOnce().catch(() => {});
    return notice("The analysis was interrupted, most likely by a server restart. It can continue where it stopped.",
      "Continue", () => startAnalysis("Continue."));
  }
  if (WORKING.includes(run.state) && run.research?.finished) {  // the report is being written by a running request
    show("analysis");
    return watch();
  }
  if (WORKING.includes(run.state) || (confirmed && run.brief)) {
    show("analysis");
    await pollOnce().catch(() => {});
    const said = lastReply(run);
    return notice(run.state === "Researching" && said ? md(said) : "The sections are confirmed and the brief is ready.",
      run.state === "Researching" ? "Continue" : "Run the analysis",
      () => startAnalysis(run.state === "Researching" ? "Continue." : AUTO_START));
  }
  if (run.tracks.length && run.state !== "Draft" && run.state !== "Preparing") return openSections(run);
  show("upload");
}

// --- Results: the written report, or the Lead's accepted findings when there is none ---
function findingHtml(f) {
  if (f.kind === "observation") {
    const values = f.values.map((v) => `<span class="val mono" title="${esc(v.name)}">${esc(v.value)} ${esc(v.unit)}</span>`).join("");
    return `<p>${esc(f.text)}</p>${values ? `<div class="vals">${values}</div>` : ""}`;
  }
  if (f.kind === "comparison") return `<p><span class="k">vs ${esc(f.target)}</span> ${esc(f.text)}</p>`;
  if (f.kind === "interpretation") {
    return `<p><span class="k">Possible cause</span> ${esc(f.text)}</p>${f.alternatives.length ? `<p class="muted small">Or: ${esc(f.alternatives.join("; "))}</p>` : ""}`;
  }
  return `<p><span class="k try">Try</span> ${esc(f.action)}</p><p class="muted small">Check: ${esc(f.how_to_verify)}</p>`;
}

function figuresFor(run, taskIds) {
  const seen = new Set(), out = [];
  for (const tid of taskIds) {
    for (const ev of run.research.results[tid]?.evidence || []) {
      for (const f of run.evidence_index[ev.id]?.figures || []) {
        if (seen.has(f.id)) continue;
        seen.add(f.id);
        out.push(`<figure><img loading="lazy" src="/api/runs/${state.runId}/figures/${f.id}" alt="${esc(f.title)}"><figcaption>${esc(f.title)}</figcaption></figure>`);
      }
    }
  }
  return out.join("");
}

async function showResults(run) {
  const report = run.report?.status === "ready" ? run.report : null;
  $("#report-bar").hidden = $("#report-frame").hidden = !report;
  $("#findings-view").hidden = !!report;
  if (report) showReport(run, report);
  else showFindings(run);
  renderChat(run);
  $("#res-usage").textContent = `${run.usage.calls} model calls · $${run.usage.cost_usd.toFixed(2)}`;
  show("results");
  state.next = 0;
  state.events = [];
  const p = await api(`/api/runs/${state.runId}/progress?since=0`);
  state.events = p.events;
  state.next = p.next;
  renderResultsTrace();
}

// The results page's trace, with its size on the closed summary line.
function renderResultsTrace() {
  renderTrace($("#trace-results"), state.events);
  const ev = state.events.filter(Boolean), t = (s) => Date.parse(s);
  if (!ev.length) { $("#trace-summary").textContent = ""; return; }
  const secs = (Math.max(...ev.map((e) => (e.ended_at ? t(e.ended_at) : Date.now()))) - Math.min(...ev.map((e) => t(e.started_at)))) / 1000;
  const whole = Math.round(secs), span = secs < 60 ? `${secs.toFixed(1)} s` : `${Math.floor(whole / 60)} min ${String(whole % 60).padStart(2, "0")} s`;
  $("#trace-summary").textContent = `${ev.length} steps · ${span}`;
}

const WARN = '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" aria-hidden="true"><path d="M8 3.75v5.5M8 12.1v.15"/></svg>';
const LEAD_AVATAR = '<span class="lead-avatar" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="4.5" stroke="currentColor" stroke-width="1.5"/></svg></span>';
const leadBubble = (html, extra = "") => `<div class="msg assistant ${extra}">${LEAD_AVATAR}<div class="bubble"><span class="who">Lead</span>${html}</div></div>`;

// The report is its own page (the same HTML as the PDF), shown in a frame sized to its content.
function showReport(run, report) {
  const base = `/api/runs/${state.runId}`;
  const lost = run.state === "Partial" ? run.state_history.at(-1)?.detail : "";
  $("#report-status").innerHTML = `<span class="ic">${lost ? WARN : CHECK}</span><span>${esc(lost ? `Incomplete: ${lost}.` : "Every number in it was checked against the measurements.")}</span>`;
  $("#report-status").classList.toggle("warn", !!lost);
  $("#report-open").href = `${base}/report`;
  $("#report-pdf").href = `${base}/report.pdf`;
  $("#report-pdf").hidden = !report.pdf_path;
  const frame = $("#report-frame");
  const src = `${base}/report?at=${encodeURIComponent(report.at || "")}`;
  if (frame.dataset.src === src) return;
  frame.dataset.src = src;
  frame.onload = () => {
    const doc = frame.contentDocument;
    const fit = () => { frame.style.height = `${doc.documentElement.scrollHeight}px`; };
    fit();
    new frame.contentWindow.ResizeObserver(fit).observe(doc.body);
    // In-page links (a source to its figure) scroll this page, since the frame never scrolls itself.
    doc.addEventListener("click", (e) => {
      const a = e.target.closest('a[href^="#"]');
      const el = a && doc.getElementById(a.getAttribute("href").slice(1));
      if (!el) return;
      e.preventDefault();
      window.scrollTo({ top: frame.getBoundingClientRect().top + window.scrollY + el.getBoundingClientRect().top - 90, behavior: "smooth" });
    });
  };
  frame.src = src;
}

function showFindings(run) {
  const fin = run.research.finished, accepted = fin.accepted_findings || {};
  $("#findings-note").textContent = run.report?.error
    ? `The written report is not available (${run.report.error}). These are the findings the Lead accepted, with the measurements behind them.`
    : "These are the findings the Lead accepted, with the measurements behind them.";
  $("#res-summary").textContent = fin.summary || "Research ended without firm conclusions.";
  $("#priorities").innerHTML = fin.priorities.map((p, i) => `
    <article class="priority">
      <div class="priority-n">${String(i + 1).padStart(2, "0")}</div>
      <div><h3>${esc(p.title)}</h3><p class="why">${esc(p.why)}</p>
        ${p.finding_refs.map((r) => accepted[r]).filter(Boolean).map((f) => `<div class="finding">${findingHtml(f)}</div>`).join("")}
      </div>
    </article>`).join("");
  $("#preserve").innerHTML = fin.preserve.map((x) => `<li>${esc(x)}</li>`).join("") || '<li class="muted">Nothing noted.</li>';
  $("#limitations").innerHTML = fin.limitations.map((x) => `<li>${esc(x)}</li>`).join("") || '<li class="muted">None noted.</li>';
  const groups = {};
  for (const f of Object.values(accepted)) (groups[f.specialist] ||= []).push(f);
  $("#findings").innerHTML = Object.entries(groups).map(([k, fs]) => `
    <section class="group-card" style="--c:${SPECIALIST_COLOURS[k]}">
      <h3><span class="dot"></span>${esc(SPECIALISTS[k] || k)}</h3>
      ${fs.map((f) => `<div class="finding">${findingHtml(f)}</div>`).join("")}
      <div class="figs">${figuresFor(run, [...new Set(fs.map((f) => f.task_id))])}</div>
    </section>`).join("");
}

function renderChat(run) {
  const since = run.research.finished?.at || "";
  const msgs = (run.transcript || []).filter((m) => m.at > since);
  $("#chat-log").innerHTML = msgs.map((m) => (m.role === "assistant" ? leadBubble(md(m.text)) : `<p class="msg user">${esc(m.text)}</p>`)).join("");
}

async function askFollowUp(e) {
  e.preventDefault();
  const input = $("#chat-input"), text = input.value.trim();
  if (!text) return;
  input.value = "";
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<p class="msg user">${esc(text)}</p>${leadBubble('<p class="thinking" role="status"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><b id="follow-stage">Looking into it…</b></p>', "pending")}`);
  const timer = setInterval(async () => {
    const p = await api(`/api/runs/${state.runId}/progress?since=${state.next}`).catch(() => null);
    if (!p) return;
    p.events.forEach((ev, i) => { state.events[p.from + i] = ev; });
    state.next = p.next;
    if ($("#follow-stage")) $("#follow-stage").textContent = p.stage;
    renderResultsTrace();
  }, 1500);
  try {
    await sendChat(text);
  } catch (err) {
    alert(err.message);
  } finally {
    clearInterval(timer);
  }
  renderChat(await refreshRun());
  const p = await api(`/api/runs/${state.runId}/progress?since=0`);
  state.events = p.events;
  state.next = p.next;
  renderResultsTrace();
}

// --- Start ---
async function start() {
  renderCards();
  updatePrepare();
  $("#prepare").addEventListener("click", prepare);
  $("#demo").addEventListener("click", demo);
  $("#confirm").addEventListener("click", confirmAll);
  $("#edit-sections").addEventListener("click", () => openSections(state.run));
  $("#chat-form").addEventListener("submit", askFollowUp);
  $("#lead-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#lead-input").value.trim();
    if (!text || state.leadBusy) return;
    $("#lead-input").value = "";
    askLead(text);
  });
  window.addEventListener("beforeunload", (e) => {
    if (!$("#view-sections").hidden && state.editors.some((ed) => ed.dirty)) e.preventDefault();
  });
  const id = new URLSearchParams(location.hash.slice(1)).get("run");
  if (id) {
    try {
      const run = await api(`/api/runs/${id}`);
      setRun(id);
      state.run = run;
      $("#global-comment").value = run.comments?.global || "";
      for (const t of run.tracks) {
        state.uploaded[t.id] = true;
        const c = card(t.id);
        c.querySelector(".drop-main").textContent = t.filename;
        c.querySelector(".drop-sub").textContent = mb(t.source.bytes);
        c.querySelector(".drop").classList.add("has-file");
        c.querySelector(".remove").hidden = false;
        c.querySelector("textarea").value = t.comment || "";
        setStatus(t.id, "ok", `✓ Ready · ${fmtTime(t.source.duration_s)} · ${t.grid.bpm.toFixed(2)} BPM`);
      }
      updatePrepare();
      const progress = await api(`/api/runs/${id}/progress?since=0`);
      if (progress.busy) {
        show("analysis");
        state.events = progress.events;
        state.next = progress.next;
        return watch();
      }
      return route(run);
    } catch {
      setRun(null);
    }
  }
  show("upload");
}

start();
