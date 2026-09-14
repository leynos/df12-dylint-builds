"""Tests for reading back a draft release's assets before the audit.

The audit's whole value is that it reads GitHub rather than the runner,
and a draft is visible only to a token with push access. Every decision
that used to live in a shell loop is exercised here against a local
server standing in for the API: what a 404 means when the release
plainly exists, what a retryable status is worth, what a permanent one
is worth, a truncated body, an empty release, and an asset name that
tries to leave the directory.
"""

from __future__ import annotations

import http.server
import json
import threading
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from audit_draft import Retry, assets_of, download_assets, main, release_for_tag
from package import PackagingError

TOKEN = "test-token"
REPO = "leynos/df12-dylint-builds"
TAG = "v6.0.4"
ASSET = b"the archive bytes"


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    """A GitHub release API, as much of it as this script reads.

    The behaviour is set per test on the class rather than passed in,
    because `http.server` builds a handler per request and there is
    nowhere else to put it.
    """

    #: Status to answer the release lookup with, or None for the release.
    release_status: typing.ClassVar[int | None] = None
    #: The release body to serve.
    release: typing.ClassVar[dict] = {}
    #: Paths already asked for, so a flaky answer can succeed on retry.
    seen: typing.ClassVar[set[str]] = set()
    #: How the asset endpoint should behave.
    asset_mode: typing.ClassVar[str] = "ok"

    def do_GET(self) -> None:
        """Serve the release lookup or an asset body."""
        if "/releases/tags/" in self.path:
            self._serve_release()
            return
        self._serve_asset()

    def _serve_release(self) -> None:
        """Answer the release-by-tag lookup."""
        if self.release_status is not None:
            self.send_error(self.release_status, "no")
            return
        body = json.dumps(self.release).encode()
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
    _ApiHandler.release_status = None
    _ApiHandler.release = {}
    _ApiHandler.seen = set()
    _ApiHandler.asset_mode = "ok"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _release(root: str, *names: str) -> dict:
    """Return a release body naming assets served by this API.

    Parameters
    ----------
    root:
        The API root.
    *names:
        Asset names.

    Returns
    -------
    dict
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


class TestReadingTheRelease:
    """Finding the draft, or failing in a way that says why."""

    def test_a_draft_is_returned_whole(self, api: str) -> None:
        """A readable draft comes back as the release object."""
        _ApiHandler.release = _release(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, TOKEN, api=api)

        assert release["tag_name"] == TAG, "the release must come back intact"

    def test_a_404_says_what_it_usually_means_here(self, api: str) -> None:
        """A 404 is reported as the under-privileged-token case.

        The API answers the same way for a release that does not exist
        and for a draft the token cannot see. The second is the failure
        that lost run 34208473988, so the message names it rather than
        leaving the reader to guess.
        """
        _ApiHandler.release_status = 404

        with pytest.raises(PackagingError, match="push access"):
            release_for_tag(REPO, TAG, TOKEN, api=api)

    def test_another_status_is_reported_with_its_code(self, api: str) -> None:
        """A non-404 failure names the status rather than guessing."""
        _ApiHandler.release_status = 500

        with pytest.raises(PackagingError, match="HTTP 500"):
            release_for_tag(REPO, TAG, TOKEN, api=api)


class TestTheAssetList:
    """An empty release is a failure, not a clean sheet."""

    def test_an_empty_release_is_refused(self) -> None:
        """No assets means the audit would check nothing.

        An audit over an empty directory passes every check it is given,
        which is indistinguishable from success and is the outcome the
        audit exists to prevent.
        """
        with pytest.raises(PackagingError, match="no assets"):
            assets_of({"assets": []})

    def test_a_missing_asset_list_is_refused(self) -> None:
        """A release with no asset list at all is equally unusable."""
        with pytest.raises(PackagingError, match="no asset list"):
            assets_of({})


class TestDownloadingTheAssets:
    """What the retry is worth, and what it is not."""

    def test_every_asset_is_written(self, api: str, tmp_path: Path) -> None:
        """Each named asset arrives in the destination directory."""
        _ApiHandler.release = _release(api, "a.tar.gz", "a.tar.gz.sha256")

        written = download_assets(
            _ApiHandler.release, tmp_path / "dist", TOKEN, retry=Retry(backoff=0)
        )

        assert [path.name for path in written] == ["a.tar.gz", "a.tar.gz.sha256"], (
            "every asset the release lists must be written, in its order"
        )
        assert written[0].read_bytes() == ASSET, "the body must be written verbatim"

    def test_a_retryable_status_is_retried_and_then_succeeds(
        self, api: str, tmp_path: Path
    ) -> None:
        """A 503 on the first attempt does not fail the audit.

        Transient failures are why the retry exists; the shell loop it
        replaces could not be tested for this.
        """
        _ApiHandler.asset_mode = "flaky"
        _ApiHandler.release = _release(api, "a.tar.gz")

        written = download_assets(
            _ApiHandler.release, tmp_path / "dist", TOKEN, retry=Retry(backoff=0)
        )

        assert written[0].read_bytes() == ASSET, "the retry must serve the real bytes"

    def test_a_permanent_status_is_not_retried(self, api: str, tmp_path: Path) -> None:
        """A 403 fails at once rather than four times.

        Asking again cannot change a permission answer, and retrying
        only delays the failure by the whole backoff.
        """
        _ApiHandler.asset_mode = "permanent"
        _ApiHandler.release = _release(api, "a.tar.gz")

        with pytest.raises(PackagingError, match="HTTP 403"):
            download_assets(
                _ApiHandler.release, tmp_path / "dist", TOKEN, retry=Retry(backoff=0)
            )

    def test_a_truncated_body_is_never_written(self, api: str, tmp_path: Path) -> None:
        """A short body fails rather than landing as an archive.

        A truncated asset written to disk would be audited as a corrupt
        archive, which reads as a build fault rather than a transfer one.
        """
        _ApiHandler.asset_mode = "truncated"
        _ApiHandler.release = _release(api, "a.tar.gz")

        with pytest.raises(PackagingError, match="attempts"):
            download_assets(
                _ApiHandler.release,
                tmp_path / "dist",
                TOKEN,
                retry=Retry(attempts=2, backoff=0),
            )
        assert not (tmp_path / "dist" / "a.tar.gz").exists(), (
            "nothing may be written unless the whole body arrived"
        )

    def test_an_escaping_asset_name_is_refused(self, api: str, tmp_path: Path) -> None:
        """A name that climbs out of the directory is rejected.

        The name comes from the release rather than from this
        repository, so it is not trusted to stay where it is put.
        """
        _ApiHandler.release = _release(api, "../escaped.tar.gz")

        with pytest.raises(PackagingError, match="outside"):
            download_assets(
                _ApiHandler.release, tmp_path / "dist", TOKEN, retry=Retry(backoff=0)
            )


class TestTheCommandLine:
    """The entry point the workflow step invokes."""

    def test_a_readable_draft_exits_zero(self, api: str, tmp_path: Path) -> None:
        """A successful download reports how much it wrote."""
        _ApiHandler.release = _release(api, "a.tar.gz")

        code = main(
            [
                "--repo",
                REPO,
                "--tag",
                TAG,
                "--token",
                TOKEN,
                "--dir",
                str(tmp_path / "dist"),
                "--api",
                api,
            ]
        )

        assert code == 0, "a readable draft must succeed"
        assert (tmp_path / "dist" / "a.tar.gz").exists(), "the asset must be written"

    def test_an_invisible_draft_exits_one(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A 404 fails the step and says why on the error channel."""
        _ApiHandler.release_status = 404

        code = main(
            [
                "--repo",
                REPO,
                "--tag",
                TAG,
                "--token",
                TOKEN,
                "--dir",
                str(tmp_path / "dist"),
                "--api",
                api,
            ]
        )

        assert code == 1, "an unreadable draft must fail the step"
        assert "push access" in capsys.readouterr().err, (
            "the failure must name the permission it needs"
        )
