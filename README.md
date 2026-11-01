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
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
$ tundravm new my-image
$ cd my-image
$ uv sync
```

tundravm is not on PyPI: the tag pins the release, `new` pins its commit, and mkosi
comes with it ([releases](docs/release.md)).

## Quickstart

`new` writes stage 1 of the one example, the attested node in
[`examples/attested-node/`](examples/attested-node/):

<!-- listing: examples/attested-node/stages/01-boot.py -->
```python
"""attested-node: the boot, stage 1 of the guide.

Debian trixie from one snapshot, with systemd, the TEE baseline (NTS time, a volatile
journal, sysctl hardening), DHCP, a firewall that drops inbound traffic nothing exposes,
filtered egress, and a dev profile with a serial console and root password "tdx".
"""

from tundravm import Debian, Image
from tundravm.components import DevAccess, Egress, Network

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    network=Network(egress=Egress()),
    dev=DevAccess(password="tdx"),
)
```

```console
$ uv run tundravm dev                           # lock, build the dev profile, boot it, probe it, follow it
$ uv run tundravm verify latest-development     # every artifact against the manifest, offline
```

That file is the whole image. Everything else is implied: the TEE baseline (NTS time, a
volatile journal, hardened sysctls, no apt timers); a measured UKI with the TDX, virtio,
NVMe, Hyper-V and gVNIC drivers; an nftables firewall that opens nothing; and the `tdxs`
evidence service, whose quote `attest` checks. Each default has a typed escape hatch.

The two commands build, boot, probe and verify the `dev` profile. A production build is
the same image without `DevAccess`, and it starts no service unless it runs under TDX.
[The guide](docs/guide.md#the-attested-node-stage-by-stage) builds the node up from there
in six stages under `examples/attested-node/stages/`: Nethermind as a component of your
own, Lighthouse, a signing proxy built from Go source, attested TLS, and operating it.
The last stage is `examples/attested-node/image.py`.

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
$ uv run tundravm check examples/attested-node/image.py --tree examples/attested-node/mkosi
```

After a docstring or parser change, `uv run python -m tundravm._reference docs/reference`
regenerates the reference.
