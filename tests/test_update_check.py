"""Tests for relay/update_check.py's version-compare and fetch logic.

Follows the FakeSession/FakeResponse pattern already used in
tests/test_relay_main.py so no real network call is made.
"""

import pytest

from relay import update_check


class FakeResponse:
    def __init__(self, text, status=200):
        self._text = text
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response_text, status=200):
        self._response_text = response_text
        self._status = status
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return FakeResponse(self._response_text, self._status)


def test_is_newer_true_when_remote_patch_is_higher():
    assert update_check.is_newer("0.1.9", "0.1.8") is True


def test_is_newer_false_when_equal():
    assert update_check.is_newer("0.1.8", "0.1.8") is False


def test_is_newer_false_when_remote_is_older():
    assert update_check.is_newer("0.1.7", "0.1.8") is False


def test_is_newer_raises_on_non_numeric_current_version():
    """Documents current behavior: a non-numeric current version (e.g. the
    "0.0.0-dev" fallback used by an unbuilt/dev relay/gui.py run, since
    relay/_version.py only exists in a PyInstaller build) raises ValueError
    rather than silently comparing as not-newer. Callers must guard this."""
    with pytest.raises(ValueError):
        update_check.is_newer("0.1.9", "0.0.0-dev")


@pytest.mark.asyncio
async def test_fetch_latest_version_returns_stripped_body():
    session = FakeSession("0.1.9\n")
    result = await update_check.fetch_latest_version(session)
    assert result == "0.1.9"
    assert session.calls == 1


@pytest.mark.asyncio
async def test_fetch_latest_version_raises_on_error_status():
    session = FakeSession("Internal Server Error", status=500)
    with pytest.raises(RuntimeError):
        await update_check.fetch_latest_version(session)
