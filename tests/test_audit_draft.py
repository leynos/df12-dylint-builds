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
from audit_draft import (
    API_VERSION,
    DEFAULT_API,
    METRIC_PREFIX,
    Api,
    Outcome,
    ReleasePayload,
    Retry,
    asset_target,
    assets_of,
    download_assets,
    latency_bucket,
    main,
    release_for_tag,
    release_from_payload,
    release_url,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from package import PackagingError
from verify_upstream import USER_AGENT

TOKEN = "test-token"
REPO = "leynos/df12-dylint-builds"
TAG = "v6.0.4"
ASSET = b"the archive bytes"
#: An API root nothing listens on, so a read that escapes the injected
#: transport fails rather than being quietly answered.
UNREACHABLE = "http://127.0.0.1:1"


def _api(root: str, *, attempts: int = 4) -> Api:
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


class _ApiHandler(http.server.BaseHTTPRequestHandler):
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

    def do_GET(self) -> None:
        """Serve the release lookup or an asset body.

        A request without the bearer token is answered 404, which is
        what GitHub does with a draft release: invisible rather than
        forbidden. Modelling that here is what makes the token load
        bearing in every test, instead of only in the ones that name it.
        """
        is_release = "/releases/tags/" in self.path
        # Lower-cased keys: `urllib` capitalises header names when it
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
    _ApiHandler.release_seen = set()
    _ApiHandler.release_first_status = None
    _ApiHandler.release_paths = []
    _ApiHandler.seen_headers = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _release(root: str, *names: str) -> ReleasePayload:
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


class TestReadingTheRelease:
    """Finding the draft, or failing in a way that says why."""

    def test_a_draft_is_returned_whole(self, api: str) -> None:
        """A readable draft comes back as the release object."""
        _ApiHandler.release = _release(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, _api(api))

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
            release_for_tag(REPO, TAG, _api(api))

    def test_a_retryable_status_is_retried_and_then_succeeds(self, api: str) -> None:
        """A 500 on the first lookup does not fail the release.

        Before the retry a single transient status failed the audit
        outright, and the asset retry never ran, because there was no
        release to take assets from.
        """
        _ApiHandler.release_first_status = 500
        _ApiHandler.release = _release(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, _api(api))

        assert release["tag_name"] == TAG, "the second attempt must be believed"

    def test_a_404_on_the_first_lookup_is_retried(self, api: str) -> None:
        """A 404 is retried, because the draft is created just before.

        The workflow creates the release in `create-release` and reads
        it back in `audit`, so the first read can precede GitHub's own
        consistency. A permanently invisible draft still fails, after
        the whole retry policy has been spent on it.
        """
        _ApiHandler.release_first_status = 404
        _ApiHandler.release = _release(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, _api(api))

        assert release["tag_name"] == TAG, "a 404 that clears must not fail the run"

    def test_a_permanent_status_is_not_retried(self, api: str) -> None:
        """A 403 fails at once and names its code.

        Asking a permission answer again cannot change it, so retrying
        only delays the failure by the whole backoff.
        """
        _ApiHandler.release_status = 403

        with pytest.raises(PackagingError, match="HTTP 403"):
            release_for_tag(REPO, TAG, _api(api))

        assert len(_ApiHandler.release_paths) == 1, (
            "a permanent status must be read once, not once per attempt; "
            f"read {_ApiHandler.release_paths}"
        )

    def test_a_status_that_never_clears_reports_the_attempts(self, api: str) -> None:
        """An exhausted retry says how many times it tried.

        A failure that took four attempts and one that took one read
        identically otherwise, and the difference is what tells a
        maintainer whether the API was unwell or the token was wrong.
        """
        _ApiHandler.release_status = 500

        with pytest.raises(PackagingError, match="after 2 attempts"):
            release_for_tag(REPO, TAG, _api(api, attempts=2))

    def test_a_tag_with_a_slash_stays_one_path_segment(self, api: str) -> None:
        """A `/` in the tag is encoded rather than splitting the path.

        `urllib.parse.quote` keeps `/` by default, so `release/6.0`
        would address `releases/tags/release/6.0` and come back as a
        not-found, which reads here as the under-privileged-token case
        it is not.
        """
        _ApiHandler.release = _release(api, "a.tar.gz")

        release_for_tag(REPO, "release/6.0", _api(api))

        assert _ApiHandler.release_paths[0].endswith("/releases/tags/release%2F6.0"), (
            "the tag must be one encoded path segment; got "
            f"{_ApiHandler.release_paths[0]}"
        )


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

    def test_an_entry_that_is_not_an_object_is_refused(self) -> None:
        """A non-object entry fails here rather than at the field read.

        Left to `_required_text` it raises an AttributeError, which
        names neither the release nor the entry, and reads as a fault in
        this script rather than in what the API returned.
        """
        with pytest.raises(PackagingError, match="where an asset object belongs"):
            assets_of({"assets": ["a.tar.gz"]})


class TestDownloadingTheAssets:
    """What the retry is worth, and what it is not."""

    def test_every_asset_is_written(self, api: str, tmp_path: Path) -> None:
        """Each named asset arrives in the destination directory."""
        _ApiHandler.release = _release(api, "a.tar.gz", "a.tar.gz.sha256")

        written = download_assets(_ApiHandler.release, tmp_path / "dist", _api(api))

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

        written = download_assets(_ApiHandler.release, tmp_path / "dist", _api(api))

        assert written[0].read_bytes() == ASSET, "the retry must serve the real bytes"

    def test_a_permanent_status_is_not_retried(self, api: str, tmp_path: Path) -> None:
        """A 403 fails at once rather than four times.

        Asking again cannot change a permission answer, and retrying
        only delays the failure by the whole backoff.
        """
        _ApiHandler.asset_mode = "permanent"
        _ApiHandler.release = _release(api, "a.tar.gz")

        with pytest.raises(PackagingError, match="HTTP 403"):
            download_assets(_ApiHandler.release, tmp_path / "dist", _api(api))

    def test_a_truncated_body_is_never_written(self, api: str, tmp_path: Path) -> None:
        """A short body fails rather than landing as an archive.

        A truncated asset written to disk would be audited as a corrupt
        archive, which reads as a build fault rather than a transfer one.
        """
        _ApiHandler.asset_mode = "truncated"
        _ApiHandler.release = _release(api, "a.tar.gz")

        with pytest.raises(PackagingError, match="attempts"):
            download_assets(
                _ApiHandler.release, tmp_path / "dist", _api(api, attempts=2)
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
            download_assets(_ApiHandler.release, tmp_path / "dist", _api(api))


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
                "--retry-backoff",
                "0",
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
                "--retry-backoff",
                "0",
            ]
        )

        assert code == 1, "an unreadable draft must fail the step"
        assert "push access" in capsys.readouterr().err, (
            "the failure must name the permission it needs"
        )


class TestWhatTheRequestsCarry:
    """The headers are the fix, so they are asserted rather than assumed.

    A draft release is invisible without push access, which is what lost
    run 34208473988. The token, and the media types that decide whether
    an asset arrives as bytes or as its own metadata, are the mechanism
    this whole script exists to get right. Before these, deleting the
    `Authorization` header outright left all 130 tests passing.
    """

    @staticmethod
    def _headers_for(fragment: str) -> dict[str, str]:
        """Return the headers of the first request whose path holds *fragment*.

        Parameters
        ----------
        fragment:
            A distinguishing part of the path.

        Returns
        -------
        dict[str, str]
            That request's headers.
        """
        for path, headers in _ApiHandler.seen_headers:
            if fragment in path:
                return headers
        message = f"no request was made to a path containing {fragment!r}"
        raise AssertionError(message)

    def test_the_release_lookup_carries_the_token_and_the_json_media_type(
        self, api: str
    ) -> None:
        """The lookup authenticates and asks for the API's JSON.

        Without the bearer token the API reports a draft as not found,
        which is indistinguishable from a release that does not exist.
        """
        _ApiHandler.release = _release(api, "a.tar.gz")

        release_for_tag(REPO, TAG, _api(api))

        headers = self._headers_for("/releases/tags/")
        assert headers.get("authorization") == f"Bearer {TOKEN}", (
            "the lookup must send the bearer token, or a draft is invisible: "
            f"{headers.get('authorization')!r}"
        )
        assert headers.get("accept") == "application/vnd.github+json", (
            f"the lookup must ask for the API's JSON: {headers.get('accept')!r}"
        )

    def test_an_asset_download_asks_for_bytes_rather_than_metadata(
        self, api: str, tmp_path: Path
    ) -> None:
        """The download asks for the octet stream, not the asset object.

        Any other media type returns the asset's metadata again, which
        would be written out as though it were an archive and audited as
        a corrupt one.
        """
        _ApiHandler.release = _release(api, "a.tar.gz")

        download_assets(_ApiHandler.release, tmp_path / "dist", _api(api))

        headers = self._headers_for("/releases/assets/")
        assert headers.get("accept") == "application/octet-stream", (
            "an asset body needs the octet stream; anything else returns the "
            f"metadata again: {headers.get('accept')!r}"
        )
        assert headers.get("authorization") == f"Bearer {TOKEN}", (
            "a draft's assets need the token as much as the draft does: "
            f"{headers.get('authorization')!r}"
        )

    def test_every_request_names_this_tool_and_the_api_version(
        self, api: str, tmp_path: Path
    ) -> None:
        """Both endpoints send the user agent and the API version.

        The version pins the response shape this script was written
        against, so a future default cannot change it silently, and the
        user agent is what GitHub's rate limiting attributes the reads
        to.
        """
        _ApiHandler.release = _release(api, "a.tar.gz")

        download_assets(
            release_for_tag(REPO, TAG, _api(api)), tmp_path / "dist", _api(api)
        )

        for fragment in ("/releases/tags/", "/releases/assets/"):
            headers = self._headers_for(fragment)
            assert headers.get("user-agent") == USER_AGENT, (
                f"{fragment} must name this tool: {headers.get('user-agent')!r}"
            )
            assert headers.get("x-github-api-version") == API_VERSION, (
                f"{fragment} must pin the API version: "
                f"{headers.get('x-github-api-version')!r}"
            )


class _RefusingTransport:
    """A transport that fails the test if anything asks it to read.

    The architecture claim is that the query functions reach no network.
    A stand-in server cannot state that, because a function that never
    calls it looks exactly like one that calls it and is answered. This
    can: it is the absence of a call, made assertable.
    """

    def __call__(self, url: str, headers: typing.Mapping[str, str]) -> bytes:
        """Fail, naming the address the caller should not have read."""
        message = f"a query path opened a connection to {url}"
        raise AssertionError(message)


class _TabledTransport:
    """A transport that answers from a fixed body and records the reads.

    The counterpart to `_RefusingTransport`: where that one proves a
    path reaches no network, this proves the boundary reaches the
    network only through what it was handed. Without it, injecting the
    transport and calling `urllib` directly are indistinguishable,
    because the tests' stand-in server answers either one.
    """

    def __init__(self, body: bytes) -> None:
        """Answer every read with ``body``."""
        self.body = body
        self.urls: list[str] = []

    def __call__(self, url: str, headers: typing.Mapping[str, str]) -> bytes:
        """Record ``url`` and return the body."""
        self.urls.append(url)
        return self.body


class _RecordingSleeper:
    """A sleeper that records what it was asked to wait, and waits none.

    The retry policy's waits are a decision this module makes, so they
    are read back rather than served out.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.waits: list[float] = []

    def __call__(self, seconds: float, /) -> None:
        """Record ``seconds`` instead of waiting it."""
        self.waits.append(seconds)


class TestTheQueryPathsTouchNothing:
    """`release_url` and `release_from_payload` are pure, and it is proved.

    Both were inside `release_for_tag`, which opens a socket, so the
    quoting and the payload check could only be exercised through one.
    Splitting them out is worth nothing unless the split is asserted,
    and a test that merely calls them through a working server would
    pass just as well before the split as after it.
    """

    def test_the_address_is_built_without_reading_anything(self) -> None:
        """Building the lookup address is arithmetic on strings.

        It takes no `Api` at all, which is the strongest form the claim
        has: there is nothing to read through even in principle.
        """
        url = release_url(REPO, TAG)

        assert url == f"{DEFAULT_API}/repos/{REPO}/releases/tags/{TAG}", url

    def test_a_tag_with_a_slash_stays_one_path_segment(self) -> None:
        """A tag is one segment, so its separator must be escaped.

        Unescaped, ``release/6.0`` addresses a release named ``6.0``
        under a directory ``release``, and the API's not-found reads
        here as the under-privileged-token case, which is the one
        failure this whole script exists to tell apart.
        """
        url = release_url(REPO, "release/6.0")

        assert url.endswith("/releases/tags/release%2F6.0"), url

    def test_a_payload_check_reads_no_network_and_no_disk(self, tmp_path: Path) -> None:
        """Deciding what a decoded body is needs neither of them."""
        before = sorted(tmp_path.iterdir())

        release = release_from_payload({"assets": [{"name": "a"}]}, subject="s")

        assert release == {"assets": [{"name": "a"}]}, release
        assert sorted(tmp_path.iterdir()) == before, "the query wrote a file"

    def test_a_list_body_is_named_for_what_it_is(self) -> None:
        """An array reaching `assets_of` fails on the wrong thing."""
        with pytest.raises(PackagingError, match="returned list, not a release"):
            release_from_payload([], subject="a release")

    def test_the_boundary_reads_only_through_the_transport_it_was_given(
        self,
    ) -> None:
        """The lookup goes through the injected transport, not around it.

        The root points at a port nothing listens on, so a read that
        reached `urllib` would fail rather than quietly agree. This is
        what makes the injection load bearing: with the stand-in server
        answering, calling the transport and calling `urllib` directly
        are the same observation.
        """
        transport = _TabledTransport(json.dumps({"assets": []}).encode())
        client = Api(token=TOKEN, root=UNREACHABLE, transport=transport)

        release = release_for_tag(REPO, TAG, client)

        assert release == {"assets": []}, release
        assert transport.urls == [release_url(REPO, TAG, UNREACHABLE)], transport.urls


class TestTheRetryWaitsAreReadBackRatherThanServed:
    """The backoff is injected, so its schedule can be asserted.

    Before the sleeper was injected the only evidence that a retry
    waited at all was that the suite was slow, and the suite set the
    backoff to zero precisely so that it would not be.
    """

    def test_the_wait_grows_with_the_attempt(self, api: str) -> None:
        """Each failed attempt waits longer than the one before it.

        A flat wait retries a rate limit at the rate that caused it.
        """
        sleeper = _RecordingSleeper()
        _ApiHandler.release_status = 503
        client = Api(
            token=TOKEN,
            root=api,
            retry=Retry(attempts=3, backoff=2),
            sleeper=sleeper,
        )

        with pytest.raises(PackagingError):
            release_for_tag(REPO, TAG, client)

        assert sleeper.waits == [2.0, 4.0], (
            f"two failures before the last attempt, growing: {sleeper.waits}"
        )

    def test_a_read_that_succeeds_first_time_never_waits(self, api: str) -> None:
        """Nothing is slept on the happy path."""
        sleeper = _RecordingSleeper()
        _ApiHandler.release = _release(api, "a.tar.gz")
        client = Api(token=TOKEN, root=api, retry=Retry(attempts=3), sleeper=sleeper)

        release_for_tag(REPO, TAG, client)

        assert sleeper.waits == [], sleeper.waits


#: Roots that leave a directory outright, whatever follows them. An
#: absolute name discards the directory it was joined to, so these need
#: no climbing to escape.
ABSOLUTE_ROOTS = ("/etc", "/", "/tmp/elsewhere")

#: Characters a release asset's name is allowed to be built from, for the
#: names that are meant to be accepted.
SAFE_ALPHABET = st.characters(whitelist_categories=("Ll", "Lu", "Nd"))


@settings(max_examples=100, deadline=None)
@given(
    leaf=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
    suffix=st.sampled_from(["", ".tar.gz", ".zip", ".sha256"]),
)
def test_a_name_that_stays_put_is_written_inside_the_destination(
    tmp_path_factory: pytest.TempPathFactory, leaf: str, suffix: str
) -> None:
    """A plain asset name resolves to a path under the download directory.

    The containment rule is only worth having if it also lets the
    ordinary case through. A check that refused every name would satisfy
    the escape property on its own, so the two are asserted together:
    this is the half that says the rule is narrow.
    """
    destination = tmp_path_factory.mktemp("assets")
    name = f"{leaf}{suffix}"

    _, target = asset_target({"name": name, "url": "https://example/x"}, destination)

    assert target.is_relative_to(destination.resolve()), target
    assert target.name == name, target


@settings(max_examples=150, deadline=None)
@given(
    data=st.data(),
    leaf=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
    depth=st.integers(min_value=0, max_value=3),
)
def test_a_name_that_climbs_out_of_the_destination_is_refused(
    tmp_path_factory: pytest.TempPathFactory,
    data: st.DataObject,
    leaf: str,
    depth: int,
) -> None:
    """Traversal that clears the download directory is rejected.

    Nobody writes these names down: they arrive in a release payload
    from GitHub. The interesting shape is traversal buried under
    real-looking directories, so the climb is drawn to exceed the depth
    it has to undo rather than fixed, and a name that merely returns to
    where it started is never generated: that one is contained, and
    refusing it would be a different rule.
    """
    destination = tmp_path_factory.mktemp("assets")
    climb = data.draw(st.integers(min_value=depth + 1, max_value=depth + 3))
    parts = [f"d{index}" for index in range(depth)] + [".."] * climb + [leaf]
    name = "/".join(parts)

    with pytest.raises(PackagingError, match="would write outside"):
        asset_target({"name": name, "url": "https://example/x"}, destination)


@settings(max_examples=50, deadline=None)
@given(
    root=st.sampled_from(ABSOLUTE_ROOTS),
    leaf=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
)
def test_an_absolute_name_is_refused(
    tmp_path_factory: pytest.TempPathFactory, root: str, leaf: str
) -> None:
    """An absolute asset name is rejected rather than followed.

    Joining an absolute path discards the directory on its left, so this
    escapes without climbing at all, and the arithmetic the traversal
    case relies on never runs.
    """
    destination = tmp_path_factory.mktemp("assets")
    name = f"{root}/{leaf}"

    with pytest.raises(PackagingError, match="would write outside"):
        asset_target({"name": name, "url": "https://example/x"}, destination)


class _SteppingClock:
    """A clock that advances a fixed amount each time it is read.

    A latency bucket asserted against a real clock is either flaky or
    trivially `under-1s`. This makes the duration a decision of the
    test, so every bucket boundary can be named.
    """

    def __init__(self, step: float) -> None:
        """Advance by ``step`` seconds per reading."""
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        """Return the reading, then advance."""
        reading = self.now
        self.now += self.step
        return reading


def _metrics(captured: str) -> dict[str, str]:
    """Return the metric lines in ``captured``, by name.

    Parameters
    ----------
    captured:
        Standard output from a run.

    Returns
    -------
    dict[str, str]
        Metric name to value, with the namespace prefix stripped.
    """
    found: dict[str, str] = {}
    for line in captured.splitlines():
        if line.startswith(METRIC_PREFIX):
            name, _, value = line.removeprefix(METRIC_PREFIX).partition("=")
            found[name] = value
    return found


class TestTheLatencyLabelsAreBounded:
    """A duration is reported as a bucket, never as itself."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "under-1s"),
            (0.999, "under-1s"),
            (1.0, "under-5s"),
            (29.9, "under-30s"),
            (30.0, "under-120s"),
            (119.9, "under-120s"),
            (120.0, "over-120s"),
            (10_000.0, "over-120s"),
        ],
    )
    def test_each_boundary_falls_on_the_side_it_is_named_for(
        self, seconds: float, expected: str
    ) -> None:
        """The bound is exclusive, so a bucket never includes its name."""
        assert latency_bucket(seconds) == expected

    def test_the_label_set_is_closed(self) -> None:
        """Every duration lands in one of five labels, and no other.

        An unbounded label is the defect this guards: a raw duration as
        a metric value makes every run its own series.
        """
        labels = {latency_bucket(second / 4) for second in range(0, 2000)}

        assert labels <= {
            "under-1s",
            "under-5s",
            "under-30s",
            "under-120s",
            "over-120s",
        }, labels


class TestTheReleaseLookupIsMeasured:
    """The lookup reports its outcome, attempts and latency, always.

    The retry was added because a single 500 lost a release. A retry
    nobody can count is a retry nobody can tell is happening, and the
    run it saves looks exactly like a run that never needed it.
    """

    def test_a_first_time_success_reports_one_attempt(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ordinary case is counted too, or a rate has no denominator."""
        _ApiHandler.release = _release(api, "a.tar.gz")

        release_for_tag(REPO, TAG, _api(api))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.OK, found
        assert found["release-lookup.attempts"] == "1", found

    def test_a_retried_success_reports_the_attempt_it_took(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The attempt count is what makes the retry rate readable."""
        _ApiHandler.release = _release(api, "a.tar.gz")
        _ApiHandler.release_first_status = 503

        release_for_tag(REPO, TAG, _api(api))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.OK, found
        assert found["release-lookup.attempts"] == "2", found

    def test_an_exhausted_lookup_reports_the_category_it_failed_on(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failure is measured, or the metric only counts good news."""
        _ApiHandler.release_status = 503

        with pytest.raises(PackagingError):
            release_for_tag(REPO, TAG, _api(api, attempts=3))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.RETRYABLE_STATUS, found
        assert found["release-lookup.attempts"] == "3", found

    def test_a_permanent_status_is_a_different_category(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A 401 is not a transient failure, and must not be counted as one."""
        _ApiHandler.release_status = 401

        with pytest.raises(PackagingError):
            release_for_tag(REPO, TAG, _api(api))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.PERMANENT_STATUS, found

    def test_the_latency_is_reported_as_a_bucket(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A slow lookup is distinguishable from a fast one, coarsely."""
        _ApiHandler.release = _release(api, "a.tar.gz")
        client = _api(api)._replace(clock=_SteppingClock(20.0))

        release_for_tag(REPO, TAG, client)

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.latency"] == "under-30s", found

    def test_no_metric_line_carries_the_token_or_the_address(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A metric is a label, not a log line.

        A token in a workflow log is a leak, and a URL as a label is an
        unbounded series. Both are easy to add by accident and neither
        is visible in the value a test happens to assert.
        """
        _ApiHandler.release = _release(api, "a.tar.gz")

        release_for_tag(REPO, TAG, _api(api))

        lines = [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith(METRIC_PREFIX)
        ]
        assert lines, "the lookup reported no metric at all"
        for line in lines:
            assert TOKEN not in line, line
            assert api not in line, line
            assert "http" not in line, line


class TestTheAssetDownloadIsMeasured:
    """Downloads report what arrived, not merely that something did."""

    def test_a_complete_download_reports_what_it_wrote(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The count is the audit's own denominator."""
        release = _release(api, "a.tar.gz", "b.tar.gz")

        download_assets(release, tmp_path / "dist", _api(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.outcome"] == Outcome.OK, found
        assert found["asset-download.written"] == "2", found

    def test_a_release_with_no_assets_reports_nothing_written(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty release is the failure the audit exists to catch.

        Run 34208473988 is exactly this shape from the audit's side, so
        it must be countable rather than only readable in a message.
        """
        with pytest.raises(PackagingError):
            download_assets({"assets": []}, tmp_path / "dist", _api(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.written"] == "0", found
        assert found["asset-download.outcome"] != Outcome.OK, found
