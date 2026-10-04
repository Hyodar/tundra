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

Python SDK for building, measuring and deploying TDX-enabled VM images. An image is an immutable Python value: a `Recipe` of typed declarations, grouped into fragments and specialized by variants. tundravm resolves it, lints it, compiles it to a byte-reproducible [mkosi](https://github.com/systemd/mkosi) project tree, pins every source in a lockfile, and bakes the tree into a bootable disk for QEMU, Azure or GCP. Each lifecycle step is a plain function with explicit inputs and results, and the `tundravm` command runs the same steps.

## Quickstart

```python
# node.py
from tundravm import File, Fragment, Package, Recipe, Service, Unit, User, Variant
from tundravm.backends import LimaMkosiBackend

recipe = Recipe(
    name="node",
    common=Fragment("node", items=(
        Package("curl"),
        File("/usr/bin/app", "#!/bin/sh\nexec sleep infinity\n", mode=0o755),
        User("app", shell="/bin/false"),
        Service("app", "/usr/bin/app", user="app", restart="on-failure"),
        Unit("ssh.socket", enabled=False, masked=True),
    )),
    variants=(Variant("default", target="qemu"), Variant("azure", parent="default", target="azure")),
)
backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
```

```bash
tundravm init . --name node --ci github         # or start from the starter recipe and a CI workflow
tundravm inspect node.py                        # dry run: what each variant will contain
tundravm lint node.py                           # every diagnostic; exit 1 on errors
tundravm compile node.py --out mkosi            # emit the mkosi tree, one directory per variant
tundravm lock node.py                           # write build/tundravm.lock (section digests + source pins)
tundravm bake node.py                           # build every variant, frozen against the lock
tundravm measure build --variant default        # expected RTMRs of the baked artifact
tundravm deploy build --variant default --target qemu
```

Commands run on every declared variant unless you pass `--variant NAME` (repeatable). `uv add tundravm` (or `uv sync` in this repo) installs the `tundravm` command. The [tutorial](docs/tutorial.md) runs every step with real output on the in-process backend, which needs no build tools.

## Why

Hand-maintained mkosi trees for TDX images are hard to review, drift easily, and are painful to keep reproducible across cloud targets. A recipe gives you:

- **Deterministic output.** The same recipe always compiles to the same mkosi tree, byte for byte. `compile --check` fails CI when the committed tree is stale.
- **One definition, several targets.** A variant with `target="azure"` or `target="gcp"` gets the platform integration; QEMU qcow2, Azure VHD and GCP tar.gz come from one recipe.
- **Composition without mutation.** Fragments are values: a plain `Fragment(...)`, or a `Composite` subclass configured by its fields. Keys, disks and secrets reference each other as objects, and the runtime-init sequence they need is derived, not hand-ordered.
- **Locked inputs.** The lockfile records a digest per recipe section and a commit or hash per source build. Bakes are frozen against it, and `lock --check` names what drifted.

The [`surge-tdx-prover`](examples/surge-tdx-prover/) example reproduces the full [NethermindEth/nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) image as one declarative recipe. It compiles byte-for-byte to the committed upstream tree, all four variants (`default`, `azure`, `gcp`, `devtools`).

## Concepts in one screen

| Concept | What it is |
|---|---|
| **Recipe** | The whole image: name, base, arch, mirrors, epoch, the `common` fragment and the variants. An immutable value bound to `recipe` in a Python file. |
| **Declaration** | One typed fact about the image: `Package`, `File`, `Template`, `User`, `Group`, `Service`, `Unit`, `Hook`, `Init`, `Key`, `Disk`, `Secrets`, `Build`, `Kernel`, `Setting`, ... Identified by type plus natural key (package name, file path, unit name). |
| **Fragment** | A named group of declarations and nested fragments, with `requires` (fragments that must also be present) and `checks` (lint functions). A reusable "module" is a `Composite`: a `Fragment` subclass whose fields are its configuration. |
| **Variant** | An overlay on its parent: `add` a fragment, `replace` or `remove` inherited declarations, set the `target`. One mkosi directory and one artifact per variant. |
| **Lock** | Section digests of the resolved recipe plus a pin per source build. `tundravm.lock`, committed next to the tree. |
| **Tree** | The compiled mkosi project, held in memory until written. Its digest is what `compile --check` and golden tests compare. |
| **Artifact** | A baked disk image of one variant and target, with the recipe, lock and tree digests it came from. Measurements and deployments take an artifact. |

See [`docs/concepts.md`](docs/concepts.md) for resolution rules, runtime-init ordering and the lifecycle.

## Fragments and composition

A reusable piece of an image is a `Composite`: a `Fragment` subclass whose dataclass fields are its configuration and whose `compose()` returns its contents. `requires` names fragments that must be in the same variant, and `checks` run against the resolved variant during `lint`:

```python
from dataclasses import dataclass

from tundravm import Diagnostic, Disk, Fragment, Init, Package, Resolved, Unit
from tundravm.declarative.utils import Composite

def exporter_check(image: Resolved) -> tuple[Diagnostic, ...]:
    if any(isinstance(item, Disk) and item.mount == "/persistent" for item in image.items):
        return ()
    return (Diagnostic("exporter-storage-missing", "the exporter needs a disk mounted at /persistent"),)

@dataclass(frozen=True, slots=True, kw_only=True)
class PrometheusExporter(Composite):
    storage: Fragment

    def compose(self) -> Fragment:
        return Fragment(
            "prometheus-exporter",
            requires=(self.storage.name,),
            checks=(exporter_check,),
            items=(
                self.storage,
                Package("prometheus-node-exporter"),
                Init("exporter-directory", "install -d -m 0755 /persistent/exporter\n", priority=25, after=("disks",)),
                Unit("prometheus-node-exporter.service", EXPORTER_UNIT, enabled=True, after_init=True),
            ),
        )
```

`PrometheusExporter(storage=storage)` is itself a `Fragment`. A one-off with nothing to configure can stay a plain `Fragment(...)` value. Identical fragments included twice expand once; two different fragments with the same name are an error. `tundravm.declarative.utils` ships `Tdxs()` (the attestation quote service), `DevTools()` (serial console and root login, never ship it), `EfiStub(snapshot=, version=)` and `Backports()`. The full example is in [`docs/module-authoring.md`](docs/module-authoring.md).

Keys, disks and secrets are declarations that reference each other by object, so a disk cannot name a key that does not exist:

```python
key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key)
storage = Fragment("secure-storage", items=(key, disk, Secrets(store=disk)))
```

## Variants

```python
variants=(
    Variant("default", target="qemu"),
    Variant("azure", parent="default", target="azure"),
    Variant("azure-debug", parent="azure", add=DevTools()),       # chained: azure plus debugging, never ship it
    Variant("prod", replace=(File("/etc/motd", "prod\n"),)),
    Variant("slim", remove=(Package("jq"),)),
    Variant("clouds", parent="default", targets=("azure", "gcp")),  # one variant, two disk images
    Variant("bare", parent=None, add=Fragment("bare", items=(Package("curl"),))),
)
```

- `parent` is `"base"` (the default, meaning `Recipe.common`), another variant's name, or `None` for a standalone variant that inherits nothing.
- `add` is a fragment. Adding a declaration whose identity the variant already inherits with a different value is an `identity-collision` error; use `replace`, which swaps an inherited declaration of the same identity (`replace-missing` if there is none). `remove` drops one (`remove-missing` if there is none).
- `target` (or `targets` for several outputs) is `qemu`, `azure` or `gcp`, inherited from the parent; cloud targets add their platform integration. A child cannot drop a cloud target it inherits (`target-inconsistent`).

`Setting`s and the `Kernel` belong to the variant that declares them; declare them in `common` to share them. A variant with its own settings or kernel lowers standalone. See [Variants and targets](docs/concepts.md#variants-and-targets).

## Reproducibility

- **Byte-stable trees.** `epoch=0` (the default) emits a fixed `SourceDateEpoch`/`SOURCE_DATE_EPOCH`, a stable `Seed` and strips `IMAGE_VERSION`. `tundravm compile --check` exits 1 when the committed tree differs from the recipe; `tundravm.testing.assert_tree` does the same in a test.
- **Lock sections.** `tundravm lock` writes one digest per section (`base`, `arch`, `variants.<variant>.packages`, `variants.<variant>.files`, ...). `lock --check` prints the drift (`~ variants.default.packages: +htop`) and exits 1. A lock of every variant covers `--variant` subsets.
- **Pinned sources.** Every `Build` source is resolved to a commit (`Git`) or a hash (`Http`) at lock time; compile and bake fetch exactly that. `lock --update NAME` re-resolves one source; `lock --offline` never touches the network.
- **Frozen bakes.** `bake` is frozen against `build/tundravm.lock` whenever it exists and refuses a recipe that drifted (`E_LOCKFILE`).
- **Snapshot mirrors.** `Recipe(mirror=..., tools_mirror=...)` pins the Debian archive; `EfiStub()` pins the EFI stub.

See [`docs/reproducibility.md`](docs/reproducibility.md).

## CLI

| Command | Does |
|---|---|
| `init [DIR]` | Write a starter recipe and a `.gitignore` block (`--ci github` adds a workflow) |
| `inspect RECIPE` | Dry run per variant (`--format text\|json\|markdown`) |
| `lint RECIPE` | Every diagnostic; exit 1 on errors (`--strict`: also warnings) |
| `compile RECIPE` | Emit the mkosi tree; `--check` exits 1 if the tree at `--out` is stale |
| `diff RECIPE` | Unified diff from a compiled tree to the recipe (`--stat`) |
| `lock RECIPE` | Write the lockfile; `--check` reports drift, `--update`, `--offline`, `--explain` |
| `bake RECIPE` | Build the variants; `--backend`, `--lockfile`, `--out`, `-v`/`-q`/`--json-logs` |
| `measure MANIFEST` | Expected measurements of a baked variant (`--scheme rtmr\|azure\|gcp`) |
| `deploy MANIFEST` | Deploy a baked variant (`--target qemu\|azure\|gcp`, `--param KEY=VALUE`) |
| `doctor [RECIPE]` | Probe the host tools a backend needs; with a recipe, lint it too |
| `ci RECIPE` | `lint --strict`, `compile --check` and `lock --check`; stop at the first failure |

`--variant NAME` is repeatable on every recipe command; omitting it selects every variant. See [`docs/cli.md`](docs/cli.md) for flags, recipe loading, output formats and exit codes.

## Backends

| Backend | When to use |
|---|---|
| `LimaMkosiBackend` | Default. Runs mkosi inside a Lima VM with Nix. macOS and Linux. |
| `NixMkosiBackend` | Linux with [Nix](https://nixos.org/download.html). Runs mkosi via `nix develop` on the host. |
| `LocalLinuxBackend` | Linux with `mkosi` (v25+) on `PATH` and `sudo` or `unshare`. No Nix. |
| `InProcessBackend` | Tests and tutorials. Writes simulated artifacts, needs no tools. |

A recipe file binds its backend to a module-level `backend`; `tundravm bake --backend lima|nix|local|inprocess` overrides it, and the Python `bake()` takes `Backend(kind)`. `inspect`, `lint`, `compile` and `lock` never need one. `tundravm doctor` reports which backends your host can run.

## Examples

| Example | Description |
|---|---|
| [`surge-tdx-prover/`](examples/surge-tdx-prover/) | The full [nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) image: Raiko, Taiko client and Nethermind built from source, TPM-sealed key, encrypted disk, secrets. Compiles byte-for-byte to the committed upstream tree, all four variants. |
| [`nethermind_tdx.py`](examples/nethermind_tdx.py) | The base layer as a fragment, `NethermindBase()`: TDX kernel, EFI stub pinning, backports, debloat, `Tdxs()` |
| [`fragments/`](examples/fragments/) | `Raiko()`, `TaikoClient()`, `Nethermind()`: fragments with a `Build`, a user, a unit and an env file |
| [`full_api.py`](examples/full_api.py) | Most declaration types in one recipe: kernel, keys, disks, secrets, builds, variants |
| [`multi_profile_cloud.py`](examples/multi_profile_cloud.py) | Standalone variants per target, each with its own guest agent |
| [`qemu_basic.py`](examples/qemu_basic.py) | Minimal QEMU image |
| [`tdxs_fragment.py`](examples/tdxs_fragment.py) | The `Tdxs()` fragment |
| [`strict_secrets.py`](examples/strict_secrets.py) | Secret schemas and delivery targets, validated at boot |

```bash
tundravm inspect examples/surge-tdx-prover/image.py --variant azure
python -m examples.surge-tdx-prover compile --check   # compare with the committed mkosi/ tree
```

## Documentation

| Doc | Contents |
|---|---|
| [`docs/concepts.md`](docs/concepts.md) | The seven concepts, resolution, lowering, runtime-init, keys/disks/secrets, variants, lifecycle |
| [`docs/tutorial.md`](docs/tutorial.md) | From `tundravm init` to bake, measure, deploy, a fragment and a test, with real output |
| [`docs/cli.md`](docs/cli.md) | Every command and flag, recipe loading, output formats, exit codes, CI |
| [`docs/api.md`](docs/api.md) | Every declaration, lifecycle function, result type, error and lint rule |
| [`docs/module-authoring.md`](docs/module-authoring.md) | Writing fragments: `requires`, `checks`, `Init` ordering, builds, tests |
| [`docs/testing.md`](docs/testing.md) | `tundravm.testing` helpers and pytest fixtures |
| [`docs/reproducibility.md`](docs/reproducibility.md) | Reproducible output, lock sections, pinned sources, golden trees |
| [`docs/policy.md`](docs/policy.md) | `Policy` options and CI settings |
| [`docs/design/declarative-api.md`](docs/design/declarative-api.md) | Design record of this API, with implementation notes |

## Development

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest
```

## Troubleshooting

Every SDK error prints `error [E_CODE]: message`, an optional `Hint:` and context lines, and exits 2.

| Error | Fix |
|---|---|
| `E_VALIDATION` | A declaration is malformed, or the recipe has error diagnostics. `tundravm lint RECIPE` lists them all. Also raised for an unknown `--variant` |
| `E_LINT` | `bake` refused a recipe with error-level compiler findings. Run `tundravm lint RECIPE` |
| `E_LOCKFILE` | The recipe drifted from the lockfile. `tundravm lock RECIPE --check` shows what; `tundravm lock RECIPE` accepts it |
| `E_STATE` | No `bake-result.json` where `measure`/`deploy` looked, or no artifact for that variant/target. Bake first, and pass the bake's `--out` directory |
| `E_MEASUREMENT` | No `measured-boot`/`dstack-mr` on `PATH`, or a simulated artifact. Install a tool, or pass `--allow-placeholder` for test values |
| `E_DEPLOYMENT` | A simulated artifact, an artifact of another target, or a missing target tool (`qemu-system-x86_64`, `az`, `gcloud`) |
| `E_POLICY` | A `Policy` setting refused the operation (frozen lock, offline network) |
| `E_BACKEND_EXECUTION` | The build failed. Run `tundravm doctor RECIPE`; check the mkosi version (>= 25) and platform |
| `E_REPRODUCIBILITY` | Two builds that should match did not |
