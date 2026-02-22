# tdxvm-sdk

Python SDK for declaratively building, measuring, and deploying TDX-enabled VM images.

`tdxvm-sdk` is a library-first workflow (no required CLI) designed for:
- reproducible image recipes,
- profile-aware build/deploy orchestration,
- measurable attestation outputs,
- policy-driven CI strictness.

## Feature Matrix

| Capability | Status | Notes |
| --- | --- | --- |
| Declarative recipe model | Implemented | Side effects only in explicit output operations (`lock`, `emit_mkosi`, `bake`). |
| mkosi emission pipeline | Implemented | Deterministic per-profile conf/script generation with phase ordering validation. |
| Output target conversion | Implemented | `qemu`, `azure`, `gcp` conversion in `bake`. |
| Build backends | Implemented | `lima` and `local_linux` backend contracts + prerequisite checks. |
| Integrity fetch | Implemented | hash-checked fetch + git ref/tree verification with mutable-ref policy control. |
| Lock + frozen mode | Implemented | lockfile digest validation and frozen enforcement. |
| Content-addressed cache | Implemented | canonical cache keys + manifest verification. |
| Measurement backends | Implemented | `rtmr`, `azure`, `gcp` derivation + JSON/CBOR export + verify API. |
| Deploy adapters | Implemented | typed adapters for `qemu`, `azure`, `gcp`. |
| Secrets workflow | Implemented | required/schema targets + HTTP delivery validation + runtime materialization. |
| Policy engine | Implemented | frozen lock, mutable refs, integrity, network mode. |
| Observability | Implemented | structured logs + schemaed build reports. |

## Architecture Overview

Core layers:
1. `Image` recipe API (`src/tdx/image.py`)
2. typed models (`src/tdx/models.py`)
3. compiler/emission (`src/tdx/compiler/`)
4. execution backends (`src/tdx/backends/`)
5. lock/cache/fetch/policy subsystems
6. measurement + deploy adapters
7. built-in modules (`Init`, `Tdxs`)

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
img.run("prepare", "echo", "preparing")
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
img.secret(
    "api_token",
    required=True,
    schema=SecretSchema(kind="string", min_length=8, pattern="^tok_"),
    targets=(
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    ),
)

secret_spec = img.state.profiles["default"].secrets[0]
init = Init(secrets=(secret_spec,))
delivery = init.secrets_delivery("http_post", completion="all_required", reject_unknown=True)

validation = delivery.validate_payload({"api_token": "tok_123456"})
runtime = delivery.materialize_runtime("runtime")
print(validation.ready, runtime.global_env_path)
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
