# Developers' guide

## Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| Python | 3.12 or newer | `scripts/` targets 3.12; the gates run on 3.13 |
| `uv` | any recent release | runs the tests and Ruff in throwaway environments |
| `make` | any | the gates are Makefile targets |
| `markdownlint-cli2` | 0.20 or newer | the Markdown gate |
| Ruff | 0.15.12 | pinned in the Makefile as `RUFF_VERSION` |

No Rust toolchain is needed to develop here. Dylint is built on the release
runners, never locally.

Install `uv` and `markdownlint-cli2` by whatever means the system
prefers, then:

```console
make all
```

`uv` fetches pytest, PyYAML and Hypothesis on demand from the versions
pinned in the Makefile's `PYTEST_DEPS`; `pyproject.toml` lists the same
bounds under `dependency-groups.dev` for an editor that wants a resolved
environment. Ruff is run through `uv tool run` at the pinned version, so a
different Ruff on the PATH does not change the verdict. Either tool can be
overridden for a one-off run with `make MDLINT=... markdownlint` or
`make RUFF_VERSION=... ruff`.

The Markdown gate is the one place where a green local run is not proof:
CI runs a newer markdownlint than most hosts have, and the two disagree
about consecutive blank lines.

## Layout

| Path | Purpose |
| --- | --- |
| `dylint.toml` | The single source of truth: version, commit, targets, runners, formats |
| `scripts/dylint_config.py` | Parses and validates the configuration, derives every name |
| `scripts/matrix.py` | Prints the build matrix, the pinned facts and validates a tag |
| `scripts/package.py` | Packages, verifies and audits archives and sidecars |
| `scripts/verify_upstream.py` | Downloads upstream's archives and checks them the same way |
| `tests/` | Unit tests, property tests and the workflow contracts |

## Errors

Both command lines handle exactly two exception types, `ConfigError` and
`PackagingError`, and turn them into an `error: ...` line and exit status 1.
Anything else reaches the runner as a traceback, which names a Python frame
rather than the asset that failed.

Every boundary that reads something therefore converts what the library
raises. `_reading` in `scripts/package.py` wraps the filesystem and archive
calls, so a truncated tarball, an unreadable sidecar or a file that is not a
zip is reported as `<name>: could not read ...`. `smoke_test` converts both
a binary that will not start and one that does not finish inside
`SMOKE_TIMEOUT`. `download` converts a destination it cannot write, which
the retry loop must not treat as a transient network failure.

A new function that opens a file, reads an archive or spawns a process needs
the same treatment and a test that proves it, or the release fails with a
traceback instead of a diagnosis.

## Commit gates

```console
make all
```

That runs, in order, `make check-fmt`, `make lint` (ruff and
markdownlint-cli2) and `make test`. Run them sequentially, never in
parallel, and read the whole output rather than the tail: a clean summary
from one tool can sit above another tool's failure.

`make fmt` fixes formatting and the lint findings that are auto-fixable.

## Adding or changing a target

Edit `dylint.toml` and nothing else. The matrix, the archive names, the
expected asset set and the contracts all follow. The configuration refuses:

- a target that upstream already publishes, so this repository cannot
  shadow an upstream asset
- a tag that disagrees with the configured version
- an archive format other than `tar.gz` or `zip`
- a version that is not `MAJOR.MINOR.PATCH`, or a commit that is not 40 hex

The executable suffix is derived from the target triple rather than
configured, so a Windows target cannot be declared without `.exe`.

Each matrix leg must name a runner of the target's own architecture, because
the build verifies each archive by extracting it and running the binary. A
contract enforces that; cross-compiling would need a different verification
story.

## Moving to a new upstream release

1. Read the new version from crates.io and the tag from upstream's releases.
2. Resolve the tag to a commit:
   `gh api repos/trailofbits/dylint/git/ref/tags/vX.Y.Z --jq .object.sha`.
3. Update `version`, `tag` and `commit` in `dylint.toml`.
4. Check whether upstream has started building either target published
   here. If it has, remove it; the configuration refuses the overlap anyway.
5. `make all`, commit, merge, then push `vX.Y.Z+build.1`.

## The workflow contracts

`tests/test_workflow_contracts.py` asserts the mechanisms the release relies
on, not the prose around them: the exact `run:` command, the `uses:` pin, the
runner label, the job dependencies. An assertion that merely finds an
identifier would be satisfied by the comment above a deleted line.

Every contract's doc comment records a mutation that was applied once and
made it fail. A new contract earns the same treatment: change the line it
protects, watch the test fail, restore, and write the mutation down.

Mutating a source file and reverting it calls for clearing
`scripts/__pycache__` and `tests/__pycache__` first. Python validates cached
bytecode on the source's size and modification time to the second, so an
edit of the same length that is reverted within a second is not noticed.

## Running things by hand

```console
make matrix     # the release build matrix as JSON
make expected   # every asset name a release must publish
make upstream   # download upstream's archives and check them
```

`python3 scripts/package.py pack --target <triple> --source-dir <dir>
--out-dir dist` packages a directory of built binaries, and
`python3 scripts/package.py verify --dist dist --target <triple>` extracts
each archive and runs the binary inside it.

## The release workflow

Triggered by a `v*` tag push. In order:

1. `prepare` validates the tag against `dylint.toml` and emits the matrix
   and the pinned upstream facts.
2. `create-release` opens a draft, refusing to touch a release that has
   already been published.
3. `build` runs once per target: checks out dylint at the pinned commit,
   builds both binaries with upstream's command, packages, verifies by
   running each binary, and uploads.
4. `verify-upstream` downloads upstream's Linux archives and checks their
   sidecars and layout with the same code, proving format parity.
5. `audit` downloads every draft asset afresh and checks the set, the
   sidecars and the layout. It runs on Linux and so cannot execute the
   binaries; that has already happened on each build leg.
6. `publish` clears the draft flag.

### Permissions and the draft

The workflow defaults to `contents: read`. Four jobs raise it to
`contents: write`: `create-release`, `build`, `audit` and `publish`.

`audit` is the surprising one, because it only reads. A draft release is
visible only to a token with push access, so with `contents: read` the API
answers "release not found" for a draft that plainly exists, and the audit
fails having checked nothing. Run 34208473988 lost a release to this: both
build legs succeeded and uploaded all twelve assets, and the audit could not
see the draft holding them.

A contract asserts the audit job's permission for that reason, and a second
asserts that the set of jobs holding write is exactly those four, so the
grant cannot spread to `prepare` or `verify-upstream`, neither of which
touches the release.

### When a release fails

Two cases, and they are not the same.

If the workflow is right and a job failed for a transient reason, re-run
the failed jobs. `create-release` resumes a draft left by an earlier run of
the same tag, and refuses a tag that is already published, so a retry
cannot alter what a consumer has already seen.

If the workflow itself is wrong, the tag cannot be re-run. A re-run uses the
workflow file as it was at that tag, so it will fail the same way. Fix the
workflow, merge it, and push the next build number. **This is what the build
number is for.** It distinguishes a repackaging of the same upstream release
from a new upstream release, so a broken run costs a build number rather
than a version.

`v6.0.4+build.1` is the worked example. Both legs built and uploaded all
twelve assets, the audit could not see the draft, and the fix was a change
to the workflow. It was abandoned in favour of `v6.0.4+build.2`, and its
draft release and tag were deleted so that the only tag in the repository is
one that published.

Never delete or re-push a tag whose release was published. Consumers pin the
digests in its sidecars, and a published release is immutable by design; the
draft of a failed run is the only thing that may be discarded.
