from __future__ import annotations

import secrets
import threading
from collections.abc import Mapping, Sequence


def job_identity(
    job: Mapping[str, object],
    *,
    fields: Sequence[str],
) -> tuple[str, ...]:
    return tuple(str(job.get(field) or "") for field in fields)


class OneShotJobAdmissionAuthority:
    """Process-private, one-shot dispatch tickets bound to one durable job row."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._lock = threading.Lock()

    def issue(self, job_id: str, identity: tuple[str, ...]) -> str:
        if not job_id or not identity or any(not value for value in identity):
            raise ValueError("Worker admission requires one complete durable job identity.")
        token = secrets.token_urlsafe(32)
        with self._lock:
            while token in self._pending:
                token = secrets.token_urlsafe(32)
            self._pending[token] = (job_id, identity)
        return token

    def consume(
        self,
        token: object,
        *,
        job_id: str,
        identity: tuple[str, ...],
    ) -> bool:
        if not isinstance(token, str):
            return False
        with self._lock:
            admitted = self._pending.pop(token, None)
        return admitted == (job_id, identity)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._pending.pop(token, None)
