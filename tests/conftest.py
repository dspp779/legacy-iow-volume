"""All tests are offline, including accidental calls to the real upload API."""

import urllib.request

import pytest


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("live HTTP is forbidden in the offline test suite")

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", blocked)
