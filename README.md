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

Python SDK for declaratively building, measuring, and deploying TDX-enabled VM images.
Library-first (no CLI required), designed for reproducible recipes, profile-aware orchestration, and policy-driven CI.

## Quickstart

```bash
uv sync
```

```python
from tdx import Image

img = Image(base="debian/bookworm", arch="x86_64", backend="local_linux")
img.install("systemd", "curl", "jq")
img.file("/etc/motd", content="TDX node\n")
img.user("app", system=True, shell="/bin/false")
img.service("app", exec="/usr/bin/app", enabled=True)
img.debloat(enabled=True)
img.output_targets("qemu")

img.compile("build/mkosi")           # compile recipe into mkosi project tree
img.lock()                            # write build/tdx.lock
result = img.bake(frozen=True)        # build with lockfile enforcement
```

## Features

| | |
|---|---|
| **Declarative recipes** | Packages, build packages, files, templates, skeletons, users, services, secrets, partitions |
| **Deterministic emission** | Generates buildable mkosi v26 project trees matching [nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) |
| **Profile system** | Per-profile packages, output targets, and overrides with context managers |
| **Platform profiles** | Class-based Azure and GCP platform profiles via `AzurePlatform().apply(img)`, `GcpPlatform().apply(img)` |
| **Reproducibility hooks** | EFI stub pinning (`efi_stub`), backports source generation (`backports`), image version stripping (`strip_image_version`) |
| **Systemd debloat** | `mkosi-chroot dpkg-query` based binary/unit cleanup with configurable whitelists |
| **Cloud targets** | Auto-generated GCP (ESP+GPT tar.gz) and Azure (VHD) postoutput scripts |
| **Custom init** | Built-in TDX init script (mount + pivot_root + `minimal.target`) |
| **Lockfile + policy** | Frozen bakes, mutable-ref enforcement, integrity checks |
| **Measurement** | RTMR / Azure / GCP attestation measurement interfaces |
| **Modules** | Composable modules via `module.apply(img)` — `Init`, `KeyGeneration`, `DiskEncryption`, `SecretDelivery`, `Tdxs`, `Devtools` |
| **Phase hooks** | `prepare`, `postinst`, `finalize`, `postoutput`, `on_boot`, `sync` convenience methods |
| **Backends** | `local_linux` (direct mkosi), `lima` (macOS VM), `inprocess` (testing) |

## mkosi Alignment

The emission pipeline generates configs that match the nethermind-tdx reference:

- `Architecture=x86-64` / `arm64` mapped from Python arch types
- `mkosi-chroot useradd` and `mkosi-chroot systemctl enable` (not raw shell)
- `mkosi-chroot dpkg-query -L systemd` for binary cleanup and unit masking
- `default.target` symlinked to `minimal.target`
- `ManifestFormat=json`, `CleanPackageMetadata=true`, `WithNetwork=true|false`
- Git-based `mkosi.version` script (`YYYY-MM-DD.hash[-dirty]`)
- Native profiles mode: root `mkosi.conf` + `mkosi.profiles/<name>/`
- EFI stub pinning from Debian snapshot archives for reproducible boot
- Dynamic backports source generation via `mkosi.prepare` hooks
- Environment variable passthrough (`Environment=`, `EnvironmentFiles=`)

```python
from tdx import Image, Kernel

img = Image(
    base="debian/trixie",
    reproducible=True,
    init_script=Image.DEFAULT_TDX_INIT,
    environment_passthrough=("KERNEL_IMAGE", "KERNEL_VERSION"),
)
img.kernel = Kernel.tdx_kernel("6.13.12", cmdline="console=tty0", config_file="kernel/config")
img.efi_stub(snapshot_url="https://snapshot.debian.org/archive/debian/...", package_version="255.4-1")
img.backports()
img.install("systemd", "kmod")
img.build_install("build-essential", "git")
img.debloat(enabled=True)
img.output_targets("qemu", "gcp", "azure")
img.compile("build/mkosi")
```

## Multi-Profile Builds

```python
from tdx import Image
from tdx.modules import Devtools
from tdx.platforms import AzurePlatform, GcpPlatform

img = Image()
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

## Composable Init Modules

Modules register boot-time steps via `image.add_init_script()` with priority ordering.
`Init` collects them and generates `/usr/bin/runtime-init` + a systemd service.

```python
from tdx import Image
from tdx.modules import DiskEncryption, Init, KeyGeneration, SecretDelivery

img = Image(base="debian/trixie", reproducible=True)

# Each module builds a Go binary and registers its invocation on the image
KeyGeneration(strategy="tpm").apply(img)          # priority 10
DiskEncryption(device="/dev/vda3").apply(img)      # priority 20
SecretDelivery(method="http_post").apply(img)      # priority 30

# Init reads registered init_scripts, sorts by priority, emits runtime-init
Init().apply(img)
```

## Secrets

```python
from tdx.models import SecretSchema, SecretTarget
from tdx.modules import Init

secret = img.secret(
    "api_token",
    required=True,
    schema=SecretSchema(kind="string", min_length=8, pattern="^tok_"),
    targets=(
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    ),
)
init = Init(secrets=(secret,))
delivery = init.secrets_delivery("http_post", completion="all_required")
validation = delivery.validate_payload({"api_token": "tok_123456"})
```

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

## Dependencies

| | |
|---|---|
| **Runtime** | Python >= 3.12, `cbor2` |
| **Dev** | `ruff`, `mypy`, `pytest` (via `uv sync`) |
| **mkosi** | >= 25 required, v26 recommended |
| **Lima** | `limactl` for macOS builds |

Install mkosi v26:

```bash
pip install --break-system-packages 'mkosi @ git+https://github.com/systemd/mkosi.git@v26'
```

## Architecture

```
Image recipe API          src/tdx/image.py
  |
  v
Typed models              src/tdx/models.py
  |
  v
Compiler / emission       src/tdx/compiler/
  |
  +-> mkosi.conf, phase scripts, skeleton, extra trees
  |
  v
Execution backends        src/tdx/backends/
  |                         local_linux  |  lima  |  inprocess
  v
Lock / cache / policy     src/tdx/lockfile.py, cache.py, policy.py
  |
  v
Measure + deploy          src/tdx/measure.py, deploy.py
  |
  v
Core modules              src/tdx/modules/ (Init, KeyGeneration, DiskEncryption, SecretDelivery, Tdxs, Devtools)
  |
  v
Example modules           examples/modules/ (Nethermind, Raiko, TaikoClient)
  |
  v
Platform profiles         src/tdx/platforms/ (AzurePlatform, GcpPlatform)
```

## Examples

| Example | Description |
|---|---|
| [`nethermind_tdx.py`](examples/nethermind_tdx.py) | Base layer for [NethermindEth/nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) — TDX kernel build, EFI stub pinning, backports, full debloat, skeleton files, Tdxs module |
| [`surge_tdx_prover.py`](examples/surge_tdx_prover.py) | Complete nethermind-tdx image — composes the base layer with Init + composable modules (KeyGeneration, DiskEncryption, SecretDelivery), Raiko, TaikoClient, Nethermind, Devtools modules, and Azure/GCP platform profiles |
| [`full_api.py`](examples/full_api.py) | End-to-end: kernel, repos, secrets, composable init modules (KeyGeneration, DiskEncryption, SecretDelivery) + Init + Tdxs, multi-profile cloud deploys |
| [`multi_profile_cloud.py`](examples/multi_profile_cloud.py) | Per-profile Azure / GCP / QEMU output targets |
| [`tdxs_module.py`](examples/tdxs_module.py) | Minimal Tdxs quote service integration |
| [`strict_secrets.py`](examples/strict_secrets.py) | Secret schemas with pattern validation and delivery |
| [`qemu_basic.py`](examples/qemu_basic.py) | Minimal QEMU-only recipe |

The `nethermind_tdx` + `surge_tdx_prover` pair reproduces the full upstream [nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) repo. Integration tests verify SDK output matches the upstream reference across all directories (`integration_tests/`).

## Troubleshooting

| Error | Fix |
|---|---|
| `E_LOCKFILE` | Run `img.lock()` with current recipe, then `bake(frozen=True)` |
| `E_POLICY` | Update policy config or invocation mode |
| `E_DEPLOYMENT` | Ensure `output_targets(...)` includes target, rerun `bake()` |
| `E_MEASUREMENT` | Run `bake()` before `measure(...)` |
| `E_BACKEND_EXECUTION` | Check mkosi version (`>= 25`), platform, and tool availability |

## Development

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```
