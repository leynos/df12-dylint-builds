# Developers' guide

## Layout

| Path | Purpose |
| --- | --- |
| `dylint.toml` | The single source of truth: version, commit, targets, runners, formats |
| `scripts/dylint_config.py` | Parses and validates the configuration, derives every name |
| `scripts/matrix.py` | Prints the build matrix, the pinned facts and validates a tag |
| `scripts/package.py` | Packages, verifies and audits archives and sidecars |
| `scripts/verify_upstream.py` | Downloads upstream's archives and checks them the same way |
| `tests/` | Unit tests, property tests and the workflow contracts |

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
4. Check whether upstream has started building either of our targets. If it
   has, remove it here; the configuration will refuse the overlap anyway.
5. `make all`, commit, merge, then push `vX.Y.Z+build.1`.

## The workflow contracts

`tests/test_workflow_contracts.py` asserts the mechanisms the release relies
on, not the prose around them: the exact `run:` command, the `uses:` pin, the
runner label, the job dependencies. An assertion that merely finds an
identifier would be satisfied by the comment above a deleted line.

Every contract's doc comment records a mutation that was applied once and
made it fail. When you add a contract, do the same: change the line it
protects, watch the test fail, restore, and write the mutation down.

If you mutate a source file and revert it, clear `scripts/__pycache__` and
`tests/__pycache__` first. Python validates cached bytecode on the source's
size and modification time to the second, so an edit of the same length that
is reverted within a second is not noticed.

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

Only `create-release`, `build` and `publish` hold `contents: write`.
