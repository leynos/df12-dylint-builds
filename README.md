# df12-dylint-builds

Prebuilt [Dylint](https://github.com/trailofbits/dylint) release archives for
the platforms upstream does not publish.

Upstream publishes `cargo-dylint` and `dylint-link` archives for Linux only.
This repository builds the same two binaries, from the same upstream tag and
with the same build command, for the targets the estate also needs, and
publishes them under archive names and checksum sidecars identical in shape to
upstream's, so a consumer can resolve any target from one manifest.

The build workflow, the consumer contract and the architecture decision record
land in the first pull request against this repository.

## Licence

ISC. See [LICENSE](LICENSE).
