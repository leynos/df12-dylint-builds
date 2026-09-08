# Users' guide: consuming the archives

This repository publishes Dylint binaries for the two targets upstream does
not build. Everything here is a contract: it is asserted by tests, and a
release that would break it fails instead of publishing.

## Where the assets are

Releases of `leynos/df12-dylint-builds`. A release tag is
`v<dylint version>+build.<n>`, and every asset in it carries the dylint
version, not the tag.

```text
https://github.com/leynos/df12-dylint-builds/releases/download/v6.0.4+build.1/<asset>
```

A `+` in a tag is accepted unencoded in that path, and also works as
`%2B`.

## Asset names

Upstream's scheme, unchanged:

```text
<binary>-<target>-v<version>.tar.gz
<binary>-<target>-v<version>.tar.gz.sha256
```

`<binary>` is `cargo-dylint` or `dylint-link`. `<version>` is the dylint
version, without the build number. For `x86_64-pc-windows-msvc` there is
also a `.zip` and its own `.sha256`.

The complete set for a release is printed by `make expected`.

| Target | Source | Formats |
| --- | --- | --- |
| `x86_64-unknown-linux-gnu` | upstream | `tar.gz` |
| `aarch64-unknown-linux-gnu` | upstream | `tar.gz` |
| `x86_64-apple-darwin` | here | `tar.gz` |
| `x86_64-pc-windows-msvc` | here | `tar.gz`, `zip` |

## Archive layout

One directory, named for the archive stem, containing one executable:

```text
cargo-dylint-x86_64-apple-darwin-v6.0.4/
cargo-dylint-x86_64-apple-darwin-v6.0.4/cargo-dylint
```

On Windows the executable carries `.exe`. The file is recorded with mode
`0755`, in the zip as well as the tar, and the zip's entries are marked as
Unix-created so that the mode is not discarded.

Whether the extracted file is executable depends on the extractor. GNU
`tar`, `bsdtar` and `unzip` apply the recorded mode. Python's
`zipfile.extractall` and `shutil.unpack_archive` do not: they ignore the
mode entirely and leave the file non-executable. If extraction goes
through one of those, `chmod +x` the binary afterwards.

## Checksums

Each archive has a sidecar in `sha256sum` format: the hex digest, two
spaces, the archive's base name, and a newline.

```console
$ cat cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz.sha256
<64 hex digits>  cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz
$ sha256sum -c cargo-dylint-x86_64-apple-darwin-v6.0.4.tar.gz.sha256
```

This is byte-for-byte the format upstream uses, which the release proves by
downloading upstream's own sidecars and checking them with the same code.
A consumer needs one verification path for all four targets.

## What is not published

- No detached signatures. Upstream signs its archives with minisign using a
  key generated inside its release run; there is no stable public key to
  pin, so a signature here would add ceremony without adding trust.

  This means the sidecars detect corruption, not forgery. A sidecar travels
  with the archive it describes, so checking one against the other proves
  only that the download arrived intact. It cannot detect anyone who can
  replace both assets, which is anyone who can publish to this repository.
  A consumer that needs more than corruption detection should record a
  digest at the point it first vets a release and pin that, rather than
  re-reading the sidecar on each fetch.
- No `aarch64-apple-darwin`. Upstream builds it from v6.0.5 onwards.
- No archives for any target upstream already publishes. Take those from
  upstream.

## Stability

- A published release is immutable. The workflow refuses to re-upload an
  asset to a release that is not still a draft, so a digest recorded against
  a release stays valid.
- Archives are deterministic: the same binaries repackaged give the same
  digest.
- The asset set is audited after upload. A release with a missing or
  unexpected asset is never published.
