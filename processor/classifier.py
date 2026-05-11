import io
import math
import json
import re
import cv2
import numpy as np
from google import genai
from google.genai import types
from PIL import Image, ImageDraw

GRID_COLS = 3
THUMB_SIZE = (390, 844)
CONFIDENCE_THRESHOLD = 0.60

MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite-preview",
]

PROMPT = """You are a mobile app UX analyst. Analyze screenshots from a utility app screen recording (call recorder, voice recorder, phone cleaner, plant scanner, fitness tracker, etc.) in SEQUENCE ORDER.
Frames are numbered {start}–{end} (top-left to bottom-right).

For each frame follow these steps IN ORDER:

── STEP 1: READ ALL VISIBLE TEXT ──────────────────────────────────────────────
Before classifying, read and note internally EVERY text element:
• Headline and subtitle
• ALL list item labels and their subtexts (read each one)
• ALL tab names — both active (highlighted/pill) and inactive
• ALL button labels and link texts
• Prices, billing periods, badge text ("FREE TRIAL", "BEST VALUE", etc.)
• Toggle labels, section headers, empty-state messages, error text

── STEP 2: TRANSITION VALIDATION — RUN BEFORE ANY UI PARSING ─────────────────
Check all six flags in order. At the first TRUE flag, stop and output transition.

  FLAG 0 — Frame is unreadable (blur / artifacts / ghosting):
    The majority of the frame is motion-blurred, out-of-focus, compression-artifacted,
    or two frames are ghosted/overlaid. If you cannot read the main headline OR cannot
    identify the primary UI component → FLAG_TRANSITION immediately, do not attempt
    to read any content.

  FLAG 1 — Global scaling / edge margins (early OS swipe-to-home, app switcher entry):
    The root app interface does NOT occupy 100% of the image canvas. Negative space,
    a solid color (black, white, grey), OS wallpaper, or a blurred layer is visible at
    the absolute left, right, or bottom edge of the canvas — the app has started scaling
    down into a card. → FLAG_TRANSITION.

  FLAG 2 — OS-applied corner radius:
    The outer corners of the app container are visibly rounded with large radii that
    disconnect the UI from the 90-degree physical corners of the canvas. These are
    OS-applied corners (not the app's own UI corners), signalling the app window is
    detaching from the screen. → FLAG_TRANSITION.

  FLAG 3 — Semantic duplication on X-axis:
    A singleton element (top navigation bar, screen title text, search bar, tab bar, FAB)
    that should appear ONCE per screen is visible at TWO different horizontal positions
    simultaneously — one copy from the outgoing screen, one from the incoming screen,
    both partially visible side by side. → FLAG_TRANSITION.

  FLAG 4 — Bounding box clipped by internal vertical seam:
    A button label, text node, card, or list row is truncated not at the physical screen
    edge but by a sharp internal vertical line running top-to-bottom through the UI.
    That line is the boundary between two slides. → FLAG_TRANSITION.
    Example: "Cancel" label cut in half; a list row vanishing behind a mid-screen seam.

  FLAG 5 — Vertical strip of OS background or blur:
    A vertical column of any width running the full height of the frame shows OS
    wallpaper, solid color, or blur — cleanly separating two distinct layout regions
    on its left and right. → FLAG_TRANSITION.

  → IF any flag is TRUE: output transition, conf 0.95, empty key_text/components/state.
    Do NOT read or describe any app content in the frame.
  → IF all flags are FALSE AND multiple fully-formed app cards are visible floating on
    OS background → app_switcher.

Other OS layers:
• Bottom sheet open → classify the SHEET, not the dimmed app behind
• Action sheet open → classify the ACTION SHEET, not the dimmed app behind
• Alert / dialog open → classify the ALERT, not the app behind
• Two sheets or dialogs stacked → classify only the TOPMOST layer
• OS notification shade or Control Center pulled down → system_tray

── STEP 3: CLASSIFY ────────────────────────────────────────────────────────────
Screen types:
- onboarding — full-screen feature slide, NO bottom tab bar visible, NO price text, NO price boxes. A hero_illustration alone does NOT make a screen onboarding — if ANY price ($/€ + billing period) is visible → paywall, not onboarding. Name from headline (snake_case).
- special_offer — screen whose PRIMARY visual is a discount signal: large "XX% OFF" badge, "One Time Offer", "Limited Time Offer", "Special Offer", "Exclusive Deal", or a crossed-out original price next to a reduced price. Usually paired with a countdown timer or urgency copy ("This offer expires in…", "Don't miss out"). Appears as a downsell after the user dismisses the main paywall. Name from the discount value or offer type (e.g. 50_off, one_time, limited_time). ALWAYS takes priority over paywall when a discount badge or "% OFF" text is present.
- paywall — HARD PRICE OVERRIDE: if ANY standalone price text ($/€ amount + billing period, or "per month" / "per year" / "per week" / "lifetime") is visible as a conversion element → paywall. This overrides ALL other visual signals: hero_illustration, segmented_control, tab_bar_pill, list_view, feature checklist — none of these cancel the price signal. A paywall is allowed to contain a hero_illustration AND a segmented_control (used as a plan switcher, not app navigation) AND a list_view (feature checklist) — these are standard paywall UI patterns, not evidence of onboarding or home.
  PLAN SWITCHER NOTE: segmented_control or tab_bar_pill showing plan options ("Monthly | Annual | Lifetime") is a paywall UI element, NOT home screen navigation. Price visible nearby → paywall.
  EXCEPTION — secondary upsell only: if the dominant UI is a media player (waveform + playback controls + time counter) OR a recordings list, and a price appears only as a small locked-feature banner or padlock overlay → classify by dominant UI, NOT paywall.
  Name from billing period (annual / monthly / lifetime / weekly).
- permission — OS system dialog asking for mic/camera/notification access, OR pre-permission explanation card.
- home — bottom tab navigation bar is visible; main app content.
- settings — list of rows with toggles, chevrons, or disclosure arrows organized in sections.
- rating — "Rate this app" dialog with 5-star widget.
- loading — full-screen ongoing process with minimal interactive UI. Two forms: (a) app logo + activity spinner, nothing else; (b) downloading or initializing screen: "Downloading AI model", "Processing your recording", "Setting up…", "Preparing…" — dominant element is a progress bar or spinner with a process description label. If the screen exists solely to show a background operation in progress → loading, NOT home, NOT onboarding.
- system_tray — iOS Control Center or Android notification shade pulled from the top. Clock, battery, wifi icons visible. Classify this ONLY — completely ignore any app behind it.
- app_switcher — gesture is COMPLETE: multiple scaled app cards fully formed and floating on OS background. Cards have rounded OS corners. Classify this ONLY — do NOT read card content.
- bottom_sheet — tall panel (≥40% screen height) anchored at bottom WITH a visible drag handle (pill/bar) at its top. Rich content: lists, forms, pickers, text fields, input rows. Always has a title header. CRITICAL: ignore the dimmed app content behind the sheet entirely — do NOT read or classify what is visible behind the overlay. The empty state or list visible behind a bottom_sheet is NOT a separate screen. Name from the sheet's own title (snake_case).
- action_sheet — short contextual action menu at bottom (≤35% screen height), NO drag handle. Contains ONLY text action labels (3–7 items), optionally with leading icons. iOS: isolated "Cancel" button appears BELOW the action group with a visible gap. Android: same panel without the gap. No toggles, no chevrons, no input fields — pure tap-to-execute actions. Name from context action (snake_case).
- alert — small centered modal dialog that does NOT touch the screen bottom. Has a title (1 line), optional body text (1–3 lines), and 1–3 buttons at its base. iOS: inset rounded rectangle on system-blurred or dark scrim, narrower than screen width. Android: white card with title, body, text buttons. Name from dialog title (snake_case).
- home_screen — OS launcher: app icon grid arranged on wallpaper background, dock bar at bottom. No in-app navigation or app-specific UI. Classify as home_screen regardless of which icons are visible. key_text = a few visible app names joined with " · ".
- transition — ANY of: motion-blurred frame, focus-blurred frame, compression artifacts making text unreadable, two frames ghosted/overlaid, mid-swipe, partial slide, zoom animation, AND any frame where the app is scaling/shrinking toward a card but cards are not fully formed. If you cannot read the headline or identify a primary UI component → transition. When in doubt → always pick transition.
- unsorted — LAST RESORT ONLY. Use only if the frame is completely unrecognizable (black frame, camera pointed away, device lock screen). For ANY mobile app UI, always pick the closest match above.

Feature naming (snake_case from the most prominent title/headline):
- onboarding: "Record Any Call" → call_recording · "Transcribe Conversations" → transcription · "Remove Background Noise" → noise_cancellation · "Identify Any Plant" → plant_identification
- special_offer: name from discount value or offer type → "50% OFF" → 50_off · "One Time Offer" → one_time · "Limited Time" → limited_time · "Black Friday Deal" → black_friday
- paywall: read price period → annual | monthly | lifetime | weekly
- bottom_sheet: "Create Folder" → create_folder · "Choose a destination" → choose_destination · "Share Recording" → share_recording · "Sort by" → sort_by
- action_sheet: name from context or first non-Cancel action → "Delete Recording" → delete_recording · "Share" → share · "Sort by" → sort_by
- alert: name from dialog title → "Delete Recording?" → delete_recording · "Clear All?" → clear_all · "Storage Full" → storage_full
- home: if multiple tabs exist, name from the ACTIVE tab → "Recordings" → recordings · "Favorites" → favorites · "Folders" → folders
- settings: name from the section heading if one section dominates → "Notifications" → notifications · "Storage" → storage
- home_screen: no sub-naming → label stays "home_screen"

── STEP 4: LIST FOREGROUND UI COMPONENTS ──────────────────────────────────────
Use only components visible in the foreground layer. Choose from:
hero_illustration, hero_video, app_mockup, progress_dots, progress_bar, step_counter,
cta_full_width, cta_pill, cta_outlined, skip_button,
gradient_bg, dark_bg, image_bg,
price_cards_single, price_cards_dual, price_cards_triple,
free_trial_badge, lifetime_offer, urgency_timer,
feature_checklist, comparison_table, social_proof, guarantee_badge,
close_button, restore_link,
bottom_tab_bar, list_view, toggle_rows, search_bar, card_grid,
segmented_control, tab_bar_pill,
notification_shade, quick_settings_tiles, notification_cards,
app_cards_stack, dark_background,
sheet_handle, sheet_overlay, action_buttons_row, drag_indicator,
list_item_checked, list_item_radio, list_item_default,
player_timeline, slider,
alert_dialog_box, action_sheet_cancel, destructive_action_row,
discount_badge, original_price_crossed

FOUR-WAY DISTINCTION — bottom_sheet vs action_sheet vs alert vs rating:
• bottom_sheet — anchored at bottom, HAS drag handle (pill/bar at very top of panel),
  tall (≥40% screen). Rich content: titles, lists, forms, pickers, input rows.
  → If you see a drag handle + structured content → bottom_sheet.
• action_sheet — anchored at bottom, NO drag handle, short (≤35% screen). Text-ONLY
  tap-to-execute action rows. iOS: an isolated "Cancel" button sits BELOW the action
  group with a visible gap (separate rounded rect). Android: same without the gap.
  No toggles, no chevrons, no text fields.
  → If you see ONLY labelled actions + a Cancel → action_sheet.
• alert — centered modal, does NOT touch the bottom edge, noticeably NARROWER than
  screen width. Title + 1–3 lines of body + 1–3 buttons at the base.
  iOS: inset rounded rect on system blur. Android: white card, text buttons.
  → If the dialog floats in the CENTER of the screen → alert.
• rating — a 5-star widget is the dominant element. May look like a dialog but the
  star row is the key signal → always rating.

THREE-WAY DISTINCTION — progress_bar vs slider vs player_timeline:
• progress_bar — NO draggable thumb. Shows completion of a process (loading, upload,
  onboarding step). Static fill, no time labels, no playback controls nearby.
• slider — HAS a draggable circular thumb on a track. Used for settings / preferences
  (volume level, playback speed, sensitivity). Labels on left/right are values or
  setting names, NOT timestamps. No play/pause button nearby.
• player_timeline — HAS a draggable thumb/playhead on a waveform or uniform track.
  ALWAYS has elapsed + total time labels (e.g. "0:32" on left, "2:14" on right, or
  a single remaining counter). Play/pause/stop buttons are visible in the same UI block.
  Found in voice memo player, call recording player, audio editor.
  → If you see a waveform OR time counters flanking a scrub track → player_timeline.
  → If you see a thumb but no timestamps → slider.
  → If you see no thumb at all → progress_bar.

── STEP 5: INTERACTION STATE ───────────────────────────────────────────────────
Look for visual evidence of a non-default state:
- keyboard_open — software keyboard occupies the bottom half of the screen
- item_selected — one list row has a checkmark ✓ or blue highlight
- multi_select — two or more rows have checkmarks ✓
- modal_open — an alert dialog or overlay sits on top of the main content
- menu_open — a dropdown or context menu is expanded
- empty_state — list is empty, shows illustration or placeholder text
- error_state — red error text or error banner is visible
- scrolled — content is visibly scrolled (sticky header, cut-off first item)
- loading_content — spinner inside a content list (not full-screen loader)
- "" — default resting state, nothing active

IMPORTANT: Same screen type with DIFFERENT state, DIFFERENT active tab, DIFFERENT checked item, or DIFFERENT visible text = DISTINCT screen. Do NOT collapse them.

── OUTPUT ──────────────────────────────────────────────────────────────────────
Return ONLY a JSON array of exactly {count} objects:
[{{"label": "type/feature", "conf": 0.95, "key_text": "exact quoted text", "components": ["comp1", "comp2"], "state": ""}}, ...]

key_text: exact short quote of the most prominent foreground text (sheet title > dialog title > headline > active tab > price).
  Exception — bottom_sheet with NO explicit title (only action rows): set key_text to ALL visible row labels joined with " · " (e.g. "Create Folder · Move To Folder · Add Recordings"). This is required to distinguish two bottom_sheets that share the same first row but have different total content.
  Exception — action_sheet: set key_text to ALL action row labels joined with " · " (exclude "Cancel"). This distinguishes two action sheets that differ only in their last action.
  Exception — alert: set key_text to the dialog title text only (not the body).
state: one value from the list above, or "".
conf: 0–1 confidence. If you are unsure, still pick the best-fit type and lower the conf — do NOT default to unsorted.
NEVER use "unsorted" for a frame that shows a normal mobile app UI. Pick the closest type and set conf accordingly.

Examples:
[
  {{"label": "onboarding/call_recording", "conf": 0.97, "key_text": "Record Any Call Automatically", "components": ["hero_illustration", "progress_dots", "cta_full_width"], "state": ""}},
  {{"label": "paywall/annual", "conf": 0.93, "key_text": "$39.99 / year", "components": ["price_cards_dual", "free_trial_badge", "feature_checklist", "close_button"], "state": ""}},
  {{"label": "home/recordings", "conf": 0.88, "key_text": "Recordings", "components": ["bottom_tab_bar", "tab_bar_pill", "list_view", "search_bar"], "state": ""}},
  {{"label": "home/folders", "conf": 0.87, "key_text": "Folders", "components": ["bottom_tab_bar", "tab_bar_pill", "card_grid"], "state": ""}},
  {{"label": "home/recordings", "conf": 0.84, "key_text": "Recordings", "components": ["bottom_tab_bar", "list_view", "search_bar"], "state": "keyboard_open"}},
  {{"label": "home/player", "conf": 0.91, "key_text": "My Recording — 0:32 / 2:14", "components": ["bottom_tab_bar", "player_timeline", "action_buttons_row"], "state": ""}},
  {{"label": "bottom_sheet/create_folder", "conf": 0.94, "key_text": "Create Folder", "components": ["sheet_handle", "action_buttons_row"], "state": ""}},
  {{"label": "bottom_sheet/choose_destination", "conf": 0.92, "key_text": "Choose a destination", "components": ["sheet_handle", "list_view", "list_item_checked", "cta_pill"], "state": "item_selected"}},
  {{"label": "special_offer/50_off", "conf": 0.96, "key_text": "50% OFF — One Time Offer", "components": ["discount_badge", "original_price_crossed", "urgency_timer", "cta_full_width"], "state": ""}},
  {{"label": "action_sheet/delete_recording", "conf": 0.95, "key_text": "Delete Recording · Share · Rename", "components": ["action_sheet_cancel", "destructive_action_row"], "state": ""}},
  {{"label": "alert/delete_recording", "conf": 0.96, "key_text": "Delete Recording?", "components": ["alert_dialog_box", "destructive_action_row"], "state": ""}},
  {{"label": "settings/notifications", "conf": 0.85, "key_text": "Notifications", "components": ["list_view", "toggle_rows"], "state": ""}},
  {{"label": "app_switcher", "conf": 0.98, "key_text": "app switcher", "components": ["app_cards_stack", "dark_background"], "state": ""}},
  {{"label": "home_screen", "conf": 0.98, "key_text": "Messages · Camera · Maps · Photos", "components": [], "state": ""}},
  {{"label": "transition", "conf": 0.95, "key_text": "", "components": [], "state": ""}}
]"""


def _classify_local_fallback(frame_path: str) -> dict:
    """Pixel-only heuristic classifier. Runs when all Gemini models fail."""
    img = cv2.imread(frame_path)
    if img is None:
        return {"label": "unsorted", "conf": 0.0, "key_text": "", "components": [], "state": ""}

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    label = "home"
    conf = 0.25
    components = []
    state = ""

    # --- Nearly black frame → skip ---
    if np.mean(gray) < 20:
        return {"label": "unsorted", "conf": 0.1, "key_text": "", "components": [], "state": ""}

    # --- Loading: very low variance across whole frame ---
    if np.std(gray) < 35:
        return {"label": "loading", "conf": 0.55, "key_text": "", "components": [], "state": ""}

    # --- Bottom sheet: bright panel in bottom 60%, dark overlay in top 30% ---
    top_mean = np.mean(gray[:int(h * 0.30), :])
    bottom_mean = np.mean(gray[int(h * 0.40):, :])
    if bottom_mean > 210 and top_mean < 90:
        return {"label": "bottom_sheet/sheet", "conf": 0.50,
                "key_text": "", "components": ["sheet_handle", "sheet_overlay"], "state": ""}

    # --- Keyboard: bottom 45% is a large uniform light block ---
    kb_region = gray[int(h * 0.55):, :]
    kb_row_stds = np.array([np.std(kb_region[r]) for r in range(0, kb_region.shape[0], 4)])
    if np.mean(kb_row_stds) < 22 and np.mean(kb_region) > 175:
        state = "keyboard_open"

    # --- Tab bar: bottom ~10% has structural variance (icons), not solid ---
    tab_strip = gray[int(h * 0.88):, :]
    if tab_strip.shape[0] > 10 and np.std(tab_strip) > 18:
        components.append("bottom_tab_bar")
        label = "home"
        conf = 0.40

    # --- No tab bar, content starts high → likely onboarding ---
    if "bottom_tab_bar" not in components:
        content_top = gray[int(h * 0.10):int(h * 0.30), :]
        if np.std(content_top) > 40:
            label = "onboarding"
            conf = 0.30

    if np.mean(gray) < 100:
        components.append("dark_bg")

    return {"label": label, "conf": conf, "key_text": "", "components": components, "state": state}


def classify_frames(frame_paths: list[str], api_key: str, log_fn=None) -> list[dict]:
    client = genai.Client(api_key=api_key)
    results = []
    batch_size = GRID_COLS * GRID_COLS
    _log = log_fn or print

    for batch_start in range(0, len(frame_paths), batch_size):
        batch = frame_paths[batch_start: batch_start + batch_size]
        grid_image = _make_grid(batch, offset=batch_start)
        items = _ask_gemini_with_fallback(client, grid_image, batch, batch_start, _log)
        results.extend(items)

    return results


def _make_grid(paths: list[str], offset: int = 0) -> Image.Image:
    thumbs = []
    for i, p in enumerate(paths):
        img = Image.open(p).convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        canvas = Image.new("RGB", THUMB_SIZE, (30, 30, 30))
        off = ((THUMB_SIZE[0] - img.width) // 2, (THUMB_SIZE[1] - img.height) // 2)
        canvas.paste(img, off)

        draw = ImageDraw.Draw(canvas)
        badge = str(offset + i + 1)
        draw.rectangle([6, 6, 36, 26], fill=(0, 0, 0))
        draw.text((10, 8), badge, fill=(255, 255, 255))

        thumbs.append(canvas)

    cols = min(GRID_COLS, len(thumbs))
    rows = math.ceil(len(thumbs) / cols)
    grid = Image.new("RGB", (cols * THUMB_SIZE[0], rows * THUMB_SIZE[1]), (10, 10, 10))
    for idx, thumb in enumerate(thumbs):
        row, col = divmod(idx, cols)
        grid.paste(thumb, (col * THUMB_SIZE[0], row * THUMB_SIZE[1]))

    return grid


def _ask_gemini_with_fallback(client: genai.Client, grid_image: Image.Image, batch: list[str], offset: int, log_fn=print) -> list[dict]:
    count = len(batch)
    for model in MODELS:
        try:
            result = _ask_gemini(client, grid_image, count, offset, model)
            log_fn(f"[AI] batch {offset+1}-{offset+count}: {model} ok")
            return result
        except Exception as e:
            err = str(e)
            if any(x in err for x in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "404", "NOT_FOUND")):
                log_fn(f"[AI] {model} failed: {err[:120]}")
            else:
                raise
    log_fn("[AI] ALL MODELS FAILED — using local heuristic fallback")
    results = []
    for path in batch:
        r = _classify_local_fallback(path)
        log_fn(f"[LOCAL] {r['label']} ({r['conf']:.2f})")
        results.append(r)
    return results


def _ask_gemini(client: genai.Client, grid_image: Image.Image, count: int, offset: int, model: str = MODELS[0]) -> list[dict]:
    buf = io.BytesIO()
    grid_image.save(buf, format="JPEG", quality=85)

    prompt = PROMPT.format(
        start=offset + 1,
        end=offset + count,
        count=count,
        threshold=CONFIDENCE_THRESHOLD,
    )

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
            prompt,
        ],
    )
    text = response.text.strip()

    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            items = json.loads(match.group())
            if len(items) == count:
                parsed = []
                for item in items:
                    conf = float(item.get("conf", 1.0))
                    parsed.append({
                        "label": "unsorted" if conf < CONFIDENCE_THRESHOLD else item.get("label", "unsorted"),
                        "conf": conf,
                        "key_text": item.get("key_text", ""),
                        "components": item.get("components", []),
                        "state": item.get("state", ""),
                    })
                return parsed
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

    return [{"label": "unsorted", "conf": 0.0, "key_text": "", "components": [], "state": ""}] * count
