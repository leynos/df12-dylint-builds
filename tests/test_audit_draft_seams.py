"""The reader's seams, and the proof that they carry the traffic.

Splitting a query out of a boundary is worth nothing unless the split
is asserted, and a test that reaches the query through a working server
passes just as well before the split as after it. The doubles here are
what make the difference visible: one transport refuses to be called,
another answers from a table, and the sleeper records a schedule rather
than serving it out.
"""

from __future__ import annotations

import email.message
import json
import typing
import urllib.error
from pathlib import Path

import pytest
from audit_draft import (
    DEFAULT_API,
    Api,
    Retry,
    download_assets,
    release_for_tag,
    release_from_payload,
    release_url,
)
from audit_draft_support import (
    REPO,
    TAG,
    TOKEN,
    UNREACHABLE,
    ApiHandler,
    release_body,
)
from package import PackagingError


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
        ApiHandler.release_status = 503
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
        ApiHandler.release = release_body(api, "a.tar.gz")
        client = Api(token=TOKEN, root=api, retry=Retry(attempts=3), sleeper=sleeper)

        release_for_tag(REPO, TAG, client)

        assert sleeper.waits == [], sleeper.waits


class _FailingThenAnsweringTransport:
    """A transport that refuses a fixed number of reads, then answers.

    The asset path's retry loop lives in the shared downloader, which
    opened its own socket until this round. Driving it from here is the
    only way to assert that a retry happens without waiting one out
    against a real server.
    """

    def __init__(self, body: bytes, failures: int) -> None:
        """Fail ``failures`` reads with a retryable status, then answer."""
        self.body = body
        self.remaining = failures
        self.urls: list[str] = []

    def __call__(self, url: str, headers: typing.Mapping[str, str]) -> bytes:
        """Record ``url``, then raise or answer."""
        self.urls.append(url)
        if self.remaining:
            self.remaining -= 1
            raise urllib.error.HTTPError(
                url, 503, "busy", email.message.Message(), None
            )
        return self.body


class TestTheAssetDownloadsUseTheSameSeams:
    """An asset body goes through the injected transport and sleeper.

    The release lookup was injected a round earlier and the asset
    downloads were not: `download_assets` held an `Api` carrying a
    transport, a sleeper and a clock, and called the shared downloader
    with neither of the first two. Everything below the lookup therefore
    reached the real socket and the real clock, and no test could say so
    because the stand-in server answered either way.
    """

    def test_an_asset_body_arrives_through_the_transport_it_was_given(
        self, tmp_path: Path
    ) -> None:
        """The asset is written from what the transport returned.

        The root points at a port nothing listens on, so a download that
        reached `urllib` would fail rather than quietly agree.
        """
        transport = _TabledTransport(b"an archive")
        client = Api(token=TOKEN, root=UNREACHABLE, transport=transport)
        release = release_body(UNREACHABLE, "a.tar.gz")

        written = download_assets(release, tmp_path, client)

        assert written == [tmp_path / "a.tar.gz"], written
        assert written[0].read_bytes() == b"an archive"
        assert transport.urls == [f"{UNREACHABLE}/repos/{REPO}/releases/assets/0"], (
            transport.urls
        )

    def test_an_asset_retry_waits_on_the_sleeper_it_was_given(
        self, tmp_path: Path
    ) -> None:
        """A retried asset waits the growing schedule, and waits none of it.

        Two retryable failures then a body: the wait after each failure
        is recorded rather than served, so the schedule is an assertion
        instead of a delay.
        """
        transport = _FailingThenAnsweringTransport(b"an archive", failures=2)
        sleeper = _RecordingSleeper()
        client = Api(
            token=TOKEN,
            root=UNREACHABLE,
            retry=Retry(attempts=3, backoff=2),
            transport=transport,
            sleeper=sleeper,
        )
        release = release_body(UNREACHABLE, "a.tar.gz")

        written = download_assets(release, tmp_path, client)

        assert written[0].read_bytes() == b"an archive"
        assert sleeper.waits == [2.0, 4.0], (
            f"two failures before the body arrived, growing: {sleeper.waits}"
        )

    def test_an_asset_download_never_reaches_the_network_itself(
        self, tmp_path: Path
    ) -> None:
        """Handing down a refusing transport stops the download dead.

        The other direction of the same claim. If the shared downloader
        still opened its own socket, this would pass by reaching the
        unreachable root and failing there, so the assertion is on the
        refusal's own message rather than on the failure alone.
        """
        client = Api(token=TOKEN, root=UNREACHABLE, transport=_RefusingTransport())
        release = release_body(UNREACHABLE, "a.tar.gz")

        with pytest.raises(AssertionError, match="a query path opened a connection"):
            download_assets(release, tmp_path, client)
