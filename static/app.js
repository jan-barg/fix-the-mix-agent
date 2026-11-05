// Fix the Mix front end: upload → sections → confirmed (DESIGN.md §9). The run ID lives in the URL
// hash, so a reload returns to the same run.
import { SectionEditor, TRACK_COLOURS, labelColour, fmtTime, errorText } from "/static/editor.js";

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
const state = { runId: null, run: null, files: {}, uploaded: {}, busy: false, editors: [] };

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
  for (const v of ["upload", "sections", "confirmed"]) $(`#view-${v}`).hidden = v !== view;
  const order = ["upload", "sections", "analysis", "report"];
  const current = view === "confirmed" ? "analysis" : view;
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
        setStatus(id, "err", err.message);
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

// --- Sections view ---
async function openSections(run) {
  state.run = run || await api(`/api/runs/${state.runId}`);
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
    updateConfirm();
  } finally {
    overlay(null);
  }
}

function updateConfirm() {
  const accepted = state.editors.filter((e) => e.accepted).length;
  $("#confirm-status").textContent = `${accepted} of ${state.editors.length} accepted · confirming accepts the rest as they are`;
}

async function confirmAll() {
  const btn = $("#confirm");
  btn.disabled = true;
  overlay("Saving sections…");
  try {
    for (const e of state.editors) if (!e.accepted || e.dirty) await e.save(true);
    state.run = await api(`/api/runs/${state.runId}/sections/confirm`, { method: "POST" });
    state.editors.forEach((e) => e.audio.pause());
    renderSummary();
    show("confirmed");
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    overlay(null);
  }
}

function renderSummary() {
  $("#summary").innerHTML = state.run.tracks.map((t) => {
    const maps = state.run.section_maps[t.id], m = maps[maps.length - 1];
    const pills = m.sections.map((s) => `<span class="pill" style="background:${labelColour(s.label)}" title="${fmtTime(s.start_s)}–${fmtTime(s.end_s)}">${s.label} · ${s.end_bar - s.start_bar} bars</span>`).join("");
    return `<div class="summary-row" style="--c:${TRACK_COLOURS[t.id]}">
      <div class="ed-head"><span class="dot"></span><span class="ed-title">${t.display_name}</span><span class="ed-file">${t.filename}</span>
      <span class="spacer"></span><span class="badge">${m.bpm.toFixed(2)} BPM · ${m.grid_source === "user" ? "your grid" : "detected grid"}</span></div>
      <div class="pills">${pills}</div></div>`;
  }).join("");
}

// --- Start ---
async function start() {
  renderCards();
  updatePrepare();
  $("#prepare").addEventListener("click", prepare);
  $("#demo").addEventListener("click", demo);
  $("#confirm").addEventListener("click", confirmAll);
  $("#back-to-sections").addEventListener("click", () => openSections(state.run));
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
      const allConfirmed = run.tracks.length && run.tracks.every((t) => run.section_maps[t.id].at(-1).confirmed);
      if (allConfirmed) { renderSummary(); show("confirmed"); return; }
      if (run.state === "ReviewingSections") { await openSections(run); return; }
    } catch {
      setRun(null);
    }
  }
  show("upload");
}

start();
