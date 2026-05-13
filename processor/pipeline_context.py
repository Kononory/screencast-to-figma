from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class PipelineContext:
    job_id: str
    source_type: Literal["url", "file"]
    video_url: str | None
    video_path: str | None

    tmp_dir: Path
    output_dir: Path
    sessions_dir: Path

    consec_threshold: int
    global_threshold: int

    classify: bool
    api_key: str
    provider: str

    @property
    def job_output_dir(self) -> Path:
        return self.output_dir / self.job_id

    @property
    def job_tmp_dir(self) -> Path:
        return self.tmp_dir / self.job_id

    @property
    def session_path(self) -> Path:
        return self.sessions_dir / f"{self.job_id}.json"


@dataclass
class PipelineResult:
    manifest_path: str
    extracted: int
    dupes: int
