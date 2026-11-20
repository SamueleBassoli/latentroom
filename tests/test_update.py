"""Check for updates: one GitHub request, faked here; no real network in tests."""

import io
import json
import urllib.error

import pytest

from latentroom import __version__
from latentroom.errors import LatentroomError
from latentroom.web import update


def fake(monkeypatch, answer):
    """urlopen gives `answer`: a dict/str body, or raises it when it is an exception."""
    seen = []

    def urlopen(rq, timeout):
        seen.append((rq.full_url, rq.get_header("User-agent"), timeout))
        if isinstance(answer, BaseException):
            raise answer
        return io.BytesIO((answer if isinstance(answer, str) else json.dumps(answer)).encode())

    monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)
    return seen


def http_error(code):
    return urllib.error.HTTPError(update.URL, code, "x", {}, io.BytesIO(b"{}"))


@pytest.mark.parametrize(
    ("tag", "newer"),
    [("v0.2.0", True), (f"v{__version__}", False), (__version__, False), ("v0.0.9", False)],
)
def test_newer_same_older(monkeypatch, tag, newer):
    seen = fake(monkeypatch, {"tag_name": tag, "html_url": "https://evil.example"})
    assert update.check() == {"current": __version__, "latest": tag.lstrip("v"), "newer": newer}
    url = "https://api.github.com/repos/SamueleBassoli/latentroom/releases/latest"
    assert seen == [(url, f"Latentroom/{__version__}", 10)]  # one request, nothing else read


def test_versions_compare_as_numbers(monkeypatch):
    monkeypatch.setattr(update, "__version__", "0.9.0")
    fake(monkeypatch, {"tag_name": "v0.10.0"})
    assert update.check()["newer"] is True  # as text "0.10.0" < "0.9.0"


def test_no_published_release_is_unknown_not_the_latest(monkeypatch):
    """A 404 is also a private, renamed or deleted repo: latest None, which the
    page shows as "could not find", never "You have the latest version" (test_browser)."""
    fake(monkeypatch, http_error(404))
    assert update.check() == {"current": __version__, "latest": None, "newer": False}


@pytest.mark.parametrize(
    "answer",
    [
        TimeoutError("timed out"),
        urllib.error.URLError("no network"),
        http_error(403),  # rate limit
        http_error(500),
        "<html>not json",
        {"message": "API rate limit exceeded"},
        {"tag_name": "nightly"},
    ],
)
def test_offline_or_a_bad_answer_says_could_not_reach(monkeypatch, answer):
    fake(monkeypatch, answer)
    with pytest.raises(LatentroomError) as e:
        update.check()
    assert (e.value.code, e.value.status, e.value.message) == ("offline", 502, update.OFFLINE)
