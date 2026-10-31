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

Python SDK for building, measuring and deploying TDX-enabled VM images. An image is an immutable Python value: an `Image` that names what it runs and nothing about how it is built. Everything a confidential image needs and no one wants to repeat is implied — Debian trixie from a locked snapshot, a measured UKI, NTS time, a RAM-only journal, a firewall that opens only what a service exposes, one hardened system user per service, and the evidence service an attestation reads. tundravm resolves it, lints it, compiles it to a byte-reproducible [mkosi](https://github.com/systemd/mkosi) project tree, pins every source in a lockfile, and builds the tree into an immutable build directory: a bootable disk for QEMU, Azure or GCP, its measurements, its SBOM and a signed manifest. The `tundravm` command is the operator's loop in twelve commands: `new`, `check`, `lock`, `build`, `show`, `run`, `deploy`, `attest`, `secrets`, `verify`, `convert`, `doctor`.

## Install

tundravm is not on PyPI yet: install it from git with pip or uv. mkosi comes with it: tundravm depends on mkosi 27.1 (pinned to the release tag, since mkosi is not on PyPI either) and runs it in-process, so there is no separate mkosi to install. To start a project without installing anything first:

```bash
uvx --from git+https://github.com/Hyodar/tundra tundravm new my-image --ci
cd my-image
uv sync              # installs the pinned tundravm (and pytest) into .venv, writes uv.lock
git add uv.lock      # committed, the generated workflow installs exactly it (`uv sync --frozen`)
```

`new` writes `dependencies = ["tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"]` into `pyproject.toml`, pinned to the commit of the tundravm that ran it (read from its install metadata; when that is unknown the line follows `master` and the comment above it says so), so `uv sync` installs the same tundravm on a fresh machine or a CI runner. `git` must be on `PATH`. In a project that already has a `pyproject.toml`, add it yourself: `uv add "tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"`, and a `[tool.tundravm]` table with `image = "image.py"`. To work against a local checkout, `uv add --editable PATH/TO/tundravm` replaces the git dependency; in this repository, `uv sync` installs the `tundravm` command (see [Development](#development)). [Installing tundravm in the project](docs/cli.md#installing-tundravm-in-the-project) has the details.

## Quickstart

```python
# demo.py
from tundravm import TCP, File, Image, Service

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
```

That is the whole image. `app.service` runs as the generated system user `app`, under the
tested `static` hardening preset, restarting on failure; the firewall opens `8080` because
the service exposed it, and the management ingress on `:8745` because every image reserves
it. `Image`'s docstring lists each implied item with its one escape hatch, and
`tundravm show` prints the resolved result. The file names no build backend: Linux
x86-64 builds locally and every other host in Lima, unless `TUNDRAVM_BACKEND` or the project
file's `backend` says otherwise. The operator transcript for it:

```console
$ tundravm new demo && cd demo
$ tundravm build                       # lock written, payload.efi + qemu.raw, measurements, manifest
$ tundravm run --mode simulated --dev  # instance demo-1; :8080 and :8745 forwarded; verdict: simulated
$ curl http://127.0.0.1:8080/          # <h1>demo</h1>
$ tundravm run stop demo-1
```

The build lands in `build/demo/production/<build-id>/` (the dev build in
`build/demo/development/<build-id>/`): `payload.efi`, `qemu.raw`, `measurements.json`,
`sbom.spdx.json`, `inputs/`, `tree/`, `manifest.json` and `observations.json`, published at
once and never written again; `<build-id>` is the digest of the manifest.

## A confidential prover

```python
# prover.py
from tundravm import AttestedTLS, DevAccess, Image, OperatorKey, RootFS, Secret, Volume
from tundravm.components import Raiko

data = Volume("data", mount="/persistent", unlock=OperatorKey())
config = Secret("raiko-config", rotation="restart")
raiko = Raiko(config=config, state=data.directory("raiko"), listen="127.0.0.1:8080")

image = Image(
    "prover",
    components=[raiko],
    volumes=[data],
    secrets=[config],
    ingress=[AttestedTLS(443, upstream=raiko.http)],
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
```

`data` is a LUKS2 volume found by its attachment name (`tundravm-data`) whose passphrase an
operator supplies after attesting the instance, so Raiko's state never opens on a machine
nobody checked; `TpmSeal(platform=, pcrs=, recovery=)` seals it to a stated vTPM instead.
`config` is a secret delivered after attestation and read by Raiko as a systemd credential:
nothing is written into the image. `Raiko` is a component of the shipped library
(`tundravm.components`) that pins its revision and Cargo toolchain, runs in the `tdx` group
after the evidence service, and exports `raiko.http` with its readiness.
`AttestedTLS(443, upstream=raiko.http)` is the only way requests reach it: Raiko listens on
the loopback, and an ingress fronting a port some service also exposes is refused.
`RootFS.verity()` puts the large payload on a dm-verity erofs root. `DevAccess` lives only in
the non-production `dev` profile. The caller never names a unit, a port or a key file inside
any of it; `docs/api.md` has every slot and what it lowers onto, and the attested channel that
delivers the passphrase and the secret is tundra-tools' next step (plan gate 3).

```bash
tundravm lock --update raiko                      # pin the Raiko revision and toolchain in build/tundravm.lock
tundravm check --tree mkosi                       # lint, lock drift, and the committed tree against the image
tundravm build --target gcp --reproduce --sign release-key   # payload.efi, qemu.raw, gcp.tar.gz, built twice, signed
tundravm verify latest --require-signature --allowed-signers allowed_signers   # re-check it anywhere
tundravm deploy gcp --build build/prover/production/<id> --volume data=create:100GiB   # instance prover-1
tundravm attest prover-1 --policy policies/prover-gcp.json --output attest-1.json  # quote vs policy, delivers nothing
tundravm secrets unlock prover-1 data             # OperatorKey(): the passphrase after a fresh attestation
tundravm secrets deliver prover-1 --policy policies/prover-gcp.json raiko-config=@raiko.json
tundravm show --diff <id> <id2>                   # packages, files, units, boot payload, measurements
tundravm deploy upgrade prover-1 --build build/prover/production/<id2> --policy policies/prover-gcp.json
```

Run the commands with `uv run` in the project (or with its `.venv` activated). `build` refuses a stale lock and writes a missing one; it builds one profile (`--profile P`, `--dev`) into an immutable directory whose `manifest.json` binds every artifact, the UKI and its root hash, the lock, the tree, the source pins, the package manifest, the SBOM and the toolchain, and whose `observations.json` records when, where and whether an independent rebuild (`--reproduce`) matched. `measurements.json` holds a prediction per target and boot path, each with a `tundravm.policy/2` an operator approves into `policies/` (the platform registers MRTD and RTMR0 come from a quote of a host you trust). `run --mode simulated` boots a build locally with no TDX; `run --mode tdx` needs a TDX host (`tundravm doctor --for tdx`). `attest` and `secrets` reach an instance over today's control-plane bootstrap: the operator's SSH key POSTed to the management port, an **unverified first contact** until the attested endpoint lands (plan gate 3); values travel on the SSH command's stdin only. [docs/cli.md](docs/cli.md) has every command and flag.

`new --template service|prover` picks the starter image (default `service`; `prover` is the app behind an `AttestedTLS` ingress with its state on an operator-unlocked volume and a delivered secret, plus `policies/`). `new` writes a `[tool.tundravm]` table (`image`, `build`) into `pyproject.toml` that every command reads, so `IMAGE` is optional in a project; `TUNDRAVM_IMAGE`, `TUNDRAVM_BUILD` and `TUNDRAVM_BACKEND` override it.

`new --ci` writes `.github/workflows/tundravm.yml` with two jobs. `check` runs on every push to `main` and every pull request: it posts `tundravm show` to the job summary, then runs `check --tree mkosi` and `lock --check`. `build` is opt-in: it runs after `check` only when the repository variable `TUNDRAVM_BACKEND` names a backend (`local` or `inprocess`), builds with `--reproduce`, runs `verify latest` and uploads the build directory.

## Why

Hand-maintained mkosi trees for TDX images are hard to review, drift easily, and are painful to keep reproducible across cloud targets. A recipe gives you:

- **Deterministic output.** The same recipe always compiles to the same mkosi tree, byte for byte. `check --tree DIR` fails CI when the committed tree is stale.
- **A TEE baseline.** `TeeBase()` declares what a confidential VM on an untrusted host needs, each piece behind a flag: `chrony` over NTS (the host cannot set the clock), DHCP through systemd-networkd (`Network()`), an nftables `Firewall()` that drops inbound traffic except the ports the variant's services declare, a RAM-only journal, sysctl hardening, a pinned resolver and masked apt timers and tty1 getty. The `service`, `cloud` and `prover` templates start from it.
- **Strict, typed systemd units.** `Service`, `Task` (a oneshot at boot or on a timer), `Service(socket=Listen(...))`, `Mount` and `Unit(dropin=DropIn(...))` render unit files from typed fields, nobody writes unit text; `Service` and `Task` default to a dedicated system user, the `Hardening.strict()` sandbox and a wait for the boot steps they need, and a service restarts on failure. `tundravm convert PATH --from systemd` turns existing unit text into these declarations.
- **One image, every cloud.** `Variant("default", outputs=("qemu", "azure", "gcp"))` builds one UKI and wraps it as an Azure VHD and a GCP tar.gz; the cloud glue is gated at boot (`ConditionVirtualization=microsoft`/`google`), so one set of measurements covers every output.
- **Composition without mutation.** Fragments are values: a plain `Fragment(...)`, or a `@component` class configured by its fields. Keys, disks and secrets reference each other as objects, and the runtime-init sequence they need is derived, not hand-ordered.
- **Composable kernels.** The distribution kernel keeps only the drivers the variant's outputs boot on (`Debian(kernel_modules=)` adds more), or `Kernel(version, source, base=DebianConfig(...), patches=(KernelPatch(...),))` builds one from Debian's config, patches and `KernelConfig` fragments any component can add (`KernelHardening()`), and fails the build when Kconfig drops a requested option.
- **Locked inputs.** The lockfile records a digest per recipe section, the build toolchain and a commit or hash per source build. Builds are frozen against it, and `lock --check` names what drifted.
- **Build records.** A build directory is the auditor's record: the canonical, optionally signed `manifest.json` (artifact index, UKI and root hash, lock, tree digest, sources, packages, SBOM with the Go modules and Rust crates embedded in built binaries, measurements, toolchain, and an explicit `unchecked` list), `observations.json` with the reproduction outcome, and `tundravm verify` to check it on any machine, with `--rebuild` for an independent rebuild.
- **Secrets lint.** `secret-in-file` and `secret-in-env` refuse private keys, cloud and GitHub tokens and literal passwords baked into files, units or service environments, naming the kind and line but never the value; credentials go in `Secrets` and arrive at boot, written by the provider `Secrets(delivery=)` names (`secret-target-unsupported` refuses a target nobody writes: the built-in delivery writes only the SSH key).

The [`surge-tdx-prover`](examples/surge-tdx-prover/) example is the [NethermindEth/nethermind-tdx](https://github.com/NethermindEth/nethermind-tdx) prover stack as a 33-line `Image` over `tundravm.components` (`Nethermind`, `TaikoClient`, `Raiko`): their state on an operator-unlocked volume, the Engine API JWT and both configurations delivered, Raiko behind `AttestedTLS`. It compiles byte-for-byte to its committed mkosi-native tree (`examples/surge-tdx-prover/mkosi`): one verity-rooted payload with the Azure and GCP glue, the QEMU disk, the `azure` and `gcp` wrappers and the `dev` profile.

## Concepts in one screen

| Concept | What it is |
|---|---|
| **Recipe** | The whole image: its `platform` (`Debian(release, snapshot=, boot=, efi_stub=)`: the distribution, archive, arch, epoch and the packages every variant boots with), its own `items` and the variants. An immutable value bound to `recipe` in a Python file. |
| **Declaration** | One typed fact about the image: `Package`, `File`, `FileTree`, `Template`, `User`, `Group`, `Service`, `Task`, `Mount`, `Unit`, `Hook`, `BootStep`, `Key`, `Disk`, `Secrets`, `Firewall`, `Build`, `Kernel`, `KernelConfig`, `Setting`, ... Identified by type plus natural key (package name, file path, unit name); `value.ref` (or `Type.ref(key)`) names it in a variant's `changes`. |
| **Fragment** | A named group of declarations and nested fragments, with `requires` (fragments that must also be present) and `checks` (lint functions). A reusable "module" is a `@component` class: a `Fragment` whose fields are its configuration. |
| **Variant** | An overlay on its parent: `add` a fragment, edit or remove inherited declarations by ref (`changes`), name its `outputs` (`qemu`, `azure`, `gcp`). One mkosi directory per variant and one artifact per output. |
| **Lock** | Section digests of the resolved recipe plus a pin per source build. `tundravm.lock`, committed next to the tree. |
| **Tree** | The compiled mkosi project, held in memory until written. Its digest is what `check --tree` and golden tests compare. |
| **Build** | An immutable directory of one profile's artifacts, measurements, SBOM, inputs and tree, identified by its manifest's digest. `run`, `deploy` and `verify` take a build. |

See [`docs/concepts.md`](docs/concepts.md) for resolution rules, runtime-init ordering, the TEE baseline, kernels and the lifecycle.

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

`PrometheusExporter(storage=storage, disk=disk)` is itself a `Fragment`; its boot step runs once the disk is mounted, and the unit starts once the step has run (a service without `needs=` waits for every boot step; a unit waits only for its `needs=`). A one-off with nothing to configure can stay a plain `Fragment(...)` value. Identical fragments included twice expand once; two different fragments with the same name are an error. `tundravm.declarative.utils` ships `TeeBase()` (the confidential-VM baseline, with `Network()` and `Firewall()`), `Tdxs()` (the attestation quote service), `DevTools()` (serial console and root login, never ship it), `KernelHardening()` (hardening kernel config), `GoBinary(name, source, package=)` and `CargoBinary` (a program built from source, installed at its `.executable`), `EfiStub(snapshot=, version=)` and `Backports()`. A component can also export typed values, such as an `HttpEndpoint`, that another component takes as a field instead of parsing its files, and `ConfigFile.yaml`/`toml`/`json`/`ini`/`env` render config files from data as ordinary `File`s. The full example is in [`docs/module-authoring.md`](docs/module-authoring.md).

Keys, disks and secrets are declarations that reference each other by object, so a disk cannot name a key that does not exist:

```python
key = Key("key_persistent")
disk = Disk("disk_persistent", mount="/persistent", key=key)
storage = Fragment("secure-storage", items=(key, disk, Secrets(store=disk)))
```

## Variants

```python
variants=(
    Variant("default", outputs=("qemu",)),  # the payload, in the QEMU raw disk
    Variant("cloud", outputs=("azure", "gcp")),  # the same UKI in an Azure VHD and a GCE tar.gz
    Variant("debug", inherits="default", add=DevTools(), release=False),  # never released
    Variant("prod", changes=(motd.ref.edit(content="prod\n"),)),  # motd = File.text("/etc/motd", ...)
)
```

- `inherits` is `"base"` (the default, meaning `Recipe.items`) or the default variant's name. Every variant boots the default variant's payload plus what it adds: one that adds content compiles to an mkosi profile overlay (`payload/mkosi.profiles/<name>/`, selected with `--profile`), one that only names outputs to their wrapper images (`--dependency`).
- `add` is a fragment; `packages("strace", "gdb")` bundles packages without inventing a fragment name. Adding a declaration whose identity the variant already inherits with a different value is an `identity-collision` error.
- `changes` edits inherited declarations by identity: `value.ref.edit(field=...)` sets fields (`edit-missing` if the variant inherits no such identity), and `Type.ref(key)` (`File.ref("/etc/motd")`) names one you hold no value for. A profile can only add to the payload, so a variant compiles when it replaces nothing but a `File`, `Repository` or `Debloat` and removes nothing; a different image (another key size, a removed package, a standalone `inherits=None`) is another recipe.
- `outputs` names the targets the variant builds (`qemu`, `azure`, `gcp`), inherited from the variant it inherits when empty. The payload carries the integration of every cloud the recipe names, gated at boot by `ConditionVirtualization=` so it is inert elsewhere, and every output wraps the same UKI: one sha256 and one set of measurements. A child cannot drop a cloud target it inherits (`target-inconsistent`).

`Setting`s and the `Kernel` are the payload's: declare them in `Recipe.items`; a variant with its own does not compile. See [Variants and targets](docs/concepts.md#variants-and-targets) and [the compiled tree](docs/concepts.md#the-compiled-tree).

## Reproducibility

- **Byte-stable trees.** `Debian(..., epoch=0)` (the default) emits a fixed `SourceDateEpoch`/`SOURCE_DATE_EPOCH`, a stable `Seed` and strips `IMAGE_VERSION`. `tundravm check --tree DIR` exits 1 when the committed tree differs from the recipe; `tundravm.testing.assert_tree` does the same in a test.
- **Lock sections.** `tundravm lock` writes a version 5 lockfile with one digest per section: `distribution`, `compiler`, and per variant `packages`, `files`, `kernel`, `debloat`, ... (`variants.<variant>.packages`), plus the build `toolchain` (mkosi and its commit, the tools tree's distribution, release and snapshot, tundravm, the measurement tools) that `check` compares and every build manifest records. The recipe digest includes the tundravm version. `lock --check` prints the drift (`~ variants.default.packages: +htop`) and exits 1. A lock of every profile covers `check --profile` subsets.
- **Pinned archive.** `Debian("trixie", snapshot="20251113T083151Z")` builds packages and the tools tree from that snapshot.debian.org snapshot (mkosi's `Snapshot=`); `mirror` and `tools_mirror` are mirror roots mkosi completes. `Debian(..., efi_stub=version)` pins the EFI stub to a version that same snapshot carries.
- **Pinned sources.** Every `Build` source, and a built kernel's, is resolved to a commit (`Git`) or a hash (`Http`) at lock time. `build` checks exactly those pins out on the host, as you, into the image's workspace and mounts the checkouts into the build, so the build sandbox never fetches; `build --offline` builds only from what the lock pins. Checkouts and dependency caches are verified before reuse, and `build --clean-cache` starts the workspace afresh. `lock --update NAME` re-resolves one source (`<variant>/<name>` for a build pinned per variant); `lock --check` never touches the network.
- **Frozen builds.** `build` is frozen against `BUILD/tundravm.lock` and refuses an image that drifted from it (`E_LOCKFILE`); it writes the lock only when there is none. `build --reproduce` builds again in an independent workspace and records whether every artifact matched.

See [`docs/reproducibility.md`](docs/reproducibility.md).

## CLI

| Command | Does |
|---|---|
| `new NAME` | A project: the image file, tests, `pyproject.toml`, `.gitignore` (`--template service\|prover`, `--ci` adds a workflow) |
| `check [IMAGE]` | Declarations, every lint rule, drift from the lock and the toolchain; `--tree DIR` compares a committed tree; exit 1 on errors |
| `lock [IMAGE]` | Write `BUILD/tundravm.lock`; `--update [INPUT...]` re-resolves, `--check` reports drift and exits 1 |
| `build [IMAGE]` | One profile (`--profile P`, `--dev`) into `BUILD/<image>/production/<id>/`; `--target azure,gcp`, `--reproduce`, `--sign KEY`, `--offline`, `--clean-cache`; `--tree-only DIR` writes the mkosi tree |
| `show [BUILD]` | The resolved image or a build (`--units`, `--tree`, `--measurements`, `--json`); `--diff OLD NEW` between two builds |
| `run [BUILD] --mode simulated\|tdx` | A local instance (`--dev`, `--detach`); `run status\|logs\|stop\|ssh\|mode INSTANCE` |
| `deploy azure\|gcp --build BUILD` | A cloud TDX VM with a receipt (`--volume NAME=create:SIZE\|existing:ID`); `deploy status\|logs\|mode\|upgrade\|rollback INSTANCE` |
| `attest INSTANCE --policy FILE` | Fresh evidence against a policy (`--output REPORT`, `--validator`); delivers nothing |
| `secrets deliver\|unlock INSTANCE` | Credentials (`--policy FILE NAME=@FILE...`) or a volume's passphrase, after a fresh attestation |
| `verify BUILD` | The build directory against its manifest (`--require-signature`, `--allowed-signers FILE`, `--rebuild`) |
| `convert PATH --from mkosi\|systemd` | An mkosi tree as an `Image` file (`--allow-scripts`), or unit files as typed declarations |
| `doctor` | What this host can build, run, deploy and verify (`--for build\|run\|tdx\|azure\|gcp`) |

Exit codes: 0 success, 1 a failed check, 2 an SDK or usage error. See [`docs/cli.md`](docs/cli.md) for every flag, the build directory and manifest schema, receipts and the control-plane bootstrap; shell completion is `python -m tundravm.completion bash|zsh|fish`.

## Backends

| Backend | When to use |
|---|---|
| `LocalLinuxBackend` | x86-64 Linux. Runs the pinned mkosi in-process; needs `apt`/`dpkg` with `debian-archive-keyring`, and unprivileged user namespaces or `sudo`. Every other build tool comes from mkosi's tools tree. |
| `LimaMkosiBackend` | macOS (and non-x86-64 Linux). Runs the same mkosi in an x86-64 Debian Lima guest; on Apple Silicon the guest is emulated (QEMU TCG): same bytes, several times slower. |
| `InProcessBackend` | Tests and tutorials. Writes simulated artifacts, needs no tools. |

An image file names no backend: the loader picks `LocalLinuxBackend` on Linux x86-64 and `LimaMkosiBackend` everywhere else, `TUNDRAVM_BACKEND=local|lima|inprocess` or the project file's `backend` overrides that, and a file that still binds a module-level `backend` keeps it unless `TUNDRAVM_BACKEND` names another. The Python `bake()` takes `Backend(kind)`. A build never switches backend on failure. `check`, `show`, `lock` and `build --tree-only` never need one. `tundravm doctor` prints what this host can build, run, deploy and verify, one row per capability, each failure naming its fix (`tundravm doctor --for build`).

## Examples

| Example | What it teaches |
|---|---|
| [`01_minimal.py`](examples/01_minimal.py) | The smallest recipe: a base, packages and a file as the recipe's `items`; no variants means one QEMU `default` |
| [`02_variants.py`](examples/02_variants.py) | `Variant`s as additions to one payload: `add`, `changes` that edit by identity (refs), `outputs=("azure", "gcp")` wrappers around the same UKI, `packages()` |
| [`03_fragments.py`](examples/03_fragments.py) | A reusable plain `Fragment`, `@component` classes with `ConfigFile`-rendered config, `requires=`, a lint `check` and an exported `HttpEndpoint` consumed by a health check |
| [`04_storage_and_secrets.py`](examples/04_storage_and_secrets.py) | `Volume` with `OperatorKey()` and `TpmSeal(...)` (and why the seal limits the targets), `volume.directory()` as a service's state, required and optional `Secret`s read as credentials |
| [`05_source_builds.py`](examples/05_source_builds.py) | `Build` from an `Http` tarball `using` the `Go` toolchain and from a `Git` tag with a script; file and directory installs; `cache_namespace` |
| [`06_attestation.py`](examples/06_attestation.py) | The evidence service replaced by `Tdxs.from_policy()` (attester and verifier of `peer.policy.json`), a service that requires `EVIDENCE`, and an `AttestedTLS` ingress |
| [`nethermind_base.py`](examples/nethermind_base.py) | The nethermind-tdx base layer as an `Image`: snapshot, EFI stub, the TDX kernel built from source |
| [`surge-tdx-prover/`](examples/surge-tdx-prover/) | The flagship: Nethermind, the taiko driver and Raiko from `tundravm.components`, byte-identical to its committed tree |

Every example loads, lints without errors and compiles deterministically; `tests/lint/test_examples_api.py` enforces it. Check the flagship against its golden tree with `tundravm check examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi`.

## Documentation

| Doc | Contents |
|---|---|
| [`docs/concepts.md`](docs/concepts.md) | The model in one recipe: platform, items, services and tasks, runtime-init, keys/disks/secrets, variants and resolution, networking and the TEE baseline, kernels, lowering, lifecycle, release and trust |
| [`docs/tutorial.md`](docs/tutorial.md) | A walk through a project, a fragment, a test and the CI workflow, with real output |
| [`docs/cli.md`](docs/cli.md) | The twelve commands and every flag, build directories and manifests, receipts, exit codes |
| [`docs/api.md`](docs/api.md) | Every declaration, lifecycle function, result type, error and lint rule |
| [`docs/module-authoring.md`](docs/module-authoring.md) | Writing fragments: `requires`, `checks`, boot-step ordering (`needs=`, `.ready`), builds, tests |
| [`docs/testing.md`](docs/testing.md) | `tundravm.testing` helpers and pytest fixtures, booting a baked image under QEMU in a test |
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

`uv run pytest` skips the real bakes on a host that gives mkosi neither user namespaces nor non-interactive `sudo`, and the boots (`tests/integration/test_templates_boot.py`, marker `boot`) without QEMU and OVMF too. `tundravm.testing.boot()` boots a baked UKI under QEMU with OVMF and reports the units its serial console shows started and failed; CI's `boot` job bakes and boots the `minimal` and `service` templates on every push ([docs/testing.md](docs/testing.md#booting-an-image-in-tests)).

## Troubleshooting

Every SDK error prints `error [E_CODE]: message`, an optional `Hint:` and context lines, and exits 2. Under `--json` stderr gets one JSON line instead: `{"schema":"tundravm.error/1","code":...,"message":...,"hint":...,"context":{...}}`.

| Error | Fix |
|---|---|
| `E_VALIDATION` | A declaration is malformed, or the image has error diagnostics. `tundravm check` lists them all. Also raised for an unknown `--profile` |
| `E_LINT` | `build` refused an image with error-level compiler findings. Run `tundravm check` |
| `secret-in-file` (lint) | A `File`, `Template` or `FileTree` ships what looks like a private key, token or password, which every copy of the image would carry. Deliver it at boot with `Secrets` and a `SecretFile` (or `SecretEnv`; `secret-in-env` is the same for `Service(env=...)` and unit files) under `delivery=ExternalDelivery(...)` naming what writes it (the built-in `tundra-tools` delivery writes only the SSH key: `secret-target-unsupported`), or pass `allow_secret=True` to the declaration when the value only looks like a credential |
| `E_LOCKFILE` | The image drifted from `BUILD/tundravm.lock`, which `build` never updates. `tundravm lock --check` shows the drift; `tundravm lock` accepts it (`--update NAME` moves a pin) |
| `E_STATE` | No build where `show`, `run`, `deploy` or `verify` looked (`tundravm build` first, or pass `BUILD`), no instance of that name, or a source checkout or dependency cache missing for `build --offline` |
| `E_MEASUREMENT` | An artifact that is not a UKI or a disk booting one, or a placeholder policy given to `attest`. A simulated build records its measurements as `unchecked` instead. Measurement is built in: no external tool is needed |
| `E_ATTESTATION` | `attest` could not reach the tdxs issuer or validator, or either failed |
| `E_DEPLOYMENT` | A missing tool (`qemu-system-x86_64`, OVMF, `az`, `gcloud`), `run --mode tdx` on a host without TDX, a volume Azure cannot attach, or a refused `deploy upgrade` (the reason is named) |
| `E_ARTIFACT_CHANGED` | An artifact's bytes no longer match the sha256 recorded for it. Build again; `tundravm verify BUILD` names the file |
| `E_SOURCE` | A source could not be resolved, or a checkout in the workspace changed since the fetch: `tundravm build --clean-cache` |
| `E_POLICY` | A `Policy` setting refused the operation (frozen lock, offline network) |
| `E_BACKEND_EXECUTION` | The build failed. The error names mkosi's log (in `BUILD/<image>/.work/`); `tundravm build -v` echoes it; `tundravm doctor --for build` checks the host |
| `E_REPRODUCIBILITY` | Two builds that should match did not |
