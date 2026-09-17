"""The stand-in releases API the draft-reader tests are driven through.

The reader's whole point is that it talks to GitHub, so the tests talk
to a local server that behaves the way the releases API does, including
the part that caused the incident: a draft is invisible without push
access, and the API says so with a 404 rather than a 403.

Shared because the reader is exercised from four angles, and a second
copy of this server would be a second set of answers to keep true.
"""

from __future__ import annotations

import http.server
import json
import threading
import typing
from collections.abc import Iterator

import pytest
from audit_draft import Api, ReleasePayload, Retry

TOKEN = "test-token"
REPO = "leynos/df12-dylint-builds"
TAG = "v6.0.4"
ASSET = b"the archive bytes"
#: An API root nothing listens on, so a read that escapes the injected
#: transport fails rather than being quietly answered.
UNREACHABLE = "http://127.0.0.1:1"


def api_for(root: str, *, attempts: int = 4) -> Api:
    """Return an `Api` pointed at the stand-in server, with no backoff.

    A real backoff would make every retry test wait out the policy the
    release uses, so the tests set it to zero and vary only the attempt
    count.

    Parameters
    ----------
    root:
        The stand-in API's root.
    attempts:
        How many times a read may be tried.

    Returns
    -------
    Api
        The client configuration.
    """
    return Api(token=TOKEN, root=root, retry=Retry(attempts=attempts, backoff=0))


class ApiHandler(http.server.BaseHTTPRequestHandler):
    """A GitHub release API, as much of it as this script reads.

    The behaviour is set per test on the class rather than passed in,
    because `http.server` builds a handler per request and there is
    nowhere else to put it.
    """

    #: Status to answer the release lookup with, or None for the release.
    release_status: typing.ClassVar[int | None] = None
    #: The release body to serve.
    release: typing.ClassVar[ReleasePayload] = {}
    #: Paths already asked for, so a flaky answer can succeed on retry.
    seen: typing.ClassVar[set[str]] = set()
    #: How the asset endpoint should behave.
    asset_mode: typing.ClassVar[str] = "ok"
    #: Release paths already asked for, so the lookup can be flaky too.
    release_seen: typing.ClassVar[set[str]] = set()
    #: Status to answer the first release lookup with, then serve the
    #: release. `None` leaves `release_status` in charge.
    release_first_status: typing.ClassVar[int | None] = None
    #: Every release path asked for, in order, so a test can read back
    #: the URL the script built.
    release_paths: typing.ClassVar[list[str]] = []
    #: Every request's path and headers, in order. The audit exists
    #: because a read without push access sees nothing, so what it sends
    #: is the mechanism and has to be assertable.
    seen_headers: typing.ClassVar[list[tuple[str, dict[str, str]]]] = []
    #: A release body to serve verbatim, bypassing the JSON encoding.
    #: `None` serves `release` encoded. This exists so a response that
    #: is not JSON at all can be served: a body built by `json.dumps`
    #: always decodes, so the reader's malformed-payload path could not
    #: otherwise be reached through the API a caller actually uses.
    release_raw: typing.ClassVar[bytes | None] = None

    def do_GET(self) -> None:
        """Serve the release lookup or an asset body.

        A request without the bearer token is answered 404, which is
        what GitHub does with a draft release: invisible rather than
        forbidden. Modelling that here is what makes the token load
        bearing in every test, instead of only in the ones that name it.
        """
        is_release = "/releases/tags/" in self.path
        # Lower-cased keys: `urllib` capitalizes header names when it
        # builds a request, so it sends `X-github-api-version`. HTTP
        # header names are case-insensitive and `self.headers` honours
        # that, but a plain `dict` of it does not, and a lookup by the
        # documented spelling would miss.
        self.seen_headers.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()})
        )
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            self.send_error(404, "no such release")
            return
        if is_release:
            self._serve_release()
            return
        self._serve_asset()

    def _serve_release(self) -> None:
        """Answer the release-by-tag lookup."""
        self.release_paths.append(self.path)
        if self.release_first_status is not None and self.path not in self.release_seen:
            self.release_seen.add(self.path)
            self.send_error(self.release_first_status, "not yet")
            return
        if self.release_status is not None:
            self.send_error(self.release_status, "no")
            return
        body = (
            self.release_raw
            if self.release_raw is not None
            else json.dumps(self.release).encode()
        )
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self) -> None:
        """Answer an asset download according to `asset_mode`."""
        if self.asset_mode == "permanent":
            self.send_error(403, "forbidden")
            return
        if self.asset_mode == "flaky" and self.path not in self.seen:
            self.seen.add(self.path)
            self.send_error(503, "try again")
            return
        if self.asset_mode == "truncated":
            self.send_response(200)
            self.send_header("Content-Length", "4096")
            self.end_headers()
            self.wfile.write(b"short")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(ASSET)))
        self.end_headers()
        self.wfile.write(ASSET)

    def log_message(self, *args: object) -> None:
        """Discard the access log."""


@pytest.fixture
def api() -> Iterator[str]:
    """Serve the stand-in API and yield its root, resetting it after."""
    ApiHandler.release_status = None
    ApiHandler.release = {}
    ApiHandler.seen = set()
    ApiHandler.asset_mode = "ok"
    ApiHandler.release_seen = set()
    ApiHandler.release_first_status = None
    ApiHandler.release_paths = []
    ApiHandler.seen_headers = []
    ApiHandler.release_raw = None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def release_body(root: str, *names: str) -> ReleasePayload:
    """Return a release body naming assets served by this API.

    Parameters
    ----------
    root:
        The API root.
    *names:
        Asset names.

    Returns
    -------
    ReleasePayload
        The release body.
    """
    return {
        "tag_name": TAG,
        "draft": True,
        "assets": [
            {"name": name, "url": f"{root}/repos/{REPO}/releases/assets/{index}"}
            for index, name in enumerate(names)
        ],
    }
