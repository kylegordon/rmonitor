"""`.env` file persistence for the relay's GUI build.

Reads/writes the same plain `KEY=value`-per-line format the repo's root
`.env`/`.env.example` already use, so the GUI's config surface stays a
familiar, inspectable file rather than inventing a new format.
"""

from pathlib import Path

import platformdirs

_APP_NAME = "rmonitor-relay"


def default_env_path() -> Path:
    """Where the GUI build's `.env` lives: Roaming AppData on Windows,
    the XDG config dir on Linux."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / ".env"


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
