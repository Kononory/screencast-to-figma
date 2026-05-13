from collections.abc import Callable

from processor.ai.types import Analysis, Classification
from processor.analyzer import analyze_ux as legacy_analyze_ux
from processor.classifier import classify_frames as legacy_classify_frames


class GeminiProvider:
    name = "gemini"

    def classify_frames(
        self,
        frames: list[str],
        *,
        api_key: str,
        log_fn: Callable[[str], None] | None = None,
        debug_dir: str | None = None,
    ) -> list[Classification]:
        return legacy_classify_frames(frames, api_key, log_fn=log_fn, debug_dir=debug_dir)

    def analyze_ux(
        self,
        frames: list[str],
        *,
        api_key: str,
    ) -> Analysis | None:
        return legacy_analyze_ux(frames, api_key)
