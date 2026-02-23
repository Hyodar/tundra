<p align="center">
  <img src="docs/logo.svg" alt="tdxvm-sdk" width="600"/>
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

img.emit_mkosi("build/mkosi")        # inspect the generated mkosi tree
img.lock()                            # write build/tdx.lock
result = img.bake(frozen=True)        # build with lockfile enforcement
```

## Features

| | |
|---|---|
| **Declarative recipes** | Packages, files, templates, users, services, secrets, partitions |
| **Deterministic emission** | Generates buildable mkosi v26 project trees matching [nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) |
| **Profile system** | Per-profile packages, output targets, and overrides with context managers |
| **Systemd debloat** | `mkosi-chroot dpkg-query` based binary/unit cleanup with configurable whitelists |
| **Cloud targets** | Auto-generated GCP (ESP+GPT tar.gz) and Azure (VHD) postoutput scripts |
| **Custom init** | Built-in TDX init script (mount + pivot_root + `minimal.target`) |
| **Lockfile + policy** | Frozen bakes, mutable-ref enforcement, integrity checks |
| **Measurement** | RTMR / Azure / GCP attestation measurement interfaces |
| **Modules** | Composable `Init` and `Tdxs` modules via `module.apply(img)` |
| **Backends** | `local_linux` (direct mkosi), `lima` (macOS VM), `inprocess` (testing) |

## mkosi Alignment

The emission pipeline generates configs that match the nethermind-tdx reference:

- `Architecture=x86-64` / `arm64` mapped from Python arch types
- `mkosi-chroot useradd` and `mkosi-chroot systemctl enable` (not raw shell)
- `mkosi-chroot dpkg-query -L systemd` for binary cleanup and unit masking
- `default.target` symlinked to `minimal.target`
- `ManifestFormat=json`, `CleanPackageMetadata=yes`, `WithNetwork=yes|no`
- Git-based `mkosi.version` script (`YYYY-MM-DD.hash[-dirty]`)
- Native profiles mode: root `mkosi.conf` + `mkosi.profiles/<name>/`

```python
img = Image(
    base="debian/bookworm",
    init_script=Image.DEFAULT_TDX_INIT,
    generate_version_script=True,
)
img.install("systemd", "kmod")
img.debloat(enabled=True)
img.output_targets("qemu", "gcp", "azure")
img.emit_mkosi("build/mkosi")
# build/mkosi/mkosi.version
# build/mkosi/default/mkosi.conf
# build/mkosi/default/mkosi.skeleton/init
# build/mkosi/default/scripts/gcp-postoutput.sh
# build/mkosi/default/scripts/azure-postoutput.sh
```

## Multi-Profile Builds

```python
from tdx import Image

img = Image()
img.output_targets("qemu")

with img.profile("azure"):
    img.output_targets("azure")
    img.install("waagent")

with img.profile("gcp"):
    img.output_targets("gcp")
    img.install("google-guest-agent")

with img.all_profiles():
    results = img.bake()
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
Built-in modules          src/tdx/modules/ (Init, Tdxs)
```

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
