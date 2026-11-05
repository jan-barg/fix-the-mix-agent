# Fix the Mix — Technical Design

**Status:** Draft 1 · November 5, 2026 · companion to [SPEC.md](SPEC.md)

SPEC.md says *what* the system does and the rules it must follow. This document says *how* it is built for the November 11 deadline. Where the two disagree, SPEC.md wins on behaviour and this document wins on implementation. Scope cuts agreed on November 5 are marked **Cut**.

## 1. Decisions at a glance

| Area | Decision |
|---|---|
| Starting point | Project 1 starter `gemini-web-tool-calling` (FastAPI, one `index.html`, `/chat` returning `tool_calls`) |
| Language / packaging | Python 3.12, `uv`, FastAPI + uvicorn |
| LLM | LiteLLM, as in the starter, called with `litellm.acompletion` so calls can run concurrently. Each role's model is a config string, so any role can switch between Claude and Gemini without code changes. Anthropic API key from Secret Manager in production and `.env` locally; Gemini on Vertex uses Google credentials |
| Models | `anthropic/claude-opus-5-5` for every role by default, overridable per role with an env var; effort set per role (§7.1) |
| Audio decoding | FFmpeg subprocess → float32 NumPy array, first audio stream only (`-map 0:a:0`; ignores embedded cover art) |
| Signal processing | NumPy, SciPy (filters, STFT), librosa (beat tracking, onset strength) |
| Loudness and peaks | FFmpeg `ebur128` filter with `metadata=1` + `ametadata=print`, run on the peak-normalised signal piped in as float. Verified on the demo corpus: 100 ms M/S/I/LRA readings plus per-channel sample and true peaks, ~1 s for a 5-minute track |
| Charts | matplotlib (Agg backend) → PNG, rendered only from measured arrays |
| Frontend | Vanilla HTML/JS/CSS like the starter, no build step. wavesurfer.js v7 with the regions and timeline plugins for the section editor |
| Report | Jinja2 HTML template; the same HTML is rendered to PDF with WeasyPrint |
| Storage | Google Cloud Storage bucket in production; local `.data/` directory in development, behind one storage interface |
| Run state | One JSON document per run in the bucket, cached in memory. **Cut:** no Firestore |
| Execution | Request-bound: each phase's work runs inside the HTTP request that starts it. One Cloud Run instance, 60-minute request timeout |
| Uploads | One file per request, WAV or MP3, at most 30 MiB. Larger files are rejected with a request for an MP3 export |
| Source separation | **Cut.** Kick fallback is "try another candidate, otherwise limit kick claims" |
| Section detection | Heuristic detector on a bar/phrase grid; every label is a suggestion the user confirms |
| Interactive charts | **Cut.** Static PNG figures in the web report and PDF |

## 2. Repository layout

```text
app/
  main.py              FastAPI app: /chat, /clear, static files; preloads LiteLLM after startup
  api.py               companion /api routes (§3.3): runs, uploads, audio, peaks, section maps, demo
  config.py            settings from env (bucket, limits, model names, caps)
  storage.py           Storage interface: LocalStorage, GCSStorage
  runs.py              Run document model, load/save, state transitions, decoded-audio cache
  trace.py             Trace events, mapping to /chat tool_calls
  audio/
    decode.py          probe + decode with FFmpeg, peak normalisation
    loudness.py        ebur128 runner and parser
    grid.py            tempo, beats, downbeats, bars, phrases
    sections.py        section proposal, bar spans, cross-track matching
    peaks.py           three-band waveform peaks for the section editor
    bands.py           band definitions, filters, envelopes, fractional-octave spectra
    tonal.py           tonal balance measurements
    stereo.py          stereo and mono measurements
    lowend.py          low-end distribution, event-aligned envelopes, recovery
    kick.py            kick candidates and kick analysis
    figures.py         every chart; shared track colours and scales
    features.py        whole-track base analysis run at upload, cached by file hash
  evidence.py          Evidence records, cache keys, bundle assembly for researchers
  tools.py             the measurement tools of §5.4: run the audio code, build Evidence and figures
  agents/
    llm.py             LiteLLM wrapper: per-role model routing, retries, JSON validation, usage and cost logging
    lead.py            Lead loop, tool definitions, phase gating, round cap
    researchers.py     specialist calls with structured output
    writer.py          report drafting with structured output
    humanizer.py       deferred stage (skipped unless a skill file is configured)
    schemas.py         Pydantic models for researcher results and the report
    prompts/           lead.md, researcher_base.md, kick.md, low_end.md, tonal_loudness.md, stereo.md, writer.md
  report/
    validate.py        numbers, figure links, internal-ID leaks, required sections
    render.py          Jinja2 → HTML, WeasyPrint → PDF
    templates/report.html
static/
  index.html  styles.css
  app.js               views, uploads with progress, demo, confirm (run ID in the URL hash)
  editor.js            canvas section editor: three-band waveform, grid, boundaries, playback
  report.js  progress-messages.js   (day 4–5)
demo/
  corpus.json          demo track list: roles, comments, origin (audio itself is not in git)
  audio/               local copies of demo audio (git-ignored; uploaded to the bucket)
scripts/
  smoke_llm.py         LiteLLM checks of §7.1
  inspect_audio.py     prepare and meter audio files from the command line
  demo_measurements.py run every measurement tool on the demo corpus; writes evidence and charts to .data/
  upload_demo.py       copy demo audio to the bucket
  baselines.py         README no-tool baselines (same model, no tools, no system prompt)
tests/                 synthetic-signal tests for the measurement code (§12)
Dockerfile  pyproject.toml  uv.lock  README.md  submission.json  SPEC.md  DESIGN.md
```

## 3. External contract and API

### 3.1 `/chat` (kept from the starter)

```text
POST /chat  {"message": str, "session_id": str | null}
  → {"response": str, "session_id": str, "tool_calls": [ToolCall]}
POST /clear?session_id=...
```

`session_id` is the run ID. The request and response shapes stay exactly as in the starter. Each `tool_calls` entry keeps `name`, `args` and `result` and gains extra fields:

```json
{
  "name": "run_researcher",
  "agent": "lead",
  "args": {"specialist": "low_end", "question": "…", "sections": ["Drop 1 ↔ Ref A Drop 1"]},
  "result": "6 observations, 2 experiments, 1 follow-up request (summary)",
  "kind": "llm_tool_call",
  "task_id": "t_07", "parent_task_id": "t_01", "round": 1,
  "started_at": "2026-11-09T14:02:11.402Z", "ended_at": "2026-11-09T14:02:39.871Z",
  "status": "ok"
}
```

- `agent` is `lead`, `kick_researcher`, `low_end_researcher`, `tonal_loudness_researcher`, `stereo_researcher`, `writer`, `humanizer` or `system`.
- `kind` is `llm_tool_call` (a model asked for it), `helper` (code ran a measurement for a tool call), or `model_call` (a researcher, Writer or Humanizer invocation).
- `result` is a short summary (at most ~500 characters). Full outputs live in the run folder and the trace panel links to them.
- Timestamps let the trace panel draw researchers as overlapping bars, which demonstrates parallelism.

### 3.2 How each phase uses `/chat`

| Phase | Frontend sends | Server does | `/chat` response |
|---|---|---|---|
| Brief | After uploads finish: the global comment, or a default request if it is empty | Lead reads the run manifest (tracks, roles, comments, metadata) and either calls `record_brief` or asks one question | Clarifying question, or "Brief recorded — review your sections" |
| Clarify | The user's answer | Lead continues the same conversation | As above |
| Research | "Sections confirmed — run the analysis." (sent when the user confirms all maps) | Lead plans, fans out researchers, follows up, calls `finish_research`; then Writer → Humanizer (if configured) → validation → HTML + PDF | Short summary with the top priorities and a link to the report; the full trace in `tool_calls` |
| Follow-up | Any question | Lead answers using existing evidence, running new measurements or researchers when the question needs them (one extra round per question) | The answer, plus its trace |

The Lead keeps one append-only conversation per run (§7.1).

### 3.3 Companion endpoints

| Method and path | Purpose |
|---|---|
| `POST /api/runs` | Create a run → `{run_id}` |
| `POST /api/runs/{run}/tracks` | Multipart upload of one file with `role` (`user` or `reference`), `slot` (1–3 for references) and `comment`. Validates, decodes, normalises and runs the base analysis (§5.2) synchronously. Returns the track record |
| `DELETE /api/runs/{run}/tracks/{track}` | Remove or replace a track |
| `PUT /api/runs/{run}/comments` | Global comment and per-track comments |
| `GET /api/runs/{run}` | Run document for the UI: state, tracks, maps, artifacts |
| `GET /api/runs/{run}/progress` | Current stage, a fun message, recent trace events. Polled about every 1.5 s while a `/chat` request is running |
| `GET /api/runs/{run}/tracks/{track}/audio` | Original audio for playback, with single-range `Range` support (206) so the browser can seek |
| `GET /api/runs/{run}/tracks/{track}/peaks` | Precomputed waveform peaks (§5.2) so the browser does not decode the whole file |
| `GET` / `PUT /api/runs/{run}/tracks/{track}/sections` | Read the current section map, or save a user-edited map (creates a new version) |
| `POST /api/runs/{run}/sections/confirm` | Mark all current maps as confirmed |
| `GET /api/runs/{run}/report`, `/report.pdf`, `/figures/{name}.png` | Report artifacts |
| `POST /api/demo` | Create a run from the demo corpus without uploading |
| `GET /api/health` | Liveness check (`/healthz` is reserved on `run.app`) |

Run IDs are random UUIDs and work as capability URLs. There is no endpoint that lists runs.

### 3.4 Upload limits

- Formats: WAV and MP3, checked by extension and confirmed with `ffprobe`.
- Size: at most 30 MiB per file, checked in the browser before upload and again on the server. Cloud Run rejects HTTP/1 request bodies over 32 MiB, so this is a hard ceiling. A 4-minute 16-bit stereo WAV is about 42 MB, so most full-length WAVs will be over the limit. Error text: "That file is larger than 30 MB. Please export an MP3 (320 kbps works well) and upload that instead."
- Duration: 20 seconds to 10 minutes.
- Both limits are shown on the upload screen.

## 4. Run state

### 4.1 Run document

Stored at `runs/{run_id}/run.json`. It holds everything except large arrays and figures.

```text
Run
  id, created_at, state (SPEC §9.3 states plus Interrupted), state_history[]
  comments: global
  tracks[]: id, role, slot, display_name, comment, source{filename, sha256, bytes, format,
            sample_rate, channels, duration_s, decode_warnings[]},
            prep{original_sample_peak_dbfs, applied_gain_db, silent: bool, resampled_from?}
  section_maps{track_id: [SectionMap versions]}
      SectionMap: version, origin (proposed | user), confirmed, bpm, downbeat_s,
                  grid_source (detected | user), sections[]{id, label, start_s, end_s,
                  start_bar, end_bar, label_confidence (proposed only)}
  brief: goals[], preserve[], reference_roles[]{track_id, aspects[], exclusions[]},
         comparison_mode (separate | aspects | average), combined_target?{contributors, aspects,
         method, weights}, focus_sections[], production_stage, assumptions[]
  lead_messages[]       append-only Claude conversation (content blocks as JSON)
  research: rounds_used, researcher_results{task_id: ResearcherResult}, accepted{…}
  evidence_index{evidence_id: summary}     full records in evidence/
  report: status, html_path, pdf_path, humanizer: skipped | applied | reverted, validation[]
  progress: stage, detail, updated_at
```

### 4.2 Bucket layout

```text
runs/{run_id}/run.json
runs/{run_id}/trace.jsonl
runs/{run_id}/tracks/{track_id}/original.{mp3|wav}
runs/{run_id}/evidence/{evidence_id}.json      record (§6)
runs/{run_id}/evidence/{evidence_id}.npz       arrays, when present
runs/{run_id}/figures/{figure_id}.png
runs/{run_id}/report/report.json | report.html | report.pdf
cache/features/{sha256}-{config_version}.npz   whole-track base analysis, shared across runs
demo/audio/*.mp3, demo/corpus.json
```

### 4.3 Single-instance assumptions

- Cloud Run runs with `max-instances=1`, so the in-memory run cache and progress state are authoritative. Every state change is also written to `run.json`.
- When a run is loaded with a working state (`Preparing`, `Researching`, `Writing`, …) but no active task in this process, it becomes `Interrupted`. The UI offers "Resume", which restarts the interrupted phase. Measurements already cached are reused.
- Decoded audio stays in memory rather than in `/tmp`, because Cloud Run's filesystem is memory-backed anyway. Signals are re-decoded on demand (about 1 s per track) and dropped from an LRU cache.

## 5. Audio pipeline

### 5.1 Decode and normalise

1. `ffprobe` reads the codec, sample rate, channels and duration, and confirms there is an audio stream.
2. FFmpeg decodes the first audio stream to interleaved float32 at the source channel count (mono stays mono). Every track is resampled to 44.1 kHz with FFmpeg's resampler, and the original rate is recorded.
3. The sample peak `p` is the maximum absolute value across all channels after decoding and resampling. Decoded MP3s can exceed full scale: the Weightless demo track decodes to a 1.168 sample peak (+1.35 dBFS). Float decoding keeps these overs, and normalisation brings them back to 0 dBFS.
4. If `p` is below 1e-6 (−120 dBFS), the track is marked silent and normalisation is unavailable. Otherwise the gain is `1/p`, recorded in dB, and applied to every channel.
5. The original file is stored unchanged. The normalised array is never written to disk.

### 5.2 Base analysis at upload

Runs once per file hash and configuration version, and is stored in `cache/features/`. The demo tracks are therefore analysed only once.

| Feature | Method | Default settings |
|---|---|---|
| Loudness series | `ebur128` on the normalised signal piped to FFmpeg | M, S, I, LRA every 100 ms; per-channel sample and true peaks |
| Grid | `librosa.beat.beat_track` gives a first tempo only: its frame-quantised estimate is off by about 2 BPM (134 reads as 136). A kick-weighted onset envelope is then folded onto candidate periods within ±4 BPM (vector strength, 0.01 then 0.0005 BPM steps); a whole-number BPM wins when it lines the onsets up at least 95% as well. The grid is always one straight line of beats. Its position is the peak of the beat-folded onset envelope, with 64-beat windows weighted by how coherently their onsets align (a weighted circular mean alone sat about 25 ms late on Scars). Downbeat phase: harmonic change (beat-synchronous chroma) plus broadband onset strength, best of four. `constant_tempo` records whether the onsets support the straight grid (phase residual under 20 ms and over 80% agreement). **File start:** a beat up to 50 ms before 0 s is kept as the first beat instead of wrapping to the next period, and when the downbeat is a guess and the file starts with sound on a beat, that beat is bar 1 (a file exported from bar 1 starts there). See §5.3 for the check | 4/4 assumed; bars and 8-bar phrases derived |
| Fractional-octave spectra | STFT power aggregated into 1/6-octave bands with overlap weighting (bins narrower than a band are split across bands), Parseval-scaled so band powers sum to the mean square (a full-scale sine reads −3 dB); L, R, mid, side and the cross-spectrum Re(L·R*) for per-band correlation | n_fft 16384, hop 4096, Hann; 20 Hz–20 kHz |
| Band envelopes | Zero-phase 4th-order Butterworth band-pass, then the analytic (Hilbert) envelope averaged over one period of the band's lowest frequency (at least 10 ms). A plain 10 ms RMS window ripples at a 40 Hz bass's own frequency, which looks like level movement. All envelopes share one 5 ms time axis | Bands: sub 30–60, low 60–120, upper bass 120–300, low-mid 300–600 Hz (configurable) |
| Onsets | Kick-weighted onset strength: 32 mel bands up to 250 Hz | 22.05 kHz, hop 128 (5.8 ms) |
| Waveform peaks | Per 10 ms bucket: sample min/max (int8) and RMS of three bands (uint8, scaled to the track's 99.5th percentile): low below 200 Hz, mid, high above 2 kHz. Base64 JSON, cached in `cache/peaks/` (about 100–200 KB per track) | 100 buckets/s |
| Section proposal | §5.3 | |

### 5.3 Section proposal

- **Bar features:** short-term loudness, low-band share, kick presence, spectral centroid and side/mid ratio, z-scored per track. Kick presence is beat-synchronous low-end modulation: the body level (sub + low bands) in the first 60 ms after each beat minus its level 60–90% of the way through the beat, clipped to −10…20 dB. A kick makes it clearly positive; a sustained bass with no kick keeps it near 0.
- **Novelty and boundaries:** distance between the 4-bar windows before and after each bar. Boundaries are allowed only on 8-bar phrase starts. The phrase offset is the alignment whose bar starts carry the most novelty, because tracks often open with a pickup or an odd-length intro. Candidates whose z-scored novelty is above 0 become boundaries.
- **Labels:** per section, the power-mean momentary loudness, the loudness rise from first to second half, and the mean kick presence.
  - "drop" = within 3 LU of the loudest section with kick presence of at least 3 dB.
  - "breakdown" = kick presence under 1.5 dB, or more than 6 LU below the loudest section.
  - "build" = the section directly before a drop, when it rises by more than 1 LU or has no full kick.
  - "intro" / "outro" = the first and last sections unless they are drops.
  - Everything else is "unknown". Adjacent sections with the same label (except builds) are merged.
- Each proposed label has a confidence word (`likely` or `guess`). The editor shows guesses with a "?" and drops the word once the user edits or accepts the map. Chorus/verse/bridge are only available when the user picks them.
- **Bar numbers:** bar 1 starts at the first downbeat (which may sit up to 50 ms before 0 s, §5.2) and bar 0 is a pickup. A time within 2% of a bar line counts as on it, so rounding to milliseconds never moves a boundary into the next bar. Server (`sections.bar_span`) and editor use the same rule.
- **Files that start on bar 1 (checked November 5):** each demo track was cut at a downbeat, exactly and 3 or 10 ms either side, at bar 1 and at bar 33. Before the fix, a cut exactly on bar 1 lost bar 1 on three of the four tracks: the fitted phase came out a millisecond or two before 0 s and wrapped a whole beat later, so every bar number shifted by one. After the fix, 35 of 40 cuts put bar 1 at the cut. The rest are the Scars cuts from bar 33, where the tempo comes out as 134.07 instead of 134 and the grid drifts (a tempo-snapping issue, not the file start).
- **Day-3 check on the demo tracks:**
  - Scars (Extended): intro, an unlabelled 16 bars, a short drop, build, drop 1:49–3:00, breakdown, build, drop 3:43–4:41, outro.
  - Endless Light: intro, drop 0:45–1:56, breakdown, build, drop 3:22–4:25.
  - FE!N: intro, build, drop, build, drop.
  - Weightless: intro, drop 0:35–1:34, build, drop 2:04–5:01, breakdown, outro.
  - The main drops and breakdowns land on the right phrases. The weak spots are the short "drop" in the Scars intro and the long merged drop in Weightless, which the editor fixes in a few clicks. The producer's own timestamps (§14) are still needed to check them properly.

### 5.4 Measurement tools

Every tool returns an Evidence record (§6) and its figures. Settings, bands and normalisation mode are always recorded. All primary measurements use the whole-track peak-normalised signal. Sections inherit their track's gain, and nothing is renormalised per section, channel or fold-down.

| Tool | Scope | Outputs |
|---|---|---|
| `measure_loudness` | Tracks, optional sections | Integrated LUFS (whole track; for a section, computed from its 400 ms blocks with standard gating when at least 3 s long), short-term series, mean and max short-term LUFS per section, LRA, sample peak dBFS, true peak dBTP, PLR (true peak − integrated) as the dynamics descriptor, build→drop contrast in LU. Figures: loudness timeline with section shading; peak/loudness comparison |
| `measure_tonal_balance` | Whole tracks or matched section pairs; `mode=peak_normalised` (primary) or `level_matched` (secondary, labelled) | Power-averaged 1/6-octave long-term spectrum in dB; summary energies for sub, bass, low-mid, mid, high-mid and air; difference curves against each reference or the combined target. Figures: tonal curves; difference curve |
| `measure_stereo` | Whole tracks or section pairs | Side-to-mid ratio by 1/3-octave band in dB; L/R correlation over 400 ms windows; mono change by band = 10·log10(\|M\|² / ((\|L\|²+\|R\|²)/2)) with M = (L+R)/2, which is 0 dB for identical channels, about −3 dB for uncorrelated and −∞ for opposite polarity. Flags mono input and silent bands. Figures: side/mid by frequency; correlation timeline; mono change curve |
| `find_kick_candidates` | One track, optional preferred ranges | Ranked on-beat kicks (the grid's kick events, so bass notes are never candidates) with timestamps and reasons, at least 4 s apart. Fullness = 30–120 Hz body peak within 120 ms, relative to the 95th percentile of the better-isolated half of the kicks (a kick over a bass note reads up to ~4 dB fuller only because the bass adds to it). Isolation = body peak above the level 90–40 ms before the onset (any later and the envelope's smoothing already shows the kick). Score = fullness, penalised 6 points per dB beyond −3 dB, + 0.5 × isolation (capped at 20) + 0.3 × decay drop 200–300 ms after the onset (capped at 30) − 0.3 × 8–16 kHz excess − 10 when the next onset of any kind is under 250 ms away. Flags: weaker or filtered variation, bass overlap (isolation under 12 dB), hats or cymbals, next onset too close. Figure: strip of the top candidates |
| `analyse_kick` | One candidate per track | 400 ms crop. Attack/body ratio (2–8 kHz energy in the first 15 ms vs. 40–120 Hz energy over 15–150 ms). Low-band decay time to −20 dB, unavailable if interrupted. Body peak frequency and centroid. Pitch trajectory only when the spectral peak is prominent enough. Level in the parent track's normalisation, plus an optional labelled gain-matched view. Figures: envelope overlay; kick spectrograms on one colour scale |
| `measure_low_end` | Matched section pairs, bands | 20–300 Hz distribution (1/6 octave); kick-aligned median envelopes with interquartile range per band; recovery estimates (§5.5); upper-bass harmonic band moving with the low bands. Figures: event-aligned envelopes in ms and beat fractions; low-end spectrogram of a 4-bar passage with kick markers and a full-band companion |
| `render_spectrogram` | Track, time range, frequency range, preset | Presets: `overview` (n_fft 4096 / hop 1024), `low_detail` (8192 / 256, up to 400 Hz), `transient` (1024 / 128). One fixed dB range for every track in a request |

**Matching sections:** sections are paired by label and order (Drop 1 ↔ Drop 1). The Lead can override the pairs. When tempos differ, durations are also reported in beats. Nothing is time-stretched.

**Combined targets:** the mean of the contributors' dB curves (tonal, stereo by band) or of their per-section values (loudness), with equal weights unless the brief sets them. The method is recorded in the evidence.

### 5.5 Ducking recovery (validated on day 2)

**Kick events:** the strongest kick-weighted onset within ±40 ms of each grid beat, kept when it is at least 30% of the track's typical (90th percentile) on-beat strength.

Single events are too noisy to measure: a bass line re-articulates inside most beats. A first version required each event's own late-beat level to be steady, and it rejected every event on all four demo tracks. The estimate therefore comes from the **median across kicks**, using each band's envelope:

- **Event curves:** one beat of envelope per kick, from 20 ms before it, each taken relative to its own baseline.
- **Baseline:** median level from 50% to 80% of the beat. It stops short of the next kick, because zero-phase filtering and smoothing spread the next kick slightly backwards.
- **Dip:** minimum of the median curve from the kick's own peak in that band (at least 30 ms after the onset) to 50% of the beat. In the sub band a pitch-swept kick's body arrives late (near 100 ms), so a dip searched earlier would only be the kick's attack. The kick's peak is the first prominent peak within 150 ms that the curve rises into from the onset; otherwise the onset.
- **Depth:** baseline − dip, in dB.
- **Recovery time:** from the dip until the median curve is back within 10% of the depth from the baseline. Reported in ms, as a fraction of the beat, and as the time after the kick. The spread is a bootstrap over kicks (200 resamples, IQR).
- **Shape:** quick return, gradual swell or delayed return, from the median curve.
- **Consistency:** the share of kicks at least half the depth down at the median dip time.
- **Unavailable when:**
  - fewer than 8 kicks;
  - the median curve moves more than 3 dB (10th–90th percentile) inside the baseline window, meaning a bass note or phrase change sits there;
  - the depth is under 1.5 dB;
  - the dip shows in under 60% of kicks;
  - there is no recovery by 80% of the beat.
- **Timing resolution:** 5 ms steps, further limited by envelope smoothing (sub 33 ms, low 17 ms, upper bass and low-mid 10 ms). The note goes into every evidence record.

**Validation** (`tests/test_measurements.py`): a 128 BPM loop has kicks at 70→45 Hz and a 220 Hz bass. The bass is held at −12 dB for 50 ms, then ramped back to 0 dB by 200 ms, so it should be back within 10% at 185 ms. The estimator measures a 11.95 dB depth and recovery at 175 ms, which is within one smoothing window. An un-ducked bass reports no dip. A bass that drops 10 dB at 65% of the beat is reported unavailable.

**Demo drops** (loudest 32 bars):

| Track | Upper-bass dip | Recovered after the kick | Kicks showing the dip | Low band (60–120 Hz) |
|---|---|---|---|---|
| Scars | 19.6 dB | ≈170 ms | 90% | 10.4 dB dip, recovered by ≈185 ms |
| Endless Light | 7.3 dB | ≈175 ms | 78% | Unavailable: off-beat bass notes |

The kick itself dominates the sub and low bands. The researcher prompt and the report therefore treat the upper-bass and harmonic bands as the primary evidence of bass ducking, and present sub- and low-band behaviour as a combined kick-and-bass envelope.

## 6. Evidence model

```text
Evidence
  id: "ev_" + 8 hex          (never shown in the report)
  tool, run_id, task_id, created_at
  source: track_ids[], section_ids[], ranges[{track_id, start_s, end_s}],
          signal: peak_normalised | level_matched | original_level
  method: params{…}, bands[], units{…}, normalisation{gain_db per track}, config_version
  results: scalars{name: {value, unit}}, tables[], arrays_ref?
  figures[]: {id, title, caption, path}
  reliability: status ok | qualified | unavailable, warnings[]
  repro: cache_key, code_version (git SHA), file sha256s
```

- **Cache key:** SHA-256 of the tool name, input file hashes, section boundaries used, parameters and `config_version`. Editing comments does not invalidate it; moving a boundary does.
- **Captions** are generated by code from `source`, for example "Spectral comparison of your track, Drop 1, vs Reference A, Drop 1." Prose can reference them but never invents them.
- **Researcher bundles:** each piece of evidence is a compact text summary (scalars with units, method, reliability) plus its PNG figures as image blocks. Raw arrays are never sent.

## 7. Agents

### 7.1 Models and the LLM layer

All model calls go through `app/agents/llm.py`, a thin wrapper over `litellm.acompletion`. Agent code never names a provider; each role reads its model string from config.

| Role | Default model (env override) | Effort | Output |
|---|---|---|---|
| Lead | `anthropic/claude-opus-5-5` (`LEAD_MODEL`) | `high` | Tool calls and chat text |
| Researchers (4 specialists) | `anthropic/claude-opus-5-5` (`RESEARCHER_MODEL`) | `medium` | `ResearcherResult` JSON, validated with Pydantic |
| Writer | `anthropic/claude-opus-5-5` (`WRITER_MODEL`) | `high` | `ReportDraft` JSON, validated with Pydantic |
| Humanizer (deferred) | `anthropic/claude-opus-5-5` (`HUMANIZER_MODEL`) | `medium` | Same `ReportDraft` schema with only prose fields changed |
| README baselines | same as the Lead (`BASELINE_MODEL`) | default | Plain text |

Switching a role is one env var, for example `RESEARCHER_MODEL=vertex_ai/gemini-3.5-flash-lite`.

What the wrapper does:

- **Message format:** LiteLLM speaks the OpenAI format. Tools are `{"type": "function", ...}` definitions, images are `image_url` blocks with base64 `data:image/png` URLs, and tool results are `role: "tool"` messages. LiteLLM translates these for Claude and Gemini.
- **Effort:** passed as `reasoning_effort`. On Opus 5.5 thinking is always on and effort is the only control.
- **Structured output:** requests JSON for the role's Pydantic schema with `response_format` where the model supports it, and always validates the reply with Pydantic. A reply that fails validation gets one retry with the error message, then the call is recorded as failed. Validation is what guarantees the shape, so this works on any model.
- **Parallel tool calls:** all `run_researcher` calls in one Lead response run concurrently with `asyncio.gather`. Each gets its own `tool` message; a failed call returns an error result rather than being dropped.
- **Append-only history:** each assistant message is appended exactly as returned (`model_dump()`, as in the starter, which keeps Claude's `thinking_blocks`), and earlier turns are never edited.
- **Constant tool set:** the Lead has the same tools in every phase, and the harness rejects out-of-phase calls with an error result. Changing tool definitions mid-conversation would break prompt caching.
- **Prompt caching:** `cache_control` on the Lead's system prompt, which LiteLLM passes through to Anthropic. The system prompt and tool list are byte-stable (no timestamps or run IDs in them).
- **Failures:** a `finish_reason` other than a normal stop or tool call (for example `length` or `content_filter`) is treated as a failed call with its reason recorded. LiteLLM's retries handle 429/5xx, and service retries do not count as research rounds.
- **Usage:** tokens and `litellm.completion_cost` are logged per call and per run.

**Day-1 smoke test (must pass before the agents are built on it).** Through LiteLLM with `anthropic/claude-opus-5-5`:

1. Two tool calls in one response.
2. A second turn after the tool results, with the first turn's thinking blocks in the history. Opus 5.5 always thinks, and newer Anthropic accounts reject a history whose thinking blocks were altered or dropped.
3. A PNG image input.
4. `response_format` with a JSON schema. LiteLLM has in the past implemented this for Claude as a forced tool call, which Opus 5.5 rejects. If that happens, the wrapper asks for JSON in the prompt instead and relies on Pydantic validation.
5. `reasoning_effort` is accepted and changes the thinking effort.

If check 2 fails, the fallback is to send Claude calls for the Lead through the Anthropic SDK behind the same wrapper interface. Only `llm.py` would change.

**Result (November 5, LiteLLM 1.108.0, `scripts/smoke_llm.py`):** all five checks pass on `anthropic/claude-opus-5-5` and on `vertex_ai/gemini-3.5-flash-lite`. The Opus 5.5 run kept a thinking block in the first assistant message, and the next two turns on that history succeeded. LiteLLM maps `reasoning_effort` to adaptive thinking with summarised display plus `output_config.effort`, and maps `response_format` to native structured output, not a forced tool. One quirk: LiteLLM 1.108 imports `mcp` on any call with tools but does not declare it, so `mcp` is a direct dependency. Rerun the script after upgrading LiteLLM or switching a role's model.

**Cost:** a full run is very roughly 0.3–0.5 M input tokens and 40–70 k output tokens, about $2–3 at Opus 5.5 list prices. The logged usage replaces this estimate after the first runs. Pointing `RESEARCHER_MODEL` at Sonnet 5.5 would roughly halve the cost, and at Gemini would move that part onto your Google Cloud credits.

### 7.2 Lead tools

| Tool | Phase | Effect |
|---|---|---|
| `record_brief` | Brief | Stores the brief (§4.1). Asking a clarifying question needs no tool: if the Lead ends its turn without `record_brief`, the run waits for the user's answer (`NeedsClarification`) |
| `get_run_overview` | Any | Tracks, roles, confirmed maps, matched section pairs, base metrics, and the evidence already available |
| `measure_loudness`, `measure_tonal_balance`, `measure_stereo`, `find_kick_candidates`, `analyse_kick`, `measure_low_end`, `render_spectrogram` | Research, follow-up | Runs the helper (cached) and returns the evidence ID, summary and figure titles |
| `run_researcher` | Research, follow-up | `{specialist, question, scope{section_pairs, tracks}, user_intent, evidence_ids[]}`. The harness adds the specialist's default bundle (below) to any evidence the Lead supplies, runs the helpers, then invokes the researcher in a fresh context |
| `finish_research` | Research | `{accepted[]{task_id, finding_refs[]}, rejected[]{finding_ref, reason}, priorities[≤3], preserve[], limitations[]}`. Ends research and starts the Writer |

**Default bundles** (built from cached helpers, so a full first fan-out needs one Lead turn):

| Specialist | Default evidence |
|---|---|
| `kick` | Top three candidates per track, `analyse_kick` on the best candidate per track, candidate strip and kick spectrograms |
| `low_end` | `measure_low_end` on the focus section pair (default: first drop of each track), low-end and full-band spectrograms of a 4-bar passage |
| `tonal_loudness` | `measure_loudness` (whole track and per section), `measure_tonal_balance` (whole track and matched pairs) |
| `stereo` | `measure_stereo` (whole track and matched pairs) |

**Caps:**
- A round is a Lead response containing at least one `run_researcher` call. At most 3 rounds (configurable); after that `run_researcher` returns "round cap reached — call finish_research".
- At most 12 Lead turns in the research phase.
- One extra round per follow-up question.

### 7.3 Researcher contract

- **Inputs:** a system prompt (shared rules plus the specialist's section) and a user message containing the task, the relevant brief excerpt, reference roles, evidence summaries and figures.
- **Output (`ResearcherResult`, matching SPEC §7.3):** question; scope; observations[]{text, evidence_ids[], values[]{name, value, unit, evidence_id}}; comparisons[]{target, text, evidence_ids[]}; interpretations[]{text, alternatives[]}; experiments[]{action, how_to_verify, supports_goal}; preserve[]; limitations[]; confidence (plain language); follow_up_requests[]{need, why, suggested_tool, params}.
- **Check before the Lead sees it:** every `values[]` entry must match a scalar in the cited evidence, after rounding. Mismatches are attached as warnings for the Lead to reject or re-request.

### 7.4 Writer

- **Inputs:** the brief, the Lead's accepted findings with their researcher results, Lead priorities and limitations, and the figure catalogue (figure ID, title, generated caption, what it compares).
- **Output (`ReportDraft`):**
  - `priorities[≤3]`, each with title, why it matters, observation, experiment, verification and figure IDs.
  - `preserve[]`.
  - `sections`: general, kick_low_end, stereo, section_by_section, method_notes. Each section is a list of blocks: paragraphs with text and claims[]{value, unit, evidence_id}, and figure blocks.
  - `limitations[]`.
- **Rendering:** figures and captions are rendered by code; the Writer chooses which figures to show and where.

### 7.5 Validation and repair (before rendering)

1. Every figure ID exists and its file was rendered.
2. Every number with a unit in the prose (regex covering dB, LUFS, LU, dBTP, dBFS, Hz, kHz, ms, s, BPM, %, beats, bars) matches a claim whose value matches the cited evidence after rounding.
3. No internal identifiers (`ev_`, `trk_`, `sec_`, `t_`) appear in the prose.
4. All required sections are present, and there are no more than three priorities.

Violations go back to the Writer once, with the list of problems. Anything still failing is dropped from the report, and the run ends `Partial` with the reason shown.

### 7.6 Humanizer (deferred)

- **Skipped:** when `HUMANIZER_SKILL_PATH` is unset, the stage is skipped and the run records `humanizer: skipped`.
- **When configured:** the skill text becomes the system prompt, and only prose fields are sent. Protected tokens (numbers with units, track and section names, figure references, hedging words that mark hypotheses) are compared before and after the edit. Any change reverts that block to the Writer's version.

## 8. Trace

- Every helper run, tool call and model call appends a JSON line to `runs/{run}/trace.jsonl` with the fields in §3.1.
- Each `/chat` response includes the events created during that request.
- The UI trace panel shows a timeline in which parallel researchers appear as overlapping bars, with expandable arguments and results.
- Plans, evidence and decisions are shown. Thinking content is never shown.

## 9. Frontend

The frontend is one page with five views, switched by run state:

1. **Upload:** explanation of the product, the global comment box above the cards, your-track card plus three reference cards (file, comment), limits shown, client-side size and format checks. "Prepare tracks" uploads in parallel, then sends the brief to `/chat`.
2. **Clarify:** a chat strip for the Lead's question, if any.
3. **Sections:** one editor per track (`editor.js`), drawn on a canvas from the precomputed peaks. No waveform library: the three-band peaks need custom drawing anyway, and it keeps the page dependency-free.
   - Three-band waveform (low blue, mid orange, high white), an overview strip with the visible window, and a section strip coloured by label with bar counts.
   - Bar/beat grid with bar numbers on a ruler and 8-bar phrases marked. Boundaries snap to bars, beats or nothing; Alt ignores the grid while dragging.
   - Split at the cursor (S), merge with the next section (M), relabel from a dropdown (custom text allowed).
   - Play, play the selected section, or play ±4 s around the nearest boundary. Starting one editor pauses the others.
   - Zoom with the buttons, +/− or ctrl/pinch-wheel; drag the waveform to scroll.
   - BPM field and downbeat nudge (±1 beat, ±10 ms). Editing either switches to a straight user grid.
   - "Accept" per track saves a new map version. "Confirm all" saves any unaccepted maps as they are, then confirms. Leaving the page with unsaved edits asks first.
   - The run ID sits in the URL hash, so a reload returns to the same run and view.
4. **Progress:** a stage line with a rotating fun message (`progress-messages.js`, e.g. "Feeling the bass thump in the chest", "Mono-Stereo-Mono-Stereo… ARGHHHHHHH") and a live trace panel.
5. **Report:** the rendered report, PDF download, follow-up chat, and the trace panel.

Track colours are fixed: your track plus References 1–3, with a distinct style for a combined target. The same values are used in the CSS and in `figures.py`.

## 10. Report and PDF

- **HTML:** a Jinja2 template renders `ReportDraft` plus the figure catalogue into one HTML document. The web view serves it with figure URLs.
- **PDF:** WeasyPrint renders the same template with print CSS and local figure paths.
- **Captions:** each caption appears under its figure and links to it. Timestamps appear in an expandable "source details" line.
- **PDF failure:** if PDF rendering fails, the web report and chat answer are still delivered, and the failure is shown (SPEC §10).

## 11. Deployment

- **Container:** `python:3.12-slim` plus `apt-get install ffmpeg libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core`, then `uv sync --frozen`. It runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- **Service:**

  ```bash
  gcloud run deploy fix-the-mix --source . --project agentic-ai-msds --region us-east1 \
    --service-account fix-the-mix-run@agentic-ai-msds.iam.gserviceaccount.com \
    --cpu 4 --memory 8Gi --cpu-boost --timeout 3600 --max-instances 1 --concurrency 8 \
    --set-secrets ANTHROPIC_API_KEY=fix-the-mix-anthropic-api-key:latest \
    --set-env-vars STORAGE=gcs,BUCKET=agentic-ai-msds-fix-the-mix \
    --allow-unauthenticated --quiet
  ```

- **GCP resources (created November 5):**
  - Project `agentic-ai-msds`, region `us-east1`. The project also hosts an unrelated `scavagent` service; nothing here touches its resources.
  - Bucket `gs://agentic-ai-msds-fix-the-mix`: uniform access, public access prevention enforced.
  - Service account `fix-the-mix-run@agentic-ai-msds.iam.gserviceaccount.com` with `roles/storage.objectAdmin` on that bucket only and `roles/secretmanager.secretAccessor` on that secret only. If any role is switched to a Gemini model, it also needs `roles/aiplatform.user`.
  - Secret `fix-the-mix-anthropic-api-key`. The project's existing `anthropic-api-key` secret belongs to the other app and is not used.
  - `.gcloudignore` and `.dockerignore` keep `.env`, local data and audio out of the upload and the image.
- **Billing:** request-based billing is fine because all work happens inside requests.
- **Cold starts:** on November 5 a request to the scaled-to-zero day-1 build failed with a 500 ("aborted after waiting too long for an available instance"). The new instance took 17 s from start to listening, longer than Cloud Run held the request.
  - Fix: LiteLLM, the slowest import, now loads on first use and is preloaded in a background thread once the server is listening. Importing `app.main` dropped from 2.1–3 s to 0.6 s locally.
  - The demo's feature and peaks caches for the current `config_version` are copied to the bucket, so the demo run decodes but does not re-analyse.
  - Keep `min-instances 0` normally and set it to 1 for the grading window, so graders never hit a cold start.
- **Image contents:** `app/`, `static/` and `demo/corpus.json`. Demo audio is read from the bucket.
- **Deploy early:** a placeholder app is deployed on day 1 so that container, secret and bucket problems show up early.

## 12. Development and validation

- **Local setup:** `uv run uvicorn app.main:app --reload` with `STORAGE=local` and `.env` holding `ANTHROPIC_API_KEY`. FFmpeg 8 and the gcloud CLI are already installed on the development machine; WeasyPrint needs `brew install pango` locally.
- **Synthetic-signal tests** (pytest, no network), covering SPEC §12.4:
  - Peak normalisation: 0 dBFS across channels with one shared gain; a silent input is reported unavailable.
  - Constant gain invariance: a −6 dB copy normalises to identical measurements.
  - Build→drop contrast survives normalisation.
  - Band spectra: band powers add up to the mean square; a full-scale sine reads −3 dB in its band.
  - Section loudness: integrated loudness from the momentary blocks matches FFmpeg's, gates included.
  - Stereo: identical L/R gives 0 dB mono change and correlation 1; a polarity-flipped channel cancels; uncorrelated noise gives about −3 dB.
  - Grid and kick events: a synthetic 128 BPM loop gives 128.00 BPM and kick events within 12 ms (median) of the true kicks.
  - File start: the loop cut 0, 10 and 20 ms into its first kick keeps that kick's beat as bar 1 at 0 s.
  - Ducking: a sine bass with a known sidechain envelope plus kicks recovers within tolerance; an un-ducked bass reports no dip; a changing bass line is marked unavailable (§5.5).
  - Kicks: exposed kicks outrank kicks over a bass note, with clearly higher isolation, and are not called weaker.
- **Agent checks on the demo corpus:**
  - A vague request triggers clarification.
  - A round-2 follow-up actually happens.
  - Researchers overlap in the trace.
  - The round cap holds.
  - Report validation passes with no evidence IDs in the text.
- **Baselines:** `scripts/baselines.py` sends each README sample question to the Lead's model through LiteLLM with no tools and no system prompt, and stores the raw answers.

### Demo corpus

Real use always analyses whatever the user uploads. The files in `demo/audio/` are for development and for the one-click demo (`POST /api/demo`), which gives graders a reproducible run without uploading anything:

| File | Role in development and the demo |
|---|---|
| `scars-extended.mp3` | Your track (Jan Barg, ALPHUH — Scars, Extended) |
| `endless-light-radio-edit.mp3` | Reference |
| `fein-jan-barg-remix.mp3` | Reference |
| `weightless-extended.mp3` | Reference |
| `scars-radio.mp3` | Spare: same song, shorter arrangement; useful for testing section matching |

Roles and comments for the demo live in `demo/corpus.json` and can change without code changes.

## 13. Schedule

| Day | Date | Work | Done when |
|---|---|---|---|
| 1 | Thu Nov 5 | This design; repo scaffold from the starter; LiteLLM smoke test (§7.1); storage layer; decode/normalise; `ebur128` parser; placeholder deployed to Cloud Run with bucket and secret | Smoke test passes; `/chat` answers on Cloud Run; a demo track decodes, normalises and meters locally |
| 2 | Fri Nov 6 | Base analysis, grid, all measurement tools, figures, evidence records, synthetic tests | Every tool produces sensible numbers and charts for all four demo tracks |
| 3 | Sat Nov 7 | Upload view and endpoints, section proposal, section editor | A run goes from upload to confirmed maps in the browser |
| 4 | Sun Nov 8 | Lead loop, tools, clarification, parallel researchers, round cap, trace, progress | A research run produces accepted findings with a follow-up round visible in the trace |
| 5 | Mon Nov 9 | Writer, validation and repair, HTML report, PDF, chart and report visual design pass (§14), follow-up chat, full run on Cloud Run | The deployed app produces a report and PDF from the demo corpus |
| 6 | Tue Nov 10 | README (data loading, three sample queries with real answers, baselines), `submission.json`, polish, Humanizer if supplied | Submission-ready |
| — | Wed Nov 11 | Buffer and submission | |

If the schedule slips, cut in this order: kick pitch trajectory → section-label heuristics (leave all as "unknown") → combined-target averaging beyond tonal curves → follow-up chat after the report. Never cut parallel researchers, follow-up rounds, clarification, the trace, or the Cloud Run deployment.

## 14. Open items

| Item | Needed by |
|---|---|
| Visual design pass for charts and the report. The figures are correct but look like plain matplotlib; they need a designed, polished look (typography, palette, spacing, consistent with the web report and PDF). Content and data stay as they are | Day 5 |
| Comments and reference roles for the demo corpus (`demo/corpus.json`) | Day 4 |
| BPM and drop timestamps of the demo tracks from the producer, to check the grid and section proposals (detected: Scars Extended 134, Endless Light 134, FE!N 136, Weightless 130) | Day 3 |
| Model per role: Opus 5.5 everywhere (default), or Sonnet 5.5 / Gemini for researchers to cut cost | Day 4 |
| Humanizer skill file | Any time; the stage is skipped until then |
