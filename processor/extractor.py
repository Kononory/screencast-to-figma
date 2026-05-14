import os
import glob
import ffmpeg
import imagehash
import cv2
import numpy as np
from PIL import Image

from processor.motion import (
    MotionZone,
    MotionZoneConfig,
    SCOUT_FPS_DEFAULT,
    dense_fps_for_zone,
    detect_motion_zones,
    index_in_any_zone,
)

STATUS_BAR_CROP = 0.08  # top 8% is OS status bar — ignored for dedup (clock ticks)


def extract_frames(video_path: str, output_dir: str, consec_threshold: int = 3, global_threshold: int = 3) -> list[str]:
    frames_dir = os.path.join(output_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    (
        ffmpeg
        .input(video_path)
        .filter("fps", fps=4)
        .output(
            os.path.join(frames_dir, "frame_%04d.jpg"),
            **{"q:v": 2}
        )
        .overwrite_output()
        .run(quiet=True)
    )

    paths = sorted(glob.glob(os.path.join(frames_dir, "frame_*.jpg")))

    # Full-image hashes for stability filter (needs whole frame including status bar)
    full_hashes = [imagehash.phash(Image.open(p)) for p in paths]

    # Pass 1: remove mid-transition frames
    _dump_diffs(paths, full_hashes)
    stable = _stability_filter(paths, full_hashes, diff_threshold=18)

    # Pass 2: trim trailing motion (swipe-to-home, tray open, etc. at end of recording)
    stable = _trim_trailing_motion(stable, full_hashes, motion_threshold=15)

    # Content hashes with status bar cropped — used for dedup only, full images kept on disk
    content_hashes = {p: _content_hash(p) for p in stable}

    # Pass 3: remove consecutive near-identical frames (held screen)
    after_consec = _dedup_consecutive(stable, content_hashes, dup_threshold=consec_threshold)

    # Pass 4: remove revisited identical screens from anywhere in the video
    return _dedup_global(after_consec, content_hashes, dup_threshold=global_threshold)


# -----------------------------------------------------------------------------
# Adaptive extraction (Session 20)
# -----------------------------------------------------------------------------

def _scout_extract(
    video_path: str,
    output_dir: str,
    fps: int,
) -> tuple[list[str], list]:
    """First pass: cheap fps=N extraction. Returns (paths, phashes)."""
    scout_dir = os.path.join(output_dir, "frames")
    os.makedirs(scout_dir, exist_ok=True)
    (
        ffmpeg
        .input(video_path)
        .filter("fps", fps=fps)
        .output(
            os.path.join(scout_dir, "frame_%04d.jpg"),
            **{"q:v": 2}
        )
        .overwrite_output()
        .run(quiet=True)
    )
    paths = sorted(glob.glob(os.path.join(scout_dir, "frame_*.jpg")))
    hashes = [imagehash.phash(Image.open(p)) for p in paths]
    return paths, hashes


def _dense_extract_zone(
    video_path: str,
    output_dir: str,
    zone: MotionZone,
    dense_fps: int,
) -> tuple[list[str], dict[str, int]]:
    """Second pass: re-extract a single motion zone at high fps.

    Uses input-side -ss for fast seek (imprecise on non-keyframes, ~50-200ms
    drift for screen recordings — acceptable since we expand zones by ±1 scout
    frame on the detection side). Returns (paths, timestamp_map_ms).
    """
    if zone.start_ms is None or zone.end_ms is None:
        return [], {}
    dense_dir = os.path.join(output_dir, "frames_dense", zone.zone_id)
    os.makedirs(dense_dir, exist_ok=True)
    start_s = max(0, zone.start_ms) / 1000.0
    duration_s = max(0.05, (zone.end_ms - zone.start_ms) / 1000.0)
    out_pattern = os.path.join(dense_dir, "frame_%04d.jpg")
    (
        ffmpeg
        .input(video_path, ss=start_s)
        .filter("fps", fps=dense_fps)
        .output(out_pattern, t=duration_s, **{"q:v": 2})
        .overwrite_output()
        .run(quiet=True)
    )
    paths = sorted(glob.glob(os.path.join(dense_dir, "frame_*.jpg")))
    # Rename to a globally-unique scheme so segment/timeline/manifest paths never collide.
    renamed: list[str] = []
    ts_map: dict[str, int] = {}
    for i, p in enumerate(paths):
        new_name = f"frame_z{zone.zone_id.replace('motion_', '')}_{i + 1:04d}.jpg"
        new_path = os.path.join(dense_dir, new_name)
        if new_path != p:
            os.rename(p, new_path)
        renamed.append(new_path)
        ts_map[new_path] = int(zone.start_ms + (i * 1000) // max(dense_fps, 1))
    return renamed, ts_map


def _filter_scout_outside_zones(
    paths: list[str],
    hashes: list,
    zones: list[MotionZone],
    consec_threshold: int,
    global_threshold: int,
) -> list[str]:
    """Stability filter + dedup, applied ONLY to scout frames outside any zone.

    Scout frames inside zones are passed through unchanged; the dense pass
    covers those regions and the filters here would shred them at higher fps.
    """
    out_paths = list(paths)
    out_hashes = list(hashes)

    # Stability filter: skip in-zone indices.
    stable = []
    stable_hashes = []
    for i, path in enumerate(out_paths):
        in_zone = index_in_any_zone(i, zones)
        if in_zone:
            stable.append(path)
            stable_hashes.append(out_hashes[i])
            continue
        # Apply the existing filter rules. For simplicity reuse the original
        # algorithm by calling _stability_filter on the surrounding sub-window
        # would over-engineer it; instead inline the simplest rule here:
        # delete frames where prev_diff > 18 AND next_diff > 18 (the rest of
        # the heuristics live in the legacy path).
        prev_diff = abs(out_hashes[i] - out_hashes[i - 1]) if i > 0 else -1
        next_diff = abs(out_hashes[i] - out_hashes[i + 1]) if i + 1 < len(out_hashes) else -1
        if prev_diff > 18 and next_diff > 18:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        stable.append(path)
        stable_hashes.append(out_hashes[i])

    # Dedup consecutive on the *stable* list, again only between out-of-zone frames.
    # We keep zone frames untouched so the dense pass remains authoritative inside them.
    content = {p: _content_hash(p) for p in stable}
    deduped: list[str] = []
    last_h = None
    for path in stable:
        # Determine if this scout path is inside a zone (lookup by original index).
        try:
            orig_idx = out_paths.index(path)
        except ValueError:
            orig_idx = -1
        if index_in_any_zone(orig_idx, zones):
            deduped.append(path)
            last_h = None  # reset so the next out-of-zone frame is kept
            continue
        h = content[path]
        if last_h is None or abs(h - last_h) >= consec_threshold:
            deduped.append(path)
            last_h = h
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return deduped


def extract_frames_adaptive(
    video_path: str,
    output_dir: str,
    consec_threshold: int = 3,
    global_threshold: int = 3,
    motion_config: MotionZoneConfig | None = None,
    scout_fps: int = SCOUT_FPS_DEFAULT,
) -> tuple[list[str], dict[str, int], list[MotionZone]]:
    """Two-pass adaptive extraction.

    Returns:
        merged_paths: timestamp-ordered list of frame paths to feed downstream.
        timestamp_map: {path: ms} for every frame in merged_paths.
        zones: detected MotionZone objects (empty list if none).
    """
    if motion_config is None:
        motion_config = MotionZoneConfig()

    # Pass 1: scout.
    scout_paths, scout_hashes = _scout_extract(video_path, output_dir, fps=scout_fps)
    if not scout_paths:
        return [], {}, []

    # Detect zones.
    zones = detect_motion_zones(scout_hashes, fps=scout_fps, config=motion_config)

    # Pass 2: dense extraction per zone.
    dense_paths: list[str] = []
    dense_ts: dict[str, int] = {}
    for z in zones:
        try:
            dense_fps = dense_fps_for_zone(z, motion_config)
            paths, ts_map = _dense_extract_zone(video_path, output_dir, z, dense_fps)
            dense_paths.extend(paths)
            dense_ts.update(ts_map)
        except Exception:
            # Skip this zone; the scout pass still covers it at lower density.
            continue

    # Filter scout frames outside zones; preserve in-zone scout frames as-is.
    scout_kept = _filter_scout_outside_zones(
        scout_paths, scout_hashes, zones, consec_threshold, global_threshold
    )

    # Build a timestamp map for scout-kept frames (filename-derived).
    ts_map: dict[str, int] = {}
    for p in scout_kept:
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            n = int(name.split("_")[-1])
            ts_map[p] = ((n - 1) * 1000) // max(scout_fps, 1)
        except ValueError:
            pass
    ts_map.update(dense_ts)

    # Merge and sort by timestamp.
    merged = sorted(set(scout_kept) | set(dense_paths), key=lambda p: ts_map.get(p, 0))

    # Global dedup across the merged list using content hashes. Only collapses
    # truly-identical screens; in-zone frames with state changes survive.
    content = {p: _content_hash(p) for p in merged}
    seen = []
    final: list[str] = []
    for p in merged:
        h = content[p]
        if all(abs(h - s) >= global_threshold for s in seen):
            seen.append(h)
            final.append(p)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    return final, {p: ts_map[p] for p in final if p in ts_map}, zones


def _dump_diffs(paths, hashes):
    import os as _os
    if not paths:
        return
    log_path = "diffs_debug.txt"
    lines = []
    for i, p in enumerate(paths):
        prev_d = abs(hashes[i] - hashes[i-1]) if i > 0 else -1
        next_d = abs(hashes[i] - hashes[i+1]) if i < len(paths)-1 else -1
        lrsplit = None
        try:
            img = Image.open(p)
            w, h = img.size
            lrsplit = abs(imagehash.phash(img.crop((0,0,w//2,h))) - imagehash.phash(img.crop((w//2,0,w,h))))
        except Exception:
            pass
        lines.append(f"{i:04d} {_os.path.basename(p)} prev={prev_d:3} next={next_d:3} lr={lrsplit}")
    with open(log_path, "w") as f:
        f.write("\n".join(lines))


def _content_hash(path):
    img = Image.open(path)
    crop_top = max(60, int(img.height * STATUS_BAR_CROP))
    return imagehash.phash(img.crop((0, crop_top, img.width, img.height)))


def _is_horizontal_split(path, threshold=18):
    """Full-height seam test at 33%, 50%, 67% — catches obvious two-screen splits."""
    img = Image.open(path)
    w, h = img.size
    for x in [w // 3, w // 2, (2 * w) // 3]:
        if abs(imagehash.phash(img.crop((0, 0, x, h))) -
               imagehash.phash(img.crop((x, 0, w, h)))) > threshold:
            return True
    return False


def _is_slide_blend(paths, idx, match_threshold=8):
    """
    Catches same-app push transitions that fool _is_horizontal_split.

    At the 50% point of a push slide:
      frame[i] left half  ≈  frame[i-1] right half  (outgoing screen has slid left)
      frame[i] right half ≈  frame[i+1] left half   (incoming screen arrived from right)

    A stable held screen fails this: its left half does NOT match the
    previous frame's right half (they're different halves of the same design).
    """
    if idx == 0 or idx == len(paths) - 1:
        return False
    try:
        img = Image.open(paths[idx])
        w, h = img.size
        mid = w // 2

        curr_left = imagehash.phash(img.crop((0, 0, mid, h)))
        prev_right = imagehash.phash(Image.open(paths[idx - 1]).crop((mid, 0, w, h)))
        if abs(curr_left - prev_right) > match_threshold:
            return False

        curr_right = imagehash.phash(img.crop((mid, 0, w, h)))
        next_left = imagehash.phash(Image.open(paths[idx + 1]).crop((0, 0, mid, h)))
        return abs(curr_right - next_left) <= match_threshold
    except OSError:
        return False


def _has_edge_mismatch(paths, idx, edge_frac=0.06, threshold=12):
    """
    A stable held screen has the same left edge as the next frame.
    A transition frame has a sliver of the outgoing screen at the left edge
    that vanishes in the next frame — so left edges differ significantly.
    Covers both the 50/50 slide (outgoing screen's right portion at left edge)
    and the 90%+ nearly-complete slide (tiny sliver still visible at left edge).
    """
    if idx >= len(paths) - 1:
        return False
    try:
        img = Image.open(paths[idx])
        img_next = Image.open(paths[idx + 1])
    except OSError:
        return False

    w, h = img.size
    edge_w = max(20, int(w * edge_frac))

    curr_edge = imagehash.phash(img.crop((0, 0, edge_w, h)))
    next_edge = imagehash.phash(img_next.crop((0, 0, edge_w, h)))
    return abs(curr_edge - next_edge) > threshold



def _is_fade_blend(paths, idx, blend_threshold=0.12):
    """
    Detects cross-dissolve/fade frames where current ≈ alpha*prev + (1-alpha)*next.
    Push-slides have a sharp seam; fades blend the whole frame uniformly.
    Downsamples to 64x64 grayscale for speed.
    """
    if idx == 0 or idx >= len(paths) - 1:
        return False
    try:
        def load(p):
            return np.array(Image.open(p).convert("L").resize((64, 64), Image.LANCZOS), dtype=np.float32)
        prev = load(paths[idx - 1])
        curr = load(paths[idx])
        nxt  = load(paths[idx + 1])
        best = min(
            np.mean(np.abs(curr - (a * prev + (1 - a) * nxt)))
            for a in (0.3, 0.5, 0.7)
        )
        return best / 255.0 < blend_threshold
    except OSError:
        return False


def _stability_filter(paths, hashes, diff_threshold=18):
    stable = []
    for i, path in enumerate(paths):
        if i == 0:
            stable.append(path)
            continue

        prev_diff = abs(hashes[i] - hashes[i - 1])

        if i == len(paths) - 1:
            # Last frame: only one neighbor — remove if it looks like a horizontal transition
            if _is_horizontal_split(path):
                os.remove(path)
            else:
                stable.append(path)
            continue

        next_diff = abs(hashes[i] - hashes[i + 1])

        if prev_diff > diff_threshold and next_diff > diff_threshold:
            os.remove(path)
        elif min(prev_diff, next_diff) > 4 and _is_horizontal_split(path):
            # Both neighbors are at least slightly different (not a held stable screen or
            # a just-appeared first frame) AND halves look like two different screens
            os.remove(path)
        elif (prev_diff > 12 and next_diff <= 8
              and i + 2 < len(paths)
              and abs(hashes[i + 1] - hashes[i + 2]) > 12
              and _is_horizontal_split(path)):
            # First frame of a 2-frame consecutive transition: large-small-large diff pattern
            os.remove(path)
        elif (prev_diff <= 8 and next_diff > 12
              and i >= 2
              and abs(hashes[i - 2] - hashes[i - 1]) > 12
              and _is_horizontal_split(path)):
            # Second frame of a 2-frame consecutive transition: symmetric case
            os.remove(path)
        elif prev_diff > 5 and next_diff > 5 and _is_slide_blend(paths, i):
            os.remove(path)
        elif prev_diff > 5 and next_diff > 5 and _is_fade_blend(paths, i):
            os.remove(path)
        elif next_diff <= 10 and _has_edge_mismatch(paths, i):
            os.remove(path)
        else:
            stable.append(path)

    return stable


def _trim_trailing_motion(stable, full_hashes, motion_threshold=15):
    """
    Remove trailing frames caused by a swipe gesture at the end of the recording
    (swipe to home, tray open, app switcher).
    Walks backwards from the end removing frames with large diff from their predecessor.
    Stops when two consecutive frames are similar (stable content reached).
    """
    if len(stable) < 3:
        return stable

    i = len(stable) - 1
    while i > 0:
        h_curr = imagehash.phash(Image.open(stable[i]))
        h_prev = imagehash.phash(Image.open(stable[i - 1]))
        if abs(h_curr - h_prev) > motion_threshold:
            os.remove(stable[i])
            i -= 1
        else:
            break

    return stable[:i + 1]


def _dedup_consecutive(paths, hashes, dup_threshold=3):
    unique = []
    last_hash = None
    for path in paths:
        h = hashes[path]
        if last_hash is None or abs(h - last_hash) >= dup_threshold:
            unique.append(path)
            last_hash = h
        else:
            os.remove(path)
    return unique


def _dedup_global(paths, hashes, dup_threshold=3):
    seen = []
    unique = []
    for path in paths:
        h = hashes[path]
        if all(abs(h - s) >= dup_threshold for s in seen):
            seen.append(h)
            unique.append(path)
        else:
            os.remove(path)
    return unique


def filter_blank_frames(paths: list[str]) -> tuple[list[str], int]:
    """Remove pure-black and pure-white frames. Returns (kept, removed_count)."""
    kept = []
    removed = 0
    for p in paths:
        img = Image.open(p).convert("L")
        mean = sum(img.getdata()) / (img.width * img.height)
        if mean < 15 or mean > 240:
            os.remove(p)
            removed += 1
        else:
            kept.append(p)
    return kept, removed


def _original_index(path):
    """Extract frame number from filename for indexing into full_hashes."""
    name = os.path.splitext(os.path.basename(path))[0]
    return int(name.split('_')[-1]) - 1
