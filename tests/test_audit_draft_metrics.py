"""What the reader reports about its own retries.

The retry exists because a single 500 lost a release. A retry nobody
can count is a retry nobody can tell is happening, and the run it saves
looks exactly like a run that never needed it. Every value asserted
here is drawn from a closed set or is a count: a raw duration or an
address as a label makes every run its own series.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from audit_draft import (
    METRIC_PREFIX,
    Outcome,
    download_assets,
    latency_bucket,
    release_for_tag,
)
from audit_draft_support import REPO, TAG, TOKEN, ApiHandler, api_for, release_body
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
