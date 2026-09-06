"""Package built dylint binaries into upstream-shaped release archives.

An archive is named ``<binary>-<target>-v<version>.<format>`` and holds a
single directory of the same stem containing a single executable, which is
exactly the layout of upstream's Linux archives. Each archive is written
alongside a ``.sha256`` sidecar in ``sha256sum`` format.

Archives are deterministic: entry ownership, permissions and timestamps are
fixed, so rebuilding the same binaries yields the same digest.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import gzip
import hashlib
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from dylint_config import Config, ConfigError, default_config_path, load_config

# 1980-01-01T00:00:00Z: the earliest timestamp the zip format can store, and
# so the one fixed point both formats can agree on.
FIXED_MTIME: Final = 315532800
DIR_MODE: Final = 0o755
EXE_MODE: Final = 0o755
# The zip "created by" code for Unix. Only entries carrying it have their
# mode bits honoured by a POSIX extractor.
UNIX_CREATE_SYSTEM: Final = 3
CHUNK: Final = 1 << 20
SMOKE_TIMEOUT: Final = 120


class PackagingError(RuntimeError):
    """Raised when packaging or verifying an archive fails."""


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sidecar(archive: Path) -> Path:
    """Write ``<archive>.sha256`` in ``sha256sum`` format and return its path.

    The format matches upstream's byte for byte: the digest, two spaces, the
    archive's base name and a trailing newline.
    """
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{sha256_file(archive)}  {archive.name}\n", encoding="utf-8")
    return sidecar


def read_sidecar(sidecar: Path) -> tuple[str, str]:
    """Return the (digest, file name) a sidecar records."""
    text = sidecar.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        raise PackagingError(f"{sidecar.name}: sidecar must end with a newline")
    fields = text.rstrip("\n").split("  ")
    if len(fields) != 2 or len(fields[0]) != 64:
        raise PackagingError(f"{sidecar.name}: not a sha256sum line: {text!r}")
    return fields[0], fields[1]


def check_sidecar(archive: Path) -> str:
    """Verify an archive against its sidecar and return the digest."""
    sidecar = archive.with_name(archive.name + ".sha256")
    if not sidecar.is_file():
        raise PackagingError(f"{archive.name}: sidecar is missing")
    recorded, name = read_sidecar(sidecar)
    if name != archive.name:
        raise PackagingError(
            f"{sidecar.name}: names {name!r}, expected {archive.name!r}"
        )
    actual = sha256_file(archive)
    if actual != recorded:
        raise PackagingError(
            f"{archive.name}: digest {actual} does not match sidecar {recorded}"
        )
    return actual


def parse_archive_name(config: Config, name: str) -> tuple[str, str, str]:
    """Return the (binary, target, format) an archive name encodes.

    The name is matched against the names the configuration generates, so an
    asset whose name drifts from the contract is rejected rather than parsed.
    """
    for target in config.target_triples:
        for binary in config.binaries:
            for fmt in config.target(target).formats:
                if name == config.archive_name(binary, target, fmt):
                    return binary, target, fmt
    raise PackagingError(f"{name}: not an archive this configuration produces")


def _tar_archive(archive: Path, stem: str, binary_path: Path) -> None:
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        directory = tarfile.TarInfo(stem)
        directory.type = tarfile.DIRTYPE
        directory.mode = DIR_MODE
        directory.mtime = FIXED_MTIME
        tar.addfile(directory)

        member = tarfile.TarInfo(f"{stem}/{binary_path.name}")
        member.size = binary_path.stat().st_size
        member.mode = EXE_MODE
        member.mtime = FIXED_MTIME
        with binary_path.open("rb") as handle:
            tar.addfile(member, handle)


def _zip_archive(archive: Path, stem: str, binary_path: Path) -> None:
    fixed = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        directory = zipfile.ZipInfo(f"{stem}/", date_time=fixed)
        directory.create_system = UNIX_CREATE_SYSTEM
        directory.external_attr = (stat.S_IFDIR | DIR_MODE) << 16 | 0x10
        zf.writestr(directory, b"")

        member = zipfile.ZipInfo(f"{stem}/{binary_path.name}", date_time=fixed)
        # ZipInfo defaults create_system to MS-DOS when it is built on
        # Windows, and a POSIX extractor ignores the Unix mode bits of an
        # MS-DOS entry. The Windows leg packs this archive, so the field is
        # set explicitly or the recorded mode would be silently discarded.
        member.create_system = UNIX_CREATE_SYSTEM
        member.compress_type = zipfile.ZIP_DEFLATED
        member.external_attr = (stat.S_IFREG | EXE_MODE) << 16
        zf.writestr(member, binary_path.read_bytes())


def pack(config: Config, target: str, source_dir: Path, out_dir: Path) -> list[Path]:
    """Package every configured archive for ``target`` and return their paths."""
    spec = config.target(target)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for binary in config.binaries:
        binary_path = source_dir / f"{binary}{spec.exe_suffix}"
        if not binary_path.is_file():
            raise PackagingError(f"built binary is missing: {binary_path}")
        stem = config.stem(binary, target)
        for fmt in spec.formats:
            archive = out_dir / config.archive_name(binary, target, fmt)
            if fmt == "tar.gz":
                _tar_archive(archive, stem, binary_path)
            else:
                _zip_archive(archive, stem, binary_path)
            write_sidecar(archive)
            written.append(archive)
    return written


def extract(archive: Path, fmt: str, destination: Path) -> None:
    """Extract an archive, refusing any member that escapes ``destination``."""
    root = destination.resolve()

    def guard(name: str) -> Path:
        resolved = (destination / name).resolve()
        if resolved != root and root not in resolved.parents:
            raise PackagingError(f"{archive.name}: member escapes the archive: {name}")
        return resolved

    if fmt == "tar.gz":
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if not (member.isfile() or member.isdir()):
                    raise PackagingError(
                        f"{archive.name}: unexpected member type: {member.name}"
                    )
                guard(member.name)
            tar.extractall(destination, members=members, filter="data")
        return

    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            guard(name)
        zf.extractall(destination)


def _archive_members(archive: Path) -> tuple[list[str], dict[str, int]]:
    """Return an archive's sorted member names and the modes of its files."""
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
        names = sorted(member.name.rstrip("/") for member in members)
        modes = {m.name.rstrip("/"): m.mode for m in members if m.isfile()}
        return names, modes
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
    names = sorted(info.filename.rstrip("/") for info in infos)
    # An entry that does not claim Unix origin has no mode a POSIX extractor
    # will honour, so it is reported as having none rather than as having
    # whatever happens to sit in the upper attribute bits.
    modes = {
        info.filename: (info.external_attr >> 16) & 0o7777
        if info.create_system == UNIX_CREATE_SYSTEM
        else 0
        for info in infos
        if not info.is_dir()
    }
    return names, modes


def check_layout(archive: Path, stem: str, member: str) -> None:
    """Check that an archive holds exactly ``stem/`` and one executable file.

    This is the layout contract consumers depend on, and it is applied to
    upstream's archives as well as ours so that any divergence is caught.
    """
    expected = f"{stem}/{member}"
    names, modes = _archive_members(archive)
    if names != sorted({stem, expected}):
        raise PackagingError(
            f"{archive.name}: layout is {names}, expected [{stem!r}, {expected!r}]"
        )
    if not modes.get(expected, 0) & 0o111:
        raise PackagingError(f"{archive.name}: {expected} is not executable")


def smoke_command(binary: str, exe: Path) -> list[str]:
    """Return the command that proves a packaged binary runs.

    ``cargo-dylint`` reports its version through its cargo subcommand.
    ``dylint-link`` is a linker wrapper with no version of its own: it
    forwards every argument to the platform linker, so it is run bare and
    judged on whether it starts and reports something.
    """
    if binary == "cargo-dylint":
        return [str(exe), "dylint", "--version"]
    return [str(exe)]


def smoke_test(config: Config, binary: str, exe: Path) -> str:
    """Run the packaged binary and return its combined output."""
    command = smoke_command(binary, exe)
    environment = dict(os.environ)
    # dylint-link resolves the linker through the active toolchain and exits
    # early without this; the value only has to name a toolchain.
    environment.setdefault("RUSTUP_TOOLCHAIN", "stable")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT,
            env=environment,
            check=False,
        )
    except OSError as error:
        raise PackagingError(
            f"{exe.name}: the packaged binary did not start: {error}"
        ) from error
    output = completed.stdout + completed.stderr
    if binary == "cargo-dylint":
        expected = f"cargo-dylint {config.version}"
        if completed.returncode != 0 or expected not in completed.stdout:
            raise PackagingError(
                f"{exe.name}: expected {expected!r} on stdout, got "
                f"exit {completed.returncode} and {output!r}"
            )
    elif not output.strip():
        raise PackagingError(f"{exe.name}: ran but produced no output")
    return output


@dc.dataclass(frozen=True, slots=True)
class ArchiveReport:
    """What reading an archive establishes, without running anything in it.

    Attributes
    ----------
    binary:
        The binary the archive carries, ``cargo-dylint`` or ``dylint-link``.
    target:
        The target triple the binary was built for.
    fmt:
        The archive format, ``tar.gz`` or ``zip``.
    digest:
        The archive's SHA-256 digest, which matched its sidecar.
    """

    binary: str
    target: str
    fmt: str
    digest: str


def inspect_archive(config: Config, archive: Path) -> ArchiveReport:
    """Check everything about an archive that can be checked by reading it.

    This is the whole of the contract that does not require the archive's
    platform: the name resolves to a leg this configuration produces, the
    sidecar matches, and the layout holds one directory and one executable.

    Parameters
    ----------
    config:
        The configuration the archive is expected to conform to.
    archive:
        The archive to read. Its sidecar must sit beside it.

    Returns
    -------
    ArchiveReport
        What the archive was found to be.

    Raises
    ------
    PackagingError
        If the name, the sidecar or the layout does not hold.
    """
    binary, target, fmt = parse_archive_name(config, archive.name)
    digest = check_sidecar(archive)
    check_layout(
        archive,
        config.stem(binary, target),
        f"{binary}{config.target(target).exe_suffix}",
    )
    return ArchiveReport(binary=binary, target=target, fmt=fmt, digest=digest)


def run_packaged_binary(config: Config, archive: Path, report: ArchiveReport) -> str:
    """Extract an archive to a temporary directory and run the binary inside it.

    This is the half of verification that touches the filesystem and spawns a
    process, and it only means anything on a runner of the archive's own
    architecture.

    Parameters
    ----------
    config:
        The configuration the archive conforms to.
    archive:
        The archive to extract.
    report:
        What :func:`inspect_archive` found, which names the binary and target.

    Returns
    -------
    str
        The binary's combined output.

    Raises
    ------
    PackagingError
        If a member escapes the extraction directory, or the binary does not
        start or does not report what it should.
    """
    with tempfile.TemporaryDirectory() as raw:
        destination = Path(raw)
        extract(archive, report.fmt, destination)
        exe = (
            destination
            / config.stem(report.binary, report.target)
            / f"{report.binary}{config.target(report.target).exe_suffix}"
        )
        # The recorded mode has already been checked by check_layout.
        # Python's extractors discard it, so the bit is restored here to
        # run the binary rather than to assert anything about it.
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return smoke_test(config, report.binary, exe)


def verify(config: Config, archive: Path, *, run_smoke: bool = True) -> str:
    """Verify one archive and return its digest.

    Reading the archive is always done; running the binary inside it is done
    unless ``run_smoke`` is false, which is how the Linux audit re-checks
    assets it cannot execute.
    """
    report = inspect_archive(config, archive)
    if run_smoke:
        run_packaged_binary(config, archive, report)
    return report.digest


def _resolve_config(path: str | None) -> Config:
    return load_config(path or default_config_path())


def _cmd_pack(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    for archive in pack(config, args.target, Path(args.source_dir), Path(args.out_dir)):
        print(f"packed {archive.name}")
    return 0


def expected_assets(config: Config, target: str | None) -> tuple[str, ...]:
    """Return every asset name a release (or one leg of it) must publish."""
    archives = (
        config.archive_names(target) if target else config.released_archive_names()
    )
    return tuple(
        name for archive in archives for name in (archive, f"{archive}.sha256")
    )


def verify_dist(
    config: Config, dist: Path, *, target: str | None, run_smoke: bool
) -> list[str]:
    """Verify a directory of assets against the set the configuration expects.

    Without a target the directory must hold the whole release and nothing
    else, so an asset that should not be there fails the audit as loudly as
    one that is missing.
    """
    expected = set(expected_assets(config, target))
    present = {path.name for path in dist.iterdir() if path.is_file()}
    if target is not None:
        present &= expected
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    if missing or extra:
        raise PackagingError(
            f"{dist}: asset set does not match the configuration; "
            f"missing={missing} unexpected={extra}"
        )
    return [
        verify(config, dist / name, run_smoke=run_smoke)
        for name in sorted(expected)
        if not name.endswith(".sha256")
    ]


def _cmd_verify(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    run_smoke = not args.no_smoke
    if args.archive:
        for name in args.archive:
            digest = verify(config, Path(name), run_smoke=run_smoke)
            print(f"verified {Path(name).name} {digest}")
        return 0
    if not args.dist:
        raise PackagingError("verify needs either archive paths or --dist")
    for digest in verify_dist(
        config, Path(args.dist), target=args.target, run_smoke=run_smoke
    ):
        print(f"verified {digest}")
    return 0


def _cmd_expected(args: argparse.Namespace) -> int:
    config = _resolve_config(args.config)
    names = (
        config.archive_names(args.target)
        if args.target
        else config.released_archive_names()
    )
    for name in names:
        print(name)
        print(f"{name}.sha256")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to dylint.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    packer = sub.add_parser("pack", help="package one target's archives")
    packer.add_argument("--target", required=True)
    packer.add_argument("--source-dir", required=True)
    packer.add_argument("--out-dir", required=True)
    packer.set_defaults(func=_cmd_pack)

    verifier = sub.add_parser("verify", help="verify packaged archives")
    verifier.add_argument("archive", nargs="*")
    verifier.add_argument(
        "--dist", help="verify every expected asset in this directory"
    )
    verifier.add_argument("--target", help="restrict --dist to one target's assets")
    verifier.add_argument(
        "--no-smoke",
        action="store_true",
        help="check the sidecar and layout without running the binary",
    )
    verifier.set_defaults(func=_cmd_verify)

    expected = sub.add_parser("expected", help="print the expected asset names")
    expected.add_argument("--target")
    expected.set_defaults(func=_cmd_expected)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, PackagingError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
