"""Contracts for the release and CI workflows.

Each assertion matches the mechanism it protects, the exact ``run:`` command,
``uses:`` reference or runner label, rather than a step name or a comment, so
deleting the protected line fails the contract even when its description
survives. Every contract's doc comment records the mutation that was applied
once to prove it fails.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from dylint_config import Config, default_config_path, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")
WORKFLOW_NAMES = ("release.yml", "ci.yml")

# Upstream's asset names, from the release this repository mirrors. The
# archives published here must be indistinguishable in shape from these.
UPSTREAM_ASSET_NAMES = (
    "cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz",
    "cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz.sha256",
    "dylint-link-x86_64-unknown-linux-gnu-v6.0.4.tar.gz",
    "dylint-link-x86_64-unknown-linux-gnu-v6.0.4.tar.gz.sha256",
)
UPSTREAM_NAME_RE = re.compile(
    r"^(?P<binary>cargo-dylint|dylint-link)-(?P<target>[0-9a-z_]+-[0-9a-z_]+-[0-9a-z_-]+)"
    r"-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz$"
)

# Commands that would compile or fetch a tool on the runner. Building dylint
# itself is the point of this repository and is matched separately.
TOOL_INSTALL_TOKENS = (
    re.compile(r"\bcargo install\b"),
    re.compile(r"\bpip3? install\b"),
    re.compile(r"\bapt(-get)? install\b"),
    re.compile(r"\bbrew install\b"),
    re.compile(r"\bnpm install\b"),
    re.compile(r"\bcurl\b[^\n]*\|\s*(ba)?sh\b"),
)


def load_workflow(name: str) -> dict[str, Any]:
    """Parse a workflow file into a dictionary."""
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def jobs_of(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the workflow's jobs."""
    return workflow["jobs"]


def steps_of(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    """Return the steps of ``job``."""
    return workflow["jobs"][job].get("steps", [])


def run_commands(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """Return every (job, run command) pair in the workflow."""
    return [
        (job, step["run"])
        for job, spec in jobs_of(workflow).items()
        for step in spec.get("steps", [])
        if "run" in step
    ]


def steps_running(workflow: dict[str, Any], job: str, pattern: str) -> list[str]:
    """Return the ``run`` bodies in ``job`` that contain ``pattern``."""
    return [
        step["run"]
        for step in steps_of(workflow, job)
        if pattern in step.get("run", "")
    ]


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    """Return the parsed release workflow."""
    return load_workflow("release.yml")


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    """Return the parsed CI workflow."""
    return load_workflow("ci.yml")


@pytest.fixture(scope="module")
def config() -> Config:
    """Return the repository's committed configuration."""
    return load_config(default_config_path())


# --- pinning and triggers ---------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_every_action_is_pinned_by_commit_sha(name: str) -> None:
    """``uses:`` references carry a 40-hex commit, never a tag or a branch.

    Mutation: pinning actions/setup-python to ``@v7`` failed this contract.
    """
    workflow = load_workflow(name)
    for job, spec in jobs_of(workflow).items():
        for step in spec.get("steps", []):
            if "uses" in step:
                assert SHA_PIN.match(step["uses"]), (
                    f"{name}/{job}: unpinned {step['uses']}"
                )


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_every_checkout_refuses_to_persist_credentials(name: str) -> None:
    """Checkouts leave no token in the working tree for a later step to reuse.

    Mutation: removing ``persist-credentials: false`` from the release
    workflow's first checkout failed this contract.
    """
    workflow = load_workflow(name)
    for job, spec in jobs_of(workflow).items():
        for step in spec.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step.get("with", {}).get("persist-credentials") is False, (
                    f"{name}/{job}: checkout must set persist-credentials: false"
                )


def test_the_release_workflow_runs_only_on_tags(release: dict[str, Any]) -> None:
    """The release triggers on tag pushes and on nothing else.

    Mutation: adding a ``branches: [main]`` push trigger failed this contract.
    """
    triggers = release[True] if True in release else release["on"]
    assert set(triggers) == {"push"}, f"unexpected triggers: {sorted(triggers)}"
    push = triggers["push"]
    assert set(push) == {"tags"}, f"push must be tag-only, found {sorted(push)}"
    assert push["tags"] == ["v*"]


def test_every_job_has_a_timeout(release: dict[str, Any], ci: dict[str, Any]) -> None:
    """A hung runner is cut off rather than burning its full six hours.

    Mutation: deleting ``timeout-minutes`` from the release build job failed
    this contract.
    """
    for name, workflow in (("release.yml", release), ("ci.yml", ci)):
        for job, spec in jobs_of(workflow).items():
            assert isinstance(spec.get("timeout-minutes"), int), (
                f"{name}/{job}: missing timeout-minutes"
            )


# --- the matrix -------------------------------------------------------------


def test_the_matrix_carries_exactly_the_two_missing_targets(config: Config) -> None:
    """The build matrix is the two targets upstream does not publish.

    Mutation: adding an ``aarch64-apple-darwin`` target to dylint.toml failed
    this contract.
    """
    legs = config.matrix()["include"]
    assert [leg["target"] for leg in legs] == [
        "x86_64-apple-darwin",
        "x86_64-pc-windows-msvc",
    ]


def test_each_matrix_leg_names_a_runner_of_its_own_architecture(
    config: Config,
) -> None:
    """An x86_64 target is built on an x64 runner, so its binary can be run.

    Mutation: pointing the darwin leg at ``macos-latest``, which is arm64,
    failed this contract.
    """
    x64_runners = {"macos-15-intel", "macos-26-intel", "windows-latest", "ubuntu-24.04"}
    for leg in config.matrix()["include"]:
        assert leg["target"].startswith("x86_64-")
        assert leg["runner"] in x64_runners, leg


def test_the_release_matrix_comes_from_the_configuration(
    release: dict[str, Any],
) -> None:
    """The workflow reads its matrix from dylint.toml rather than repeating it.

    Mutation: replacing the ``fromJSON`` expression with a literal matrix
    failed this contract.
    """
    build = jobs_of(release)["build"]
    assert (
        build["strategy"]["matrix"] == "${{ fromJSON(needs.prepare.outputs.matrix) }}"
    )
    assert build["runs-on"] == "${{ matrix.runner }}"
    assert steps_running(release, "prepare", "python scripts/matrix.py matrix")


# --- archive names and sidecars ---------------------------------------------


def test_the_published_names_match_upstreams_pattern(config: Config) -> None:
    """Every tar.gz name parses under the pattern upstream's own assets match.

    Mutation: renaming the archive stem to ``<binary>-v<version>-<target>``
    failed this contract.
    """
    for name in UPSTREAM_ASSET_NAMES:
        if name.endswith(".tar.gz"):
            assert UPSTREAM_NAME_RE.match(name), f"fixture drifted: {name}"
    for name in config.released_archive_names():
        if not name.endswith(".tar.gz"):
            continue
        match = UPSTREAM_NAME_RE.match(name)
        assert match, f"not upstream-shaped: {name}"
        assert match["version"] == config.version
        assert match["binary"] in config.binaries
        assert match["target"] in config.target_triples


def test_the_release_publishes_every_configured_archive(config: Config) -> None:
    """Both binaries are published for every target, in every format it declares.

    The version is taken from the configuration rather than written out, so a
    routine upstream bump does not have to be repeated here. Mutation:
    dropping ``dylint-link`` from dylint.toml's binaries failed this contract.
    """
    assert set(config.binaries) == {"cargo-dylint", "dylint-link"}, (
        "both binaries are part of the consumer contract; dropping one "
        "silently removes an asset consumers resolve by name"
    )
    assert set(config.released_archive_names()) == {
        f"{binary}-{target}-v{config.version}.{fmt}"
        for binary in ("cargo-dylint", "dylint-link")
        for target in config.target_triples
        for fmt in config.target(target).formats
    }


def test_the_windows_leg_also_publishes_a_zip(config: Config) -> None:
    """Consumers choose an extractor by extension, so Windows gets both forms.

    Mutation: removing ``"zip"`` from the Windows target's formats failed this
    contract.
    """
    assert set(config.target("x86_64-pc-windows-msvc").formats) == {"tar.gz", "zip"}


def test_the_build_leg_packs_and_verifies_by_command(release: dict[str, Any]) -> None:
    """Packaging and verification are asserted as commands, not as step names.

    The sidecar is written by ``package.py pack``; deleting the ``pack`` step
    leaves nothing to upload. Mutation: replacing the verify command with
    ``true`` failed this contract.
    """
    packs = steps_running(release, "build", "scripts/package.py pack")
    verifies = steps_running(release, "build", "scripts/package.py verify")
    assert len(packs) == 1, packs
    assert len(verifies) == 1, verifies
    assert "--target" in packs[0] and "--out-dir dist" in packs[0]
    assert "--dist dist" in verifies[0]
    assert "--no-smoke" not in verifies[0], (
        "the build leg must run the binary it just packaged"
    )


def test_the_audit_rechecks_every_sidecar_after_upload(
    release: dict[str, Any],
) -> None:
    """The published assets are downloaded again and re-verified as a set.

    Mutation: dropping ``--dist audit-dist`` from the audit command failed
    this contract.
    """
    audits = steps_running(release, "audit", "scripts/package.py verify")
    assert len(audits) == 1, audits
    assert "--dist audit-dist" in audits[0]
    downloads = steps_running(release, "audit", "gh release download")
    assert downloads and "for attempt in 1 2 3" in downloads[0], (
        "asset downloads must be retried"
    )


def test_the_upstream_job_downloads_and_verifies_upstreams_sidecars(
    release: dict[str, Any], config: Config
) -> None:
    """A Linux job proves our sidecar format is the one upstream publishes.

    Mutation: deleting the verify_upstream step from the release workflow
    failed this contract.
    """
    job = jobs_of(release)["verify-upstream"]
    assert job["runs-on"] == "${{ needs.prepare.outputs.upstream_runner }}"
    commands = steps_running(release, "verify-upstream", "scripts/verify_upstream.py")
    assert len(commands) == 1, commands
    # The upstream list is authoritative twice over: it decides what is
    # checked, and the configuration refuses to build anything on it.
    assert set(config.upstream.targets) == {
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
    }, (
        "the upstream list decides what the parity job checks and what this "
        "repository is forbidden to build; both Linux targets belong on it"
    )
    assert not any(
        pattern.search(command)
        for _, command in run_commands(release)
        if "verify_upstream" in command
        for pattern in TOOL_INSTALL_TOKENS
    )


# --- what the build actually builds -----------------------------------------


def test_dylint_is_checked_out_at_the_pinned_commit(
    release: dict[str, Any], config: Config
) -> None:
    """The build uses the pinned commit, so a moved upstream tag cannot change it.

    Mutation: changing the checkout ``ref`` to ``needs.prepare.outputs.tag``
    failed this contract.
    """
    checkouts = [
        step
        for step in steps_of(release, "build")
        if step.get("with", {}).get("repository") == "trailofbits/dylint"
    ]
    assert len(checkouts) == 1, checkouts
    assert checkouts[0]["with"]["ref"] == "${{ needs.prepare.outputs.commit }}"
    assert re.fullmatch(r"[0-9a-f]{40}", config.commit), (
        f"dylint.commit must be a 40-hex commit, found {config.commit!r}; "
        "a tag or branch here would let upstream change what is built"
    )


def test_the_build_command_matches_upstreams(release: dict[str, Any]) -> None:
    """Both binaries are built in one workspace build with upstream's feature.

    Upstream's release job runs the same command; anything else would ship a
    cargo-dylint that builds its driver locally. Mutation: dropping
    ``--features="$FEATURES"`` failed this contract.
    """
    builds = steps_running(release, "build", "cargo build")
    assert len(builds) == 1, builds
    command = builds[0]
    for fragment in (
        "--locked",
        "--release",
        '--target "$TARGET"',
        "-p cargo-dylint",
        "-p dylint-link",
        '--features="$FEATURES"',
    ):
        assert fragment in command, f"missing {fragment!r} from the build command"


def test_only_dylint_is_built_on_the_runner(release: dict[str, Any]) -> None:
    """No step installs or compiles tooling; the runner's toolchain is used as is.

    Mutation: adding ``cargo install cargo-dylint`` to the build job failed
    this contract.
    """
    for job, command in run_commands(release):
        for pattern in TOOL_INSTALL_TOKENS:
            assert not pattern.search(command), f"release.yml/{job}: {pattern.pattern}"


def test_the_tag_is_checked_before_anything_is_built(release: dict[str, Any]) -> None:
    """A tag that does not name the configured version stops the first job.

    Mutation: replacing the check-tag command with a bare echo failed this
    contract.
    """
    checks = steps_running(release, "prepare", "scripts/matrix.py check-tag")
    assert len(checks) == 1, checks
    assert '"$REF_NAME"' in checks[0]
    assert jobs_of(release)["build"]["needs"] == ["prepare", "create-release"]


def test_publishing_waits_for_the_audit_and_the_upstream_check(
    release: dict[str, Any],
) -> None:
    """A draft becomes a release only after both proofs have passed.

    Mutation: removing ``verify-upstream`` from the publish job's needs failed
    this contract.
    """
    publish = jobs_of(release)["publish"]
    assert set(publish["needs"]) >= {"audit", "verify-upstream"}
    commands = steps_running(release, "publish", "gh release edit")
    assert len(commands) == 1 and "--draft=false" in commands[0]


def test_a_published_release_is_never_rebuilt(release: dict[str, Any]) -> None:
    """Re-running a published tag fails rather than replacing an asset.

    Mutation: deleting the ``exit 1`` from the immutability guard failed this
    contract.
    """
    guards = steps_running(release, "create-release", "isDraft")
    assert len(guards) == 1, guards
    assert 'if [ "$is_draft" != "true" ]; then' in guards[0]
    assert "exit 1" in guards[0]


def test_write_permission_is_confined_to_the_jobs_that_publish(
    release: dict[str, Any],
) -> None:
    """Only the jobs that touch the release carry contents: write.

    Mutation: granting ``contents: write`` to the audit job failed this
    contract.
    """
    assert release["permissions"] == {"contents": "read"}
    writers = {
        job
        for job, spec in jobs_of(release).items()
        if spec.get("permissions", {}).get("contents") == "write"
    }
    assert writers == {"create-release", "build", "publish"}


# --- the CI workflow --------------------------------------------------------


def test_ci_runs_the_repository_gates(ci: dict[str, Any]) -> None:
    """CI runs the same make targets a developer runs before committing.

    Mutation: replacing ``make test`` with ``pytest -q`` failed this contract.
    """
    commands = {command.strip() for _, command in run_commands(ci)}
    assert {"make check-fmt", "make ruff", "make test"} <= commands


def test_ci_checks_upstream_parity_against_the_live_release(ci: dict[str, Any]) -> None:
    """A change to the sidecar rules is proved against upstream's real assets.

    Mutation: deleting the upstream-parity job failed this contract.
    """
    commands = steps_running(ci, "upstream-parity", "scripts/verify_upstream.py")
    assert len(commands) == 1, commands
