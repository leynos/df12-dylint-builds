"""Tests for the job that checks upstream's own archives and sidecars."""

from __future__ import annotations

import functools
import http.server
import tarfile
import threading
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FIXTURE_COMMIT, FIXTURE_CONFIG, write_stubs
from dylint_config import parse_config
from package import PackagingError, pack, write_sidecar
from verify_upstream import download, main, verify_upstream

UPSTREAM_TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """A file server that does not narrate every request to the test output."""

    def log_message(self, *args: object) -> None:
        """Discard the access log."""


class _FlakyHandler(http.server.BaseHTTPRequestHandler):
    """Answer 503 once per path, then serve the real bytes.

    A retryable status is not a permanent one, and the only way to tell the
    two apart is to serve a status that later succeeds.
    """

    served: typing.ClassVar[set[str]] = set()
    payload: typing.ClassVar[bytes] = b"recovered"

    def do_GET(self) -> None:
        """Fail the first request for a path and satisfy the second."""
        if self.path not in self.served:
            self.served.add(self.path)
            self.send_error(503, "try again")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: object) -> None:
        """Discard the access log."""


class _TruncatingHandler(http.server.BaseHTTPRequestHandler):
    """Promise more bytes than it sends, as a dropped connection would."""

    def do_GET(self) -> None:
        """Send a short body under an honest-looking Content-Length."""
        self.send_response(200)
        self.send_header("Content-Length", "4096")
        self.end_headers()
        self.wfile.write(b"short")

    def log_message(self, *args: object) -> None:
        """Discard the access log."""


def _serving_config(base_url: str) -> str:
    """Return a configuration whose upstream release URL points at a fixture."""
    return FIXTURE_CONFIG.replace(
        'releases_url = "https://github.com/trailofbits/dylint/releases/download"',
        f'releases_url = "{base_url}"',
    )


@pytest.fixture
def upstream_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Serve a directory over HTTP and yield its base URL and the served path."""
    root = tmp_path / "served" / "v6.0.4"
    root.mkdir(parents=True)

    handler = functools.partial(_QuietHandler, directory=str(tmp_path / "served"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# A configuration that builds the targets upstream publishes. It exists only
# to manufacture archives shaped exactly like upstream's for the fixture
# server, so the checks below run against something upstream-like. Its own
# upstream list names a target it does not build, because a configuration is
# not allowed to build what it claims upstream publishes.
PUBLISHER_CONFIG = f"""
schema_version = 1

[dylint]
version = "6.0.4"
repository = "https://github.com/trailofbits/dylint"
tag = "v6.0.4"
commit = "{FIXTURE_COMMIT}"
features = "dylint/__driver_from_crates_io"
binaries = ["cargo-dylint", "dylint-link"]

[targets."{UPSTREAM_TARGETS[0]}"]
runner = "ubuntu-24.04"
formats = ["tar.gz"]

[targets."{UPSTREAM_TARGETS[1]}"]
runner = "ubuntu-24.04-arm"
formats = ["tar.gz"]

[upstream]
runner = "ubuntu-24.04"
releases_url = "https://example.invalid"
targets = ["x86_64-apple-darwin"]
"""


def _publish_upstream_fixture(root: Path, tmp_path: Path) -> None:
    """Package archives for each upstream target and serve them as upstream would."""
    config = parse_config(PUBLISHER_CONFIG)
    for target in UPSTREAM_TARGETS:
        pack(config, target, write_stubs(tmp_path / "release"), root)


def test_upstreams_archives_pass_our_own_checks(
    upstream_server: tuple[str, Path], tmp_path: Path
) -> None:
    """The sidecar and layout rules we publish under are the rules upstream meets."""
    base_url, root = upstream_server
    _publish_upstream_fixture(root, tmp_path)
    config = parse_config(_serving_config(base_url))
    digests = verify_upstream(config, tmp_path / "work")
    assert len(digests) == len(config.binaries) * len(config.upstream.targets), (
        "every upstream binary must be checked for every upstream target"
    )


def test_a_corrupt_upstream_archive_fails_the_check(
    upstream_server: tuple[str, Path], tmp_path: Path
) -> None:
    """A download that does not match its sidecar stops the release."""
    base_url, root = upstream_server
    _publish_upstream_fixture(root, tmp_path)
    archive = root / "cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"corrupt")
    config = parse_config(_serving_config(base_url))
    with pytest.raises(PackagingError, match="does not match sidecar"):
        verify_upstream(config, tmp_path / "work")


def test_an_upstream_archive_with_a_stray_member_fails_the_layout_check(
    upstream_server: tuple[str, Path], tmp_path: Path
) -> None:
    """Upstream's archives are held to this repository's layout, not just its digests.

    The archive is repacked with an extra member and its sidecar rewritten to
    match, so the download and the checksum both succeed and only the layout
    check can object. Deleting the ``check_layout`` call in ``verify_upstream``
    makes this test fail; without it the suite passes with that call removed.
    """
    base_url, root = upstream_server
    _publish_upstream_fixture(root, tmp_path)
    archive = root / f"cargo-dylint-{UPSTREAM_TARGETS[0]}-v6.0.4.tar.gz"
    stem = f"cargo-dylint-{UPSTREAM_TARGETS[0]}-v6.0.4"
    payload = tmp_path / "payload"
    payload.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname=f"{stem}/cargo-dylint")
        tar.add(payload, arcname=f"{stem}/LICENSE")
    write_sidecar(archive)
    config = parse_config(_serving_config(base_url))
    with pytest.raises(PackagingError, match="layout is"):
        verify_upstream(config, tmp_path / "work")


def test_a_download_that_cannot_be_written_names_the_destination(
    upstream_server: tuple[str, Path], tmp_path: Path
) -> None:
    """A local write failure is a packaging error, not an unhandled OSError.

    The body arrives, so the retry loop has nothing to retry; only the write
    can fail, and the command line handles no other exception type.
    """
    base_url, root = upstream_server
    (root / "probe.txt").write_bytes(b"probe")
    destination = tmp_path / "absent-directory" / "probe.txt"
    with pytest.raises(PackagingError, match="could not write"):
        download(f"{base_url}/v6.0.4/probe.txt", destination, backoff=0)


def test_a_download_is_retried_before_it_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Transient network failures are retried, and the last error is reported."""
    with pytest.raises(PackagingError, match="after 2 attempts"):
        download("http://127.0.0.1:1/absent", tmp_path / "out", attempts=2, backoff=0)
    assert "attempt 1" in capsys.readouterr().out


def test_a_download_returns_the_destination(
    upstream_server: tuple[str, Path], tmp_path: Path
) -> None:
    """A successful download writes the bytes it fetched."""
    base_url, root = upstream_server
    (root / "probe.txt").write_bytes(b"probe")
    destination = download(f"{base_url}/v6.0.4/probe.txt", tmp_path / "probe.txt")
    assert destination.read_bytes() == b"probe"


@pytest.fixture
def truncating_server() -> Iterator[str]:
    """Serve responses whose bodies are shorter than their Content-Length."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncatingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_permanent_status_is_not_retried(
    upstream_server: tuple[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 404 will not become a 200, so it fails at once and says so."""
    base_url, _ = upstream_server
    with pytest.raises(PackagingError, match="HTTP 404"):
        download(f"{base_url}/v6.0.4/absent.tar.gz", tmp_path / "out", backoff=0)
    assert "retrying" not in capsys.readouterr().out, (
        "a status that will not change must fail without a retry"
    )


def test_a_truncated_response_is_retried_and_never_written(
    truncating_server: str, tmp_path: Path
) -> None:
    """A body shorter than its Content-Length is a failure, not a short archive."""
    destination = tmp_path / "out"
    with pytest.raises(PackagingError, match="after 2 attempts"):
        download(f"{truncating_server}/anything", destination, attempts=2, backoff=0)
    assert not destination.exists(), "a truncated body must not be written out"


@pytest.fixture
def flaky_server() -> Iterator[str]:
    """Serve one 503 per path before serving the real bytes."""
    _FlakyHandler.served = set()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_retryable_status_is_retried_and_then_succeeds(
    flaky_server: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 503 is a request to come back, so the download comes back.

    Mutation: treating every HTTP status as permanent, by removing the
    ``RETRYABLE_STATUSES`` test, passes every other download test. Only this
    one distinguishes a status worth retrying from one that is not.
    """
    destination = download(f"{flaky_server}/asset.tar.gz", tmp_path / "out", backoff=0)
    assert destination.read_bytes() == _FlakyHandler.payload, (
        "the retry must write the body of the attempt that succeeded"
    )
    assert "retrying" in capsys.readouterr().out, (
        "a retryable status must say it is coming back"
    )


def test_the_command_line_verifies_upstream_and_exits_zero(
    upstream_server: tuple[str, Path], tmp_path: Path
) -> None:
    """The release and CI workflows invoke this entry point, so it is tested."""
    base_url, root = upstream_server
    _publish_upstream_fixture(root, tmp_path)
    config_path = tmp_path / "dylint.toml"
    config_path.write_text(_serving_config(base_url), encoding="utf-8")
    status = main(["--config", str(config_path), "--work-dir", str(tmp_path / "work")])
    assert status == 0, "a verified upstream must exit zero"


def test_the_command_line_reports_a_corrupt_upstream_archive(
    upstream_server: tuple[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure names the archive on stderr and exits one, rather than raising.

    ``main`` handles only ``ConfigError`` and ``PackagingError``; anything
    else would reach the release log as a traceback.
    """
    base_url, root = upstream_server
    _publish_upstream_fixture(root, tmp_path)
    archive = root / f"cargo-dylint-{UPSTREAM_TARGETS[0]}-v6.0.4.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"corrupt")
    config_path = tmp_path / "dylint.toml"
    config_path.write_text(_serving_config(base_url), encoding="utf-8")
    status = main(["--config", str(config_path), "--work-dir", str(tmp_path / "work")])
    assert status == 1, "a corrupt upstream archive must fail the command"
    stderr = capsys.readouterr().err
    assert "does not match sidecar" in stderr, (
        "the failure must say what was wrong with the archive"
    )
    assert archive.name in stderr, "the failure must name the archive that failed"


def test_the_command_line_reports_an_unreadable_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad configuration is the other error the entry point handles."""
    config_path = tmp_path / "dylint.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")
    status = main(["--config", str(config_path), "--work-dir", str(tmp_path / "work")])
    assert status == 1, "an invalid configuration must fail the command"
    assert "error: " in capsys.readouterr().err, (
        "a configuration failure must be reported on stderr"
    )
