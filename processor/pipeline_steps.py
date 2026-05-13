import json
import os

from processor.ai import get_ai_provider
from processor.boundary_recovery import (
    BoundaryRecoveryConfig,
    BoundaryRecoveryOutcome,
    apply_boundary_recovery,
    save_boundary_recovery,
    serialize_boundary_recovery,
)
from processor.downloader import download_video
from processor.extractor import extract_frames, filter_blank_frames
from processor.frame_segments import (
    FrameSegment,
    StableSegmentConfig,
    apply_stable_segment_selection,
    save_segments,
    serialize_segments,
)
from processor.frame_timeline import (
    EXTRACTION_FPS_DEFAULT,
    FrameTimelineItem,
    build_timeline_items,
    save_timeline,
    serialize_timeline_items,
)
from processor.pipeline_context import PipelineContext, PipelineResult
from processor.results_store import save_to_disk
from server.models import JobState

_ANALYSIS_MAX_CANDIDATES = 20
_ANALYSIS_EXCLUDED_LABELS = {"transition", "system_tray", "app_switcher", "home_screen", "unsorted"}
_FUNNEL_EXCLUDED_LABELS = {"transition", "system_tray", "app_switcher"}


def _log(job: JobState, msg: str) -> None:
    print(msg)
    job.add_log(msg)


def _step(job: JobState, label: str, progress: int) -> None:
    job.set_progress(progress, step=label)


def prepare_video_source(ctx: PipelineContext, job: JobState) -> str:
    if ctx.source_type == "url":
        _step(job, "Downloading", 10)
        _log(job, "Downloading video...")
        video_path = download_video(ctx.video_url, str(ctx.job_tmp_dir))
        _log(job, f"Video downloaded: {os.path.basename(video_path)}")
        return video_path

    video_path = ctx.video_path
    _step(job, "Loading file", 10)
    _log(job, f"Using local file: {os.path.basename(video_path)}")
    return video_path


def extract_step(ctx: PipelineContext, job: JobState, video_path: str) -> list[str]:
    _step(job, "Extracting frames", 25)
    _log(job, f"Extracting frames (consec={ctx.consec_threshold}, global={ctx.global_threshold})...")
    frames = extract_frames(
        video_path,
        str(ctx.job_tmp_dir),
        consec_threshold=ctx.consec_threshold,
        global_threshold=ctx.global_threshold,
    )
    frames, blank_count = filter_blank_frames(frames)
    if blank_count:
        _log(job, f"Removed {blank_count} blank frames (black/white)")
    _log(job, f"Extracted {len(frames)} frames")
    return frames


def classify_step(
    ctx: PipelineContext,
    job: JobState,
    frames: list[str],
) -> tuple[list[str], list[dict]]:
    if not ctx.classify:
        _log(job, "AI classification skipped — importing all frames as-is")
        unique_frames = frames
        unique_cls = [
            {"label": "unsorted", "conf": 1.0, "key_text": "", "components": [], "state": ""}
            for _ in frames
        ]
        _log(job, f"--- {len(unique_frames)} frames ---")
        return unique_frames, unique_cls

    _step(job, "Classifying flows", 55)
    _log(job, "Classifying screens with AI...")
    provider = get_ai_provider(ctx.provider)
    classifications = provider.classify_frames(
        frames,
        api_key=ctx.api_key,
        log_fn=lambda m: _log(job, m),
        debug_dir=str(ctx.job_output_dir),
    )

    _log(job, f"--- AI output ({len(frames)} frames) ---")
    for i, (path, item) in enumerate(zip(frames, classifications)):
        state_tag = f" [{item.get('state')}]" if item.get("state") else ""
        _log(job, f"  {i+1:02d} {item['label']} ({item['conf']:.2f}) \"{item.get('key_text','')}\"{state_tag}")

    # Deduplicate semantically: same label + key_text + components + state = same screen
    seen = set()
    unique_frames: list[str] = []
    unique_cls: list[dict] = []
    for path, item in zip(frames, classifications):
        text = item["key_text"].strip().lower()
        components_key = tuple(sorted(item.get("components", [])))
        state = item.get("state", "")
        if text:
            key = (item["label"], text, components_key, state)
        elif components_key:
            key = (item["label"], components_key, state)
        else:
            key = path
        if key not in seen:
            seen.add(key)
            unique_frames.append(path)
            unique_cls.append(item)
        else:
            _log(job, f"  DEDUP: {item['label']} \"{item.get('key_text','')}\" dropped")

    _log(job, f"--- {len(unique_frames)} unique screens (from {len(frames)}) ---")
    cats = sorted(set(c["label"] for c in unique_cls if c["label"] not in ("transition", "unsorted")))
    _log(job, f"Sections: {', '.join(cats) or 'none'}")
    return unique_frames, unique_cls


def analyze_step(
    ctx: PipelineContext,
    job: JobState,
    unique_frames: list[str],
    unique_cls: list[dict],
) -> dict | None:
    if not (ctx.classify and unique_frames):
        return None

    _step(job, "Analyzing", 75)
    _log(job, "Running UX analysis...")
    candidates = [
        p for p, c in zip(unique_frames, unique_cls)
        if c["label"].split("/")[0] not in _ANALYSIS_EXCLUDED_LABELS
    ]
    if not candidates:
        return None

    if len(candidates) > _ANALYSIS_MAX_CANDIDATES:
        step = len(candidates) / _ANALYSIS_MAX_CANDIDATES
        candidates = [candidates[int(i * step)] for i in range(_ANALYSIS_MAX_CANDIDATES)]

    provider = get_ai_provider(ctx.provider)
    analysis = provider.analyze_ux(candidates, api_key=ctx.api_key)
    _log(job, "UX analysis complete" if analysis else "UX analysis failed")
    return analysis


def save_step(
    ctx: PipelineContext,
    job: JobState,
    unique_frames: list[str],
    unique_cls: list[dict],
    analysis: dict | None,
) -> str:
    _step(job, "Saving", 88)
    _log(job, "Saving results...")
    manifest_path = save_to_disk(unique_frames, unique_cls, str(ctx.job_output_dir))

    if analysis:
        with open(manifest_path) as f:
            mdata = json.load(f)
        mdata["_analysis"] = analysis
        with open(manifest_path, "w") as f:
            json.dump(mdata, f, indent=2)

        funnel = [c["label"] for c in unique_cls if c["label"] not in _FUNNEL_EXCLUDED_LABELS]
        paywall_pos = next((i for i, l in enumerate(funnel) if l.startswith("paywall")), None)
        profile = {
            "job_id": ctx.job_id,
            "funnel_sequence": funnel,
            "paywall_position": paywall_pos,
            "onboarding_count": sum(1 for l in funnel if l.startswith("onboarding")),
            "has_downsell": any(l.startswith("special_offer") for l in funnel),
            "strategy_coherence": analysis.get("strategy_coherence"),
            "competitive_tier": analysis.get("competitive_tier"),
            "monetization_hypothesis": analysis.get("monetization_hypothesis"),
            "onboarding_hypothesis": analysis.get("onboarding_hypothesis"),
            "feature_strategy_reasoning": analysis.get("feature_strategy_reasoning"),
        }
        os.makedirs(str(ctx.sessions_dir), exist_ok=True)
        with open(str(ctx.session_path), "w") as f:
            json.dump(profile, f, indent=2)

    return manifest_path


def finalize_step(
    ctx: PipelineContext,
    job: JobState,
    total_frames: int,
    unique_count: int,
    manifest_path: str,
) -> PipelineResult:
    job.manifest_path = manifest_path
    job.extracted = total_frames
    job.dupes = total_frames - unique_count
    job.mark_done(manifest_path=manifest_path, step="Done")
    _log(job, f"Done. Job ID: {ctx.job_id}")
    return PipelineResult(
        manifest_path=manifest_path,
        extracted=total_frames,
        dupes=total_frames - unique_count,
    )


def run_processing_steps(ctx: PipelineContext, job: JobState) -> PipelineResult:
    video_path = prepare_video_source(ctx, job)
    frames = extract_step(ctx, job, video_path)
    total_frames = len(frames)

    items, segments, fallback_reason, representative_frames = _apply_segment_selection_safely(
        ctx, job, frames
    )

    br_outcome, br_fallback_reason, representative_frames = _apply_boundary_recovery_safely(
        ctx, job, items, segments, representative_frames
    )

    unique_frames, unique_cls = classify_step(ctx, job, representative_frames)
    _update_items_with_classifications(items, unique_frames, unique_cls)

    analysis = analyze_step(ctx, job, unique_frames, unique_cls)
    manifest_path = save_step(ctx, job, unique_frames, unique_cls, analysis)

    _save_timeline_and_segments_safely(
        ctx, job, items, segments, fallback_reason, br_outcome, br_fallback_reason
    )

    return finalize_step(ctx, job, total_frames, len(unique_frames), manifest_path)


def _apply_segment_selection_safely(
    ctx: PipelineContext,
    job: JobState,
    frames: list[str],
) -> tuple[list[FrameTimelineItem], list, str | None, list[str]]:
    """Build timeline items, detect stable segments, select representatives.

    Returns (items, segments, fallback_reason, representative_paths).
    On any failure, items get a safe default ('selected_by_existing_pipeline')
    and representative_paths == frames so the caller behaves like the pre-job
    pipeline.
    """
    items = build_timeline_items(frames, fps=EXTRACTION_FPS_DEFAULT)
    if not items:
        return items, [], None, []

    _log(job, "Detecting stable segments...")
    try:
        config = StableSegmentConfig()
        segments, representative_paths = apply_stable_segment_selection(items, config)
        if not representative_paths:
            raise RuntimeError("empty_representatives")
        _log(job, f"Stable segments detected: {len(segments)}")
        _log(job, f"Representative frames selected: {len(representative_paths)}")
        return items, segments, None, representative_paths
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        _log(job, f"Warning: stable segment selection failed: {reason}")
        _log(job, f"Segment selection fallback used: {reason}")
        # Preserve current behavior: every extracted frame goes forward as 'final'.
        for it in items:
            it.status = "final"
            it.is_final = True
            it.keep_reason = "selected_by_existing_pipeline"
            it.remove_reason = None
            it.segment_id = None
        return items, [], reason, list(frames)


def _update_items_with_classifications(
    items: list[FrameTimelineItem],
    unique_frames: list[str],
    classifications: list[dict],
) -> None:
    """Attach label/screen_type to items that reached the manifest.

    A representative that was sent to Gemini but dropped by the classifier's
    semantic dedup is downgraded from status='final' to status='removed' with
    remove_reason='duplicate_removed_by_classifier'.
    """
    unique_index_by_path: dict[str, int] = {}
    for i, p in enumerate(unique_frames):
        unique_index_by_path.setdefault(p, i)

    for it in items:
        was_final = it.is_final or it.status == "final"
        if not was_final:
            continue
        if it.path in unique_index_by_path:
            idx = unique_index_by_path[it.path]
            if idx < len(classifications):
                cls = classifications[idx]
                if isinstance(cls, dict):
                    label = cls.get("label")
                    if label is not None:
                        it.label = str(label)
                    screen_type = cls.get("screen_type")
                    if screen_type:
                        it.screen_type = str(screen_type)
                    elif label is not None:
                        it.screen_type = str(label).split("/", 1)[0]
        else:
            # Representative dropped by classifier's semantic dedup.
            it.is_final = False
            it.status = "removed"
            it.remove_reason = "duplicate_removed_by_classifier"
            it.is_duplicate_removed = True
            it.keep_reason = None


def _apply_boundary_recovery_safely(
    ctx: PipelineContext,
    job: JobState,
    items: list[FrameTimelineItem],
    segments: list[FrameSegment],
    representative_frames: list[str],
) -> tuple[BoundaryRecoveryOutcome | None, str | None, list[str]]:
    """Run boundary recovery between segment selection and classification.

    On any failure or empty result, log a warning and forward the original
    representative_frames unchanged so the rest of the pipeline behaves exactly
    as it did before this job.
    """
    if not items or not representative_frames:
        return None, None, representative_frames

    _log(job, "Boundary recovery: detecting transition boundaries...")
    config = BoundaryRecoveryConfig()
    try:
        rep_items = [it for it in items if it.is_final and it.path in set(representative_frames)]
        if not rep_items:
            # representative_frames are paths from extracted_frames; map back conservatively.
            rep_items = [it for it in items if it.path in set(representative_frames)]
        outcome = apply_boundary_recovery(
            items=items,
            segments=segments,
            selected_representatives=rep_items,
            motion_zones=None,
            config=config,
        )
        new_paths = [it.path for it in outcome.final_representatives]
        if not new_paths:
            raise RuntimeError("empty_after_boundary_recovery")
        recovered_count = sum(len(b.recovered_frame_ids) for b in outcome.boundaries)
        contaminated_count = sum(1 for c in outcome.contamination_by_id.values() if c.is_contaminated)
        rejected_count = sum(len(b.rejected_candidate_ids) for b in outcome.boundaries)
        _log(job, f"Boundary recovery: detected {len(outcome.boundaries)} boundaries")
        if contaminated_count:
            _log(job, f"Transition contamination: detected {contaminated_count} contaminated candidates")
        if recovered_count:
            _log(job, f"Boundary recovery: recovered {recovered_count} states")
        if rejected_count:
            _log(job, f"Boundary recovery: rejected {rejected_count} contaminated candidates")
        if outcome.edge_guard_warnings:
            _log(job, f"Boundary recovery: transition-edge warnings: {outcome.edge_guard_warnings}")
        return outcome, None, new_paths
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        _log(job, f"Boundary recovery fallback used: {reason}")
        return None, reason, representative_frames


def _save_timeline_and_segments_safely(
    ctx: PipelineContext,
    job: JobState,
    items: list[FrameTimelineItem],
    segments: list,
    fallback_reason: str | None,
    br_outcome: BoundaryRecoveryOutcome | None = None,
    br_fallback_reason: str | None = None,
) -> None:
    try:
        timeline_payload = serialize_timeline_items(
            items,
            job_id=ctx.job_id,
            fps=EXTRACTION_FPS_DEFAULT,
            fallback_used=fallback_reason is not None,
            fallback_reason=fallback_reason,
        )
        if br_outcome is not None:
            timeline_payload["boundary_recovery"] = _br_top_level_block(
                br_outcome, fallback_used=False, fallback_reason=None
            )
        elif br_fallback_reason is not None:
            timeline_payload["boundary_recovery"] = {
                "enabled": True,
                "boundaries_detected": 0,
                "issues_detected": 0,
                "recovered_states": 0,
                "contaminated_candidates": 0,
                "contaminated_candidates_rejected": 0,
                "transition_edge_warnings": 0,
                "fallback_used": True,
                "fallback_reason": br_fallback_reason,
                "warnings": [],
            }
        timeline_path = save_timeline(timeline_payload, str(ctx.job_output_dir))
        _log(job, f"Timeline metadata saved: {timeline_path}")

        config = StableSegmentConfig()
        segments_payload = serialize_segments(
            job_id=ctx.job_id,
            segments=segments,
            items=items,
            config=config,
            fallback_used=fallback_reason is not None,
            fallback_reason=fallback_reason,
        )
        segments_path = save_segments(segments_payload, str(ctx.job_output_dir))
        _log(job, f"Segments metadata saved: {segments_path}")

        if br_outcome is not None or br_fallback_reason is not None:
            br_payload = serialize_boundary_recovery(
                job_id=ctx.job_id,
                outcome=br_outcome
                or BoundaryRecoveryOutcome(
                    boundaries=[],
                    contamination_by_id={},
                    final_representatives=[],
                    edge_guard_warnings=0,
                    short_state_preserved=0,
                ),
                fallback_used=br_fallback_reason is not None,
                fallback_reason=br_fallback_reason,
            )
            br_path = save_boundary_recovery(br_payload, str(ctx.job_output_dir))
            _log(job, f"Boundary recovery metadata saved: {br_path}")
    except Exception as exc:
        _log(job, f"Warning: failed to save timeline/segments metadata: {exc}")


def _br_top_level_block(
    outcome: BoundaryRecoveryOutcome,
    fallback_used: bool,
    fallback_reason: str | None,
) -> dict:
    from processor.boundary_recovery import compute_boundary_recovery_summary
    return compute_boundary_recovery_summary(outcome, fallback_used, fallback_reason)
