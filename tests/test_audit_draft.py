"""Tests for reading back a draft release's assets before the audit.

The audit's whole value is that it reads GitHub rather than the runner,
and a draft is visible only to a token with push access. Every decision
that used to live in a shell loop is exercised here against the
stand-in server in `audit_draft_support`: what a 404 means when the
release plainly exists, what a retryable status is worth, what a
permanent one is worth, a truncated body, an empty release, and an
asset name that tries to leave the directory.

The seams, the request headers, the asset-name property and the metrics
have modules of their own, named for what they assert.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from audit_draft import assets_of, download_assets, main, release_for_tag
from audit_draft_support import (
    ASSET,
    REPO,
    TAG,
    TOKEN,
    ApiHandler,
    api_for,
    release_body,
)
from package import PackagingError


class TestReadingTheRelease:
    """Finding the draft, or failing in a way that says why."""

    def test_a_draft_is_returned_whole(self, api: str) -> None:
        """A readable draft comes back as the release object."""
        ApiHandler.release = release_body(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, api_for(api))

        assert release["tag_name"] == TAG, "the release must come back intact"

    def test_a_404_says_what_it_usually_means_here(self, api: str) -> None:
        """A 404 is reported as the under-privileged-token case.

        The API answers the same way for a release that does not exist
        and for a draft the token cannot see. The second is the failure
        that lost run 34208473988, so the message names it rather than
        leaving the reader to guess.
        """
        ApiHandler.release_status = 404

        with pytest.raises(PackagingError, match="push access"):
            release_for_tag(REPO, TAG, api_for(api))

    def test_a_retryable_status_is_retried_and_then_succeeds(self, api: str) -> None:
        """A 500 on the first lookup does not fail the release.

        Before the retry a single transient status failed the audit
        outright, and the asset retry never ran, because there was no
        release to take assets from.
        """
        ApiHandler.release_first_status = 500
        ApiHandler.release = release_body(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, api_for(api))

        assert release["tag_name"] == TAG, "the second attempt must be believed"

    def test_a_404_on_the_first_lookup_is_retried(self, api: str) -> None:
        """A 404 is retried, because the draft is created just before.

        The workflow creates the release in `create-release` and reads
        it back in `audit`, so the first read can precede GitHub's own
        consistency. A permanently invisible draft still fails, after
        the whole retry policy has been spent on it.
        """
        ApiHandler.release_first_status = 404
        ApiHandler.release = release_body(api, "a.tar.gz")

        release = release_for_tag(REPO, TAG, api_for(api))

        assert release["tag_name"] == TAG, "a 404 that clears must not fail the run"

    def test_a_permanent_status_is_not_retried(self, api: str) -> None:
        """A 403 fails at once and names its code.

        Asking a permission answer again cannot change it, so retrying
        only delays the failure by the whole backoff.
        """
        ApiHandler.release_status = 403

        with pytest.raises(PackagingError, match="HTTP 403"):
            release_for_tag(REPO, TAG, api_for(api))

        assert len(ApiHandler.release_paths) == 1, (
            "a permanent status must be read once, not once per attempt; "
            f"read {ApiHandler.release_paths}"
        )

    def test_a_status_that_never_clears_reports_the_attempts(self, api: str) -> None:
        """An exhausted retry says how many times it tried.

        A failure that took four attempts and one that took one read
        identically otherwise, and the difference is what tells a
        maintainer whether the API was unwell or the token was wrong.
        """
        ApiHandler.release_status = 500

        with pytest.raises(PackagingError, match="after 2 attempts"):
            release_for_tag(REPO, TAG, api_for(api, attempts=2))

    def test_a_tag_with_a_slash_stays_one_path_segment(self, api: str) -> None:
        """A `/` in the tag is encoded rather than splitting the path.

        `urllib.parse.quote` keeps `/` by default, so `release/6.0`
        would address `releases/tags/release/6.0` and come back as a
        not-found, which reads here as the under-privileged-token case
        it is not.
        """
        ApiHandler.release = release_body(api, "a.tar.gz")

        release_for_tag(REPO, "release/6.0", api_for(api))

        assert ApiHandler.release_paths[0].endswith("/releases/tags/release%2F6.0"), (
            "the tag must be one encoded path segment; got "
            f"{ApiHandler.release_paths[0]}"
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
        ApiHandler.release = release_body(api, "a.tar.gz", "a.tar.gz.sha256")

        written = download_assets(ApiHandler.release, tmp_path / "dist", api_for(api))

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
        ApiHandler.asset_mode = "flaky"
        ApiHandler.release = release_body(api, "a.tar.gz")

        written = download_assets(ApiHandler.release, tmp_path / "dist", api_for(api))

        assert written[0].read_bytes() == ASSET, "the retry must serve the real bytes"

    def test_a_permanent_status_is_not_retried(self, api: str, tmp_path: Path) -> None:
        """A 403 fails at once rather than four times.

        Asking again cannot change a permission answer, and retrying
        only delays the failure by the whole backoff.
        """
        ApiHandler.asset_mode = "permanent"
        ApiHandler.release = release_body(api, "a.tar.gz")

        with pytest.raises(PackagingError, match="HTTP 403"):
            download_assets(ApiHandler.release, tmp_path / "dist", api_for(api))

    def test_a_truncated_body_is_never_written(self, api: str, tmp_path: Path) -> None:
        """A short body fails rather than landing as an archive.

        A truncated asset written to disk would be audited as a corrupt
        archive, which reads as a build fault rather than a transfer one.
        """
        ApiHandler.asset_mode = "truncated"
        ApiHandler.release = release_body(api, "a.tar.gz")

        with pytest.raises(PackagingError, match="attempts"):
            download_assets(
                ApiHandler.release, tmp_path / "dist", api_for(api, attempts=2)
            )
        assert not (tmp_path / "dist" / "a.tar.gz").exists(), (
            "nothing may be written unless the whole body arrived"
        )

    def test_an_escaping_asset_name_is_refused(self, api: str, tmp_path: Path) -> None:
        """A name that climbs out of the directory is rejected.

        The name comes from the release rather than from this
        repository, so it is not trusted to stay where it is put.
        """
        ApiHandler.release = release_body(api, "../escaped.tar.gz")

        with pytest.raises(PackagingError, match="outside"):
            download_assets(ApiHandler.release, tmp_path / "dist", api_for(api))


class TestTheCommandLine:
    """The entry point the workflow step invokes."""

    def test_a_readable_draft_exits_zero(self, api: str, tmp_path: Path) -> None:
        """A successful download reports how much it wrote."""
        ApiHandler.release = release_body(api, "a.tar.gz")

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
        ApiHandler.release_status = 404

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
