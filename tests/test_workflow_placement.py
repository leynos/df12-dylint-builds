"""Where the workflows run, and which runner each event selects.

Split from :mod:`test_workflow_contracts`, which asserts what the release
does: the exact commands, the pinned references, the permissions. These
ask a different question, about placement, and the two together exceeded
the 600-line ceiling CodeScene applies to a single file.

The declaration is read as a condition and two arms rather than as a set
of labels, and then evaluated. Which label sits in which arm is not the
same question as which arm an event takes, and the event that takes the
false arm without setting the field at all is the one no positional
reading establishes.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import pytest
import yaml
from dylint_config import Config, default_config_path, load_config
from test_workflow_contracts import jobs_of

if typ.TYPE_CHECKING:
    from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
WORKFLOWS: Path = REPO_ROOT / ".github" / "workflows"

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
#: than only those it recognizes: a job added with an unrecognized label
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
    declaration : str
        A job's ``runs-on`` value.
    fork : bool or None
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

    Parameters
    ----------
    config : Config
        The parsed ``dylint.toml``.

    Returns
    -------
    frozenset[str]
        Every GitHub-hosted label this repository permits.

    Examples
    --------
    >>> "ubuntu-latest" in permitted_hosted_labels(  # doctest: +SKIP
    ...     load_config(default_config_path())
    ... )
    True
    """
    from_config = {target.runner for target in config.targets}
    from_config.add(config.upstream.runner)
    return frozenset(from_config | {CI_FORK_LABEL})


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    """Parse the release workflow once for the module."""
    return yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    """Parse the CI workflow once for the module."""
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config() -> Config:
    """Read the repository's configuration once for the module."""
    return load_config(default_config_path())


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
    a literal rather than by collecting the ones already recognized. An
    earlier form gathered the jobs whose label was in the permitted set
    and compared that with the list, so a job added on an unrecognized
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
