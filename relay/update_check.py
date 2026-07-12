"""Update-notification check for the relay's GUI build.

Polls the repo's root `VERSION` file (the same source of truth the release
workflow already reads/bumps) and compares it against the running build's
baked-in version. Notify-only: no auto-update/auto-apply.
"""

import aiohttp

VERSION_URL = "https://raw.githubusercontent.com/kylegordon/rmonitor/master/VERSION"
RELEASES_URL = "https://github.com/kylegordon/rmonitor/releases"


async def fetch_latest_version(
    session: aiohttp.ClientSession | None = None, url: str = VERSION_URL
) -> str:
    """Fetch and return the stripped contents of the root `VERSION` file."""
    if session is not None:
        return await _fetch(session, url)
    async with aiohttp.ClientSession() as owned_session:
        return await _fetch(owned_session, url)


async def _fetch(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
        resp.raise_for_status()
        return (await resp.text()).strip()


def is_newer(remote: str, current: str) -> bool:
    """Tuple-compare two `MAJOR.MINOR.PATCH` version strings."""

    def _tuple(v: str) -> tuple[int, ...]:
        return tuple(int(part) for part in v.strip().split("."))

    return _tuple(remote) > _tuple(current)
