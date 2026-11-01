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

tundravm is mkosi made opinionated for confidential VMs: Debian, systemd and Intel TDX,
declared as one Python `Image`. It compiles the image to a plain mkosi 27.1 tree, builds it
into an immutable directory with a signed manifest, and runs, deploys and attests the result.

## Install

```console
$ uvx --from git+https://github.com/Hyodar/tundra tundravm new my-image
$ cd my-image
$ uv sync
```

tundravm is not on PyPI: `new` pins the commit it ran from, and mkosi comes with it.

## Quickstart

```python
from tundravm import File, Image, Service
from tundravm.ports import TCP

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
```

```console
$ uv run tundravm build --dev                   # lock, payload.efi, qemu.raw, measurements, manifest
$ uv run tundravm run --mode simulated --dev    # instance my-image-1, ports forwarded to 127.0.0.1
$ uv run tundravm verify latest-development     # every artifact against the manifest, offline
```

That file is the whole image. Everything else is implied: Debian trixie from a locked
snapshot; the TEE baseline (NTS time, a volatile journal, hardened sysctls, no apt timers);
a measured UKI with the TDX, virtio, NVMe, Hyper-V and gVNIC drivers; an nftables firewall
that opens only `8080` and the management port `8745`; `app.service` as its own system user
under a tested sandbox, restarted on failure; and the `tdxs` evidence service, whose quote
`attest` checks. Each default has a typed escape hatch.

The three commands build, boot and verify the `dev` profile. A production build is the
same image without `DevAccess`, and it starts no service unless it runs under TDX. `new`
writes a starter like this one, with a Go program in `app/` in place of busybox.

## Documentation

- [The guide](docs/guide.md): projects, images, services, components, profiles, sources,
  the lock, testing, builds, run, deploy and attest, converting, CI.
- [Trust, limits and recovery](docs/trust.md): what the image defends against, what is not
  built yet, and how to recover an instance.
- Reference, generated from the code: [commands](docs/reference/cli.md),
  [API](docs/reference/api.md) and [check codes](docs/reference/lint.md).

## Development

```console
$ uv sync
$ uv run ruff check . && uv run ruff format --check src tests examples
$ uv run mypy .
$ uv run pytest -q -m "not integration"
$ uv run tundravm check examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi
```

After a docstring or parser change, `uv run python -m tundravm._reference docs/reference`
regenerates the reference.
