// Section editor: a Rekordbox-style waveform and phrase editor drawn on canvas (SPEC §2.3, DESIGN.md §9).
// The waveform comes from precomputed peaks (100 buckets/s with low/mid/high band levels), so the
// browser never decodes the file; playback streams the original audio with range requests.

export const LABELS = ["intro", "build", "drop", "breakdown", "verse", "chorus", "bridge", "outro", "unknown"];
export const LABEL_COLOURS = {
  intro: "#a9bedb", build: "#e6c27a", drop: "#e2907a", breakdown: "#9fcbb4", verse: "#bdb0dc",
  chorus: "#e7a9c2", bridge: "#9dcfd9", outro: "#b3b8c4", unknown: "#d2d5da",
};
export const TRACK_COLOURS = { user: "#14161b", ref1: "#2a7891", ref2: "#5d8a2c", ref3: "#7a62a8" };
const CUSTOM_COLOUR = "#d3bf98";
// The canvas is a dark display whatever the page around it: a cool haze for the whole signal,
// the bass as a solid orange core (DESIGN.md §9).
const SCREEN = {
  well: "#0e1115", band: "#0a0c0f", line: "rgba(255,255,255,0.06)", pill: "#1c2129",
  grid: ["rgba(255,255,255,0.03)", "rgba(255,255,255,0.065)", "rgba(255,255,255,0.16)"],
  tick: ["rgba(255,255,255,0.2)", "rgba(255,255,255,0.8)"], num: ["#7c8592", "#eef1f5"],
  bound: ["rgba(255,255,255,0.26)", "rgba(255,255,255,0.85)"], handle: "#1c2129", handleLine: ["rgba(255,255,255,0.4)", "#fff"],
  tint: [0.045, 0.11], haze: "rgba(150,168,196,0.20)", body: "rgba(150,168,196,0.30)", edge: "rgba(226,234,245,0.42)",
  core: ["#b8441a", "#f2763a"], cursor: "#f2763a", playhead: "#fff",
  ovHaze: "rgba(160,178,204,0.32)", ovCore: "#f07a3e", ovDim: "rgba(14,17,21,0.72)", ovBox: "#fff",
};
const RULER = 26, STRIP = 38, MIN_LEN = 0.5, MAX_PX_PER_S = 600;
const START_TOLERANCE_S = 0.05;  // grid.py: a downbeat this close before 0 s is bar 1 at the file start

export const labelColour = (label) => LABEL_COLOURS[label] || CUSTOM_COLOUR;

// FastAPI sends a string for our own errors and a list of {msg} for request validation errors.
// Track names, file names and custom labels come from the user: escape them before they go into markup.
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
// Write text only when it differs, so a button under the pointer keeps its nodes.
const setText = (el, text) => { if (el.textContent !== text) el.textContent = text; };
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

export function errorText(detail) {
  return Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail;
}
export function fmtTime(t) {
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

function decode(b64, Type) {
  const bin = atob(b64), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Type(bytes.buffer);
}

function rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
// A label colour lifted toward white, so section names stay readable on the dark strip.
function lift(hex, k = 0.3) {
  const n = parseInt(hex.slice(1), 16), up = (v) => Math.round(v + (255 - v) * k);
  return `rgb(${up((n >> 16) & 255)},${up((n >> 8) & 255)},${up(n & 255)})`;
}
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Band shares stacked from the centre out: the bass share is the core, bass plus mids the body.
const share = (c) => { const s = c.l + c.m * 0.8 + c.h * 0.55 || 1; return [c.l / s, (c.m * 0.8) / s]; };
// A 1-2-3-2-1 filter, so the outline reads as one shape rather than a row of bars.
function smooth(v) {
  const n = v.length, o = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    o[i] = (v[Math.max(0, i - 2)] + 2 * v[Math.max(0, i - 1)] + 3 * v[i] + 2 * v[Math.min(n - 1, i + 1)] + v[Math.min(n - 1, i + 2)]) / 9;
  }
  return o;
}
// A filled shape mirrored around the centre line, and its top and bottom outline.
function mirror(ys, mid) {
  const p = new Path2D(), n = ys.length;
  p.moveTo(0, mid - ys[0]);
  for (let i = 1; i < n; i++) p.lineTo(i, mid - ys[i]);
  for (let i = n - 1; i >= 0; i--) p.lineTo(i, mid + ys[i]);
  p.closePath();
  return p;
}
function outline(ys, mid) {
  const p = new Path2D(), n = ys.length;
  p.moveTo(0, mid - ys[0]);
  for (let i = 1; i < n; i++) p.lineTo(i, mid - ys[i]);
  p.moveTo(0, mid + ys[0]);
  for (let i = 1; i < n; i++) p.lineTo(i, mid + ys[i]);
  return p;
}

const icon = {
  play: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M4 2.5v11l9-5.5z"/></svg>',
  pause: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M4 2.5h3v11H4zM9 2.5h3v11H9z"/></svg>',
};

let uid = 0;

export class SectionEditor {
  constructor(root, { runId, track, map, peaks, onChange }) {
    this.root = root;
    this.runId = runId;
    this.track = track;
    this.onChange = onChange || (() => {});
    this.duration = track.source.duration_s;
    this.bpm = map.bpm;
    this.downbeat = map.downbeat_s;
    this.gridSource = map.grid_source;
    this.checked = map.origin === "checked"; // a demo map the producer already checked
    this.sections = map.sections.map((s) => ({ ...s }));
    this.accepted = map.accepted || map.confirmed;
    this.dirty = false;
    this.revision = 0; // bumped on every edit, so a save that returns after further edits cannot erase them
    this.selected = 0;
    this.cursor = 0;
    this.snap = "bar";
    this.peaks = {
      rate: peaks.rate, length: peaks.length,
      min: decode(peaks.min, Int8Array), max: decode(peaks.max, Int8Array),
      low: decode(peaks.low, Uint8Array), mid: decode(peaks.mid, Uint8Array), high: decode(peaks.high, Uint8Array),
    };
    this.audio = new Audio(`/api/runs/${runId}/tracks/${track.id}/audio`);
    this.audio.preload = "metadata";
    this.stopAt = null;
    this.fonts = { ui: cssVar("--font") || "system-ui, sans-serif", mono: cssVar("--mono") || "ui-monospace, monospace" };
    this.render();
    this.fit();
    this.bind();
    this.draw();
    document.fonts?.ready.then(() => this.draw()); // redraw the canvas text once the web fonts arrive
  }

  // --- Grid ---
  get beat() { return 60 / this.bpm; }
  get barLen() { return 4 * this.beat; }
  snapTime(t, mode = this.snap) {
    if (mode === "off") return t;
    const p = mode === "bar" ? this.barLen : this.beat;
    const k = Math.round((t - this.downbeat) / p);
    return Math.min(this.duration, Math.max(0, this.downbeat + k * p));
  }
  barOf(t) { return (t - this.downbeat) / this.barLen + 1; }
  // Same rule as sections.bar_span on the server: positions within 2% of a bar line count as on it,
  // and a section from the file start is bar 1 when the first downbeat is within START_TOLERANCE_S.
  barSpan(s) {
    const pos = (t) => { const x = (t - this.downbeat) / this.barLen, r = Math.round(x); return Math.abs(x - r) < 0.02 ? r : x; };
    let first = Math.max(0, Math.floor(pos(s.start_s)) + 1);
    if (s.start_s <= 0 && this.downbeat <= START_TOLERANCE_S) first = Math.max(first, 1);
    return [first, Math.ceil(pos(s.end_s)) + 1];
  }

  // --- View ---
  get width() { return this.mainCanvas.clientWidth || 1; }
  fit() { this.pxPerS = this.width / this.duration; this.viewStart = 0; }
  clampView() {
    const minPx = this.width / this.duration;
    this.pxPerS = Math.min(MAX_PX_PER_S, Math.max(minPx, this.pxPerS));
    const span = this.width / this.pxPerS;
    this.viewStart = Math.min(Math.max(0, this.viewStart), Math.max(0, this.duration - span));
  }
  zoom(factor, anchorX = this.width / 2) {
    const t = this.xToT(anchorX);
    this.pxPerS *= factor;
    this.clampView();
    this.viewStart = t - anchorX / this.pxPerS;
    this.clampView();
    this.draw();
  }
  xToT(x) { return this.viewStart + x / this.pxPerS; }
  tToX(t) { return (t - this.viewStart) * this.pxPerS; }

  // --- DOM ---
  render() {
    const t = this.track, colour = TRACK_COLOURS[t.id] || "#999", id = ++uid;
    const options = LABELS.map((l) => `<option value="${l}">${l}</option>`).join("") + '<option value="__custom">custom…</option>';
    this.root.className = "editor";
    this.root.tabIndex = 0;
    this.root.style.setProperty("--c", colour);
    this.root.innerHTML = `
      <div class="ed-head">
        <span class="dot"></span>
        <span class="ed-title">${esc(t.display_name)}</span>
        <span class="ed-file" title="${esc(t.filename)}">${esc(t.filename)}</span>
        <span class="badge grid-badge"></span>
        <span class="badge state-badge"></span>
        <button class="btn sm accept" type="button">Accept sections</button>
      </div>
      <div class="canvas-wrap ov-wrap"><canvas class="overview"></canvas></div>
      <div class="canvas-wrap"><canvas class="main-canvas"></canvas></div>
      <div class="toolbar">
        <div class="tg"><span class="tg-l">Play</span><div class="group">
          <button class="tb play" data-act="play" title="Play / pause (Space)">${icon.play}<span>Play</span></button>
          <button class="tb" data-act="play-section" title="Play the selected section">Section</button>
          <button class="tb" data-act="play-boundary" title="Play 4 s either side of the nearest boundary">Boundary <span class="mono">±4 s</span></button>
        </div></div>
        <div class="tg"><span class="tg-l">Edit</span><div class="group">
          <button class="tb" data-act="split" title="Split at the cursor (S)">Split</button>
          <button class="tb" data-act="merge" title="Merge with the next section (M)">Merge →</button>
          <span class="tb-sep"></span>
          <label class="sr" for="lbl-${id}">Label</label>
          <select id="lbl-${id}" data-act="label" title="Label of the selected section">${options}</select>
        </div></div>
        <div class="tg"><span class="tg-l">Snap</span><div class="group">
          <button class="tb" data-snap="bar">Bar</button>
          <button class="tb" data-snap="beat">Beat</button>
          <button class="tb" data-snap="off">Off</button>
        </div></div>
        <div class="tg"><span class="tg-l">Zoom</span><div class="group">
          <button class="tb mono" data-act="zoom-out" title="Zoom out (−)" aria-label="Zoom out">−</button>
          <button class="tb" data-act="fit" title="Whole track">Fit</button>
          <button class="tb mono" data-act="zoom-in" title="Zoom in (+)" aria-label="Zoom in">+</button>
        </div></div>
        <div class="tg"><label class="tg-l" for="bpm-${id}">BPM · downbeat</label><div class="group">
          <input id="bpm-${id}" type="number" step="0.01" min="60" max="200" data-act="bpm">
          <span class="tb-sep"></span>
          <button class="tb mono" data-nudge="-beat" title="Move the downbeat one beat earlier">−1 beat</button>
          <button class="tb mono" data-nudge="-0.01" title="10 ms earlier">−10 ms</button>
          <button class="tb mono" data-nudge="0.01" title="10 ms later">+10 ms</button>
          <button class="tb mono" data-nudge="beat" title="Move the downbeat one beat later">+1 beat</button>
        </div></div>
        <div class="tg tg-readout"><span class="tg-l">Position</span><output class="readout"></output></div>
      </div>`;
    this.$ = (sel) => this.root.querySelector(sel);
    this.overview = this.$(".overview");
    this.mainCanvas = this.$(".main-canvas");
    this.$('[data-act="bpm"]').value = this.bpm.toFixed(2);
    this.updateChrome();
  }

  updateChrome() {
    this.root.classList.toggle("accepted", this.accepted);
    const stateBadge = this.$(".state-badge");
    stateBadge.className = `badge state-badge ${this.accepted ? "ok" : ""}`;
    stateBadge.textContent = this.accepted ? "✓ Accepted" : this.dirty ? "Edited" : this.checked ? "Checked by the producer" : "Suggested";
    const g = this.track.grid;
    const gridBadge = this.$(".grid-badge");
    const noGrid = this.gridSource === "detected" && g.downbeat_confidence === "none";
    const checkGrid = this.gridSource === "detected" && !this.checked && (!g.constant_tempo || g.downbeat_confidence !== "likely");
    gridBadge.className = `badge grid-badge ${checkGrid ? "warn" : ""}`;
    gridBadge.textContent = this.gridSource === "user" ? "Your grid" : noGrid ? "No beat found" : checkGrid ? "Check the grid" : "Grid detected";
    gridBadge.title = noGrid ? "No steady beat was detected, so this is a placeholder 120 BPM grid: set the BPM and nudge the downbeat."
      : checkGrid ? "The detected bar starts may be off: listen at a boundary and nudge the downbeat if needed." : "";
    setText(this.$(".accept"), this.accepted ? "Accepted" : "Accept sections");
    this.$(".accept").classList.toggle("primary", !this.accepted);
    this.root.querySelectorAll("[data-snap]").forEach((b) => b.classList.toggle("on", b.dataset.snap === this.snap));
    const s = this.sections[this.selected];
    const select = this.$('[data-act="label"]');
    if (s) {
      if (!LABELS.includes(s.label) && ![...select.options].some((o) => o.value === s.label)) {
        select.add(new Option(s.label, s.label), select.options.length - 1);
      }
      select.value = s.label;
    }
    // Rewrite the button only when it changes: replacing its content under a pressed mouse button
    // loses the click, so a Pause pressed during playback would do nothing.
    const playing = !this.audio.paused;
    const playBtn = this.$('[data-act="play"]');
    if (playBtn.dataset.state !== String(playing)) {
      playBtn.dataset.state = String(playing);
      playBtn.innerHTML = `${playing ? icon.pause : icon.play}<span>${playing ? "Pause" : "Play"}</span>`;
    }
    this.updateReadout();
  }

  // The playhead position, every animation frame while playing.
  updateReadout() {
    const t = this.audio.paused ? this.cursor : this.audio.currentTime;
    // A short lead-in before the first downbeat reads as bar 1, as in barSpan.
    const bar = Math.max(this.barOf(t), this.downbeat <= START_TOLERANCE_S ? 1 : 0);
    const beatIn = Math.floor((bar - Math.floor(bar)) * 4) + 1;
    setText(this.$(".readout"), `${fmtTime(t)} · bar ${Math.floor(bar)}.${beatIn}`);
  }

  changed() {
    this.revision++;
    this.dirty = true;
    this.accepted = false;
    this.updateChrome();
    this.draw();
    this.onChange(this);
  }

  // --- Events ---
  bind() {
    const main = this.mainCanvas;
    let drag = null;
    const pos = (e, el) => { const r = el.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

    main.addEventListener("pointerdown", (e) => {
      const { x } = pos(e, main);
      main.setPointerCapture(e.pointerId);
      const b = this.hitBoundary(x);
      drag = b ? { kind: "boundary", index: b, moved: false } : { kind: "click", x0: x, moved: false };
      this.root.focus({ preventScroll: true });
    });
    main.addEventListener("pointermove", (e) => {
      const { x } = pos(e, main);
      if (!drag) { main.style.cursor = this.hitBoundary(x) ? "ew-resize" : "crosshair"; return; }
      if (drag.kind === "boundary") {
        const i = drag.index, prev = this.sections[i - 1], cur = this.sections[i];
        let t = e.altKey ? this.xToT(x) : this.snapTime(this.xToT(x));
        t = Math.min(cur.end_s - MIN_LEN, Math.max(prev.start_s + MIN_LEN, t));
        if (Math.abs(t - cur.start_s) > 1e-6) {
          prev.end_s = cur.start_s = t;
          prev.label_confidence = cur.label_confidence = null;
          drag.moved = true;
          this.draw();
        }
      } else if (Math.abs(x - drag.x0) > 3) {
        drag.moved = true;
        this.viewStart -= (e.movementX || 0) / this.pxPerS;  // drag the waveform to scroll
        this.clampView();
        this.draw();
      }
    });
    main.addEventListener("pointerup", (e) => {
      const { x } = pos(e, main);
      if (drag?.kind === "boundary" && drag.moved) this.changed();
      if (drag?.kind === "click" && !drag.moved) {
        const t = this.xToT(x);
        this.cursor = Math.min(this.duration, Math.max(0, t));
        this.selected = this.sectionAt(t);
        if (!this.audio.paused) { this.audio.currentTime = this.cursor; this.stopAt = null; }
        this.updateChrome();
        this.draw();
      }
      drag = null;
    });
    main.addEventListener("wheel", (e) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        this.zoom(Math.exp(-e.deltaY * 0.01), pos(e, main).x);
      } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
        e.preventDefault();
        this.viewStart += (e.deltaX || e.deltaY) / this.pxPerS;
        this.clampView();
        this.draw();
      }
    }, { passive: false });

    // The overview: drag an edge of the window in view to zoom, drag inside it to scroll, drag anywhere else to
    // frame a new range; a click centres the view there, a double-click shows the whole track.
    const ov = this.overview;
    let ovDrag = null;
    const ovT = (e) => Math.min(this.duration, Math.max(0, (pos(e, ov).x / ov.clientWidth) * this.duration));
    const ovHit = (e) => {
      const x = pos(e, ov).x, k = ov.clientWidth / this.duration;
      const a = this.viewStart * k, b = (this.viewStart + this.width / this.pxPerS) * k;
      if (b - a >= ov.clientWidth - 2) return "frame";  // the whole track is in view: no window to grab
      if (Math.abs(x - a) <= 8) return "start";
      if (Math.abs(x - b) <= 8) return "end";
      return x > a && x < b ? "inside" : "frame";
    };
    const showRange = (t0, t1) => {
      this.pxPerS = this.width / Math.max(t1 - t0, this.width / MAX_PX_PER_S);
      this.viewStart = t0;
      this.clampView();
      this.draw();
    };
    ov.addEventListener("pointerdown", (e) => {
      ov.setPointerCapture(e.pointerId);
      const t = ovT(e), start = this.viewStart, end = start + this.width / this.pxPerS;
      ovDrag = { hit: ovHit(e), t0: t, x0: pos(e, ov).x, offset: t - start, start, end, moved: false };
      if (ovDrag.hit === "inside") ov.style.cursor = "grabbing";
    });
    ov.addEventListener("pointermove", (e) => {
      const d = ovDrag;
      if (!d) {
        ov.style.cursor = { start: "ew-resize", end: "ew-resize", inside: "grab" }[ovHit(e)] || "crosshair";
        return;
      }
      if (!d.moved && Math.abs(pos(e, ov).x - d.x0) < 3) return;
      d.moved = true;
      const t = ovT(e), least = this.width / MAX_PX_PER_S;
      if (d.hit === "start") showRange(Math.min(t, d.end - least), d.end);
      else if (d.hit === "end") showRange(d.start, Math.max(t, d.start + least));
      else if (d.hit === "inside") { this.viewStart = t - d.offset; this.clampView(); this.draw(); }
      else showRange(Math.min(d.t0, t), Math.max(d.t0, t));
    });
    ov.addEventListener("pointerup", (e) => {
      if (ovDrag && !ovDrag.moved) {  // a click: same zoom, centred there
        this.viewStart = ovDrag.t0 - this.width / this.pxPerS / 2;
        this.clampView();
        this.draw();
      }
      ovDrag = null;
      ov.style.cursor = { start: "ew-resize", end: "ew-resize", inside: "grab" }[ovHit(e)] || "crosshair";
    });
    ov.addEventListener("dblclick", () => { this.fit(); this.draw(); });

    this.root.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn || !this.root.contains(btn)) return;
      if (btn.dataset.snap) { this.snap = btn.dataset.snap; this.updateChrome(); return; }
      if (btn.dataset.nudge) { this.nudge(btn.dataset.nudge); return; }
      if (btn.classList.contains("accept")) { this.accept(); return; }
      ({
        play: () => this.togglePlay(),
        "play-section": () => this.playSection(),
        "play-boundary": () => this.playBoundary(),
        split: () => this.split(),
        merge: () => this.merge(),
        "zoom-in": () => this.zoom(2),
        "zoom-out": () => this.zoom(0.5),
        fit: () => { this.fit(); this.draw(); },
      })[btn.dataset.act]?.();
    });
    this.$('[data-act="label"]').addEventListener("change", (e) => this.relabel(e.target.value));
    this.$('[data-act="bpm"]').addEventListener("change", (e) => {
      const v = parseFloat(e.target.value);
      if (!(v >= 60 && v <= 200)) { e.target.value = this.bpm.toFixed(2); return; }
      this.bpm = v;
      this.gridSource = "user";
      this.changed();
    });
    this.root.addEventListener("keydown", (e) => {
      if (e.target.closest("input, select, textarea")) return;
      const k = e.key.toLowerCase();
      if (k === " ") { e.preventDefault(); this.togglePlay(); }
      else if (k === "s") this.split();
      else if (k === "m") this.merge();
      else if (k === "+" || k === "=") this.zoom(2);
      else if (k === "-") this.zoom(0.5);
    });

    this.audio.addEventListener("play", () => {
      window.dispatchEvent(new CustomEvent("ftm:play", { detail: this }));
      this.updateChrome();
      this.tick();
    });
    this.audio.addEventListener("pause", () => { this.updateChrome(); this.draw(); });
    window.addEventListener("ftm:play", (e) => { if (e.detail !== this && !this.audio.paused) this.audio.pause(); });
    new ResizeObserver(() => { this.clampView(); this.draw(); }).observe(this.mainCanvas);
  }

  hitBoundary(x) {
    for (let i = 1; i < this.sections.length; i++) {
      if (Math.abs(this.tToX(this.sections[i].start_s) - x) <= 6) return i;
    }
    return 0;
  }
  sectionAt(t) {
    const i = this.sections.findIndex((s) => t >= s.start_s && t < s.end_s);
    return i < 0 ? this.sections.length - 1 : i;
  }

  // --- Editing ---
  split() {
    const t = this.snapTime(this.cursor);
    const i = this.sectionAt(t), s = this.sections[i];
    if (t - s.start_s < MIN_LEN || s.end_s - t < MIN_LEN) return;
    this.sections.splice(i + 1, 0, { ...s, id: `n${Date.now().toString(36)}`, start_s: t, label_confidence: null });
    s.end_s = t;
    s.label_confidence = null;
    this.selected = i + 1;
    this.changed();
  }
  merge() {
    const i = this.selected;
    if (i >= this.sections.length - 1) return;
    this.sections[i].end_s = this.sections[i + 1].end_s;
    this.sections[i].label_confidence = null;
    this.sections.splice(i + 1, 1);
    this.changed();
  }
  relabel(value) {
    const s = this.sections[this.selected];
    if (!s) return;
    if (value === "__custom") {
      const text = (prompt("Custom label (e.g. 'drop 2', 'riser', 'main groove')", s.label) || "").trim().slice(0, 40);
      if (!text) { this.updateChrome(); return; }
      value = text;
    }
    s.label = value;
    s.label_confidence = null;
    this.changed();
  }
  nudge(kind) {
    const delta = kind === "beat" ? this.beat : kind === "-beat" ? -this.beat : parseFloat(kind);
    // Keep the first downbeat in [-START_TOLERANCE_S, bar - START_TOLERANCE_S), as grid.py does.
    const bar = this.barLen, d = this.downbeat + delta + START_TOLERANCE_S;
    this.downbeat = ((d % bar) + bar) % bar - START_TOLERANCE_S;
    this.gridSource = "user";
    this.changed();
  }

  // --- Playback ---
  playRange(a, b) {
    this.audio.currentTime = Math.max(0, a);
    this.stopAt = b;
    this.audio.play();
  }
  togglePlay() {
    if (this.audio.paused) { this.stopAt = null; this.audio.currentTime = this.cursor; this.audio.play(); }
    else { this.cursor = this.audio.currentTime; this.audio.pause(); }
  }
  playSection() {
    const s = this.sections[this.selected];
    if (s) this.playRange(s.start_s, s.end_s);
  }
  playBoundary() {
    if (this.sections.length < 2) return;
    let best = 1;
    for (let i = 1; i < this.sections.length; i++) {
      if (Math.abs(this.sections[i].start_s - this.cursor) < Math.abs(this.sections[best].start_s - this.cursor)) best = i;
    }
    const b = this.sections[best].start_s;
    this.playRange(b - 4, b + 4);
  }
  tick() {
    if (this.audio.paused) return;
    const t = this.audio.currentTime;
    if (this.stopAt !== null && t >= this.stopAt) { this.audio.pause(); this.stopAt = null; return; }
    const span = this.width / this.pxPerS;
    if (t < this.viewStart || t > this.viewStart + span * 0.92) { this.viewStart = t - span * 0.1; this.clampView(); }
    this.draw();
    this.updateReadout();
    requestAnimationFrame(() => this.tick());
  }

  // --- Saving ---
  payload(accepted) {
    return {
      sections: this.sections.map(({ id, label, start_s, end_s }) => ({ id, label, start_s, end_s })),
      bpm: this.bpm, downbeat_s: this.downbeat, grid_source: this.gridSource, accepted,
    };
  }
  async save(accepted = true) {
    const revision = this.revision;
    const r = await fetch(`/api/runs/${this.runId}/tracks/${this.track.id}/sections`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this.payload(accepted)),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(errorText(body.detail) || "Could not save the sections.");
    if (this.revision !== revision) return body; // edited while saving: keep the newer local edits, still unsaved
    this.sections = body.sections;
    this.accepted = body.accepted;
    this.dirty = false;
    this.updateChrome();
    this.draw();
    return body;
  }
  async accept() {
    const btn = this.$(".accept");
    btn.disabled = true;
    try { await this.save(true); this.onChange(this); }
    catch (err) { alert(err.message); }
    finally { btn.disabled = false; }
  }

  // --- Drawing ---
  setup(canvas) {
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  column(t0, t1) {
    const p = this.peaks, i0 = Math.max(0, Math.floor(t0 * p.rate)), i1 = Math.min(p.length, Math.max(i0 + 1, Math.ceil(t1 * p.rate)));
    let lo = 0, hi = 0, l = 0, m = 0, h = 0;
    for (let i = i0; i < i1; i++) {
      if (p.min[i] < lo) lo = p.min[i];
      if (p.max[i] > hi) hi = p.max[i];
      if (p.low[i] > l) l = p.low[i];
      if (p.mid[i] > m) m = p.mid[i];
      if (p.high[i] > h) h = p.high[i];
    }
    return { amp: Math.max(hi, -lo) / 127, l: l / 255, m: m / 255, h: h / 255 };
  }

  drawWave(ctx, w, mid, amp, t0, pxPerS) {
    // Three smoothed layers from the centre out: the whole signal as a cool haze, bass plus mids as a
    // denser body, and the bass share as a solid orange core, so kicks and bass lines read at a glance.
    const n = Math.ceil(w) + 1, outer = new Float32Array(n), body = new Float32Array(n), core = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const c = this.column(t0 + i / pxPerS, t0 + (i + 1) / pxPerS), a = c.amp * amp, [sl, sm] = share(c);
      outer[i] = a; body[i] = a * (sl + sm); core[i] = a * sl;
    }
    const o = smooth(outer);
    ctx.fillStyle = SCREEN.haze; ctx.fill(mirror(o, mid));
    ctx.fillStyle = SCREEN.body; ctx.fill(mirror(smooth(body), mid));
    const g = ctx.createLinearGradient(0, mid - amp * 0.7, 0, mid + amp * 0.7);
    g.addColorStop(0, SCREEN.core[0]); g.addColorStop(0.5, SCREEN.core[1]); g.addColorStop(1, SCREEN.core[0]);
    ctx.fillStyle = g; ctx.fill(mirror(smooth(core), mid));
    ctx.strokeStyle = SCREEN.edge; ctx.lineWidth = 1; ctx.stroke(outline(o, mid));
  }

  draw() {
    if (!this.mainCanvas) return;
    this.drawMain();
    this.drawOverview();
  }

  drawMain() {
    const { ctx, w, h } = this.setup(this.mainCanvas);
    const waveTop = RULER, stripTop = h - STRIP, waveH = stripTop - waveTop, mid = waveTop + waveH / 2;
    const tEnd = this.xToT(w);
    ctx.fillStyle = SCREEN.well;
    ctx.fillRect(0, 0, w, h);

    // Section tints behind the waveform.
    this.sections.forEach((s, i) => {
      const x0 = this.tToX(s.start_s), x1 = this.tToX(s.end_s);
      if (x1 < 0 || x0 > w) return;
      ctx.fillStyle = rgba(labelColour(s.label), SCREEN.tint[i === this.selected ? 1 : 0]);
      ctx.fillRect(x0, waveTop, x1 - x0, waveH);
    });

    // Ruler band.
    ctx.fillStyle = SCREEN.band;
    ctx.fillRect(0, 0, w, RULER);
    ctx.fillStyle = SCREEN.line;
    ctx.fillRect(0, RULER - 1, w, 1);

    // Grid: beats when there is room, bars, 8-bar phrases; bar ticks and numbers on the ruler, phrase numbers in a pill.
    const pxPerBeat = this.beat * this.pxPerS, pxPerBar = 4 * pxPerBeat;
    const labelEvery = [1, 2, 4, 8, 16, 32, 64].find((n) => n * pxPerBar >= 46) || 64;
    const firstBeat = Math.floor((this.viewStart - this.downbeat) / this.beat);
    ctx.textBaseline = "middle";
    for (let k = firstBeat; ; k++) {
      const t = this.downbeat + k * this.beat;
      if (t > tEnd) break;
      if (t < -START_TOLERANCE_S) continue;
      const x = Math.round(this.tToX(Math.max(0, t))) + 0.5;
      const isBar = ((k % 4) + 4) % 4 === 0, bar = Math.floor(k / 4) + 1;
      if (!isBar && pxPerBeat < 7) continue;
      if (isBar && pxPerBar < 3 && (bar - 1) % 8 !== 0) continue;
      const phrase = isBar && (bar - 1) % 8 === 0;
      ctx.strokeStyle = SCREEN.grid[phrase ? 2 : isBar ? 1 : 0];
      ctx.beginPath(); ctx.moveTo(x, waveTop); ctx.lineTo(x, stripTop); ctx.stroke();
      if (!isBar) continue;
      ctx.fillStyle = SCREEN.tick[phrase ? 1 : 0];
      ctx.fillRect(x - 0.5, phrase ? RULER - 11 : RULER - 6, 1, phrase ? 10 : 5);
      if ((bar - 1) % labelEvery === 0) {
        const text = String(bar);
        ctx.font = `${phrase ? 500 : 400} 10.5px ${this.fonts.mono}`;
        if (phrase) {
          ctx.fillStyle = SCREEN.pill;
          ctx.beginPath(); ctx.roundRect(x + 3, 3, ctx.measureText(text).width + 10, 15, 5); ctx.fill();
        }
        ctx.fillStyle = SCREEN.num[phrase ? 1 : 0];
        ctx.fillText(text, x + 8, 11);
      }
    }

    this.drawWave(ctx, w, mid, (waveH / 2 - 10) * 0.94, this.viewStart, this.pxPerS);

    // Section strip: the selected section filled in its colour, the others as tinted pills with coloured names.
    ctx.fillStyle = SCREEN.band;
    ctx.fillRect(0, stripTop, w, STRIP);
    ctx.fillStyle = SCREEN.line;
    ctx.fillRect(0, stripTop, w, 1);
    ctx.font = `600 12px ${this.fonts.ui}`;
    this.sections.forEach((s, i) => {
      const x0 = Math.max(0, this.tToX(s.start_s)), x1 = Math.min(w, this.tToX(s.end_s));
      if (x1 <= 0 || x0 >= w) return;
      const col = labelColour(s.label), sel = i === this.selected;
      ctx.fillStyle = rgba(col, sel ? 0.92 : 0.16);
      ctx.beginPath(); ctx.roundRect(x0 + 2, stripTop + 7, Math.max(0, x1 - x0 - 4), STRIP - 14, 8); ctx.fill();
      const guess = s.label_confidence === "guess" ? "?" : "";
      const [b0, b1] = this.barSpan(s), room = x1 - x0 - 26;
      const full = `${s.label}${guess} · ${b1 - b0} bars`, short = `${s.label}${guess}`;
      const text = ctx.measureText(full).width <= room ? full : ctx.measureText(short).width <= room ? short : "";
      if (text) {
        ctx.save();
        ctx.beginPath(); ctx.rect(x0 + 6, stripTop, x1 - x0 - 12, STRIP); ctx.clip();
        ctx.fillStyle = sel ? "#14161b" : lift(col);
        ctx.fillText(text, x0 + 13, stripTop + STRIP / 2 + 0.5);
        ctx.restore();
      }
    });

    // Boundaries, with a grab handle where each meets the strip; the selected section's edges are brighter.
    for (let i = 1; i < this.sections.length; i++) {
      const x = Math.round(this.tToX(this.sections[i].start_s)) + 0.5;
      if (x < -6 || x > w + 6) continue;
      const hot = i === this.selected || i - 1 === this.selected;
      ctx.strokeStyle = SCREEN.bound[hot ? 1 : 0];
      ctx.lineWidth = hot ? 1.5 : 1;
      ctx.beginPath(); ctx.moveTo(x, RULER); ctx.lineTo(x, h - 6); ctx.stroke();
      ctx.lineWidth = 1;
      ctx.fillStyle = SCREEN.handle;
      ctx.strokeStyle = SCREEN.handleLine[hot ? 1 : 0];
      ctx.beginPath(); ctx.roundRect(x - 4.5, stripTop - 12, 9, 24, 4.5); ctx.fill(); ctx.stroke();
      ctx.fillStyle = SCREEN.handleLine[hot ? 1 : 0];
      ctx.fillRect(x - 1.5, stripTop - 5, 1, 10);
      ctx.fillRect(x + 0.5, stripTop - 5, 1, 10);
    }

    // Edit cursor and playhead.
    const cx = Math.round(this.tToX(this.cursor)) + 0.5;
    ctx.setLineDash([3, 3]); ctx.strokeStyle = SCREEN.cursor;
    ctx.beginPath(); ctx.moveTo(cx, RULER); ctx.lineTo(cx, stripTop); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = SCREEN.cursor;
    ctx.beginPath(); ctx.moveTo(cx - 5, RULER); ctx.lineTo(cx + 5, RULER); ctx.lineTo(cx, RULER + 6); ctx.closePath(); ctx.fill();
    if (!this.audio.paused) {
      const px = Math.round(this.tToX(this.audio.currentTime)) + 0.5;
      ctx.strokeStyle = SCREEN.playhead; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
      ctx.lineWidth = 1;
    }
  }

  drawOverview() {
    const { ctx, w, h } = this.setup(this.overview);
    const pxPerS = w / this.duration, mid = (h - 6) / 2, half = mid - 4;
    ctx.fillStyle = SCREEN.well;
    ctx.fillRect(0, 0, w, h);
    const haze = new Path2D(), core = new Path2D();
    for (let x = 0; x < w; x++) {
      const c = this.column(x / pxPerS, (x + 1) / pxPerS), a = c.amp * half, k = a * share(c)[0];
      haze.rect(x, mid - a, 1, a * 2);
      core.rect(x, mid - k, 1, k * 2);
    }
    ctx.fillStyle = SCREEN.ovHaze; ctx.fill(haze);
    ctx.fillStyle = SCREEN.ovCore; ctx.fill(core);
    this.sections.forEach((s) => {
      ctx.fillStyle = labelColour(s.label);
      ctx.fillRect(s.start_s * pxPerS + 1, h - 6, Math.max(1, (s.end_s - s.start_s) * pxPerS - 2), 4);
    });
    // The part in view: the rest dimmed, a rounded box around it (none when the whole track fits).
    const vx = this.viewStart * pxPerS, vw = (this.width / this.pxPerS) * pxPerS;
    if (vw < w - 2) {
      ctx.fillStyle = SCREEN.ovDim;
      ctx.fillRect(0, 0, vx, h);
      ctx.fillRect(vx + vw, 0, w - vx - vw, h);
      ctx.strokeStyle = SCREEN.ovBox; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.roundRect(vx + 0.75, 0.75, Math.max(4, vw - 1.5), h - 1.5, 7); ctx.stroke();
      ctx.lineWidth = 1;
      // Grips on both edges: drag them to zoom.
      for (const gx of [Math.max(4, vx + 0.75), Math.min(w - 4, vx + vw - 0.75)]) {
        ctx.fillStyle = SCREEN.ovBox;
        ctx.beginPath(); ctx.roundRect(gx - 3.5, h / 2 - 10, 7, 20, 3.5); ctx.fill();
        ctx.fillStyle = SCREEN.well;
        ctx.fillRect(gx - 0.5, h / 2 - 5, 1, 10);
      }
    }
    const t = this.audio.paused ? this.cursor : this.audio.currentTime;
    ctx.fillStyle = this.audio.paused ? SCREEN.cursor : SCREEN.playhead;
    ctx.fillRect(Math.round(t * pxPerS) - 0.75, 0, 1.5, h);
  }
}
