"""What the draft reader actually puts on the wire.

A draft release is invisible without push access, which is what lost
run 34208473988. The token, and the media types that decide whether an
asset arrives as bytes or as its own metadata, are the mechanism this
whole script exists to get right. Before these assertions, deleting the
`Authorization` header outright left every test passing.
"""

from __future__ import annotations

from pathlib import Path

from audit_draft import API_VERSION, download_assets, release_for_tag
from audit_draft_support import REPO, TAG, TOKEN, ApiHandler, api_for, release_body
from verify_upstream import USER_AGENT


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
        for path, headers in ApiHandler.seen_headers:
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
        ApiHandler.release = release_body(api, "a.tar.gz")

        release_for_tag(REPO, TAG, api_for(api))

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
        ApiHandler.release = release_body(api, "a.tar.gz")

        download_assets(ApiHandler.release, tmp_path / "dist", api_for(api))

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
        ApiHandler.release = release_body(api, "a.tar.gz")

        download_assets(
            release_for_tag(REPO, TAG, api_for(api)), tmp_path / "dist", api_for(api)
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
