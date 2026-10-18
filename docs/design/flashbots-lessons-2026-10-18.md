# Lessons from flashbots-images for tundravm

Studied: `flashbots/flashbots-images` at `c39373dd` (2026-10-16, shallow clone in
`scratchpad/flashbots-images`, 229 files). Compared with tundravm at `/home/franco/tundra`
(read-only). Paths prefixed `fb:` are in flashbots-images; `tv:` are in tundravm.

Context worth knowing first: tundravm's golden contract (the nethermind-tdx surge tree) descends
from an older flashbots-images. The `/init` pivot hack, `minimal.target`, the partition seed
`630b5f72-…`, the kernel command line, the `kernel-yocto.config` and the debloat lists in tundravm
all come from there (`tv:examples/nethermind_base.py:53-59`, `tv:src/tundravm/compiler/emit_mkosi.py:90-107`,
`fb:shared/mkosi.conf:22,26`, `fb:shared/mkosi.extra/init`). flashbots-images has moved on since
that fork: Debian-config kernel with fragments, no modules, `Snapshot=`, binary SBOMs, verity root,
repart-built GCP disks, portable measurements and an attested-host-key flow. Most of the lessons
below are about that newer material.

---

## 1. What flashbots-images is and how it is organised

A toolkit of reproducible, minimal, hardened Debian (trixie) images for Intel TDX: the "Flashbox"
bottom-of-block searcher sandbox (`flashbox-l1`), three L2 builder images (op-rbuilder, bproxy,
simulator) and a `tdx-dummy` test image. Built with mkosi (pinned commit) in a Nix dev shell,
by default inside a per-checkout Lima VM. Output is one UKI; the whole OS lives in the UKI's
initramfs (~20 MB base), so the UKI hash covers the whole OS (`fb:README.md:22`,
`fb:modules/flashbox/flashbox-l1/readme.md` "audit and run the local measurement software").

### Layout

| Path | Role |
|---|---|
| `fb:images/<id>.conf` | Top-level image = an `[Include]` list of `shared/` + modules, plus `[Config] Profiles=` (e.g. `azure,gcp` both on) and a per-image `[Distribution] Snapshot=` (`fb:images/flashbox-l1.conf:1-11`, `fb:images/l2-op-rbuilder.conf:1-14`) |
| `fb:shared/mkosi.conf` | Base: trixie x86-64, `Format=uki`, `ManifestFormat=json`, `Seed=`, `SourceDateEpoch=0`, `CleanPackageMetadata=true`, `VolatilePackages=linux-image-*-mkosi-cloud`, kernel pins via `Environment=` (`KERNEL_GIT_SHA`, Debian linux-config deb version + sha256 + snapshot), hardened cmdline, 13 runtime packages, phase script globs (`fb:shared/mkosi.conf:1-73`) |
| `fb:shared/mkosi.{sync,build,postinst,finalize,postoutput}.d/` | Phase scripts as numbered directories: apt sources from the snapshot (`10-setup-apt.sh`), umask normalisation (`20-normalize-umask.sh`), kernel build (`10-kernel.sh`), EFI stub copy, systemd debloat, SBOM capture, `IMAGE_VERSION` strip, path debloat, `.osrel` strip, SBOM render |
| `fb:shared/kernel/config.d/` | Kernel config fragments: `00-no-modules`, `01-sane-defaults` (222 lines of enables and attack-surface pins), `02-disable-unneeded`, `03-bbr-net-fq`; `modules.config` for the opt-in modular build |
| `fb:shared/mkosi.extra/` | Base files: custom `/init`, `minimal.target`, chrony (NTS), journald volatile caps, networkd DHCP, resolved DoT, sysctl hardening, `print-measurements` (RTMRs to console at boot) |
| `fb:modules/flashbox/{common,flashbox-l1,observability}/` | Searcher sandbox: rootless podman + custom seccomp, iptables mode state machine, dropbear control plane with a C restricted shell (`searchersh.c`), `tdx-init` (LUKS, key-once), attested-tls-proxy, delay-pipe, input-only-proxy, Lighthouse, Prometheus remote-write with Vault creds |
| `fb:modules/l2/{_common,_gcp,_devtools_*,op-rbuilder,…}` | L2 builders: vault-agent + gomplate rendering units from GCE metadata, GCP ops agent built from source, versions in YAML (`fb:modules/l2/op-rbuilder/mkosi.extra/etc/flashbots/op-rbuilder.yaml`), `[Match] Profiles=devtools` gated sub-modules |
| `fb:modules/verity-root/` | Opt-in layout: `Format=disk`, erofs root + dm-verity hash partition, `systemd.volatile=overlay`, `SplitArtifacts=uki` (`fb:modules/verity-root/mkosi.conf:1-18`, `repart.d/*`) |
| `fb:mkosi.profiles/{azure,gcp,devtools}/` | Native mkosi profiles: Azure agentless provisioning + VHD, GCP udev/nvme + BadAML kernel patch + repart-built tar.gz, devtools packages + serial shell + fixed-salt root password + gitignored `custom/` overlay |
| `fb:scripts/` | `env_wrapper.sh` (Lima or Nix), `setup_deps.sh`, `make_git_package.sh` / `build_rust_package.sh` (source builds with content-keyed caches and purls), `verify-hardening.py` (in-VM regression), `verification/verify.py` (rebuild every .deb via debrebuild), `verify-lite.py` (reproduce.debian.net status), `gcp_measurements_to_dcap.sh`, `unpack_image.sh` |
| `fb:flake.nix`, `fb:flake.lock` | Pinned nixpkgs; mkosi pinned to commit `f33cc45`, PATH restricted to Nix tools; `measured-boot` v1.2.0, `dstack-mr-gcp` and `Easy-TEE/attest` pinned by hash (`fb:flake.nix:34-76,116-156`) |
| `fb:Makefile`, `fb:lima.yaml`, `fb:mkosi.version` | `make build|build-dev IMAGE=…`, `measure`, `measure-portable`, `measure-portable-gcp`, `measure-gcp`; Lima VM per checkout hash; version `YYYY-MM-DD.<sha6>[-dirty]` |
| `fb:.github/workflows/` | `build.yaml` (matrix build, optional two-runner reprotest), `_build-and-publish-image.yaml` (tag `<id>-vX.Y.Z` releases: SHA256SUMS, Sigstore provenance + SBOM attestations, R2 upload, draft release with reproduce steps) |
| `fb:tests/integration/` | pytest first-boot chain against a deployed TDX VM: key push, attestation, attested host keys, disk init, negative security tests |

### How modules compose

- **Includes, not inheritance.** An image is a flat ordered `Include=` list; list-valued keys
  (`Packages`, `ExtraTrees`, `*Scripts`) accumulate.
- **Env-var registries.** Modules contribute to shared scripts through prefixed variables that
  the scripts discover with bash `${!PREFIX@}`: `KERNEL_CONFIG_SNIPPETS_<X>`, `KERNEL_PATCHES_<X>`
  (`fb:shared/mkosi.build.d/10-kernel.sh:26-43`), `SKIP_DEBLOAT_PATHS_<X>`
  (`fb:shared/mkosi.finalize.d/90-debloat.sh:56-63`). A module adds a kernel fragment with one
  `Environment=` line (`fb:modules/flashbox/common/mkosi.conf:2`, `fb:mkosi.profiles/gcp/mkosi.conf:2-3`).
- **Profiles for targets and dev.** `Profiles=azure,gcp` in the image conf turns both on in one
  build: one UKI wrapped as `.efi`, `.vhd` and `.tar.gz`. Cloud glue gates itself at runtime
  (`ConditionVirtualization=microsoft`, `fb:mkosi.profiles/azure/mkosi.extra/etc/systemd/system/azure-complete-provisioning.service:3`;
  DMI vendor check, `fb:modules/flashbox/observability/mkosi.extra/usr/bin/flashbox-observability-setup:38-41`).
  Modules gate on profiles with `[Match] Profiles=devtools` (`fb:modules/l2/_devtools_users/mkosi.conf:1-2`).
- **Hook points by sourced files.** Generic scripts source image-specific shell fragments:
  `/etc/bob/firewall-config`, `searcher-container-{before,after}-init`, `toggle-config`
  (`fb:modules/flashbox/common/mkosi.extra/usr/bin/init-firewall.sh:155`, `init-container.sh:37,125`).

### systemd usage and hardening

- Boot target is `minimal.target` (`fb:shared/mkosi.extra/etc/systemd/system/minimal.target`), set on
  the cmdline. Every packaged systemd unit not on a whitelist is masked to `/dev/null`, every
  systemd binary not on a whitelist is deleted (`fb:shared/mkosi.postinst.d/90-debloat-systemd.sh:4-48`).
  Modules unmask what they need (`fb:modules/flashbox/common/unmask-systemd.sh`).
- Units themselves carry almost no sandboxing: most run as root (`input-only-proxy.service:18`,
  `tdx-quote-provider.service:12`); only `vault-agent` has `NoNewPrivileges`. Isolation is done at
  other layers: kernel config pins, rootless podman with a default-ERRNO seccomp profile
  (`fb:modules/flashbox/common/mkosi.extra/etc/containers/seccomp.json`), default-DROP iptables with
  maintenance/production chains and conntrack flushing on toggle (`init-firewall.sh`, `toggle`),
  a container-netns egress blocklist applied by `nsenter` (`init-container.sh:92-116`), sudoers
  whitelist, a C restricted login shell, root locked and `su` made 0700 (`fb:modules/flashbox/common/mkosi.postinst:29-34`).
- Fail-safe optional services: the setup script writes no config on any failure and the consumer
  has `ConditionPathExists=` so it is skipped, never failed (`fb:modules/flashbox/observability/mkosi.extra/etc/systemd/system/prometheus.service:7-11`).

### Packages, sources, reproducibility

- Debian from `Snapshot=` per image; the build's apt sources (with `deb-src` and backports) are
  generated in a sync script and injected with `SandboxTrees=` (`fb:shared/mkosi.sync.d/10-setup-apt.sh`,
  `fb:shared/mkosi.conf:8`).
- `SourceDateEpoch=0`, fixed `Seed=`, `IMAGE_VERSION` stripped from os-release in finalize
  (`fb:shared/mkosi.finalize.d/10-remove-image-version.sh`), `.osrel` section stripped from the UKI
  in postoutput so systemd-stub ≥256 does not merge a version-dependent os-release
  (`fb:shared/mkosi.postoutput.d/00-strip-osrel.sh:5-7`), passwd/group sorted with
  `pwck --sort`/`grpck --sort` before debloat (Debian #963788, `fb:shared/mkosi.finalize.d/90-debloat.sh:4-7`),
  `chmod -R go-w $SRCDIR` at sync time (`fb:shared/mkosi.sync.d/20-normalize-umask.sh`).
- Kernel: gregkh/linux at a full commit SHA, verified after fetch; base config is Debian's
  `linux-config` .deb for the cloud flavour from its own snapshot, pinned by sha256; fragments then
  patches; duplicate fragment names are fatal; `KBUILD_BUILD_*` fixed; `bindeb-pkg` with
  `LOCALVERSION=-mkosi-cloud`; cached by a hash of SHA + config deb + fragments + patches + snapshot;
  installed through `VolatilePackages` from `$PACKAGEDIR` (`fb:shared/mkosi.build.d/10-kernel.sh:8-212`).
  Modules are off by default; `KERNEL_MODULES` switches to `mod2yesconfig` + headers.
- Source builds clone a tag inside the build sandbox (network on), with content-keyed caches in
  `$BUILDDIR` and a per-build `manifest.md` row (built/cached, size, duration)
  (`fb:scripts/make_git_package.sh:12-109`). Rust gets reproducibility flags
  (`-C target-cpu=generic`, `--build-id=none`, `symbol-mangling-version=v0`, codegen-units 1,
  thin LTO, `cargo auditable`, merged `.cargo/config.toml` rustflags) (`fb:scripts/build_rust_package.sh:66-93`);
  Go uses `-trimpath -ldflags "-s -w -buildid="`. Prebuilt .debs are pinned by sha256
  (`fb:modules/flashbox/common/mkosi.build:44-49`).
- Verification beyond rebuild-twice: `verify.py` re-builds each installed .deb with `debrebuild`
  and compares with the cached .deb; `verify-lite.py` checks reproduce.debian.net status.

### Measurement and attestation

- `make measure`: `measured-boot --direct-uki`; `make measure-portable`: `attest measure portable`
  (image hashes for attested-tls-proxy "portable measurement policies"); `measure-portable-gcp`
  wraps them in `attestation_type: "gcp-tdx"` (adds GCP provenance: PPID against Google's
  confidential host registry); `measure-gcp`: `dstack-mr` emits `mrtd[]` per known GCP firmware and
  `rtmr0[]` per firmware × machine-type ACPI × boot variant, reshaped into OR-sets by
  `fb:scripts/gcp_measurements_to_dcap.sh` (`fb:Makefile:51-65`).
- At runtime: `print-measurements` reads RTMRs via the `/dev/tdx_guest` ioctl at boot
  (`fb:shared/mkosi.extra/usr/bin/print-measurements`); `flashbox-image-measurement.rs` parses the
  CCEL event log for the UKI's authenticode hash (RTMR1, `EV_EFI_BOOT_SERVICES_APPLICATION`) and
  exports it as a Prometheus metric (`fb:modules/flashbox/observability/flashbox-image-measurement.rs`).
- Trust bootstrap: attested-tls-proxy on :8745 fronts `ssh-pubkey-server`, which serves the dropbear
  host key (derived at boot, before any secret arrives) and later the container's host key, which is
  generated on the guest and bind-mounted read-only so the container cannot change what is served
  (`fb:…/dropbear.service.d/dropbear-prereq.conf:5-10`, `ssh-pubkey-server.service:1-17`,
  `init-container.sh:14-31`). The first SSH is therefore verified, not TOFU.

### Secrets and keys

No secret is in the image. `tdx-init` waits on :8080 for exactly one ed25519 key, once; the key is
stored in the LUKS2 header; the searcher sets the passphrase over the control-plane SSH. Host keys
are per boot (`/etc` is volatile). L2 images use vault-agent with GCE instance-identity auth and
render units/secrets from GCE metadata with gomplate (`fb:modules/l2/_common/mkosi.extra/etc/vault-agent/gomplate/config.hcl`,
`op-rbuilder.service.ctmpl`). Observability fetches Vault creds with per-field format validation
(`fb:modules/flashbox/observability/mkosi.extra/usr/lib/flashbox/vault.sh:42-57`). The build-time
GitHub token is read from a file with `set +x` around it (`fb:scripts/make_git_package.sh:24-34`).
The devtools root password is a public fixed-salt hash (`fb:mkosi.profiles/devtools/mkosi.postinst:9-13`).

### Deployment targets

QEMU (README command lines, with and without TDX), Azure (VHD; agentless "ready" report to the wire
server; `fb:mkosi.profiles/azure/`), GCP (repart-built ESP sized from the UKI, `--seed`,
`SYSTEMD_REPART_MKFS_OPTIONS_VFAT=--invariant`, fixed disk GUID, `tar --mtime --format=oldgnu -S`;
Google's udev rules and `google_nvme_id`; `nvme-cli`; gve built in; BadAML ACPI sandbox kernel
patch; generated nvme hostid/hostnqn removed; `fb:mkosi.profiles/gcp/`). Bare-metal TDX is named in
the README. Deploying itself (gcloud/az) is not in the repo.

### CI, build tooling, tests, docs

- `make` → `scripts/env_wrapper.sh` → Lima VM `tee-builder-<sha8 of repo path>` with Nix, or
  `nix develop` with `.bypass-lima`; mkosi runs under `unshare --map-auto --map-root-user`
  (`fb:flake.nix:149-156`). Podman flow documented but its Dockerfile is not in the repo.
- CI: 32-core runners, no build cache (every build is cold); reprotest on demand on a second runner
  comparing sha256 of `.efi/.raw/.tar.gz` (`fb:.github/workflows/build.yaml:140-211`). Releases
  attest SHA256SUMS and the SBOM with `actions/attest`, self-verify with `gh attestation verify`,
  upload to R2 with a sha256-pinned rclone, and write release notes with reproduce-and-attest steps
  (`fb:.github/workflows/_build-and-publish-image.yaml:145-336`).
- Tests: `fb:tests/integration/test_flashbox_l1.py` follows the real first-boot chain with
  `pytest-dependency` (deploy → push key → attest → initialize → container SSH → negative tests:
  host-key dir read-only, NTS egress blocked → Lighthouse/reth syncing). The workflow that deploys the
  VM is not in this repo. `fb:scripts/verify-hardening.py` is an in-VM regression for seccomp and
  kernel config pins (checks `/proc/kallsyms` for absent symbols).
- Docs: README quick start, `DEVELOPMENT.md` module guide, a 700-line flashbox-l1 guide with an
  attestation walkthrough. Some drift: `DEVELOPMENT.md` refers to `shared/kernel/kernel-yocto.config`,
  `buildernet/`, `services/` and `KERNEL_CONFIG_SNIPPETS` files that do not exist; the README lists
  `IMAGE=buildernet` and `IMAGE=l2-builder`, which are not images; `KERNEL_PATCHES=shared/kernel/patches`
  points at a missing directory (`fb:shared/mkosi.conf:15`).

---

## 2. Techniques worth adopting, ranked by value to tundravm users

### 2.1 Pin the build toolchain, not only the inputs

- **flashbots:** a committed `flake.lock` pins nixpkgs (`fb:flake.lock:4-10`); mkosi is pinned to a
  commit and patched (`fb:flake.nix:116-145`); mkosi's PATH is the Nix tool env only ("Don't add
  /usr/bin and /usr/sbin", `fb:flake.nix:142-143,154`); the measurement tools are pinned by hash in
  the same shell (`fb:flake.nix:34-76`). Two builders with the same commit run identical tools.
- **tundravm today:** the generated flake follows `nixos-unstable` with no lock file and passes the
  host `PATH` through (`tv:src/tundravm/backends/base.py:149-201`, same at `tv:flake.nix`); the
  local backend accepts any mkosi ≥25 (`tv:docs/reproducibility.md` "mkosi"); CI installs mkosi from
  git HEAD (`tv:.github/workflows/ci.yml` "Install mkosi"). The lockfile has no toolchain section
  (`tv:docs/reproducibility.md:62-72`); evidence only observes tool versions. `measured-boot`/`dstack-mr`
  are whatever is on PATH (`tv:src/tundravm/measure/rtmr.py:54-64`). So `witness` and
  `--verify-reproducible` can disagree across machines for reasons the lock cannot name.
- **proposal (opinionated: the tundravm version picks the toolchain):** each tundravm release ships
  a pinned toolchain (nixpkgs rev + narHash, mkosi commit, measured-boot/dstack-mr/attest pins).
  `tundravm lock` records it as a `toolchain` section; the nix and lima backends write
  `BUILD/mkosi/flake.lock` from it and restrict mkosi's PATH; the local backend checks
  `mkosi --version` against it and warns (`toolchain-unpinned`). `evidence`, `witness` and
  `release` compare toolchains and say so when they differ. Override only through
  `Mkosi(toolchain=Toolchain(nixpkgs=..., mkosi=...))`, linted `toolchain-override` (info).
  Also: Lima's template enables Rosetta (`tv:src/tundravm/backends/lima.py:49-51`), and flashbots
  documents that builds under Rosetta on ARM Macs are not reproducible (flashbox-l1 readme,
  "local measurement software" note). `doctor` and `witness` should record the builder arch and
  warn for emulated x86.

### 2.2 Real cloud measurements: portable policies, GCP firmware sets, provenance

- **flashbots:** `make measure-portable` (Easy-TEE `attest measure portable`), `measure-portable-gcp`
  (`attestation_type: gcp-tdx`, provenance checked against Google's host registry), `measure-gcp`
  (dstack-mr: `mrtd[]` per firmware, `rtmr0[]` per firmware × ACPI × boot variant) and the reshaping
  into OR-sets a verifier accepts (`fb:Makefile:51-65`, `fb:scripts/gcp_measurements_to_dcap.sh:7-28`).
  Releases publish `portable_measurements.json` and explain how to reproduce it.
- **tundravm today:** `--scheme gcp` and `--scheme azure` are placeholders and the GCP one names
  `PCR0/PCR4/PCR8`, which a TDX quote does not have (`tv:src/tundravm/measure/gcp.py:1-30`,
  `tv:docs/concepts.md` "Measurements"). The `rtmr` scheme gives RTMRs without MRTD
  ("RTMR tools do not report MRTD, so pass mrtd="). A policy is one value per register.
- **proposal:** `tundravm measure --scheme gcp` runs the pinned dstack-mr and writes a policy whose
  registers are sets (`{"mrtd": [...], "rtmr0": [...], "rtmr1": "...", ...}` paired per firmware);
  `--scheme portable` writes the UKI/initrd/cmdline image hashes for verifiers that compute registers
  per platform; `Policy` gains `attestation_type` (`dcap-tdx`, `gcp-tdx`, `azure-tdx`) and
  `provenance=True`. `Tdxs.from_policy()` and `attest` accept set-valued registers (a quote matches one
  set). Drop the PCR placeholder names. Keep `--allow-placeholder` for the in-process backend only.

### 2.3 One measured UKI for every cloud

- **flashbots:** `Profiles=azure,gcp` together in one build (`fb:images/flashbox-l1.conf:7-8`): one UKI,
  three wrappers, one set of OS measurements. Cloud glue is conditional at runtime
  (`ConditionVirtualization=microsoft`; DMI vendor checks; GCP tools inert elsewhere).
- **tundravm today:** multi-output variants exist (`outputs=("azure", "gcp")`), but the `cloud` and
  `prover` templates emit separate `azure` and `gcp` variants (`tv:src/tundravm/templates.py:231-232,277-278`),
  i.e. three UKIs and three measurement sets per release. The Azure provisioning unit has no
  condition (`tv:src/tundravm/_units/v1.py:68-73`), so in a multi-output UKI it would run and fail
  on GCP and QEMU after five retries (`tv:src/tundravm/platforms/azure.py` script).
- **proposal:** give every platform-integration unit a condition (`ConditionVirtualization=microsoft`
  for Azure, `ConditionFirmware=`/DMI `sys_vendor` match for GCP) through the typed
  `conditions=` field from `tv:docs/design/systemd-units.md` §2.8. Make the templates default to
  `Variant("default", outputs=("qemu", "azure", "gcp"))`. Add an info lint `measurement-split`
  when two released variants differ only in cloud glue, and have `measure` print "one UKI, N
  outputs" so users see the property.

### 2.4 Kernel as a pinned base plus contributed fragments and patches, verified

- **flashbots:** base config = Debian `linux-config` cloud flavour from a pinned snapshot and sha256;
  fragments from every module/profile in registry order; duplicate fragment names fatal; patches;
  fixed KBUILD vars; a cache key over all of it; a .deb installed via `VolatilePackages`
  (`fb:shared/mkosi.build.d/10-kernel.sh:13-20,26-43,56-66,113-159`). Modules off by default
  (`fb:shared/kernel/config.d/00-no-modules`), with an explicit modular mode (`modules.config`,
  `mod2yesconfig`). A kernel config deny list doubles as attack-surface reduction
  (`01-sane-defaults:135-219`, `10-bob:39-65` with a CVE rationale). `IKCONFIG=y` keeps the config
  inspectable at runtime.
- **tundravm today:** `Kernel(version, source, config: Path, cmdline, tdx)` takes one full config
  file (`tv:src/tundravm/declarative/model.py:1946-1958`); the script copies it, runs `olddefconfig`
  and `bzImage` (`tv:src/tundravm/compiler/emit_mkosi.py:588-664`). No patches, no fragments, one
  `Kernel` per variant (identity is the type), so a component cannot add the options it needs. The
  default platform uses Debian's modular `linux-image-amd64` (`tv:…/declarative/model.py:426-428`).
- **proposal:**
  ```python
  Kernel(
      "6.18.15",
      Git("https://github.com/gregkh/linux", "v6.18.15"),        # lock pins the commit
      base=DebianConfig("6.18", flavor="cloud", version="6.18.15-1~bpo13+1"),  # lock pins sha256
      modules=False,                                               # opinionated default
  )
  KernelConfig("xfs", enable=("XFS_FS",))                          # many per variant, by name
  KernelConfig("no-io-uring", disable=("IO_URING",))
  KernelPatch("badaml", assets.file("kernel/0001-badaml.patch"))
  ```
  `Tdxs()` contributes `TDX_GUEST_DRIVER`/`TSM_REPORTS`, `Disk(key=…)` contributes `DM_CRYPT`, a
  verity root contributes `DM_VERITY`/`EROFS_FS`. After `olddefconfig` the build checks every
  requested option against the final `.config` and fails with `kernel-option-dropped` (flashbots
  does not do this; `merge_config.sh` only warns). Lock and evidence record the final `.config`
  sha256. Ship `KernelHardening()` as a reviewed preset (flashbots' deny list), with `.relaxed()`.
  Conflicting `enable`/`disable` across fragments is `kernel-option-conflict`.

### 2.5 Debloat and determinism details tundravm lacks

- **flashbots:** `pwck --sort`/`grpck --sort` before debloat (`fb:shared/mkosi.finalize.d/90-debloat.sh:4-7`);
  removes `/etc/*-` backup files, `/var/lib/ucf`, `/.cache` (`:13-48`); removes only four named
  tmpfiles.d entries instead of the directory (`:39-42`); keeps `/usr/lib/modules` automatically in
  modular builds and lets modules register exceptions (`:56-63`); strips `.osrel` and re-copies the
  stripped UKI into a verity disk's ESP (`fb:shared/mkosi.postoutput.d/00-strip-osrel.sh:5-16`);
  deletes generated `/etc/nvme/hostid|hostnqn` (`fb:mkosi.profiles/gcp/mkosi.postinst:5-6`).
- **tundravm today:** the default removal list has none of `/etc/*-`, `/var/lib/ucf`, the sort step
  or `.osrel` handling, and removes all of `/usr/lib/tmpfiles.d`, `/usr/lib/systemd/network` and
  `/etc/systemd/network` (`tv:src/tundravm/models.py:30-61`). `Debloat` is one declaration per variant
  (`tv:…/declarative/model.py:1428-1460`), so a component cannot add an exception. Nothing stops
  `Debloat()` from deleting `/usr/lib/modules` under a modular distribution kernel.
- **proposal:** add the sort step, `/etc/*-`, `/var/lib/ucf` and `.osrel` stripping to the current
  dialect (nethermind-v1 untouched). Narrow tmpfiles removal to named files. Add `DebloatKeep(name,
  paths=…)` as a multi-identity declaration components can yield. Lint `debloat-removes-modules`
  (error) when the kernel is modular and `/usr/lib/modules` is removed.

### 2.6 Binary-level SBOM and reproducible Rust flags

- **flashbots:** dpkg status is captured in postinst before `CleanPackageMetadata` deletes it; syft
  catalogs local .debs, Go module info and `cargo auditable` data inside binaries; each source build
  gets a purl with `vcs_url=git+URL@commit`; the CycloneDX document is bound to the image sha256 and
  its serialNumber is a UUIDv8 derived from it, timestamp fixed (`fb:shared/mkosi.postinst.d/95-prepare-sbom.sh`,
  `fb:shared/mkosi.postoutput.d/95-gen-sbom.sh`, `fb:scripts/make_git_package.sh:40-44`). Rust is built
  with `cargo auditable` and fixed flags (`fb:scripts/build_rust_package.sh:66-93`).
- **tundravm today:** the SBOM comes from mkosi's package manifest and the lockfile pins
  (`tv:docs/concepts.md` "Supply chain", `tv:src/tundravm/declarative/bom.py`): a built binary is one
  component with no dependencies. `CargoBuild` sets no RUSTFLAGS and no auditable
  (`tv:src/tundravm/_source.py:272-320`). Go already uses `-trimpath -buildid=` (`tv:…/_source.py:217-266`).
- **proposal:** `CargoBuild(auditable=True)` by default (adds `cargo-auditable` to build packages),
  plus default `RUSTFLAGS` (`--remap-path-prefix=$PWD=.`, `-C link-arg=-Wl,--build-id=none`,
  `-C target-cpu=generic`, `CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1`, incremental off) merged with the
  project's `.cargo/config.toml` rustflags, which an exported `RUSTFLAGS` would otherwise discard.
  `tundravm sbom` adds each built binary's Go modules / auditable crates as nested components.

### 2.7 Attestation before trust: attested host keys and an attestation port

- **flashbots:** the attested channel (:8745) is up before any secret arrives and serves the SSH
  host keys; the client pins them into `known_hosts`; the container cannot influence what is served,
  and a test proves it (`fb:tests/integration/test_flashbox_l1.py:141-151,195-233`).
- **tundravm today:** `attest` reaches `tdxs` over a unix socket, which on a real VM means an SSH
  relay as root (`tv:docs/concepts.md` "Secret delivery at boot", `tv:README.md:75`); that first SSH
  is trust-on-first-use. The quote binds a nonce but not the host keys.
- **proposal:** `Tdxs(publish=AttestedEndpoint(port=8745, serve=(HostKeys(),)))`: tdxs (or a small
  proxy) answers on a TCP port with a quote whose `report_data` binds `sha512(nonce ‖ host keys)`.
  `tundravm attest --issuer tcp://VM:8745 --known-hosts FILE` verifies, then writes `known_hosts`;
  `deploy qemu` forwards the port; the host-key directory is root-owned and generated before any
  secret step (`BootStep` ordering already exists).

### 2.8 Booted-image tests

- **flashbots:** a first-boot test chain against a real VM, with negative security tests
  (`fb:tests/integration/test_flashbox_l1.py`), and an in-VM hardening regression that checks kernel
  pins through `/proc/kallsyms` (`fb:scripts/verify-hardening.py:226-260`).
- **tundravm today:** `tundravm.testing` is static: plans, lint, golden trees, simulated bakes
  (`tv:src/tundravm/testing/__init__.py:1-14`); `tests/integration` bakes but never boots.
- **proposal:** `tundravm.testing.boot(recipe, variant)` (pytest fixture) uses `deploy qemu` plus
  the delivered SSH key and returns a handle with `run()`, `unit()`, `port()`. Probes derived from the
  recipe: every `Service` active, `systemd-analyze security <unit>` exposure at most a threshold per
  `Hardening` preset, every `Listen` listening, every `KernelConfig` option visible in
  `/proc/config.gz`, RTMRs equal to `measure` output under TDX. `tundravm test --boot` runs them;
  the `init --ci github` bake job can run them under KVM.

### 2.9 Opinionated TEE base defaults

- **flashbots:** chrony with NTS sources plus hypervisor refinement bounded by `maxdistance 1.0`
  (`fb:shared/mkosi.extra/etc/chrony/chrony.conf`), journald `Storage=volatile` with caps, sysctl
  hardening, resolved DNS-over-TLS, networkd DHCP with `DNSDefaultRoute=no`, `print-measurements` at
  boot, mitigations on the cmdline (`fb:shared/mkosi.conf:26`), root tmpfs capped in L2
  (`fb:modules/l2/_common/mkosi.postinst.chroot:37-39`).
- **tundravm today:** none of these exist as declarations; networking is per recipe (a
  `network-setup.service` file in the surge example), and default debloat deletes networkd's config
  directories (`tv:src/tundravm/models.py:36,60`). The host controls a TDX guest's clock, which
  matters for TLS, certificate checks and any time-based policy.
- **proposal:** a `TeeBase()` component in `tundravm.declarative.utils` (or `Debian(base="tee")`)
  made of typed pieces: `TimeSync(nts=(...), hypervisor=True, max_distance="1s")`,
  `Journal(storage="volatile", max_size="256M")`, `Network(dhcp=True)`, `Dns(servers=..., tls=True)`,
  `Sysctl({...})`, `PrintMeasurements()`, `RootFs(max_size="4G")`. The templates use it; the lint
  `time-unauthenticated` (warning) fires on a TDX image without `TimeSync(nts=...)`.

### 2.10 CI release with Sigstore provenance and a second-runner check

- **flashbots:** tag-driven release job: SHA256SUMS, `actions/attest` provenance and SBOM
  attestation, `gh attestation verify` self-check, publish, release notes with reproduce steps
  (`fb:.github/workflows/_build-and-publish-image.yaml:66-336`); reprotest on a second runner
  (`fb:.github/workflows/build.yaml:140-211`).
- **tundravm today:** `release`, SSH-signed evidence and `witness` are local commands; `init --ci github`
  writes `check` and an opt-in `bake` job (`tv:README.md:79`).
- **proposal:** `init --ci github --release` adds a tag job that runs `tundravm release`, attests
  `SHA256SUMS` and the SBOM with `actions/attest`, and a job on a second runner that runs
  `tundravm witness` against the uploaded release. `tundravm verify` accepts a Sigstore bundle next
  to SSH signatures (`--sigstore-repo OWNER/REPO`).

### 2.11 dm-verity erofs root as a typed boot layout

- **flashbots:** `fb:modules/verity-root/` builds an erofs root with a verity hash partition, the
  UKI carrying the root hash, `systemd.volatile=overlay`, a fixed-UUID ESP, and the cloud wrappers
  reuse the disk (`fb:mkosi.profiles/gcp/mkosi.postoutput:15-20`).
- **tundravm today:** `Boot = Literal["systemd", "uki"]` (`tv:…/declarative/model.py:331`); every
  bootable image is the UKI-in-initramfs; no verity or erofs anywhere in `src/`.
- **proposal:** `Debian(boot="uki", root=VerityRoot(fs="erofs", writable="overlay"))` for images too
  big for RAM; measurement still covers everything through the root hash on the UKI cmdline; lint
  `verity-kernel-config` adds the needed `KernelConfig` automatically.

### 2.12 Smaller ones

- **Image measurement visible in the fleet:** export the UKI authenticode hash from the CCEL as a
  metric or a file (`fb:modules/flashbox/observability/flashbox-image-measurement.rs`); a
  `MeasurementReport()` component, and `measure` printing the same hash so operators can match.
- **Delivery providers that exist:** `ExternalDelivery` declares responsibility but tundravm ships
  no provider. A `VaultAgent(gcp_role=…)` component modelled on `fb:modules/l2/_common` (GCE
  identity auth, templated outputs, `ready_path`) would make secret targets work end to end.
  `tdx-init`'s "exactly one key, once" rule is a good default for the built-in SSH-key step.
- **Build report:** a `manifest.md`-style table of each build (built or cached, size, duration)
  (`fb:scripts/make_git_package.sh:62,108`) in `bake` output and evidence.
- **Local dev overlay:** a gitignored directory added only to dev variants
  (`fb:mkosi.profiles/devtools/custom/.gitignore`), as `DevTools(overlay="dev-overlay/")`.

---

## 3. Things tundravm already does better or differently

- **Typed, hardened units.** `Service`/`Task` with `Hardening.strict()`, dedicated users, state
  directories and lint (`tv:docs/design/systemd-units.md` §0, §2.3). flashbots runs most units as
  root with no sandbox directives and relies on other layers.
- **Sources fetched on the host and pinned to commits.** flashbots clones tags inside the sandbox with
  network on (`fb:scripts/make_git_package.sh:29-33`); tags can move; the fluent-bit .deb is fetched
  with no hash (`fb:modules/l2/_common/mkosi.sync:3-5`); private repos need a token file in
  `$BUILDDIR`. tundravm's `fetch` uses the user's credentials, verifies checkouts, prefetches
  Go/Cargo/.NET dependencies, and supports `--offline`.
- **Byte-reproducible compiled trees** with `compile --check`, golden tests and `lock --check`
  drift by section; flashbots has no equivalent of a lockfile beyond Nix and per-image `Snapshot=`.
- **Evidence, signed releases, witnesses, `changes --since`.** flashbots has SHA256SUMS plus GitHub
  attestations and a manual reprotest.
- **Composition model.** Identity, refs, `changes` and `requires` instead of env-var registries and
  copied modules (`fb:modules/l2/op-rbuilder` and `op-rbuilder-bproxy` are near copies).
- **Docs that are tested.** Every tundravm example loads, lints and compiles in CI; flashbots'
  docs drifted (section 1, "Docs").
- **Umask independence at compile time** (`tv:docs/reproducibility.md` "CI") instead of a sync-time
  `chmod`.
- **Secrets lint** (`secret-in-file`, `secret-in-env`) and the runtime-init readiness graph.
- **Typed deploy adapters** for QEMU (TDX flags), Azure (gallery + confidential VM) and GCP
  (`--confidential-compute-type=TDX`, `TDX_CAPABLE`) (`tv:src/tundravm/deploy/`); flashbots has none.

---

## 4. Gaps and risks flashbots handles that tundravm ignores

- **Kernel attack surface.** The default platform boots Debian's general-purpose modular kernel
  (`tv:…/declarative/model.py:427`). flashbots: no module loading, and off: io_uring, userfaultfd,
  XFRM/IPsec, vhost/vDPA, KSM, IA32 emulation, Xen, hibernation, FUSE, NFS/CIFS/Ceph, MPTCP,
  TCP-MD5, `KALLSYMS_ALL`, `CRYPTO_AUTHENCESN` (CVE-2026-31431), IPv6 in flashbox
  (`fb:shared/kernel/config.d/01-sane-defaults`, `fb:modules/flashbox/common/kernel/config.d/10-bob`).
- **BadAML.** A TDX host can supply malicious ACPI AML; flashbots patches the kernel so AML cannot
  touch guest RAM on GCP (`fb:mkosi.profiles/gcp/kernel/patches/0001-acpi-gcp-block-aml-systemmemory-ram-access.patch`).
  tundravm cannot apply a kernel patch at all.
- **Hardened kernel cmdline by default** (`mitigations=auto,nosmt spec_store_bypass_disable=on …`,
  `fb:shared/mkosi.conf:26`); tundravm only has it in the nethermind example.
- **Authenticated time.** No time sync declaration; the host controls the guest clock
  (flashbots says so in its own toggle script, `fb:modules/flashbox/common/mkosi.extra/usr/bin/toggle:96`).
- **Measurement stability.** No `.osrel` strip; worth verifying whether a tundravm UKI built with a
  newer EFI stub (the templates pin 257.8) carries a version-dependent `.osrel` that changes RTMRs.
- **passwd/group ordering** (`pwck`/`grpck --sort`) is not normalised.
- **GCP details.** tundravm's `google_nvme_id` is a simplified sysfs-serial script
  (`tv:src/tundravm/platforms/gcp.py` `GOOGLE_NVME_ID`); flashbots ships Google's own helper with
  `nvme-cli`, which reads vendor id-ns data, so device names can differ. Generated nvme
  hostid/hostnqn are not removed. `GVNIC` is advertised at deploy (`tv:src/tundravm/deploy/gcp.py:27`)
  but nothing ensures the gve driver is present once modules are debloated.
- **ESP sizing.** tundravm's GCP disk has a fixed 500 MB ESP in a 1 GB disk and Azure a 500 MB ESP
  (`tv:src/tundravm/compiler/emit_mkosi.py:131-152,189-213`); a larger UKI fails. flashbots sizes
  the ESP from the UKI with systemd-repart and `--invariant` VFAT (`fb:mkosi.profiles/gcp/mkosi.postoutput:28-47`).
- **Azure VHD determinism.** tundravm's Azure script runs `mformat` without `-N` and `mcopy` without
  `-D`/fixed mtimes (`tv:src/tundravm/compiler/emit_mkosi.py:194-199`), unlike its own GCP script.
  flashbots has the same weakness but leaves `.vhd` out of SHA256SUMS and reprotest. Check
  `--verify-reproducible` on an `azure` output before claiming it.
- **Azure glue on other clouds.** No `ConditionVirtualization` (§2.3).
- **Root filesystem in RAM without a cap.** The UKI-in-initramfs root is a tmpfs; flashbots caps it
  where data volumes exist (`fb:modules/l2/_common/mkosi.postinst.chroot:37-39`).
- **Network egress policy.** No firewall declaration; flashbots' default-DROP chains, mode switching
  and container-netns blocklist are its core product guarantee.
- **Fail-safe optional services.** tundravm units have no idiom for "skip cleanly when config is
  absent" (flashbots: setup writes nothing on failure, consumer has `ConditionPathExists=`); typed
  `conditions=` from the units design would cover it.
- **Toolchain drift and emulated builders** (§2.1).
- **Not a gap:** CI caching. flashbots does not cache in CI either (cold 32-core builds, ~30 min per
  its docs); its caches are local, content-keyed in `$BUILDDIR`, as tundravm's are.

---

## 5. Recommended next round for tundravm

1. **Pin the toolchain.** A `toolchain` lock section filled from the tundravm release (nixpkgs rev +
   narHash, mkosi commit, measured-boot/dstack-mr/attest pins); generated `flake.lock`; mkosi PATH
   restricted to the pinned env; local backend version check; `evidence`/`witness`/`release` compare
   toolchains; `doctor` warns on Rosetta/emulated builders. (§2.1)
2. **Real cloud measurements.** `measure --scheme gcp` via pinned dstack-mr with set-valued
   registers, `--scheme portable`, `Policy(attestation_type=…, provenance=…)`; `Tdxs.from_policy()`
   and `attest` accept sets; remove the PCR placeholders. (§2.2)
3. **Kernel v2.** `Kernel(base=DebianConfig(…), modules=False)`, multi-identity `KernelConfig` and
   `KernelPatch` that components contribute, verification of the final `.config`, a reviewed
   `KernelHardening()` preset, the BadAML patch for GCP outputs, and `debloat-removes-modules`. (§2.4, §2.5)
4. **One UKI for every cloud, built deterministically.** Conditions on Azure/GCP glue; templates
   default to one multi-output variant; repart-built, UKI-sized ESPs with `--invariant` VFAT for both
   GCP and Azure; `.osrel` strip, `pwck`/`grpck --sort`, `/etc/*-` removal; a reproducibility test
   per output. (§2.3, §2.5, §4)
5. **Trust bootstrap and booted tests.** `Tdxs(publish=AttestedEndpoint(...))` with host-key binding
   and `attest --known-hosts`; `tundravm.testing.boot` with probes derived from the recipe
   (units active, `systemd-analyze security` thresholds, listens, kernel options, RTMRs). (§2.7, §2.8)
6. **TEE base and supply chain defaults.** `TeeBase()` (NTS time, volatile journal, sysctl, DNS,
   network, `PrintMeasurements`, root size cap) in the templates; `CargoBuild(auditable=True)` and
   reproducible `RUSTFLAGS` by default; binary-level SBOM components. (§2.6, §2.9)
