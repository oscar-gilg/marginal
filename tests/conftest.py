"""Shared fixtures, and a hard stop between the unit suite and the network.

The unit suite is offline by contract — `pytest.ini` deselects e2e unless
`MARGINAL_E2E=1`. Nothing enforced that, and it mattered: moving the commenter
onto a tool call left one test patching `model.converse` while the code had moved
to `model.exchange`, so a "unit" test opened a real connection, spent real tokens
and took ninety seconds to fail. A patch at the wrong seam should fail loudly and
instantly, which is what this does.
"""

from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("e2e") or os.environ.get("MARGINAL_E2E") == "1":
        return

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a unit test tried to open a socket — something is patched at the wrong "
            "seam, or a stub is missing. The unit suite must not touch the network."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
