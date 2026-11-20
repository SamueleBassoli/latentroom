"""Check for updates: ONE request to GitHub, only when the user presses
the button in Settings. Never at start, never on a timer; nothing is cached or stored, and only
the version number of the newest release is read from the answer (links come from REPO)."""

import http.client
import json
import logging
import re
import urllib.error
import urllib.request

from latentroom import REPO, __version__
from latentroom.errors import LatentroomError

URL = f"https://api.github.com/repos/{REPO.removeprefix('github.com/')}/releases/latest"
OFFLINE = "Could not reach GitHub. Check your internet connection and try again."
log = logging.getLogger(__name__)


def _version(tag: str) -> tuple[int, ...]:
    """Tag or version to numbers: "v0.2.0" -> (0, 2, 0); anything else raises ValueError."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", tag)
    if not m:
        raise ValueError(f"not a version tag: {tag!r}")
    return tuple(int(x) for x in m[1].split("."))


def _offline(e: Exception) -> LatentroomError:
    log.warning("update check failed: %r", e)
    return LatentroomError("offline", OFFLINE, 502)


def check() -> dict:
    """{"current", "latest", "newer"}. GitHub found no published release (404: none yet, or the
    repo is private, renamed or gone): latest None, newer False, and the page says it does not
    know, never "latest". Offline, timeout, rate limit, an answer that is not a
    release: LatentroomError("offline")."""
    rq = urllib.request.Request(
        URL,
        headers={
            "User-Agent": f"Latentroom/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(rq, timeout=10) as r:
            latest = json.load(r)["tag_name"]
        newer = _version(latest) > _version(__version__)
    except urllib.error.HTTPError as e:  # before OSError: it is one
        if e.code != 404:  # 403/429: rate limit; 5xx: GitHub is down
            raise _offline(e) from e
        return {"current": __version__, "latest": None, "newer": False}
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as e:
        raise _offline(e) from e  # no network, timeout, not JSON
    return {"current": __version__, "latest": latest.removeprefix("v"), "newer": newer}
