# ADR 001: Prebuilt Dylint archives for macOS x86_64 and Windows

- Status: accepted
- Date: 2026-09-06

## Context

The estate resolves developer tooling from a manifest that maps a tool and a
target triple to a release archive and a checksum. Dylint is the last tool in
that manifest with no archive for two of the targets developers use.

Upstream's release workflow builds Linux archives only. The v6.0.4 release
carries `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu` and
nothing else. An `aarch64-apple-darwin` leg was added to upstream's workflow
on 2026-08-15, the day after v6.0.4 was tagged, so it will appear in the next
upstream release. Neither `x86_64-apple-darwin` nor `x86_64-pc-windows-msvc`
is built anywhere upstream, and a request for more targets is open as
trailofbits/dylint#2068.

Without archives for those targets a consumer has three options: compile
Dylint from source on every machine, drop Dylint on those platforms, or
obtain the archives elsewhere. The first is slow and violates the estate's
rule against source builds in CI. The second removes a lint gate from the
platforms most likely to need it.

## Decision

Build and publish the two missing archives from this repository, from
unmodified upstream sources at a pinned commit, in a form indistinguishable
from upstream's own.

### Which targets

`x86_64-apple-darwin` and `x86_64-pc-windows-msvc`, and no others. The
Linux targets stay upstream's, and `aarch64-apple-darwin` is deliberately
excluded because upstream now builds it. `dylint.toml` refuses a
configuration whose built targets overlap the upstream ones, so this
repository cannot quietly start shadowing an upstream asset.

### How the binaries are built

By checking out `trailofbits/dylint` at the commit the pinned tag resolves
to and running upstream's own release command:

```console
cargo build --locked --release --target "$TARGET" \
  -p cargo-dylint -p dylint-link --features=dylint/__driver_from_crates_io
```

The obvious alternative, `cargo install --locked cargo-dylint --version
6.0.4` from crates.io, was rejected. `__driver_from_crates_io` is a feature
of the `dylint` library crate, and `cargo-dylint` does not re-export it
through its own feature table, so `cargo install` cannot enable it. Upstream
enables it for release binaries so that the published `cargo-dylint` fetches
its driver from crates.io instead of building one locally. A crates.io
install would therefore produce a binary that behaves differently from every
archive upstream publishes, which defeats the purpose of the exercise.

Checking out a commit rather than a tag means a retagged upstream release
cannot change what this repository builds without an explicit change to
`dylint.toml`.

### Archive naming and layout

Upstream's, exactly:

- `<binary>-<target>-v<version>.tar.gz`
- one directory named for that stem, containing one executable
- a sidecar `<archive>.sha256` holding the digest, two spaces, the archive's
  base name and a newline, which is what `sha256sum -c` and `shasum -c`
  expect

The Windows target also gets a `.zip` of the same content. Consumers pick an
extractor by extension, and a Windows consumer that cannot invoke `tar`
needs the zip; a consumer that can use either is unaffected by its presence.

Archives are written with fixed ownership, permissions and timestamps, so
rebuilding the same binaries yields the same digest.

A release job downloads upstream's own Linux archives and applies these same
sidecar and layout checks to them. If upstream changes either format, the
release fails rather than publishing something a consumer would treat
differently from upstream's assets.

### Tag scheme

`v<dylint version>+build.<n>`, for example `v6.0.4+build.1`.

The version must be the upstream version, because it appears inside every
archive name; a separate versioning scheme for this repository would put two
version numbers in play. The build number distinguishes a repackaging of the
same upstream release, which is needed when a build is fixed but upstream
has not moved.

`+` is semantic versioning's build-metadata separator, which is what this
number is. It was tested against GitHub before being adopted: a tag
containing `+` pushes, resolves through the refs API, accepts a release, and
its assets download both as `probe%2Bbuild.1` and as `probe+build.1`, and
through `gh release download`. The alternative, `-build.<n>`, would read as
a semantic versioning pre-release, implying the build is older than the
plain version, which is the wrong relation.

The release workflow builds its tag pattern from `dylint.toml`, so a tag
naming any version other than the configured one fails the first job before
anything is built.

## Consequences

- The estate's tool manifest can resolve Dylint on all four targets.
- The repository must be retagged when upstream releases a new version:
  update `dylint.toml`'s version, tag and commit, then push a `+build.1`
  tag. Nothing else changes.
- The build requirements discovered on each platform are recorded in
  `docs/build-requirements.md` so they can be offered upstream. If upstream
  adopts them, this repository's built targets shrink to nothing and it can
  be archived.
- Publishing binaries built from someone else's source carries a
  distribution responsibility. Dylint is MIT or Apache-2.0, both of which
  permit redistribution; the sources are unmodified and the commit is
  recorded in the configuration and in every release.
