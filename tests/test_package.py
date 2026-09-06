"""Tests for packaging, the sidecar format and archive verification."""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

import pytest
from conftest import (
    POSIX_TARGET,
    WINDOWS_TARGET,
    requires_posix_exec,
    write_stubs,
)
from dylint_config import Config, exe_suffix
from hypothesis import given, settings
from hypothesis import strategies as st
from package import (
    UNIX_CREATE_SYSTEM,
    PackagingError,
    check_layout,
    check_sidecar,
    expected_assets,
    extract,
    inspect_archive,
    pack,
    parse_archive_name,
    read_sidecar,
    smoke_command,
    verify,
    verify_dist,
    write_sidecar,
)

# One of upstream's own sidecars, byte for byte, from
# cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz.sha256.
UPSTREAM_SIDECAR = (
    "14195423ac6bfe6b055ffa94e0c48e282e1f5997abd98dfb4ea8bdf4633aec5c  "
    "cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz\n"
)


def test_the_sidecar_matches_upstreams_format(tmp_path: Path) -> None:
    """A sidecar is the digest, two spaces, the base name and a newline."""
    archive = tmp_path / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    archive.write_bytes(b"payload")
    sidecar = write_sidecar(archive)
    digest = hashlib.sha256(b"payload").hexdigest()
    assert sidecar.read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"


def test_upstreams_own_sidecar_parses(tmp_path: Path) -> None:
    """The parser accepts an upstream sidecar unchanged, proving format parity."""
    sidecar = tmp_path / "upstream.sha256"
    sidecar.write_text(UPSTREAM_SIDECAR, encoding="utf-8")
    digest, name = read_sidecar(sidecar)
    assert digest == UPSTREAM_SIDECAR.split("  ")[0]
    assert name == "cargo-dylint-x86_64-unknown-linux-gnu-v6.0.4.tar.gz"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("deadbeef  a.tar.gz\n", "not a sha256sum line", id="short-digest"),
        pytest.param(f"{'a' * 64} a.tar.gz\n", "not a sha256sum line", id="one-space"),
        pytest.param(
            f"{'a' * 64}  a.tar.gz", "must end with a newline", id="no-newline"
        ),
    ],
)
def test_a_malformed_sidecar_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    """A sidecar that a consumer's sha256sum would reject is refused here first."""
    sidecar = tmp_path / "a.tar.gz.sha256"
    sidecar.write_text(text, encoding="utf-8")
    with pytest.raises(PackagingError, match=message):
        read_sidecar(sidecar)


def test_a_tampered_archive_fails_its_sidecar(packed_dist: Path) -> None:
    """Changing an archive after packaging is caught by the digest check."""
    archive = packed_dist / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(PackagingError, match="does not match sidecar"):
        check_sidecar(archive)


def test_a_sidecar_naming_another_file_is_refused(packed_dist: Path) -> None:
    """A sidecar must name the archive it sits beside."""
    archive = packed_dist / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    sidecar = archive.with_name(archive.name + ".sha256")
    digest, _ = read_sidecar(sidecar)
    sidecar.write_text(f"{digest}  other.tar.gz\n", encoding="utf-8")
    with pytest.raises(PackagingError, match="expected"):
        check_sidecar(archive)


def test_a_missing_sidecar_is_refused(packed_dist: Path) -> None:
    """An archive published without its sidecar fails verification."""
    archive = packed_dist / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    archive.with_name(archive.name + ".sha256").unlink()
    with pytest.raises(PackagingError, match="sidecar is missing"):
        check_sidecar(archive)


def test_the_tar_layout_matches_upstreams(
    fixture_config: Config, packed_dist: Path
) -> None:
    """A tar.gz holds one directory and one executable, named for the stem."""
    archive = packed_dist / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
    stem = fixture_config.stem("cargo-dylint", POSIX_TARGET)
    assert [member.name for member in members] == [stem, f"{stem}/cargo-dylint"]
    assert members[0].isdir()
    assert members[1].mode & 0o111


def test_the_windows_archives_carry_the_exe_suffix(
    fixture_config: Config, tmp_path: Path
) -> None:
    """Windows binaries are packaged as cargo-dylint.exe, not cargo-dylint."""
    source = write_stubs(tmp_path / "release", exe_suffix=".exe")
    dist = tmp_path / "dist"
    pack(fixture_config, WINDOWS_TARGET, source, dist)
    stem = fixture_config.stem("cargo-dylint", WINDOWS_TARGET)
    with zipfile.ZipFile(dist / f"{stem}.zip") as archive:
        assert sorted(archive.namelist()) == [f"{stem}/", f"{stem}/cargo-dylint.exe"]


def test_the_windows_zip_records_an_executable_mode(
    fixture_config: Config, tmp_path: Path
) -> None:
    """A consumer extracting the zip on a POSIX host still gets an executable.

    A POSIX extractor honours the mode bits only on an entry that claims Unix
    origin, and ``ZipInfo`` defaults to MS-DOS when it is constructed on
    Windows, which is where this archive is packed. Both halves are asserted:
    the mode and the origin that makes it mean anything.
    """
    source = write_stubs(tmp_path / "release", exe_suffix=".exe")
    dist = tmp_path / "dist"
    pack(fixture_config, WINDOWS_TARGET, source, dist)
    stem = fixture_config.stem("dylint-link", WINDOWS_TARGET)
    with zipfile.ZipFile(dist / f"{stem}.zip") as archive:
        infos = archive.infolist()
        info = archive.getinfo(f"{stem}/dylint-link.exe")
    assert [entry.create_system for entry in infos] == [UNIX_CREATE_SYSTEM] * len(
        infos
    ), "every zip entry must claim Unix origin or its mode is discarded"
    assert (info.external_attr >> 16) & 0o111, "the packaged binary is not executable"


def test_a_zip_entry_of_msdos_origin_fails_the_layout_check(
    fixture_config: Config, tmp_path: Path
) -> None:
    """An entry whose mode a POSIX extractor would discard is not executable.

    Reading the raw attribute bits would pass this archive, which is how the
    defect this guards against would have shipped.
    """
    source = write_stubs(tmp_path / "release", exe_suffix=".exe")
    dist = tmp_path / "dist"
    pack(fixture_config, WINDOWS_TARGET, source, dist)
    stem = fixture_config.stem("cargo-dylint", WINDOWS_TARGET)
    original = dist / f"{stem}.zip"
    rebuilt = tmp_path / "msdos.zip"
    with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(rebuilt, "w") as zf:
        for info in source_zip.infolist():
            copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            copy.create_system = 0
            copy.external_attr = info.external_attr
            zf.writestr(copy, source_zip.read(info.filename))
    with pytest.raises(PackagingError, match="is not executable"):
        check_layout(rebuilt, stem, "cargo-dylint.exe")


@pytest.mark.parametrize(
    ("target", "name"),
    [
        pytest.param(
            POSIX_TARGET,
            "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz",
            id="tar-gz",
        ),
        pytest.param(
            WINDOWS_TARGET,
            "cargo-dylint-x86_64-pc-windows-msvc-v6.0.4.zip",
            id="zip",
        ),
    ],
)
def test_packaging_is_deterministic(
    fixture_config: Config, tmp_path: Path, target: str, name: str
) -> None:
    """Packaging the same binary twice yields the same digest, in both formats.

    Timestamps, ownership and the zip's originating system are fixed so a
    rebuild can be compared against a published sidecar, and so the same
    binary packed on a different host gives the same archive.
    """
    source = write_stubs(tmp_path / "release", exe_suffix=exe_suffix(target))
    first = tmp_path / "first"
    second = tmp_path / "second"
    pack(fixture_config, target, source, first)
    pack(fixture_config, target, source, second)
    assert (first / name).read_bytes() == (second / name).read_bytes()


def test_a_missing_built_binary_names_the_path(
    fixture_config: Config, tmp_path: Path
) -> None:
    """Packaging a target whose build produced nothing fails with the path."""
    empty = tmp_path / "release"
    empty.mkdir()
    with pytest.raises(PackagingError, match="built binary is missing"):
        pack(fixture_config, POSIX_TARGET, empty, tmp_path / "dist")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(
            "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz",
            ("cargo-dylint", "x86_64-apple-darwin", "tar.gz"),
            id="darwin-targz",
        ),
        pytest.param(
            "dylint-link-x86_64-pc-windows-msvc-v6.0.4.zip",
            ("dylint-link", "x86_64-pc-windows-msvc", "zip"),
            id="windows-zip",
        ),
    ],
)
def test_an_asset_name_resolves_to_its_leg(
    fixture_config: Config, name: str, expected: tuple[str, str, str]
) -> None:
    """An asset name is parsed by matching the names the configuration makes."""
    assert parse_archive_name(fixture_config, name) == expected


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            "cargo-dylint-x86_64-apple-darwin-v6.0.4.zip", id="format-not-built"
        ),
        pytest.param(
            "cargo-dylint-aarch64-apple-darwin-v6.0.4.tar.gz", id="target-not-built"
        ),
        pytest.param(
            "cargo-dylint-x86_64-apple-darwin-v6.0.3.tar.gz", id="wrong-version"
        ),
        pytest.param(
            "dylint-driver-x86_64-apple-darwin-v6.0.4.tar.gz", id="wrong-binary"
        ),
    ],
)
def test_a_name_this_repository_never_makes_is_refused(
    fixture_config: Config, name: str
) -> None:
    """An asset whose name drifts from the contract is rejected, not parsed."""
    with pytest.raises(PackagingError, match="not an archive this configuration"):
        parse_archive_name(fixture_config, name)


@requires_posix_exec
def test_a_packed_archive_verifies_end_to_end(
    fixture_config: Config, packed_dist: Path
) -> None:
    """Verification extracts each archive and runs the binary it contains."""
    digests = verify_dist(
        fixture_config, packed_dist, target=POSIX_TARGET, run_smoke=True
    )
    assert len(digests) == 2


@requires_posix_exec
def test_a_binary_reporting_the_wrong_version_fails(
    fixture_config: Config, tmp_path: Path
) -> None:
    """A cargo-dylint that reports another version is not the one we pinned."""
    source = tmp_path / "release"
    source.mkdir()
    (source / "cargo-dylint").write_text(
        '#!/bin/sh\necho "cargo-dylint 1.2.3"\n', encoding="utf-8"
    )
    (source / "cargo-dylint").chmod(0o755)
    (source / "dylint-link").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (source / "dylint-link").chmod(0o755)
    dist = tmp_path / "dist"
    pack(fixture_config, POSIX_TARGET, source, dist)
    archive = dist / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    with pytest.raises(PackagingError, match=r"expected 'cargo-dylint 6\.0\.4'"):
        verify(fixture_config, archive)


def test_the_smoke_command_asks_cargo_dylint_for_its_version() -> None:
    """cargo-dylint reports its version through its cargo subcommand, not --version."""
    assert smoke_command("cargo-dylint", Path("/bin/x"))[-2:] == ["dylint", "--version"]


def test_the_smoke_command_runs_dylint_link_bare() -> None:
    """dylint-link forwards its arguments to the linker, so it is run with none."""
    assert smoke_command("dylint-link", Path("/bin/x")) == ["/bin/x"]


def test_the_expected_asset_set_pairs_every_archive_with_a_sidecar(
    fixture_config: Config,
) -> None:
    """Every archive the release publishes is expected to carry a sidecar."""
    assets = expected_assets(fixture_config, None)
    archives = [name for name in assets if not name.endswith(".sha256")]
    assert sorted(assets) == sorted(archives + [f"{name}.sha256" for name in archives])


def test_a_missing_asset_fails_the_audit(
    fixture_config: Config, packed_dist: Path
) -> None:
    """An incomplete release is refused before it can be published."""
    (packed_dist / "dylint-link-x86_64-apple-darwin-v6.0.4.tar.gz").unlink()
    with pytest.raises(PackagingError, match="missing="):
        verify_dist(fixture_config, packed_dist, target=POSIX_TARGET, run_smoke=False)


def test_an_unexpected_asset_fails_the_audit(
    fixture_config: Config, packed_dist: Path
) -> None:
    """A whole-release audit refuses an asset the configuration never makes."""
    (packed_dist / "stray.tar.gz").write_bytes(b"")
    with pytest.raises(PackagingError, match="unexpected="):
        verify_dist(fixture_config, packed_dist, target=None, run_smoke=False)


@pytest.mark.parametrize("fmt", ["tar.gz", "zip"])
@settings(max_examples=50, deadline=None)
@given(
    escape=st.sampled_from(["..", "../..", "/etc"]),
    leaf=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd")),
        min_size=1,
        max_size=8,
    ),
)
def test_an_archive_member_cannot_escape_the_extraction_directory(
    tmp_path_factory: pytest.TempPathFactory, fmt: str, escape: str, leaf: str
) -> None:
    """Extraction refuses a member whose path resolves outside the destination.

    Nobody writes these paths down, so they are generated: a hostile archive
    is the one input a consumer cannot inspect before extracting. Both
    formats are covered, because the consumer chooses the extractor and the
    zip path is the one a Windows consumer takes.
    """
    directory = tmp_path_factory.mktemp("escape")
    member = f"{escape}/{leaf}"
    archive = directory / f"hostile.{fmt}"
    if fmt == "tar.gz":
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(member)
            info.size = 0
            tar.addfile(info)
    else:
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(member, b"")
    with pytest.raises(PackagingError, match="escapes the archive"):
        extract(archive, fmt, directory / "out")


@requires_posix_exec
def test_a_packed_windows_release_verifies_end_to_end(
    fixture_config: Config, tmp_path: Path
) -> None:
    """Both Windows formats verify with the binary actually run from each.

    The Windows leg is the one that packs a zip, carries an ``.exe`` suffix
    and takes the zip extraction path, so it is exercised whole rather than
    inspected piecemeal.
    """
    source = write_stubs(tmp_path / "release", exe_suffix=".exe")
    dist = tmp_path / "dist"
    packed = pack(fixture_config, WINDOWS_TARGET, source, dist)
    digests = verify_dist(fixture_config, dist, target=WINDOWS_TARGET, run_smoke=True)
    assert sorted(archive.name for archive in packed) == sorted(
        name
        for name in expected_assets(fixture_config, WINDOWS_TARGET)
        if not name.endswith(".sha256")
    ), "the Windows leg must publish a tar.gz and a zip for each binary"
    assert len(digests) == len(packed), (
        "every archive the Windows leg packs must verify, zip included"
    )


@requires_posix_exec
def test_inspecting_an_archive_reports_what_it_is(
    fixture_config: Config, packed_dist: Path
) -> None:
    """Reading an archive establishes its leg and digest without running it."""
    archive = packed_dist / "cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz"
    report = inspect_archive(fixture_config, archive)
    assert (report.binary, report.target, report.fmt) == (
        "cargo-dylint",
        POSIX_TARGET,
        "tar.gz",
    ), "the report must name the leg the archive belongs to"
    assert report.digest == check_sidecar(archive), (
        "the reported digest must be the one the sidecar records"
    )
