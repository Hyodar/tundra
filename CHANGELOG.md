# Changelog

## Unreleased

tundravm v2 makes mkosi accessible for confidential VMs by making the choices for you:
Debian trixie, systemd, a measured UKI, Intel TDX. An image is one Python `Image` with a
few typed slots. What every confidential image needs is implied, and each implied default
has one escape hatch. The compiler writes one mkosi 27.1 tree that uses mkosi's own
composition, so the tree stays readable and buildable without tundravm. mkosi 27.1 is a
pinned dependency, run in-process.

The command line is the operator's loop in twelve commands. A build is an immutable
directory with a canonical, optionally signed manifest that `verify` checks anywhere.
`run`, `deploy`, `attest` and `secrets` work with explicit builds and write receipts.
Nothing here keeps a compatibility alias: every old name and verb is gone, and the
sections below say where each went. Real TDX quotes (gate 2) and the attested channel for
delivery and unlock (gate 3) are not done; until gate 3, `attest` and `secrets` say
`unverified first contact`.

### The mkosi-native tree

- One mkosi 27.1 project per image. The root `mkosi.conf` is the QEMU raw disk
  (`qemu.raw`: an ESP around `payload.efi` through `ExtraTrees=%O/payload.efi`).
  `mkosi.images/payload/` includes `payload/`, the OS: `mkosi.conf` (`Format=uki`), one
  `mkosi.conf.d/NN-<concern>.conf` per component, the trees and the `mkosi.<phase>.d/`
  scripts. `mkosi.images/azure/` and `mkosi.images/gcp/` wrap the same payload and
  convert it at postoutput into `azure.vhd` (fixed VHD) and `gcp.tar.gz` (oldgnu sparse).
- Every tree pins `MinimumVersion=27.1`, `Snapshot=`, `ToolsTree=default` with its
  distribution, release and snapshot, `SourceDateEpoch=`, a `Seed=` per image,
  `History=no` and `WithNetwork=`, and ships `/etc/machine-id` as `uninitialized`.
- The payload carries the provisioning glue of every cloud target the image names,
  inert elsewhere through `ConditionVirtualization=`, so one UKI and one set of
  measurements cover every disk.
- `RootFS.verity()`: an erofs root and its dm-verity hash partition beside the ESP in
  every disk, the root hash on the UKI command line, a volatile overlay on top. CI builds
  it, boots it from dm-verity, and checks that both converters keep the partitions.
- Lint `mkosi-configure-script` and `mkosi-environment-passthrough` refuse what would make
  `mkosi summary` lie or leak the host.
- The Linux backend runs the pinned mkosi in a child forked by mkosi's `fork_and_wait`,
  never from `PATH`; every other tool comes from the pinned tools tree. The Lima backend
  runs the same mkosi commit in an x86-64 Debian guest. Builds never switch backend.
- `doctor` prints one row per capability (`build:x86-64`, `build:go`, `run:simulated`,
  `run:tdx`, `deploy:gcp`, `deploy:azure`, `verify:*`), each failure with its fix; `--for`
  picks one. `build:go` says what the host's `go` is for and wants 1.21 or newer.
- A Go source builds with the toolchain its `go.mod` names (the `toolchain` line, else the
  `go` line), pinned: the host's `go` fetches that release into the module cache with the
  modules (`GOTOOLCHAIN=<release>`), the offline build selects it from the copied cache,
  and Debian's `golang` is only the bootstrap. A release the host cannot fetch is
  `E_SOURCE` naming it and its module, never a failure inside mkosi; a host without `go`
  1.21+ is `E_STATE` naming the source and the fix before anything is fetched;
  `Go(env=)` refuses `GOTOOLCHAIN`. `Tdxs` builds tundra-tools as a Go build (prefetched modules and
  toolchain, `CGO_ENABLED=0`, `-buildvcs=false`) instead of a script that needed the
  network, so an image builds from tundra-tools master (`go 1.25.0`) on trixie (Go 1.24).
- Evidence: `docs/design/evidence/gate1-tree.md`.

### The concise SDK

- `Image`, `Service`, `Component` and `Profile` are the public surface, and every public
  name has one home, with no aliases: `tundravm` (18: `Image`, `Profile`, `Service`, the
  contents `Package`, `File`, `Tree`, `User`, `Group`, the foundation `Debian`, `Kernel`,
  `RootFS`, `Mkosi`, and `load`, `lower`, `compile`, `bake`, `doctor`), `tundravm.ports`
  (`TCP`, `UDP`, `AttestedTLS`, `TLS`), `tundravm.systemd` (what `Service` keywords take),
  `tundravm.components` (`Component`, its exports, `Resolved`, `Diagnostic` and the
  shipped library), `tundravm.storage` (`Volume`, `VolumeDirectory`, `OperatorKey`,
  `TpmSeal`, `BrokerKey`), `tundravm.secrets` (`Secret`, `Credential`), `tundravm.sources`
  (`GoBinary`, `CargoBinary`, `Build`, `Install`, `Git`, `Http`) and `tundravm.errors`. The
  quickstart imports from two of them; the last release had 126 names in `tundravm`. Design:
  `docs/design/namespaces-2026-10-31.md`.
- `Image(name, ...)` slots: `base`, `packages`, `files`, `services`, `components`,
  `volumes`, `secrets`, `ingress`, `network`, `users`, `groups`, `kernel`, `rootfs`,
  `observability`, `targets`, `dev`, `profiles`, `mkosi`. Implied: Debian trixie from the
  locked snapshot, the TEE baseline, a curated kernel module set, a firewall that opens
  only exposed ports and the management port 8745, one hardened user per service, and the
  `tdxs` evidence service. `Image`'s docstring lists each with its escape hatch.
- `Service(name, argv, ...)` takes an argv, never a command line. `requires=[x.ready]`
  waits for real readiness (`Notify()`, `HttpProbe`, `TcpProbe`); `expose=` opens the
  firewall; `state=`, `credentials=`, `resources=`, `shutdown=`, `timer=`, `socket=` and
  `unit=DropIn(...)` group the rest.
- `Hardening.preset("static" | "dotnet" | "jit" | "node" | "python")` names the runtime
  class; each preset's deviation from `static` is written beside it.
- `Component` is a class with configuration fields, `compose()`, typed exports
  (`Endpoint`, `ServiceRef`, `ServiceReady`) and an optional `check()` lint rule.
- `Profile(extends=, add=, changes=, production=)` with `value.ref.edit(...)` and
  `value.ref.remove()`. `dev=DevAccess(...)` is the non-production `dev` profile.
- The runtime slots: `Volume(name, mount=, unlock=OperatorKey() | TpmSeal(...) | BrokerKey(...))`
  with `volume.directory(...)` as a service's state; `Secret(name, required=, rotation=)`
  read as a systemd credential; `AttestedTLS(port, upstream=)` and `TLS`, refused unless
  they are the only path to a loopback upstream; `RootFS.initramfs()` and
  `RootFS.verity()`; `Observability(logs=JournalExport(...), metrics=PrometheusRemoteWrite(...))`.
  The image records all of them in `/etc/tundravm/runtime.json`.
- `tundravm.components`: `TeeBase`, `Network`, `Firewall`, `Tdxs` (implied, replaced by
  passing one), `SshAccess`, `DevAccess`, `Observability`, and the surge workloads
  `Nethermind`, `TaikoClient` and `Raiko`, each with exports and a `check()`.
- The templates (`service`, `prover`), the examples and surge are `Image` files.
  `examples/surge-tdx-prover/image.py` is 33 lines (was 151 plus 500 more in helpers);
  byte parity with the 2024 nethermind-tdx tree is dropped.
- An image file names no backend: `TUNDRAVM_BACKEND`, else `local` on Linux x86-64 and
  `lima` elsewhere.
- `tundravm.testing.plan(image)` takes an `Image`; the planned value is `PlannedImage`.
- Evidence: `docs/design/evidence/gate4-sdk.md`.

### Profiles and the ESP

- Every profile is lowered whatever a compile selects (35a24b1). The shared payload is
  the intersection of all profiles; `payload/mkosi.profiles/<name>/` holds what each
  builds beyond it. A declaration one profile removes moves into the overlays of the
  profiles that keep it, the default profile's included through
  `payload/mkosi.conf.d/99-<default>.conf`. A profile's build no longer depends on which
  other profiles are compiled with it, and no shared script names a profile.
- The ESP is a fixed 128M FAT32 of 512-byte clusters (9894da4):
  `SizeMinBytes=SizeMaxBytes=128M`, `Minimize=off`,
  `SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"`. OVMF reads it as FAT32 by cluster count,
  and the disks no longer carry a 481 MiB ESP. A UKI over about 125 MiB fails the disk
  build; `RootFS.verity()` keeps the OS out of the UKI.
- The changelog and plan record both (79dd67a).

### The operator's loop

- Twelve commands: `new`, `check`, `lock`, `build`, `show`, `run`, `deploy`, `attest`,
  `secrets`, `verify`, `convert`, `doctor`. Shell completion is
  `python -m tundravm.completion bash|zsh|fish`.
- `build` writes `BUILD/<image>/production/<id>/` (`development/` for a non-production
  profile): `payload.efi`, `qemu.raw`, each target's disk, `packages.json`,
  `measurements.json`, `sbom.spdx.json`, `inputs/`, `tree/`, `manifest.json`,
  `manifest.sig` with `--sign`, `observations.json`. It is published with one rename, made
  read-only and never written again. `<id>` is the manifest's digest; `latest-production`
  and `latest-development` point at the newest. A stale lock is refused, a missing one is
  written first.
- `manifest.json` (`tundravm.manifest/1`) binds every artifact and input by sha256, the
  tree by a digest over paths, modes, contents and symlink targets, the UKI and its root
  hash, the lock, the sources (each Go source with its toolchain and checksum), the
  package manifest, the SBOM, the measurement records and the toolchain, with explicit
  `missing` and `unchecked` states; `show BUILD` prints the Go toolchains. `verify` checks it
  offline, a signature against an OpenSSH allowed-signers file, and `--rebuild`
  reproduces it. A signed manifest was verified from a second checkout.
- `build --reproduce` builds again in an independent workspace and records the comparison
  in `observations.json`.
- `measurements.json` holds one record per target and boot path: predictions, unknowns
  with their reason, RTMR3 outside the policy, and a `tundravm.policy/2` policy that
  `attest --policy` takes.
- `run [BUILD] --mode simulated|tdx` boots `qemu.raw` through OVMF from its ESP as a named
  instance with receipts under `.tundravm/instances/`; `run status|logs|stop|ssh|mode`.
- `deploy azure|gcp --build BUILD [--volume NAME=create:SIZE|existing:ID]` publishes and
  starts an explicit build with a receipt; `deploy status|logs|mode|upgrade|rollback`.
  `upgrade` and `rollback` run the one replacement workflow on GCP.
- `attest INSTANCE --policy FILE` verifies fresh evidence and delivers nothing.
  `secrets deliver` and `secrets unlock` attest again first; production delivery needs
  `trusted`. Values travel on the SSH command's stdin only.
- `[tool.tundravm]` keeps `image`, `build` and `backend`, under `TUNDRAVM_IMAGE`,
  `TUNDRAVM_BUILD` and `TUNDRAVM_BACKEND`.
- Every code `check` reports is a registered rule with a severity, what it checks and how
  to fix it. `check --explain CODE ...` prints them, and a text report ends with the
  `--explain` command naming its codes.
- `check`'s text report prints a finding several profiles share as one line naming them
  (`[dev, production]`), and a hint consecutive findings share once after them. `--json`
  and the GitHub annotations keep one finding per profile and subject. `[image]` marks a
  finding about the whole image.
- `check --tree DIR` reports a missing tree once, and says of each drifted file whether
  the tree misses it, has it extra or has it modified.
- Lockfile version 6 names a profile's sections `profiles.<name>.<key>`, the word the API
  uses; a lockfile an older tundravm wrote is refused, with `tundravm lock` as the fix.
  `check` reports a profile's lock drift as that profile's finding
  (`lock-changed [dev] files: files changed since the lock`).
- `new` prints the first loop after `uv sync` (`lock`, `build --tree-only mkosi`, `pytest`,
  `build --dev`, `run --mode simulated --dev`), and the project README and the image's
  docstring list the same commands with `uv run`. The generated image is `ruff format`
  clean. The `--ci` workflow fences `show` in the job summary, checks out without
  credentials and uses the actions tundravm's own CI uses.
- Hints name the fix: a lock that cannot read tundra-tools says the repository is private
  and how to reach it or a mirror; `deploy` of a target the build lacks says to add it to
  `targets=`; an unknown `--explain` code points at the lint reference; a missing instance
  and a `run` without `--mode` give the command to start one.
- Where each removed verb went:

  | Removed | Now |
  |---|---|
  | `init` | `new NAME [--template service\|prover] [--ci]` |
  | `inspect`, `explore`, `preview`, `explain` | `show [IMAGE]`, `--units`, `--tree`, `--json` |
  | `status` | `show BUILD`, `run status`, `deploy status` |
  | `diff`, `changes` | `check --tree DIR`, `show --diff OLD NEW` |
  | `lint`, `compile --check`, `ci` | `check [--profile P] [--tree DIR]` |
  | `compile` | `build --tree-only DIR` |
  | `fetch` | `build` fetches; `lock --update`; `build --offline` |
  | `bake`, `bake --verify-reproducible` | `build`, `build --reproduce` |
  | `measure`, `measure --export-policy` | `measurements.json` in every build |
  | `sbom` | `sbom.spdx.json` in every build |
  | `evidence`, `release`, `witness`, `verify` (bundles) | `manifest.json`, `manifest.sig`, `verify BUILD` |
  | `deploy qemu` | `run [BUILD] --mode simulated\|tdx` |
  | `deploy azure\|gcp --param` | `deploy azure\|gcp --build BUILD` with `[tool.tundravm.azure\|gcp]` |
  | `clean` | `build --clean-cache` |
  | `config` | the resolution rule in every `--help` epilog |
  | `completion` | `python -m tundravm.completion SHELL` |
  | `units convert` | `convert PATH --from systemd` |
  | `units render` | `show IMAGE --units` |
  | `import` | `convert PATH --from mkosi` |

- Evidence: `docs/design/evidence/gate5-cli.md`.

### Execution modes

- A production profile is fail-closed. Every tree ships `tdx-evidence.service`, a root
  oneshot that looks for the `tdx_guest` CPU flag, then `/dev/tdx_guest` or configfs-tsm's
  report directory. Every unit made from a `Service`, and every scheduled `Task`, has
  `Requires=` and `After=` on it, `tdxs.service` included. Without a TDX quote path a
  production profile's gate fails with `tdx-evidence: refused: no TDX quote path` on the
  console, and none of its services start. There is no fallback.
- A development profile (`Variant(release=False)`, `Profile(production=False)`, `dev=`)
  sets the gate's mode through a drop-in. Without TDX it records `simulated` in
  `/run/tundravm/mode` and its services run. Boot tasks, probes, `Unit` declarations,
  mounts, `runtime-init` (the control plane) and the cloud glue are not gated.
- `attest` reads the guest's mode over the session: `simulated` makes the evidence
  simulated, never the reverse. For a production build, simulated evidence never passes,
  and a TD with the `DEBUG` attribute is `untrusted`. The quote's `TDATTRIBUTES` is
  parsed, and records carry `debug`, `simulated`, `production` and `guest_mode`.
- `secrets deliver` and `secrets unlock` refuse a production build in a simulated
  instance before they contact it. A development build in a simulated instance releases
  on `simulated` only. A production build on TDX also needs no `DEBUG` attribute.
- `run --mode simulated` of a production build still boots, so its console shows the
  refusal. `run status` and `run mode` say `fail-closed`.
- CI boots the production fixture without TDX and asserts the refusal. It builds the
  fixture's dev profile and runs it with `run --mode simulated` until the host fetches its
  page. Evidence: `docs/design/evidence/gate3-fail-closed.md`.

### CI

- The jobs that bake the `Image` templates read tundra-tools with the repository secret
  `TUNDRA_TOOLS_TOKEN` when it is set (`docs/guide.md#ci`). Without it, the template bake
  and boot tests skip and name the repository they cannot read; with it, they must pass.
  The `test` job runs only the unit suite; the new `templates` job bakes for real, and a
  newer push to a branch cancels the run it supersedes.
- The boot harness finds a marker that kernel lines interrupted on the serial console. It
  also matches the console with each run of whole kernel lines written into another line
  cut out, so `fixture-boot` no longer waits out its budget when a `virtio_blk` line lands
  inside the gate's refusal.

### Documentation

- `README.md` is one screen: what tundravm is, install, the quickstart and its transcript.
- `docs/guide.md` is the one guide, task-ordered, with what is proven and what is not.
- `docs/trust.md` is the threat model, the platform limits and recovery.
- `docs/reference/cli.md` and `docs/reference/api.md` are generated by
  `python -m tundravm._reference docs/reference` from the parser and the `__all__` of each
  public namespace, one section each; `tests/docs/test_reference.py` fails when they are
  stale.
- `docs/reference/lint.md` is generated the same way from the rule registry: every code
  `tundravm check` reports, its severity, what it checks and how to fix it.
- `tests/docs/test_docs.py` runs every Python block of the prose pages, parses every
  documented command with the real parser, and resolves every relative link and anchor.
- Deleted: `docs/tutorial.md`, `docs/api.md`, `docs/cli.md`, `docs/concepts.md`,
  `docs/module-authoring.md`, `docs/testing.md`, `docs/policy.md` and
  `docs/reproducibility.md`, 63 000 words written for the pre-v2 SDK and CLI.

### Removed

- The per-variant tree layouts: `Mkosi(layout=, dialect=, init_script=, cloud_postoutput=)`,
  the `nethermind-v1` dialect (sandbox-fetching hooks, legacy units and debloat list,
  historical cloud scripts), per-variant settings, kernels and source keys,
  `Tdxs(legacy_units=)`, the lowering-parity and byte-identical nethermind golden tests.
- The tree importer and `tundravm import` (now `convert --from mkosi`).
- The nix backend, the generated `flake.nix` and `flake.lock`, the nixpkgs pin and
  resolver, Lima's Rosetta option, the repository's `flake.nix`.
- `Recipe`, `Variant`, `Fragment`, `Task`, `Unit`, `Disk`, `Key`, `Secrets`, `Firewall`
  and the other internal model names from the top level of `tundravm`.
- The 23 old CLI verbs, without aliases (table above).
- The flat spellings (`from tundravm import TCP, Volume, Secret, ...`), `tundravm.image`
  (now the private `tundravm._image`), `tundravm.components.nethermind` and `.taiko`,
  `IMAGE_OBJECT_NAMES`, and the public-looking internals `tundravm.observability` and
  `tundravm.platforms` (now `_reporting` and `_platforms`).
- `evidence.py`, `release.py`, `witness.py`, `verify.py`, `changes.py`, `explore.py`,
  `preview.py`, `watch.py`, `status.py` and `clean.py`, with their bundle schemas, which
  had no consumer.
- The in-process backend: `TUNDRAVM_BACKEND`, `[tool.tundravm] backend` and
  `Backend(...)` take `local` or `lima`. Builds no longer produce simulated artifacts, and
  the manifest has no `simulated` field (`run --mode simulated` is unchanged).
- `tundravm.testing.fake_bake` and `bake_in_process`; `FakeMkosi` (mkosi) and the new
  `FakeSources` (git and http fetches) let a test run a real build with nothing forked or
  fetched.
- Placeholder measurements: `measure()` drops `allow_placeholder=` and
  `PlaceholderMeasurementWarning`, and an artifact that is no UKI or disk is always refused.
  `Tdxs.from_policy(allow_placeholder=True)` still reads a committed placeholder policy.
- `bake-result.json`: a build's record is its build directory's `manifest.json`.
  `tundravm.bake()` drops `verify_reproducible=` and `offline=` (`tundravm build --reproduce`
  and `--offline`).
- `tundravm.declarative.deploy`, `Qemu`, `Azure`, `Gcp`, `Deployment`, `diff`, `fetch` and
  `read_artifacts`: `tundravm run`, `tundravm deploy`, `tundravm check --tree` and
  `tundravm build` replace them.
- `explain.py`: `show` keeps the description and provenance rendering it needs.
- The `tundravm fetch` hints in emitted build scripts and source errors: they now say
  `run tundravm lock, then tundravm build` and `is not fetched: run tundravm build`.
