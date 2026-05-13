from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    job_id: str
    status: Literal["queued", "running", "done", "error"] = "queued"
    progress: int = 0
    step: str = ""
    log: list[str] = field(default_factory=list)
    manifest_path: str | None = None
    extracted: int = 0
    dupes: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def add_log(self, message: str) -> None:
        self.log.append(message)
        self.touch()

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)
        self.add_log(f"Warning: {message}")

    def set_progress(self, progress: int, step: str | None = None) -> None:
        self.progress = progress
        if step is not None:
            self.step = step
        self.touch()

    def mark_running(self, step: str = "") -> None:
        self.status = "running"
        if step:
            self.step = step
        self.touch()

    def mark_done(self, manifest_path: str | None = None, step: str = "Done") -> None:
        self.status = "done"
        self.progress = 100
        self.step = step
        if manifest_path is not None:
            self.manifest_path = manifest_path
        self.touch()

    def mark_error(self, error: str) -> None:
        self.status = "error"
        self.error = error
        self.add_log(f"Error: {error}")

    def to_status_response(self) -> dict:
        response = {
            "status": self.status,
            "log": self.log,
            "progress": self.progress,
            "step": self.step,
            "extracted": self.extracted,
            "dupes": self.dupes,
        }
        if self.error:
            response["error"] = self.error
        if self.warnings:
            response["warnings"] = self.warnings
        return response
