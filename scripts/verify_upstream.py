"""Download upstream's published archives and verify their sidecars.

This job builds nothing. It exists to prove that the checksum sidecar and
archive layout this repository publishes are the same ones upstream
publishes, so a consumer can treat every target the same way.
"""

from __future__ import annotations

import argparse
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


def download(
    url: str,
    destination: Path,
    *,
    attempts: int = ATTEMPTS,
    backoff: float = BACKOFF_SECONDS,
) -> Path:
    """Download ``url`` to ``destination``, retrying transient failures."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                destination.write_bytes(response.read())
        except (urllib.error.URLError, OSError) as error:
            last = error
            if attempt == attempts:
                break
            print(f"attempt {attempt} for {url} failed: {error}; retrying")
            time.sleep(backoff * attempt)
        else:
            return destination
    raise PackagingError(f"could not download {url} after {attempts} attempts: {last}")


def verify_upstream(config: Config, work: Path) -> list[str]:
    """Download and verify every upstream archive, returning their digests."""
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
    """Run the command-line interface."""
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
