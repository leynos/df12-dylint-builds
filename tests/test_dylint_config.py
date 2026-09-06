"""Unit tests for the configuration parser and the names it derives."""

from __future__ import annotations

import re

import pytest
from conftest import (
    FIXTURE_COMMIT,
    FIXTURE_CONFIG,
    FIXTURE_VERSION,
    POSIX_TARGET,
    WINDOWS_TARGET,
)
from dylint_config import (
    Config,
    ConfigError,
    default_config_path,
    exe_suffix,
    load_config,
    parse_config,
)

# The shape of every archive upstream publishes, taken from the asset list of
# https://github.com/trailofbits/dylint/releases/tag/v6.0.4.
UPSTREAM_NAME_RE = re.compile(
    r"^(cargo-dylint|dylint-link)-[0-9a-z_]+-[0-9a-z_]+-[0-9a-z_-]+-v"
    r"[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$"
)


@pytest.fixture
def repo_config() -> Config:
    """Return the repository's own committed configuration."""
    return load_config(default_config_path())


def test_the_repository_configuration_parses(repo_config: Config) -> None:
    """The committed dylint.toml is valid and names the version it builds."""
    assert repo_config.version == FIXTURE_VERSION
    assert repo_config.binaries == ("cargo-dylint", "dylint-link")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("x86_64-pc-windows-msvc", ".exe", id="windows-msvc"),
        pytest.param("x86_64-apple-darwin", "", id="apple-darwin"),
        pytest.param("x86_64-unknown-linux-gnu", "", id="linux-gnu"),
    ],
)
def test_the_executable_suffix_follows_the_triple(target: str, expected: str) -> None:
    """Only Windows targets carry .exe, and the triple alone decides it."""
    assert exe_suffix(target) == expected


@pytest.mark.parametrize(
    ("binary", "target", "fmt", "expected"),
    [
        pytest.param(
            "cargo-dylint",
            POSIX_TARGET,
            "tar.gz",
            "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz",
            id="cargo-dylint-darwin-targz",
        ),
        pytest.param(
            "dylint-link",
            WINDOWS_TARGET,
            "zip",
            "dylint-link-x86_64-pc-windows-msvc-v6.0.4.zip",
            id="dylint-link-windows-zip",
        ),
    ],
)
def test_archive_names_are_upstream_shaped(
    fixture_config: Config, binary: str, target: str, fmt: str, expected: str
) -> None:
    """Archive names are <binary>-<target>-v<version>.<format>, as upstream's are."""
    assert fixture_config.archive_name(binary, target, fmt) == expected


def test_the_tar_names_match_the_upstream_pattern(fixture_config: Config) -> None:
    """Every tar.gz this repository publishes matches upstream's name shape."""
    names = [
        name
        for name in fixture_config.released_archive_names()
        if name.endswith(".tar.gz")
    ]
    assert names
    for name in names:
        assert UPSTREAM_NAME_RE.match(name), name


def test_the_upstream_names_match_the_published_assets(fixture_config: Config) -> None:
    """The names the release downloads are the assets upstream actually has."""
    assert fixture_config.upstream_archive_names() == (
        "cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz",
        "dylint-link-x86_64-unknown-linux-gnu-v6.0.4.tar.gz",
    )


def test_the_upstream_url_uses_the_pinned_tag(fixture_config: Config) -> None:
    """Upstream downloads come from the pinned tag, not from latest."""
    assert fixture_config.upstream_url("a.tar.gz") == (
        "https://github.com/trailofbits/dylint/releases/download/v6.0.4/a.tar.gz"
    )


def test_the_stem_is_the_directory_inside_the_archive(fixture_config: Config) -> None:
    """The archive stem and its single directory entry are the same string."""
    assert fixture_config.stem("cargo-dylint", POSIX_TARGET) == (
        "cargo-dylint-x86_64-apple-darwin-v6.0.4"
    )


@pytest.mark.parametrize(
    ("tag", "accepted"),
    [
        pytest.param("v6.0.4+build.1", True, id="first-build"),
        pytest.param("v6.0.4+build.12", True, id="later-build"),
        pytest.param("v6.0.4", False, id="upstream-tag-alone"),
        pytest.param("v6.0.3+build.1", False, id="other-version"),
        pytest.param("v6.0.4-build.1", False, id="hyphen-instead-of-plus"),
        pytest.param("v6.0.4+build.01", False, id="leading-zero"),
        pytest.param("v6.0.4+build.", False, id="no-number"),
        pytest.param("v6.0.4+build.1-rc", False, id="trailing-junk"),
    ],
)
def test_release_tags_name_the_version_they_build(
    fixture_config: Config, tag: str, accepted: bool
) -> None:
    """A tag must carry the configured version and a build number."""
    assert bool(fixture_config.tag_pattern().match(tag)) is accepted


def test_the_matrix_carries_each_target_once(fixture_config: Config) -> None:
    """The matrix has one leg per configured target with its runner label."""
    assert fixture_config.matrix() == {
        "include": [
            {
                "target": "x86_64-apple-darwin",
                "runner": "macos-15-intel",
                "formats": "tar.gz",
            },
            {
                "target": "x86_64-pc-windows-msvc",
                "runner": "windows-latest",
                "formats": "tar.gz zip",
            },
        ]
    }


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            ("schema_version = 1", "schema_version = 2"), "schema_version", id="schema"
        ),
        pytest.param(
            ('version = "6.0.4"', 'version = "6.0"'), "MAJOR.MINOR.PATCH", id="version"
        ),
        pytest.param(
            (f'commit = "{FIXTURE_COMMIT}"', 'commit = "abc"'),
            "40-hex",
            id="commit",
        ),
        pytest.param(
            ('tag = "v6.0.4"', 'tag = "v6.0.5"'), "dylint.tag must be", id="tag"
        ),
        pytest.param(
            ('runner = "windows-latest"', 'runner = ""'),
            "non-empty string",
            id="runner",
        ),
        pytest.param(
            ('formats = ["tar.gz"]', 'formats = ["rar"]'), "unknown format", id="format"
        ),
        pytest.param(
            ('binaries = ["cargo-dylint", "dylint-link"]', "binaries = []"),
            "non-empty array",
            id="binaries",
        ),
        pytest.param(
            (
                'targets = ["x86_64-unknown-linux-gnu"]',
                'targets = ["x86_64-apple-darwin"]',
            ),
            "overlap",
            id="overlapping-targets",
        ),
    ],
)
def test_a_broken_configuration_is_refused(edit: tuple[str, str], message: str) -> None:
    """Each validation rule rejects its own kind of breakage with a clear error."""
    source, replacement = edit
    text = FIXTURE_CONFIG
    assert source in text, source
    with pytest.raises(ConfigError, match=message):
        parse_config(text.replace(source, replacement, 1))


def test_an_unknown_target_is_named_in_the_error(fixture_config: Config) -> None:
    """Asking for a target that is not configured fails loudly."""
    with pytest.raises(ConfigError, match="unknown target: sparc-unknown-none"):
        fixture_config.target("sparc-unknown-none")


def test_malformed_toml_is_reported_as_such() -> None:
    """A syntax error names the file rather than escaping as a TOMLDecodeError."""
    with pytest.raises(ConfigError, match="not valid TOML"):
        parse_config("schema_version = ")
