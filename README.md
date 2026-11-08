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
OS as one Python `Image`, and tundravm turns it into a Debian image that boots as an Intel
TDX VM, whose memory the host cannot read and whose contents anyone can verify.

## First run

```console
$ sudo apt install debian-archive-keyring golang qemu-system-x86 ovmf
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
$ tundravm doctor
$ tundravm new my-image && cd my-image
$ uv run tundravm dev
```

On Debian 13 or Ubuntu 24.04. `dev` builds the new image and boots it under QEMU.

## Next

- [Terms](docs/terms.md): the ideas, in the order you meet them.
- [The guide](docs/guide.md): one image, built in six stages.
- [How do I](docs/howto.md): the tasks around it.
- [The manual](docs/manual.md), with the generated [command](docs/reference/cli.md),
  [API](docs/reference/api.md) and [check code](docs/reference/lint.md) references.
- [Trust, limits and recovery](docs/trust.md): what the image defends against.

[`examples/flashbox`](examples/flashbox/) ports flashbots-images' searcher sandbox to one
image.

## Development

```console
$ uv sync
$ uv run ruff check . && uv run mypy . && uv run pytest -q -m "not integration"
```
