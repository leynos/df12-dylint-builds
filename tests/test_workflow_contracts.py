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

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
WORKFLOWS: Path = REPO_ROOT / ".github" / "workflows"
SHA_PIN: re.Pattern[str] = re.compile(r"^[^@]+@[0-9a-f]{40}$")
WORKFLOW_NAMES: tuple[str, ...] = ("release.yml", "ci.yml")

#: The event field that distinguishes a fork's pull request. Named once
#: so the placement contract asserts this field rather than matching the
#: expression loosely.
FORK_FIELD: str = "github.event.pull_request.head.repo.fork"

#: The paid runner the CI lanes use when they can. Written here once, and
#: asserted as this exact label: "not a GitHub-hosted label" would be
#: satisfied by a misspelling, and a job pointed at a runner that does
#: not exist waits for one indefinitely, which is the same outcome as the
#: missing fork fallback this change exists to fix.
CI_UBICLOUD_LABEL: str = "ubicloud-standard-2"

#: The GitHub-hosted label a fork's pull request falls back to.
CI_FORK_LABEL: str = "ubuntu-latest"

#: The short-circuit idiom GitHub Actions expressions use in place of a
#: ternary, read as a condition and its two arms. The arms are read by
#: position: an expression that sends a fork to the paid runner names
#: exactly the same two labels as one that does not, so a contract
#: comparing label sets accepts both.
RUNNER_TERNARY: re.Pattern[str] = re.compile(
    r"^\s*\$\{\{\s*(?P<condition>.+)\s*&&\s*'(?P<when_true>[^']*)'"
    r"\s*\|\|\s*'(?P<when_false>[^']*)'\s*\}\}\s*$",
    re.DOTALL,
)

#: The release jobs that name a GitHub-hosted label literally. Four of
#: them are movable tag lanes and are deliberately not moved here; the
#: two remaining jobs take their labels from an expression, one from the
#: matrix and one from a `prepare` output, and both trace back to
#: dylint.toml.
HOSTED_RELEASE_JOBS: frozenset[str] = frozenset(
    {"prepare", "create-release", "audit", "publish"}
)

#: The release jobs whose runner comes from an expression rather than a
#: literal. Named so the release contract accounts for every job rather
#: than only those it recognises: a job added with an unrecognised label
#: would otherwise fall into neither set and be asserted about by
#: nothing.
DERIVED_RELEASE_JOBS: frozenset[str] = frozenset({"build", "verify-upstream"})


def select_runner(declaration: str, *, fork: bool | None) -> str:
    """Return the label GitHub selects, given the fork field's value.

    Evaluates the declaration rather than inspecting it. Reading the
    arms by position tells you which label sits where; it does not tell
    you which one an event actually gets, and the three events this
    repository sees are a fork's pull request, its own pull request and
    a dispatch. The last two do not set the field at all.

    A missing property is falsy in a GitHub Actions expression, so an
    absent field takes the same arm as an explicit false. That is why
    ``None`` is a value here rather than an error.

    Only the shape this repository uses is modelled: a condition that is
    exactly the fork field. Anything else raises, because a contract
    that needs to know which arm an event takes must be able to tell
    that it cannot, rather than receive a label chosen by position in a
    shape this function does not understand.

    Parameters
    ----------
    declaration:
        A job's ``runs-on`` value.
    fork:
        The value of ``github.event.pull_request.head.repo.fork``, or
        None when the event does not set it.

    Returns
    -------
    str
        The label the job lands on.

    Raises
    ------
    AssertionError
        If the declaration is not the fork-keyed short-circuit form.
    """
    match = RUNNER_TERNARY.match(declaration)
    assert match is not None, f"not a two-armed expression: {declaration!r}"
    condition = match.group("condition").strip()
    assert condition == FORK_FIELD, (
        f"this evaluator models only {FORK_FIELD}, not {condition!r}"
    )
    return match.group("when_true") if fork else match.group("when_false")


def permitted_hosted_labels(config: Config) -> frozenset[str]:
    """Return the GitHub-hosted labels this repository permits.

    Read from `dylint.toml` rather than written down, because that file
    is where a runner label is decided: the two cross-compilation
    targets name theirs, and the upstream verification lane names one
    more. The fork fallback's label is the only one this repository uses
    that the configuration has no opinion about, and it is added by name.

    Listing GitHub's catalogue instead would make the release contract
    weaker rather than stronger. That contract asserts which jobs sit on
    a hosted label, so every extra label in the set is a label a new job
    could take without the assertion noticing.
    """
    from_config = {target.runner for target in config.targets}
    from_config.add(config.upstream.runner)
    return frozenset(from_config | {CI_FORK_LABEL})


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

    The two steps have to name the same directory, or the audit verifies
    a directory nobody filled and passes over nothing.

    Mutation: dropping ``--dist audit-dist`` from the audit command, and
    separately pointing the download at another directory, each failed
    this contract.
    """
    audits = steps_running(release, "audit", "scripts/package.py verify")
    assert len(audits) == 1, (
        f"the audit verifies in exactly one step; found {len(audits)}"
    )
    assert "--dist audit-dist" in audits[0], (
        f"the audit must verify the directory it downloaded into: {audits[0]}"
    )
    downloads = steps_running(release, "audit", "scripts/audit_draft.py")
    assert len(downloads) == 1, (
        f"the audit reads the draft in exactly one step; found {len(downloads)}"
    )
    assert "--dir audit-dist" in downloads[0], (
        f"the download must fill the directory the verification reads: {downloads[0]}"
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


def test_write_permission_is_confined_to_the_jobs_that_touch_the_release(
    release: dict[str, Any],
) -> None:
    """Only the jobs that reach the release carry contents: write.

    The default is read, and the two jobs that neither create, upload to,
    read nor publish the release keep it: `prepare` and `verify-upstream`.

    Mutation: granting ``contents: write`` to the ``prepare`` job failed
    this contract.
    """
    assert release["permissions"] == {"contents": "read"}, (
        "the workflow default must be contents: read, so that a job holding "
        "write says so itself rather than inheriting it: "
        f"{release['permissions']}"
    )
    writers = {
        job
        for job, spec in jobs_of(release).items()
        if spec.get("permissions", {}).get("contents") == "write"
    }
    expected_writers = {"create-release", "build", "audit", "publish"}
    assert writers == expected_writers, (
        "contents: write belongs to exactly the jobs that reach the release; "
        f"{sorted(writers - expected_writers)} gained it and "
        f"{sorted(expected_writers - writers)} lost it"
    )


def test_the_audit_can_see_the_draft_it_audits(release: dict[str, Any]) -> None:
    """The audit job carries the permission a draft release requires to read.

    A draft is visible only to a token with push access. With
    ``contents: read`` the API answers "release not found" for a draft that
    exists, so the audit fails having checked nothing; run 34208473988 lost
    a release to exactly this. The job still only reads.

    Mutation: removing the audit job's ``permissions`` block, so that it
    inherits the workflow's ``contents: read``, failed this contract.
    """
    audit = jobs_of(release)["audit"]
    assert audit.get("permissions", {}).get("contents") == "write", (
        "the audit job cannot download a draft release without push access"
    )


def test_the_audits_download_is_one_command_rather_than_a_shell_loop(
    release: dict[str, Any],
) -> None:
    """The step that reads the draft invokes a script and nothing else.

    It used to be a `for` loop with a conditional and a `sleep`, written
    inline in the `run` block, and that shape is why none of the audit's
    decisions had a test: the retry, the treatment of a permanent status
    and the refusal to write a truncated body were all buried in shell
    that only a release could execute. They live in
    `scripts/audit_draft.py` now, with `tests/test_audit_draft.py`
    driving each of them against a local server.

    This contract is what stops the loop coming back. It matches shell
    control flow rather than the absence of a script name, because a
    step can invoke the script and still grow a loop around it.

    Mutation: restoring the `for attempt in 1 2 3` loop failed this
    contract.
    """
    downloads = [
        step
        for step in steps_of(release, "audit")
        if "scripts/audit_draft.py" in step.get("run", "")
    ]
    assert len(downloads) == 1, (
        f"the audit reads the draft in exactly one step; found {len(downloads)}"
    )
    body = downloads[0]["run"]
    control_flow = [
        token
        for token in ("for ", "while ", "if ", "&&", "||", ";", "sleep ")
        if token in body
    ]
    assert not control_flow, (
        "the step that reads the draft must be one command, so that its "
        "decisions live where a test can drive them; found shell control "
        f"flow {control_flow} in: {body}"
    )


def test_the_audits_download_is_given_the_token_the_grant_provides(
    release: dict[str, Any],
) -> None:
    """The step that reads the draft receives the job's token and repository.

    The permission grant is necessary and not sufficient. A job holding
    ``contents: write`` whose download step is handed no ``GH_TOKEN``
    fails exactly as the unprivileged run did, with the draft reported as
    not found. Asserting the grant alone would pass with the token
    deleted, which is the state that reproduces run 34208473988.

    ``scripts/audit_draft.py`` reads neither variable from the
    environment: it takes ``--token`` and ``--repo``. So the environment
    half of this contract is satisfied by a step that never passes either
    on, which fails at run time and passes here. Both halves are
    asserted: the variables exist, and the command spends them.

    ``GH_REPO`` travels with the token because the checkout persists no
    credentials, so nothing else names the repository.

    Mutation: deleting ``GH_TOKEN`` from the step's ``env``, deleting
    ``GH_REPO``, and separately dropping ``--token "$GH_TOKEN"`` and
    ``--repo "$GH_REPO"`` from the command, each failed this contract.
    """
    downloads = [
        step
        for step in steps_of(release, "audit")
        if "scripts/audit_draft.py" in step.get("run", "")
    ]
    assert len(downloads) == 1, (
        f"the audit reads the draft in exactly one step; found {len(downloads)}"
    )
    env = downloads[0].get("env", {})
    assert env.get("GH_TOKEN") == "${{ secrets.GITHUB_TOKEN }}", (
        "the download step must be handed the job's token, or the write "
        f"permission never reaches the audit: {env.get('GH_TOKEN')!r}"
    )
    assert env.get("GH_REPO") == "${{ github.repository }}", (
        "the download step must name the repository, since the checkout "
        f"persists no credentials to infer it from: {env.get('GH_REPO')!r}"
    )
    body = downloads[0]["run"]
    assert '--token "$GH_TOKEN"' in body, (
        "the audit script reads no environment variable; the token must "
        f"reach it through --token, or the grant stops at the step: {body}"
    )
    assert '--repo "$GH_REPO"' in body, (
        "the audit script reads no environment variable; the repository "
        f"must reach it through --repo: {body}"
    )


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


def test_no_runner_selection_hides_a_line_break(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    """Every ``runs-on`` parses to a single line.

    A folded scalar keeps the break of a more-indented continuation, so
    the parsed value carries a newline inside the expression. GitHub
    evaluates it regardless and the job lands on the right runner, which
    is exactly why a green run proves nothing and this reads the parsed
    document instead.

    Read across both workflows rather than the CI lanes alone: the
    release matrix takes its label from an expression too, and the fault
    is a property of the declaration, not of which lane declares it.

    Mutation: indenting the continuation of ``checks``'s ``runs-on`` one
    level deeper failed this contract.
    """
    for name, workflow in (("release.yml", release), ("ci.yml", ci)):
        for job, spec in jobs_of(workflow).items():
            declaration = spec.get("runs-on", "")
            assert "\n" not in str(declaration), (
                f"{name}:{job} declares a runs-on carrying a line break, so "
                f"the expression is split across lines: {declaration!r}"
            )


def test_every_ci_lane_falls_back_to_a_hosted_runner_for_a_fork(
    ci: dict[str, Any], config: Config
) -> None:
    """A fork's pull request lands on a GitHub-hosted runner.

    A fork cannot obtain an Ubicloud runner, so a bare Ubicloud label
    leaves the job unschedulable and the pull request waiting on a check
    that will never start.

    Stated over every job in the workflow rather than the two named
    today, so a lane added later cannot be the one that omits it. The
    fork field is asserted by name because the failure this guards
    against is a plausible sibling field in an otherwise identical
    expression: ``head.repo.private`` reads almost the same and would
    send every private-repository pull request to a hosted runner.

    Each arm is asserted for what it is rather than for what it is not.
    "Some hosted label and some other label" is satisfied by the two
    declarations this contract must tell apart: the one with its arms
    swapped, which sends a fork to the paid runner, and the one whose
    paid label is misspelled, which sends everything else to a runner
    that does not exist. Both wait for a runner indefinitely, which is
    the failure this change exists to remove.

    Mutations: ``head.repo.fork`` for ``head.repo.private``, a bare
    ``ubicloud-standard-2``, the two arms swapped, and
    ``ubicloud-standard-two`` for the paid label each failed this.
    """
    hosted = permitted_hosted_labels(config)
    for job, spec in jobs_of(ci).items():
        declaration = str(spec.get("runs-on", ""))
        match = RUNNER_TERNARY.match(declaration)

        assert match is not None, (
            f"ci.yml:{job} does not select its runner by a condition and two "
            f"arms, so a fork's pull request cannot be sent elsewhere: "
            f"{declaration!r}"
        )
        assert FORK_FIELD in match.group("condition"), (
            f"ci.yml:{job} must key its runner on {FORK_FIELD} so a fork's "
            f"pull request can run it: {match.group('condition')!r}"
        )
        assert match.group("when_true") in hosted, (
            f"ci.yml:{job} sends a fork's pull request to "
            f"{match.group('when_true')!r}, which is not a GitHub-hosted "
            f"label this repository uses, so the job is never scheduled"
        )
        assert match.group("when_false") == CI_UBICLOUD_LABEL, (
            f"ci.yml:{job} sends its own pull requests to "
            f"{match.group('when_false')!r} rather than {CI_UBICLOUD_LABEL!r}"
        )


@pytest.mark.parametrize(
    ("fork", "event", "expected"),
    [
        pytest.param(True, "a fork's pull request", CI_FORK_LABEL, id="fork"),
        pytest.param(
            False, "this repository's pull request", CI_UBICLOUD_LABEL, id="same-repo"
        ),
        pytest.param(
            None, "a push or a dispatch", CI_UBICLOUD_LABEL, id="field-absent"
        ),
    ],
)
def test_every_ci_lane_lands_where_the_event_requires(
    ci: dict[str, Any], fork: bool | None, event: str, expected: str
) -> None:
    """Evaluating the declaration gives the right runner for each event.

    The contract above reads the arms and asserts each for what it is,
    which catches a swap and a misspelling. It still does not say what
    an event gets, and the third case is why that matters: a push and a
    `workflow_dispatch` set no `pull_request` context at all, so the
    field is absent rather than false. A missing property is falsy in a
    GitHub Actions expression, so those take the Ubicloud arm, and
    nothing in a positional reading of the arms establishes that.

    Stated over every job, and over the three events this repository
    sees rather than the two the change was written for.

    Mutations: swapping the arms fails the fork case; negating the
    condition would fail all three, and is refused earlier by the
    evaluator, which models only the fork field.
    """
    for job, spec in jobs_of(ci).items():
        landed = select_runner(str(spec.get("runs-on", "")), fork=fork)

        assert landed == expected, (
            f"ci.yml:{job} sends {event} to {landed!r} rather than {expected!r}"
        )


def test_the_release_lanes_stay_where_they_are(
    release: dict[str, Any], config: Config
) -> None:
    """The release workflow is untouched by this change, and says so.

    Tag lanes are movable under the placement rule and four of these six
    jobs could take an Ubicloud runner. They are deliberately left alone
    here so that this change is one bounded thing, and this contract is
    what stops the two halves being confused: a later placement change to
    the release lane has to edit this list, which is where the decision
    gets recorded.

    The two remaining jobs take their label from an expression, one from
    the matrix and one from a ``prepare`` output, and both trace back to
    ``dylint.toml``. They must never move: they exist to build the Apple
    and Windows targets upstream omits.

    Every job is accounted for, by partitioning on whether the label is
    a literal rather than by collecting the ones already recognised. An
    earlier form gathered the jobs whose label was in the permitted set
    and compared that with the list, so a job added on an unrecognised
    label fell into neither side and the equality passed with the new
    job asserted about by nothing.

    Mutations: putting an Ubicloud label on ``prepare``, and adding a
    job on a label this repository does not use, each failed this.
    """
    permitted = permitted_hosted_labels(config)
    literal: dict[str, str] = {}
    derived: set[str] = set()
    for job, spec in jobs_of(release).items():
        declaration = str(spec.get("runs-on", ""))
        if "${{" in declaration:
            derived.add(job)
        else:
            literal[job] = declaration

    assert set(literal) == HOSTED_RELEASE_JOBS, (
        f"release.yml's jobs on a literal label changed: {sorted(literal)} "
        f"against {sorted(HOSTED_RELEASE_JOBS)}. Moving one is a placement "
        "decision and belongs in a change of its own."
    )
    assert derived == DERIVED_RELEASE_JOBS, (
        f"release.yml's jobs taking their runner from an expression changed: "
        f"{sorted(derived)} against {sorted(DERIVED_RELEASE_JOBS)}"
    )
    unpermitted = {
        job: label for job, label in literal.items() if label not in permitted
    }
    assert not unpermitted, (
        f"release.yml names runner labels this repository does not permit: "
        f"{unpermitted}. The permitted set comes from dylint.toml; a new one "
        "is a placement decision and belongs in a change of its own."
    )
