"""Shared fixtures: a small validated configuration and stub binaries.

The stubs are executable scripts that imitate the two real binaries closely
enough for the packaging and verification code paths to run end to end
without a Rust toolchain.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dylint_config import Config, parse_config

FIXTURE_VERSION = "6.0.4"
FIXTURE_COMMIT = "09bf11417d8cfc4d0c2ef9053898c6a9f378f794"

FIXTURE_CONFIG = f"""
schema_version = 1

[dylint]
version = "{FIXTURE_VERSION}"
repository = "https://github.com/trailofbits/dylint"
tag = "v{FIXTURE_VERSION}"
commit = "{FIXTURE_COMMIT}"
features = "dylint/__driver_from_crates_io"
binaries = ["cargo-dylint", "dylint-link"]

[targets."x86_64-apple-darwin"]
runner = "macos-15-intel"
formats = ["tar.gz"]

[targets."x86_64-pc-windows-msvc"]
runner = "windows-latest"
formats = ["tar.gz", "zip"]

[upstream]
runner = "ubuntu-24.04"
releases_url = "https://github.com/trailofbits/dylint/releases/download"
targets = ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"]
"""

# A target with no ``.exe`` suffix and one archive format, so tests that do
# not care about Windows stay short.
POSIX_TARGET = "x86_64-apple-darwin"
WINDOWS_TARGET = "x86_64-pc-windows-msvc"

STUB_SOURCES = {
    "cargo-dylint": (
        "#!/bin/sh\n"
        'if [ "$1" = "dylint" ] && [ "$2" = "--version" ]; then\n'
        f'  echo "cargo-dylint {FIXTURE_VERSION}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "error: unexpected argument" >&2\n'
        "exit 2\n"
    ),
    # The real dylint-link forwards to the platform linker and has no version
    # of its own; the stub only has to start and say something.
    "dylint-link": (
        "#!/bin/sh\n"
        'echo "dylint-link: RUSTUP_TOOLCHAIN=${RUSTUP_TOOLCHAIN:-unset}"\n'
        "exit 0\n"
    ),
}

requires_posix_exec = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stub binaries are shell scripts, which Windows cannot execute",
)


@pytest.fixture
def fixture_config() -> Config:
    """Return a validated configuration matching this repository's shape."""
    return parse_config(FIXTURE_CONFIG)


def write_stubs(directory: Path, *, exe_suffix: str = "") -> Path:
    """Write executable stubs for every configured binary into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in STUB_SOURCES.items():
        path = directory / f"{name}{exe_suffix}"
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | 0o755)
    return directory


@pytest.fixture
def stub_source(tmp_path: Path) -> Path:
    """Return a directory of stub binaries for the POSIX fixture target."""
    return write_stubs(tmp_path / "release")


@pytest.fixture
def packed_dist(tmp_path: Path, fixture_config: Config, stub_source: Path) -> Path:
    """Return a dist directory holding the POSIX target's packed archives."""
    from package import pack

    dist = tmp_path / "dist"
    pack(fixture_config, POSIX_TARGET, stub_source, dist)
    return dist


@pytest.fixture
def complete_dist(tmp_path: Path, fixture_config: Config, stub_source: Path) -> Path:
    """Return a dist directory holding every asset the release publishes.

    A whole-release audit compares the directory against the complete
    expected set, so a test of that audit needs a directory that is complete
    to begin with; otherwise every such test also reports missing assets and
    cannot tell one failure from another.
    """
    from package import pack

    dist = tmp_path / "complete-dist"
    pack(fixture_config, POSIX_TARGET, stub_source, dist)
    windows_source = write_stubs(tmp_path / "release-windows", exe_suffix=".exe")
    pack(fixture_config, WINDOWS_TARGET, windows_source, dist)
    return dist


@pytest.fixture(autouse=True)
def _quiet_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's ambient toolchain out of the stub smoke tests."""
    monkeypatch.delenv("RUSTUP_TOOLCHAIN", raising=False)
    assert "RUSTUP_TOOLCHAIN" not in os.environ
