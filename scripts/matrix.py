"""Print the release build matrix and tag facts derived from ``dylint.toml``.

The release workflow calls this instead of hard-coding a target, a runner or
a version, so the configuration stays the only place any of them appear.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from dylint_config import Config, ConfigError, default_config_path, load_config


def check_tag(config: Config, tag: str) -> str:
    """Return ``tag`` if it is a release tag for the configured version.

    Release tags are ``v<dylint version>+build.<n>``: the version identifies
    what upstream released and the build number distinguishes reruns of this
    repository's own packaging of it.
    """
    if not config.tag_pattern().match(tag):
        raise ConfigError(
            f"release tags must be v{config.version}+build.<n>, found {tag!r}"
        )
    return tag


def _cmd_matrix(config: Config, _args: argparse.Namespace) -> int:
    print(json.dumps(config.matrix(), separators=(",", ":"), sort_keys=True))
    return 0


def _cmd_check_tag(config: Config, args: argparse.Namespace) -> int:
    print(check_tag(config, args.tag))
    return 0


def _cmd_facts(config: Config, _args: argparse.Namespace) -> int:
    for key, value in (
        ("version", config.version),
        ("repository", config.repository),
        ("tag", config.tag),
        ("commit", config.commit),
        ("features", config.features),
        ("binaries", " ".join(config.binaries)),
        ("upstream_runner", config.upstream.runner),
    ):
        print(f"{key}={value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to dylint.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("matrix", help="print the build matrix as JSON").set_defaults(
        func=_cmd_matrix
    )
    sub.add_parser(
        "facts", help="print upstream facts as GITHUB_OUTPUT assignments"
    ).set_defaults(func=_cmd_facts)
    tag = sub.add_parser("check-tag", help="validate a release tag")
    tag.add_argument("tag")
    tag.set_defaults(func=_cmd_check_tag)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config or default_config_path())
        return int(args.func(config, args))
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
