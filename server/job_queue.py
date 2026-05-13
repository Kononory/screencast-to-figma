import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from server.job_store import job_store


@dataclass
class QueuedJob:
    job_id: str
    kind: Literal["url", "file"]
    runner: Callable[[], None]


class LocalJobQueue:
    def __init__(self, max_workers: int = 1) -> None:
        self._queue: queue.Queue[QueuedJob] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()
        self._max_workers = max_workers

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for i in range(self._max_workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"local-job-worker-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)

    def enqueue(self, job: QueuedJob) -> None:
        self._queue.put(job)

    def size(self) -> int:
        return self._queue.qsize()

    def _worker_loop(self) -> None:
        while True:
            queued_job = self._queue.get()
            try:
                queued_job.runner()
            except Exception as exc:
                self._mark_job_error(queued_job.job_id, exc)
            finally:
                self._queue.task_done()

    def _mark_job_error(self, job_id: str, exc: Exception) -> None:
        traceback.print_exc()
        job = job_store.get(job_id)
        if job is None:
            return
        # Pipeline normally catches its own exceptions and sets this shape.
        # This is the safety net for anything that escapes the pipeline.
        if job.status != "error":
            job.status = "error"
            job.add_log(f"Error: {exc}")


local_job_queue = LocalJobQueue(max_workers=1)
