import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np
from google import genai
from google.genai import types
from PIL import Image

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

_ENV_MODEL = os.environ.get("GEMINI_MODEL", "").strip()

_DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite-preview",
]

# Env override prepended; duplicates removed while preserving order.
MODELS = list(dict.fromkeys([m for m in [_ENV_MODEL, *_DEFAULT_MODELS] if m]))

INPUT_STRATEGY = "single_image_per_request"
MAX_IMAGE_BYTES = 18 * 1024 * 1024  # keep well under Gemini's inline-bytes ceiling
SAFETY_RESIZE_MAX = (1290, 2796)    # only used if a frame exceeds MAX_IMAGE_BYTES
SAFETY_JPEG_QUALITY = 95
LOW_CONFIDENCE_THRESHOLD = 0.65
DEBUG_RAW_SNIPPET_MAX = 4000        # don't bloat the debug file with huge responses

_LEGACY_REQUIRED = ("label", "conf", "key_text", "components", "state")


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

PROMPT = """You are a senior mobile UX analyst inspecting ONE screenshot from a real mobile app screen recording.
The app can be from ANY modern mobile category: utility, productivity, AI chat/image/video/audio tools, ecommerce, marketplace, food delivery, ride hailing, fintech, banking, crypto, social, messenger, dating, streaming/media, education, health/fitness, travel/booking, creator tools, games.

You are looking at ONE screenshot. Do NOT invent context from other screens. Classify only what is visible.

── STEP 1: READ EVERY VISIBLE UI ELEMENT (silent pass) ─────────────────────────
Before classifying, mentally read EVERY text element on the screen:
• Headline, subtitle, section headers
• ALL list rows, all tab labels (active AND inactive), all button/CTA labels
• Prices, billing periods, trial terms, discount badges, "Best Value" stickers
• Selected plan indicators, promo codes, urgency timers
• Settings rows, toggle labels, permission text, legal/copyright lines
• Alert titles + body, empty-state text, error messages, success messages
• Chat input placeholders, search placeholders, form labels
• Badges, counters, dates, times, locations, ratings
• OS bars: status bar, home indicator (these are NOT app content)

Pay special attention to small text. The point is to read what the user is reading.

── STEP 2: TRANSITION VALIDATION — RUN BEFORE ANY UI PARSING ──────────────────
Check six flags in order. At the first TRUE flag, output transition immediately.

  FLAG 0 — Frame is unreadable (blur / artifacts / ghosting):
    Majority of frame is motion-blurred, focus-blurred, compression-artifacted,
    or two frames are ghosted together. If you cannot read the headline OR cannot
    identify the primary UI component → transition.

  FLAG 1 — Global scaling / edge margins:
    The app does NOT occupy 100% of the canvas. Negative space, solid color, OS
    wallpaper, or blur is visible at the left/right/bottom edge — the app is
    scaling down into a card. → transition.

  FLAG 2 — OS-applied corner radius:
    Outer app corners are visibly rounded with large radii that disconnect the UI
    from the physical canvas corners. → transition.

  FLAG 3 — Semantic duplication on X-axis:
    A singleton element (nav bar, tab bar, FAB, search bar, title) appears at TWO
    different horizontal positions simultaneously. → transition.

  FLAG 4 — Bounding box clipped by internal vertical seam:
    A button/label/card is truncated not at the screen edge but by a sharp internal
    vertical line. → transition.

  FLAG 5 — Vertical strip of OS background or blur:
    A vertical column of any width shows OS wallpaper / blur / solid color cleanly
    separating two layout regions. → transition.

If any flag is true: output transition with conf 0.95, set is_transition=true, set
key_text/components/state empty, do NOT parse content.

Topmost overlay rule (applies when no transition flag fires):
• If a bottom_sheet, action_sheet, or alert overlay is present, classify the OVERLAY,
  ignore the dimmed app behind. If two overlays are stacked, classify the topmost.
• OS notification shade / Control Center → system_tray (ignore app behind).
• Multiple scaled app cards fully formed on OS background → app_switcher.

── STEP 3: CLASSIFY (screen_type vocabulary) ──────────────────────────────────
Pick the SINGLE best match. Use feature-level naming "<type>/<feature>" wherever a
clear feature is visible (e.g. "paywall/annual", "home/recordings", "ai_chat/new_chat").

Vocabulary:
splash, onboarding, auth, login, signup, sign_in_with_apple_google, otp_verification,
profile_setup, personalization_quiz, survey, permission, notification_prompt,
home, dashboard, feed, search, search_results, product_detail, cart, checkout, payment,
order_tracking, map, chat, ai_chat, ai_generation_loading, ai_result, editor,
camera_capture, scanner, recorder, library, files, settings, account, profile,
subscription_management, paywall, special_offer, trial_offer, rating_prompt, alert,
bottom_sheet, action_sheet, webview, empty_state, error, success, loading, transition,
system_ui, system_tray, app_switcher, keyboard_state, home_screen, unsorted.

Key disambiguation rules (apply in order; first hit wins):
• HARD PRICE OVERRIDE — paywall: if ANY standalone price ($ / € / £ + billing period,
  or "per month/week/year", "lifetime", "annual", "monthly", "yearly") is the dominant
  conversion element → paywall. Plan switchers (segmented_control / tab_bar_pill with
  Monthly | Annual | Lifetime) are paywall UI, not home navigation. Hero illustrations,
  feature lists, social proof do NOT override a visible price.
  Exception: if the dominant UI is a working media player or a working content list and
  the price appears ONLY as a small locked-feature banner / inline upgrade row, classify
  by dominant UI (e.g. home/player), NOT paywall.
• special_offer takes priority over paywall when a discount badge / "% OFF" / crossed-out
  price / countdown is the dominant signal (downsell after paywall dismiss).
• onboarding: full-screen feature slide with NO price text, NO subscription terms, NO
  bottom tab bar. Hero illustrations and progress dots alone do NOT make a paywall.
• auth/login/signup: form-driven with email, password, social sign-in buttons, "Continue
  with Apple/Google/Phone". otp_verification: 4–6 digit code input.
• permission: any OS-system permission dialog OR a pre-permission card explaining why.
  notification_prompt is a sub-case for notifications specifically.
• ai_chat: conversation thread + message composer. ai_generation_loading: progress UI
  for image/video/audio generation. ai_result: generated artifact + share/save/edit.
• checkout/payment: visible totals, payment method picker, Apple/Google Pay buttons,
  shipping info, "Place order". cart: line items + quantity.
• map: a visible map canvas dominates. order_tracking: status timeline + map/ETA.
• editor / camera_capture / scanner / recorder: creation tools dominate the canvas.
• system_tray: notification shade / Control Center pulled down — classify ONLY this.
• app_switcher: multiple scaled app cards floating on OS background.
• home_screen: OS launcher (icon grid + dock) — NOT inside any app.
• unsorted is LAST RESORT (black frame, lock screen, camera pointed away). For any
  mobile-app UI, always pick the closest match and lower conf if unsure.

Feature naming examples (snake_case from the dominant title/active tab):
• paywall: annual, monthly, lifetime, weekly, trial_offer
• special_offer: 50_off, one_time, limited_time, black_friday
• onboarding: call_recording, ai_translation, find_friends, scan_anything
• home: recordings, folders, feed, for_you, library, chats
• bottom_sheet: create_folder, share_recording, choose_destination, sort_by
• action_sheet: delete_recording, share, sort_by
• alert: delete_recording, clear_all, storage_full, network_error
• settings: notifications, privacy, account, storage, language
• ai_chat: new_chat, conversation, prompt_suggestions
• search: empty, results, no_results
• checkout: shipping, payment, review
• map: home, route, order_tracking

── STEP 4: LIST FOREGROUND COMPONENTS ─────────────────────────────────────────
Choose visible foreground components. Vocabulary (extend with sensible additions if a
clearly visible element is not listed — keep names snake_case):
hero_illustration, hero_video, app_mockup, progress_dots, progress_bar, step_counter,
cta_full_width, cta_pill, cta_outlined, skip_button,
gradient_bg, dark_bg, image_bg,
price_cards_single, price_cards_dual, price_cards_triple,
free_trial_badge, lifetime_offer, urgency_timer,
feature_checklist, comparison_table, social_proof, guarantee_badge,
close_button, restore_link,
bottom_tab_bar, top_tab_bar, list_view, toggle_rows, search_bar, card_grid,
segmented_control, tab_bar_pill,
notification_shade, quick_settings_tiles, notification_cards,
app_cards_stack, dark_background,
sheet_handle, sheet_overlay, action_buttons_row, drag_indicator,
list_item_checked, list_item_radio, list_item_default,
player_timeline, slider,
alert_dialog_box, action_sheet_cancel, destructive_action_row,
discount_badge, original_price_crossed,
text_field, password_field, otp_input, phone_input,
chat_message_bubble, chat_input, prompt_card, suggestion_chip,
order_summary, payment_method_picker, apple_pay_button, google_pay_button,
map_canvas, route_line, location_pin,
camera_viewfinder, capture_shutter, gallery_thumb, scan_frame,
empty_state_illustration, error_banner, success_banner, loading_spinner

THREE-WAY DISTINCTION — bottom_sheet vs action_sheet vs alert:
• bottom_sheet — anchored at bottom, drag handle visible, tall (≥40%), structured
  content (lists, forms, pickers, inputs).
• action_sheet — anchored at bottom, NO drag handle, short (≤35%), text-only action
  rows. iOS: isolated Cancel below the group.
• alert — centered modal, narrower than screen, does NOT touch bottom edge.
  Title + body + 1–3 buttons.

THREE-WAY DISTINCTION — progress_bar vs slider vs player_timeline:
• progress_bar — no thumb, static fill, no time labels.
• slider — has draggable thumb, no timestamps, used in settings.
• player_timeline — draggable thumb on waveform/track WITH elapsed+total time labels
  and play/pause nearby.

── STEP 5: INTERACTION STATE ──────────────────────────────────────────────────
Choose at most one from: keyboard_open, item_selected, multi_select, modal_open,
menu_open, empty_state, error_state, scrolled, loading_content. Otherwise use "".
Same screen type with different state = a distinct screen — do NOT collapse them.

── STEP 6: OUTPUT (strict JSON) ───────────────────────────────────────────────
Return ONE JSON object with this exact shape and NO other text — no prose, no markdown
fences, no ```json wrapper:

{
  "frames": [
    {
      "frame_index": 1,
      "label": "<type>" or "<type>/<feature>",
      "screen_type": "<type from the STEP 3 vocabulary>",
      "conf": 0.0-1.0,
      "key_text": "exact short quote of the dominant foreground text",
      "all_visible_text": "every readable text element joined with ' · '",
      "components": ["component1", "component2"],
      "state": "<one state from STEP 5 or ''>",
      "is_transition": true|false,
      "is_scroll_state": true|false,
      "same_screen_as_previous": false,
      "needs_review": true|false,
      "review_reason": "short reason if needs_review is true, else ''",
      "uncertainty_reason": "short reason if you are unsure, else ''"
    }
  ]
}

key_text rules:
• Most prominent foreground text (sheet title > dialog title > headline > active tab > price).
• bottom_sheet with no title → join ALL row labels with " · ".
• action_sheet → join all action labels (exclude Cancel) with " · ".
• alert → the dialog title only.

all_visible_text rules:
• Concatenate every visible text element you read in STEP 1 separated by " · ".
• Keep it concise (≤ 300 chars) — drop duplicates and OS bars.
• Include ALL prices, trial terms, and CTA labels verbatim.

conf rules:
• If unsure of the type, still pick the best fit and LOWER the conf. Never default to
  unsorted for a frame that shows real app UI. The system flags low conf for review.
"""


# -----------------------------------------------------------------------------
# Validation rules
# -----------------------------------------------------------------------------

_PAYWALL_TYPES = {"paywall", "special_offer", "trial_offer", "subscription"}
_PAYWALL_KEYWORDS = (
    "price", "$", "€", "£", "trial", "free trial", "subscribe", "subscription",
    "premium", "unlock", "continue", "yearly", "monthly", "weekly", "week",
    "month", "year", "/year", "/month", "/week", "per year", "per month",
    "per week", "lifetime", "annual", "% off", "save",
)
_ONBOARDING_PRICE_LEAK_KEYWORDS = (
    "$", "€", "£", "/year", "/month", "/week", "per year", "per month",
    "free trial", "subscribe", "subscription", "premium",
)
_CHECKOUT_KEYWORDS = (
    "cart", "checkout", "payment", "order", "card", "apple pay", "google pay",
    "shipping", "delivery", "subtotal", "total", "place order", "billing",
)
_AUTH_KEYWORDS = (
    "sign in", "sign up", "log in", "login", "email", "password",
    "continue with", "apple", "google", "phone", "code", "verify",
)
_PERMISSION_KEYWORDS = (
    "allow", "permission", "notifications", "camera", "microphone", "photos",
    "location", "tracking", "bluetooth", "contacts", "calendar",
)


def _has_any(text: str, keywords) -> bool:
    if not text:
        return False
    needle = text.lower()
    return any(k.lower() in needle for k in keywords)


def validate_classifications(items: list[dict], log_fn=None) -> None:
    """Annotate items with needs_review / review_reason / validation_warnings.

    Never changes the label or conf — only marks suspicious items so a human can
    inspect them later. Operates in place.
    """
    log = log_fn or print
    flagged = 0

    for item in items:
        warnings: list[str] = []
        label = (item.get("label") or "").lower()
        screen_type = (item.get("screen_type") or "").lower()
        top_type = screen_type or label.split("/", 1)[0]
        key_text = item.get("key_text") or ""
        all_text = item.get("all_visible_text") or ""
        combined = f"{key_text}\n{all_text}"
        conf = float(item.get("conf", 0.0))

        # 1. Paywall claim without pricing language
        if top_type in _PAYWALL_TYPES and not _has_any(combined, _PAYWALL_KEYWORDS):
            warnings.append("paywall_without_pricing_text")

        # 2. Onboarding screen showing pricing/subscription text
        if top_type == "onboarding" and _has_any(combined, _ONBOARDING_PRICE_LEAK_KEYWORDS):
            warnings.append("onboarding_with_pricing_text")

        # 3. Transition with readable content
        if (top_type == "transition" or item.get("is_transition")) and \
           len(all_text.strip()) > 24 and len(key_text.strip()) > 2:
            warnings.append("transition_with_content")

        # 4. Empty text at high confidence
        if conf >= LOW_CONFIDENCE_THRESHOLD and not key_text.strip() and not all_text.strip() \
           and top_type not in ("transition", "system_tray", "app_switcher", "home_screen", "loading", "splash"):
            warnings.append("empty_text_at_high_conf")

        # 5. Low confidence
        if conf < LOW_CONFIDENCE_THRESHOLD:
            warnings.append("low_confidence")

        # 6. Checkout / payment / cart without relevant words
        if top_type in {"checkout", "payment", "cart"} and not _has_any(combined, _CHECKOUT_KEYWORDS):
            warnings.append("checkout_without_checkout_text")

        # 7. Auth / login / signup without relevant words
        if top_type in {"auth", "login", "signup", "otp_verification",
                        "sign_in_with_apple_google"} and not _has_any(combined, _AUTH_KEYWORDS):
            warnings.append("auth_without_auth_text")

        # 8. Permission without relevant words
        if top_type in {"permission", "notification_prompt"} and not _has_any(combined, _PERMISSION_KEYWORDS):
            warnings.append("permission_without_permission_text")

        if warnings:
            flagged += 1
            item["needs_review"] = True
            existing = item.get("review_reason") or ""
            reasons = [r for r in (existing, *warnings) if r]
            item["review_reason"] = "; ".join(dict.fromkeys(reasons))
            existing_warnings = item.get("validation_warnings") or []
            seen = set(existing_warnings)
            for w in warnings:
                if w not in seen:
                    existing_warnings.append(w)
                    seen.add(w)
            item["validation_warnings"] = existing_warnings

    if flagged:
        log(f"Gemini validation: {flagged} frames marked needs_review")


# -----------------------------------------------------------------------------
# JSON parsing helpers
# -----------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_text(text: str) -> str:
    """Best-effort isolation of a JSON document from a raw Gemini response.

    Tolerates: code fences, leading/trailing prose, and bare JSON. Returns the
    inner JSON text if a balanced object or array can be located, else returns
    the original text so the parser can still fail cleanly.
    """
    if not text:
        return ""
    s = text.strip()
    fence = _FENCE_RE.search(s)
    if fence:
        s = fence.group(1).strip()

    obj_start = s.find("{")
    obj_end = s.rfind("}")
    arr_start = s.find("[")
    arr_end = s.rfind("]")

    candidates: list[tuple[int, int]] = []
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        candidates.append((obj_start, obj_end + 1))
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        candidates.append((arr_start, arr_end + 1))

    if not candidates:
        return s
    start, end = min(candidates, key=lambda se: se[0])
    return s[start:end]


def safe_parse_json(text: str) -> Any:
    """Try to parse JSON from a possibly-noisy response. Returns None on failure."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    extracted = extract_json_text(text)
    if extracted == text:
        return None
    try:
        return json.loads(extracted)
    except (json.JSONDecodeError, ValueError):
        return None


def _coerce_components(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return [str(value)]


def _coerce_conf(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _fallback_item(index: int, reason: str = "Gemini response could not be parsed or was incomplete") -> dict:
    return {
        "label": "unsorted",
        "screen_type": "unsorted",
        "conf": 0.0,
        "key_text": "",
        "all_visible_text": "",
        "components": [],
        "state": "",
        "is_transition": False,
        "is_scroll_state": False,
        "same_screen_as_previous": False,
        "needs_review": True,
        "review_reason": "classification_fallback",
        "uncertainty_reason": reason,
        "frame_index": index + 1,
    }


def normalize_classification_item(item: Any, index: int) -> dict:
    """Coerce a raw Gemini item into the canonical classification shape.

    Always returns a dict with every legacy field present and typed correctly.
    Optional fields are preserved when provided. Missing items return a
    fallback classification with needs_review=True.
    """
    if not isinstance(item, dict):
        return _fallback_item(index, reason=f"item at index {index} was not an object")

    label = item.get("label") or item.get("screen_type") or "unsorted"
    screen_type = item.get("screen_type") or (str(label).split("/", 1)[0] if label else "unsorted")

    normalized = {
        "label": str(label),
        "screen_type": str(screen_type),
        "conf": _coerce_conf(item.get("conf", 0.0)),
        "key_text": str(item.get("key_text", "") or ""),
        "all_visible_text": str(item.get("all_visible_text", "") or ""),
        "components": _coerce_components(item.get("components")),
        "state": str(item.get("state", "") or ""),
        "is_transition": _coerce_bool(item.get("is_transition", False)),
        "is_scroll_state": _coerce_bool(item.get("is_scroll_state", False)),
        "same_screen_as_previous": _coerce_bool(item.get("same_screen_as_previous", False)),
        "needs_review": _coerce_bool(item.get("needs_review", False)),
        "review_reason": str(item.get("review_reason", "") or ""),
        "uncertainty_reason": str(item.get("uncertainty_reason", "") or ""),
    }

    # Preserve frame_index if Gemini returned one; else derive from position.
    raw_frame_index = item.get("frame_index")
    try:
        normalized["frame_index"] = int(raw_frame_index) if raw_frame_index is not None else index + 1
    except (TypeError, ValueError):
        normalized["frame_index"] = index + 1

    # If state is "scrolled", reflect it in is_scroll_state for redundancy-safe consumers.
    if normalized["state"] == "scrolled":
        normalized["is_scroll_state"] = True

    return normalized


def normalize_classification_response(raw: Any, expected_count: int) -> list[dict]:
    """Coerce a raw parsed response into exactly expected_count classification dicts.

    Accepted shapes: list, {"frames": [...]}, {"screens": [...]}, {"classifications":
    [...]}, or a single object treated as the only item. Missing items get fallback
    classifications, extra items are dropped.
    """
    items: list[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("frames", "screens", "classifications", "items"):
            if isinstance(raw.get(key), list):
                items = raw[key]
                break
        else:
            # Bare single-frame object
            items = [raw]
    else:
        items = []

    normalized: list[dict] = []
    for i in range(expected_count):
        if i < len(items):
            normalized.append(normalize_classification_item(items[i], i))
        else:
            normalized.append(_fallback_item(i, reason="missing item from Gemini response"))
    return normalized


# -----------------------------------------------------------------------------
# Local fallback (kept for all-models-failed case)
# -----------------------------------------------------------------------------

def _classify_local_fallback(frame_path: str) -> dict:
    """Pixel-only heuristic classifier. Runs when all Gemini models fail."""
    img = cv2.imread(frame_path)
    if img is None:
        return _fallback_item(0, reason="cv2 could not read frame for local heuristic")

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    label = "home"
    conf = 0.25
    components: list[str] = []
    state = ""

    if np.mean(gray) < 20:
        return {**_fallback_item(0, reason="all_models_failed_dark_frame"), "label": "unsorted", "conf": 0.1}

    if np.std(gray) < 35:
        out = _fallback_item(0, reason="all_models_failed_local_heuristic")
        out.update({"label": "loading", "screen_type": "loading", "conf": 0.55})
        return out

    top_mean = np.mean(gray[:int(h * 0.30), :])
    bottom_mean = np.mean(gray[int(h * 0.40):, :])
    if bottom_mean > 210 and top_mean < 90:
        out = _fallback_item(0, reason="all_models_failed_local_heuristic")
        out.update({
            "label": "bottom_sheet/sheet",
            "screen_type": "bottom_sheet",
            "conf": 0.50,
            "components": ["sheet_handle", "sheet_overlay"],
        })
        return out

    kb_region = gray[int(h * 0.55):, :]
    kb_row_stds = np.array([np.std(kb_region[r]) for r in range(0, kb_region.shape[0], 4)])
    if np.mean(kb_row_stds) < 22 and np.mean(kb_region) > 175:
        state = "keyboard_open"

    tab_strip = gray[int(h * 0.88):, :]
    if tab_strip.shape[0] > 10 and np.std(tab_strip) > 18:
        components.append("bottom_tab_bar")
        label = "home"
        conf = 0.40

    if "bottom_tab_bar" not in components:
        content_top = gray[int(h * 0.10):int(h * 0.30), :]
        if np.std(content_top) > 40:
            label = "onboarding"
            conf = 0.30

    if np.mean(gray) < 100:
        components.append("dark_bg")

    out = _fallback_item(0, reason="all_models_failed_local_heuristic")
    out.update({
        "label": label,
        "screen_type": label.split("/", 1)[0],
        "conf": conf,
        "components": components,
        "state": state,
    })
    return out


# -----------------------------------------------------------------------------
# Image preparation
# -----------------------------------------------------------------------------

def _load_image_bytes(path: str) -> tuple[bytes, str]:
    """Return (data, mime_type) for the frame, re-encoding only if needed.

    Default: send the original JPEG bytes from ffmpeg (q:v=2, high quality) with
    no re-encoding. If the file exceeds MAX_IMAGE_BYTES, downscale once to fit
    SAFETY_RESIZE_MAX and re-encode at JPEG quality 95.
    """
    size = os.path.getsize(path)
    if size <= MAX_IMAGE_BYTES:
        with open(path, "rb") as f:
            return f.read(), "image/jpeg"

    img = Image.open(path).convert("RGB")
    img.thumbnail(SAFETY_RESIZE_MAX, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=SAFETY_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "image/jpeg"


# -----------------------------------------------------------------------------
# Gemini call (single image per request)
# -----------------------------------------------------------------------------

def _ask_gemini_with_fallback(
    client: genai.Client,
    image_data: bytes,
    mime_type: str,
    frame_index: int,
    log_fn,
) -> tuple[dict, list[dict]]:
    """Call Gemini for one image. Returns (debug_info, [error_records]).

    debug_info shape:
      {
        "model_used": str | None,
        "raw_text": str (snippet),
        "parsed": Any | None,   # safe_parse_json output
        "fallback_used": bool,
      }
    """
    attempts: list[dict] = []
    for model in MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=image_data, mime_type=mime_type),
                    PROMPT,
                ],
            )
            text = (response.text or "").strip()
            snippet = text[:DEBUG_RAW_SNIPPET_MAX]
            parsed = safe_parse_json(text)
            log_fn(f"[AI] frame {frame_index}: {model} ok")
            return (
                {"model_used": model, "raw_text": snippet, "parsed": parsed, "fallback_used": False},
                attempts,
            )
        except Exception as e:
            err = str(e)
            attempts.append({"model": model, "error": err[:400]})
            if any(x in err for x in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "404", "NOT_FOUND")):
                log_fn(f"[AI] {model} failed: {err[:120]}")
                continue
            # Unexpected error — fall through to next model rather than crash the whole job.
            log_fn(f"[AI] {model} unexpected error: {err[:120]}")
            continue

    log_fn(f"[AI] ALL MODELS FAILED frame {frame_index} — using local heuristic fallback")
    return (
        {"model_used": None, "raw_text": "", "parsed": None, "fallback_used": True},
        attempts,
    )


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

def classify_frames(
    frame_paths: list[str],
    api_key: str,
    log_fn=None,
    debug_dir: str | None = None,
) -> list[dict]:
    log = log_fn or print
    if not frame_paths:
        return []

    client = genai.Client(api_key=api_key)

    log(f"Gemini classification: {len(frame_paths)} frames, "
        f"strategy={INPUT_STRATEGY}, requests={len(frame_paths)}")

    request_count = 0
    parsed_ok = 0
    fallback_count = 0
    normalized_all: list[dict] = []
    per_frame_debug: list[dict] = []
    errors: list[dict] = []
    models_used: list[str] = []

    for index, path in enumerate(frame_paths):
        try:
            image_data, mime_type = _load_image_bytes(path)
        except Exception as e:
            errors.append({"frame_index": index + 1, "stage": "load_image", "error": str(e)[:300]})
            normalized_all.append(_fallback_item(index, reason=f"could not load image: {e}"))
            per_frame_debug.append({
                "frame_index": index + 1,
                "frame_path_basename": os.path.basename(path),
                "model_used": None,
                "raw_response_snippet": "",
                "parsed_item": None,
                "normalized_item": normalized_all[-1],
                "fallback_used": True,
            })
            continue

        debug_info, attempts = _ask_gemini_with_fallback(client, image_data, mime_type, index + 1, log)
        request_count += 1
        if debug_info["model_used"]:
            models_used.append(debug_info["model_used"])
        if attempts:
            errors.extend({"frame_index": index + 1, **a} for a in attempts)

        if debug_info["fallback_used"]:
            fallback_count += 1
            heuristic = _classify_local_fallback(path)
            heuristic["frame_index"] = index + 1
            normalized = normalize_classification_item(heuristic, index)
            parsed_item = None
        else:
            parsed = debug_info["parsed"]
            if parsed is None:
                normalized = _fallback_item(index, reason="JSON parse failed")
            else:
                norm_list = normalize_classification_response(parsed, expected_count=1)
                normalized = norm_list[0]
                # Use position-derived index over what Gemini wrote, to stay aligned with our list.
                normalized["frame_index"] = index + 1
                parsed_ok += 1
            parsed_item = parsed

        normalized_all.append(normalized)
        per_frame_debug.append({
            "frame_index": index + 1,
            "frame_path_basename": os.path.basename(path),
            "model_used": debug_info["model_used"],
            "raw_response_snippet": debug_info["raw_text"],
            "parsed_item": parsed_item,
            "normalized_item": normalized,
            "fallback_used": debug_info["fallback_used"],
        })

    log(f"Gemini parsed {parsed_ok}/{len(frame_paths)} classifications")
    if fallback_count:
        log(f"Gemini fallback: {fallback_count} frames used local heuristic")

    validate_classifications(normalized_all, log_fn=log)

    if debug_dir:
        _write_debug_file(
            debug_dir=debug_dir,
            frame_count=len(frame_paths),
            request_count=request_count,
            parsed_ok=parsed_ok,
            fallback_count=fallback_count,
            models_used=models_used,
            per_frame=per_frame_debug,
            errors=errors,
        )

    return normalized_all


# -----------------------------------------------------------------------------
# Debug writer
# -----------------------------------------------------------------------------

def _write_debug_file(
    *,
    debug_dir: str,
    frame_count: int,
    request_count: int,
    parsed_ok: int,
    fallback_count: int,
    models_used: list[str],
    per_frame: list[dict],
    errors: list[dict],
) -> None:
    os.makedirs(debug_dir, exist_ok=True)
    needs_review_count = sum(1 for f in per_frame if f["normalized_item"].get("needs_review"))
    validation_warnings = {
        f["frame_index"]: f["normalized_item"].get("validation_warnings", [])
        for f in per_frame
        if f["normalized_item"].get("validation_warnings")
    }
    debug = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "gemini",
        "input_strategy": INPUT_STRATEGY,
        "frame_count": frame_count,
        "request_count": request_count,
        "parsed_ok": parsed_ok,
        "fallback_used_count": fallback_count,
        "needs_review_count": needs_review_count,
        "models_available": MODELS,
        "models_used": models_used,
        "frames": per_frame,
        "validation_warnings_by_frame": validation_warnings,
        "errors": errors,
    }
    path = os.path.join(debug_dir, "classification_debug.json")
    with open(path, "w") as f:
        json.dump(debug, f, indent=2)
