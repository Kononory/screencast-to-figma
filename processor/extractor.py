import os
import glob
import ffmpeg
import imagehash
import cv2
import numpy as np
from PIL import Image

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
