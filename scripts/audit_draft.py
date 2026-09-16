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
import enum
import http.client
import json
import sys
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from package import PackagingError
from verify_upstream import (
    DEFAULT_RETRY,
    RETRYABLE_STATUSES,
    USER_AGENT,
    Retry,
    Sleeper,
    Transport,
    download,
    urlopen_bytes,
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


class Clock(typing.Protocol):
    """Where a duration comes from.

    Injected so a latency bucket can be asserted rather than waited
    for. It reads a monotonic counter, not a wall clock: the value is
    only ever subtracted from another reading of itself.
    """

    def __call__(self) -> float:
        """Return the current reading, in seconds."""
        ...


class Api(typing.NamedTuple):
    """How to reach the releases API: where, with what, and how hard.

    One value rather than six parameters. The root exists so a test
    can point this at a local server, the retry so it need not wait out
    a real backoff, and the transport and sleeper so a query can be
    exercised with no network and no clock at all; all of them travel
    with the token on every call, and passing them separately made each
    signature read as though the others had been forgotten.

    Attributes
    ----------
    token:
        A GitHub token with push access, which is what makes a draft
        release visible at all.
    root:
        The API root.
    retry:
        How many times to try a read, and how long to wait between.
    transport:
        What performs every read, the release lookup and each
        asset body alike. Defaults to the real one.
    sleeper:
        What waits between attempts. Defaults to `time.sleep`.
    clock:
        Where a duration is read from, for the latency metric.
        Defaults to `time.monotonic`.
    """

    token: str
    root: str = DEFAULT_API
    retry: Retry = DEFAULT_RETRY
    transport: Transport = urlopen_bytes
    sleeper: Sleeper = time.sleep
    clock: Clock = time.monotonic


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
    # `json.loads` decodes before it parses, so bytes that are not text
    # raise this instead, and it is a sibling of `JSONDecodeError`
    # rather than one of its kind. Left out, it escaped the
    # `PackagingError` contract altogether and, worse, passed through
    # the metric's `finally` with no cause recorded, so a lookup that
    # raised was counted as a success.
    UnicodeDecodeError,
    OSError,
)

#: The failures that mean the server answered and the answer was not
#: usable JSON. Asking again returns the same body, so neither is worth
#: a retry, and both are the same thing to a reader of the metric.
NOT_JSON_FAILURES: Final = (json.JSONDecodeError, UnicodeDecodeError)


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
    if isinstance(error, NOT_JSON_FAILURES):
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


class Outcome(enum.StrEnum):
    """How one operation ended, in a vocabulary that cannot grow at runtime.

    A metric label taken from a message, a status code or an exception
    string is unbounded: every new failure mode becomes a new series,
    and the count of the mode that matters is spread across all of them.
    These are the categories the retry policy actually distinguishes,
    and nothing else is ever reported.
    """

    OK = "ok"
    RETRYABLE_STATUS = "retryable-status"
    PERMANENT_STATUS = "permanent-status"
    NOT_JSON = "not-json"
    NOT_A_RELEASE = "not-a-release"
    TRANSPORT_ERROR = "transport-error"
    #: The release listed no assets. The audit's own failure mode
    #: rather than a fault of the connection: an audit over an empty
    #: directory passes every check it is given.
    NO_ASSETS = "no-assets"
    #: An asset's name or URL was unusable, most often a name that
    #: would have been written outside the download directory.
    REJECTED_ASSET = "rejected-asset"
    #: An asset was listed and could not be fetched.
    ASSET_UNREADABLE = "asset-unreadable"


#: The latency buckets a duration is reported in, as an upper bound and
#: its label. A raw duration is an unbounded label, so it is placed in
#: one of these instead; the boundaries are chosen around the retry
#: policy, whose four attempts and 5, 10, 15 second waits put an
#: exhausted lookup just past thirty seconds.
LATENCY_BUCKETS: Final = (
    (1.0, "under-1s"),
    (5.0, "under-5s"),
    (30.0, "under-30s"),
    (120.0, "under-120s"),
)
#: What a duration past the last bucket is reported as.
SLOWEST_BUCKET: Final = "over-120s"
#: The prefix every metric line carries, so a workflow log can be read
#: for them without matching the prose around them.
METRIC_PREFIX: Final = "metric audit-draft."


def latency_bucket(seconds: float) -> str:
    """Return the bounded label ``seconds`` is reported under.

    Parameters
    ----------
    seconds:
        How long the operation took.

    Returns
    -------
    str
        One of the `LATENCY_BUCKETS` labels, or `SLOWEST_BUCKET`.

    Examples
    --------
    >>> latency_bucket(0.2)
    'under-1s'
    >>> latency_bucket(45.0)
    'under-120s'
    >>> latency_bucket(600.0)
    'over-120s'
    """
    for bound, label in LATENCY_BUCKETS:
        if seconds < bound:
            return label
    return SLOWEST_BUCKET


def report(name: str, value: object) -> None:
    """Emit one metric line on stdout.

    The workflow log is where these are read, which is the only place
    this script has: it runs once per release and leaves nothing behind.
    Callers pass a bounded value; nothing here makes one bounded, and
    nothing here is given a token, a URL, an asset name or a payload.

    Parameters
    ----------
    name:
        The metric's name, under the `audit-draft` namespace.
    value:
        Its bounded value.
    """
    print(f"{METRIC_PREFIX}{name}={value}")


def outcome_of(error: BaseException | None) -> Outcome:
    """Return the bounded category one read failure belongs to.

    Parameters
    ----------
    error:
        What the attempt raised, or None if it succeeded.

    Returns
    -------
    Outcome
        The category.

    Examples
    --------
    >>> outcome_of(None)
    <Outcome.OK: 'ok'>
    >>> outcome_of(TimeoutError("timed out"))
    <Outcome.TRANSPORT_ERROR: 'transport-error'>
    """
    if error is None:
        return Outcome.OK
    if isinstance(error, urllib.error.HTTPError):
        if error.code in LOOKUP_RETRYABLE_STATUSES:
            return Outcome.RETRYABLE_STATUS
        return Outcome.PERMANENT_STATUS
    if isinstance(error, NOT_JSON_FAILURES):
        return Outcome.NOT_JSON
    return Outcome.TRANSPORT_ERROR


#: Statuses the release lookup tries again on, over and above the ones
#: any download retries. A release read soon after it was created can
#: answer 404 before GitHub is consistent, and the workflow does exactly
#: that: `create-release` creates the draft, `build` uploads to it, and
#: `audit` reads it back.
#:
#: Retrying 404 is not free. Under `DEFAULT_RETRY` an under-privileged
#: token, which answers 404 every time, now fails after four attempts
#: and three sleeps of 5, 10 and 15 seconds rather than at once. Thirty
#: seconds on a release that was going to fail anyway buys a race that
#: is otherwise invisible in the log, and the failure message is
#: unchanged.
LOOKUP_RETRYABLE_STATUSES: Final = RETRYABLE_STATUSES | {404}


def _read_json_once(
    url: str,
    headers: Mapping[str, str],
    *,
    subject: str,
    transport: Transport,
) -> object:
    """Read one JSON document, or raise the verdict on the failure.

    Parameters
    ----------
    url:
        The address to read.
    headers:
        The request headers, already carrying the token.
    subject:
        What is being read, for the failure messages.
    transport:
        What performs the read.

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
    try:
        return json.loads(transport(url, headers))
    except READ_FAILURES as error:
        raise _verdict_on(subject, error) from error


def _report_read(
    started: float, attempts: int, cause: BaseException | None, api: Api
) -> None:
    """Report how one release lookup ended.

    Parameters
    ----------
    started:
        The clock reading taken before the first attempt.
    attempts:
        How many attempts were made.
    cause:
        What the last attempt raised, or None if it succeeded.
    api:
        How to reach the API; its clock is read for the duration.
    """
    report("release-lookup.outcome", outcome_of(cause))
    report("release-lookup.attempts", attempts)
    report("release-lookup.latency", latency_bucket(api.clock() - started))


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
    cause: BaseException | None = None
    started = api.clock()
    attempt = 0
    try:
        for attempt in range(1, api.retry.attempts + 1):
            try:
                payload = _read_json_once(
                    url, headers, subject=subject, transport=api.transport
                )
            except _TransientError as transient:
                last = str(transient)
                cause = transient.__cause__
            except PackagingError as permanent:
                cause = permanent.__cause__
                raise
            else:
                cause = None
                return payload
            if attempt == api.retry.attempts:
                break
            print(f"attempt {attempt} to read {subject} failed: {last}; retrying")
            api.sleeper(api.retry.backoff * attempt)
        message = f"{last} (after {api.retry.attempts} attempts)"
        raise PackagingError(message)
    finally:
        # Reported from `finally` so the failing paths are counted too:
        # a retry rate is only readable next to the failures it did not
        # prevent, and those are exactly the runs that raise from here.
        _report_read(started, attempt, cause, api)


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


def release_url(repo: str, tag: str, root: str = DEFAULT_API) -> str:
    """Return the address of the release GitHub holds for ``tag``.

    A pure function of its arguments: it opens nothing and reads
    nothing. The quoting is the whole of its content, and the quoting
    has been wrong before, so it is worth being able to state the answer
    without a server in the way.

    Parameters
    ----------
    repo:
        The repository in ``owner/name`` form.
    tag:
        The release tag.
    root:
        The API root.

    Returns
    -------
    str
        The address of the release lookup.

    Examples
    --------
    >>> release_url("leynos/df12-dylint-builds", "v1.0.0")
    'https://api.github.com/repos/leynos/df12-dylint-builds/releases/tags/v1.0.0'
    >>> release_url("leynos/df12-dylint-builds", "release/6.0").rsplit("/", 1)[-1]
    'release%2F6.0'
    """
    owner_repo = urllib.parse.quote(repo)
    # ``safe=""`` because a tag is one path segment. The default keeps
    # ``/`` unencoded, so a tag such as ``release/6.0`` would address
    # ``releases/tags/release/6.0`` and come back as a not-found, which
    # reads here as the under-privileged-token case it is not.
    quoted_tag = urllib.parse.quote(tag, safe="")
    return f"{root}/repos/{owner_repo}/releases/tags/{quoted_tag}"


def release_from_payload(payload: object, *, subject: str) -> ReleasePayload:
    """Return ``payload`` as a release, or say what arrived instead.

    Pure: this decides what a decoded body is, and nothing else. The
    API answers a lookup with an array in at least one shape, and an
    array reaching `assets_of` fails on the wrong thing.

    Parameters
    ----------
    payload:
        A decoded JSON body.
    subject:
        What was being read, for the failure message.

    Returns
    -------
    ReleasePayload
        The release object.

    Raises
    ------
    PackagingError
        If the body is not an object.

    Examples
    --------
    >>> release_from_payload({"assets": []}, subject="a release")
    {'assets': []}
    """
    if not isinstance(payload, dict):
        message = f"{subject}: returned {type(payload).__name__}, not a release"
        raise PackagingError(message)
    return payload


def release_for_tag(repo: str, tag: str, api: Api) -> ReleasePayload:
    """Return the release GitHub holds for ``tag``.

    The fallible boundary: it is the composition of `release_url`, the
    retrying read through `api.transport`, and `release_from_payload`.
    Everything it decides is in one of those three; what it adds is the
    network.

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
    subject = f"{repo} release {tag}"
    payload = _read_json(release_url(repo, tag, api.root), api, subject=subject)
    # Reported separately from the lookup, and not from inside
    # `release_from_payload`, which is pure. The lookup metric measures a
    # retrying network operation; this measures the shape of what it
    # brought back, which no retry would change, so folding the two into
    # one outcome would put a permanent failure in a series read for
    # transient ones.
    outcome = Outcome.NOT_A_RELEASE
    try:
        release = release_from_payload(payload, subject=subject)
        outcome = Outcome.OK
    finally:
        report("release-payload.outcome", outcome)
    return release


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


def asset_target(asset: AssetPayload, destination: Path) -> tuple[str, Path]:
    """Return one asset's download URL and where it may be written.

    The name comes from the release rather than from this repository, so
    it is not trusted to stay inside the directory. Public because this
    containment rule is the one decision here that a caller cannot
    inspect its input for beforehand, and it is worth being able to
    generate names against it rather than list them.

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
    started = api.clock()
    # What would be reported if the next statement raised. Everything
    # below fails as `PackagingError`, so classifying by exception type
    # put an empty release, a name that would escape the directory and
    # an unreachable asset into one bucket named for the connection:
    # three faults, three remedies, one label. The stage is what tells
    # them apart, so it is recorded as it is reached rather than
    # reconstructed afterwards.
    stage = Outcome.NO_ASSETS
    try:
        for asset in assets_of(release):
            stage = Outcome.REJECTED_ASSET
            url, target = asset_target(asset, destination)
            stage = Outcome.ASSET_UNREADABLE
            download(
                url,
                target,
                retry=api.retry,
                headers=headers,
                transport=api.transport,
                sleeper=api.sleeper,
            )
            written.append(target)
        stage = Outcome.OK
    finally:
        # The count is how many arrived, not how many were listed: on a
        # failure the difference between the two is the whole story, and
        # neither number alone tells it.
        report("asset-download.outcome", stage)
        report("asset-download.written", len(written))
        report("asset-download.latency", latency_bucket(api.clock() - started))
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
