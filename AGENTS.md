# Assistant Instructions

This repository publishes prebuilt Dylint archives for the targets upstream
does not build. Read `README.md` first; it states the archive layout, the
naming scheme and the consumer contract.

## Rules

- `dylint.toml` is the single source of truth. The release workflow derives
  its matrix from it and the contract tests read it; never hard-code a
  version, target, runner label or archive name anywhere else.
- Dylint is pinned to an upstream tag **and** the commit that tag resolved
  to. The build checks out the commit, so a moved tag cannot change what is
  built.
- The build command is upstream's own, including
  `--features=dylint/__driver_from_crates_io`. Changing it means shipping a
  binary that behaves differently from upstream's; record why in an ADR.
- Archives contain one directory and one executable, and carry a `.sha256`
  sidecar in `sha256sum` format. `scripts/package.py` is the reference
  implementation and the release applies it to upstream's archives too.
- Releases come from `v<version>+build.<n>` tags through `release.yml`,
  never by hand, and a published release is never rebuilt in place.
- GitHub Actions are referenced by 40-hex commit SHA.
- Nothing is installed or compiled on the runner except dylint itself.
- Shell scripts start with `set -euo pipefail` and carry the exec bit.
- Prose uses en-GB-oxendict spelling ("-ize", "-yse", "-our"); quoted
  identifiers keep their upstream spelling.
- Never open an issue, comment or pull request on a repository outside the
  `leynos` organisation. Upstream requests are drafted here for a human to
  submit.

## Commit gates

Run `make all` before committing. It executes `make check-fmt`, `make lint`
(ruff and markdownlint) and `make test` (unit tests, property tests and the
workflow contracts). Each contract matches the mechanism it protects, the
`run:` command or the exact label, not a step name. When you add one, mutate
the protected line once, confirm the test fails, and record that mutation in
the test's doc comment.

Commit messages use the imperative mood, a subject of about 50 characters,
and a body wrapped at 72 columns explaining what changed and why. Do not add
attribution or session trailers.
