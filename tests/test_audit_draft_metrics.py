"""What the reader reports about its own retries.

The retry exists because a single 500 lost a release. A retry nobody
can count is a retry nobody can tell is happening, and the run it saves
looks exactly like a run that never needed it. Every value asserted
here is drawn from a closed set or is a count: a raw duration or an
address as a label makes every run its own series.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from audit_draft import (
    METRIC_PREFIX,
    Api,
    Outcome,
    download_assets,
    latency_bucket,
    release_for_tag,
)
from audit_draft_support import (
    REPO,
    TAG,
    TOKEN,
    UNREACHABLE,
    ApiHandler,
    api_for,
    release_body,
)
from package import PackagingError


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


class _BadBytesTransport:
    """A transport answering with bytes that are not text at all.

    A server can return a body that decodes before it parses and a body
    that does neither. The second raises a different exception, and the
    difference is what this exists to reach.
    """

    def __init__(self, body: bytes) -> None:
        """Answer every read with ``body``."""
        self.body = body

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        """Return the undecodable body, ignoring what was asked for."""
        del url, headers
        return self.body


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
        assert latency_bucket(seconds) == expected, (
            f"{seconds} seconds must be reported as {expected!r}, not "
            f"{latency_bucket(seconds)!r}"
        )

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
        ApiHandler.release = release_body(api, "a.tar.gz")

        release_for_tag(REPO, TAG, api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.OK, found
        assert found["release-lookup.attempts"] == "1", found

    def test_a_retried_success_reports_the_attempt_it_took(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The attempt count is what makes the retry rate readable."""
        ApiHandler.release = release_body(api, "a.tar.gz")
        ApiHandler.release_first_status = 503

        release_for_tag(REPO, TAG, api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.OK, found
        assert found["release-lookup.attempts"] == "2", found

    def test_an_exhausted_lookup_reports_the_category_it_failed_on(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failure is measured, or the metric only counts good news."""
        ApiHandler.release_status = 503

        with pytest.raises(PackagingError):
            release_for_tag(REPO, TAG, api_for(api, attempts=3))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.RETRYABLE_STATUS, found
        assert found["release-lookup.attempts"] == "3", found

    def test_a_permanent_status_is_a_different_category(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A 401 is not a transient failure, and must not be counted as one."""
        ApiHandler.release_status = 401

        with pytest.raises(PackagingError):
            release_for_tag(REPO, TAG, api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.PERMANENT_STATUS, found

    def test_the_latency_is_reported_as_a_bucket(
        self, api: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A slow lookup is distinguishable from a fast one, coarsely."""
        ApiHandler.release = release_body(api, "a.tar.gz")
        client = api_for(api)._replace(clock=_SteppingClock(20.0))

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
        ApiHandler.release = release_body(api, "a.tar.gz")

        release_for_tag(REPO, TAG, api_for(api))

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
        release = release_body(api, "a.tar.gz", "b.tar.gz")

        download_assets(release, tmp_path / "dist", api_for(api))

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
            download_assets({"assets": []}, tmp_path / "dist", api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.written"] == "0", found
        assert found["asset-download.outcome"] != Outcome.OK, found


class TestAFailureIsNeverReportedAsSuccess:
    """A lookup that fails must not leave `ok` in the log.

    The outcome is reported from a `finally`, which runs however the
    body left. That is only half the guarantee: the cause it reports is
    read from a variable the `except` clauses set, so a failure that no
    clause catches passes through the `finally` with the cause still
    unset and is counted as a success.
    """

    def test_a_body_that_is_not_utf8_is_a_malformed_response(self) -> None:
        """Bytes that are not text are a bad body, not a bad connection.

        `json.loads` raises `UnicodeDecodeError` rather than
        `JSONDecodeError` for these, and the two are siblings rather
        than one being the other, so catching the JSON one alone lets
        this through.
        """
        transport = _BadBytesTransport(b"\xff\xfe not utf-8 at all")
        client = Api(token=TOKEN, root=UNREACHABLE, transport=transport)

        with pytest.raises(PackagingError, match="not JSON"):
            release_for_tag(REPO, TAG, client)

    def test_the_metric_for_that_lookup_says_it_failed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The same failure must be counted as one.

        This is the half that matters for a rate. A read that raised and
        was logged as `ok` makes the failure invisible in exactly the
        measurement that exists to find it.
        """
        transport = _BadBytesTransport(b"\xff\xfe not utf-8 at all")
        client = Api(token=TOKEN, root=UNREACHABLE, transport=transport)

        with pytest.raises(PackagingError):
            release_for_tag(REPO, TAG, client)

        found = _metrics(capsys.readouterr().out)
        assert found["release-lookup.outcome"] == Outcome.NOT_JSON, found


class TestTheAssetOutcomeSaysWhichFailure:
    """An asset failure is categorised rather than lumped together.

    Every failure below `download_assets` arrives as `PackagingError`,
    so classifying by exception type put a permanent status, an asset
    name that would escape the directory, and a release with no assets
    at all into one bucket named for the connection. Three different
    faults reported as the same transient one.
    """

    def test_an_empty_release_is_not_reported_as_a_transport_error(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Finding nothing to audit is the audit's own failure mode."""
        with pytest.raises(PackagingError):
            download_assets({"assets": []}, tmp_path / "dist", api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.outcome"] == Outcome.NO_ASSETS, found

    def test_an_uncreatable_destination_says_so(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A directory that cannot be made is not a connection fault.

        Nothing is wrong with the release, the connection or the assets,
        and no retry or re-release would change it, so it carries its
        own value rather than the one the next step would have reported.
        """
        blocked = tmp_path / "a-file"
        blocked.write_text("not a directory", encoding="utf-8")

        with pytest.raises(PackagingError):
            download_assets(
                release_body(api, "a.tar.gz"), blocked / "dist", api_for(api)
            )

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.outcome"] == Outcome.DESTINATION_UNWRITABLE, found

    def test_an_entry_that_is_not_an_asset_is_a_rejected_asset(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A list holding something else is not an empty list.

        The release lists one entry, so the audit has something to read
        and finds it unusable. Reporting `no-assets` would say the
        release was empty, which sends a maintainer to the upload step
        rather than to the entry that is wrong.
        """
        with pytest.raises(PackagingError):
            download_assets({"assets": ["a.tar.gz"]}, tmp_path / "dist", api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.outcome"] == Outcome.REJECTED_ASSET, found

    def test_an_escaping_asset_name_reports_itself(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A name that would leave the directory is a rejected asset."""
        release = {"assets": [{"name": "../escape.tar.gz", "url": f"{api}/x"}]}

        with pytest.raises(PackagingError):
            download_assets(release, tmp_path / "dist", api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.outcome"] == Outcome.REJECTED_ASSET, found

    def test_a_permanent_status_on_an_asset_reports_itself(
        self, api: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A status no retry will change is not a transport error."""
        ApiHandler.asset_mode = "permanent"
        release = release_body(api, "a.tar.gz")

        with pytest.raises(PackagingError):
            download_assets(release, tmp_path / "dist", api_for(api))

        found = _metrics(capsys.readouterr().out)
        assert found["asset-download.outcome"] == Outcome.ASSET_UNREADABLE, found
