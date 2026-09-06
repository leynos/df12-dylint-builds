"""Download upstream's published archives and verify their sidecars.

This job builds nothing. It exists to prove that the checksum sidecar and
archive layout this repository publishes are the same ones upstream
publishes, so a consumer can treat every target the same way.
"""

from __future__ import annotations

import argparse
import http.client
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from dylint_config import Config, ConfigError, default_config_path, load_config
from package import PackagingError, check_layout, check_sidecar

ATTEMPTS: Final = 4
BACKOFF_SECONDS: Final = 5
TIMEOUT_SECONDS: Final = 120
USER_AGENT: Final = "df12-dylint-builds"
# Statuses worth another attempt. Anything else is the server saying no,
# and asking again four times only delays the failure.
RETRYABLE_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})


def download(
    url: str,
    destination: Path,
    *,
    attempts: int = ATTEMPTS,
    backoff: float = BACKOFF_SECONDS,
) -> Path:
    """Download ``url`` to ``destination``, retrying transient failures.

    Parameters
    ----------
    url:
        The address to fetch.
    destination:
        Where to write the response body. Nothing is written unless the whole
        body arrives, so a truncated response cannot be mistaken for an
        archive.
    attempts:
        How many times to try before giving up.
    backoff:
        Seconds to wait after the first failure, scaled by the attempt number.

    Returns
    -------
    Path
        ``destination``.

    Raises
    ------
    PackagingError
        If the server returns a status that will not change on a retry, if
        every attempt fails, or if the body arrives but ``destination``
        cannot be written.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUSES:
                raise PackagingError(
                    f"{url}: server returned HTTP {error.code}"
                ) from error
            last = error
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            # A truncated body arrives here as an IncompleteRead, so a short
            # response is retried rather than written out as an archive.
            last = error
        else:
            try:
                destination.write_bytes(payload)
            except OSError as error:
                # The body arrived; the failure is local, and no number of
                # retries will make the destination writable.
                raise PackagingError(
                    f"could not write {destination}: {error}"
                ) from error
            return destination
        if attempt == attempts:
            break
        print(f"attempt {attempt} for {url} failed: {last}; retrying")
        time.sleep(backoff * attempt)
    raise PackagingError(f"could not download {url} after {attempts} attempts: {last}")


def verify_upstream(config: Config, work: Path) -> list[str]:
    """Download and verify every upstream archive.

    Parameters
    ----------
    config:
        The configuration naming the upstream tag, targets and binaries.
    work:
        A directory to download into; it is created if it does not exist.

    Returns
    -------
    list of str
        The digest of each archive, in the order they were checked.

    Raises
    ------
    PackagingError
        If a download fails, or if an archive does not match its sidecar or
        the layout this repository publishes under.
    """
    work.mkdir(parents=True, exist_ok=True)
    digests: list[str] = []
    for target in config.upstream.targets:
        for binary in config.binaries:
            name = config.archive_name(binary, target, "tar.gz")
            archive = download(config.upstream_url(name), work / name)
            download(config.upstream_url(f"{name}.sha256"), work / f"{name}.sha256")
            digest = check_sidecar(archive)
            check_layout(archive, config.stem(binary, target), binary)
            print(f"verified upstream {name} {digest}")
            digests.append(digest)
    return digests


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface.

    Parameters
    ----------
    argv:
        Arguments to parse, defaulting to ``sys.argv[1:]``.

    Returns
    -------
    int
        Zero when every upstream archive verified, one otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to dylint.toml")
    parser.add_argument("--work-dir", default="upstream-dist")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config or default_config_path())
        verify_upstream(config, Path(args.work_dir))
    except (ConfigError, PackagingError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
