<p align="center">
  <img src="docs/logo.svg" alt="tundravm" width="700"/>
</p>

<p align="center">
  <a href="https://github.com/Hyodar/tundravm/actions/workflows/ci.yml"><img src="https://github.com/Hyodar/tundravm/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"/></a>
  <a href="https://github.com/systemd/mkosi"><img src="https://img.shields.io/badge/mkosi-v26-8b5cf6.svg" alt="mkosi v26"/></a>
</p>

<br>

Python SDK for building, measuring, and deploying TDX-enabled VM images. Write the image as a Python recipe, compile it to a reproducible [mkosi](https://github.com/systemd/mkosi) project tree, and bake it into a bootable disk for QEMU, Azure, or GCP.

## Quickstart

```python
# node.py
from tundravm import Image
from tundravm.backends import LimaMkosiBackend

img = Image(base="debian/trixie", backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
img.install("systemd", "curl", "jq")
img.user("app", system=True, shell="/bin/false")
img.service("app", command="/usr/bin/app", user="app", env={"LOG_LEVEL": "info"})
img.targets("qemu")
```

```bash
tundravm explain node.py                  # dry run: what the image will contain
tundravm check node.py                    # lint the recipe
tundravm compile node.py                  # emit build/mkosi
tundravm bake node.py --lock              # write build/tundravm.lock, then build
tundravm bake node.py -v                  # stream mkosi output; -q summary only, --json-logs for CI
tundravm ci node.py --out mkosi           # check --strict + compile --check + lock --check, for CI
tundravm init . --name node --ci github   # bootstrap a project with a GitHub Actions workflow
tundravm measure node.py --backend rtmr   # expected TDX measurements
tundravm deploy node.py --target qemu     # boot the artifact
```

`uv add tundravm` (or `uv sync` in this repo) installs the `tundravm` command. `tundravm new node.py` writes a starter recipe. The [tutorial](docs/tutorial.md) walks through every step with real output.

## Why

Hand-maintained mkosi trees for TDX images are hard to review, drift easily, and are painful to keep reproducible across cloud targets. A recipe is a short Python file that gives you:

- **Deterministic output.** The same recipe always produces the same mkosi tree, byte for byte.
- **Multi-cloud from one definition.** Azure VHD, GCP tar.gz, and QEMU qcow2 from one `Image`.
- **Composable modules.** `KeyGeneration`, `DiskEncryption`, `SecretDelivery` wire themselves into the boot sequence.
- **Lockfile and policy.** Frozen bakes, mutable-ref enforcement, integrity checks for CI.

The [`surge-tdx-prover`](examples/surge-tdx-prover/) example reproduces the full [NethermindEth/nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) repository in under 250 lines of Python. A golden test checks the output against the upstream tree.

## What you get

- **Dry-run explain.** `tundravm explain` / `img.explain()` lists packages, files with digests, users, services, hooks, init scripts and modules per profile, without compiling.
- **Linter.** `tundravm check` / `img.check()` returns `Diagnostic`s: services running as undeclared users, duplicate or relative file paths, missing platform modules, init priority collisions, undelivered secrets, and module-specific checks. `bake()` refuses recipes with errors.
- **Drift diff.** `tundravm diff` shows a recipe change as a unified diff of the compiled tree. `compile --check` fails CI when the committed tree is stale.
- **Lockfile.** `tundravm lock` pins the recipe digest per section. `lock --check` names what drifted (`~ profiles.default.packages: +htop`). `bake --frozen` refuses a stale lock.
- **Multi-cloud.** Per-profile output targets, measurements (`rtmr` via `measured-boot`/`dstack-mr`; `azure`/`gcp` placeholders are opt-in, never silent) and deploy adapters (`qemu`, `azure`, `gcp`).
- **Modules.** One `Module` base class with `requires`, init priorities and checks.
- **Testing toolkit.** `tundravm.testing` and a pytest plugin: compiled-tree readers, lint asserts, golden trees, in-process bakes.

Services, groups and packaged units are declared, not scripted:

```python
img.group("eth", system=True).user("nethermind", system=True, groups=("eth",))
img.service("nethermind", command="/usr/bin/nethermind", user="nethermind", group="eth",
            restart="on-failure", limits={"NOFILE": 1048576}, env_file="/etc/nethermind/env")
img.enable("openntpd", "dropbear").mask("ssh.service", "ssh.socket")
```

## Profiles

A profile is a variant of the image, such as one per cloud. A profile extends the default profile: its image is everything declared on the default plus its own additions. On a conflict (same file path, unit, user) the profile wins. `output_targets` and `debloat` fall back to the default when the profile does not set them.

```python
from tundravm.modules import Devtools
from tundravm.platforms import AzurePlatform, GcpPlatform

azure = img.profile("azure")
azure.apply(AzurePlatform()).install("waagent")

img.profile("gcp").apply(GcpPlatform())

with img.profile("dev"):
    img.apply(Devtools())

with img.all_profiles():
    img.bake()
```

`img.profile(name)` returns a `Profile`. Calls on it declare into that profile and chain. `with img.profile(name):` scopes every `img.*` call in the block. Pass `extends=None` for a standalone profile. On the CLI, select profiles with `--profile NAME` (repeatable) or `--all-profiles`.

## Modules

A module bundles packages, files, services, build hooks and a boot script. Apply one or more in order with `img.apply(...)`:

```python
from tundravm.modules import DiskEncryption, KeyGeneration, SecretDelivery

keys = KeyGeneration()
keys.key("key_persistent", strategy="tpm")

disks = DiskEncryption()
disks.disk("disk_persistent", device="/dev/vda3")

img.apply(keys, disks, SecretDelivery(method="http_post", host="0.0.0.0", port=8080))
```

Every module subclasses `tundravm.modules.Module` and overrides `setup()`, `install()`, `init_script()` and `check()` as needed. Class attributes declare `requires` (modules that must be applied first) and `init_priority` (where its boot script runs in `/usr/bin/runtime-init`). See [`docs/module-authoring.md`](docs/module-authoring.md).

| Module | What it does | Init priority |
|---|---|---|
| `KeyGeneration` | Keys from TPM-backed random or a named pipe; several named keys | 10 |
| `DiskEncryption` | LUKS2 disks with naming, format policy and mount controls | 20 |
| `SecretDelivery` | SSH key and secret delivery over HTTP, with schemas and targets | 30 |
| `Tdxs` | `tundra-tools` issuer/validator service, socket and validator config | |
| `Devtools` | Serial console, root password, SSH and debugging tools for dev profiles | |

`tundravm.platforms` adds `AzurePlatform` and `GcpPlatform`. At `compile()` the SDK writes `/usr/bin/runtime-init` with the boot scripts in priority order, a `runtime-init.service`, and `After=`/`Requires=runtime-init.service` on every other service.

Secrets are declared on `SecretDelivery`:

```python
from tundravm import SecretSchema, SecretTarget

delivery = SecretDelivery(method="http_post", host="0.0.0.0", port=8080)
delivery.secret(
    "api_token",
    required=True,
    schema=SecretSchema(kind="string", min_length=8, pattern="^tok_"),
    targets=(SecretTarget.file("/run/secrets/api-token"), SecretTarget.env("API_TOKEN", scope="global")),
)
img.apply(delivery)
```

Files and directories come from strings, bytes or the host:

```python
img.file("/etc/app/config.toml", src="config/app.toml")
img.directory("/opt/app", src="dist/", exclude=["*.pyc", "tests"])
```

## CLI

Every command takes a recipe file: a Python file that binds an `Image` to `img` or defines `build() -> Image`.

| Command | Does |
|---|---|
| `new PATH` | Write a starter recipe (`--backend lima\|nix\|local\|inprocess`) |
| `explain` | Dry run; `--json` for machine output |
| `check` | Lint; exit 1 on errors (`--strict`: also warnings) |
| `compile` | Emit the mkosi tree; `--check` exits 1 if the tree at `--out` is stale |
| `diff` | Unified diff between the recipe and a compiled tree |
| `digest` | Recipe digest used by lockfiles |
| `lock` | Write the lockfile; `--check` reports drift, `--explain` reports then writes |
| `bake` | Compile and build; `--lock`, `--frozen`, `--out` |
| `measure` | Expected measurements from the last bake (`--backend rtmr\|azure\|gcp`) |
| `deploy` | Deploy the last baked artifact (`--target qemu\|azure\|gcp`) |
| `doctor` | Probe host tools for each backend, or for one recipe's backend |

See [`docs/cli.md`](docs/cli.md) for options, recipe loading rules and exit codes.

## Backends

| Backend | When to use |
|---|---|
| `LimaMkosiBackend` | Default. Runs mkosi inside a Lima VM with Nix. macOS and Linux. |
| `NixMkosiBackend` | Linux with [Nix](https://nixos.org/download.html). Runs mkosi via `nix develop` on the host. |
| `LocalLinuxBackend` | Linux with `mkosi` (v25+) on `PATH` and `sudo` or `unshare`. No Nix. |
| `InProcessBackend` | Tests and tutorials. Writes placeholder artifacts, needs no tools. |

```python
from tundravm.backends import LimaMkosiBackend, LocalLinuxBackend, NixMkosiBackend

Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
Image(backend=NixMkosiBackend())
Image(backend=LocalLinuxBackend())
```

`compile()`, `lock()`, `explain()` and `check()` never need a backend. `tundravm doctor` reports which backends your host can run.

## Reproducibility

- **Snapshot mirrors.** Pin `img.mirror` to a Debian snapshot URL.
- **EFI stub pinning.** `img.efi_stub()` installs a fixed systemd-boot-efi version.
- **Lockfiles.** `img.lock()` records the recipe digest; `bake(frozen=True)` refuses a changed recipe.
- **Debloat.** Strips unused binaries and units and installs a minimal default target.
- **IMAGE_VERSION stripping.** On by default with `reproducible=True`.

See [`docs/reproducibility.md`](docs/reproducibility.md).

## Policy

```python
from tundravm import Policy

img.set_policy(Policy(require_frozen_lock=True, mutable_ref_policy="error", require_integrity=True))
```

See [`docs/policy.md`](docs/policy.md).

## Examples

| Example | Description |
|---|---|
| [`surge-tdx-prover/`](examples/surge-tdx-prover/) | Full [nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) image: all modules, Azure/GCP/devtools profiles |
| [`nethermind_tdx.py`](examples/nethermind_tdx.py) | Base layer: TDX kernel, EFI stub pinning, backports, debloat, Tdxs |
| [`full_api.py`](examples/full_api.py) | End to end: kernel, secrets, init modules, multi-profile cloud deploys |
| [`multi_profile_cloud.py`](examples/multi_profile_cloud.py) | Per-profile Azure / GCP / QEMU output targets |
| [`qemu_basic.py`](examples/qemu_basic.py) | Minimal QEMU-only image |
| [`tdxs_module.py`](examples/tdxs_module.py) | Applying the `Tdxs` module |
| [`strict_secrets.py`](examples/strict_secrets.py) | Secret schemas on `SecretDelivery`, validated at boot |

Every example loads with the CLI, e.g. `tundravm explain examples/qemu_basic.py` or `tundravm explain examples/surge-tdx-prover/image.py --profile azure`.

```bash
python -m examples.surge-tdx-prover compile   # tundravm compile --out examples/surge-tdx-prover/mkosi
python -m examples.surge-tdx-prover bake      # tundravm bake --lock
```

## Documentation

| Doc | Contents |
|---|---|
| [`docs/concepts.md`](docs/concepts.md) | Recipe vs tree vs artifact, profiles, build phases, runtime-init, lockfile, backends, measurements, deploy |
| [`docs/tutorial.md`](docs/tutorial.md) | From `tundravm new` to bake, measure, deploy, CI and tests, with real output |
| [`docs/cli.md`](docs/cli.md) | Commands, options, recipe loading, exit codes |
| [`docs/api.md`](docs/api.md) | `Image` and `Profile` reference, models, diagnostics, errors |
| [`docs/module-authoring.md`](docs/module-authoring.md) | Subclassing `Module`: `requires`, init priorities, checks |
| [`docs/testing.md`](docs/testing.md) | `tundravm.testing` helpers and pytest fixtures |
| [`docs/policy.md`](docs/policy.md) | Policy options and CI settings |
| [`docs/reproducibility.md`](docs/reproducibility.md) | Reproducible build settings, lockfile sections, mkosi requirements |

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
| `E_VALIDATION` from `bake` | The linter found errors. Run `tundravm check RECIPE` |
| `E_LOCKFILE` | Recipe changed since the lock. `tundravm lock RECIPE --check` shows what; `tundravm lock RECIPE` accepts it |
| `E_STATE` | No `bake-result.json`. Run `tundravm bake` first, or pass the bake's `--out DIR` to `measure`/`deploy` |
| `E_POLICY` | Update the policy or bake with `--frozen` |
| `E_DEPLOYMENT` | Add the target with `output_targets(...)` and rebake, or install the target's tool (`qemu-system-x86_64`, `az`, `gcloud`) |
| `E_MEASUREMENT` | Bake the profile you are measuring |
| `E_BACKEND_EXECUTION` | Run `tundravm doctor RECIPE`; check mkosi version (>= 25) and platform |
