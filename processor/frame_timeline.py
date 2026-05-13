"""Timeline metadata layer for the extraction pipeline.

The point of this module is to record what happened to each frame the pipeline
handled — when it was sampled in the source video, whether it survived
semantic dedup, what label Gemini gave it — without changing any of the
pipeline's existing behavior.

The timeline is written to `output/<job_id>/timeline.json` after the manifest
has been saved. It is purely additive metadata for local debugging; no
existing route, manifest, or session output depends on it.
"""

from dataclasses import asdict, dataclass, field
from typing import Any
import json
import os
import re

# Must stay in sync with the fps used by processor.extractor's
# `.filter("fps", fps=4)` ffmpeg call. If that value ever changes,
# update this constant or the inferred timestamps will be wrong.
EXTRACTION_FPS_DEFAULT = 4

TIMELINE_VERSION = 1
_FRAME_NUM_RE = re.compile(r"frame_(\d+)\.jpg$", re.IGNORECASE)


@dataclass
class FrameTimelineItem:
    frame_id: str
    index: int
    timestamp_ms: int | None
    path: str
    filename: str

    source: str = "extracted"

    status: str = "candidate"
    keep_reason: str | None = None
    remove_reason: str | None = None

    is_final: bool = False
    is_blank_removed: bool = False
    is_duplicate_removed: bool = False

    label: str | None = None
    screen_type: str | None = None

    motion_score_prev: float | None = None
    motion_score_next: float | None = None
    sharpness_score: float | None = None
    brightness_score: float | None = None
    contrast_score: float | None = None

    segment_id: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)


def infer_timestamp_ms_from_filename(
    filename: str,
    fps: int = EXTRACTION_FPS_DEFAULT,
) -> tuple[int | None, str]:
    """Return (timestamp_ms, source_tag) for an ffmpeg-numbered frame filename.

    Filenames of the form `frame_NNNN.jpg` (as produced by extractor.py) map to
    `(NNNN - 1) * 1000 // fps` milliseconds. Anything else returns
    (None, "unknown") so callers know to ignore the timestamp.
    """
    if fps <= 0:
        return None, "unknown"
    m = _FRAME_NUM_RE.search(os.path.basename(filename))
    if not m:
        return None, "unknown"
    n = int(m.group(1))
    return ((n - 1) * 1000) // fps, "inferred_from_filename_and_fps"


def _frame_id_from_path(path: str) -> str:
    base = os.path.basename(path)
    stem, _ = os.path.splitext(base)
    return stem or base


def build_pipeline_timeline(
    *,
    job_id: str,
    extracted_frames: list[str],
    final_frames: list[str],
    final_classifications: list[dict] | None = None,
    fps: int = EXTRACTION_FPS_DEFAULT,
) -> dict:
    """Construct a JSON-serializable timeline dict from pipeline outputs.

    extracted_frames: the frames that survived extract_frames + filter_blank_frames.
    final_frames:     subset that survived semantic dedup (== what reached manifest).
    final_classifications: classification dicts aligned with final_frames.

    Items appear in extraction order. Frames in final_frames are marked
    status="final"; the rest were dropped by semantic dedup and are marked
    status="removed", remove_reason="duplicate_removed".
    """
    final_index_by_path: dict[str, int] = {}
    for i, p in enumerate(final_frames):
        # If the same path appears twice (shouldn't happen in practice), keep
        # the first occurrence so the timeline matches the classification list.
        final_index_by_path.setdefault(p, i)

    classifications = final_classifications or []
    items: list[dict] = []
    timestamp_sources: set[str] = set()

    for idx, path in enumerate(extracted_frames):
        filename = os.path.basename(path)
        ts_ms, ts_source = infer_timestamp_ms_from_filename(filename, fps=fps)
        timestamp_sources.add(ts_source)

        item = FrameTimelineItem(
            frame_id=_frame_id_from_path(path),
            index=idx,
            timestamp_ms=ts_ms,
            path=path,
            filename=filename,
            source="extracted",
        )
        item.extra["timestamp_source"] = ts_source

        if path in final_index_by_path:
            item.status = "final"
            item.is_final = True
            item.keep_reason = "selected_by_existing_pipeline"
            final_idx = final_index_by_path[path]
            if final_idx < len(classifications):
                cls = classifications[final_idx]
                if isinstance(cls, dict):
                    label = cls.get("label")
                    if label is not None:
                        item.label = str(label)
                    screen_type = cls.get("screen_type")
                    if screen_type:
                        item.screen_type = str(screen_type)
                    elif label is not None:
                        item.screen_type = str(label).split("/", 1)[0]
        else:
            item.status = "removed"
            item.is_duplicate_removed = True
            item.remove_reason = "duplicate_removed"

        items.append(asdict(item))

    final_count = sum(1 for it in items if it["is_final"])

    if not timestamp_sources:
        timestamp_strategy = "no_frames"
    elif timestamp_sources == {"inferred_from_filename_and_fps"}:
        timestamp_strategy = "inferred_from_filename_and_fps"
    elif timestamp_sources == {"unknown"}:
        timestamp_strategy = "unknown"
    else:
        timestamp_strategy = "mixed"

    return {
        "version": TIMELINE_VERSION,
        "job_id": job_id,
        "frame_count": len(items),
        "final_count": final_count,
        "fps": fps,
        "summary": {
            "total_items": len(items),
            "final_count": final_count,
            "removed_count": len(items) - final_count,
            "timestamp_strategy": timestamp_strategy,
            "notes": [
                "Timeline currently tracks frames after existing extraction/filtering.",
                "Removed blank/stability/phash frames from inside extract_frames are not represented as individual items in this version.",
                "TODO: surface candidates removed by stability filter and phash dedup.",
            ],
        },
        "items": items,
    }


def save_timeline(timeline: dict, output_dir: str) -> str:
    """Write timeline.json to output_dir and return its absolute path."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "timeline.json")
    with open(path, "w") as f:
        json.dump(timeline, f, indent=2)
    return path


def build_timeline_items(
    extracted_frames: list[str],
    fps: int = EXTRACTION_FPS_DEFAULT,
) -> list[FrameTimelineItem]:
    """Build FrameTimelineItem objects from extracted frame paths.

    Returned items have status="candidate" and no classification info attached.
    Segment selection and classification fill those in later by mutating items
    in place.
    """
    items: list[FrameTimelineItem] = []
    for idx, path in enumerate(extracted_frames):
        filename = os.path.basename(path)
        ts_ms, ts_source = infer_timestamp_ms_from_filename(filename, fps=fps)
        item = FrameTimelineItem(
            frame_id=_frame_id_from_path(path),
            index=idx,
            timestamp_ms=ts_ms,
            path=path,
            filename=filename,
            source="extracted",
        )
        item.extra["timestamp_source"] = ts_source
        items.append(item)
    return items


def serialize_timeline_items(
    items: list[FrameTimelineItem],
    job_id: str,
    fps: int = EXTRACTION_FPS_DEFAULT,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict:
    """Serialize a list of FrameTimelineItem objects into the timeline JSON dict.

    Mirrors build_pipeline_timeline's output shape so existing consumers of
    timeline.json keep working. Adds segment_id to each item (or null when no
    segment selection ran).
    """
    timestamp_sources: set[str] = set()
    for it in items:
        timestamp_sources.add(it.extra.get("timestamp_source", "unknown"))

    if not timestamp_sources:
        timestamp_strategy = "no_frames"
    elif timestamp_sources == {"inferred_from_filename_and_fps"}:
        timestamp_strategy = "inferred_from_filename_and_fps"
    elif timestamp_sources == {"unknown"}:
        timestamp_strategy = "unknown"
    else:
        timestamp_strategy = "mixed"

    item_dicts = [asdict(it) for it in items]
    final_count = sum(1 for it in items if it.is_final)
    removed_count = sum(1 for it in items if it.status == "removed")

    notes = [
        "Timeline tracks frames after existing extraction/filtering.",
        "Items inside a stable segment but not chosen as the representative are marked status='removed' with remove_reason='non_representative_segment_frame'.",
        "Representatives dropped by the classifier's semantic dedup are marked status='removed' with remove_reason='duplicate_removed_by_classifier'.",
        "TODO: surface candidates removed by stability filter and phash dedup inside extract_frames.",
    ]
    if fallback_used:
        notes.append("Stable segment selection fell back to preserving original frames.")

    payload = {
        "version": TIMELINE_VERSION,
        "job_id": job_id,
        "frame_count": len(items),
        "final_count": final_count,
        "fps": fps,
        "fallback_used": fallback_used,
        "summary": {
            "total_items": len(items),
            "final_count": final_count,
            "removed_count": removed_count,
            "timestamp_strategy": timestamp_strategy,
            "notes": notes,
        },
        "items": item_dicts,
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    return payload
