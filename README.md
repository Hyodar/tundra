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

tundravm builds confidential VM images without making you learn mkosi. You describe the
operating system as one Python `Image` value. tundravm turns it into a plain mkosi
directory and builds a Debian image with systemd. The image boots as an Intel TDX VM. The
host cannot read the memory of such a VM, and a remote party can verify what it runs.
tundravm is for people who know Linux and Docker and need a VM that others can check.

## First run

```console
$ sudo apt install debian-archive-keyring golang qemu-system-x86 ovmf
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
$ tundravm doctor
$ tundravm new my-image && cd my-image
$ uv run tundravm dev
```

The packages are for Debian 13 or Ubuntu 24.04. `doctor` tells you how to fix anything
that is missing. `new` writes a project whose image is stage 1 of the [guide](docs/guide.md).
`dev` builds that image, boots it under QEMU without TDX and prints its boot probe, a short
checklist the image runs on itself.

## Next

1. [Terms](docs/terms.md) explains the ideas in the order the guide needs them.
2. [The guide](docs/guide.md) builds one image up in six stages.
3. [How do I](docs/howto.md) covers the tasks around that story, from a lock to a
   maintenance window.
4. [The manual](docs/manual.md) describes every slot, rule and file.
5. [Trust, limits and recovery](docs/trust.md) says what the image defends against.
6. The [commands](docs/reference/cli.md), the [API](docs/reference/api.md) and the
   [check codes](docs/reference/lint.md) are generated from the code. [Releasing](docs/release.md)
   says what a tag pins.

[`examples/flashbox`](examples/flashbox/) ports the searcher sandbox of flashbots-images to
one image.

## Development

```console
$ uv sync
$ uv run ruff check . && uv run mypy . && uv run pytest -q -m "not integration"
$ uv run python -m tundravm._reference docs/reference
```

Run the last command after you change a docstring or the parser, to regenerate the reference.
