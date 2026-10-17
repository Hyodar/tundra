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

## Install

tundravm is not on PyPI yet: install it from git. To start a project without installing anything first:

```bash
uvx --from git+https://github.com/Hyodar/tundra tundravm init my-image --ci github
cd my-image
uv sync              # installs the pinned tundravm (and pytest) into .venv, writes uv.lock
git add uv.lock      # committed, the generated workflow installs exactly it (`uv sync --frozen`)
```

`init` writes `dependencies = ["tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"]` into `pyproject.toml`, pinned to the commit of the tundravm that ran it (read from its install metadata; when that is unknown the line follows `master` and the comment above it says so), so `uv sync` installs the same tundravm on a fresh machine or a CI runner. `git` must be on `PATH`. In a project that already has a `pyproject.toml`, add it yourself: `uv add "tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"`. The generated workflow syncs with `--frozen` once `uv.lock` is committed and installs a pinned mkosi (`v26`) in its bake job. To work against a local checkout, `uv add --editable PATH/TO/tundravm` replaces the git dependency; in this repository, `uv sync` installs the `tundravm` command (see [Development](#development)). [Installing tundravm in the project](docs/cli.md#installing-tundravm-in-the-project) has the details.

## Quickstart

```python
# node.py
from tundravm import Debian, File, Package, Recipe, Service, Unit, User, Variant
from tundravm.backends import LimaMkosiBackend

recipe = Recipe(
    name="node",
    platform=Debian("trixie"),  # the kernel and systemd boot set every variant needs
    items=(
        Package("curl"),
        File.text("/usr/bin/app", "#!/bin/sh\nexec sleep infinity\n", mode=0o755),
        User("app", shell="/bin/false"),
        Service("app", "/usr/bin/app", user="app", restart="on-failure"),
        Unit("ssh.socket", enabled=False, masked=True),
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("azure", inherits="default", outputs=("azure",)),
    ),
)
backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
```

```bash
tundravm init . --name node --ci github                 # or scaffold: starter recipe, [tool.tundravm], tests, CI
tundravm lock                                           # build/tundravm.lock: section digests + source pins
tundravm compile --tree mkosi                           # emit the mkosi tree, one directory per variant; commit it
tundravm watch                                          # one lint + tree-check line per save; Ctrl-C stops
tundravm bake --verify-reproducible                     # build every variant twice, frozen against the lock
tundravm measure --variant default --export-policy build/default/policy.json  # expected RTMRs, where evidence looks
tundravm deploy qemu --variant default --param tdx=true # boot it as a TDX guest (needs a TDX host, see below)
tundravm attest --issuer unix:./tdxs.sock --validator unix:./validator.sock \
  --policy build/default/policy.json --record build/default/attestation.json  # quote vs policy, kept as a record
tundravm evidence --variant default --attestation build/default/attestation.json \
  --sign ~/.ssh/id_ed25519 --bundle evidence.tar.gz      # signed auditor record with a verdict
tundravm verify evidence.tar.gz --build build --allowed-signers allowed_signers  # re-check it anywhere, image bytes too
tundravm release --version 1.0 --variant default \
  --require reproducible,measured,lock,attested \
  --attestation build/default/attestation.json         # releases/node-1.0, immutable once it passes
tundravm changes --since releases/node-1.0 --format markdown  # after the next edit: what a reviewer checks
```

Run the commands with `uv run` in the project (or with its `.venv` activated); `tundravm status` names the next one to run at any point. Pass the same `--variant` to `measure`, `deploy`, `evidence` and `release`: `measure` and `deploy` need one when the bake holds several variants, and `evidence` and `release` otherwise cover every baked (or eligible) variant, each of which then needs its own policy and record. `measure --export-policy build/default/policy.json` writes the policy where `evidence` looks for it by default (`BUILD/<variant>/policy.json`); a policy anywhere else needs `evidence --policy FILE`, and without one `evidence` notes the variant as unmeasured. `attest` asks the booted image's tdxs service for a quote, so the recipe needs `Tdxs` (the `prover` template declares it); `--validator` names a tdxs validator that checks the quote's signature and collateral, and `--record` keeps the result for `evidence --attestation` and `release --attestation` (at `BUILD/<variant>/attestation.json`, `status` counts it towards release readiness). `evidence --sign` signs `evidence.json` with an SSH key (`ssh-keygen -Y sign`); `verify --allowed-signers` checks that signature against an OpenSSH allowed-signers file, and `verify --build build` also re-hashes the images the bundle describes (without it the artifacts' bytes are not checked). `release` bakes twice, measures, bundles and verifies again into `releases/node-1.0`; `--require` makes each named check fatal. A reproducible bake gives the release the same artifact and policy, so the record still applies; when it does not, the release stops at `attested`: attest the image against `releases/node-1.0/default/policy.json` and resume with `tundravm release --version 1.0 --attestation FILE`. `changes --since` compares the recipe and lockfile as they are now with that release.

Moving from the simulator to a TDX host takes `--param tdx=true` (TDVF firmware through `-bios`), a host kernel, KVM and QEMU with TDX support, a tdxs validator running on the verifying side, and a way to reach the image's `/var/tdxs.sock`: the key `Secrets` delivers is installed with `no-port-forwarding`, so `ssh -L` with it is refused. [CLI: Real TDX](docs/cli.md#real-tdx) has the sequence; it has not been run on TDX hardware.

`init --template minimal|service|cloud|prover` picks the starter recipe (default `service`; `init --list-templates` describes them). `init --app go --source URL@REF` (or `--path DIR`) instead writes a recipe that builds one Go program with `GoBinary` and runs it as a service under its own user, with optional `--port`, `--disk NAME` (an encrypted state disk) and `--secret NAME --delivery external:AGENT`, plus tests over the planned image ([CLI: A Go app](docs/cli.md#a-go-app)). `init` also writes a `[tool.tundravm]` table (`recipe`, `tree`, `build`, `lockfile`, `backend`) into `pyproject.toml` that every command reads, so in a scaffolded project `RECIPE` is optional (`tundravm status` with no arguments reports on `node.py`) and `measure`, `deploy` and `sbom` find the bake in the configured `build` directory. Commands run on every declared variant unless you pass `--variant NAME` (repeatable). The [tutorial](docs/tutorial.md) runs every step with real output on the in-process backend, which needs no build tools.

`init --ci github` writes `.github/workflows/tundravm.yml` with two jobs. `check` runs on every push to `main` and every pull request: it posts `inspect --format markdown` to the job summary and runs `tundravm ci`, which fails on lint findings or a stale committed tree or lockfile. Both install the project with `uv sync`, `uv sync --frozen` once `uv.lock` is committed. `bake` is opt-in: it runs after `check` only when the repository variable `TUNDRAVM_BAKE_BACKEND` names a backend (`local` or `inprocess`; `lima` and `nix` need tools `ubuntu-latest` lacks), then installs mkosi `v26`, fetches, bakes with `--verify-reproducible`, and uploads the `evidence --bundle --html` output with the built images as the `tundravm-bake` artifact.

## Why

Hand-maintained mkosi trees for TDX images are hard to review, drift easily, and are painful to keep reproducible across cloud targets. A recipe gives you:

- **Deterministic output.** The same recipe always compiles to the same mkosi tree, byte for byte. `compile --check` fails CI when the committed tree is stale.
- **One definition, several targets.** A variant with `outputs=("azure",)` or `outputs=("gcp",)` gets the platform integration; QEMU qcow2, Azure VHD and GCP tar.gz come from one recipe.
- **Composition without mutation.** Fragments are values: a plain `Fragment(...)`, or a `@component` class configured by its fields. Keys, disks and secrets reference each other as objects, and the runtime-init sequence they need is derived, not hand-ordered.
- **Locked inputs.** The lockfile records a digest per recipe section and a commit or hash per source build. Bakes are frozen against it, and `lock --check` names what drifted.
- **Auditor evidence.** `tundravm evidence` packs one bake's record (digests, the lockfile and its drift, every artifact re-hashed, the reproducibility outcome, the measurements policy, SBOMs, lint) into an `evidence.json` index with a pass, fail or incomplete verdict and stable reason codes (`--require` makes missing checks count), a byte-reproducible `tar.gz` under `SOURCE_DATE_EPOCH` and a one-page HTML report.
- **Secrets lint.** `secret-in-file` and `secret-in-env` refuse private keys, cloud and GitHub tokens and literal passwords baked into files, units or service environments, naming the kind and line but never the value; credentials go in `Secrets` and arrive at boot, written by the provider `Secrets(delivery=)` names (`secret-target-unsupported` refuses a target nobody writes: the built-in delivery writes only the SSH key).

The [`surge-tdx-prover`](examples/surge-tdx-prover/) example reproduces the full [NethermindEth/nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) image as one declarative recipe. It compiles byte-for-byte to the committed upstream tree, all four variants (`default`, `azure`, `gcp`, `devtools`).

## Concepts in one screen

| Concept | What it is |
|---|---|
| **Recipe** | The whole image: its `platform` (`Debian(release, snapshot=, boot=, efi_stub=)`: the distribution, archive, arch, epoch and the packages every variant boots with), its own `items` and the variants. An immutable value bound to `recipe` in a Python file. |
| **Declaration** | One typed fact about the image: `Package`, `File`, `FileTree`, `Template`, `User`, `Group`, `Service`, `Unit`, `Hook`, `BootStep`, `Key`, `Disk`, `Secrets`, `Build`, `Kernel`, `Setting`, ... Identified by type plus natural key (package name, file path, unit name); `value.ref` (or `Type.ref(key)`) names it in a variant's `changes`. |
| **Fragment** | A named group of declarations and nested fragments, with `requires` (fragments that must also be present) and `checks` (lint functions). A reusable "module" is a `@component` class: a `Fragment` whose fields are its configuration. |
| **Variant** | An overlay on its parent: `add` a fragment, edit or remove inherited declarations by ref (`changes`), name its `outputs` (`qemu`, `azure`, `gcp`). One mkosi directory per variant and one artifact per output. |
| **Lock** | Section digests of the resolved recipe plus a pin per source build. `tundravm.lock`, committed next to the tree. |
| **Tree** | The compiled mkosi project, held in memory until written. Its digest is what `compile --check` and golden tests compare. |
| **Artifact** | A baked disk image of one variant and target, with the recipe, lock and tree digests it came from. Measurements and deployments take an artifact. |

See [`docs/concepts.md`](docs/concepts.md) for resolution rules, runtime-init ordering and the lifecycle.

## Fragments and composition

A reusable piece of an image is a component: a class decorated with `@component`, whose fields are its configuration and whose `compose()` yields its contents. `@component(requires=...)` names fragments that must be in the same variant, and a `check` method runs against the resolved variant during `lint`:

```python
from collections.abc import Iterator

from tundravm import BootStep, Declaration, Diagnostic, Disk, Fragment, Package, Resolved, Unit, component
from tundravm.declarative.utils import Composite

@component
class PrometheusExporter(Composite):
    storage: Fragment
    disk: Disk

    def compose(self) -> Iterator[Declaration | Fragment]:
        yield self.storage
        yield Package("prometheus-node-exporter")
        directory = BootStep("exporter-directory", "install -d -m 0755 /persistent/exporter\n", after=(self.disk.ready,))
        yield directory
        yield Unit("prometheus-node-exporter.service", EXPORTER_UNIT, enabled=True, needs=(directory.ready,))

    def check(self, image: Resolved) -> tuple[Diagnostic, ...]:
        if any(isinstance(item, Disk) and item.mount == "/persistent" for item in image.items):
            return ()
        return (Diagnostic("exporter-storage-missing", "error", "the exporter needs a disk mounted at /persistent"),)
```

`PrometheusExporter(storage=storage, disk=disk)` is itself a `Fragment`; its boot step runs once the disk is mounted, and the unit starts once the step has run (a service without `needs=` waits for every boot step; a unit waits only for its `needs=`). A one-off with nothing to configure can stay a plain `Fragment(...)` value. Identical fragments included twice expand once; two different fragments with the same name are an error. `tundravm.declarative.utils` ships `Tdxs()` (the attestation quote service), `DevTools()` (serial console and root login, never ship it), `GoBinary(name, source, package=)` and `CargoBinary` (a program built from source, installed at its `.executable`), `EfiStub(snapshot=, version=)` and `Backports()`. A component can also export typed values, such as an `HttpEndpoint`, that another component takes as a field instead of parsing its files, and `ConfigFile.yaml`/`toml`/`json`/`ini`/`env` render config files from data as ordinary `File`s. The full example is in [`docs/module-authoring.md`](docs/module-authoring.md).

Keys, disks and secrets are declarations that reference each other by object, so a disk cannot name a key that does not exist:

```python
key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key)
storage = Fragment("secure-storage", items=(key, disk, Secrets(store=disk)))
```

## Variants

```python
variants=(
    Variant("default", outputs=("qemu",)),
    Variant("azure", inherits="default", outputs=("azure",)),
    Variant("azure-debug", inherits="azure", add=DevTools(), release=False),  # chained; never released
    Variant("prod", changes=(motd.ref.edit(content="prod\n"),)),  # motd = File.text("/etc/motd", ...)
    Variant("slim", changes=(Package.ref("jq").remove(),)),
    Variant("clouds", inherits="default", outputs=("azure", "gcp")),  # one variant, two disk images
    Variant("bare", inherits=None, add=packages("curl")),
)
```

- `inherits` is `"base"` (the default, meaning `Recipe.items`), another variant's name, or `None` for a standalone variant that inherits nothing.
- `add` is a fragment; `packages("strace", "gdb")` bundles packages without inventing a fragment name. Adding a declaration whose identity the variant already inherits with a different value is an `identity-collision` error.
- `changes` edits or drops inherited declarations by identity: `value.ref.edit(field=...)` sets fields (`edit-missing` if the variant inherits no such identity), `value.ref.remove()` drops one (`remove-missing`), and `Type.ref(key)` (`Unit.ref("ssh.service")`) names one you hold no value for. `Disk(key=key)` and `Secrets(store=disk)` hold refs too, so `key.ref.edit(size=32)` is the whole change for a smaller key.
- `outputs` names the targets the variant builds (`qemu`, `azure`, `gcp`; one disk image each), inherited from the variant it inherits when empty; cloud targets add their platform integration. A child cannot drop a cloud target it inherits (`target-inconsistent`).

`Setting`s and the `Kernel` belong to the variant that declares them; declare them in `Recipe.items` to share them. A variant with its own settings or kernel lowers standalone. See [Variants and targets](docs/concepts.md#variants-and-targets).

## Reproducibility

- **Byte-stable trees.** `Debian(..., epoch=0)` (the default) emits a fixed `SourceDateEpoch`/`SOURCE_DATE_EPOCH`, a stable `Seed` and strips `IMAGE_VERSION`. `tundravm compile --check` exits 1 when the committed tree differs from the recipe; `tundravm.testing.assert_tree` does the same in a test.
- **Lock sections.** `tundravm lock` writes a version 4 lockfile with one digest per section: `distribution`, `compiler`, and per variant `packages`, `files`, `kernel`, `debloat`, ... (`variants.<variant>.packages`). The recipe digest includes the tundravm version. `lock --check` prints the drift (`~ variants.default.packages: +htop`) and exits 1. A lock of every variant covers `--variant` subsets.
- **Pinned archive.** `Debian("trixie", snapshot="20251113T083151Z")` builds packages and the tools tree from that snapshot.debian.org snapshot (mkosi's `Snapshot=`); `mirror` and `tools_mirror` are mirror roots mkosi completes. `Debian(..., efi_stub=version)` pins the EFI stub to a version that same snapshot carries.
- **Pinned sources.** Every `Build` source, and a built kernel's, is resolved to a commit (`Git`) or a hash (`Http`) at lock time. `tundravm fetch` checks exactly those pins out on the host, as you, into `build/.sources/`; `bake` fetches first and mounts the checkouts into the build, so the build sandbox never fetches and an air-gapped bake needs `tundravm fetch` first (sources and, when the host has the toolchain, dependency caches), `bake --offline`, and a reachable local package mirror. Checkouts and dependency caches are verified before reuse, and `fetch --force` replaces a modified checkout and refills the caches. `lock --update NAME` re-resolves one source (`<variant>/<name>` for a build pinned per variant); `lock --offline` never touches the network. `fetch` also prefetches each Go, Cargo and .NET build's dependencies with the host toolchain into `build/.sources/deps/` and downloads the `EfiStub` package, so `bake --offline` (mkosi `--with-network=no`) builds with no network in the sandbox.
- **Frozen bakes.** `bake` is frozen against `--lockfile`, the `lockfile` of `[tool.tundravm]` (which must exist), or `build/tundravm.lock` whenever it exists, and refuses a recipe that drifted (`E_LOCKFILE`).

See [`docs/reproducibility.md`](docs/reproducibility.md).

## CLI

| Command | Does |
|---|---|
| `init [DIR]` | Scaffold a recipe, tests, `pyproject.toml` and a `.gitignore` block, then probe the backend (`--ci github` adds a workflow: a `check` job and an opt-in `bake` job) |
| `inspect RECIPE` | Dry run per variant (`--format text\|json\|markdown`); `--variant V --why SUBJECT` explains one emitted object (a path, `unit:`, `package:`, `hook:`, `init:`) |
| `lint RECIPE` | Every diagnostic; exit 1 on errors (`--strict`: also warnings) |
| `compile RECIPE` | Emit the mkosi tree into `--tree DIR`; `--check` exits 1 if the tree there is stale |
| `diff RECIPE` | Unified diff from the compiled tree at `--tree DIR` to the recipe (`--stat`) |
| `lock RECIPE` | Write the lockfile; `--check` reports drift, `--update`, `--offline`, `--explain` |
| `fetch RECIPE` | Check the pinned sources out on the host into `BUILD/.sources/` (`--build DIR`) and prefetch Go, Cargo and .NET dependencies |
| `bake RECIPE` | Fetch, then build the variants into `--build DIR`; `--backend`, `--lockfile`, `--no-fetch`, `--offline` (no network in the build sandbox), `-v`/`-q`/`--json-logs`; `--verify-reproducible` bakes twice and compares artifacts |
| `measure` | Expected measurements of a variant baked into `--build DIR` (`--scheme rtmr\|azure\|gcp`, `--format text\|json\|markdown`); `--export-policy FILE` writes the verifier policy `Tdxs.from_policy()` reads |
| `deploy TARGET` | Deploy a variant baked into `--build DIR` to `qemu`, `azure` or `gcp` (`--param KEY=VALUE`; `--attach` keeps QEMU in the foreground) |
| `attest` | Ask a running image's `tdxs` issuer (`--issuer`) for a nonce-bound quote, have a `tdxs` validator (`--validator`) judge its signature and collateral, and check MRTD and RTMR0..RTMR3 against a policy (`--policy FILE`). `trusted` needs the validator; without one the verdict is `measurements-match`/`measurements-mismatch`; a simulator is `simulated` (exit 1 unless `--allow-simulated`) |
| `sbom` | Bill of materials of a variant baked into `--build DIR`: mkosi's package manifest, the lockfile's source pins and the recipe metadata (`--format spdx-json\|cyclonedx-json\|text\|markdown`, `--output FILE`); bound to the bake's lockfile and manifest (`--allow-unbound`) |
| `evidence [RECIPE]` | An auditor's record of a bake in `BUILD/evidence/`: recipe and tree digests, the lockfile with its drift, each artifact re-hashed, the reproducibility outcome, the measurements policy, SPDX SBOMs, lint and provenance summaries, tool versions, indexed by `evidence.json`; `--bundle FILE.tar.gz` (deterministic under `SOURCE_DATE_EPOCH`), `--html FILE`; a pass, fail or incomplete verdict with stable reason codes, `--require reproducible,measured,real,lock` makes missing checks count; exit 1 unless the verdict is `pass`; `--sign KEY` writes an SSH signature (`evidence.json.sig`) |
| `verify EVIDENCE` | Checks an evidence bundle or directory on its own, no recipe or checkout: every member against `evidence.json`, the index against its members, the artifacts in `--build DIR` re-hashed, `evidence.json.sig` against `--allowed-signers FILE`, `--require` checks; one pass/fail/missing per check with a reason code; exit 1 unless all pass |
| `changes [RECIPE] --since EVIDENCE` | What changed since a release's evidence: recipe and tree digests, lockfile sections, source pins, packages, emitted files with the declarations behind them, measured registers; `--format markdown` for a pull request |
| `explore [RECIPE]` | One self-contained HTML page that explains the recipe: settings, lint, lock state, bake artifacts, a declarations × variants matrix with provenance, each variant's tree with contents and the declarations behind every file, hooks and builds in run order; `BUILD/explore.html` or `--output`; `--format json` |
| `release [RECIPE] --version X` | Cut `releases/<recipe>-<X>/` from the locked recipe: lock check, fetch, a bake checked by a second build, measure, SBOMs, signed evidence, verify, changes since the last release, `RELEASE.md`; resumable, immutable once it passes; exit 1 when the evidence, verify or a `--require` check fails |
| `witness RELEASE_DIR` | Rebuild a release from your own checkout with its own lockfile and sign whether every artifact's sha256 matches (`--builder`, `--sign KEY`); `verify RELEASE_DIR --min-witnesses N` then needs N distinct signed witnesses, the publisher excluded |
| `doctor [RECIPE]` | Probe the host tools a backend needs; with a recipe, lint it too |
| `ci RECIPE` | `lint --strict`, `compile --check` and `lock --check`; stop at the first failure |
| `watch [RECIPE]` | Lint the recipe and compare it with its committed tree (`--tree`, else `[tool.tundravm]` `tree`, else `BUILD/mkosi`) at start and whenever the recipe or a `.py` beside it changes, one line per check; never writes unless `--write`; `--interval SECONDS`; Ctrl-C exits 0 |
| `import TREE` | Generate a recipe module from an existing mkosi tree; prints coverage, notes and whether it compiles back (`--output FILE`, `--name`, `--dialect`) |
| `status [RECIPE]` | Read-only report of each lifecycle step and the next command to run (`--verify` hashes the artifacts) |
| `clean [RECIPE]` | Remove output of the build directory (`--build DIR`) by part (`--what sources,tree,artifacts,evidence,state,reproduce` or `--what all`); without `--what` it lists what `all` would remove |
| `config [RECIPE]` | The `recipe`, `tree`, `build`, `lockfile` and `backend` the commands resolve, whether each came from a flag, `[tool.tundravm]`, the default or (the backend) the recipe file, and whether the recipe and lockfile exist |
| `completion SHELL` | Print a bash, zsh or fish completion script; `inspect RECIPE --diff-variants A B` lists what differs between two variants |

`--variant NAME` is repeatable on every recipe command; omitting it selects every variant. `RECIPE` may be omitted wherever a `pyproject.toml` with a `[tool.tundravm]` table is found in the working directory or above it; `--tree DIR` is always the compiled mkosi tree, `--build DIR` the build directory (default: the table's `build`, else `build`) and `--output FILE` a document written. See [`docs/cli.md`](docs/cli.md) for flags, recipe loading, output formats and exit codes.

## Backends

| Backend | When to use |
|---|---|
| `LimaMkosiBackend` | Default. Runs mkosi inside a Lima VM with Nix. macOS and Linux. |
| `NixMkosiBackend` | Linux with [Nix](https://nixos.org/download.html). Runs mkosi via `nix develop` on the host. |
| `LocalLinuxBackend` | Linux with `mkosi` (v25+) on `PATH` and `sudo` or `unshare`. No Nix. |
| `InProcessBackend` | Tests and tutorials. Writes simulated artifacts, needs no tools. |

A recipe file binds its backend to a module-level `backend`; `tundravm bake --backend lima|nix|local|inprocess` overrides it, and the Python `bake()` takes `Backend(kind)`. `inspect`, `lint`, `compile` and `lock` never need one. `tundravm doctor` reports which backends your host can run.

## Examples

| Example | What it teaches |
|---|---|
| [`01_minimal.py`](examples/01_minimal.py) | The smallest recipe: a base, packages and a file as the recipe's `items`; no variants means one QEMU `default` |
| [`02_variants.py`](examples/02_variants.py) | `Variant` overlays: `add`, `changes` that edit/remove by identity (refs), `inherits` chains, a standalone `inherits=None` variant, `outputs=("azure", "gcp")`, `packages()` |
| [`03_fragments.py`](examples/03_fragments.py) | A reusable plain `Fragment`, `@component` classes with `ConfigFile`-rendered config, `requires=`, a lint `check` and an exported `HttpEndpoint` consumed by a health check |
| [`04_storage_and_secrets.py`](examples/04_storage_and_secrets.py) | TPM-sealed `Key`, encrypted `Disk`, `Secrets` with `Schema`s and file/env targets an `ExternalDelivery` writes, `RuntimeTools`, `Partition` |
| [`05_source_builds.py`](examples/05_source_builds.py) | `Build` from an `Http` tarball `using` the `Go` toolchain and from a `Git` tag with a script; file and directory installs; `cache_namespace` |
| [`06_attestation.py`](examples/06_attestation.py) | `Tdxs()` as attester and as verifier built with `Tdxs.from_policy()` from `peer.policy.json` (a placeholder export of `measure --export-policy`), plus `Policy(require_frozen_lock=True)` |
| [`nethermind_base.py`](examples/nethermind_base.py) | The nethermind-tdx base layer as one `Composite`: TDX kernel, EFI stub, backports, debloat, `Tdxs()` |
| [`fragments/`](examples/fragments/) | `Raiko()`, `TaikoClient()`, `Nethermind()`: each with a `Build`, a user, a unit and an env file |
| [`surge-tdx-prover/`](examples/surge-tdx-prover/) | The flagship: the full nethermind-tdx image, byte-identical to the committed tree for all four variants |

Every example loads, lints without errors and compiles deterministically; `tests/lint/test_examples_api.py` enforces it. Check the flagship against its golden tree with `tundravm compile examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi --check`.

## Documentation

| Doc | Contents |
|---|---|
| [`docs/concepts.md`](docs/concepts.md) | The model in one recipe, the concepts, resolution, lowering, runtime-init, keys/disks/secrets, variants, lifecycle |
| [`docs/tutorial.md`](docs/tutorial.md) | From `tundravm init` through `watch`, bake, measure, deploy, a fragment, a test and the CI workflow, with real output |
| [`docs/cli.md`](docs/cli.md) | Every command and flag, recipe loading, output formats, exit codes, CI |
| [`docs/api.md`](docs/api.md) | Every declaration, lifecycle function, result type, error and lint rule |
| [`docs/module-authoring.md`](docs/module-authoring.md) | Writing fragments: `requires`, `checks`, boot-step ordering (`needs=`, `.ready`), builds, tests |
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

Every SDK error prints `error [E_CODE]: message`, an optional `Hint:` and context lines, and exits 2. Under `--format json` stderr gets one JSON line instead: `{"schema":"tundravm.error/1","code":...,"message":...,"hint":...,"context":{...}}`.

| Error | Fix |
|---|---|
| `E_VALIDATION` | A declaration is malformed, or the recipe has error diagnostics. `tundravm lint RECIPE` lists them all. Also raised for an unknown `--variant` |
| `E_LINT` | `bake` refused a recipe with error-level compiler findings. Run `tundravm lint RECIPE` |
| `secret-in-file` (lint) | A `File`, `Template` or `FileTree` ships what looks like a private key, token or password, which every copy of the image would carry. Deliver it at boot with `Secrets` and a `SecretFile` (or `SecretEnv`; `secret-in-env` is the same for `Service(env=...)` and unit files) under `delivery=ExternalDelivery(...)` naming what writes it (the built-in `tundra-tools` delivery writes only the SSH key: `secret-target-unsupported`), or pass `allow_secret=True` to the declaration when the value only looks like a credential |
| `E_LOCKFILE` | The recipe drifted from the lockfile, or `bake` found that the lockfile `[tool.tundravm]` configures does not exist. `tundravm lock RECIPE --check` shows the drift; `tundravm lock RECIPE` accepts it or writes the missing lockfile |
| `E_STATE` | No `bake-result.json` where `measure`/`deploy`/`sbom`/`evidence` looked, or no artifact for that variant/target: bake first, and pass the bake's `--build` directory. Or `bake --no-fetch` found a source checkout missing, or `bake --offline` a dependency cache missing or incomplete (the error names the entry): run `tundravm fetch RECIPE` |
| `E_MEASUREMENT` | No `measured-boot`/`dstack-mr` on `PATH`, or a simulated artifact. Install a tool, or pass `--allow-placeholder` for test values |
| `E_ATTESTATION` | `attest` could not reach the tdxs issuer or validator, or either failed |
| `E_DEPLOYMENT` | A simulated artifact (`--allow-simulated-artifact` for a test run), an artifact of another target, or a missing target tool (`qemu-system-x86_64`, `az`, `gcloud`) |
| `E_ARTIFACT_CHANGED` | The artifact's bytes no longer match the sha256 the bake recorded (`measure`, `deploy`, `status --verify`). Bake again |
| `E_SOURCE` | A source could not be resolved, or a checkout in `build/.sources` changed since the fetch: `tundravm fetch RECIPE --force` |
| `E_POLICY` | A `Policy` setting refused the operation (frozen lock, offline network) |
| `E_BACKEND_EXECUTION` | The build failed. Run `tundravm doctor RECIPE`; check the mkosi version (>= 25) and platform |
| `E_REPRODUCIBILITY` | Two builds that should match did not |
