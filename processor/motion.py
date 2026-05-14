"""Motion zone detection over the scout-pass frame timeline.

Produces two zone kinds the dense extraction pass can re-sample:

- ``multi_frame_motion_zone`` — a run of scout frames where consecutive phash
  distance stays above ``motion_threshold``. Captures slow transitions and
  animations.
- ``single_frame_anomaly_zone`` — a scout frame whose phash differs strongly
  from *both* neighbours. That is the signature of a state visible for ~one
  scout frame (modal pop, toast, error flash). Today the stability filter
  deletes these; here we mark a small zone around them so the dense pass can
  recover the transient state.

The module knows nothing about ffmpeg or about boundary recovery. It just
takes hashes and timestamps in, returns ``MotionZone`` objects out.
"""

from dataclasses import dataclass
from typing import Any
import os


# Bumped from 4 → 6 in Session 20: catches transitions ≥167ms instead of ≥250ms,
# shrinking the "happens entirely between two scout frames" blind spot.
SCOUT_FPS_DEFAULT = 6


@dataclass
class MotionZoneConfig:
    motion_threshold: int = 8
    anomaly_threshold: int = 12
    smoothing_window: int = 3
    expand_frames: int = 1
    merge_gap_frames: int = 1

    # Duration ladder for dense_fps_for_zone.
    short_zone_ms: int = 400
    medium_zone_ms: int = 1500
    dense_fps_short: int = 30
    dense_fps_medium: int = 15
    dense_fps_long: int = 8


@dataclass
class MotionZone:
    zone_id: str
    start_ms: int | None
    end_ms: int | None
    peak_ms: int | None = None
    peak_score: float | None = None
    zone_type: str = "multi_frame_motion_zone"

    start_index: int | None = None
    end_index: int | None = None

    # Backwards compat with the boundary_recovery placeholder. Filled in by
    # apply_boundary_recovery; not used by motion detection itself.
    extra: dict[str, Any] | None = None


def _zone_id(idx: int) -> str:
    return f"motion_{idx + 1:04d}"


def _ts_for_index(idx: int, fps: int) -> int:
    """Scout-pass timestamp inference: index 0 → 0ms, index 1 → 1000/fps ms, …"""
    if fps <= 0:
        return idx
    return (idx * 1000) // fps


def compute_consecutive_motion(hashes: list) -> list[float]:
    """Return |hash[i+1] - hash[i]| for every consecutive pair. Length N-1."""
    if not hashes or len(hashes) < 2:
        return []
    return [float(abs(hashes[i + 1] - hashes[i])) for i in range(len(hashes) - 1)]


def _smooth(series: list[float], window: int) -> list[float]:
    if window <= 1 or not series:
        return list(series)
    half = window // 2
    out: list[float] = []
    for i in range(len(series)):
        lo = max(0, i - half)
        hi = min(len(series), i + half + 1)
        window_slice = series[lo:hi]
        out.append(sum(window_slice) / len(window_slice))
    return out


def _build_zone(
    indices: list[int],
    motion: list[float],
    fps: int,
    config: MotionZoneConfig,
    zone_type: str,
) -> MotionZone:
    start_idx = max(0, indices[0] - config.expand_frames)
    end_idx = indices[-1] + config.expand_frames
    start_ms = _ts_for_index(start_idx, fps)
    end_ms = _ts_for_index(end_idx + 1, fps)  # inclusive end → +1 frame's ts
    peak_local = max(range(len(indices)), key=lambda k: motion[indices[k]])
    peak_idx = indices[peak_local]
    peak_ms = _ts_for_index(peak_idx, fps)
    peak_score = float(motion[peak_idx])
    return MotionZone(
        zone_id="",  # assigned after sort
        start_ms=start_ms,
        end_ms=end_ms,
        peak_ms=peak_ms,
        peak_score=peak_score,
        zone_type=zone_type,
        start_index=start_idx,
        end_index=end_idx,
    )


def _detect_multi_frame_zones(
    motion: list[float],
    fps: int,
    config: MotionZoneConfig,
) -> list[MotionZone]:
    if not motion:
        return []
    smoothed = _smooth(motion, config.smoothing_window)
    in_zone = False
    current: list[int] = []
    zones: list[MotionZone] = []
    for i, m in enumerate(smoothed):
        if m > config.motion_threshold:
            current.append(i)
            in_zone = True
        else:
            if in_zone and current:
                zones.append(_build_zone(current, motion, fps, config, "multi_frame_motion_zone"))
                current = []
            in_zone = False
    if current:
        zones.append(_build_zone(current, motion, fps, config, "multi_frame_motion_zone"))
    return zones


def _detect_single_frame_anomalies(
    motion: list[float],
    fps: int,
    config: MotionZoneConfig,
) -> list[MotionZone]:
    """Find scout frames with high diff to BOTH neighbours.

    motion[i] = |hash[i+1] - hash[i]|. A "single-frame anomaly" at scout
    frame N means motion[N-1] (= diff with prev) and motion[N] (= diff with
    next) are both above the anomaly_threshold. We build a zone of width
    ±expand_frames around that scout frame.
    """
    if len(motion) < 2:
        return []
    zones: list[MotionZone] = []
    for n in range(1, len(motion)):
        prev_d = motion[n - 1]
        next_d = motion[n]
        if prev_d > config.anomaly_threshold and next_d > config.anomaly_threshold:
            anomaly_scout_index = n
            start_idx = max(0, anomaly_scout_index - config.expand_frames)
            end_idx = anomaly_scout_index + config.expand_frames
            start_ms = _ts_for_index(start_idx, fps)
            end_ms = _ts_for_index(end_idx + 1, fps)
            zones.append(MotionZone(
                zone_id="",
                start_ms=start_ms,
                end_ms=end_ms,
                peak_ms=_ts_for_index(anomaly_scout_index, fps),
                peak_score=float(max(prev_d, next_d)),
                zone_type="single_frame_anomaly_zone",
                start_index=start_idx,
                end_index=end_idx,
            ))
    return zones


def _merge_overlapping(zones: list[MotionZone], fps: int, config: MotionZoneConfig) -> list[MotionZone]:
    if not zones:
        return zones
    zones = sorted(zones, key=lambda z: (z.start_ms if z.start_ms is not None else 0))
    merged: list[MotionZone] = [zones[0]]
    gap_ms = (config.merge_gap_frames * 1000) // max(fps, 1)
    for z in zones[1:]:
        last = merged[-1]
        if (
            z.start_ms is not None
            and last.end_ms is not None
            and z.start_ms - last.end_ms <= gap_ms
        ):
            last.end_ms = max(last.end_ms, z.end_ms or last.end_ms)
            if z.end_index is not None:
                last.end_index = max(last.end_index or 0, z.end_index)
            # Keep the higher-priority zone_type. multi_frame wins ties.
            if last.zone_type == "single_frame_anomaly_zone" and z.zone_type == "multi_frame_motion_zone":
                last.zone_type = "multi_frame_motion_zone"
            if (z.peak_score or 0) > (last.peak_score or 0):
                last.peak_ms = z.peak_ms
                last.peak_score = z.peak_score
        else:
            merged.append(z)
    return merged


def detect_motion_zones(
    scout_hashes: list,
    fps: int = SCOUT_FPS_DEFAULT,
    config: MotionZoneConfig | None = None,
) -> list[MotionZone]:
    """Detect both multi-frame motion zones and single-frame anomaly zones."""
    if config is None:
        config = MotionZoneConfig()
    if not scout_hashes or len(scout_hashes) < 2:
        return []

    motion = compute_consecutive_motion(scout_hashes)
    multi = _detect_multi_frame_zones(motion, fps, config)
    anomaly = _detect_single_frame_anomalies(motion, fps, config)
    merged = _merge_overlapping(multi + anomaly, fps, config)
    for i, z in enumerate(merged):
        z.zone_id = _zone_id(i)
    return merged


def dense_fps_for_zone(zone: MotionZone, config: MotionZoneConfig | None = None) -> int:
    """Duration ladder: <400ms → 30 fps, 400-1500ms → 15 fps, >1500ms → 8 fps."""
    if config is None:
        config = MotionZoneConfig()
    if zone.start_ms is None or zone.end_ms is None:
        return config.dense_fps_medium
    duration_ms = max(1, zone.end_ms - zone.start_ms)
    if duration_ms <= config.short_zone_ms:
        return config.dense_fps_short
    if duration_ms <= config.medium_zone_ms:
        return config.dense_fps_medium
    return config.dense_fps_long


def index_in_any_zone(scout_index: int, zones: list[MotionZone]) -> bool:
    """True if a scout frame at this index falls inside any zone (by index)."""
    for z in zones:
        if z.start_index is None or z.end_index is None:
            continue
        if z.start_index <= scout_index <= z.end_index:
            return True
    return False
