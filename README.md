# df12-dylint-builds

Prebuilt [Dylint](https://github.com/trailofbits/dylint) release archives for
the platforms upstream does not publish.

Upstream's release workflow publishes `cargo-dylint` and `dylint-link` for
Linux only. This repository builds the same two binaries, from the same
upstream commit and with the same build command, for the two targets the
estate also needs:

- `x86_64-apple-darwin`
- `x86_64-pc-windows-msvc`

The archives are named and shaped exactly as upstream's are, so a consumer
can resolve any target from one manifest and use one extractor per
extension.

## What a release contains

For each binary and target, a `tar.gz` archive and its `.sha256` sidecar.
The Windows target additionally gets a `.zip`, because consumers choose an
extractor by file extension.

| Asset | Example |
| --- | --- |
| Archive | `cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz` |
| Sidecar | `cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz.sha256` |
| Windows zip | `dylint-link-x86_64-pc-windows-msvc-v6.0.4.zip` |

Every archive holds one directory, named for the archive stem, containing
one executable. That is upstream's layout, and the release verifies
upstream's own archives against the same rule.

See [the users' guide](docs/users-guide.md) for the full consumer contract.

## Configuration

[`dylint.toml`](dylint.toml) is the single source of truth. It pins the
upstream version, tag and commit, and lists the targets, runners and archive
formats. The release workflow derives its matrix from it and the contract
tests read it, so nothing is written down twice.

## Releases

A release is cut by pushing a tag of the form `v<dylint version>+build.<n>`,
for example `v6.0.4+build.1`. The version names the upstream release being
mirrored; the build number distinguishes reruns of this repository's own
packaging of it. The reasoning is in
[ADR 001](docs/adr-001-prebuilt-dylint-archives.md).

## Development

Run `make all` before committing. See
[the developers' guide](docs/developers-guide.md).

## Relationship to upstream

This repository exists to fill a gap, not to fork anything. It builds
unmodified upstream sources at a pinned commit with upstream's own release
command. The intent is that the requirements it uncovers on each platform
are offered upstream, so that these targets are eventually published there
and this repository can be retired; see
[trailofbits/dylint#2068](https://github.com/trailofbits/dylint/issues/2068).

## Licence

ISC. See [LICENSE](LICENSE). Dylint itself is MIT or Apache-2.0 and is
redistributed here unmodified.
