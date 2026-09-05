"""`.env` file persistence for the relay's GUI build.

Reads/writes the same plain `KEY=value`-per-line format the repo's root
`.env`/`.env.example` already use, so the GUI's config surface stays a
familiar, inspectable file rather than inventing a new format.
"""

import logging
import shutil
from pathlib import Path

import platformdirs

_APP_NAME = "rmonitor-relay"

log = logging.getLogger("relay.env_config")


def default_env_path() -> Path:
    """Where the GUI build's `.env` lives: Roaming AppData on Windows,
    the XDG config dir on Linux.

    `appauthor=False` and `roaming=True` are both load-bearing on Windows.
    platformdirs defaults `roaming` to False (Local AppData) and, when
    `appauthor` is None rather than False, falls back to the appname as the
    author, yielding a doubled `rmonitor-relay\\rmonitor-relay` segment.
    Neither parameter has any effect on the Unix backend.
    """
    config_dir = platformdirs.user_config_dir(_APP_NAME, appauthor=False, roaming=True)
    return Path(config_dir) / ".env"


def legacy_env_path() -> Path:
    """Where releases up to 0.1.13 put the file: platformdirs' defaults,
    i.e. `%LOCALAPPDATA%\\rmonitor-relay\\rmonitor-relay\\.env` on Windows.
    Identical to `default_env_path()` on Linux, where the two parameters
    corrected above are ignored."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / ".env"


def migrate_legacy_env_file() -> None:
    """Move a pre-0.1.14 Windows `.env` to the documented Roaming location.

    Called once at GUI startup, before the file is read. An upgrade would
    otherwise silently lose the operator's feed IP, port, server URL and
    secret, since the corrected path resolves somewhere the old file isn't.
    """
    current, legacy = default_env_path(), legacy_env_path()
    if legacy == current or not legacy.exists() or current.exists():
        return
    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(current))
    except OSError as exc:
        # Startup must not depend on the migration succeeding; the GUI falls
        # back to defaults, which the operator can re-enter and save.
        log.warning("Could not migrate %s to %s: %s", legacy, current, exc)
    else:
        log.info("Migrated relay config from %s to %s", legacy, current)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a `KEY=value`-per-line file. Returns `{}` if it doesn't exist."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def save_env_file(path: Path, updates: dict[str, str]) -> None:
    """Write `updates` into `path`, preserving any existing lines untouched.

    Reads the file first (if present), replaces the value of any line whose
    key matches a key in `updates` in place, and appends keys from `updates`
    that aren't already present. Comments, blank lines, ordering, and any
    hand-added/unknown keys survive verbatim.
    """
    existing_lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    output_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                output_lines.append(f"{key}={remaining.pop(key)}")
                continue
        output_lines.append(line)
    for key, value in remaining.items():
        output_lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output_lines) + "\n" if output_lines else "")
