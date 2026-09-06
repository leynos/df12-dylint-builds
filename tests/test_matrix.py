"""Tests for the command-line interface the release workflow calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURE_CONFIG
from dylint_config import ConfigError, parse_config
from matrix import check_tag, main


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Write the fixture configuration to a file the CLI can read."""
    path = tmp_path / "dylint.toml"
    path.write_text(FIXTURE_CONFIG, encoding="utf-8")
    return path


def test_the_matrix_command_prints_parsable_json(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow feeds this straight to fromJSON, so it must parse."""
    assert main(["--config", str(config_path), "matrix"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == parse_config(FIXTURE_CONFIG).matrix()


def test_the_facts_command_emits_output_assignments(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Facts are written as key=value lines appended to GITHUB_OUTPUT."""
    assert main(["--config", str(config_path), "facts"]) == 0
    facts = dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines() if line
    )
    assert facts["version"] == "6.0.4"
    assert facts["commit"] == "09bf11417d8cfc4d0c2ef9053898c6a9f378f794"
    assert facts["features"] == "dylint/__driver_from_crates_io"


def test_a_valid_tag_is_echoed(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow uses the echoed tag, so it must come back unchanged."""
    assert main(["--config", str(config_path), "check-tag", "v6.0.4+build.2"]) == 0
    assert capsys.readouterr().out.strip() == "v6.0.4+build.2"


def test_a_tag_for_another_version_fails_the_release(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tag that disagrees with dylint.toml stops the release at the first job."""
    assert main(["--config", str(config_path), "check-tag", "v5.0.0+build.1"]) == 1
    assert "v6.0.4+build.<n>" in capsys.readouterr().err


def test_check_tag_reports_the_configured_version() -> None:
    """The error names the version the repository is configured to build."""
    config = parse_config(FIXTURE_CONFIG)
    with pytest.raises(ConfigError, match=r"v6\.0\.4\+build"):
        check_tag(config, "nonsense")


def test_a_missing_configuration_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A misplaced configuration fails the job instead of yielding an empty matrix."""
    assert main(["--config", str(tmp_path / "absent.toml"), "matrix"]) == 1
    assert "cannot read" in capsys.readouterr().err
