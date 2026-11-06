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
  $("#cards").innerHTML = SLOTS.map((s) => `
    <article class="card" data-slot="${s.id}" style="--c:${TRACK_COLOURS[s.id]}">
      <div class="card-head">
        <span class="dot"></span><span class="card-title">${s.title}</span>
        <span class="tag ${s.required ? "req" : ""}">${s.required ? "Required" : "Optional"}</span>
      </div>
      <label class="drop">
        <span class="drop-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></span>
        <span class="drop-text"><span class="drop-main">Drop audio or choose a file</span><span class="drop-sub">${s.hint}</span></span>
        <button class="remove" type="button" title="Remove" hidden>✕</button>
        <input type="file" accept=".wav,.mp3,audio/wav,audio/mpeg" />
      </label>
      <div class="status"></div>
      <textarea rows="2" placeholder="${s.placeholder}"></textarea>
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

function renderLead() {
  const run = state.run, badge = $("#brief-badge");
  const asking = run.state === "NeedsClarification";
  badge.className = `badge ${state.leadBusy ? "" : run.brief ? "ok" : asking ? "warn" : ""}`;
  badge.textContent = state.leadBusy ? "Reading your notes…" : run.brief ? "Brief recorded" : asking ? "Needs your answer" : "No brief yet";
  $("#lead-msg").innerHTML = state.leadBusy
    ? '<p class="thinking"><span class="spinner"></span>Reading your comments and the tracks…</p>'
    : md(lastReply(run)) || '<p class="muted">The Lead turns your comments into a brief for the analysis.</p>';
  $("#brief").innerHTML = run.brief ? renderBrief(run) : "";
  $("#lead-form").hidden = state.leadBusy;
  $("#lead-input").placeholder = asking ? "Your answer…" : "Tell the Lead what to change in the brief (optional)…";
}

function renderBrief(run) {
  const b = run.brief, name = (id) => run.tracks.find((t) => t.id === id)?.display_name || id;
  const rows = [
    ["Goals", b.goals.join("; ")],
    ["Keep", b.preserve.join("; ")],
    ...b.reference_roles.map((r) => [name(r.track_id), (r.aspects.join(", ") || "general comparison")
      + (r.exclusions.length ? ` · not: ${r.exclusions.join(", ")}` : "")]),
    ["Stage", b.production_stage],
  ].filter(([, v]) => v);
  return `<dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

function updateConfirm() {
  const accepted = state.editors.filter((e) => e.accepted).length;
  const ready = state.run?.brief && !state.leadBusy;
  $("#confirm").disabled = !ready;
  $("#confirm-status").textContent = state.leadBusy ? "Waiting for the Lead to read your notes…"
    : !state.run?.brief ? "Answer the Lead above to continue."
    : `${accepted} of ${state.editors.length} accepted · confirming accepts the rest as they are`;
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
  $("#stage-meta").textContent = `${p.rounds_used ? `round ${p.rounds_used} · ` : ""}${p.usage.calls} model calls · $${p.usage.cost_usd.toFixed(2)}`;
  $("#stage-pulse").classList.toggle("idle", !p.busy);
  funFor(p.busy ? state.events.filter((e) => e?.status === "running") : []);
  // This view shows the current request: from the Lead's first turn of the latest message.
  const first = state.events.findLastIndex((e) => e?.agent === "lead" && e.kind === "model_call" && e.args?.turn === 1);
  renderTrace($("#trace"), state.events.slice(Math.max(first, 0)));
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
const SPECIALIST_COLOURS = { kick: "#ff8a5b", low_end: "#3fa7d6", tonal_loudness: "#4cc27a", stereo: "#a98be0" };

function traceLabel(e) {
  if (e.kind === "model_call" && e.agent === "lead") {
    return { text: `Lead · ${e.args?.phase?.replace("_", "-") ?? ""} turn ${e.args?.turn ?? ""}`, colour: "var(--accent)" };
  }
  if (e.name.startsWith("researcher:")) {
    const k = e.name.split(":")[1];
    return { text: `${SPECIALISTS[k] || k} researcher`, colour: SPECIALIST_COLOURS[k] };
  }
  if (e.name === "run_researcher") {
    const k = e.args?.specialist;
    return { text: `run_researcher · ${SPECIALISTS[k] || k}`, colour: `color-mix(in srgb, ${SPECIALIST_COLOURS[k] || "#888"} 45%, #2a3039)` };
  }
  if (e.kind === "helper") return { text: e.name + (e.cached ? " (cached)" : ""), colour: "#59626e" };
  return { text: e.name, colour: "#c9a96a" };
}

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
  el.innerHTML = list.map((e) => {
    const { text, colour } = traceLabel(e);
    const start = t(e.started_at), end = e.ended_at ? t(e.ended_at) : now;
    const left = ((start - t0) / (t1 - t0)) * 100, width = Math.max(((end - start) / (t1 - t0)) * 100, 0.5);
    const secs = ((end - start) / 1000).toFixed(1);
    const args = typeof e.args === "string" ? e.args : JSON.stringify(e.args, null, 1);
    return `<details class="tr ${e.kind} ${e.status}" data-id="${e.task_id}" ${state.openRows.has(e.task_id) ? "open" : ""}>
      <summary>
        <span class="tr-label" style="padding-left:${depth[e.task_id] * 14}px">${e.round ? `<b class="tr-round">R${e.round}</b>` : ""}${esc(text)}</span>
        <span class="tr-lane"><span class="tr-bar" style="left:${left}%;width:${width}%;--c:${colour}"></span></span>
        <span class="tr-time mono">${e.status === "running" ? "…" : `${secs}s`}</span>
      </summary>
      <div class="tr-body">
        <div class="tr-k">${esc(e.kind)} · ${esc(e.agent)} · ${esc(e.task_id)}${e.parent_task_id ? ` ← ${esc(e.parent_task_id)}` : ""}${e.cost_usd ? ` · $${e.cost_usd.toFixed(3)}` : ""}</div>
        <div class="tr-k">Arguments</div><pre>${esc(args)}</pre>
        <div class="tr-k">Result</div><pre>${esc(e.result || "")}</pre>
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
  return `<p><span class="k">Try</span> ${esc(f.action)}</p><p class="muted small">Check: ${esc(f.how_to_verify)}</p>`;
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
  renderTrace($("#trace-results"), state.events);
}

// The report is its own page (the same HTML as the PDF), shown in a frame sized to its content.
function showReport(run, report) {
  const base = `/api/runs/${state.runId}`;
  const lost = run.state === "Partial" ? run.state_history.at(-1)?.detail : "";
  $("#report-status").textContent = lost ? `Incomplete: ${lost}.` : "Every number in it was checked against the measurements.";
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
      <div class="priority-n">${i + 1}</div>
      <div><h3>${esc(p.title)}</h3><p class="muted">${esc(p.why)}</p>
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
  $("#chat-log").innerHTML = msgs.map((m) => `<div class="msg ${m.role}">${m.role === "assistant" ? md(m.text) : `<p>${esc(m.text)}</p>`}</div>`).join("");
}

async function askFollowUp(e) {
  e.preventDefault();
  const input = $("#chat-input"), text = input.value.trim();
  if (!text) return;
  input.value = "";
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="msg user"><p>${esc(text)}</p></div><div class="msg assistant pending"><p class="thinking"><span class="spinner"></span><span id="follow-stage">Looking into it…</span></p></div>`);
  const timer = setInterval(async () => {
    const p = await api(`/api/runs/${state.runId}/progress?since=${state.next}`).catch(() => null);
    if (!p) return;
    p.events.forEach((ev, i) => { state.events[p.from + i] = ev; });
    state.next = p.next;
    if ($("#follow-stage")) $("#follow-stage").textContent = p.stage;
    renderTrace($("#trace-results"), state.events);
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
  renderTrace($("#trace-results"), state.events);
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
