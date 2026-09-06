"""Parse and validate ``dylint.toml``, the single source of truth.

Everything the release workflow needs, the build matrix, the archive names
and the upstream URLs, is derived from this module so that a target, a
version or a runner label is written down exactly once.
"""

from __future__ import annotations

import dataclasses as dc
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final = 1
FORMATS: Final = ("tar.gz", "zip")
WINDOWS_SUFFIX: Final = "-pc-windows-msvc"
EXE_SUFFIX: Final = ".exe"

VERSION_RE: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_RE: Final = re.compile(r"^[0-9a-f]{40}$")
TARGET_RE: Final = re.compile(r"^[0-9a-z_]+-[0-9a-z_]+-[0-9a-z_-]+$")
BINARY_RE: Final = re.compile(r"^[a-z][0-9a-z-]*$")


class ConfigError(ValueError):
    """Raised when ``dylint.toml`` is missing, malformed or inconsistent."""


def exe_suffix(target: str) -> str:
    """Return the executable suffix a target's binaries carry.

    The suffix is derived from the target triple rather than configured, so
    a configuration cannot claim a Windows target without ``.exe``.
    """
    return EXE_SUFFIX if target.endswith(WINDOWS_SUFFIX) else ""


@dc.dataclass(frozen=True, slots=True)
class Target:
    """A target this repository builds, and the runner that builds it."""

    triple: str
    runner: str
    formats: tuple[str, ...]

    @property
    def exe_suffix(self) -> str:
        """Return the executable suffix for binaries built for this target."""
        return exe_suffix(self.triple)


@dc.dataclass(frozen=True, slots=True)
class Upstream:
    """The upstream-published targets whose sidecars the release verifies."""

    runner: str
    releases_url: str
    targets: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class Config:
    """A validated ``dylint.toml``."""

    version: str
    repository: str
    tag: str
    commit: str
    features: str
    binaries: tuple[str, ...]
    targets: tuple[Target, ...]
    upstream: Upstream

    @property
    def target_triples(self) -> tuple[str, ...]:
        """Return the built target triples in configuration order."""
        return tuple(target.triple for target in self.targets)

    def target(self, triple: str) -> Target:
        """Return the configured target named ``triple``."""
        for candidate in self.targets:
            if candidate.triple == triple:
                return candidate
        raise ConfigError(f"unknown target: {triple}")

    def stem(self, binary: str, target: str) -> str:
        """Return the archive stem, which is also the directory inside it."""
        return f"{binary}-{target}-v{self.version}"

    def archive_name(self, binary: str, target: str, fmt: str) -> str:
        """Return the archive file name for one binary, target and format."""
        if fmt not in FORMATS:
            raise ConfigError(f"unknown archive format: {fmt}")
        return f"{self.stem(binary, target)}.{fmt}"

    def archive_names(self, target: str) -> tuple[str, ...]:
        """Return every archive name produced for ``target``."""
        spec = self.target(target)
        return tuple(
            self.archive_name(binary, target, fmt)
            for binary in self.binaries
            for fmt in spec.formats
        )

    def released_archive_names(self) -> tuple[str, ...]:
        """Return every archive name the release is expected to publish."""
        return tuple(
            name
            for target in self.target_triples
            for name in self.archive_names(target)
        )

    def upstream_archive_names(self) -> tuple[str, ...]:
        """Return the upstream archive names the release downloads."""
        return tuple(
            self.archive_name(binary, target, "tar.gz")
            for target in self.upstream.targets
            for binary in self.binaries
        )

    def upstream_url(self, name: str) -> str:
        """Return the upstream download URL for an archive or sidecar."""
        return f"{self.upstream.releases_url}/{self.tag}/{name}"

    def tag_pattern(self) -> re.Pattern[str]:
        """Return the pattern a release tag for this configuration must match.

        The dylint version is baked into the pattern, so a tag can never
        disagree with the version the workflow actually builds.
        """
        return re.compile(rf"^v{re.escape(self.version)}\+build\.(?!0[0-9])[0-9]+$")

    def matrix(self) -> dict[str, list[dict[str, str]]]:
        """Return the build matrix the release workflow consumes."""
        return {
            "include": [
                {
                    "target": target.triple,
                    "runner": target.runner,
                    "formats": " ".join(target.formats),
                }
                for target in self.targets
            ]
        }


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _require_str(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = _require(mapping, key, where)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value


def _require_str_list(
    mapping: Mapping[str, Any], key: str, where: str
) -> tuple[str, ...]:
    value = _require(mapping, key, where)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}.{key} must be a non-empty array")
    if not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{where}.{key} must contain only non-empty strings")
    if len(set(value)) != len(value):
        raise ConfigError(f"{where}.{key} must not repeat an entry")
    return tuple(value)


def _parse_targets(raw: Mapping[str, Any]) -> tuple[Target, ...]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("targets must be a non-empty table")
    targets: list[Target] = []
    for triple, spec in raw.items():
        where = f"targets.{triple}"
        if not TARGET_RE.match(triple):
            raise ConfigError(f"{where}: not a target triple")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a table")
        formats = _require_str_list(spec, "formats", where)
        unknown = [fmt for fmt in formats if fmt not in FORMATS]
        if unknown:
            raise ConfigError(f"{where}.formats: unknown format(s) {unknown}")
        targets.append(
            Target(
                triple=triple,
                runner=_require_str(spec, "runner", where),
                formats=formats,
            )
        )
    return tuple(targets)


def parse_config(text: str) -> Config:
    """Parse and validate a configuration document."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"dylint.toml is not valid TOML: {error}") from error

    schema = raw.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}, found {schema!r}")

    dylint = _require(raw, "dylint", "dylint.toml")
    if not isinstance(dylint, dict):
        raise ConfigError("dylint must be a table")

    version = _require_str(dylint, "version", "dylint")
    if not VERSION_RE.match(version):
        raise ConfigError(
            f"dylint.version must be MAJOR.MINOR.PATCH, found {version!r}"
        )

    commit = _require_str(dylint, "commit", "dylint")
    if not COMMIT_RE.match(commit):
        raise ConfigError("dylint.commit must be a 40-hex commit SHA")

    tag = _require_str(dylint, "tag", "dylint")
    if tag != f"v{version}":
        raise ConfigError(f"dylint.tag must be v{version}, found {tag!r}")

    binaries = _require_str_list(dylint, "binaries", "dylint")
    invalid = [name for name in binaries if not BINARY_RE.match(name)]
    if invalid:
        raise ConfigError(f"dylint.binaries: not binary names: {invalid}")

    upstream_raw = _require(raw, "upstream", "dylint.toml")
    if not isinstance(upstream_raw, dict):
        raise ConfigError("upstream must be a table")
    upstream = Upstream(
        runner=_require_str(upstream_raw, "runner", "upstream"),
        releases_url=_require_str(upstream_raw, "releases_url", "upstream"),
        targets=_require_str_list(upstream_raw, "targets", "upstream"),
    )

    targets = _parse_targets(_require(raw, "targets", "dylint.toml"))
    overlap = sorted(set(upstream.targets) & {target.triple for target in targets})
    if overlap:
        raise ConfigError(
            f"targets and upstream.targets overlap: {overlap}; this repository "
            "exists to publish what upstream does not"
        )

    return Config(
        version=version,
        repository=_require_str(dylint, "repository", "dylint"),
        tag=tag,
        commit=commit,
        features=_require_str(dylint, "features", "dylint"),
        binaries=binaries,
        targets=targets,
        upstream=upstream,
    )


def load_config(path: Path | str) -> Config:
    """Load and validate the configuration at ``path``."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    return parse_config(text)


def default_config_path() -> Path:
    """Return the repository's own ``dylint.toml``."""
    return Path(__file__).resolve().parents[1] / "dylint.toml"


def iter_binary_targets(config: Config) -> Iterator[tuple[str, Target]]:
    """Yield every (binary, target) pair the repository builds."""
    for target in config.targets:
        for binary in config.binaries:
            yield binary, target


__all__: Sequence[str] = (
    "Config",
    "ConfigError",
    "Target",
    "Upstream",
    "default_config_path",
    "exe_suffix",
    "iter_binary_targets",
    "load_config",
    "parse_config",
)
