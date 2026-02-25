# SPEC: TDX VM Python SDK

## 0. Document Metadata
- Status: Proposed
- Audience: SDK maintainers, module authors, security reviewers, platform engineers
- Scope: Python SDK for building, measuring, and deploying TDX-capable VM images using mkosi as the build substrate
- Primary output: deterministic image artifacts + verifiable measurement manifests

## 0.1 Locked Product Decisions
- V1 scope includes all core capabilities in this spec: build, measure, deploy, modules, secrets, policy, and reproducibility controls.
- Python support is latest stable CPython only at release time (no backport compatibility target in V1).
- SDK-only product surface. No official CLI in V1.
- CI default is frozen lock enforcement (`img.bake(frozen=True)` and stale lock is a hard failure).
- Mutable git refs are allowed in developer flows with visible warnings; strict mode can escalate warnings to failures.
- Default distro baseline is Debian.
- Build backends in V1 are both first-class: Lima and local Linux.
- Deploy targets in V1 are all first-class: `qemu`, `azure`, and `gcp` (plus optional `ssh` adapter).
- Measurement support in V1 includes both raw TDX RTMR and cloud PCR models.
- Secret delivery is owned by `Init`, with default method `http_post`.
- Image hardening default is debloated builds; users must opt out explicitly.
- API strategy is clean break. No compatibility shim layer for legacy script APIs.
- Recipe methods are declarative only; filesystem/build artifacts are produced only by explicit output operations (`lock`, `emit_mkosi`, `bake`, optional measurement export methods).
- Target-specific image conversion is part of `bake`, not `deploy`.
- Cloud-specific in-guest config fetch scripts (metadata/Vault bootstrap shell patterns) are not part of the SDK design.

---

## 1. Executive Summary

This specification defines a high-quality Python SDK that compiles a Python DSL into mkosi build inputs and runtime artifacts for TDX-enabled virtual machines. The SDK is intentionally a library (not a mandatory CLI), with strong defaults for reproducibility and hardening, while preserving escape hatches for advanced users.

The design centers on four constraints:
1. Reproducibility: same inputs should produce byte-stable artifacts whenever upstream inputs are pinned.
2. Measurability: image outputs must map cleanly to expected attestation measurements.
3. Composability: modules should be reusable and safe to combine (`setup` once, `install` many).
4. Operability: users need straightforward build/measure/deploy flows with profile-aware behavior.

The SDK includes:
- A typed `Image` API
- Profile-scoped configuration and operations
- Reproducible source builders (Go, Rust, .NET, C/C++, script fallback)
- Verified fetch primitives with mandatory integrity
- Core modules for TDX quote service (`Tdxs`) and pre-systemd initialization (`Init`)
- Lockfile + content-addressed cache
- Measurement and verification interfaces for raw TDX and cloud variants

---

## 2. Problem Statement

Existing mkosi-based TDX image workflows are usually shell-script heavy. They work, but they have recurring issues:
- Build logic spread across conf files + shell scripts + ad hoc templating.
- Reproducibility gaps from branch-based git clones (`main`, `master`, feature branches).
- Weak cache keys that do not include all build inputs.
- Inconsistent contracts between docs and code (for example, transport request schema drift).
- Difficult multi-profile orchestration without duplication and fragile Makefile logic.

This SDK should preserve the practical strengths from existing projects while eliminating the repeatable failure modes above.

---

## 3. Goals

1. Provide a Python-first API for complete image definition and lifecycle operations.
2. Preserve full control over mkosi lifecycle phases, but expose safer high-level methods.
3. Make deterministic builds the default, including toolchain pinning and integrity verification.
4. Support module ecosystem ergonomics with explicit dependencies and idempotent setup.
5. Provide first-class support for TDX boot measurements and runtime quote validation workflows.
6. Support multiple profiles with a shared execution environment and cross-profile cache reuse.
7. Keep escape hatches available (`run`, `prepare`, raw scripts) without making them the primary path.

---

## 4. Non-Goals

1. Replacing mkosi internals with a custom image builder.
2. Automatically solving arbitrary dependency graphs between modules.
3. Supporting non-Linux host build execution as a first-class direct mode.
4. Building a mandatory CLI surface.
5. Hiding all shell scripting forever; shell escape hatches remain necessary.
6. Providing provider-specific in-guest metadata/Vault config-fetch frameworks as first-class SDK abstractions.

---

## 5. Design Principles

1. Full configurability with opinionated defaults.
2. Deterministic output over convenience when tradeoffs conflict.
3. Explicit dependency and phase mapping over hidden magic.
4. Safe-by-default security posture for TDX workloads.
5. API stability and typed contracts over implicit dict-based interfaces.

---

## 6. Terminology

- `Image`: root SDK object collecting declarations.
- `Profile`: named config scope with isolated output artifacts and optionally shared build cache.
- `BuildSpec`: typed declaration of one build artifact.
- `Module`: reusable Python package that configures images via SDK APIs.
- `setup`: module method for one-time build/package setup.
- `install`: module method for per-instance runtime configuration.
- `bake`: image compilation to disk artifact.
- `measure`: expected attestation value derivation from artifacts.
- `deploy`: launch/upload operation for a selected backend.

---

## 7. High-Level Architecture

```text
User Python code
    -> Image DSL + Modules + Build Specs
    -> Validation + Normalization
    -> Internal IR (phase-indexed, profile-indexed)
    -> Emit mkosi tree + helper scripts + metadata
    -> Execute mkosi in backend environment (lima-vm by default)
    -> Collect artifacts + manifests + logs
    -> Optional measure + deploy
```

### 7.1 Core Components

1. `tdx.image`: public `Image` API and profile contexts.
2. `tdx.ir`: canonical intermediate representation for config, phases, and artifacts.
3. `tdx.compiler`: validators + emitters from IR to mkosi tree.
4. `tdx.backends`: build runtime backends (`LimaBackend` and `LocalLinuxBackend`, both first-class in V1).
5. `tdx.builders`: typed language/toolchain builders.
6. `tdx.fetch`: integrity-verified resource fetching and dirhash-aware git fetch.
7. `tundravm.lockfile`: lock resolution and frozen enforcement.
8. `tdx.cache`: content-addressed artifact and fetch caches.
9. `tdx.measure`: measurement replay and quote verification.
10. `tdx.deploy`: target adapters (qemu, azure, gcp, ssh).
11. `tdx.modules`: built-in modules (`Tdxs`, `Init`, init functionalities).

### 7.2 Internal IR Shape (Conceptual)

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Phase = Literal[
    "sync", "skeleton", "prepare", "build", "extra", "postinst",
    "finalize", "postoutput", "clean", "repart", "boot"
]

@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    shell: bool = False

@dataclass
class ProfileIR:
    name: str
    packages: set[str] = field(default_factory=set)
    build_packages: set[str] = field(default_factory=set)
    phases: dict[Phase, list[Command]] = field(default_factory=dict)
    files: list["FileEntry"] = field(default_factory=list)
    services: list["ServiceSpec"] = field(default_factory=list)
    users: list["UserSpec"] = field(default_factory=list)
    secrets: list["SecretSpec"] = field(default_factory=list)
    builds: list["BuildSpec"] = field(default_factory=list)

@dataclass
class ImageIR:
    base: str
    arch: Literal["x86_64", "aarch64"]
    default_profile: str
    profiles: dict[str, ProfileIR]
```

Key point: the public API records declarations; execution happens only in operations (`bake`, `measure`, `deploy`, `lock`).

---

## 8. Public API Specification

## 8.0 Runtime and Scope Requirements

- Runtime target: latest stable CPython only for V1.
- Product surface: library-only API (no CLI).
- V1 includes full feature scope in this spec, not a reduced subset.

## 8.0.1 Side-Effect Model (Recipe vs Execution)

- Declarative methods (`install`, `build`, `file`, `template`, `service`, `user`, `debloat`, `repository`, profile scoping) only mutate in-memory recipe/IR state.
- No mkosi files, build trees, or output images are created by declarative methods.
- Filesystem/output side effects happen only through explicit output operations:
1. `img.lock()` writes `tundravm.lock`.
2. `img.emit_mkosi(path)` writes generated mkosi config/script tree.
3. `img.bake(...)` writes build artifacts and converted target images.
4. `measure().to_json()/to_cbor()` writes exported measurement files.
5. `deploy(...)` performs launch/upload only and does not run conversion.

## 8.1 `Image` Constructor

```python
from tundravm import Image

img = Image(
    build_dir="build",
    base="debian/bookworm",
    arch="x86_64",
    default_profile="default",
    target="x86_64",
    output_targets=("qemu",),  # qemu | azure | gcp
    backend="lima",
    reproducible=True,
)
```

### 8.1.1 Constructor Fields
- `build_dir`: output root. Default `./build`.
- `base`: distro/release tuple.
- `arch`: image architecture.
- `default_profile`: active profile outside explicit context manager.
- `target`: default build target architecture for builders.
- `output_targets`: target artifact formats to produce during `bake`.
- `backend`: build execution backend.
- `reproducible`: enables deterministic defaults globally.
- Profile-scoped override: `img.output_targets(...)` inside `with img.profile(...)`.

## 8.2 Core Methods

```python
result = img.bake(frozen=True)
measurements = img.measure(backend="rtmr")
deploy_result = img.deploy(target="qemu", memory="4G", cpus=2)
```

### 8.2.1 `bake`
- Generates mkosi artifacts from IR.
- Executes mkosi via backend.
- Performs target-specific conversion pipeline for selected `output_targets` as part of bake:
1. `qemu`: native runtime artifacts (for example UKI/raw as configured).
2. `azure`: Azure-ready image artifact (for example fixed VHD).
3. `gcp`: GCP-ready image artifact (for example `disk.raw` tarball format).
- Produces per-target artifact paths in `BakeResult`.
- Returns `BakeResult` for single profile or mapping-like result for multi-profile context.

### 8.2.2 `measure`
- Requires built artifacts.
- Replays measurement model (`rtmr`, `azure`, `gcp`).
- Returns typed `Measurements` object with serialization and verification helpers.

### 8.2.3 `deploy`
- Reads profile artifact already produced by `bake` for the requested target format.
- Dispatches to target adapter.
- Does not perform image conversion.
- Produces `DeployResult` including IDs/URIs/IP where relevant.

### 8.2.4 Output Target Recipe Examples

```python
# Global default target output for all profiles
img.output_targets("qemu")

# Profile-specific output target recipes
with img.profile("azure"):
    img.output_targets("azure")

with img.profile("gcp"):
    img.output_targets("gcp")

# Build all profiles; each profile gets its declared baked artifact format(s)
with img.all_profiles():
    results = img.bake(frozen=True)
```

Rules:
- `output_targets` is declarative recipe state.
- `bake` fails with a validation error if `deploy(target=...)` is requested for a target format that was not baked.

## 8.3 Profile Context Managers

```python
with img.profile("dev"):
    img.install("strace", "gdb")

with img.profiles("dev", "azure"):
    img.bake()

with img.all_profiles():
    img.bake()
    img.measure(backend="rtmr")
```

Semantics:
- Single profile context changes active profile.
- Multi-profile context broadcasts declarations and operations to selected profiles.
- Build backend process may be shared for performance when possible.

## 8.4 Files and Templates

```python
img.file("/etc/motd", content="Trusted domain\n")
img.file("/etc/app/config.toml", src="./config.toml")
img.template(
    src="./templates/app.toml.j2",
    dest="/etc/app/config.toml",
    vars={"network": "mainnet", "rpc_port": 8545},
)
```

Rules:
- `src` and `content` are mutually exclusive.
- deterministic renderer settings required (`trim_blocks`, stable key order, newline normalization).

## 8.5 Lifecycle Hooks

```python
img.sync(["git", "submodule", "update", "--init", "--recursive"])
img.prepare(["pip", "install", "--root", "$BUILDROOT", "pyyaml"])
img.run(["sysctl", "--system"])
img.finalize(["bash", "-lc", "du -sh $BUILDROOT > $OUTPUTDIR/size.txt"], shell=False)
img.postoutput(["sha256sum", "$OUTPUTDIR/latest.efi"])
img.clean(["rm", "-rf", "./tmp-cache"])
```

Quality decision:
- Prefer `argv` form over raw shell strings.
- Keep raw shell escape hatch via `shell=True` for advanced users.

## 8.6 Packages and Repositories

```python
img.install("ca-certificates", "iptables")
img.repository(
    url="https://packages.microsoft.com/debian/12/prod",
    suite="bookworm",
    components=["main"],
    keyring="./keys/microsoft.gpg",
)
```

Rules:
- package lists deduplicated + sorted.
- repository keys must be local files or fetch-verified artifacts.

## 8.7 Users and Services

```python
img.user(
    "nethermind",
    system=True,
    home="/var/lib/nethermind",
    shell="/usr/sbin/nologin",
    uid=800,
    groups=["eth"],
)

img.service(
    name="nethermind",
    exec=["/opt/nethermind/nethermind", "--config", "/etc/nethermind/config.json"],
    user="nethermind",
    after=["network-online.target"],
    requires=["network-online.target"],
    restart="always",
    extra_unit={"Service": {"MemoryMax": "8G", "LimitNOFILE": "65535"}},
)
```

Rules:
- user names unique per profile.
- service names unique per profile.
- service/user graph validated before emit.

## 8.8 Secrets

```python
from tundravm.modules.init import Init
from tundravm import SecretSchema, SecretTarget

# Declare required secrets + delivery targets
img.secret(
    "JWT_SECRET",
    required=True,
    schema=SecretSchema(format="hex", min_len=64, max_len=64),
    targets=[
        SecretTarget.file(dest="/run/tdx-secrets/jwt.hex", owner="nethermind", mode="0440"),
        SecretTarget.env(name="JWT_SECRET", scope="global"),
    ],
)

img.secret(
    "RPC_TOKEN",
    required=True,
    targets=[SecretTarget.env(name="RPC_TOKEN", scope="global")],
)

init = Init(handoff="systemd")
init.secrets_delivery(
    method="http_post",   # default if not specified
    bind="0.0.0.0:8081",
    path="/secrets",
    payload="json",
    completion="all_required",
    reject_unknown=True,
)

img.service(
    name="nethermind",
    exec=["/opt/nethermind/nethermind", "--config", "/etc/nethermind/config.json"],
    after=["secrets-ready.target"],
    requires=["secrets-ready.target"],
)
```

Rules:
- secret values never baked into image tree.
- secret declarations become runtime contract + readiness dependency.
- secret delivery transport is configured in `Init`; direct `image.secret_delivery()` is intentionally not part of the clean-break V1 API.
- delivery method knows all expected secret names from recipe declarations and validates completion before releasing `secrets-ready.target`.
- secrets can target files, global env, or both in one declaration.

## 8.9 Build API

```python
from tundravm import Build
from tundravm.builders.go import GoBuild
from tundravm.builders.rust import RustBuild
from tundravm.builders.dotnet import DotnetBuild

img.build(
    GoBuild(
        name="prover",
        version="1.22.5",
        src="./prover",
        output="/usr/local/bin/prover",
        ldflags="-s -w -X main.version=1.0.0",
        target="x86_64",
    ),
    RustBuild(
        name="raiko",
        toolchain="1.83.0",
        src="./raiko",
        output="/usr/local/bin/raiko",
        features=["tdx", "sgx"],
        build_deps=["libssl-dev", "pkg-config"],
    ),
    DotnetBuild(
        name="nethermind",
        sdk_version="10.0",
        src="./nethermind",
        project="src/Nethermind/Nethermind.Runner",
        output="/opt/nethermind",
        self_contained=True,
    ),
    Build.script(
        name="custom-tool",
        src="./tools/custom",
        build_script=["bash", "-lc", "make release"],
        artifacts={"build/tool": "/usr/local/bin/tool"},
        build_deps=["cmake"],
    ),
)
```

## 8.10 Fetch API

```python
from tundravm import fetch, fetch_git

go_tar = fetch(
    "https://go.dev/dl/go1.22.5.linux-amd64.tar.gz",
    sha256="904b924d435eaea086515c6fc840b4ab...",
)

go_src = fetch_git(
    "https://go.googlesource.com/go",
    tag="go1.22.5",
    sha256="a1b2c3...",  # tree hash
)
```

Rules:
- integrity is mandatory.
- git fetch must resolve immutable revision and verify tree hash.

---

## 9. mkosi Lifecycle Mapping

Phase mapping is explicit and deterministic.

| Order | mkosi phase | SDK API |
|---|---|---|
| 1 | sync | `image.sync()` |
| 2 | skeleton | `image.skeleton()` |
| 3 | package resolution/install | `image.install()`, `build_deps`, `repository()` |
| 4 | prepare | `image.prepare()` |
| 5 | build | `image.build()` |
| 6 | extra files | `file()`, `template()`, unit file emit |
| 7 | postinst | `user()`, service enablement, `run()` |
| 8 | repart/finalize | `partitions()`, `finalize()`, `debloat()` |
| 9 | output write | mkosi output |
| 10 | postoutput | `postoutput()` |
| 11 | clean | `clean()` |
| runtime | boot | `on_boot()` or `Init` module |

### 9.1 Ordering Guarantees

1. Cross-phase ordering is fixed by mkosi phase order.
2. Within the same phase, declaration order is preserved unless explicitly documented otherwise.
3. `user()` emissions happen before service enablement in postinst.
4. `run()` commands execute after users/services/secret dirs are prepared.

### 9.2 Validation Rules

Compiler should fail early when:
- `prepare` references artifacts only available after `build`.
- `service(user=...)` references unknown user and implicit creation is disabled by policy.
- `Init.secrets_delivery(method="script")` is selected without a fetch script.
- two declarations write same destination path with conflicting contents unless `allow_overwrite=True` is set.

---

## 10. Module System Specification

## 10.1 Contract

```python
from tundravm import Image

class ModuleProtocol:
    def setup(self, image: Image) -> None: ...
    def install(self, image: Image, **kwargs) -> None: ...
    def apply(self, image: Image, **kwargs) -> None: ...
```

`setup`:
- compile artifacts
- install shared runtime dependencies
- idempotent by cache-key semantics

`install`:
- create users
- write configs
- register service instances

`apply`:
- convenience default (`setup` then `install`)

## 10.2 Example: Multi-instance Nethermind Module

```python
from importlib.resources import files
from tundravm import Image, Build


def _data(name: str) -> str:
    return str(files("tdx_nethermind").joinpath("data", name))


class Nethermind:
    def setup(self, image: Image) -> None:
        image.install("ca-certificates", "libsnappy1v5")
        image.build(Build.dotnet(
            name="nethermind",
            sdk_version="10.0",
            src=".",
            project="src/Nethermind/Nethermind.Runner",
            output="/opt/nethermind",
            self_contained=True,
            build_deps=["libsnappy-dev", "libgflags-dev"],
        ))

    def install(
        self,
        image: Image,
        *,
        name: str = "nethermind",
        network: str = "mainnet",
        datadir: str | None = None,
        rpc_port: int = 8545,
    ) -> None:
        if datadir is None:
            datadir = f"/var/lib/{name}"

        image.user(name, system=True, home=datadir)
        image.template(
            src=_data("nethermind.cfg.j2"),
            dest=f"/etc/{name}/config.json",
            vars={"network": network, "datadir": datadir, "rpc_port": rpc_port},
        )
        image.service(
            name=name,
            exec=["/opt/nethermind/nethermind", "--config", f"/etc/{name}/config.json"],
            user=name,
            restart="always",
            after=["network-online.target"],
        )

    def apply(self, image: Image, **kwargs) -> None:
        self.setup(image)
        self.install(image, **kwargs)
```

## 10.3 Explicit Dependency Pattern

```python
from tdx_dotnet_runtime import DotnetRuntime

class Nethermind:
    def setup(self, image):
        DotnetRuntime(version="10.0").setup(image)
        image.build(...)
```

No solver is required. Idempotent `setup` keeps this safe.

---

## 11. Core Modules

## 11.1 `tdx.modules.tdxs`

Purpose: provide quote issue/validate service inside guest via Unix socket.

```python
from tundravm.modules.tdxs import Tdxs

Tdxs(
    version="v0.5.0",
    issuer="tdx",       # tdx | azure | simulator
    validator="tdx",    # tdx | azure | simulator
    socket="/run/tdxs/tdxs.sock",
    socket_owner="root",
    socket_group="tdx",
    socket_perm="0660",
).apply(img)
```

Expected outputs:
- tdxs binary build + install
- config file
- service unit
- optional socket unit for activation
- group contract for consumer services

## 11.2 `tdx.modules.init`

Purpose: pre-systemd execution for operations that must happen before application services start.

```python
from tundravm.modules.init import Init
from tundravm.modules.init.encryption import DiskEncryption
from tundravm.modules.init.ssh import SshKeyDelivery

init = Init(handoff="systemd")
init.add(DiskEncryption(
    format="on_initialize",
    key_strategy="random",
    disk_strategy="largest",
    tpm=True,
))
init.add(SshKeyDelivery(method="http", persist_in_luks=True))
init.secrets_delivery(method="http_post")  # default method
init.apply(img)
```

Design note:
- `image.on_boot()` remains available for simple post-systemd tasks.
- `Init` is preferred for disk/key boot-critical workflows.

---

## 12. Reproducibility and Supply Chain

## 12.1 Deterministic Build Requirements

The SDK must set deterministic defaults unless explicitly disabled:
- `SOURCE_DATE_EPOCH`
- path remapping flags for language toolchains
- stable sorting for emitted lists and scripts
- normalized file mode and mtimes for emitted trees
- hermetic-ish build environment definitions (bounded by mkosi and package manager)

## 12.2 Lockfile (`tundravm.lock`)

```toml
version = 1

[[module]]
name = "tdx-nethermind"
version = "1.32.3"
source = "pypi"
url = "https://files.pythonhosted.org/..."
integrity = "sha256:..."

[[fetch]]
url = "https://go.dev/dl/go1.22.5.linux-amd64.tar.gz"
integrity = "sha256:904b..."

[[git]]
url = "https://github.com/NethermindEth/tdxs"
ref = "refs/tags/v0.5.0"
commit = "abcdef123456..."
tree = "sha256:..."
```

Behavior:
- `img.lock()` resolves and writes lockfile.
- `img.bake(frozen=True)` fails on unresolved/stale lock.
- lockfile is profile-agnostic for source inputs, profile-specific only for output artifacts.

## 12.3 Cache Key Design

Current weak keys (name + version only) are not enough. Required key inputs:
- builder implementation version
- source content hash or git tree hash
- build command + flags
- environment variables
- build dependencies list
- toolchain identity and binary hashes
- target architecture
- reproducibility mode

Recommended key formula:

```text
cache_key = sha256(canonical_json({
  "builder": ...,
  "src_tree": ...,
  "toolchain": ...,
  "target": ...,
  "flags": ...,
  "deps": ...,
  "env": ...,
  "sdk_version": ...,
}))
```

## 12.4 Fetch Policy

- Hash is mandatory.
- Redirect final URL must be captured in lock metadata.
- Transport-level TLS verification mandatory.
- `fetch_git` requires immutable ref resolution and tree hash verification.

---

## 13. Security Model

## 13.1 Threats

1. Malicious or drifting upstream source dependencies.
2. Build host compromise altering outputs.
3. Secret leakage into image artifacts or logs.
4. Service over-privilege and unnecessary attack surface.
5. Quote verification using stale or mismatched expected measurements.

## 13.2 Mitigations

- Mandatory integrity checks for fetched resources.
- Frozen lockfile mode for CI and release paths.
- Secrets declared at build time but injected post-measurement only.
- Debloat + unit whitelisting defaults for minimal runtime surface.
- Measurement export with profile + artifact metadata linkage.
- Service sandboxing presets.

## 13.3 Service Hardening Presets

```python
img.service(
    name="my-service",
    exec=["/usr/local/bin/my-service"],
    security_profile="strict",
)
```

`strict` expands to:
- `NoNewPrivileges=yes`
- `PrivateTmp=yes`
- `ProtectSystem=strict`
- `ProtectHome=yes`
- optional capability bounding set

---

## 14. Measurement and Verification Design

## 14.1 Backends

1. `rtmr`: raw TDX runtime measurement registers.
2. `azure`: cloud vTPM PCR model.
3. `gcp`: cloud-specific PCR policy model.

## 14.2 Measurement Artifact Contract

```json
{
  "profile": "default",
  "image_id": "surge-tdx-prover",
  "artifact": "build/default/latest.efi",
  "backend": "rtmr",
  "values": {
    "0": "0x...",
    "1": "0x...",
    "2": "0x...",
    "3": "0x..."
  },
  "generated_at": "2026-02-22T00:00:00Z",
  "tool_version": "tundravm 0.1.0"
}
```

## 14.3 Verification API

```python
m = img.measure(backend="rtmr")
ok = m.verify(quote="./quote.bin")
if not ok.valid:
    print(ok.reason)
    print(ok.mismatched_registers)
```

## 14.4 Quote Service Integration Example

```python
from tundravm.modules.tdxs import Tdxs

Tdxs(issuer="tdx", validator="tdx").apply(img)

img.service(
    name="prover",
    exec=["/usr/local/bin/prover", "--tdxs-socket", "/run/tdxs/tdxs.sock"],
    user="prover",
    after=["tdxs.service"],
    requires=["tdxs.service"],
)
img.run(["usermod", "-aG", "tdx", "prover"])
```

---

## 15. Secrets Delivery Design

## 15.1 Interface

```python
from tundravm.modules.init import Init
from tundravm import SecretSchema, SecretTarget

img.secret(
    "JWT_SECRET",
    required=True,
    schema=SecretSchema(format="hex", min_len=64, max_len=64),
    targets=[
        SecretTarget.file(dest="/run/tdx-secrets/jwt.hex", owner="root", mode="0440"),
        SecretTarget.env(name="JWT_SECRET", scope="global"),
    ],
)

init = Init(handoff="systemd")
init.secrets_delivery(
    method="http_post",
    bind="0.0.0.0:8081",
    path="/secrets",
    payload="json",
    completion="all_required",
    reject_unknown=True,
)  # default method is http_post
```

## 15.2 Runtime Contract

- A generated `secrets-ready.target` indicates completion.
- Services can depend on it to avoid startup races.
- Delivery adapter writes atomically (`tmpfile + rename`) and applies ownership/mode.
- Secret material is delivered post-measurement.
- API boundary is clean break: delivery config belongs to `Init`; legacy direct delivery hooks are not supported.
- Runtime config and secret delivery remain provider-agnostic (`http_post` default), not cloud-specific metadata/Vault scripts.
- `Init` generates an expected-secret manifest from all `img.secret(...)` declarations.
- Completion policy `all_required` means readiness is reached only when all required secrets are received and validated.

## 15.3 HTTP POST JSON Validation Contract

Default `http_post` payload:

```json
{
  "secrets": {
    "JWT_SECRET": "abcdef...",
    "RPC_TOKEN": "token-value"
  }
}
```

Validation rules:
1. Missing required keys -> request rejected.
2. Unknown keys rejected when `reject_unknown=True`.
3. Value validators from `SecretSchema` enforced per secret.
4. Partial updates allowed only for optional secrets; required-set completion gate remains strict.
5. Idempotent retries accepted when values are unchanged.

Delivery outputs:
- File targets: atomically written under declared destination with mode/owner/group.
- Global env targets: exported into generated runtime env file (for example `/run/tdx-secrets/global.env`) and loaded by generated secret-runtime unit before `secrets-ready.target`.

## 15.4 Anti-Leak Controls

1. secret names may appear in logs; values must never appear.
2. generated build scripts must redact secret environment names by policy.
3. secret declaration metadata in lockfile is allowed only without values.
4. global env exports are runtime-only and must not be persisted into immutable image files.
5. services should still prefer file targets for high-sensitivity material unless env use is explicitly required.

---

## 16. Build Backend Design (Lima and Local Linux)

## 16.1 Lima Backend

Responsibilities:
- VM lifecycle management
- mount source/cache/output paths
- invoke mkosi with backend-specific output/cache directories
- collect artifacts back to host tree

## 16.2 Local Linux Backend

Responsibilities:
- execute mkosi directly on Linux hosts with required kernel/userns/systemd prerequisites
- validate host capability before running
- preserve the same build/cache/output contract as Lima backend

## 16.3 Backend Interface

```python
class BuildBackend(Protocol):
    def prepare(self, request: "BakeRequest") -> None: ...
    def execute(self, request: "BakeRequest") -> "BakeResult": ...
    def cleanup(self, request: "BakeRequest") -> None: ...
```

## 16.4 Multi-profile Optimization

Inside `with img.profiles(...)`:
- backend runtime starts once.
- profiles execute sequentially or parallel based on host constraints.
- cache is shared.

---

## 17. Error Model

```python
class TdxError(Exception):
    code: str
    hint: str | None

class ValidationError(TdxError): ...
class LockfileError(TdxError): ...
class ReproducibilityError(TdxError): ...
class BackendExecutionError(TdxError): ...
class MeasurementError(TdxError): ...
class DeploymentError(TdxError): ...
```

Principles:
- deterministic, machine-readable error codes
- actionable hints
- include phase/profile context

Example:

```text
E_PHASE_ORDER_INVALID: command in prepare phase references '/opt/nethermind'
hint: move command to image.run() or install-time module logic
profile: default
```

---

## 18. Observability

## 18.1 Logging

- structured logs with fields: `profile`, `phase`, `operation`, `module`, `builder`, `duration_ms`.
- plain human-readable stream for interactive use.

## 18.2 Emitted Build Report

`build/<profile>/report.json` includes:
- effective package lists
- build specs and cache hit/miss
- emitted phase scripts checksums
- artifact digests
- lockfile digest

---

## 19. Testing Strategy

## 19.1 Unit Tests

- IR normalization rules
- phase mapping validation
- lockfile parsing/roundtrip
- cache key determinism
- renderer determinism

## 19.2 Integration Tests

- minimal image bake success
- module `setup` idempotency
- dual-instance module install
- secrets-ready dependency
- measure output schema compatibility

## 19.3 Reproducibility Tests

1. Build same profile twice with frozen lock.
2. Compare artifact hashes.
3. Compare emitted build reports except allowed volatile fields.

## 19.4 Golden Tests

- `emit_mkosi()` outputs tested via golden directories per profile.
- changes require explicit acceptance in PR.

---

## 20. Critical Issues in Current Ideas and Reference Workflows, with Fixes

## 20.1 Unpinned Mutable Git Refs

Issue:
- Using `main`, `master`, or feature branches for build sources breaks reproducibility and traceability.

Fix:
- `fetch_git` must resolve to immutable commit + tree hash.
- emit visible warnings for mutable refs by default.
- strict policy mode can escalate mutable refs to errors.

Policy example:

```python
img.policy(
    warn_on_mutable_git_refs=True,
    require_lock_for_bake=True,
)
```

## 20.2 Weak Cache Keys

Issue:
- Caches keyed by `name-version` or similar can return wrong artifacts when flags/toolchains differ.

Fix:
- content-addressed keys from full canonical spec.
- store manifest next to artifact and verify before reuse.

## 20.3 Docs/Protocol Drift in Quote Service APIs

Issue:
- transport docs and implementation can drift (`type` vs `method` request field patterns).

Fix:
- SDK ships a versioned client schema and integration test against supported tdxs versions.
- module validates service API compatibility at build time.

## 20.4 Unsafe String Shell Commands as Primary Interface

Issue:
- string shell commands increase quoting/injection mistakes and non-portable behavior.

Fix:
- list-form command API default (`argv`), shell strings as opt-in.

## 20.5 Destructive Disk Format Ambiguity

Issue:
- mixed strategies (`on_initialize`, `on_fail`, `always`, `never`) can be misunderstood; `on_fail` can hide corruption and reformat unexpectedly.

Fix:
- support explicit policy:

```python
DiskEncryption(
    format="on_initialize",
    on_mount_failure="fail",  # fail | reformat
)
```

Default `on_mount_failure="fail"` for safety.

## 20.6 Secret Handling via Temporary Paths

Issue:
- writing runtime secrets to weakly controlled temporary paths can expose material.

Fix:
- default secret destinations under dedicated root-owned runtime dir (`/run/tdx-secrets`).
- strict perms and service dependency on secrets-ready.

## 20.7 Build-Time Network Non-Determinism

Issue:
- unrestricted network in build phase makes builds drift or fail unpredictably.

Fix:
- build network mode policy:

```python
img.policy(network_mode="locked")
```

`locked` allows only fetches defined in lockfile.

## 20.8 Systemd Debloat Overreach Risk

Issue:
- aggressive unit masking can remove required units unexpectedly for certain modules.

Fix:
- use profile-aware debloat presets and compile-time unit dependency check.
- provide explain mode to show what will be masked and why.

## 20.9 Profile Artifact Collisions

Issue:
- multiple profiles writing to same artifact names can overwrite each other.

Fix:
- enforce `build/<profile>/...` output isolation.
- stable `latest` symlink per profile only.

## 20.10 Inconsistent Reproducibility Metadata

Issue:
- image version scripts that include dirty git state can affect metadata.

Fix:
- keep informative build metadata outside measured payload by default.
- expose configurable `image_version_mode` with warning when it impacts reproducibility.

## 20.11 Cloud-Specific In-Guest Config Fetch Scripts

Issue:
- Embedding provider-specific metadata/Vault fetch shell logic inside guest images reduces portability, increases operational coupling, and weakens clean-break API goals.

Fix:
- Do not model these scripts as first-class SDK functionality.
- Use generic `Init` delivery flows (`http_post` default, optional alternatives) for runtime config/secrets injection.
- Keep provider integrations in deploy/control-plane tooling, not inside the guest recipe model.

---

## 21. Detailed Debloat Design

## 21.1 API

```python
img.debloat()
img.debloat(systemd_minimize=False)
img.debloat(paths_skip=["/usr/share/bash-completion"])
img.debloat(paths_remove_extra=["/usr/share/fonts"])
```

## 21.2 Behavior

1. Path stripping runs in finalize on host with `$BUILDROOT`.
2. systemd minimization applies allow-list and masks others.
3. Debloat is enabled by default in V1.
4. Optional profile overrides are available for dev ergonomics.

## 21.3 Explain Mode

```python
plan = img.explain_debloat()
print(plan.to_table())
```

Outputs:
- removed paths
- masked units
- kept units and reason

---

## 22. Deployment Design

Target conversion is part of `bake`. Deployment adapters assume target-ready artifacts already exist.

## 22.1 QEMU

```python
img.deploy(
    target="qemu",
    memory="8G",
    cpus=4,
    vsock_cid=5,
    tdx=True,
    hostfwd=["2222:22", "8545:8545"],
)
```

## 22.2 Azure

```python
img.deploy(
    target="azure",
    resource_group="tdx-rg",
    location="eastus",
    vm_size="Standard_DC4as_v5",
    image_name="tdx-node-v1",
)
```

## 22.3 GCP

```python
img.deploy(
    target="gcp",
    project="my-project",
    zone="us-central1-a",
    machine_type="n2d-standard-4",
    image_name="tdx-node-v1",
)
```

Deploy adapters must validate required params and provide typed results.

---

## 23. Package Layout Proposal

```text
tdx/
  __init__.py
  image.py
  profiles.py
  kernel.py
  models.py
  errors.py
  ir/
    model.py
    normalize.py
    validate.py
  compiler/
    emit_mkosi.py
    emit_scripts.py
  builders/
    go.py
    rust.py
    dotnet.py
    c.py
    script.py
  cache/
    store.py
    keys.py
  fetch/
    http.py
    git.py
  lockfile/
    model.py
    resolve.py
    io.py
  backends/
    lima.py
    local_linux.py
  measure/
    rtmr.py
    azure.py
    gcp.py
  deploy/
    qemu.py
    azure.py
    gcp.py
    ssh.py
  modules/
    tdxs/
    init/
  templates/
    mkosi/
```

---

## 24. End-to-End Example: High-Quality Image Definition

```python
#!/usr/bin/env python3
from tundravm import Image, Build, Kernel, SecretSchema, SecretTarget
from tundravm.modules.tdxs import Tdxs
from tundravm.modules.init import Init
from tundravm.modules.init.encryption import DiskEncryption
from tundravm.modules.init.ssh import SshKeyDelivery

img = Image(
    build_dir="build",
    base="debian/bookworm",
    arch="x86_64",
    default_profile="default",
    output_targets=("qemu",),
    reproducible=True,
)

img.kernel = Kernel.tdx(version="6.8")
img.install("ca-certificates", "iptables", "dropbear")
img.repository(
    url="https://snapshot.debian.org/archive/debian/20260115T000000Z/",
    suite="bookworm",
    components=["main"],
)

init = Init(handoff="systemd")
init.add(DiskEncryption(
    format="on_initialize",
    key_strategy="random",
    disk_strategy="largest",
    tpm=True,
    on_mount_failure="fail",
))
init.add(SshKeyDelivery(method="http", persist_in_luks=True))
init.secrets_delivery(method="http_post")
init.apply(img)

Tdxs(
    version="v0.5.0",
    issuer="tdx",
    validator="tdx",
    socket_group="tdx",
).apply(img)

img.build(Build.go(
    name="my-prover",
    version="1.22.5",
    src="./prover",
    output="/usr/local/bin/my-prover",
    ldflags="-s -w -X main.version=1.0.0",
))

img.user("my-prover", system=True, home="/var/lib/my-prover", groups=["tdx"])
img.service(
    name="my-prover",
    exec=["/usr/local/bin/my-prover", "--socket", "/run/tdxs/tdxs.sock"],
    user="my-prover",
    after=["network-online.target", "tdxs.service", "secrets-ready.target"],
    requires=["tdxs.service", "secrets-ready.target"],
    restart="always",
)

img.secret(
    "JWT_SECRET",
    required=True,
    schema=SecretSchema(format="hex", min_len=64, max_len=64),
    targets=[
        SecretTarget.file(dest="/run/tdx-secrets/jwt.hex", owner="my-prover", mode="0440"),
        SecretTarget.env(name="JWT_SECRET", scope="global"),
    ],
)
img.secret(
    "RPC_TOKEN",
    required=True,
    targets=[SecretTarget.env(name="RPC_TOKEN", scope="global")],
)

# Debloat is default; explicit call optional.
img.debloat()

# Default profile bake
img.lock()
img.bake(frozen=True)
rtmr = img.measure(backend="rtmr")
rtmr.to_json("build/default/measurements.json")

# Dev profile
with img.profile("dev"):
    img.install("strace", "gdb", "vim")
    img.ssh(enabled=True)
    img.debloat(enabled=False)

# Profile-specific artifact recipes
with img.profile("azure"):
    img.output_targets("azure")
with img.profile("gcp"):
    img.output_targets("gcp")

with img.profiles("default", "dev"):
    img.bake(frozen=True)

with img.profile("dev"):
    img.deploy(target="qemu", memory="8G", cpus=4)
```

---

## 25. Example: Authoring a New Third-Party Module

```python
from importlib.resources import files
from tundravm import Image, Build


def _data(name: str) -> str:
    return str(files("tdx_metrics").joinpath("data", name))


class MetricsAgent:
    def setup(self, image: Image) -> None:
        image.install("ca-certificates")
        image.build(Build.go(
            name="metrics-agent",
            version="1.22.5",
            src=".",
            output="/usr/local/bin/metrics-agent",
        ))

    def install(self, image: Image, *, name: str = "metrics", port: int = 9090) -> None:
        image.user(name, system=True, home=f"/var/lib/{name}")
        image.template(
            src=_data("config.yaml.j2"),
            dest=f"/etc/{name}/config.yaml",
            vars={"port": port},
        )
        image.service(
            name=name,
            exec=["/usr/local/bin/metrics-agent", "--config", f"/etc/{name}/config.yaml"],
            user=name,
            restart="always",
        )

    def apply(self, image: Image, **kwargs) -> None:
        self.setup(image)
        self.install(image, **kwargs)
```

Usage:

```python
from tundravm import Image
from tdx_metrics import MetricsAgent

img = Image(build_dir="build", base="debian/bookworm")
MetricsAgent().apply(img, port=9191)
img.bake()
```

---

## 26. Example: Strong Policy Mode for CI

```python
img.policy(
    require_lock_for_bake=True,
    warn_on_mutable_git_refs=True,
    require_integrity_for_all_fetches=True,
    network_mode="locked",
    fail_on_shell_string_commands=False,
)

img.lock()
img.bake(frozen=True)
```

CI checks should fail when:
- unresolved lock entries exist
- strict mutable-ref policy is enabled and mutable refs appear
- emitted mkosi tree differs from golden baseline unexpectedly
- reproducibility smoke test fails

---

## 27. Migration Guidance from Script-Heavy Repos

No compatibility shim layer is planned. Migration is explicit and API-driven.

## 27.1 Migration Strategy

1. Map existing mkosi conf/scripts to `Image` declarations phase-by-phase.
2. Move service build scripts into typed builders or `Build.script` wrappers.
3. Replace branch-based git sources with locked tags/commits.
4. Port templating into `image.template` with explicit vars.
5. Move boot-critical logic from ad hoc oneshots into `Init` functionalities.
6. Encode debloat behavior in declarative `image.debloat` calls.

## 27.2 Practical Example

Old pattern:
- `mkosi.build` calls `git clone --branch main`.

New pattern:

```python
src = fetch_git(
    "https://github.com/example/project",
    tag="v1.2.3",
    sha256="...",
)
img.build(Build.go(name="project", src=src, output="/usr/bin/project"))
```

---

## 28. Roadmap

## V1 Release Scope
- Full scope in this spec ships in V1.
- Includes both build backends (`lima`, `local_linux`).
- Includes all deploy targets (`qemu`, `azure`, `gcp`).
- Includes both measurement families (RTMR and cloud PCR models).
- Includes `Tdxs`, `Init`, and init-owned secret delivery with default `http_post`.
- Includes lockfile-first CI policy and default debloated images.

## Delivery Milestones (Toward V1 GA)
1. Compiler + API + both backends.
2. Reproducibility controls + lockfile + cache.
3. TDX modules + measurement + secret delivery in `Init`.
4. Deploy adapters + policy engine + ecosystem docs.

---

## 29. Acceptance Criteria for This Spec

1. A maintainer can implement the SDK architecture without inventing missing core behavior.
2. A module author can build and install multi-instance services with idempotent setup.
3. Reproducibility-critical behaviors are explicit and testable.
4. Security and attestation flows are part of core design, not add-ons.
5. Critical known issues from prior script-heavy approaches have concrete mitigations.

---

## 30. Final Recommendations

1. Keep `Image` API small and typed, and push complexity into internal IR/compiler.
2. Treat lockfile + fetch integrity as hard requirements, not optional best practices.
3. Make profile behavior explicit in all operation results and artifact paths.
4. Preserve escape hatches, but default users to argv-based safe commands.
5. Keep the API clean break and validate it early against real-world module sets (Nethermind-like, builder-like, and minimal test images).

This design deliberately balances practical mkosi workflows with stronger correctness guarantees, so teams can move from fragile shell orchestration to an auditable, composable SDK without losing flexibility.
