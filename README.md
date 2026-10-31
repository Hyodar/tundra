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

tundravm is not on PyPI. `new` pins the commit it ran from in `pyproject.toml`, and mkosi
comes with it.

## Quickstart

```python
from tundravm import TCP, File, Image, Service

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
$ tundravm new demo && cd demo
$ tundravm build                  # lock, payload.efi, qemu.raw, measurements, manifest
$ tundravm run --mode simulated   # instance demo-1, ports forwarded to 127.0.0.1
$ tundravm verify latest          # every artifact against the manifest, offline
```

That file is the whole image; `new` writes the same page served by a small `App` component,
plus a `dev` profile. Implied with no further words: Debian trixie from a locked
snapshot; the TEE baseline (NTS time, a volatile journal, sysctl hardening, no apt timers);
a measured UKI with the TDX, virtio, NVMe, Hyper-V and gVNIC drivers; an nftables firewall
that opens only `8080` and the management port `8745`; `app.service` as its own system
user under a tested sandbox, restarting on failure; and the `tdxs` evidence service that
produces the quote an attestation checks. Each default has one typed escape hatch.

## Documentation

- [The guide](docs/guide.md): projects, images, services, components, profiles, volumes
  and secrets, the mkosi tree, the lock, builds, run, deploy, attest, testing.
- [Trust, limits and recovery](docs/trust.md): what the image defends against, what is not
  built yet, and how to recover an instance.
- Reference, generated from the code: [commands](docs/reference/cli.md) and
  [API](docs/reference/api.md).

## Development

```console
$ uv sync
$ uv run ruff check . && uv run ruff format --check src tests examples
$ uv run mypy .
$ uv run pytest -q -m "not integration"
$ uv run tundravm check examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi
```

After a change to a docstring or the parser, `uv run python -m tundravm._reference docs/reference`
regenerates the reference.
