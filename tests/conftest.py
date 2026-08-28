"""Fixtures shared by more than one test module."""

import pytest

from labdrivers.core.transport import RecordingTransport
from labdrivers.server.hub import Hub


@pytest.fixture
def hub():
    """A hub holding one lock-in, wired to a recording transport."""
    holder = Hub(config=None)
    transport = RecordingTransport(default="0")
    holder.add("lockin", "Sr830", {"transport": transport})
    transport.clear()
    return holder
