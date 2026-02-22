# tdxvm-sdk

[![CI](https://github.com/flashbots/tdxvm/actions/workflows/ci.yml/badge.svg)](https://github.com/flashbots/tdxvm/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Python SDK for declaratively building, measuring, and deploying TDX-enabled VM images.

`tdxvm-sdk` is a library-first workflow (no required CLI) designed for:
- reproducible image recipes,
- profile-aware build/deploy orchestration,
- measurable attestation outputs,
- policy-driven CI strictness.

## ✅ What Exists Today

- Declarative image recipe API with deterministic profile-scoped state.
- Lockfile, frozen bake enforcement, policy validation, and integrity-aware fetch.
- Deterministic mkosi tree emission (`mkosi.conf` + phase scripts).
- Built-in `Init` and `Tdxs` modules with `img.use(...)` wiring.
- Secret contract declaration + runtime delivery validation/materialization.
- Typed measure/deploy interfaces and structured build reports/logs.

## Architecture Overview

Core layers:
1. `Image` recipe API (`src/tdx/image.py`)
2. typed models (`src/tdx/models.py`)
3. compiler/emission (`src/tdx/compiler/`)
4. execution backends (`src/tdx/backends/`)
5. lock/cache/fetch/policy subsystems
6. measurement + deploy adapters
7. built-in modules (`Init`, `Tdxs`)

## 📦 Dependencies

- Runtime:
  - Python `>=3.12`
  - `cbor2`
- Dev/test:
  - `ruff`, `mypy`, `pytest` (installed by `uv sync --dev`)
- Host tools (operation-dependent):
  - `mkosi` for `local_linux` backend preflight checks.
  - `limactl` for `lima` backend preflight checks.
- Module host prerequisites:
  - Modules can declare `required_host_commands()`.
  - `img.use(...)` validates those commands and raises `E_VALIDATION` if missing.

```python
from tdx import Image


class SignedArtifactModule:
    def required_host_commands(self) -> tuple[str, ...]:
        return ("cosign",)

    def setup(self, image: Image) -> None:
        image.install("ca-certificates")

    def install(self, image: Image) -> None:
        image.file("/etc/example/signed.conf", content="enabled=true\n")


img = Image()
img.use(SignedArtifactModule())  # raises ValidationError when `cosign` is missing
```

## ⚠️ Simulated vs Real

- Real today:
  - Recipe declaration and deterministic emission.
  - Lock/policy/fetch/cache validation logic.
  - Secret schema and runtime delivery validation logic.
- Simulated today:
  - `Image.bake()` currently creates deterministic placeholder artifacts and reports.
  - Deploy adapters return typed synthetic deployment metadata (no cloud API calls yet).
  - Measurement backends derive deterministic digest sets from artifact bytes (not hardware RTMR/PCR extraction).
  - Backend executors (`lima`, `local_linux`) exist with prereq checks, but are not yet wired into `Image.bake()`.

## Quickstart

### 1. Install and sync

```bash
uv sync
```

### 2. Author a recipe

```python
from tdx import Image

img = Image(base="debian/bookworm", arch="x86_64")
img.install("curl", "jq")
img.file("/etc/motd", content="TDX node\n")
img.run("echo", "preparing", phase="prepare")
img.output_targets("qemu")
```

### 3. Lock + bake + measure + deploy

```python
img.lock()                      # writes build/tdx.lock
bake_result = img.bake(frozen=True)

measurements = img.measure(backend="rtmr")
print(measurements.to_json())

deploy_result = img.deploy(target="qemu")
print(deploy_result.deployment_id, deploy_result.endpoint)
```

## Multi-Profile and Output Targets

```python
from tdx import Image

img = Image()
img.output_targets("qemu")  # default profile

with img.profile("azure"):
    img.output_targets("azure")
    img.install("waagent")

with img.profile("gcp"):
    img.output_targets("gcp")
    img.install("google-guest-agent")

with img.all_profiles():
    results = img.bake()
```

## Secrets Validation Workflow

```python
from tdx import Image
from tdx.models import SecretSchema, SecretTarget
from tdx.modules import Init

img = Image()
secret_spec = img.secret(
    "api_token",
    required=True,
    schema=SecretSchema(kind="string", min_length=8, pattern="^tok_"),
    targets=(
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    ),
)
init = Init(secrets=(secret_spec,))
delivery = init.secrets_delivery("http_post", completion="all_required", reject_unknown=True)

validation = delivery.validate_payload({"api_token": "tok_123456"})
runtime = delivery.materialize_runtime("runtime")
print(validation.ready, runtime.global_env_path)
```

## Core Modules and Full API Shape

```python
from tdx import Image
from tdx.models import SecretSchema, SecretTarget
from tdx.modules import Init, Tdxs

img = Image(base="debian/bookworm", arch="x86_64")
img.install("ca-certificates", "curl")
img.output_targets("qemu", "azure", "gcp")
img.debloat(enabled=True)

secret = img.secret(
    "jwt_secret",
    schema=SecretSchema(kind="string", min_length=64, max_length=64),
    targets=(
        SecretTarget.file("/run/tdx-secrets/jwt.hex"),
        SecretTarget.env("JWT_SECRET", scope="global"),
    ),
)

init = Init(secrets=(secret,), handoff="systemd")
init.secrets_delivery("http_post")
tdxs = Tdxs.issuer()

img.use(init, tdxs)
img.run("usermod", "-aG", "tdxs", "root", phase="postinst")

img.lock()
img.bake(frozen=True)
print(img.measure(backend="rtmr").to_json())
print(img.deploy(target="qemu").deployment_id)
```

## Reproducibility and Locking

- Generate lock state: `img.lock()`
- Enforce lock in CI: `img.bake(frozen=True)`
- Reproducibility tests live in `tests/test_reproducibility.py`
- See `docs/reproducibility.md` for strict flow guidance.

## Policy Modes

```python
from tdx.policy import Policy

policy = Policy(
    require_frozen_lock=True,
    mutable_ref_policy="error",
    require_integrity=True,
    network_mode="online",
)

img.set_policy(policy)
```

Detailed policy reference: `docs/policy.md`.

## Troubleshooting

- `E_LOCKFILE`: stale/missing lock in frozen mode.
  - fix: run `img.lock()` with current recipe and rerun `bake(frozen=True)`.
- `E_POLICY`: policy blocked operation (offline network, mutable ref escalation, non-frozen bake).
  - fix: update policy config or invocation mode.
- `E_DEPLOYMENT`: target artifact missing for deploy.
  - fix: ensure `output_targets(...)` includes target and rerun `bake()`.
- `E_MEASUREMENT`: measurement requested without baked artifacts.
  - fix: run `bake()` before `measure(...)`.

## Development and Contribution

Local quality gates:

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```

CI runs the same commands via `.github/workflows/ci.yml`.
