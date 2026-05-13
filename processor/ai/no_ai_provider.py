from collections.abc import Callable

from processor.ai.types import Analysis, Classification


class NoAIProvider:
    name = "none"

    def classify_frames(
        self,
        frames: list[str],
        *,
        api_key: str,
        log_fn: Callable[[str], None] | None = None,
        debug_dir: str | None = None,
    ) -> list[Classification]:
        return []

    def analyze_ux(
        self,
        frames: list[str],
        *,
        api_key: str,
    ) -> Analysis | None:
        return None
