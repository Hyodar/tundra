# Releasing tundravm

A `v*` tag releases tundravm. The tag is the version: `v2.0.0` builds `tundravm 2.0.0`,
and a commit after it reports `2.0.0.post1.dev<n>+g<sha>`.

## Cut a release

1. On a green master, move the Unreleased notes under the version:
   `uv run python scripts/release.py cut 2.0.0`.
2. Read the new `## 2.0.0` section of `CHANGELOG.md`, then commit it alone:
   `git commit -am "docs: the changelog releases 2.0.0"`.
3. Push master and wait for `ci` to pass on that commit.
4. Tag that commit and push the tag: `git tag -a v2.0.0 -m "tundravm 2.0.0"`, then
   `git push origin v2.0.0`.
5. Watch the `release` workflow, then read the release page it creates.

A tag whose `CHANGELOG.md` has no section for its version is refused, so a release always
carries its notes in the tagged tree. Nothing is committed by the workflow.

## What the workflow does

1. Runs a copy of `ci.yml`'s test job: lint, types, the unit suite, the Go tests of
   `tools/`, and `check` of the example against its committed tree.
2. Builds the sdist and the wheel with `uv build` and checks both carry the tag's version.
3. Installs the wheel in a clean venv: `--version` prints the tag's version, `doctor` runs,
   and the default tools source is `Git(<repo>, <the tag's commit>, subdir="tools")`.
4. Creates the GitHub release with the tag's `CHANGELOG.md` section as its body and both
   files attached.
5. Publishes to PyPI by trusted publishing, only once a `pypi` environment exists.

## The image pipeline `new` writes

`tundravm new NAME --ci` writes `.github/workflows/image.yml` for the image project.
Every push and pull request runs `check --tree mkosi` and `lock --check`, then builds the
production profile, verifies it and uploads its manifest, `measurements.json` and SBOM. A
`v*` tag also rebuilds that build on a second runner (`verify --rebuild`), attests the
provenance of every released file (on a public repository: GitHub attests private ones on
Enterprise Cloud only) and creates a release with `payload.efi`, each disk, the manifest,
the measurements, the SBOM and `SHA256SUMS`. Commit `uv.lock`, `build/tundravm.lock` and
`mkosi/` first.

## What a user installs

```console
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
```

The tag pins the release, and so does the wheel attached to it. An image built with either
builds `tools/` from the repository at the tag's commit, so no checkout of tundravm is
needed. mkosi is not on PyPI, so the wheel names mkosi 27.1 by its git commit. PyPI refuses
such a dependency: leave the `pypi` environment uncreated until mkosi is on PyPI.

Beside the wheel, a Debian 13 or Ubuntu 24.04 host needs four packages for `dev`, and
`doctor` names the command for each one missing:

| Package | For |
|---|---|
| `debian-archive-keyring` | mkosi checks its tools tree against it; every other build tool (`systemd-repart`, `mkfs.*`, `ukify`, `systemd-boot`) comes from that tree |
| `golang` (1.21 or newer) | fetches the toolchain and modules of each Go source, the evidence service's among them |
| `qemu-system-x86`, `ovmf` | boot the dev build |

A Cargo source that pins a Rust toolchain needs `rustup` too. mkosi builds in an
unprivileged user namespace, or under `sudo` where the host withholds them (Ubuntu
24.04's AppArmor default; `doctor` prints the sysctl).
