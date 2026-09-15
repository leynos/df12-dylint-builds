"""Download every asset of a draft release, so the audit can check them.

The audit exists to read back what the build legs uploaded, from GitHub,
rather than trusting the artefacts still on the runner. A draft release
is visible only to a token with push access: without one the API reports
it as not found, and the audit would pass judgement on an empty
directory. Run 34208473988 lost a release to exactly that, with both
build legs having uploaded all twelve assets.

So this reads the release by tag and every asset under it, with the
token, retrying transient failures and refusing to write a truncated
body. It replaces a shell loop in the workflow, which no test could
drive: the retry, the treatment of a permanent status, and the
distinction between an empty release and an unreadable one are decisions
rather than plumbing, and they belong somewhere they can be exercised.

Nothing is verified here. `package.py verify` is the next step and owns
the sidecars and the layout.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from package import PackagingError
from verify_upstream import (
    DEFAULT_RETRY,
    RETRYABLE_STATUSES,
    TIMEOUT_SECONDS,
    USER_AGENT,
    Retry,
    download,
)

#: One asset as the releases API describes it. The values are `object`
#: rather than `str`, because this is a payload from elsewhere: the
#: fields this script needs are read through `_required_text`, which
#: states the type it expects and says so when the answer disagrees.
#: Declaring them `str` here would assert about GitHub's output what
#: only a check can establish.
type AssetPayload = dict[str, object]
#: One release as the releases API describes it, for the same reason.
type ReleasePayload = dict[str, object]

DEFAULT_API: Final = "https://api.github.com"
#: The API version this script's response handling was written against.
API_VERSION: Final = "2022-11-28"


class Api(typing.NamedTuple):
    """How to reach the releases API: where, with what, and how hard.

    One value rather than three parameters. The root exists so a test
    can point this at a local server, and the retry so it need not wait
    out a real backoff; both travel with the token on every call, and
    passing them separately made each signature read as though the
    others had been forgotten.

    Attributes
    ----------
    token:
        A GitHub token with push access, which is what makes a draft
        release visible at all.
    root:
        The API root.
    retry:
        How many times to try a read, and how long to wait between.
    """

    token: str
    root: str = DEFAULT_API
    retry: Retry = DEFAULT_RETRY


class _TransientError(Exception):
    """One attempt failed in a way another attempt might not.

    Private, and never escapes `_read_json`: it exists so the decision
    about which failures are worth another attempt is taken once, where
    the status is in hand, rather than being re-derived by the loop.
    """


#: Everything a read can fail with. Listed once so `_read_json_once`
#: catches and `_verdict_on` classifies the same set; splitting them
#: across two `except` clauses is what makes either one grow a branch
#: per failure mode.
READ_FAILURES: Final = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    http.client.HTTPException,
    json.JSONDecodeError,
    OSError,
)


def _verdict_on(subject: str, error: Exception) -> Exception:
    """Return the exception one failed read deserves.

    A 404 gets the longest explanation because the API answers the same
    way for a release that does not exist and for a draft the token
    cannot see, and only the second has ever happened here.

    Parameters
    ----------
    subject:
        What was being read, for the failure message.
    error:
        What the attempt raised.

    Returns
    -------
    Exception
        `_TransientError` when another attempt might succeed, and
        `PackagingError` when it cannot.
    """
    if isinstance(error, urllib.error.HTTPError):
        reason = _http_reason(subject, error.code)
        if error.code in LOOKUP_RETRYABLE_STATUSES:
            return _TransientError(reason)
        return PackagingError(reason)
    if isinstance(error, json.JSONDecodeError):
        # The server answered, and answered with something that is not
        # JSON. Asking again returns the same body.
        return PackagingError(f"{subject}: returned a body that is not JSON")
    return _TransientError(f"{subject}: could not be read: {error}")


def _headers(token: str, *, accept: str) -> dict[str, str]:
    """Return the request headers for one authenticated API read.

    Parameters
    ----------
    token:
        A GitHub token with push access, which is what makes a draft
        release visible at all.
    accept:
        The media type to ask for. Asset bodies need
        ``application/octet-stream``; anything else returns the asset's
        metadata again, which would be written out as though it were an
        archive.

    Returns
    -------
    dict[str, str]
        The headers.
    """
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }


#: Statuses the release lookup tries again on, over and above the ones
#: any download retries. A release read immediately after the release was
#: created can answer 404 before GitHub is consistent, and the workflow
#: does exactly that: the draft is created in the `prepare` job and read
#: in `audit`. Retrying it costs one backoff in the case that matters --
#: an under-privileged token, which answers 404 every time -- and saves
#: the whole release from a race that is invisible in the log.
LOOKUP_RETRYABLE_STATUSES: Final = RETRYABLE_STATUSES | {404}


def _read_json_once(url: str, headers: dict[str, str], *, subject: str) -> object:
    """Read one JSON document, or raise the verdict on the failure.

    Parameters
    ----------
    url:
        The address to read.
    headers:
        The request headers, already carrying the token.
    subject:
        What is being read, for the failure messages.

    Returns
    -------
    object
        The decoded body.

    Raises
    ------
    PackagingError
        If the status will not change on a retry, or the body is not
        JSON.
    _TransientError
        If another attempt might succeed.
    """
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except READ_FAILURES as error:
        raise _verdict_on(subject, error) from error


def _read_json(url: str, api: Api, *, subject: str) -> object:
    """Read one JSON document from the API, retrying what is worth it.

    Without the retry a single 500 from the API failed the release
    outright, and the asset retry never ran, because there was no
    release to take assets from.

    Parameters
    ----------
    url:
        The address to read.
    api:
        How to reach the API.
    subject:
        What is being read, for the failure messages.

    Returns
    -------
    object
        The decoded body.

    Raises
    ------
    PackagingError
        If the read fails on a status no retry can change, if every
        attempt fails, or if the body is not JSON.
    """
    headers = _headers(api.token, accept="application/vnd.github+json")
    last = ""
    for attempt in range(1, api.retry.attempts + 1):
        try:
            return _read_json_once(url, headers, subject=subject)
        except _TransientError as transient:
            last = str(transient)
        if attempt == api.retry.attempts:
            break
        print(f"attempt {attempt} to read {subject} failed: {last}; retrying")
        time.sleep(api.retry.backoff * attempt)
    message = f"{last} (after {api.retry.attempts} attempts)"
    raise PackagingError(message)


def _http_reason(subject: str, code: int) -> str:
    """Return the failure message for one HTTP status.

    Parameters
    ----------
    subject:
        What was being read.
    code:
        The status the API returned.

    Returns
    -------
    str
        The message.
    """
    if code == 404:
        return (
            f"{subject}: not found. A draft release is visible only to a "
            f"token with push access, so this is also what an "
            f"under-privileged token sees for a draft that exists."
        )
    return f"{subject}: returned HTTP {code}"


def release_for_tag(repo: str, tag: str, api: Api) -> ReleasePayload:
    """Return the release GitHub holds for ``tag``.

    Parameters
    ----------
    repo:
        The repository in ``owner/name`` form.
    tag:
        The release tag.
    api:
        How to reach the API.

    Returns
    -------
    ReleasePayload
        The release object.

    Raises
    ------
    PackagingError
        If the release cannot be read, or the body is not an object.
    """
    owner_repo = urllib.parse.quote(repo)
    # ``safe=""`` because a tag is one path segment. The default keeps
    # ``/`` unencoded, so a tag such as ``release/6.0`` would address
    # ``releases/tags/release/6.0`` and come back as a not-found, which
    # reads here as the under-privileged-token case it is not.
    quoted_tag = urllib.parse.quote(tag, safe="")
    subject = f"{repo} release {tag}"
    payload = _read_json(
        f"{api.root}/repos/{owner_repo}/releases/tags/{quoted_tag}",
        api,
        subject=subject,
    )
    if not isinstance(payload, dict):
        message = f"{subject}: returned {type(payload).__name__}, not a release"
        raise PackagingError(message)
    return payload


def assets_of(release: ReleasePayload) -> list[AssetPayload]:
    """Return the release's assets, insisting there is at least one.

    Parameters
    ----------
    release:
        The release object.

    Returns
    -------
    list[AssetPayload]
        The assets, each with a ``name`` and a ``url``.

    Raises
    ------
    PackagingError
        If the assets are missing, not a list, empty, or not objects. An
        empty draft is reported rather than accepted: the audit's whole
        purpose is to read back what was uploaded, and finding nothing
        is the result it must never treat as a clean sheet.
    """
    assets = release.get("assets")
    if not isinstance(assets, list):
        message = "the release carries no asset list, so nothing can be audited"
        raise PackagingError(message)
    if not assets:
        message = (
            "the release carries no assets. An audit over an empty directory "
            "passes every check it is given, so this is a failure rather than "
            "a clean result"
        )
        raise PackagingError(message)
    # Checked here rather than left to the field reads: an entry that is
    # not an object reaches `_required_text` as whatever it is and fails
    # with an AttributeError, which names neither the release nor the
    # entry. This is also what makes the return annotation true.
    for entry in assets:
        if not isinstance(entry, dict):
            message = (
                f"the release lists {type(entry).__name__} where an asset "
                f"object belongs: {entry!r}"
            )
            raise PackagingError(message)
    return assets


def _required_text(asset: AssetPayload, field: str) -> str:
    """Return one string field of an asset, insisting it is present.

    Parameters
    ----------
    asset:
        The asset object.
    field:
        The field to read.

    Returns
    -------
    str
        The value.

    Raises
    ------
    PackagingError
        If the field is absent, empty, or not a string.
    """
    value = asset.get(field)
    if not isinstance(value, str) or not value:
        message = f"an asset of this release has no {field}: {asset!r}"
        raise PackagingError(message)
    return value


def _asset_target(asset: AssetPayload, destination: Path) -> tuple[str, Path]:
    """Return one asset's download URL and where it may be written.

    The name comes from the release rather than from this repository, so
    it is not trusted to stay inside the directory.

    Parameters
    ----------
    asset:
        The asset object.
    destination:
        The directory being filled.

    Returns
    -------
    tuple[str, Path]
        The URL to read, and the path to write.

    Raises
    ------
    PackagingError
        If the asset lacks a name or a URL, or the name would escape.
    """
    name = _required_text(asset, "name")
    url = _required_text(asset, "url")
    target = (destination / name).resolve()
    if not target.is_relative_to(destination.resolve()):
        message = f"asset name {name!r} would write outside {destination}"
        raise PackagingError(message)
    return url, target


def download_assets(release: ReleasePayload, destination: Path, api: Api) -> list[Path]:
    """Download every asset of ``release`` into ``destination``.

    Parameters
    ----------
    release:
        The release object.
    destination:
        The directory to write into. Created if absent.
    api:
        How to reach the API, and how hard to try each asset.

    Returns
    -------
    list[Path]
        The written files, in the order the release lists them.

    Raises
    ------
    PackagingError
        If an asset is unusable, or a download fails.
    """
    destination.mkdir(parents=True, exist_ok=True)
    headers = _headers(api.token, accept="application/octet-stream")
    written: list[Path] = []
    for asset in assets_of(release):
        url, target = _asset_target(asset, destination)
        download(url, target, retry=api.retry, headers=headers)
        written.append(target)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    """Download a draft release's assets for the audit step.

    Parameters
    ----------
    argv:
        Command-line arguments, or None to read ``sys.argv``.

    Returns
    -------
    int
        ``0`` on success, ``1`` when the release or an asset could not
        be read.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--tag", required=True, help="the release tag")
    parser.add_argument("--token", required=True, help="a token with push access")
    parser.add_argument("--dir", required=True, type=Path, help="where to write")
    parser.add_argument("--api", default=DEFAULT_API, help=argparse.SUPPRESS)
    # Suppressed for the same reason as ``--api``: a test that exercises
    # the failing path must not wait out the real backoff, which is
    # nearly half a minute across four attempts.
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=DEFAULT_RETRY.backoff,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    api = Api(
        token=args.token,
        root=args.api,
        retry=DEFAULT_RETRY._replace(backoff=args.retry_backoff),
    )
    try:
        release = release_for_tag(args.repo, args.tag, api)
        written = download_assets(release, args.dir, api)
    except PackagingError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    print(f"downloaded {len(written)} asset(s) for {args.tag} into {args.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
