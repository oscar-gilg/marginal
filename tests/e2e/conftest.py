"""Opt-in end-to-end tests against a real document and a real browser.

Skipped unless MARGINAL_E2E=1 and Chrome is answering on the debug port, so
`pytest` stays offline and instant by default. These exist because the unit tests
can only prove that the mapping logic handles a shape — not that Google sends that
shape, nor that the editor's caret traverses it the way `select_span` assumes.
Both of those fail silently, which is exactly what a test should be for.
"""

from __future__ import annotations

import os
import urllib.request

import pytest

from marginal import auth, config as config_mod
from marginal import gdocs


def _chrome_up(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def cfg():
    if os.environ.get("MARGINAL_E2E") != "1":
        pytest.skip("set MARGINAL_E2E=1 to run end-to-end tests")
    return config_mod.Config()


@pytest.fixture(scope="session")
def token(cfg):
    try:
        return gdocs.access_token(cfg.credentials, account=cfg.account)
    except (FileNotFoundError, auth.AuthError) as e:
        pytest.skip(str(e))


@pytest.fixture(scope="session")
def browser(cfg):
    if not _chrome_up(cfg.port):
        pytest.skip(f"no Chrome on debug port {cfg.port} — run `marginal chrome`")
    return cfg.port


@pytest.fixture(scope="session")
def fixture_doc(request):
    """Build the fixture once per session; reuse an existing one if named.

    MARGINAL_E2E_DOC pins a document so a run can be inspected by hand afterwards
    instead of leaving a new untitled file behind on every invocation.

    `token` is requested lazily rather than taken as a parameter, because building
    the fixture is the only part that needs Google credentials. Declared up front,
    it made *every* test depending on this skip on a machine with no OAuth — even
    ones that only read through the browser, which is the path that exists
    precisely so credentials are unnecessary.
    """
    from tests.e2e import fixture

    existing = os.environ.get("MARGINAL_E2E_DOC")
    if existing:
        return existing
    return fixture.build(request.getfixturevalue("token"))


@pytest.fixture(scope="session")
def pinned_doc(cfg):
    """A document to read without any Google credentials at all.

    Deliberately refuses to build one: creating a document needs the Docs API, and
    the point of these tests is the path that has no access to it. So this is
    opt-in on a document the signed-in Chrome can already open.
    """
    doc = os.environ.get("MARGINAL_E2E_DOC")
    if not doc:
        pytest.skip(
            "set MARGINAL_E2E_DOC to a document this Chrome profile can open, to "
            "exercise the credential-free read path"
        )
    return doc
