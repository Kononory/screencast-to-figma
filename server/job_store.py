import threading
from collections.abc import Callable

from server.models import JobState


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str) -> JobState:
        with self._lock:
            job = JobState(job_id=job_id)
            self._jobs[job_id] = job
            return job

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def require(self, job_id: str) -> JobState:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update(self, job_id: str, updater: Callable[[JobState], None]) -> JobState | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            updater(job)
            job.touch()
            return job

    def all(self) -> list[JobState]:
        with self._lock:
            return list(self._jobs.values())


job_store = JobStore()
