"""Where a release's assets may be written, and whether they may be.

Split from :mod:`audit_draft`, which reads the release and fetches what
it lists. This module answers one question: given an asset object and a
download directory, what is the URL and what is the path, and is that
path somewhere this command is willing to write.

The asset name comes from the release rather than from this repository,
so nothing here trusts it. The containment rule is the reason the module
is worth naming: a name that would escape the download directory is the
one input that turns an audit into a write anywhere on the runner.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from package import PackagingError

if typ.TYPE_CHECKING:
    from audit_draft import AssetPayload

__all__ = ["asset_target", "make_destination"]


def _required_text(asset: AssetPayload, field: str) -> str:
    """Return one string field of an asset, insisting it is present.

    Parameters
    ----------
    asset:
        The asset object.
    field:
        The field to read.

    Returns
    -------
    str
        The value.

    Raises
    ------
    PackagingError
        If the field is absent, empty, or not a string.
    """
    value = asset.get(field)
    if not isinstance(value, str) or not value:
        message = f"an asset of this release has no {field}: {asset!r}"
        raise PackagingError(message)
    return value


def _resolved(path: Path, subject: str) -> Path:
    """Resolve ``path``, reporting a filesystem failure as this command's.

    `Path.resolve` reads the filesystem, and the asset name reaching it
    is the release's rather than this repository's. Everything below
    `download_assets` is caught as `PackagingError` and reported as an
    outcome; anything else goes past that as a traceback with no
    `::error::` annotation and no metric, and the stage this would be
    reported under, `rejected-asset`, is the right one.

    Both exception types are caught, and which one matters is worth
    recording. Measured on Linux and CPython 3.13, the non-strict form
    used here does **not** raise `OSError` for the cases it looks like
    it should: it returns a path for a component under a directory it
    may not search, and for a symlink loop, and absence is normal here
    because the target does not exist yet. What it does raise is
    `ValueError`, for a name carrying a NUL byte, which is exactly the
    kind of thing an untrusted release name can hold. `OSError` is kept
    for the platforms and versions where the walk is strict enough to
    produce one.

    Parameters
    ----------
    path : Path
        The path to resolve.
    subject : str
        What the path is, for the failure message.

    Returns
    -------
    Path
        The resolved path.

    Raises
    ------
    PackagingError
        If the path cannot be resolved.
    """
    try:
        return path.resolve()
    except (OSError, ValueError) as error:
        message = (
            f"{subject} could not be resolved, so whether it stays inside "
            f"the download directory cannot be decided: "
            f"{type(error).__name__}: {error}"
        )
        raise PackagingError(message) from error


def asset_target(asset: AssetPayload, destination: Path) -> tuple[str, Path]:
    """Return one asset's download URL and where it may be written.

    The name comes from the release rather than from this repository, so
    it is not trusted to stay inside the directory. Public because this
    containment rule is the one decision here that a caller cannot
    inspect its input for beforehand, and it is worth being able to
    generate names against it rather than list them.

    Parameters
    ----------
    asset:
        The asset object.
    destination:
        The directory being filled.

    Returns
    -------
    tuple[str, Path]
        The URL to read, and the path to write.

    Raises
    ------
    PackagingError
        If the asset lacks a name or a URL, the name would escape, or
        the filesystem cannot be read to decide.
    """
    name = _required_text(asset, "name")
    url = _required_text(asset, "url")
    target = _resolved(destination / name, f"the download path for {name}")
    root = _resolved(destination, f"the download directory {destination}")
    if not target.is_relative_to(root):
        message = f"asset name {name!r} would write outside {destination}"
        raise PackagingError(message)
    return url, target


def make_destination(destination: Path) -> None:
    """Create the download directory, as this command's own failure.

    Inside the error boundary rather than before it, and translated
    rather than allowed to escape. `main` catches `PackagingError` and
    nothing else, so an `OSError` here left the command with a traceback
    instead of the `::error::` annotation a workflow log needs, and the
    `finally` below never ran, so the run reported no outcome at all.

    Parameters
    ----------
    destination:
        The directory to write into.

    Raises
    ------
    PackagingError
        If the directory cannot be created.
    """
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        message = (
            f"the download directory {destination} could not be created: "
            f"{type(error).__name__}: {error}"
        )
        raise PackagingError(message) from error
