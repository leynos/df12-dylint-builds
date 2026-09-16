"""The containment rule for an asset name, generated against.

An asset's name comes from the release payload rather than from this
repository, so it is the one input the audit cannot inspect before
acting on it. The repository already generates the analogous
archive-extraction invariant, and this follows it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from audit_draft import asset_target
from hypothesis import given, settings
from hypothesis import strategies as st
from package import PackagingError

#: Roots that leave a directory outright, whatever follows them. An
#: absolute name discards the directory it was joined to, so these need
#: no climbing to escape.
ABSOLUTE_ROOTS = ("/etc", "/", "/tmp/elsewhere")

#: Characters a release asset's name is allowed to be built from, for the
#: names that are meant to be accepted.
SAFE_ALPHABET = st.characters(whitelist_categories=("Ll", "Lu", "Nd"))


@settings(max_examples=100, deadline=None)
@given(
    leaf=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
    suffix=st.sampled_from(["", ".tar.gz", ".zip", ".sha256"]),
)
def test_a_name_that_stays_put_is_written_inside_the_destination(
    tmp_path_factory: pytest.TempPathFactory, leaf: str, suffix: str
) -> None:
    """A plain asset name resolves to a path under the download directory.

    The containment rule is only worth having if it also lets the
    ordinary case through. A check that refused every name would satisfy
    the escape property on its own, so the two are asserted together:
    this is the half that says the rule is narrow.
    """
    destination = tmp_path_factory.mktemp("assets")
    name = f"{leaf}{suffix}"

    _, target = asset_target({"name": name, "url": "https://example/x"}, destination)

    assert target.is_relative_to(destination.resolve()), target
    assert target.name == name, target


@settings(max_examples=150, deadline=None)
@given(
    data=st.data(),
    leaf=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
    depth=st.integers(min_value=0, max_value=3),
)
def test_a_name_that_climbs_out_of_the_destination_is_refused(
    tmp_path_factory: pytest.TempPathFactory,
    data: st.DataObject,
    leaf: str,
    depth: int,
) -> None:
    """Traversal that clears the download directory is rejected.

    Nobody writes these names down: they arrive in a release payload
    from GitHub. The interesting shape is traversal buried under
    real-looking directories, so the climb is drawn to exceed the depth
    it has to undo rather than fixed, and a name that merely returns to
    where it started is never generated: that one is contained, and
    refusing it would be a different rule.
    """
    destination = tmp_path_factory.mktemp("assets")
    climb = data.draw(st.integers(min_value=depth + 1, max_value=depth + 3))
    parts = [f"d{index}" for index in range(depth)] + [".."] * climb + [leaf]
    # `PurePosixPath` rather than a join: an asset name is a path, and
    # this is the spelling the repository asks for. It keeps every
    # traversal case intact, because a pure path does not resolve `..`.
    name = PurePosixPath(*parts).as_posix()

    with pytest.raises(PackagingError, match="would write outside"):
        asset_target({"name": name, "url": "https://example/x"}, destination)


@settings(max_examples=50, deadline=None)
@given(
    root=st.sampled_from(ABSOLUTE_ROOTS),
    leaf=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
)
def test_an_absolute_name_is_refused(
    tmp_path_factory: pytest.TempPathFactory, root: str, leaf: str
) -> None:
    """An absolute asset name is rejected rather than followed.

    Joining an absolute path discards the directory on its left, so this
    escapes without climbing at all, and the arithmetic the traversal
    case relies on never runs.
    """
    destination = tmp_path_factory.mktemp("assets")
    name = f"{root}/{leaf}"

    with pytest.raises(PackagingError, match="would write outside"):
        asset_target({"name": name, "url": "https://example/x"}, destination)
