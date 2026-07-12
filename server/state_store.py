"""State persistence abstraction.

The ``StateStore`` interface decouples the server from any specific storage
backend, making it straightforward to swap ``JsonFileStateStore`` (suitable
for single-node Docker deployments) for an external store such as DynamoDB or
Redis when deploying to stateless compute (e.g. AWS Lambda).
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger(__name__)


class StateStore(ABC):
    """Abstract state persistence interface."""

    @abstractmethod
    def load(self) -> dict:
        """Return persisted state as a dict, or {} if nothing is stored."""

    @abstractmethod
    def save(self, data: dict) -> None:
        """Persist *data* atomically."""


class JsonFileStateStore(StateStore):
    """Persist state as a JSON file.

    Uses a write-to-tmp-then-rename strategy to avoid partial writes.
    Suitable for single-replica Docker deployments only; not safe for
    concurrent writers.

    Saved state is timestamped and discarded on load if older than
    *max_age_seconds*. This exists so a restart shortly after a crash mid-race
    resumes cleanly, while a restart hours later (e.g. the next day, before a
    new race's ``$I`` init message arrives) doesn't merge stale competitors
    from the old race into the new one.

    The timestamp reflects when *data* last actually changed (``data["last_updated"]``,
    maintained by ``RaceState``), not when this method happened to run. ``save()``
    is called unconditionally on a fixed interval regardless of whether the feed
    is still producing new data, so stamping "now" on every write would keep
    resetting the age of genuinely stale data (e.g. a race that ended hours ago
    with the server left running) back to zero, defeating the staleness check
    entirely.
    """

    def __init__(self, path: str | Path, max_age_seconds: float | None = None) -> None:
        self._path = Path(path)
        self._max_age_seconds = max_age_seconds

    def load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load state from %s: %s", self._path, exc)
            return {}
        saved_at = payload.get("saved_at")
        data = payload.get("data", {})
        if self._max_age_seconds is not None and saved_at is not None:
            age = time.time() - saved_at
            if age > self._max_age_seconds:
                log.info(
                    "Discarding stale state from %s (%.0fs old, max age %.0fs)",
                    self._path, age, self._max_age_seconds,
                )
                return {}
        log.info("State restored from %s", self._path)
        return data

    def save(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": data.get("last_updated", time.time()), "data": data}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._path)
        log.debug("State saved to %s", self._path)
