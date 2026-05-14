"""Generalized boundary recovery + transition-contamination guard.

Runs after stable segment representative selection and before Gemini
classification. Its job is to:

- detect transition/state boundaries across the whole timeline (not just the
  first segment);
- score how likely each timeline frame is a corrupted mid-transition frame;
- recover real intermediate states that segment selection skipped, but only
  when they are stable (never when they are transition-contaminated);
- replace representatives that landed too close to a transition edge with a
  safer in-segment alternative when one exists;
- preserve short but meaningful states that look like modals / alerts /
  bottom sheets;
- detect drift-like A→C→C patterns where a stable middle candidate exists.

Everything is local. No Gemini, no OCR. If any required metadata is missing,
the implementation is conservative: it returns empty results rather than
corrupting the flow.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable
import json
import os

from processor.frame_segments import FrameSegment
from processor.frame_timeline import FrameTimelineItem
from processor.motion import MotionZone as _MotionZone  # canonical home is motion.py


BOUNDARY_RECOVERY_VERSION = 1


# Re-export so existing callers `from processor.boundary_recovery import MotionZone` keep working.
MotionZone = _MotionZone


# -----------------------------------------------------------------------------
# Config & models
# -----------------------------------------------------------------------------

@dataclass
class BoundaryRecoveryConfig:
    enabled: bool = True

    boundary_lookback_ms: int = 700
    boundary_lookahead_ms: int = 700
    transition_edge_guard_ms: int = 180

    min_state_duration_ms: int = 180
    preserve_short_states: bool = True

    duplicate_representative_window_ms: int = 1200
    near_duplicate_hash_threshold: int = 4

    recover_pre_transition_state: bool = True
    recover_post_transition_state: bool = True

    max_recovered_states_per_boundary: int = 2

    contamination_threshold: float = 0.65
    contamination_hard_threshold: float = 0.82

    mark_recovered_states_review: bool = True
    reject_contaminated_recovery_candidates: bool = True


@dataclass
class StateBoundary:
    boundary_id: str
    boundary_type: str  # transition | motion | uncertain | segment_gap

    start_ms: int | None
    end_ms: int | None
    peak_ms: int | None

    start_index: int | None = None
    end_index: int | None = None

    before_segment_id: str | None = None
    after_segment_id: str | None = None

    before_representative_id: str | None = None
    after_representative_id: str | None = None

    issue_detected: bool = False
    issue_type: str | None = None

    recovered_frame_ids: list[str] = field(default_factory=list)
    rejected_candidate_ids: list[str] = field(default_factory=list)
    contaminated_candidate_ids: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)


@dataclass
class TransitionContaminationResult:
    frame_id: str
    score: float
    is_contaminated: bool
    is_hard_contaminated: bool
    reason: str  # semicolon-separated tags


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _boundary_id(idx: int) -> str:
    return f"boundary_{idx + 1:04d}"


def _safe_int(v: Any) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _items_by_id(items: list[FrameTimelineItem]) -> dict[str, FrameTimelineItem]:
    return {it.frame_id: it for it in items}


def _segments_by_id(segments: list[FrameSegment]) -> dict[str, FrameSegment]:
    return {s.segment_id: s for s in segments}


# -----------------------------------------------------------------------------
# Boundary detection
# -----------------------------------------------------------------------------

def detect_state_boundaries(
    timeline_items: list[FrameTimelineItem],
    segments: list[FrameSegment],
    motion_zones: list[MotionZone] | None,
    selected_representatives: list[FrameTimelineItem],
    config: BoundaryRecoveryConfig,
) -> list[StateBoundary]:
    """Produce a chronological list of state boundaries.

    Sources, in priority order:
    - motion_zones, when provided (future motion module);
    - gaps between neighboring stable segments.

    Deterministic for the same inputs.
    """
    boundaries: list[StateBoundary] = []
    if not timeline_items:
        return boundaries

    # 1. Motion zones → boundaries.
    if motion_zones:
        for mz in motion_zones:
            boundaries.append(
                StateBoundary(
                    boundary_id="",  # assigned after sort
                    boundary_type=mz.zone_type or "transition",
                    start_ms=mz.start_ms,
                    end_ms=mz.end_ms,
                    peak_ms=mz.peak_ms,
                )
            )

    # 2. Segment-gap boundaries — one between every pair of adjacent segments.
    for i in range(len(segments) - 1):
        a = segments[i]
        b = segments[i + 1]
        start_ms = a.end_ms
        end_ms = b.start_ms
        if start_ms is not None and end_ms is not None and end_ms <= start_ms:
            # Touching/overlapping segments: still a boundary, but zero-width.
            peak = start_ms
        elif start_ms is not None and end_ms is not None:
            peak = (start_ms + end_ms) // 2
        else:
            peak = None
        boundaries.append(
            StateBoundary(
                boundary_id="",
                boundary_type="segment_gap",
                start_ms=start_ms,
                end_ms=end_ms,
                peak_ms=peak,
                start_index=a.end_index,
                end_index=b.start_index,
                before_segment_id=a.segment_id,
                after_segment_id=b.segment_id,
                before_representative_id=a.representative_frame_id,
                after_representative_id=b.representative_frame_id,
            )
        )

    # Sort by start_ms when available, else by start_index, else by insertion order.
    def _key(b: StateBoundary) -> tuple:
        return (
            0 if b.start_ms is not None else 1,
            b.start_ms if b.start_ms is not None else 0,
            b.start_index if b.start_index is not None else 0,
        )

    boundaries.sort(key=_key)
    for i, b in enumerate(boundaries):
        b.boundary_id = _boundary_id(i)
    return boundaries


# -----------------------------------------------------------------------------
# Transition contamination scoring
# -----------------------------------------------------------------------------

def _ts_inside_boundary(ts: int | None, b: StateBoundary) -> bool:
    if ts is None or b.start_ms is None or b.end_ms is None:
        return False
    return b.start_ms <= ts <= b.end_ms


def _ts_near_boundary_edge(ts: int | None, b: StateBoundary, edge_ms: int) -> bool:
    if ts is None:
        return False
    if b.start_ms is not None and abs(ts - b.start_ms) <= edge_ms:
        return True
    if b.end_ms is not None and abs(ts - b.end_ms) <= edge_ms:
        return True
    return False


def score_transition_contamination(
    item: FrameTimelineItem,
    prev_item: FrameTimelineItem | None,
    next_item: FrameTimelineItem | None,
    boundaries: list[StateBoundary],
    motion_zones: list[MotionZone] | None,
    config: BoundaryRecoveryConfig,
) -> TransitionContaminationResult:
    score = 0.0
    reasons: list[str] = []

    inside_zone = False
    near_edge = False
    for b in boundaries:
        if _ts_inside_boundary(item.timestamp_ms, b):
            inside_zone = True
        elif _ts_near_boundary_edge(item.timestamp_ms, b, config.transition_edge_guard_ms):
            near_edge = True

    # motion_zones param exists for future use; today we let boundaries cover the same job.

    if inside_zone:
        score += 0.35
        reasons.append("inside_transition_zone")
    if near_edge:
        score += 0.25
        reasons.append("near_transition_edge")

    mp = item.motion_score_prev
    mn = item.motion_score_next
    high_prev = isinstance(mp, (int, float)) and mp >= 0.6
    high_next = isinstance(mn, (int, float)) and mn >= 0.6
    if high_prev:
        score += 0.20
        reasons.append("high_motion_prev")
    if high_next:
        score += 0.20
        reasons.append("high_motion_next")
    if high_prev and high_next:
        score += 0.15
        reasons.append("high_motion_prev_next")

    sh = item.sharpness_score
    if isinstance(sh, (int, float)) and sh < 0.35:
        score += 0.10
        reasons.append("low_sharpness")

    if score > 1.0:
        score = 1.0
    is_contaminated = score >= config.contamination_threshold
    is_hard = score >= config.contamination_hard_threshold
    reason_str = "; ".join(reasons) if reasons else "no_signal"

    return TransitionContaminationResult(
        frame_id=item.frame_id,
        score=round(score, 3),
        is_contaminated=is_contaminated,
        is_hard_contaminated=is_hard,
        reason=reason_str,
    )


def _store_contamination_on_item(
    item: FrameTimelineItem,
    result: TransitionContaminationResult,
) -> None:
    item.extra["transition_contamination_score"] = result.score
    item.extra["is_transition_contaminated"] = result.is_contaminated
    item.extra["contamination_reason"] = result.reason


# -----------------------------------------------------------------------------
# Candidate classification
# -----------------------------------------------------------------------------

def classify_boundary_candidate(
    item: FrameTimelineItem,
    contamination: TransitionContaminationResult,
    segment: FrameSegment | None,
    config: BoundaryRecoveryConfig,
) -> str:
    """Return one of: likely_stable_state | likely_transition_contaminated | uncertain."""
    if contamination.is_contaminated:
        return "likely_transition_contaminated"

    if segment is None:
        return "uncertain"

    if segment.segment_type in ("stable", "short_stable", "single_frame"):
        if contamination.score < config.contamination_threshold:
            return "likely_stable_state"

    return "uncertain"


# -----------------------------------------------------------------------------
# Edge-guard for representatives
# -----------------------------------------------------------------------------

def is_near_transition_boundary(
    item: FrameTimelineItem,
    boundaries: list[StateBoundary],
    motion_zones: list[MotionZone] | None,
    edge_guard_ms: int,
) -> bool:
    for b in boundaries:
        if _ts_near_boundary_edge(item.timestamp_ms, b, edge_guard_ms):
            return True
        if _ts_inside_boundary(item.timestamp_ms, b):
            return True
    return False


def _find_safer_in_segment(
    item: FrameTimelineItem,
    segment: FrameSegment | None,
    items_by_id: dict[str, FrameTimelineItem],
    boundaries: list[StateBoundary],
    config: BoundaryRecoveryConfig,
    contamination_by_id: dict[str, TransitionContaminationResult],
) -> FrameTimelineItem | None:
    """Find a different frame in the same segment that isn't near a transition edge."""
    if segment is None:
        return None
    if len(segment.frame_ids) <= 1:
        return None
    midpoint_ms = None
    if segment.start_ms is not None and segment.end_ms is not None:
        midpoint_ms = (segment.start_ms + segment.end_ms) // 2

    candidates: list[FrameTimelineItem] = []
    for fid in segment.frame_ids:
        cand = items_by_id.get(fid)
        if cand is None or cand.frame_id == item.frame_id:
            continue
        if is_near_transition_boundary(cand, boundaries, None, config.transition_edge_guard_ms):
            continue
        cont = contamination_by_id.get(cand.frame_id)
        if cont and cont.is_contaminated:
            continue
        candidates.append(cand)
    if not candidates:
        return None

    def _key(c: FrameTimelineItem) -> tuple:
        mid_dist = (
            abs((c.timestamp_ms or 0) - midpoint_ms)
            if midpoint_ms is not None and c.timestamp_ms is not None
            else 0
        )
        motion = c.motion_score_prev if isinstance(c.motion_score_prev, (int, float)) else 0.0
        return (mid_dist, motion, c.index)

    candidates.sort(key=_key)
    return candidates[0]


# -----------------------------------------------------------------------------
# Skipped-state recovery
# -----------------------------------------------------------------------------

def _items_in_window(
    items: list[FrameTimelineItem],
    start_ms: int | None,
    end_ms: int | None,
    selected_ids: set[str],
) -> list[FrameTimelineItem]:
    out: list[FrameTimelineItem] = []
    for it in items:
        if it.frame_id in selected_ids:
            continue
        ts = it.timestamp_ms
        if ts is None:
            continue
        if start_ms is not None and ts < start_ms:
            continue
        if end_ms is not None and ts > end_ms:
            continue
        out.append(it)
    return out


def _segment_for_item(item: FrameTimelineItem, segments_by_id: dict[str, FrameSegment]) -> FrameSegment | None:
    if item.segment_id is None:
        return None
    return segments_by_id.get(item.segment_id)


def recover_boundary_states(
    boundary: StateBoundary,
    timeline_items: list[FrameTimelineItem],
    segments: list[FrameSegment],
    selected_representatives: list[FrameTimelineItem],
    contamination_by_frame_id: dict[str, TransitionContaminationResult],
    config: BoundaryRecoveryConfig,
) -> list[FrameTimelineItem]:
    """Return a list of timeline items to insert as recovered representatives."""
    if not config.enabled:
        return []
    selected_ids = {it.frame_id for it in selected_representatives}
    segments_by_id = _segments_by_id(segments)

    recovered: list[FrameTimelineItem] = []
    rejected: list[str] = []
    contaminated: list[str] = []

    windows: list[tuple[int | None, int | None]] = []
    if config.recover_pre_transition_state:
        if boundary.start_ms is not None:
            windows.append((
                boundary.start_ms - config.boundary_lookback_ms,
                boundary.start_ms - config.transition_edge_guard_ms,
            ))
    if config.recover_post_transition_state:
        if boundary.end_ms is not None:
            windows.append((
                boundary.end_ms + config.transition_edge_guard_ms,
                boundary.end_ms + config.boundary_lookahead_ms,
            ))

    seen_paths: set[str] = {it.path for it in selected_representatives}

    for start_ms, end_ms in windows:
        candidates = _items_in_window(timeline_items, start_ms, end_ms, selected_ids)
        for cand in candidates:
            if cand.path in seen_paths:
                continue
            cont = contamination_by_frame_id.get(cand.frame_id)
            if cont is None:
                # Treat unknown as uncertain — don't recover.
                rejected.append(cand.frame_id)
                continue
            seg = _segment_for_item(cand, segments_by_id)
            classification = classify_boundary_candidate(cand, cont, seg, config)
            if classification == "likely_transition_contaminated":
                contaminated.append(cand.frame_id)
                if config.reject_contaminated_recovery_candidates:
                    rejected.append(cand.frame_id)
                continue
            if classification == "uncertain":
                rejected.append(cand.frame_id)
                continue
            recovered.append(cand)
            seen_paths.add(cand.path)
            if len(recovered) >= config.max_recovered_states_per_boundary:
                break
        if len(recovered) >= config.max_recovered_states_per_boundary:
            break

    boundary.recovered_frame_ids = [r.frame_id for r in recovered]
    boundary.rejected_candidate_ids = rejected
    boundary.contaminated_candidate_ids = contaminated
    if contaminated and not recovered:
        boundary.issue_detected = True
        boundary.issue_type = "possible_skipped_state_only_seen_as_transition_contaminated_frame"
        boundary.warnings.append(
            "Skipped recovery because only candidate was transition-contaminated"
        )
    elif recovered:
        boundary.issue_detected = True
        # Tag by which side the first recovered frame fell on.
        if boundary.start_ms is not None and recovered[0].timestamp_ms is not None:
            if recovered[0].timestamp_ms <= boundary.start_ms:
                boundary.issue_type = "skipped_pre_transition_state"
            else:
                boundary.issue_type = "skipped_post_transition_state"
        else:
            boundary.issue_type = "skipped_post_transition_state"
    return recovered


# -----------------------------------------------------------------------------
# Drift / duplicate-representative detection
# -----------------------------------------------------------------------------

def _detect_duplicate_drift(
    boundary: StateBoundary,
    selected_representatives: list[FrameTimelineItem],
    config: BoundaryRecoveryConfig,
) -> bool:
    """Return True if before/after representatives look like an A→C→C drift pattern."""
    bid = boundary.before_representative_id
    aid = boundary.after_representative_id
    if not bid or not aid:
        return False
    before = next((r for r in selected_representatives if r.frame_id == bid), None)
    after = next((r for r in selected_representatives if r.frame_id == aid), None)
    if before is None or after is None:
        return False
    if before.path == after.path:
        return True
    if (
        before.timestamp_ms is not None
        and after.timestamp_ms is not None
        and abs(after.timestamp_ms - before.timestamp_ms) <= config.duplicate_representative_window_ms
        and before.label is not None
        and before.label == after.label
        and (before.screen_type or "") == (after.screen_type or "")
    ):
        return True
    return False


# -----------------------------------------------------------------------------
# Short-state preservation
# -----------------------------------------------------------------------------

def _preserve_short_intermediate_states(
    segments: list[FrameSegment],
    items_by_id: dict[str, FrameTimelineItem],
    selected_reps: list[FrameTimelineItem],
    contamination_by_id: dict[str, TransitionContaminationResult],
    config: BoundaryRecoveryConfig,
) -> list[FrameTimelineItem]:
    """Find short_stable segments sandwiched between other segments and preserve them."""
    if not config.preserve_short_states:
        return []
    selected_ids = {it.frame_id for it in selected_reps}
    extra: list[FrameTimelineItem] = []
    for i, seg in enumerate(segments):
        if seg.segment_type != "short_stable":
            continue
        if i == 0 or i == len(segments) - 1:
            # Skip first/last — first-segment safety is owned by frame_segments.py.
            continue
        # Skip if its representative is already in selection.
        if seg.representative_frame_id and seg.representative_frame_id in selected_ids:
            continue
        rep_id = seg.representative_frame_id
        if not rep_id:
            continue
        item = items_by_id.get(rep_id)
        if item is None:
            continue
        cont = contamination_by_id.get(item.frame_id)
        if cont and cont.is_contaminated:
            continue
        extra.append(item)
    return extra


# -----------------------------------------------------------------------------
# Top-level orchestration
# -----------------------------------------------------------------------------

@dataclass
class BoundaryRecoveryOutcome:
    boundaries: list[StateBoundary]
    contamination_by_id: dict[str, TransitionContaminationResult]
    final_representatives: list[FrameTimelineItem]
    edge_guard_warnings: int
    short_state_preserved: int


def apply_boundary_recovery(
    items: list[FrameTimelineItem],
    segments: list[FrameSegment],
    selected_representatives: list[FrameTimelineItem],
    motion_zones: list[MotionZone] | None,
    config: BoundaryRecoveryConfig,
) -> BoundaryRecoveryOutcome:
    """Full orchestration. Mutates items in place; returns the new representative list."""
    if not items or not config.enabled:
        return BoundaryRecoveryOutcome(
            boundaries=[],
            contamination_by_id={},
            final_representatives=list(selected_representatives),
            edge_guard_warnings=0,
            short_state_preserved=0,
        )

    boundaries = detect_state_boundaries(
        items, segments, motion_zones, selected_representatives, config
    )

    # 1. Score contamination for every item.
    contamination_by_id: dict[str, TransitionContaminationResult] = {}
    items_by_id = _items_by_id(items)
    segments_by_id = _segments_by_id(segments)
    indexed = sorted(items, key=lambda it: it.index)
    for i, it in enumerate(indexed):
        prev_it = indexed[i - 1] if i > 0 else None
        next_it = indexed[i + 1] if i + 1 < len(indexed) else None
        try:
            res = score_transition_contamination(
                it, prev_it, next_it, boundaries, motion_zones, config
            )
        except Exception:
            res = TransitionContaminationResult(
                frame_id=it.frame_id,
                score=0.0,
                is_contaminated=False,
                is_hard_contaminated=False,
                reason="contamination_scoring_failed",
            )
        contamination_by_id[it.frame_id] = res
        _store_contamination_on_item(it, res)

    # 2. Edge-guard pass: replace risky reps with safer same-segment candidates.
    final_reps: list[FrameTimelineItem] = list(selected_representatives)
    edge_guard_warnings = 0
    for idx, rep in enumerate(final_reps):
        if not is_near_transition_boundary(rep, boundaries, motion_zones, config.transition_edge_guard_ms):
            continue
        seg = _segment_for_item(rep, segments_by_id)
        safer = _find_safer_in_segment(rep, seg, items_by_id, boundaries, config, contamination_by_id)
        if safer is not None:
            # Swap in segment metadata
            if seg is not None:
                seg.representative_frame_id = safer.frame_id
                seg.representative_path = safer.path
                seg.representative_index = safer.index
                seg.representative_timestamp_ms = safer.timestamp_ms
                seg.extra["representative_replaced_reason"] = "near_transition_boundary"
            # Mutate timeline items
            rep.status = "removed"
            rep.is_final = False
            rep.keep_reason = None
            rep.remove_reason = "representative_replaced_near_transition_boundary"
            safer.status = "final"
            safer.is_final = True
            safer.keep_reason = "stable_segment_representative"
            safer.extra["representative_for_segment"] = seg.segment_id if seg else None
            safer.extra["replaced_representative_id"] = rep.frame_id
            final_reps[idx] = safer
        else:
            rep.extra["needs_review"] = True
            rep.extra["review_reason"] = "representative_near_transition_boundary"
            edge_guard_warnings += 1

    # 3. Boundary recovery: insert stable skipped states.
    recovered_by_boundary: list[tuple[StateBoundary, list[FrameTimelineItem]]] = []
    for b in boundaries:
        if _detect_duplicate_drift(b, final_reps, config):
            b.warnings.append("adjacent_representatives_look_like_drift")
            b.issue_detected = True
            if not b.issue_type:
                b.issue_type = "duplicate_representative_after_boundary"
        recovered = recover_boundary_states(
            b, items, segments, final_reps, contamination_by_id, config
        )
        if recovered:
            recovered_by_boundary.append((b, recovered))

    # 4. Short intermediate states (separate from boundary recovery).
    short_extra = _preserve_short_intermediate_states(
        segments, items_by_id, final_reps, contamination_by_id, config
    )

    # 5. Apply recovered + short-state insertions, mutate metadata.
    insertions: list[FrameTimelineItem] = []
    for b, recovered in recovered_by_boundary:
        for r in recovered:
            r.status = "final"
            r.is_final = True
            r.keep_reason = "recovered_boundary_state"
            r.remove_reason = None
            r.extra["boundary_recovery"] = True
            r.extra["boundary_id"] = b.boundary_id
            if config.mark_recovered_states_review:
                r.extra["needs_review"] = True
                r.extra["review_reason"] = "possible_skipped_intermediate_state"
            insertions.append(r)
    for r in short_extra:
        r.status = "final"
        r.is_final = True
        r.keep_reason = "short_state_preserved"
        r.remove_reason = None
        r.extra["needs_review"] = True
        r.extra["review_reason"] = "short_intermediate_state"
        insertions.append(r)

    # Mark contaminated, non-final items so timeline.json reflects rejection.
    for it in items:
        if it.is_final:
            continue
        cont = contamination_by_id.get(it.frame_id)
        if cont and cont.is_contaminated:
            it.remove_reason = it.remove_reason or "transition_contaminated"

    # 6. Merge final reps + insertions, sort by timestamp/index.
    merged: dict[str, FrameTimelineItem] = {it.frame_id: it for it in final_reps}
    for it in insertions:
        merged.setdefault(it.frame_id, it)
    ordered = sorted(
        merged.values(),
        key=lambda it: (
            it.timestamp_ms if it.timestamp_ms is not None else 10**12,
            it.index,
        ),
    )

    return BoundaryRecoveryOutcome(
        boundaries=boundaries,
        contamination_by_id=contamination_by_id,
        final_representatives=ordered,
        edge_guard_warnings=edge_guard_warnings,
        short_state_preserved=len(short_extra),
    )


# -----------------------------------------------------------------------------
# Serialization
# -----------------------------------------------------------------------------

def compute_boundary_recovery_summary(
    outcome: BoundaryRecoveryOutcome,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict:
    contaminated = sum(1 for c in outcome.contamination_by_id.values() if c.is_contaminated)
    rejected = sum(len(b.rejected_candidate_ids) for b in outcome.boundaries)
    recovered = sum(len(b.recovered_frame_ids) for b in outcome.boundaries)
    issues = sum(1 for b in outcome.boundaries if b.issue_detected)
    warnings: list[str] = []
    for b in outcome.boundaries:
        for w in b.warnings:
            if w not in warnings:
                warnings.append(w)
    return {
        "enabled": True,
        "boundaries_detected": len(outcome.boundaries),
        "issues_detected": issues,
        "recovered_states": recovered,
        "contaminated_candidates": contaminated,
        "contaminated_candidates_rejected": rejected,
        "transition_edge_warnings": outcome.edge_guard_warnings,
        "short_state_preserved": outcome.short_state_preserved,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "warnings": warnings,
    }


def serialize_boundary_recovery(
    job_id: str,
    outcome: BoundaryRecoveryOutcome,
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict:
    summary = compute_boundary_recovery_summary(outcome, fallback_used, fallback_reason)
    return {
        "version": BOUNDARY_RECOVERY_VERSION,
        "job_id": job_id,
        "boundary_count": summary["boundaries_detected"],
        "issue_count": summary["issues_detected"],
        "recovered_count": summary["recovered_states"],
        "contaminated_candidate_count": summary["contaminated_candidates"],
        "rejected_contaminated_candidate_count": summary["contaminated_candidates_rejected"],
        "transition_edge_warnings": summary["transition_edge_warnings"],
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "warnings": summary["warnings"],
        "boundaries": [asdict(b) for b in outcome.boundaries],
    }


def save_boundary_recovery(payload: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "boundary_recovery.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path
