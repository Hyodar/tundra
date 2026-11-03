<p align="center">
  <img src="docs/logo.svg" alt="tundravm" width="700"/>
</p>

<p align="center">
  <a href="https://github.com/Hyodar/tundravm/actions/workflows/ci.yml"><img src="https://github.com/Hyodar/tundravm/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/systemd/mkosi"><img src="https://img.shields.io/badge/mkosi-v27.1-8b5cf6.svg" alt="mkosi v27.1"/></a>
</p>

<br>

tundravm builds confidential VM images without making you learn mkosi. You declare the OS
as one Python `Image`. tundravm writes a plain mkosi tree from it and builds Debian with
systemd. The image boots as an Intel TDX VM, whose memory the host cannot read and whose
contents a remote party can verify. tundravm is for people who know Linux and Docker and
need a VM others can check.

## First run

```console
$ sudo apt install debian-archive-keyring golang qemu-system-x86 ovmf
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
$ tundravm doctor
$ tundravm new my-image && cd my-image
$ uv run tundravm dev
```

The packages are for Debian 13 or Ubuntu 24.04. `doctor` names the fix for anything
missing. `new` writes a project whose image is stage 1 of the [guide](docs/guide.md).
`dev` builds it, boots it under QEMU without TDX and prints its boot probe.

## Next

1. [Terms](docs/terms.md): the ideas, in the order the guide needs them.
2. [The guide](docs/guide.md): one image built up in six stages.
3. [How do I](docs/howto.md): lock, build, deploy, attest, deliver a secret, convert.
4. [The manual](docs/manual.md): every slot, rule and file.
5. [Trust, limits and recovery](docs/trust.md): what the image defends against.
6. Generated from the code: [commands](docs/reference/cli.md), [API](docs/reference/api.md)
   and [check codes](docs/reference/lint.md). [Releasing](docs/release.md) says what a tag
   pins.

## Development

```console
$ uv sync
$ uv run ruff check . && uv run mypy . && uv run pytest -q -m "not integration"
$ uv run python -m tundravm._reference docs/reference
```

The last command regenerates the reference after a docstring or parser change.
