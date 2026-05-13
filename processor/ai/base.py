from collections.abc import Callable
from typing import Protocol

from processor.ai.types import Analysis, Classification


class AIProvider(Protocol):
    name: str

    def classify_frames(
        self,
        frames: list[str],
        *,
        api_key: str,
        log_fn: Callable[[str], None] | None = None,
        debug_dir: str | None = None,
    ) -> list[Classification]:
        ...

    def analyze_ux(
        self,
        frames: list[str],
        *,
        api_key: str,
    ) -> Analysis | None:
        ...
