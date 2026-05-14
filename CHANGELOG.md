# Screencast to Figma — Project Changelog

> **Workflow rule**: At the start of every session, read this file first.
> At the end of every session, update it with decisions made and problems solved.

---

## Project Overview

Tool that takes a mobile app screen recording (MP4/MOV), extracts unique screens, classifies them with Gemini AI, and imports them into Figma — organized into labeled sections (onboarding, paywall, home, settings, etc.) with UI component tags and interaction state labels shown under each screenshot.

**Stack**: Flask 5055 · ffmpeg-python · imagehash · Gemini Vision API · Figma Plugin API

**Entry point**: `python app.py` (starts server on port 5055)
**Figma plugin**: `static/figma-plugin/` — load as local plugin in Figma desktop

---

## Architecture

```
video file / URL
      ↓
extractor.py   — ffmpeg 4fps → stability filter → phash dedup → frame JPEGs
      ↓
classifier.py  — Gemini vision (4×4 grid per call) → label + conf + key_text + components + state
      ↓
app.py         — semantic dedup (label+key_text+components+state) → manifest.json
      ↓
Figma plugin   — polls /status → fetches /plugin-manifest → downloads images → createSection()
```

---

## File Map

| File | Purpose |
|------|---------|
| `app.py` | Flask server, job queue, pipeline orchestration, semantic dedup, REST endpoints |
| `processor/extractor.py` | ffmpeg extraction, stability filter, phash dedup |
| `processor/classifier.py` | Gemini grid classification, model fallback, prompt |
| `processor/results_store.py` | Saves images + manifest.json to `output/<job_id>/` |
| `processor/downloader.py` | yt-dlp video download for URL input |
| `static/figma-plugin/code.js` | Figma plugin main thread — creates sections, frames, labels |
| `static/figma-plugin/ui.html` | Figma plugin UI — drop zone, progress steps, server input |
| `static/figma-plugin/manifest.json` | Figma plugin manifest with networkAccess |

---

## Decisions & Lessons Learned

### Extraction (extractor.py)

- **4fps** — gives 0.25s resolution, enough to bracket any iOS transition (250–400ms)
- **Stability filter** (threshold=18): remove a frame if it differs from BOTH neighbors by >18 phash bits — catches mid-transition blurs without removing brief real screens
- **phash dedup** (threshold=20): remove consecutive frames that are perceptually identical (same screen held)
- Threshold 12 was too aggressive and dropped quick loaders (<1s). Settled on 18.
- Do NOT use `vsync="vfr"` with ffmpeg 7.x — it causes silent 0-frame output. Plain `fps=4` filter works.

### Classification (classifier.py)

- **Grid approach**: pack up to 16 frames into a single 4×4 JPEG grid per Gemini call. More cost-efficient than per-frame calls. Numbers in top-left corner of each cell tell Gemini the frame index.
- **Model list** (in priority order): `gemini-2.5-flash` → `gemini-2.5-flash-lite`
- Fallback triggers on: `429`, `RESOURCE_EXHAUSTED`, `503`, `UNAVAILABLE`, `404`, `NOT_FOUND`
- Do NOT add Gemini 2.0 or Claude to the fallback — user rejected both
- `CONFIDENCE_THRESHOLD = 0.75` — frames below this become `unsorted`
- **Screen types**: onboarding, paywall, permission, home, settings, rating, loading, system_tray, app_switcher, bottom_sheet, transition, unsorted
- **system_tray**: ignore all blurred app UI behind it — only classify the tray itself
- **app_switcher**: do NOT read content inside app cards — only classify the switcher overlay
- **bottom_sheet**: ignore dimmed app behind it — only describe sheet content
- **transition**: any blurred/mid-swipe/partially-off-canvas frame — when in doubt, pick transition
- **state field**: one of `keyboard_open`, `item_selected`, `modal_open`, `menu_open`, `empty_state`, `error_state`, `scrolled`, `loading_content`, `multi_select` or `""` for resting/default
- state="" if the screen is in its default resting configuration

### Deduplication (app.py)

- Two-pass approach:
  1. **Pixel pass** (extractor.py): phash removes visually identical consecutive frames
  2. **Semantic pass** (app.py): key `(label, key_text, components_tuple, state)` removes same screen seen multiple times
- If `key_text` is empty AND `components` is empty → use full file path as key (keep it, never collapse unknowns)
- Including `state` in the dedup key means: `home` with keyboard open ≠ `home` at rest — kept as separate screens
- Do NOT deduplicate on label+key_text alone — same text on different component layouts = different screens

### Figma Plugin (code.js + ui.html)

- Uses `figma.createSection()` + `resizeWithoutConstraints()` to create labeled groups
- `networkAccess` in manifest.json requires a `"reasoning"` field — without it Figma throws manifest error
- Full flow lives inside the plugin: upload → poll → import. No manual job ID needed.
- Manifest survives server restarts (written to disk, read from disk in `/plugin-manifest`)
- Layout per card: image rect 390×844px, then below it:
  - `key_text` in dark at +6px (font 12, dark gray)
  - `components` joined with ` · ` at +22px (font 10, purple)
  - `state` at +38px (font 10, orange) — only rendered if non-empty
- `LABEL_H = 52` — total vertical space reserved below each image card
- All cards in one row per section (no wrapping), sections laid out left-to-right with 80px gap

### API / Server

- CORS applied globally via `after_request` — required for Figma plugin to reach localhost
- `/plugin-manifest/<job_id>` reads from disk first so it works after server restarts
- Jobs stored in-memory dict — lost on restart, but manifests persist on disk
- Server port: **5055**

---

## Session History

### Session 20
- **Adaptive FPS extraction** (`processor/motion.py` + `processor/extractor.py`) — flat 4 fps replaced with a two-pass scheme: scout pass at 6 fps for the whole video, then per-zone dense passes triggered by detected motion. Scout fps bumped 4 → 6 closes the "transition between two scout frames" blind spot from <250ms to <167ms. Dense fps scales with zone duration: `<400ms → 30 fps`, `400–1500ms → 15 fps`, `>1500ms → 8 fps` (long zones are usually scrolling, dense detail there is wasteful).
- **`processor/motion.py` (new)** — `MotionZoneConfig`, `MotionZone` (canonical home; `boundary_recovery.py` re-exports for backward compat), `compute_consecutive_motion`, `detect_motion_zones`, `dense_fps_for_zone`, `index_in_any_zone`. Detects two zone kinds: `multi_frame_motion_zone` (consecutive phash diff > 8 after a 3-frame moving average smooth) and `single_frame_anomaly_zone` (one scout frame whose phash differs strongly from BOTH neighbours — the exact signature of a transient state today's stability filter *deletes*). Anomaly zones get the short-zone fps (30) so a 167ms modal pop is captured at 5 frames.
- **`extractor.py` adaptive path** — `extract_frames_adaptive` runs the scout pass, detects zones, dense-extracts each zone via `ffmpeg -ss <start> -i <video> -t <duration> -vf fps=<dense>` (input-side seek for speed; the ±1-frame zone expansion absorbs the keyframe drift), renames dense outputs as `frame_z{NN}_NNNN.jpg` under `frames_dense/<zone_id>/`, and returns `(merged_paths, timestamp_map, zones)`. Stability filter + consecutive dedup now run only on out-of-zone scout frames — running them on dense-pass 15–30 fps frames would shred the very transitions we paid extra to capture. A final global dedup pass with `content_hash` still collapses truly identical screens across the merged list. Legacy `extract_frames` (flat 4 fps) preserved for the try/except fallback.
- **`frame_timeline.py` timestamp override** — `build_timeline_items` gains an optional `timestamp_map: dict[path, int]`. Dense-pass filenames don't match the `frame_NNNN.jpg` regex, so the map (built from `zone.start_ms + i * 1000 / dense_fps`) becomes the authoritative ms-per-path. `timestamp_source="explicit_timestamp_map"` is recorded on each item; the timeline summary picks up the new `explicit_timestamp_map` strategy value.
- **`pipeline_steps.py` wiring** — `extract_step` returns `(frames, timestamp_map, motion_zones)` and tries the adaptive path first, falling back to flat-fps on any exception with `Adaptive extraction fallback: <reason>`. `_apply_segment_selection_safely` accepts and forwards `timestamp_map`. `_apply_boundary_recovery_safely` accepts `motion_zones` and passes them into `apply_boundary_recovery` — the `motion_zones=None` placeholder from Session 19 is gone. Now contamination scoring actually lights up on real recordings: frames at zone peaks get rejected, frames just before/after become boundary-recovery candidates.
- **`boundary_recovery.py`** — removed the local `MotionZone` dataclass placeholder, imports from `motion.py` and re-exports under the same name so every existing call site keeps working.
- **Not implemented (deferred)** — GPU-accelerated ffmpeg decode (videotoolbox on Mac), opening each frame only once across stability passes (current `_is_horizontal_split` / `_is_slide_blend` etc. still re-open JPEGs), parallel scout-pass hashing. The decode-once refactor is the next obvious win once adaptive sampling lands. Smoke test at `tmp/test_motion_smoke.py` (22 checks) covers compute_consecutive_motion, MotionZone re-export, empty/tiny inputs, flat-zero-zone, multi-frame zones, single-frame anomaly zones, the duration ladder (30/15/8), `index_in_any_zone`, threshold sensitivity at 6 fps, and end-to-end boundary-recovery integration with a real `MotionZone` from `motion.py`. All three suites green: motion (22) + segments (22) + boundary_recovery (24, with one test updated to accept the new zone_type vocabulary).

### Session 19
- **Generalized boundary recovery + transition-contamination guard** (`processor/boundary_recovery.py`) — new module that runs after stable segment selection and before classification, applied across the entire timeline (not first-screen-only). Key responsibilities: detect state boundaries from motion zones (when available) or segment gaps, score every frame's likelihood of being a corrupted mid-transition frame, recover stable skipped states between representatives, reject transition-contaminated candidates, replace representatives that landed too close to a transition edge with a safer in-segment alternative, and preserve short intermediate states. Public surface: `BoundaryRecoveryConfig`, `StateBoundary`, `TransitionContaminationResult`, `MotionZone` (placeholder until motion module lands), `detect_state_boundaries`, `score_transition_contamination`, `classify_boundary_candidate`, `recover_boundary_states`, `is_near_transition_boundary`, `apply_boundary_recovery`, `serialize_boundary_recovery`, `save_boundary_recovery`.
- **Contamination scoring** — additive 0..1 score: `+0.35` inside transition zone, `+0.25` near zone edge (`transition_edge_guard_ms=180`), `+0.20` each for high `motion_score_prev` / `motion_score_next`, `+0.15` combined, `+0.10` for low sharpness. Thresholds: `>=0.65` contaminated, `>=0.82` hard-contaminated. Reasons stored as semicolon-joined tags on `item.extra.contamination_reason` along with `transition_contamination_score` and `is_transition_contaminated`. With motion/sharpness fields currently unpopulated by the extractor the score collapses to boundary-edge geometry only — exactly the conservative behaviour the spec demands.
- **Recovery rules** — for each boundary, scan pre-window `[start_ms - boundary_lookback_ms, start_ms - transition_edge_guard_ms]` and post-window `[end_ms + transition_edge_guard_ms, end_ms + boundary_lookahead_ms]` for non-selected, non-contaminated, stable-segment candidates. At most `max_recovered_states_per_boundary=2`. Recovered items get `keep_reason="recovered_boundary_state"`, `extra.boundary_recovery=true`, `extra.boundary_id`, and `extra.review_reason="possible_skipped_intermediate_state"`. If a boundary has only contaminated candidates, the boundary records `issue_type="possible_skipped_state_only_seen_as_transition_contaminated_frame"` and a warning — the contaminated frame is never inserted.
- **Edge-guard for representatives** — `_find_safer_in_segment` searches the same segment for a non-contaminated alternative outside the transition edge zone; if found, swaps representatives and tags the segment with `extra.representative_replaced_reason="near_transition_boundary"`. When no safer alternative exists, the rep stays put and is tagged `extra.review_reason="representative_near_transition_boundary"`. Edge-guard pass runs after contamination scoring and before recovery so it can use both signals.
- **Drift / duplicate-representative detection** — `_detect_duplicate_drift` fires when adjacent representatives share a path, or share `(label, screen_type)` within `duplicate_representative_window_ms=1200`. Tags the boundary with `adjacent_representatives_look_like_drift` and `issue_type="duplicate_representative_after_boundary"`; recovery then tries to fill in the missing middle state from non-contaminated candidates in the boundary windows.
- **Short intermediate state preservation** — `_preserve_short_intermediate_states` rescues `short_stable` segments that are sandwiched between other segments and have a non-contaminated representative. Tagged `keep_reason="short_state_preserved"`, `extra.review_reason="short_intermediate_state"`. First/last segments are skipped (first-segment safety lives in `frame_segments.py`).
- **Pipeline integration** (`processor/pipeline_steps.py`) — `_apply_boundary_recovery_safely` slots between `_apply_segment_selection_safely` and `classify_step`. The whole call is wrapped in try/except: any exception or empty result logs `Boundary recovery fallback used: <reason>` and forwards the original representative paths so Gemini sees exactly what it would have seen pre-job. Logs are concise: boundaries detected, contaminated/recovered/rejected counts, edge-guard warnings — no per-frame spam.
- **New artefact `output/<job_id>/boundary_recovery.json`** — version-1 schema with `version`, `job_id`, `boundary_count`, `issue_count`, `recovered_count`, `contaminated_candidate_count`, `rejected_contaminated_candidate_count`, `transition_edge_warnings`, `fallback_used`, `fallback_reason`, `warnings`, and a `boundaries[]` array (boundary_id, boundary_type, start/end/peak ms, before/after segment + representative ids, issue_detected, issue_type, recovered_frame_ids, contaminated_candidate_ids, rejected_candidate_ids, per-boundary warnings).
- **`timeline.json` gains a top-level `boundary_recovery` block** — summary counts (enabled, boundaries_detected, issues_detected, recovered_states, contaminated_candidates, contaminated_candidates_rejected, transition_edge_warnings, short_state_preserved, fallback_used, fallback_reason, warnings). Item-level changes live in `extra` so the `FrameTimelineItem` dataclass stays stable: `transition_contamination_score`, `is_transition_contaminated`, `contamination_reason`, `boundary_recovery`, `boundary_id`, `needs_review`, `review_reason`, `representative_for_segment`, `replaced_representative_id`. `segments.json` also picks up `extra.representative_replaced_reason` on segments where the rep was swapped by the edge guard.
- **Not implemented (deferred to later jobs)** — actual motion-zone extraction (`processor/motion.py` not built yet; module accepts `MotionZone` placeholders so contamination scoring lights up when the data arrives), adaptive sampling, OCR, manifest redesign, Gemini prompt changes, OpenAI/Claude providers, UI controls, aggressive duplicate deletion, full visual ML transition classifier. Smoke test at `tmp/test_boundary_recovery_smoke.py` (24 checks) covers all 11 spec acceptance criteria plus the contaminated-rejection path.

### Session 18
- **Stable segment detection + representative frame selection** (`processor/frame_segments.py`) — between extraction and classification the pipeline now groups consecutive frames into stable segments (gap rule: `max_gap_ms=500`) and forwards one representative per segment to Gemini instead of every extracted frame. Conservative by design: when timestamps are missing the index-fallback creates one single-frame segment per frame, which preserves pre-existing behavior. Defaults: `min_segment_duration_ms=250`, `edge_guard_ms=150`, `preserve_short_early_segments=True`. First segment is preserved even when short; `first_representative_starts_late` warning fires when the chosen representative is more than `edge_guard_ms*2` after the earliest observed timestamp. Representative selection picks the frame closest to the segment midpoint, falling back to the inner slice (`items[1:-1]`) when the edge guard excludes everything; deterministic tie-break by index.
- **Pipeline integration** (`processor/pipeline_steps.py`) — `_apply_segment_selection_safely` wraps the call in try/except and on any failure or empty representative list logs a warning and forwards all extracted frames unchanged so the classifier/manifest path behaves exactly as before. `_update_items_with_classifications` downgrades representatives that the classifier's semantic dedup drops to `status='removed' / remove_reason='duplicate_removed_by_classifier'`.
- **New artefact `output/<job_id>/segments.json`** — version-1 schema with `version`, `job_id`, `segment_count`, `representative_count`, `fallback_used`, `config`, `warnings`, and a `segments` array (segment_id, start/end index, start/end ms, duration_ms, frame_ids, frame_paths, segment_type, representative_frame_id, representative_path, representative_index, representative_timestamp_ms, keep_reason, needs_review, review_reason, extra). Human-readable, no API keys, no base64.
- **`timeline.json` gets `segment_id` per item, plus `fallback_used` / `fallback_reason` at the top level** — representatives carry `is_final=True` and `keep_reason="stable_segment_representative"` (or `"single_frame_segment"`); non-representative frames inside a segment are `status="removed"` with `remove_reason="non_representative_segment_frame"`. Files on disk are never deleted by segment selection.
- **Not implemented (deferred to later jobs)** — adaptive FPS, motion zone resampling, high-FPS extraction, sharpness/brightness/contrast scoring, full first-screen look-behind recovery, Gemini prompt changes, OpenAI/Claude providers, OCR. No change to Gemini behaviour, manifest schema, or Figma plugin compatibility. Smoke test at `tmp/test_segments_smoke.py` (22 checks) covers detection with/without timestamps, representative selection, first-segment safety, fallback, and JSON serialization.

### Session 17
- **Published to GitHub** — repo at https://github.com/Kononory/screencast-to-figma. Local server model: users clone, run `python app.py`, load plugin in Figma desktop. No hosted backend.
- **Plugin: API provider setup screen** — on first launch the plugin shows a settings screen with provider selector (Gemini / OpenAI / Claude / No AI) and API key input. Settings saved to `figma.clientStorage` and persist across sessions. "Change" link in footer returns to settings.
- **Plugin: AI features dimmed when no key** — AI Classification and Competitive Intelligence sections rendered at 35% opacity with `pointer-events: none` when no API configured. "Set up AI API →" link shown below them.
- **API key travels with each request** — plugin sends `api_key` and `provider` with every `/upload` and `/compare` POST. `app.py` uses request value first, falls back to `GEMINI_API_KEY` env var. Key threaded through `_run_pipeline_from_file` → `_process` → `classify_frames` / `analyze_ux`. Server no longer requires env key if plugin provides one.
- **Removed FIGMA_TOKEN** — not used anywhere in code. Dropped from `.env.example`.
- **Removed server URL field from settings** — all users run locally so `localhost:5055` is constant. No configurable URL.
- **`.gitignore` updated** — added `output/` and `diffs_debug.txt`.



### Session 1–2 (initial build)
- Built ffmpeg extractor, Gemini classifier, Flask server
- Added Figma plugin with section layout
- Fixed: ffmpeg vsync deprecated in v7, 0-frame output
- Fixed: Figma manifest networkAccess requires reasoning field
- Fixed: all screens collapsing to 1 unsorted (empty key_text dedup bug)

### Session 3
- Added model fallback (gemini-2.5-flash → gemini-2.5-flash-lite)
- Added 404/NOT_FOUND to fallback error catches (non-existent model names)
- Switched from 1fps to 4fps + stability filter for better transition handling
- Raised stability filter threshold 12→18 to preserve quick loading screens
- Added UI component detection to prompt and manifest

### Session 4
- Added `state` field to classifier prompt and response
  - Vocabulary: keyboard_open, item_selected, modal_open, menu_open, empty_state, error_state, scrolled, loading_content, multi_select
  - Dedup key updated to include state — same screen in different states = distinct screen
- State shown in Figma as orange text under component tags
- Passed `state` through full pipeline: classifier → results_store → manifest → plugin-manifest → ui.html → code.js

### Session 5
- **Fixed**: system_tray and app_switcher were appearing in Figma output — added both to exclusion list in `results_store.py` alongside `transition`. These are OS overlays, never design deliverables.
- **Fixed**: unsorted screens now also excluded from Figma output — they are noise, not design deliverables.
- **Lowered CONFIDENCE_THRESHOLD**: 0.75 → 0.60 — fewer screens fall to unsorted from borderline confidence.
- **Added sub-naming for all screen types** — every type now names from context (not just onboarding/paywall):
  - `home/recordings`, `home/folders` — named from active tab
  - `bottom_sheet/create_folder`, `bottom_sheet/choose_destination` — named from sheet title
  - `settings/notifications`, `settings/storage` — named from section heading
  - Result: Figma gets "Home / Recordings", "Bottom Sheet / Create Folder", etc. — real flow sections
- **Prompt rule: never use unsorted** — Gemini now instructed to pick the closest match and lower conf instead of falling back to unsorted. Unsorted is only for black frames / lock screens.
- **Rewrote classifier prompt** completely — key changes:
  - Structured into 5 explicit steps (READ ALL TEXT → IDENTIFY FOREGROUND LAYER → CLASSIFY → LIST COMPONENTS → STATE)
  - Step 1 now explicitly demands reading EVERY list item, tab name, button, price, section header
  - Step 2 adds "topmost sheet wins" rule — nested bottom sheets: classify only the topmost one
  - Bottom_sheet rule now says: read sheet title + EVERY row + every button (not just "describe sheet content")
  - Added `list_item_checked`, `list_item_radio`, `list_item_default`, `segmented_control`, `tab_bar_pill` to component vocabulary
  - Interaction state detection is now tied to visual evidence (checkmark ✓, blue highlight, keyboard visible, etc.)
  - Added example showing TWO different bottom_sheet entries with different key_text and state — teaches model they are distinct

### Session 9
- **Unsorted section restored in Figma** — when AI quota fails all frames land in "Unsorted" section so nothing is silently lost. Previously filtered out; now kept.
- **phash dedup restored with threshold=5, consecutive-only** — removed phash dedup caused 146 frames from a 37s video (4fps), blowing both Gemini model quotas. New `_dedup_consecutive` compares each frame only to the last kept frame (not all seen frames), so cumulative state changes still accumulate. Threshold=5 removes truly held frames (0-3 bit JPEG noise) while keeping state changes (6+ bits).
- **API errors now visible in plugin log** — `classify_frames` accepts `log_fn` callback; all Gemini failures now appear in the plugin log instead of disappearing to terminal. Shows exact error and which model failed.
- **Root cause of all-unsorted bug** — 4fps × 37s = 146 frames → 17 API batches → quota exhausted on both models → silent fallback to `unsorted (0.00)`. Was invisible because errors only printed to terminal.

### Session 16
- **Classifier: `special_offer` as a new screen type** — discount/cancel-flow screens ("50% OFF", "One Time Offer", "Limited Time Offer", crossed-out original price + countdown timer) were collapsing into `paywall`. Added `special_offer` type placed BEFORE `paywall` in STEP 3 so it takes priority when a discount badge or "% OFF" text is present. These are downsell screens shown after paywall dismiss — a distinct conversion pattern worth tracking separately in a product audit. Naming: 50_off / one_time / limited_time / black_friday. New components: `discount_badge`, `original_price_crossed`. Added few-shot example.
- **Classifier: paywall exception for functional screens with secondary upsells** — player screens (waveform + play controls + time counter) with a small locked-feature banner or inline upgrade price were being classified as paywall due to the "ANY price visible" rule being too greedy. Added EXCEPTION clause: price must be the PRIMARY content of the screen (dedicated price card/block occupying a meaningful portion); a price appearing only as a locked-feature banner, inline upgrade row, or padlock overlay on an otherwise functional screen → classify by dominant UI (home/player, home/recordings, etc.), not paywall.
- **Classifier: paywall priority rule — price is the only deciding signal** — paywall screens with social proof (star ratings, review counts like "4.8 ★ 220k reviews") were being classified as onboarding because Gemini weighted those elements as onboarding signals. Root cause: no explicit tie-breaker in the prompt. Fix: added PRIORITY RULE to `paywall` — any price box or price text ($/€ + billing period) visible anywhere → always paywall, regardless of other elements. Social proof, feature checklists, guarantee badges explicitly called out as shared elements that are NEVER the deciding signal. Added "NO price text, NO price boxes" guard to `onboarding` definition.

### Session 15
- **UX Analysis prompt rewrite — mandatory if-then-metric hypothesis format** — previous prompt produced descriptive paragraphs; user wanted explicit "If [decision] → [metric] up/down" statements. New mandatory rule appended before all field instructions: every sentence must follow the form "If [design decision] → [metric name] [increases/drops/~X%] because [mechanism]." Allowed metric names list added: trial_start_rate, trial_to_paid_rate, cancel_rate, D1/D7/D30_retention, permission_grant_rate, time_to_first_value, paywall_conversion_rate, ARPU, LTV, CAC_payback. Each of the 6 fields rewritten to demand a specific count of if-then hypotheses covering a precise list of sub-topics (paywall structure, trial length, CTA wording, drop-off step, permission timing, retention loop, A/B test ideas with MDE). "Never describe what you see" constraint made explicit.
- **code.js: 2 missing analysis keys now rendered** — `domain_analysis` and `feature_strategy_reasoning` were being generated by Gemini but silently dropped in Figma (not in the `defs` array). Added both; `defs` now renders all 6 sections: DOMAIN & POSITIONING → MONETIZATION HYPOTHESIS → ONBOARDING HYPOTHESIS → FEATURE STRATEGY → COPY & CTA → PRODUCT BETS & A/B HYPOTHESES.
- **code.js: section layout changed to single row** — cards were wrapping at 5 columns (`Math.min(count, 5)`). Changed to `cols = count` so all screens in a section appear in one horizontal row regardless of count.

### Session 14
- **Classifier: three-way component disambiguation** — `player_timeline` and `slider` added to component vocabulary alongside existing `progress_bar`. Root cause: Gemini was classifying voice recorder playback scrub bars as `progress_bar` because it was the only track-shaped component available.
  - `progress_bar` — no thumb, static fill, no time labels (loading / onboarding step indicator)
  - `slider` — draggable circular thumb, no timestamps, used in settings (volume, speed, sensitivity)
  - `player_timeline` — draggable playhead on waveform or track, ALWAYS has elapsed+total time labels flanking it, play/pause buttons nearby. Decision rules added as explicit text in STEP 4.
  - Added `home/player` example to few-shot block so Gemini has a concrete reference for `player_timeline`.
- **Classifier: bottom_sheet / action_sheet / alert disambiguation** — Added `action_sheet` and `alert` as two new screen types. Previously all overlays defaulted to `bottom_sheet` or were mis-classified.
  - `bottom_sheet` — anchored at bottom, HAS drag handle, tall (≥40% screen), rich content (lists, forms, pickers). Title header always present.
  - `action_sheet` — anchored at bottom, NO drag handle, short (≤35%), text-only action rows. iOS: isolated "Cancel" below the group with a gap. No toggles or input fields.
  - `alert` — centered modal, does NOT touch the bottom edge, narrower than screen width. Title + body + 1–3 buttons. iOS: inset rounded rect on blur. Android: white card.
  - Updated STEP 2 "Other OS layers" to cover all three.
  - Added feature naming rules for `action_sheet` and `alert`.
  - Added new components: `alert_dialog_box`, `action_sheet_cancel`, `destructive_action_row`.
  - Updated key_text rules: action_sheet = all action labels joined with " · "; alert = dialog title only.
  - Added `action_sheet/delete_recording` and `alert/delete_recording` to few-shot examples.

### Session 13
- **UX Analysis — product thinking rewrite** — prompt completely replaced. No longer a surface description. Now asks Gemini to reason like a growth PM: what metric each design decision optimizes, what A/B hypotheses are embedded, what competitor patterns are applied, what to test first. Four sections: Monetization Hypothesis · Onboarding Hypothesis · Copy & CTA · Product Bets & A/B Hypotheses. Keys in JSON renamed accordingly; `code.js` updated to match.
- **Section grouping fix** — `plugin-manifest` now merges all children into their parent section (one "Onboarding" section instead of "Onboarding / Call Recording", "Onboarding / Transcription", etc.). The `key_text` under each card already identifies the individual screen. `_analysis` key skipped via `startswith("_")` guard.
- **UX Analysis block in Figma** — after classification, one extra Gemini call sends up to 20 representative frames and asks for a structured analysis. Result appears as a text block to the LEFT of all sections in Figma (x=0, sections start at x=560).
  - `processor/analyzer.py` — `analyze_ux(frame_paths, api_key)`: sends frames + prompt, parses JSON response with model fallback
  - Prompt asks four questions: UI style · Copywriting & messaging · Monetization patterns (prices, offers, CTAs, urgency) · Onboarding flow
  - `app.py`: calls analyzer after dedup, subsamples to ≤20 non-junk frames, stores `_analysis` key in `manifest.json`
  - `/plugin-manifest`: returns `analysis` field alongside `sections`
  - `ui.html`: passes `analysis` in plugin postMessage; shows "Analyzing UX patterns…" step
  - `code.js`: loads Inter Bold; renders title + 4 labeled sections as text nodes at x=0; offsets all Figma sections to `ANALYSIS_W + SECTION_GAP = 560`
  - Cost: ~$0.001–0.003 per video (one additional Gemini call)
- **Gemini classifier improvements** — better filtering of junk frames:
  - Added FLAG 0 to STEP 2: if frame is motion-blurred, out-of-focus, compression-artifacted, or ghosted and text is unreadable → immediate transition, no content parsing
  - Added `home_screen` type (OS launcher: icon grid on wallpaper + dock). Excluded from Figma output in `results_store.py` alongside transition/system_tray/app_switcher. key_text = visible app names. No pixel heuristic (would false-positive on app grids)
  - Sharpened `transition` definition: now explicitly includes motion blur, focus blur, artifacts, ghosting, and "if you cannot read the headline → transition"
- **Deduplication UI redesign** — section header now uses `type type--medium` title + subtitle to match AI Classification section. Callout moved from above slider to below it.
- **Blank frame filter** — `filter_blank_frames` in `extractor.py` removes pure-black (mean<15) and pure-white (mean>240) frames after extraction, before classification. Called in `app.py _process`; logs "Removed N blank frames (black/white)" when any are dropped. These never reach Gemini and never appear in Figma.
- **Extraction summary in plugin** — after import, status line shows: `Ох, їбать-їбать, оце потужненько було: 20 extracted · 12 imported · 8 dupes removed`. Counts stored as `extracted`/`dupes` in the job dict, returned from `/status`, captured in `jobStats` in the plugin and shown in the `done` message handler. "dupes removed" only appears when >0.
- **AI Classification toggle** — added on/off toggle to plugin UI (default ON). When OFF, skips the entire Gemini call; all extracted frames are passed directly to Figma under "Unsorted" with no labels or metadata. Useful when you only need raw frames without AI cost or quota usage.
  - `ui.html`: toggle row between Dodep and log sections; sends `classify=true/false` in form POST
  - `app.py /upload`: reads `classify` flag, threads it through `_run_pipeline_from_file` → `_process`
  - `_process`: when `classify=False`, skips `classify_frames`, semantic dedup, and AI log; assigns all frames `unsorted` label directly

### Session 12
- **Fixed: all frames dropping to unsorted when Gemini succeeds** — root cause was non-greedy regex `\[.*?\]` in `_ask_gemini` (classifier.py:296). Non-greedy stops at the first `]` in the response, which is always inside a `"components": [...]` array, not the outer JSON array. `json.loads` received malformed JSON, threw `JSONDecodeError`, and the function silently returned `[unsorted] * count`. Fix: changed `.*?` → `.*` (greedy). The greedy version matches from the first `[` to the last `]`, capturing the full outer array. Confirmed with job 1af3e56c where all 69 frames returned unsorted despite `gemini-2.5-flash ok` on every batch.

### Session 11
- **ORB consistent-offset duplicate detector** (`_is_feature_duplicate`) — detects mid-push-navigation frames that survive all phash-based checks. Extracts ORB keypoints from left/right halves, matches them with BFMatcher (crossCheck, distance < 50), then computes horizontal offset for each match. If ≥8 matches cluster within 30px of the mean offset → transition. Geometric signature: duplicate UI element (search bar, nav bar) at a consistent horizontal shift ≈ half frame width. Normal screens: accidental cross-half matches have scattered offsets, no cluster. Wired into stability filter with `prev_diff > 3 and next_diff > 3` guard (last-chance check after all phash tests fail).
- **Server status dot in plugin UI** — green/gray/red dot next to server input field. Polls `GET /` on load and on every input change (600ms debounce, 3s timeout). Online = green, unreachable = red, checking = gray.
- **5-flag transition validation in classifier prompt** — STEP 2 restructured as ordered flag protocol (global scaling → OS corner radius → semantic X-axis duplication → bounding box clipped by internal seam → vertical OS background strip). First TRUE flag → output transition immediately, stop reading content.

### Session 10
- **Multi-split horizontal transition detection** — `_is_horizontal_split` previously only tested the 50/50 midpoint. A slide transition at 30% or 70% (seam near one edge) had its left/right halves compared at a point where one side was mixed content, reducing the signal. Now tests 33%, 50%, and 67% split points; flags as transition if ANY shows >18-bit left/right difference. Catches partial-entry frames (a new screen just peeking in from the edge).
- **Dead code removed** — `_trim_trailing_motion` had a `hash_by_path` dict built from `_original_index` that was never used in the while loop. Removed.
- **Classifier prompt: in-app navigation transition rules** — Added explicit visual cues for mid-push-navigation frames that the OS-level card check doesn't catch:
  - Duplicate full-width element (search bar, nav bar, tab bar) visible at two horizontal positions simultaneously → transition
  - Back button (`←`) not in top-left corner (centered or right-of-center) → incoming screen's nav controls mid-slide → transition
  - Any major UI element clipped/cut off at the left or right edge → screen entering/exiting viewport → transition
  - Two nav bars overlapping horizontally → transition

### Session 8
- **OS-level state detection in prompt** — STEP 2 now runs BEFORE reading any app content and checks explicit visual cues for app-minimize transitions:
  - Card scaling: app does not fill full canvas, appears as a floating card
  - Background reveal: space outside the app shows OS wallpaper / solid color / blurred layer
  - Asymmetric clipping: one edge flat/clipped, opposite edge shrinking inward
  - Rounded OS corners on the app window boundary
  - Multiple app cards floating side by side
  → Mid-gesture (still full-size but shrinking) → transition
  → Gesture complete (cards fully formed) → app_switcher
- **Distinction between transition and app_switcher** tightened: transition = app scaling toward a card but not yet complete; app_switcher = cards fully formed and browsable.

### Session 7
- **Removed phash dedup from extractor** — phash threshold=20 was silently dropping subtle state changes (checkmark appearing = 3-5 bit diff, tab switch = 5-10 bit diff — both << 20). Now only stability filter runs; true duplicates are handled by semantic dedup (AI label+key_text+state). More frames reach Gemini, more states are captured.
- **Plugin no longer auto-closes** — `figma.closePlugin()` removed; plugin stays open after import so you can read the log. A "Close plugin" button appears instead.
- **`/log/<job_id>` browser endpoint** — open `http://localhost:5055/log/<job_id>` in any browser to read the full classified-frames log any time.

### Session 6
- **Debug log panel in plugin** — after every job, a green-on-black monospace textarea shows the full per-frame AI output: `01 home/recordings (0.88) "Recordings"`, `DEDUP: home/recordings "Recordings" dropped`, etc. Essential for seeing what Gemini actually returned.
- **Grid: 4 cols → 3 cols, thumbnail 300×600 → 390×844** — larger thumbnails mean Gemini can read fine details (checkmarks, active tab highlights, small text). 9 frames per batch instead of 16; ~1.8× more API calls but much better state detection.
- **Per-frame logging** in `app.py _process`: logs every raw AI classification before dedup, and logs every frame dropped by dedup.
- **Plugin height**: 420 → 560px to fit the log panel.

---

## Known Limitations / Future Ideas

- Jobs are in-memory only — server restart loses job status (manifests are safe on disk)
- No cleanup of old output directories — grows over time
- Gemini grid approach means all 16 frames in a batch get `unsorted` if the model fails both fallbacks
- **Few-shot visual examples** — Gemini supports multiple images in one call. Could add `examples/` folder with annotated reference screenshots prepended to each batch call. Would significantly improve recognition but increases token usage. Not yet implemented.
- State vocabulary is fixed — could be extended with custom states per app category
- Could add frame timestamp metadata to manifest for easier debugging
