# Releasing tundravm

A tag that starts with `v` releases tundravm, and the tag is the version. The tag `v2.0.0`
builds `tundravm 2.0.0`, and a commit after it reports `2.0.0.post1.dev<n>+g<sha>`.

## Cut a release

1. On a green master, move the Unreleased notes under the version with
   `uv run python scripts/release.py cut 2.0.0`.
2. Read the new `## 2.0.0` section of `CHANGELOG.md`, then commit that change alone with
   `git commit -am "docs: the changelog releases 2.0.0"`.
3. Push master and wait for `ci` to pass on that commit.
4. Tag that commit with `git tag -a v2.0.0 -m "tundravm 2.0.0"`, then push the tag with
   `git push origin v2.0.0`.
5. Watch the `release` workflow, then read the release page that it creates.

The workflow refuses a tag whose `CHANGELOG.md` has no section for its version, so a release
always carries its notes in the tagged tree. The workflow itself commits nothing.

## What the workflow does

1. It runs a copy of the test job of `ci.yml`. That job runs lint, the type check, the unit
   suite, the Go tests of `tools/`, and `check` of the example against its committed tree.
2. It builds the sdist and the wheel with `uv build` and checks that both carry the version
   of the tag.
3. It installs the wheel in a clean venv. There, `--version` prints the version of the tag,
   `doctor` runs, and the default tools source is
   `Git(<repo>, <the tag's commit>, subdir="tools")`.
4. It creates the GitHub release, using the `CHANGELOG.md` section of the tag as the body and
   attaching both files.
5. It publishes to PyPI by trusted publishing, but only once a `pypi` environment exists.

## The image pipeline `new` writes

`tundravm new NAME --ci` writes `.github/workflows/image.yml` for the image project. Every
push and pull request runs `check --tree mkosi` and `lock --check`. It then builds the
production profile, verifies it and uploads its manifest, `measurements.json` and SBOM. A tag
that starts with `v` does more. It rebuilds the same build on a second runner with
`verify --rebuild`, and it attests the provenance of every released file. That attestation
works on a public repository, and GitHub supports it for private repositories only on
Enterprise Cloud. The tag also creates a release with `payload.efi`, each disk, the manifest,
the measurements, the SBOM and `SHA256SUMS`. Commit `uv.lock`, `build/tundravm.lock` and
`mkosi/` before you tag.

## What a user installs

```console
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
```

The tag pins the release, and so does the wheel attached to it. An image built with either
builds `tools/` from the repository at the commit of the tag, so you need no checkout of
tundravm. mkosi is not on PyPI, so the wheel names mkosi 27.1 by its git commit. PyPI refuses
such a dependency, so leave the `pypi` environment uncreated until mkosi is on PyPI.

Besides the wheel, a Debian 13 or Ubuntu 24.04 host needs four packages for `dev`. `doctor`
prints the command for each one that is missing.

| Package | For |
|---|---|
| `debian-archive-keyring` | mkosi checks its tools tree against it, and every other build tool (`systemd-repart`, `mkfs.*`, `ukify`, `systemd-boot`) comes from that tree |
| `golang` (1.21 or newer) | fetches the toolchain and the modules of each Go source, including the one of the evidence service |
| `qemu-system-x86`, `ovmf` | boot the dev build |

A Cargo source that pins a Rust toolchain needs `rustup` too. mkosi builds in an unprivileged
user namespace, or under `sudo` where the host withholds them. Ubuntu 24.04 withholds them by
default through AppArmor, and `doctor` prints the sysctl that changes this.
