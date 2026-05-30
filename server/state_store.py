"""State persistence abstraction.

The ``StateStore`` interface decouples the server from any specific storage
backend, making it straightforward to swap ``JsonFileStateStore`` (suitable
for single-node Docker deployments) for an external store such as DynamoDB or
Redis when deploying to stateless compute (e.g. AWS Lambda).
"""

from __future__ import annotations

import json
import logging
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
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            log.info("State restored from %s", self._path)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load state from %s: %s", self._path, exc)
            return {}

    def save(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._path)
        log.debug("State saved to %s", self._path)
