"""Stable segment detection and representative frame selection.

Groups consecutive frames that probably belong to the same UI screen into a
single stable segment, then picks one representative frame per segment to
forward to Gemini. Designed to be conservative: when in doubt, preserve
frames rather than drop them.

This module mutates FrameTimelineItem objects in place to record the chosen
segment_id, final/candidate/removed status, and keep/remove reasons so the
timeline.json output reflects what segment selection decided.
"""

from dataclasses import asdict, dataclass, field
from typing import Any
import json
import os

from processor.frame_timeline import FrameTimelineItem

SEGMENTS_VERSION = 1


@dataclass
class StableSegmentConfig:
    min_segment_duration_ms: int = 250
    edge_guard_ms: int = 150
    max_gap_ms: int = 500
    preserve_short_early_segments: bool = True
    preserve_revisited_screens: bool = True
    use_index_fallback: bool = True


@dataclass
class FrameSegment:
    segment_id: str
    start_index: int
    end_index: int
    start_ms: int | None
    end_ms: int | None
    duration_ms: int | None

    frame_ids: list[str]
    frame_paths: list[str]

    segment_type: str = "stable"
    stability_score: float | None = None

    representative_frame_id: str | None = None
    representative_path: str | None = None
    representative_index: int | None = None
    representative_timestamp_ms: int | None = None

    keep_reason: str | None = None
    remove_reason: str | None = None
    needs_review: bool = False
    review_reason: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Detection
# -----------------------------------------------------------------------------

def _segment_id(idx: int) -> str:
    return f"segment_{idx + 1:04d}"


def _classify_segment_type(duration_ms: int | None, count: int, config: StableSegmentConfig) -> str:
    if count == 1:
        return "single_frame"
    if duration_ms is None:
        return "uncertain"
    if duration_ms < config.min_segment_duration_ms:
        return "short_stable"
    return "stable"


def _finalize_bucket(bucket: list[FrameTimelineItem], seg_idx: int, config: StableSegmentConfig) -> FrameSegment:
    start_ms = bucket[0].timestamp_ms
    end_ms = bucket[-1].timestamp_ms
    duration_ms = (end_ms - start_ms) if (start_ms is not None and end_ms is not None) else None
    seg_type = _classify_segment_type(duration_ms, len(bucket), config)
    return FrameSegment(
        segment_id=_segment_id(seg_idx),
        start_index=bucket[0].index,
        end_index=bucket[-1].index,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=duration_ms,
        frame_ids=[it.frame_id for it in bucket],
        frame_paths=[it.path for it in bucket],
        segment_type=seg_type,
    )


def detect_stable_segments(
    timeline_items: list[FrameTimelineItem],
    config: StableSegmentConfig,
) -> list[FrameSegment]:
    """Group consecutive timeline items into stable segments.

    Items are assumed to be in pipeline order (already sorted by extract step).
    """
    if not timeline_items:
        return []

    all_have_ts = all(it.timestamp_ms is not None for it in timeline_items)

    if not all_have_ts:
        if not config.use_index_fallback:
            return []
        # Index fallback: each item is its own single-frame segment.
        segments: list[FrameSegment] = []
        for seg_idx, it in enumerate(timeline_items):
            seg = FrameSegment(
                segment_id=_segment_id(seg_idx),
                start_index=it.index,
                end_index=it.index,
                start_ms=it.timestamp_ms,
                end_ms=it.timestamp_ms,
                duration_ms=0 if it.timestamp_ms is not None else None,
                frame_ids=[it.frame_id],
                frame_paths=[it.path],
                segment_type="single_frame",
                needs_review=True,
                review_reason="timestamp_missing_index_fallback",
            )
            segments.append(seg)
        return segments

    segments = []
    bucket: list[FrameTimelineItem] = [timeline_items[0]]
    for curr in timeline_items[1:]:
        gap = curr.timestamp_ms - bucket[-1].timestamp_ms
        if gap <= config.max_gap_ms:
            bucket.append(curr)
        else:
            segments.append(_finalize_bucket(bucket, len(segments), config))
            bucket = [curr]
    segments.append(_finalize_bucket(bucket, len(segments), config))
    return segments


# -----------------------------------------------------------------------------
# Representative selection
# -----------------------------------------------------------------------------

def select_representative_frame(
    segment: FrameSegment,
    items_by_id: dict[str, FrameTimelineItem],
    config: StableSegmentConfig,
) -> tuple[FrameTimelineItem, str]:
    """Pick one FrameTimelineItem to forward to classification.

    Returns (chosen_item, keep_reason). Deterministic: with the same inputs,
    always returns the same frame.
    """
    items = [items_by_id[fid] for fid in segment.frame_ids]
    if not items:
        raise ValueError(f"segment {segment.segment_id} has no items")

    if len(items) == 1:
        return items[0], "single_frame_segment"

    # Multi-frame segment with timestamps: choose closest to midpoint with edge guard.
    if segment.start_ms is not None and segment.end_ms is not None:
        midpoint_ms = (segment.start_ms + segment.end_ms) // 2
        guard_lo = segment.start_ms + config.edge_guard_ms
        guard_hi = segment.end_ms - config.edge_guard_ms
        if guard_lo <= guard_hi:
            inner = [it for it in items if guard_lo <= (it.timestamp_ms or -1) <= guard_hi]
        else:
            inner = []
        if not inner:
            # Try middle slice (drop first and last) before giving up.
            if len(items) >= 3:
                inner = items[1:-1]
            else:
                inner = items
        # Pick the candidate closest to the midpoint. Ties broken by lowest index
        # for determinism.
        best = min(
            inner,
            key=lambda it: (abs((it.timestamp_ms or 0) - midpoint_ms), it.index),
        )
        return best, "stable_segment_representative"

    # No timestamps: pick by middle index. Prefer middle slice when at least 3.
    if len(items) >= 3:
        inner = items[1:-1]
    else:
        inner = items
    middle = inner[len(inner) // 2]
    return middle, "stable_segment_representative"


# -----------------------------------------------------------------------------
# Top-level orchestration
# -----------------------------------------------------------------------------

def apply_stable_segment_selection(
    items: list[FrameTimelineItem],
    config: StableSegmentConfig,
) -> tuple[list[FrameSegment], list[str]]:
    """Detect segments, pick representatives, and mutate items in place.

    Returns (segments, representative_paths). On empty input returns ([], []).
    Raises ValueError if it produces no representatives despite having items
    (so the caller can fall back to original frames).
    """
    segments = detect_stable_segments(items, config)
    if not segments:
        return [], []

    items_by_id = {it.frame_id: it for it in items}
    representative_paths: list[str] = []

    for seg in segments:
        rep_item, keep_reason = select_representative_frame(seg, items_by_id, config)
        seg.representative_frame_id = rep_item.frame_id
        seg.representative_path = rep_item.path
        seg.representative_index = rep_item.index
        seg.representative_timestamp_ms = rep_item.timestamp_ms
        seg.keep_reason = keep_reason

        # Mutate timeline items
        for fid in seg.frame_ids:
            it = items_by_id[fid]
            it.segment_id = seg.segment_id
            it.extra["segment_type"] = seg.segment_type
            if fid == rep_item.frame_id:
                it.status = "final"
                it.is_final = True
                it.keep_reason = keep_reason
                it.remove_reason = None
                it.extra["representative_for_segment"] = seg.segment_id
            else:
                it.status = "removed"
                it.is_final = False
                it.keep_reason = None
                it.remove_reason = "non_representative_segment_frame"

        representative_paths.append(rep_item.path)

    # First-screen safety
    _apply_first_screen_safety(segments, config)

    if not representative_paths:
        raise ValueError("segment selection produced no representatives")

    return segments, representative_paths


def _apply_first_screen_safety(segments: list[FrameSegment], config: StableSegmentConfig) -> None:
    """Mark short first segments and conspicuously-late first representatives."""
    if not segments:
        return
    first = segments[0]
    if first.segment_type == "short_stable" and config.preserve_short_early_segments:
        first.needs_review = True
        first.review_reason = "short_first_segment_preserved"


def compute_segment_warnings(
    segments: list[FrameSegment],
    items: list[FrameTimelineItem],
    config: StableSegmentConfig,
) -> list[str]:
    """Compute top-level warnings to embed in segments.json."""
    warnings: list[str] = []
    if not segments:
        return warnings
    first_seg = segments[0]
    if first_seg.representative_timestamp_ms is not None:
        first_observed_ts = next((it.timestamp_ms for it in items if it.timestamp_ms is not None), None)
        if first_observed_ts is not None:
            if first_seg.representative_timestamp_ms - first_observed_ts > config.edge_guard_ms * 2:
                warnings.append("first_representative_starts_late")
    if first_seg.review_reason == "short_first_segment_preserved":
        warnings.append("short_first_segment_preserved")
    return warnings


# -----------------------------------------------------------------------------
# Serialization
# -----------------------------------------------------------------------------

def serialize_segments(
    job_id: str,
    segments: list[FrameSegment],
    items: list[FrameTimelineItem],
    config: StableSegmentConfig,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict:
    payload = {
        "version": SEGMENTS_VERSION,
        "job_id": job_id,
        "segment_count": len(segments),
        "representative_count": sum(1 for s in segments if s.representative_frame_id),
        "fallback_used": fallback_used,
        "config": {
            "min_segment_duration_ms": config.min_segment_duration_ms,
            "edge_guard_ms": config.edge_guard_ms,
            "max_gap_ms": config.max_gap_ms,
            "preserve_short_early_segments": config.preserve_short_early_segments,
            "preserve_revisited_screens": config.preserve_revisited_screens,
            "use_index_fallback": config.use_index_fallback,
        },
        "warnings": compute_segment_warnings(segments, items, config),
        "segments": [asdict(s) for s in segments],
    }
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    return payload


def save_segments(payload: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "segments.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path
