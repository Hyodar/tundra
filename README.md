<p align="center">
  <img src="docs/logo.svg" alt="tdxvm-sdk" width="420"/>
</p>

<p align="center">
  <a href="https://github.com/Hyodar/tdxvm/actions/workflows/ci.yml"><img src="https://github.com/Hyodar/tdxvm/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/systemd/mkosi"><img src="https://img.shields.io/badge/mkosi-v26-8b5cf6.svg" alt="mkosi v26"/></a>
</p>

---

Python SDK for declaratively building, measuring, and deploying TDX-enabled VM images. Define your image as code, compile it to a reproducible [mkosi](https://github.com/systemd/mkosi) project tree, and bake it into a bootable disk for QEMU, Azure, or GCP.

## Quickstart

```python
from tdx import Image
from tdx.backends import LimaMkosiBackend

img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
img.install("systemd", "curl", "jq")
img.file("/etc/motd", content="TDX node\n")
img.user("app", system=True, shell="/bin/false")
img.service("app", exec="/usr/bin/app")
img.debloat(enabled=True)
img.output_targets("qemu")

img.compile("build/mkosi")           # emit mkosi project tree
img.lock()                            # write build/tdx.lock
result = img.bake(frozen=True)        # build with lockfile enforcement
```

`compile()` produces a standard mkosi directory you can inspect, diff, or build manually with `mkosi build`. `bake()` runs the full pipeline.

## Why

Hand-maintained mkosi trees for TDX images are hard to review, easy to drift, and painful to keep reproducible across cloud targets. This SDK lets you express the same image as a short Python script and get:

- **Deterministic output** — the same recipe always produces the same mkosi tree, byte-for-byte
- **Multi-cloud from one definition** — Azure VHD, GCP tar.gz, and QEMU qcow2 from a single `Image`
- **Composable modules** — drop in `KeyGeneration`, `DiskEncryption`, `SecretDelivery` and they wire themselves into the boot sequence
- **Lockfile + policy** — frozen bakes, mutable-ref enforcement, integrity checks for CI

The [`surge-tdx-prover`](examples/surge-tdx-prover/) example reproduces the full [NethermindEth/nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) repository from ~250 lines of Python. Integration tests verify the SDK output matches the upstream tree.

## Backends

The SDK provides three build backends:

| Backend | When to use |
|---|---|
| `LimaMkosiBackend` | Default. Runs mkosi inside a Lima VM with Nix. Works on macOS and Linux. |
| `NixMkosiBackend` | Native Linux with [Nix](https://nixos.org/download.html) installed. Runs mkosi via `nix develop` directly on the host. |
| `LocalLinuxBackend` | Direct `mkosi` invocation on Linux with `sudo` or `unshare`. No Nix required. |

```python
from tdx.backends import LimaMkosiBackend, NixMkosiBackend, LocalLinuxBackend

# Lima (recommended — reproducible, cross-platform)
Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))

# Native Nix (Linux only, faster — no VM overhead)
Image(backend=NixMkosiBackend())

# Direct mkosi (Linux only, requires mkosi in PATH)
Image(backend=LocalLinuxBackend())
```

## Profiles

Profiles let you customize packages, services, and output targets per deployment environment. Anything inside a `with img.profile(...)` block only applies to that profile.

```python
from tdx import Image
from tdx.backends import LimaMkosiBackend
from tdx.modules import Devtools
from tdx.platforms import AzurePlatform, GcpPlatform

img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
img.output_targets("qemu")

with img.profile("azure"):
    AzurePlatform().apply(img)

with img.profile("gcp"):
    GcpPlatform().apply(img)

with img.profile("devtools"):
    Devtools().apply(img)

with img.all_profiles():
    results = img.bake()
```

## Modules

Modules are composable units that add build steps, config files, systemd services, and init scripts to an image. Call `module.apply(img)` and the module handles the rest.

**Built-in modules** (`tdx.modules`):

| Module | What it does |
|---|---|
| `KeyGeneration` | TPM-based key derivation at boot |
| `DiskEncryption` | LUKS2 disk encryption with key from KeyGeneration |
| `SecretDelivery` | Runtime secret injection (HTTP POST, file, env) |
| `Tdxs` | TDX quote issuer/validator service (socket-activated) |
| `Devtools` | Serial console, root password, SSH for dev profiles |

**Init ordering** — modules register boot-time scripts with priority. At `compile()`, the SDK generates `/usr/bin/runtime-init` and a systemd service that runs them in order, then injects `After=runtime-init.service` into all other services automatically.

```python
from tdx import Image
from tdx.modules import DiskEncryption, KeyGeneration, SecretDelivery

img = Image(base="debian/trixie", reproducible=True)

KeyGeneration(strategy="tpm").apply(img)          # priority 10
DiskEncryption(device="/dev/vda3").apply(img)      # priority 20
SecretDelivery(method="http_post").apply(img)      # priority 30

img.compile("build/mkosi")
```

See [`docs/module-authoring.md`](docs/module-authoring.md) for writing your own modules.

## Secrets

```python
from tdx import SecretSchema, SecretTarget
from tdx.modules import SecretDelivery

delivery = SecretDelivery(method="http_post")
delivery.secret(
    "api_token",
    required=True,
    schema=SecretSchema(kind="string", min_length=8, pattern="^tok_"),
    targets=(
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    ),
)
delivery.apply(img)
```

## Reproducibility

The SDK has first-class support for reproducible builds:

- **Debian snapshot mirrors** — pin `img.mirror` to a snapshot URL so package resolution is deterministic
- **EFI stub pinning** — `img.efi_stub()` fetches a specific systemd-boot-efi version from Debian snapshots
- **Lockfiles** — `img.lock()` captures the recipe digest; `bake(frozen=True)` refuses to build if the recipe changed
- **Systemd debloat** — strips unused binaries and units via `dpkg-query`, masks the rest, replaces `default.target` with a minimal target
- **IMAGE_VERSION stripping** — removes the non-deterministic version string from os-release

See [`docs/reproducibility.md`](docs/reproducibility.md) for details.

## Policy

```python
from tdx.policy import Policy

img.set_policy(Policy(
    require_frozen_lock=True,
    mutable_ref_policy="error",
    require_integrity=True,
    network_mode="online",
))
```

See [`docs/policy.md`](docs/policy.md) for the full reference.

## Examples

| Example | Description |
|---|---|
| [`surge-tdx-prover/`](examples/surge-tdx-prover/) | Full [nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) image with all modules and Azure/GCP/devtools profiles |
| [`nethermind_tdx.py`](examples/nethermind_tdx.py) | Base layer: TDX kernel, EFI stub pinning, backports, debloat, Tdxs |
| [`full_api.py`](examples/full_api.py) | End-to-end: kernel, secrets, init modules, multi-profile cloud deploys |
| [`multi_profile_cloud.py`](examples/multi_profile_cloud.py) | Per-profile Azure / GCP / QEMU output targets |
| [`qemu_basic.py`](examples/qemu_basic.py) | Minimal QEMU-only image |

Run the surge-tdx-prover example:

```bash
python -m examples.surge-tdx-prover compile    # emit mkosi tree to examples/surge-tdx-prover/mkosi/
python -m examples.surge-tdx-prover bake        # compile + lock + build
```

## Setup

**Lima backend** (recommended): Install [Lima](https://lima-vm.io/docs/installation/), then `uv sync`. Lima runs mkosi inside a Linux VM with Nix — this ensures a consistent environment for reproducible builds.

**Nix backend**: Install [Nix](https://nixos.org/download.html) with flakes enabled, then `uv sync`. Runs mkosi directly on your Linux host via `nix develop`.

**Local backend**: Install [mkosi](https://github.com/systemd/mkosi) (v25+) on Linux, then `uv sync`.

## Development

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```

## Troubleshooting

| Error | Fix |
|---|---|
| `E_LOCKFILE` | Run `img.lock()` with current recipe, then `bake(frozen=True)` |
| `E_POLICY` | Update policy config or invocation mode |
| `E_DEPLOYMENT` | Ensure `output_targets(...)` includes target, rerun `bake()` |
| `E_MEASUREMENT` | Run `bake()` before `measure(...)` |
| `E_BACKEND_EXECUTION` | Check mkosi version (`>= 25`), platform, and tool availability |
