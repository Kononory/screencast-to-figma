import os
import shutil
from pathlib import Path

from processor.pipeline_context import PipelineContext
from processor.pipeline_steps import _log, run_processing_steps
from server.job_store import job_store

_TMP_DIR = Path("tmp")
_OUTPUT_DIR = Path("output")
_SESSIONS_DIR = Path("sessions")


def run_pipeline_from_url(
    job_id: str,
    video_url: str,
    consec_threshold: int = 3,
    global_threshold: int = 3,
) -> None:
    ctx = PipelineContext(
        job_id=job_id,
        source_type="url",
        video_url=video_url,
        video_path=None,
        tmp_dir=_TMP_DIR,
        output_dir=_OUTPUT_DIR,
        sessions_dir=_SESSIONS_DIR,
        consec_threshold=consec_threshold,
        global_threshold=global_threshold,
        classify=True,
        api_key="",
        provider="gemini",
    )
    os.makedirs(str(ctx.job_tmp_dir), exist_ok=True)
    _run_with_context(ctx)


def run_pipeline_from_file(
    job_id: str,
    video_path: str,
    consec_threshold: int = 3,
    global_threshold: int = 3,
    classify: bool = True,
    api_key: str = "",
    provider: str = "gemini",
) -> None:
    ctx = PipelineContext(
        job_id=job_id,
        source_type="file",
        video_url=None,
        video_path=video_path,
        tmp_dir=_TMP_DIR,
        output_dir=_OUTPUT_DIR,
        sessions_dir=_SESSIONS_DIR,
        consec_threshold=consec_threshold,
        global_threshold=global_threshold,
        classify=classify,
        api_key=api_key,
        provider=provider,
    )
    _run_with_context(ctx)


def _run_with_context(ctx: PipelineContext) -> None:
    job = job_store.get(ctx.job_id)
    if job is None:
        return

    job.mark_running()
    try:
        run_processing_steps(ctx, job)
    except Exception as exc:
        job.status = "error"
        _log(job, f"Error: {exc}")
    finally:
        shutil.rmtree(str(ctx.job_tmp_dir), ignore_errors=True)
