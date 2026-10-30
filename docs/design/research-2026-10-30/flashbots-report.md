# flashbots-images today, and what tundravm should take from it next

Studied: `flashbots/flashbots-images` at `09c7576` (2026-10-21), shallow clone at
`scratchpad/research/flashbots-images` (231 files). Compared with tundravm `master` at
`fcc8d8a` (2026-10-18). Paths prefixed `fb:` are in flashbots-images, `tv:` in tundravm.

The earlier study, `tv:docs/design/flashbots-lessons-2026-10-18.md`, covered the repo at
`c39373d` (2026-10-16). **The git delta since then is two commits** (`11581f0` bump deps,
`09c7576` mtip32xx off); the whole 2026-10-10..21 window is five commits. So this report is
not about new flashbots material. It is the deeper layer: the exact mkosi mechanics, the
runtime chain end to end, the dev loop, the smells, and a re-ranked adoption list that
subtracts what tundravm has implemented since 10-18 (toolchain lock, set-valued
measurements, one UKI per cloud, kernel v2 with `DebianConfig`/`KernelPatch`/
`KernelHardening`, `TeeBase`/`Network`/`Firewall`, `SshAccess`, `testing.boot`, Rosetta
warning, `pwck --sort`, cargo auditable).

---

## 0. Delta since the 10-18 study

| Commit | Date | What | Relevance |
|---|---|---|---|
| `b02f478` #236 | 10-14 | linux-config `.deb` now fetched from its own snapshot by sha256 (`KERNEL_CONFIG_DEB_SNAPSHOT/VERSION/SHA256`), no longer `BuildPackages=linux-config-6.18/trixie-backports` (`fb:shared/mkosi.build.d/10-kernel.sh:13-20,110-116`) | Already in the 10-18 doc. tundravm's `DebianConfig` has its own `snapshot` (`tv:…/model.py:2027`), so this is covered. |
| `1fb287e` #233 | 10-14 | `flashbox-image-measurement.rs`: 24-line CCEL parser exporting the UKI authenticode hash as a Prometheus gauge, via node-exporter textfile collector (`fb:modules/flashbox/observability/`) | 10-18 §2.12 item; **not implemented** in tundravm (grep: doc only). |
| `c39373d` #158 | 10-16 | `flake.nix` `perSystem`: x86_64 and aarch64 dev shells each get their own package set | Nothing for tundravm; its Lima backend already records Rosetta. |
| `11581f0` #244 | 10-19 | `Snapshot=20261018T143805Z`, kernel `KERNEL_GIT_SHA=2ff600d2…` | Values only. |
| `09c7576` #240 | 10-21 | `# CONFIG_BLK_DEV_PCIESSD_MTIP32XX is not set` (was `=y` by mistake) | tundravm `ATTACK_SURFACE` (`tv:src/tundravm/declarative/_kernel_hardening.py:17-40`) lacks `BLK_DEV_PCIESSD_MTIP32XX`; add it. |

Also worth noting from just before the window: `7b8e276` (2026-09-30, "Merge commit from
fork", a security fix): the host `ALWAYS_OUT` rule for NTS tcp/4460 let the sandboxed
container reach any host on port 4460 in production mode, because the container's traffic
leaves through `pasta` as a guest process. Fix: a third copy of the rule (container-netns
DROP in `init-container.sh:111`, the check in `toggle:224`, a readme row, an integration
test `test_container_cannot_use_time_sync_egress`). See §5 for why that matters.

---

## 1. Repository anatomy today

```
images/            5 top-level images (flat Include= lists + Profiles= + Snapshot=)
shared/            base: mkosi.conf, 4 kernel fragments, phase-script dirs, mkosi.extra
modules/           flashbox/{common,flashbox-l1,observability}, l2/{_common,_gcp,_devtools_*,op-rbuilder,op-rbuilder-bproxy,simulator}, tdx-dummy, verity-root
mkosi.profiles/    azure, gcp, devtools (auto-loaded by mkosi --profile / Profiles=)
scripts/           env_wrapper.sh, setup_deps.sh, make_git_package.sh, build_rust_package.sh, gcp_measurements_to_dcap.sh, unpack_image.sh, verify-hardening.py, verification/{verify.py,verify-lite.py,Dockerfile}
tests/integration/ pytest first-boot chain against a deployed GCP TDX VM
Makefile, mkosi.version, flake.nix, flake.lock, lima.yaml, DEVELOPMENT.md, README.md
.github/workflows/ build.yaml, flashbox-l1.yaml, _build-and-publish-image.yaml
```

### 1.1 Images

| Image | Includes (in order) | Profiles | Snapshot | Output per cloud |
|---|---|---|---|---|
| `flashbox-l1` (`fb:images/flashbox-l1.conf`) | `shared/mkosi.conf`, `modules/flashbox/common`, `modules/flashbox/flashbox-l1`, `modules/flashbox/observability` | `azure,gcp` (both on in one build) | `20261018T143805Z` | `.efi` (UKI) + `.vhd` (Azure) + `.tar.gz` (GCP `disk.raw`) + `.sbom.cdx.json` |
| `l2-op-rbuilder` (`fb:images/l2-op-rbuilder.conf`) | `shared`, `l2/_common`, `l2/_gcp`, `l2/_devtools_users`, `l2/_devtools_no_console`, `l2/_devtools_no_root_login`, `l2/op-rbuilder` | `gcp` | `20260430T025253Z` | `.efi` + `.tar.gz` |
| `l2-op-rbuilder-bproxy` | same with `l2/op-rbuilder-bproxy` (a copy of op-rbuilder plus `bproxy.service` templates) | `gcp` | `20260430T025253Z` | `.efi` + `.tar.gz` |
| `l2-simulator` | same with `l2/simulator` (copy with `simulator.service`, no `tdx-quote-provider`) | `gcp` | `20260430T025253Z` | `.efi` + `.tar.gz` |
| `tdx-dummy` (`fb:images/tdx-dummy.conf`) | `shared`, `modules/tdx-dummy` (a Go `dummy-tdx-dcap` server on :8080) | none | **none** (falls back to `deb.debian.org`, `fb:shared/mkosi.sync.d/10-setup-apt.sh:5-9`) | `.efi` |

Only `flashbox-l1` is built in CI (`fb:.github/workflows/build.yaml:55` `VALID_IMAGES =
["flashbox-l1"]`) and only it has a release workflow (`fb:.github/workflows/flashbox-l1.yaml`).
The three L2 images share `shared/mkosi.conf`'s kernel pins (6.18, Oct 2026 config) but a
userland snapshot from April 2026; they are built by hand. `README.md:42-45` still
advertises `IMAGE=buildernet` and `IMAGE=l2-builder`, which do not exist.

### 1.2 How a cloud image is produced and shipped

- Base format is `Format=uki` for everything (`fb:shared/mkosi.conf:19`): the UKI's initrd
  *is* the root filesystem, pivoted to by a 17-line `/init` (`fb:shared/mkosi.extra/init`).
- **GCP** (`fb:mkosi.profiles/gcp/mkosi.postoutput`): copies the UKI to
  `esp/EFI/BOOT/BOOTX64.EFI`, `touch -d @0` everything, computes
  `ESP_BYTES = roundup4K(uki + 32 MiB)` and `DISK_GIB = roundup1G(ESP + 1 MiB)`, appends
  `SizeMinBytes/SizeMaxBytes` to a temp copy of `repart.d/00-uki.conf`, runs
  `systemd-repart --empty=create --seed=630b5f72-… --copy-source` with
  `SYSTEMD_REPART_MKFS_OPTIONS_VFAT=--invariant` and `SOURCE_DATE_EPOCH=0`, pins the disk
  GUID with `sgdisk --disk-guid 12345678-…`, then
  `tar --mtime=@0 --dereference --format=oldgnu -Sczf <id>_<ver>.tar.gz disk.raw`. The
  `.raw` is deleted afterwards. Explicit 4 KiB rounding was #203.
- **Azure** (`fb:mkosi.profiles/azure/mkosi.postoutput`): fixed 500 MB ESP built with
  `mformat`/`mmd`/`mcopy` (no fixed timestamps), `parted` GPT, `qemu-img convert -O vpc
  -o subformat=fixed,force_size`. Not reproducible by construction and `.vhd` is excluded
  from `SHA256SUMS` and from reprotest (`fb:…/_build-and-publish-image.yaml:163`,
  `build.yaml:130`).
- **verity-root** (`fb:modules/verity-root/`): opt-in module switching to `Format=disk`,
  `RepartDirectories=`, `SplitArtifacts=uki`, `Bootable=yes`, `Bootloader=uki`,
  `KernelModulesInitrd=no`, `KernelCommandLine=systemd.volatile=overlay`,
  `RemoveFiles=/init`. Partitions: ESP 260 MB fixed UUID with `CopyFiles=/efi:/`; root
  `erofs` `Verity=data`, `Minimize=best`, `ExcludeFiles=/boot /efi /usr/lib/modules`;
  `root-verity`. Both cloud postoutput scripts detect `.Format == "disk"` in
  `$MKOSI_CONFIG` and reuse the `.raw` (`gcp/mkosi.postoutput:16-20`,
  `azure/mkosi.postoutput:9-19`). `00-strip-osrel.sh:9-16` re-copies the stripped UKI into
  the ESP of any `.raw` with `mcopy -o -i "$raw@@$((start*512))"`.
- **Upload**: `_build-and-publish-image.yaml` runs `make measure-portable`, writes
  `SHA256SUMS` over `efi,raw,tar.gz,qcow2,sbom.cdx.json` + `portable_measurements.json`,
  `actions/attest` (provenance, and the CycloneDX SBOM as a predicate), `gh attestation
  verify` self-check, sha256-pinned rclone to R2
  `flashbots-public-artifacts/<id>-images/{vX.Y.Z | dev/<branch>.<sha8>}/`, and a draft
  GitHub release whose notes contain the reproduce-and-attest steps. No `gcloud compute
  images create` / `az image` step exists in the repo; the integration test workflow that
  deploys the VM is also elsewhere.
- **Versioning**: `fb:mkosi.version` prints `YYYY-MM-DD.<sha6>[-dirty]` from git on the
  host; CI dev builds override with `--image-version=<branch>.<sha8>` through
  `MKOSI_EXTRA_ARGS`; `IMAGE_VERSION` is stripped from `os-release` in finalize so the
  version only affects filenames (`fb:shared/mkosi.finalize.d/10-remove-image-version.sh`).

---

## 2. mkosi usage patterns

### 2.1 Version pin and why

`fb:flake.nix:116-145`: `pkgs.mkosi` overridden to commit `f33cc45db3ddf0ed57594b58c9c161169d46e03c`
(unreleased mkosi main), with nixpkgs' own mkosi patches hand-filtered by hunk
(`filterdiff --hunks=x2,6`, `--hunks=x1-2`) because "the latest mkosi doesn't need them"
(`:126-127`, a TODO). Reasons visible in history: `5d61df8` #199 "Update nix flake and
mkosi version" and `abf0d3a` #193 "Support newer systemd-efi-stubs" (systemd-stub ≥256
merges `.osrel` into the initrd at runtime, hence `00-strip-osrel.sh`). Two further
mkosi-specific hacks: `sed -i -E '\#^\s+"/usr/(bin|sbin)",$#d' mkosi/run.py` so the
sandbox PATH is Nix-only (`:142-143`), and `10-efi-stub.sh` copying
`/usr/lib/systemd/boot/efi` into the *Nix store path of bootctl inside the chroot* because
mkosi's `bootctl`/`ukify` from Nix look for the stub next to themselves
(`fb:shared/mkosi.postinst.d/10-efi-stub.sh:5-9`). nixpkgs is locked at
`56c02bc00adcf003215cc4bd996d6efaf4cff188` (`fb:flake.lock`). tundravm pins mkosi `v26`
(a release) and nixpkgs by rev + narHash in the lock (`tv:src/tundravm/toolchain.py`), which
is the saner shape; nothing to copy here except the awareness that the stub location hack
exists when running mkosi from Nix.

### 2.2 The complete set of mkosi settings in use (grep of every `.conf`)

| Section | Keys | Where |
|---|---|---|
| `[Distribution]` | `Architecture=x86-64`, `Distribution=debian`, `Release=trixie`, `Snapshot=` | `shared/mkosi.conf:1-4`, `images/*.conf` |
| `[Build]` | `PackageCacheDirectory=mkosi.cache`, `SandboxTrees=mkosi.builddir/mkosi.sources:/etc/apt/sources.list.d/mkosi.sources`, `Environment=` (kernel pins, `KERNEL_CONFIG_SNIPPETS*`, `KERNEL_PATCHES*`, `SKIP_DEBLOAT_PATHS_*`, `SYSTEMD_REPART_MKFS_OPTIONS_VFAT`, `LIGHTHOUSE_BINARY` pass-through), `WithNetwork=true` | `shared/mkosi.conf:6-16`, modules |
| `[Output]` | `Format=uki` / `disk`, `ManifestFormat=json`, `OutputDirectory=build`, `Seed=630b5f72-…`, `RepartDirectories=`, `SplitArtifacts=uki` | `shared/mkosi.conf:18-22`, `verity-root/mkosi.conf:7-10` |
| `[Content]` | `SourceDateEpoch=0`, `KernelCommandLine=`, `ExtraTrees=`, `BuildScripts=`, `SyncScripts=`, `PostInstallationScripts=`, `FinalizeScripts=`, `PostOutputScripts=`, `CleanPackageMetadata=true` (`false` in devtools), `VolatilePackages=linux-image-*-mkosi-cloud` / `attested-tls-proxy`, `Packages=`, `BuildPackages=` (with `/trixie-backports` suffixes), `Bootable=yes`, `Bootloader=uki`, `KernelModulesInitrd=no`, `RemoveFiles=/init` | `shared/mkosi.conf:24-73`, modules, verity-root |
| `[Config]` | `Profiles=azure,gcp` / `gcp` | `images/*.conf` |
| `[Match]` | `Profiles=gcp` / `devtools` | `modules/l2/_gcp`, `modules/l2/_devtools_*` |
| `[Include]` | `Include=` lists | `images/*.conf` |

Oddities: `WithNetwork=true` sits under `[Content]` in `fb:modules/tdx-dummy/mkosi.conf:2`
(it is a `[Build]` key); `SyncScripts=modules/l2/_common//mkosi.sync` has a doubled slash;
`KERNEL_PATCHES=shared/kernel/patches` names a directory that does not exist (harmless
under `nullglob`).

### 2.3 Scripts by phase

| Phase | Script | What it does |
|---|---|---|
| sync | `shared/mkosi.sync.d/10-setup-apt.sh` | Reads `.Snapshot` from `/work/config.json`, writes `mkosi.builddir/mkosi.sources` (`deb deb-src`, `trixie trixie-backports`, `Trusted: yes`) which `SandboxTrees=` mounts into the sandbox (mkosi issue 1755). |
| sync | `20-normalize-umask.sh` | `chmod -cR go-w "$SRCDIR"` — **rewrites permissions of the developer's checkout** on every build. |
| sync | `modules/l2/_common/mkosi.sync` | `curl` fluent-bit 3.1.6 `.deb` into `mkosi.packages/` with no hash. |
| build | `shared/mkosi.build.d/10-kernel.sh` | Kernel (see 2.5). Also seeds `$BUILDDIR/manifest.md` (a build/cached/size/duration table every source build appends to). |
| build | `modules/*/mkosi.build` | `source scripts/make_git_package.sh` / `build_rust_package.sh`; `searchersh.c` compiled with plain `gcc`; cryptsetup v2.8.8 built from source with `--with-crypto_backend=kernel` (so AF_ALG must be in the kernel, `fb:modules/flashbox/common/kernel/config.d/10-bob:39-57`); attested-tls-proxy 2.0.5 as a sha256-checked `.deb` dropped into `$PACKAGEDIR` and installed through `VolatilePackages`. |
| postinst | `shared/mkosi.postinst.d/10-efi-stub.sh` | Nix stub-location hack (2.1). |
| postinst | `90-debloat-systemd.sh` | Whitelist of 20 units; every other systemd unit → `ln -sf /dev/null /etc/systemd/system/<unit>`; every `/usr/bin` file of the `systemd` package not in a 5-name whitelist deleted; `add-wants minimal.target chrony print-measurements systemd-resolved systemd-networkd systemd-networkd-wait-online`. |
| postinst | `95-prepare-sbom.sh` | Copies `dpkg/status`, `os-release`, copyright files into `$OUTPUTDIR/.sbom-root` before `CleanPackageMetadata` deletes them; removes locally installed `.deb`s from status and catalogs them with syft `deb-archive-cataloger`; catalogs Go module info + `cargo auditable` data from binaries. |
| postinst | module `mkosi.postinst` / `.chroot` | users/groups (`useradd -u 1000 searcher -s /usr/bin/searchersh`), `add-wants minimal.target …`, `passwd -l root`, `chmod 700 /bin/su`, `rm -r /etc/dropbear`, `mask ssh.service ssh.socket`. L2: `sed -i '1a mount -o remount,size=4G /' /init` (root tmpfs cap). |
| finalize | `10-remove-image-version.sh`, `90-debloat.sh` | `IMAGE_VERSION` strip; `pwck --sort`/`grpck --sort`; 34-path removal list incl. `/etc/*-`, `/var/lib/ucf`, `/nix`, `/.cache`, four named `tmpfiles.d` entries; `SKIP_DEBLOAT_PATHS_*` registry; `/usr/share/bash-completion` kept under devtools. |
| postoutput | `00-strip-osrel.sh`, `95-gen-sbom.sh`, profile `mkosi.postoutput` | `objcopy --remove-section=.osrel`; CycloneDX with `serialNumber` = UUIDv8 of the image sha256, timestamp 1970, bound to `.raw` if present else `.efi`; cloud wrappers (1.2). |

No `mkosi.prepare`, no `mkosi.initrd`, no `mkosi.skeleton` (DEVELOPMENT.md:27 still lists one).
`.chroot`-suffixed scripts run inside the image (`systemctl add-wants` without
`mkosi-chroot`).

### 2.4 Sharing config between images

- Flat `Include=` lists; list keys accumulate. Nothing is parameterised: three L2 modules
  are copies (`diff -rq modules/l2/op-rbuilder modules/l2/op-rbuilder-bproxy` → 10 differing
  files + 4 bproxy-only; simulator likewise).
- Env-var registries read with `${!PREFIX@}`: `KERNEL_CONFIG_SNIPPETS*`, `KERNEL_PATCHES*`
  (`10-kernel.sh:29-43`), `SKIP_DEBLOAT_PATHS_*` (`90-debloat.sh:58-60`). Alphabetical by
  variable name, base first; duplicate fragment *file names* across registries are fatal
  (`10-kernel.sh:138-141`).
- `[Match] Profiles=` modules, listed unconditionally in the image but inert unless the
  profile is on (`fb:modules/l2/_gcp/mkosi.conf:1-2`).
- Hook files sourced by generic scripts: `/etc/bob/firewall-config`,
  `/etc/bob/searcher-container-{before,after}-init`, `/etc/bob/toggle-config`
  (`init-firewall.sh:155`, `init-container.sh:37,125`, `toggle:52-60`).
- Devtools overlay: `mkosi.profiles/devtools/custom/` and `custom.postinst.d/*.sh`, both
  gitignored (`ExtraTrees=custom`, `PostInstallationScripts=custom.postinst.d/*.sh`).

### 2.5 Kernel

`fb:shared/mkosi.build.d/10-kernel.sh` (212 lines):

1. `git init` + `fetch --depth 1 origin $KERNEL_GIT_SHA` from `gregkh/linux`, verify
   `rev-parse HEAD == SHA`, drop `.git`.
2. `curl --retry 5` the `linux-config-6.18_6.18.15-1~bpo13+1_amd64.deb` from
   `snapshot.debian.org/archive/debian/20261003T143013Z/pool/main/l/linux/`, `sha256sum
   --check`, `dpkg-deb -x`, use `config.amd64_none_cloud-amd64.xz` as base.
3. `patch -p1` each `*.patch` from the registries (today only
   `mkosi.profiles/gcp/kernel/patches/0001-acpi-gcp-block-aml-systemmemory-ram-access.patch`,
   the BadAML sandbox by Paul Meyer, 2026-02-17).
4. Copy fragments into `kconfig/fragments/`, `merge_config.sh base.config fragments...`
   (default mode, i.e. `alldefconfig` with the merged file as `KCONFIG_ALLCONFIG`), then
   `make olddefconfig`, `mod2yesconfig` only when `KERNEL_MODULES` is set.
5. `KBUILD_BUILD_TIMESTAMP="$(date -u -d @0)"`, `KBUILD_BUILD_USER=mkosi`,
   `KBUILD_BUILD_HOST=mkosi-builder`, `LOCALVERSION=-mkosi-cloud`,
   `DEB_BUILD_PROFILES='pkg.linux-upstream.nokerneldbg pkg.linux-upstream.nokernelheaders'`,
   `make -j bindeb-pkg`.
6. Cache `$BUILDDIR/kernel-<sha12>-<hash12>/{kernel.deb,config,kernel.release}` keyed on
   SHA, config version + deb sha256, LOCALVERSION, snapshot, and the concatenated
   fragments and patches; copy the `.deb` to `$PACKAGEDIR` for `VolatilePackages`.

**The point most worth internalising**: Debian's cloud config builds nearly every driver
`=m`. With `# CONFIG_MODULES is not set` (`fb:shared/kernel/config.d/00-no-modules`),
`olddefconfig` turns every `=m` into `n`. That is why `01-sane-defaults` is 222 lines:
its first ~130 lines re-state as `=y` the drivers the images need
(`CONFIG_VIRTIO_NET=y`, `CONFIG_VIRTIO_BLK=y`, `CONFIG_TDX_GUEST_DRIVER=y`,
`CONFIG_TSM_REPORTS=y`, `CONFIG_CONFIGFS_FS=y`, `CONFIG_HYPERV_*=y`,
`CONFIG_MICROSOFT_MANA=y`, `CONFIG_KVM*`, `CONFIG_IP_NF_*`, `CONFIG_EFIVAR_FS=y`, …) and
the remaining ~90 are the attack-surface deny list. Per-target fragments add
`CONFIG_GVE=y` (`mkosi.profiles/gcp/kernel/config.d/20-gcp`), `CONFIG_XFS_FS=y`
(`modules/l2/_common/kernel/config.d/30-xfs`), and `10-bob` adds nftables/iptables
compat, AF_ALG for cryptsetup, and `CONFIG_IPV6=n` (the iptables rules are v4-only).
`03-bbr-net-fq` sets `fq` + BBR. tundravm's composed kernel does the same merge
(`merge_config.sh -m` + `olddefconfig`, `tv:src/tundravm/compiler/emit_mkosi.py:839-840`)
with `CONFIG_MODULES=n` and **no re-statement list** — `tv:docs/concepts.md` "Kernels":
"A built `Kernel` installs no modules and selects nothing: build its drivers in through
its config." See §6, item 1.

### 2.6 initrd, repart, units, extra trees

- initrd: the whole root is the initrd (`Format=uki`); no `mkosi.initrd`, no
  `KernelModulesInitrd` handling except `=no` in verity-root. `/init` mounts `proc sysfs
  devtmpfs tmpfs configfs` then `unshare --mount` + `mount --rbind / /@; mount --move`
  + `chroot . /lib/systemd/systemd` (`fb:shared/mkosi.extra/init`) — the configfs mount is
  for configfs-tsm.
- repart: only in `mkosi.profiles/gcp/repart.d/00-uki.conf` (ESP, `CopyFiles=/esp:/`,
  `Minimize=off`, UUID `87654321-…`) and `modules/verity-root/repart.d/*` (1.2).
- Shipped units (all `WantedBy=minimal.target`; `minimal.target` = `Requires=basic.target`,
  `AllowIsolate=yes`, `fb:shared/mkosi.extra/etc/systemd/system/minimal.target`):
  `print-measurements`, `wait-for-key` (`tdx-init wait-for-key`, `Restart=on-failure`),
  `searcher-firewall` (`init-firewall.sh`, after `network-online`), `dropbear` drop-in
  (`Requires=wait-for-key searcher-firewall`, `ExecStartPre` chown + `dropbearkey -t
  ed25519` + `.pub` derivation), `ssh-pubkey-server` (127.0.0.1:5001, after dropbear only,
  **deliberately not after disk unlock**), `attested-tls-proxy` (:8745 → 127.0.0.1:5001,
  `--server-attestation-type=auto --allowed-remote-attestation-type none`),
  `persistent-mount` (a `bash` loop polling `/proc/mounts` for ` /persistent `, no
  timeout), `input-only-proxy` (:27018, `User=root`), `delay-pipe` (300 s), `lighthouse`
  (`User=lighthouse`, `ExecStartPre=+lighthouse-init`), `searcher-container`
  (`init-container.sh`), observability: `flashbox-observability-setup`,
  `flashbox-image-measurement` (`ConditionPathExists=/sys/firmware/acpi/tables/data/CCEL`,
  `StandardOutput=truncate:/run/flashbox/metrics/image-measurement.prom`), `prometheus`
  (`ConditionPathExists=/etc/prometheus/prometheus.yml`, `User=prometheus`),
  `node-exporter` (textfile collector on `/run/flashbox/metrics`), `process-exporter`.
  Azure: `azure-complete-provisioning` (`ConditionVirtualization=microsoft`, agentless
  wire-server "Ready"). L2: `vault-agent` (root, `NoNewPrivileges=yes`, `ExecStartPre=gomplate
  --datasource gcp=gcp+meta:///`), `automount-data` (ext4 on `/dev/disk/by-id/google-data`,
  plain, `chmod 0777`), `ptlb-routes-nanny.timer`, `set-hostname`, `tdx-quote-provider`
  (root, 127.0.0.1:5757), `node-healthchecker`, `disable-root-login`; plus
  `google-cloud-ops-agent*.service` from the ops-agent source tree.
- `mkosi.extra` per module; ownership can only be fixed in postinst (mkosi issue 3065,
  `fb:modules/flashbox/common/mkosi.postinst:5-6`).

### 2.7 Secrets and attestation components

| Component | Source | Role |
|---|---|---|
| `tdx-init` v0.3.2 (Go) | built from `flashbots/tdx-init` (`fb:modules/flashbox/common/mkosi.build:28-33`) | `wait-for-key`: HTTP :8080, accepts exactly one ed25519 public key once, writes `/home/searcher/.ssh/authorized_keys` and `/etc/searcher_key`; `set-passphrase`: reads a passphrase from stdin, `luksFormat`s the data disk on first use, stores the SSH public key as a LUKS2 token so later boots read it from the header before unlocking; mounts `/persistent`. |
| `searchersh` (C) | `fb:modules/flashbox/common/searchersh.c` | Login shell of `searcher`; `execl`s one of `sudo tdx-init set-passphrase` (`initialize`), `sudo toggle`, `status`, `logs N`, `tail-the-logs`, `restart-lighthouse`, `input-cert`, `reboot`; refuses most before `/persistent` is a mount point. sudoers whitelist `fb:…/etc/sudoers.d/99-searcher`. |
| `attested-tls-proxy` 2.0.5 | prebuilt `.deb`, sha256-pinned | Server side: TLS whose cert is bound to a TDX quote; client side (`attested-tls-proxy client --measurements-file`) verifies against a policy; `gcp-tdx` type adds PPID-vs-Google-registry provenance. |
| `ssh-pubkey-server` v0.1.1 (Go) | source | Serves `/pubkey`: dropbear host key (made at boot) and the container's host key (made on the guest by `init-container.sh:22-31`, bind-mounted `:ro`). |
| `input-only-proxy` v0.0.3 (Rust) | source | TLS 1.3 mTLS where the client cert key must equal the registered SSH key; one-way to a unix socket; unbounded buffering for timing isolation. |
| `delay-pipe` v0.0.3 (Rust) | source | 5-minute log delay. |
| `vault-agent` (L2) | Vault v1.20.1 `-tags minimal` + gomplate v5 from source | GCE instance-identity auth; renders `.hcl`/`.ctmpl` from GCE metadata, then Vault templates write secrets **and whole systemd units** at boot (`op-rbuilder.service.ctmpl` → `/etc/systemd/system/op-rbuilder.service`, then `systemctl daemon-reload; add-wants; restart` from the template's `exec` block, `fb:modules/l2/op-rbuilder/mkosi.extra/etc/vault-agent/gomplate/op-rbuilder.service.hcl:12-32`). |
| `tdx-quote-provider` (L2) | from the op-rbuilder repo | HTTP quote service on 127.0.0.1:5757 consumed by op-rbuilder's `--flashtestations.quote-provider`. |
| Observability creds | `fb:modules/flashbox/observability/mkosi.extra/usr/lib/flashbox/vault.sh` | GCE JWT → Vault token → one KV blob → four values each regex-validated (`^ws-[0-9a-f]{8}-…`, `^[A-Z0-9]{20}$`, …) → `envsubst` into `prometheus.yml` (mode 640). Any failure: no config, `exit 0`, prometheus skipped by `ConditionPathExists`. |

No secret is in any image. Dev images: fixed root password `dqSPjo4p` with `-salt salt`
(`fb:mkosi.profiles/devtools/mkosi.postinst:10-13`) and dropbear's `-s -w -g` stripped.

---

## 3. The TDX runtime story, end to end

### 3.1 Boot

- Firmware: the cloud's (GCP TDVF variants, Azure's paravisor); QEMU uses the distro's
  OVMF (`README.md:99-100`, `OVMF_CODE.secboot.4m.fd` with `-machine q35,smm=on`, or TDX
  with `-object tdx-guest`). The UKI is unsigned; it boots as `EFI/BOOT/BOOTX64.EFI` or
  via `-kernel build/latest.efi`. No systemd-boot, no Secure Boot, no `.pcrsig`.
- Kernel cmdline (`fb:shared/mkosi.conf:26`): `console=tty0 console=ttyS0,115200n8
  mitigations=auto,nosmt spec_store_bypass_disable=on nospectre_v2
  transparent_hugepage=madvise systemd.unit=minimal.target`; verity adds
  `systemd.volatile=overlay`.
- systemd starts via the `/init` pivot; `minimal.target` pulls what postinst `add-wants`ed.
- `.osrel` is stripped from the UKI in postoutput so systemd-stub ≥256 cannot merge a
  version-dependent os-release into the measured initrd. tundravm deliberately keeps
  `.osrel` and measures it (`tv:docs/reproducibility.md:49`,
  `tv:src/tundravm/measure/tdx.py:24-26`) — a conscious divergence, fine as long as
  `IMAGE_VERSION` never lands in os-release.

### 3.2 Measurement

| Tool | Command | Output |
|---|---|---|
| `measured-boot` v1.2.0 (flashbots, Go) | `make measure` → `measured-boot build/latest.efi build/measurements.json --direct-uki` | RTMRs for a direct-UKI boot (QEMU/bare-metal style). |
| `Easy-TEE/attest` (`12f1e29`, Rust) | `make measure-portable` → `attest measure portable FILE > build/portable_measurements.json` | `{"dcap": {<image hashes: UKI sections>}}` — "portable measurement policy" inputs; firmware registers left to the verifier. |
| jq | `make measure-portable-gcp` | `[{attestation_type: "gcp-tdx", dcap_image_hashes: .dcap}]` → `build/measurements-gcp.json` for `attested-tls-proxy --measurements-file`. |
| `dstack-mr-gcp` (`b16e08b`, Go) | `make measure-gcp` → `dstack-mr -uki FILE` | `mrtd[]` per known GCP firmware, `rtmr0[]` per firmware × machine-type ACPI × boot variant, `rtmr1..3`; `scripts/gcp_measurements_to_dcap.sh` ("TEMPORARY") pairs them into flat OR-sets. |

At runtime: `print-measurements` (Perl, `sysopen /dev/tdx_guest`, `ioctl 0xc4405401`,
unpack 4×48 bytes at offset 784) prints `RTMR0..3` to console+journal at boot; exits 0 with
"not available" off TDX. `flashbox-image-measurement` walks the CCEL
(`/sys/firmware/acpi/tables/data/CCEL`), finds the first RTMR1 event of type
`0x80000003` (`EV_EFI_BOOT_SERVICES_APPLICATION`) and exports its SHA-384 as
`flashbox_image_measurement{uki_authenticode="…"} 1`, re-recorded as
`flashbox:image_measurement` so it passes the remote-write `flashbox:*` filter.

tundravm state: `measure --scheme qemu|portable|gcp|azure` with set-valued registers
labelled by firmware (`tv:src/tundravm/measure/policy.py:95-107`) and a GCP firmware table
(`measure/gcp_firmware.py`) — 10-18 §2.2 is done. Runtime visibility (print at boot,
CCEL metric) is not.

### 3.3 Attestation (how apps get quotes, who verifies)

- Quote source: `attested-tls-proxy` auto-detects configfs-tsm or `/dev/tdx_guest`
  (`/init` mounts configfs; kernel has `TSM_REPORTS=y`, `TDX_GUEST_DRIVER=y`). All quote
  users run as root (attested-tls-proxy, tdx-quote-provider `User=root`). tundravm's
  `Tdxs` runs unprivileged with a udev rule for `/dev/tdx_guest` and documents the
  configfs-tsm permission problem (`tv:src/tundravm/_modules/tdxs.py:142-151`); the
  round-7 review (`tv:docs/design/review-round7-2026-10-18.md` issue 5) still lists it as
  unverified on hardware. flashbots' answer is simply root.
- Trust bootstrap for humans: `attested-tls-proxy client --measurements-file policy.json
  --allow-self-signed VM:8745` then `GET http://127.0.0.1:8080/pubkey` → host keys →
  `known_hosts` (`ssh-pubkey-server/cmd/cli/add_to_known_hosts.sh`); the response header
  `X-Flashbots-Measurement` is the proof the quote verified
  (`fb:tests/integration/test_flashbox_l1.py:99`). The first SSH (control plane, before any
  disk unlock) is therefore verified. The served key directory is root-owned and
  bind-mounted read-only into the container so the sandboxed tenant cannot publish data
  through `/pubkey` (test `test_hostkey_dir_readonly` tries `touch`, `echo >`, `rm`,
  `ln -sf`, `mount -o remount,rw`).
- Trust bootstrap for machines (L2): op-rbuilder asks `tdx-quote-provider` for quotes and
  posts "flashtestations" on-chain; the policy for *what* runs is not in the image (3.6).
- tundravm: `attest --issuer unix:|tcp://` to the image's tdxs over an SSH relay; verdict
  `trusted` requires validator + nonce + MRTD/RTMR0 approved + RTMR1/2 match
  (`tv:docs/concepts.md:516`). There is no attested TCP endpoint, no host-key binding, no
  `--known-hosts` (10-18 §2.7, still open; `AttestedEndpoint`/`HostKeys` appear only in
  the design doc).

### 3.4 Disk persistence

flashbox: one data disk (50 GB in the test VM), LUKS2 formatted by `tdx-init
set-passphrase` with a **human-held passphrase entered over the attested control-plane
SSH on every boot** ("This step is necessary on each boot", `fb:…/flashbox-l1/readme.md:488`);
the registered SSH public key lives in a LUKS2 token, readable before unlocking, so
dropbear/authorized_keys work from the header alone on later boots. No TPM, no
`systemd-cryptenroll`, no sealing to measurements: the guest's vTPM (Google-operated) is
not trusted with the key. `/persistent` layout: `searcher/`, `searcher_logs/`,
`delayed_logs/`, `lighthouse/`, `lighthouse_logs/`, `input/`, `input-proxy.{crt,key}`.
Root is tmpfs (capped to 4 G only on L2). L2 data: `automount-data.sh` formats
`/dev/disk/by-id/google-data` as **plain ext4** on first boot (no encryption) and appends
`/etc/fstab`; `init-op-rbuilder.sh` renames legacy datadirs across image versions.

tundravm: `Disk(key=Key(persist_in_tpm=True))` auto-unlocks via a TPM-sealed random key
(`tv:docs/concepts.md:119-126`, `_modules/disk_encryption.py`), formats `on_initialize`
against a stable `/dev/disk/by-id/virtio-tundra-data` in the prover template
(`tv:src/tundravm/templates.py:257-269`). Key strategies are `random` and `pipe`; there is
no "passphrase from an authenticated operator" strategy and no "public key in the LUKS
header" idea. `Secrets(store=disk)` keeps the delivered SSH key *on* the disk, so the
control plane cannot come up before the disk does unless the TPM path works.

### 3.5 Network

- Host firewall: iptables (nft backend, `NETFILTER_XTABLES_COMPAT=y`), default DROP on
  INPUT/FORWARD/OUTPUT, chains `ALWAYS_{IN,OUT}` → `MODE_SELECTOR_{IN,OUT}` →
  `{MAINTENANCE,PRODUCTION}_{IN,OUT}`, loopback allowed, anti-spoof; helpers
  `accept_dst_port`, `accept_dst_ip_port`, `accept_src_ip_dst_port`, `drop_dst_ip`
  (`fb:…/usr/bin/init-firewall.sh`). Rules come from the sourced
  `/etc/bob/firewall-config` with hard-coded Flashbots/BuilderNet IPs. Production mode has
  no DNS, so `/etc/hosts` inside the container maps hostnames to those IPs
  (`searcher-container-after-init`) — a third copy of the same addresses.
- `toggle` (bash, root via sudo): `maintenance → production → stopped → (5 min) →
  maintenance`; flushes `MODE_SELECTOR_*`, kills old flows with `conntrack -D` by
  destination IP (production) or by dport (maintenance), refuses production unless ten
  container-netns DROP rules are present (`toggle:217-226`); state in
  `/etc/searcher-network.state` under `flock`; admits the 5-minute delay uses a
  host-controlled clock (`toggle:96`).
- Container netns: rootless podman (`passt`/pasta, `vfs` storage, `runc`, default-ERRNO
  seccomp of 14.8 KB) running `ubuntu:24.04` pulled at boot (`docker.io/library/ubuntu:24.04`,
  **unpinned tag, apt-get at every boot**, `init-container.sh:53-56`); `nsenter --net
  iptables` DROPs: metadata 169.254.169.254, metrics proxy 10.88.0.100, CL p2p 9000, NTP
  123, NTS 4460, replies from 27017/27018.
- IPv6 is compiled out (`10-bob:2`) because the rules are v4-only.
- Reachability: :22 dropbear control plane (searchersh, key-only, `-j -k` no forwarding),
  :10022 OpenSSH inside the container (root, searcher's key), :8745 attested TLS, :8080
  key registration (once), :27017 UDP / :27018 TLS inputs, 9000 CL p2p, 30303 EL p2p
  (maintenance only). Dev: serial getty shell + root password; dropbear password/root
  login re-enabled; L2 dev: `debian` user with sudo and `authorized_keys` rendered from
  GCE `ssh-keys` metadata.
- Time: chrony NTS to `time.cloudflare.com` and `nts.netnod.se`, hypervisor clocks
  (169.254.169.254, 169.254.169.123) only as refinement under `maxdistance 1.0`
  (`fb:shared/mkosi.extra/etc/chrony/chrony.conf`); the container is blocked from NTP/NTS
  so the host-side ALWAYS_OUT rule is not an egress channel (the 09-30 fix).
- GCP glue: `google_nvme_id` + `65-gce-disk-naming.rules` (Google's own), `nvme-cli`,
  `xxd`, `gve` built in, generated `/etc/nvme/hostid|hostnqn` removed, `set-hostname`
  from metadata, `ptlb-routes-nanny` adding `local <forwarded-ip> dev <if> proto 66` routes
  for passthrough load balancers (retries metadata 100× at 1 s).

tundravm: `Firewall` is **inbound only** ("outbound traffic is not filtered",
`tv:src/tundravm/declarative/_network.py:158,169`), collects ports from `Service(ports=)`.
No egress allow-list, no modes, no container sandbox, no netns rules.

### 3.6 Logging and observability

- journald `Storage=volatile`, `RuntimeMaxFileSize=128M`, `RuntimeMaxFiles=2`.
- flashbox: searcher logs under `/persistent/searcher_logs` rotated daily (5 kept,
  `maxsize 2G`, `copytruncate`) by cron inside the container; `delay-pipe` to
  `/persistent/delayed_logs/output.log` read through `searchersh logs|tail-the-logs`;
  Prometheus scrapes node/process exporters, keeps only `node_(cpu|memory|disk|filesystem|network|vmstat)_.*|flashbox_image_measurement`,
  re-records into `flashbox:*`, remote-writes with SigV4 to Amazon Managed Prometheus
  **through a fixed-IP TCP proxy 10.88.0.100 with `tls_config.server_name`** so no DNS is
  needed in production mode. The whole pipeline is fail-safe (3.7 of 2.7 table).
- L2: rsyslog JSON template + fluent-bit + google-cloud-ops-agent (built from source
  2.57.0) to Cloud Logging; `systemd-networkd` at debug log level permanently
  (`fb:modules/l2/_common/mkosi.extra/etc/systemd/system/systemd-networkd.service.d/override.conf`).

### 3.7 Updates

None in place. A new image means a new VM; data survives on the disk (LUKS header token
carries the key; L2 `init-op-rbuilder.sh` migrates datadir names). `reboot` is allowed
only in maintenance mode and guarded by a lock file. Releases are tags `flashbox-l1-vX.Y.Z`.

### 3.8 What tundravm has and lacks against this chain

| Chain link | flashbots | tundravm today | Gap |
|---|---|---|---|
| Boot | UKI-in-initrd, `/init` pivot, hardened cmdline | same lineage (`emit_mkosi.py:115`), `KernelHardening` | none |
| Drivers in a monolithic kernel | 01-sane-defaults re-states ~100 `=y` | user must list every driver (`concepts.md` Kernels) | **large, correctness** |
| Build-time measurement | 4 tools, OR-sets, portable | `measure` schemes with labelled sets | none |
| Runtime measurement visibility | console print + CCEL metric | none | small |
| Quote access | root services | unprivileged `Tdxs` + udev, unverified | medium (design choice) |
| Attested endpoint + host keys | :8745, `/pubkey`, ro bind mount, tested | none (relay over TOFU SSH) | **large** |
| One key, once | tdx-init | built-in `ssh-key` delivery on :8080 (`_modules/secret_delivery.py:43`) — one-shot semantics not stated | verify |
| Disk unlock | operator passphrase each boot, key in LUKS token | TPM-sealed random key | different model; no operator-passphrase option |
| Egress policy | default-DROP, allow-lists, modes, conntrack flush | none | **large** |
| Tenant sandbox | rootless podman + seccomp + netns denies + restricted shell | none | large (product-specific) |
| Fail-safe optional service | setup writes nothing + `ConditionPathExists` | typed `Condition` exists (`SshAccess` uses it) | doc/idiom only |
| Observability egress w/o DNS | fixed-IP proxy + SNI override + validated creds | `observability.py` exists; no equivalent recipe | medium |
| GCP glue | Google's udev + nvme-cli, routes nanny, hostname | simplified sysfs script (`platforms/gcp.py:60`), no routes/hostname | small |
| Root tmpfs cap | 4 G on L2 | none | small |
| Updates | new VM + datadir migration | nothing either | none |

---

## 4. Dev workflow

### 4.1 Build locally

`make build IMAGE=flashbox-l1` → `scripts/setup_deps.sh` (Linux only: checks curl,
qemu-img, newuidmap, systemd ≥250, nix + flakes; interactive apt/nix install) →
`scripts/env_wrapper.sh mkosi --force --image-id flashbox-l1 --include=images/flashbox-l1.conf`.
The wrapper: warns about the pre-`2b44885` VM name, derives `LIMA_VM=tee-builder-<sha8 of
repo path>`, creates it from `lima.yaml` (Debian 12 genericcloud, 6 CPU, 12 GiB, 100 GiB,
Rosetta on, no containerd, repo mounted at `~/mnt`, single-user Nix installed by the
provision script) unless `.bypass-lima` exists, `rm -f build/*` before every mkosi run,
runs `nix develop -c <cmd>` over `ssh -F ~/.lima/<vm>/ssh.config -t`, then makes
`build/latest.<ext>` symlinks. `make build-dev` adds `--profile=devtools` and image-id
`<id>-dev`. `make clean` deletes `build/ mkosi.builddir/ mkosi.cache/ lima-nix/` and the
Lima VM. `LIMA_CPUS/MEMORY/DISK` env override sizing; `MKOSI_EXTRA_ARGS` is appended.
Inside the shell mkosi runs as `unshare --map-auto --map-root-user --setuid=0 --setgid=0
env PATH=<nix tools only> mkosi` (`fb:flake.nix:149-156`). Caches: `mkosi.cache`
(packages), `mkosi.builddir` (kernel + source-build caches + `manifest.md`),
`mkosi.packages` (local debs). CI has no cache: cold 32-core builds, "~30 min" per the
release notes.

### 4.2 Run

No `make run`. `README.md:92-120` gives two QEMU command lines: OVMF `secboot.4m` with
`-machine q35,smm=on`, `-kernel build/latest.efi`, user-mode net with `hostfwd` 2222→22
and 8080→8080, a virtio-scsi `persistent.qcow2` (2048G sparse) at `lun=10`; and the TDX
variant (`-object tdx-guest`, `-cpu host,-kvm-steal-time,-kvmclock`). "Testing" in the
flashbox readme (`:651-662`) is a manual script: POST key, `initialize`, `journalctl -fu
searcher-container`, SSH into the container, `toggle`. Debugging during build: `socat
UNIX-LISTEN:$SRCDIR/debug.sock … EXEC:/bin/bash` (`DEVELOPMENT.md:662-668`).

### 4.3 Tests

- `fb:tests/integration/` (pytest ≥8, `pytest-dependency`, `requests`; `addopts=-rA`):
  11 tests in dependency order against `VM_IP`: `test_deploy` (8080 within 5 s; the
  workflow outside the repo already polled it), `test_push_key`, `test_attest` (8745 within
  60 s; `fetch_attested_host_keys` runs the proxy client with `MEASUREMENTS_DCAP` and
  `PROXY_CLIENT` from env, fails with a diagnostic that prints expected vs actual
  measurements via `get-tls-cert --out-measurements`), `test_initialize_disk` (300 s,
  passphrase on stdin), `test_container_ssh_ready` (SSH banner), `test_ssh_in` (re-fetch
  `/pubkey` for the container key), `test_hostkey_dir_readonly`,
  `test_container_cannot_use_time_sync_egress` (control 1.1.1.1:443 connects,
  162.159.200.123:4460 blocked), `test_lighthouse_syncing`, `test_install_reth` (downloads
  reth v2.5.2 by sha256 inside the container), `test_reth_syncing`. Timeouts add up to
  roughly 15-20 minutes plus the deploy. The key pair is cached at
  `~/.cache/flashbox-itest/id_ed25519` because registration is once per VM.
- `fb:scripts/verify-hardening.py`: run *inside* the container; probes seccomp
  (`swapon` → EPERM canary, `unshare`, `clone3`, `mount`, `bpf`, socket families 15/33/38-45,
  `pidfd_getfd`, `io_uring_*`, `userfaultfd`, `keyctl`/`add_key`/`request_key`) and kernel
  pins by absence of representative symbols in `/proc/kallsyms` (`kcm_create_basic`,
  `new_userfaultfd`, `tcp_md5_do_add`, `vhost_dev_init`, `xfrm_state_alloc`,
  `__do_sys_io_uring_setup`, …); a "tripwire" for the upstream ptrace fix. Hand-maintained
  ("When you add a seccomp rule or a kernel pin, ADD A CORRESPONDING CHECK BELOW").
- CI (`build.yaml`): push to main builds flashbox-l1 on `warp-ubuntu-latest-x64-32x`
  (`umask 022`, `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, `nix develop
  --command mkosi --force -I images/… --image-id=…`), prints sizes, sha256 of
  `.efi/.raw/.tar.gz`. **No image is booted in CI.** Dispatch-only `reprotest`: a second
  identical job on another runner and a compare job that `diff`s the sorted checksum files.

### 4.4 Reproducibility verification

Three layers, none automatic on every push: (1) the opt-in two-runner reprotest above;
(2) `DEVELOPMENT.md:459-487`: build twice, `sha256sum`, `diffoscope` on the initrds; (3)
`scripts/verification/verify.py` in a `debian:trixie` container (`mmdebstrap`, `sbuild`,
`devscripts`) re-building every `.deb` in `build/latest.manifest` with `debrebuild` and
comparing with the cached `.deb` → `/build/reproducible-report.csv`; `verify-lite.py`
instead asks `reproduce.debian.net` for each package's status. The release path's
"reproduce it yourself" is `make build` + `make measure-portable` and comparing
`portable_measurements.json` byte for byte; the readme admits ARM Macs under Rosetta do
not reproduce (`flashbox-l1/readme.md:242`).

---

## 5. Open problems and smells (things tundravm should solve better, not copy)

1. **Units rendered at boot from a secret store.** `op-rbuilder.service` does not exist in
   the measured image; vault-agent writes it from Vault data at boot, including
   `custom_flags` appended verbatim to `ExecStart`
   (`fb:…/gomplate/op-rbuilder.service.ctmpl:114-116`), then `daemon-reload` + `restart`
   from a template `exec`. For a product whose point is "attest what the builder runs",
   the attested image does not pin the builder's command line; whoever controls Vault or
   the instance metadata (`gcp.Meta "attributes/service"`) controls it. tundravm already
   has `Secrets` with `Schema` validation and a lint that refuses secrets in units; it
   should additionally refuse *units written at runtime* and offer typed, schema-bound
   boot-time templating instead (§6 item 7).
2. **Three copies of every network fact.** The 09-30 security fix shows the shape: host
   allow rule (`firewall-config`), container-netns deny (`init-container.sh`), `toggle`'s
   pre-production check, plus the readme table and hard-coded IPs in `firewall-config`,
   `toggle-config` and `/etc/hosts`. Any new host egress rule silently becomes a tenant
   egress channel unless all copies are updated. Derive, do not duplicate.
3. **Unpinned runtime inputs inside the sandbox:** `docker.io/library/ubuntu:24.04` by
   tag, `apt-get install openssh-server logrotate cron` at every container start
   (`init-container.sh:53-56`); fluent-bit `.deb` fetched without a hash
   (`modules/l2/_common/mkosi.sync`); `rustup toolchain install 1.94.0` at build time
   (`modules/l2/op-rbuilder/mkosi.build:21`); source builds clone tags, not commits, with a
   full-clone fallback (`make_git_package.sh:29-33`); `MAKE_GIT_PACKAGE_CACHE_KEY_EXTRA`
   has to be set by hand when the environment changes. tundravm's host-side `fetch` with
   commit pins and `--offline` is the better model; keep it.
4. **Root everywhere.** attested-tls-proxy, ssh-pubkey-server, tdx-init, input-only-proxy
   (`User=root` explicitly), delay-pipe, tdx-quote-provider, vault-agent (only
   `NoNewPrivileges`), lighthouse's `ExecStartPre=+`. tundravm's `Hardening.strict()`
   default is right; the open question is the quote path (round-7 issue 5).
5. **The developer's checkout is mutated** (`chmod -cR go-w "$SRCDIR"` at sync) and the
   Nix store layout leaks into the image build (`10-efi-stub.sh`). tundravm's compile-time
   umask independence and `efi_stub=` pin are the better answers.
6. **mkosi at an unreleased commit with hunk-filtered patches** (`flake.nix:126-145`),
   flagged TODO. tundravm pins `v26`.
7. **Snapshot and version drift by hand.** `Snapshot=` and the five `KERNEL_*` values are
   bumped in "bump deps" PRs; L2 images sit on an April snapshot with an October kernel
   config and are outside CI. `tdx-dummy` has no snapshot at all. tundravm's lockfile +
   `lock --check` + `changes --since` solve this; the lesson is to make `Debian(snapshot=)`
   mandatory for release variants (lint `snapshot-missing`, error at `release`).
8. **SBOM is best-effort.** #192 added, #202 reverted, reapplied, then "an attempt to fix
   SBOM"; the release workflow only warns when the SBOM is missing (`:160-162`). tundravm
   binds the SBOM to the artifact in evidence; keep it required.
9. **Reproducibility holes left open on purpose:** Azure VHD (`mformat`/`mcopy`, no
   timestamps), `.vhd` excluded from `SHA256SUMS`; the 10-18 doc noted tundravm's Azure
   script has the same weakness (`tv:src/tundravm/compiler/emit_mkosi.py` Azure
   postoutput) — worth re-checking now that `bake --verify-reproducible` covers outputs.
10. **Hand-maintained regression spec** (`verify-hardening.py` header) instead of tests
    derived from the configuration; tundravm's boot probes should be derived from
    `KernelConfig`/`Hardening` declarations.
11. **Docs drift**: `DEVELOPMENT.md` (`mkosi.skeleton`, `buildernet/`, `services/`,
    `kernel-yocto.config`, comma-separated `KERNEL_CONFIG_SNIPPETS`, `Mirror=` pinning
    instead of `Snapshot=`, `build_rust_package` documented with 6 args, function takes 7),
    `README.md` (`IMAGE=buildernet`, `IMAGE=l2-builder`), flashbox readme (`input-only-proxy`
    "pinned to v0.0.2", build pins v0.0.3). tundravm's tested docs are the fix.
12. Small bugs: `build_rust_package.sh:58` has the `>> $BUILDDIR/manifest.md` inside the
    quoted string (line is printed, not appended); `WithNetwork` under `[Content]` in
    tdx-dummy; `persistent-mount.service` is an unbounded `bash` poll loop;
    `systemd-networkd` at `debug` log level in production L2 images; `chmod 0777` on the
    L2 data mount; devtools root password committed.
13. **Time-based security delay on a host-controlled clock** (`toggle:96`), acknowledged.
    chrony NTS mitigates on the host side; the container cannot reach NTS at all, so the
    sandbox's own clock is still the host's. tundravm's `TeeBase(time=True)` covers the
    host part; nothing covers "do not build security delays on wall-clock time" (lint
    idea: `Task(schedule=…)` or sleep-based gating in a security-relevant unit).

---

## 6. Prioritised adoption list for tundravm (not in the 10-18 doc, or still open)

Each item: what, why it matters for "easy TDX images", and how it maps onto the
declarative API. Effort S/M/L.

### 1. `Kernel(modules=False)` must keep Debian's `=m` drivers — ship `CloudDrivers()` and promote `=m` to `=y` (L, correctness)

- **What:** today a composed `Kernel(base=DebianConfig(...), modules=False)` writes
  `CONFIG_MODULES=n`, merges fragments, runs `olddefconfig`, and checks only the
  *requested* options survived (`tv:src/tundravm/compiler/emit_mkosi.py:839-840`,
  `tv:docs/concepts.md` "Kernels"). Everything Debian's cloud config builds as a module
  (virtio-net/blk/scsi, nvme, gve, hv_*, mana, `tdx_guest`, `tsm_reports`, efivarfs,
  dm-crypt, nftables…) silently becomes `n`. flashbots carries 130 `=y` lines in
  `fb:shared/kernel/config.d/01-sane-defaults` precisely for this, plus `20-gcp`
  (`GVE`), `30-xfs`, `10-bob` (nft + AF_ALG for cryptsetup's kernel backend).
- **Why:** a newcomer following the docs gets a kernel that boots to no NIC, no disk and
  no `/dev/tdx_guest`, and the build reports success. This is the single most likely
  silent failure in the "easy" path.
- **API:** (a) when `modules=False`, run `make mod2yesconfig` on the base *before* merging
  deny fragments (flashbots only does it in modular mode; doing it first keeps Debian's
  driver set and lets `KernelHardening` deny on top); (b) a reviewed
  `CloudDrivers(outputs=...)` `KernelConfig` derived from the same tables as
  `tv:src/tundravm/kernel_modules.py` (`TDX_GUEST_MODULES`, `AZURE_MODULES`, `GCP_MODULES`,
  `DISK_MODULES`, `FIREWALL_MODULES`) so a composed kernel builds in exactly what the
  variant's outputs boot on — the monolithic twin of the existing `KernelModules=`
  curation; (c) `Disk(key=...)` contributes `CRYPTO_USER_API{,_HASH,_SKCIPHER}=y` when the
  cryptsetup in use has the kernel backend (flashbots' `10-bob:39-57`); (d) add
  `BLK_DEV_PCIESSD_MTIP32XX` to `ATTACK_SURFACE`; (e) a boot probe in `testing.boot`:
  `/sys/class/net/*` non-empty and `/dev/tdx_guest` or `/sys/kernel/config/tsm` present
  under `--param tdx=true`.

### 2. Egress policy and operating modes in `Firewall` (L, security)

- **What:** `Firewall` filters inbound only (`tv:src/tundravm/declarative/_network.py:158`).
  flashbots' whole guarantee is outbound: default-DROP OUTPUT, `ALWAYS_OUT` allow-list by
  IP:port, per-mode chains, `conntrack -D` on switch, a `toggle` with a state file and
  delay, and a `status` command (`init-firewall.sh`, `toggle`, `firewall-config`).
- **Why:** a confidential VM on an untrusted host exists to keep data in; without egress
  control `TeeBase()` cannot honestly call itself a TEE baseline. Modes are what make
  "maintenance vs production" a declarative fact an auditor can read in the recipe.
- **API:**
  ```python
  Firewall(
      allow=(8745,),
      egress=Egress(
          always=(Endpoint("18.221.59.61", 443, "flashbots bundle"), Port(4460, "nts"), Port(123, udp=True)),
          modes={
              "maintenance": Egress.Allow(dns=True, ports=(80, 443, 30303)),
              "production": Egress.Allow(endpoints=(Endpoint("3.136.107.142", 443),)),
          },
          initial="maintenance",
          switch=ModeSwitch(cooldown="5min", flush_conntrack=True),
      ),
  )
  ```
  Lowering renders one nftables ruleset with named chains and a `tundravm-mode` oneshot
  (`toggle`/`status`) under `Hardening.strict(capabilities=("CAP_NET_ADMIN",))`; the
  cooldown is implemented with `CLOCK_BOOTTIME`/monotonic (the host still owns time, but
  not sleep-skipping). `Service(egress=...)` can add per-service allow-lists later. Lint
  `egress-unfiltered` (warning) on a TDX variant without `egress=`.

### 3. Attested endpoint that serves the host keys before any secret (M, trust; 10-18 §2.7 still open)

- **What:** `Tdxs(publish=AttestedEndpoint(port=8745, serve=(HostKeys(),)))` plus
  `tundravm attest --issuer tcp://VM:8745 --known-hosts FILE`. New details from the
  flashbots chain worth copying exactly: the endpoint must **not** depend on disk unlock
  (`ssh-pubkey-server.service:3-7` comment), the key directory is root-owned and generated
  by the guest (`init-container.sh:14-31`), and a negative test proves a tenant cannot
  alter it (`test_hostkey_dir_readonly`).
- **Why:** today the first SSH to a tundravm image is trust-on-first-use; the relay for
  `attest` runs over that SSH. Verified-first-contact is the thing a TDX image is for.
- **API:** `SshAccess` already has the `ssh-host-keys` task; `AttestedEndpoint` binds
  `sha512(nonce ‖ host keys)` into `report_data`; `deploy qemu` forwards 8745;
  `testing.boot(..., attest=True)` asserts the served keys equal the sshd ones.

### 4. Operator-held disk key as a first-class strategy (M, trust model)

- **What:** `Key(strategy="passphrase")` / `Disk(unlock="operator")`: LUKS2 with a
  passphrase supplied over the attested control plane each boot, and the delivered SSH
  public key stored in a LUKS2 token so the control plane works from the header before
  the disk is open (`tdx-init set-passphrase`, `flashbox-l1/readme.md:205-211`). CLI:
  `tundravm unlock VM --known-hosts FILE` (reads the passphrase, sends it over the
  attested SSH). A `Secrets(store=disk)` then needs `store_in_luks_token=True` semantics
  so the key survives without the filesystem.
- **Why:** on GCP/Azure the vTPM belongs to the provider; many provers will not seal their
  disk key to it. tundravm's only persistence path today is `persist_in_tpm=True`; the
  prover template cannot express "I hold the key".
- **API:** `Key(name, strategy="passphrase", token_public_key=secrets.ssh_key)`;
  `Disk(key=key, format="on_initialize")`; lint `disk-key-tpm-on-cloud` (info) when a TPM
  key is used with `gcp`/`azure` outputs, pointing at the alternative.

### 5. Runtime measurement visibility: `TeeBase(print_measurements=True)` and `MeasurementReport()` (S)

- **What:** at boot, log `RTMR0..3` to console+journal (flashbots' 8-line Perl
  `print-measurements`; tundra-tools can do it in Go), and a `MeasurementReport(
  textfile="/run/tundravm/metrics/image.prom")` oneshot gated by
  `ConditionPathExists=/sys/firmware/acpi/tables/data/CCEL` that parses the CCEL for the
  RTMR1 `EV_EFI_BOOT_SERVICES_APPLICATION` SHA-384 (24 lines of Rust,
  `fb:modules/flashbox/observability/flashbox-image-measurement.rs`). `tundravm measure`
  prints the same authenticode hash so fleets can match.
- **Why:** operators need to see what booted without a quote round-trip; the CCEL hash is
  the cheapest fleet-wide "which image is this" signal and it is what #233 landed on
  after #218 was reverted.
- **API:** two `Task`s under `Hardening.strict(devices=("/dev/tdx_guest",))`; the metric
  file is picked up by `Observability(node_exporter=True)`; `testing.boot` asserts the
  printed RTMRs equal `measure --scheme qemu` under `--param tdx=true` (10-18 §2.8 probe).

### 6. Tenant sandbox component: `Container()` with derived netns denies (L, product)

- **What:** a `Container(image=OciImage("docker.io/library/ubuntu", digest="sha256:…"),
  user="searcher", ports=(10022,), mounts=(disk.path("searcher"),), seccomp="strict",
  host_keys=HostKeys(ro=True))` component: rootless podman + `passt`, `vfs` storage,
  default-ERRNO seccomp profile, container host key generated by the guest and
  bind-mounted `:ro`, and **netns OUTPUT denies derived from the host `Firewall.egress`
  "always" rules** (every host-only allow becomes a container deny; the mode switch
  refuses production unless they are present — the three copies in §5 item 2 collapse to
  one declaration). Add a `RestrictedShell(commands={...})` for the control plane
  (`searchersh.c` is 200 lines of `execl` dispatch; a generated C or a `ForceCommand`
  dispatcher under `sudo` whitelist).
- **Why:** this is the flashbox product and the most reusable TDX pattern after "run my
  service": host-controlled sandbox, tenant-controlled payload, auditable egress. It also
  forces pinning of the container image by digest, which flashbots does not do.
- **API:** `Container` composes `Package("podman","passt","catatonit","runc","uidmap","fuse-overlayfs")`,
  `File("/etc/containers/{containers.conf,seccomp.json}")`, a `Task("container-start")`
  with `needs=[disk.ready, firewall.ready]`, `User("searcher", uid=1000, shell=...)`.
  Lint `container-image-unpinned` (error) for tag-only images.

### 7. Boot-time configuration that is measured: `BootTemplate` with a schema, and a lint against runtime-written units (M)

- **What:** flashbots L2 renders unit files from Vault/GCE metadata at boot (§5 item 1);
  the observability path validates each field by regex before use (`vault.sh:43-57`).
  tundravm should offer the safe half: `BootTemplate(path="/etc/app/config.yaml",
  source=assets.file("config.tmpl"), values={"network": Secret.ref("network")})` where
  every substituted value is a declared `Secret` with a `Schema`, rendered by a oneshot
  with `envsubst`-style explicit variable lists; and lint `unit-written-at-runtime`
  (error) when a `Task`/`Service` writes under `/etc/systemd/system` or runs
  `daemon-reload`.
- **Why:** "what the attested image runs" must be in the image; tunables must be data
  with a schema, not `ExecStart` text. This is the lesson of op-rbuilder's `custom_flags`.
- **API:** `Secret(name, targets=(SecretFile(...),), schema=Schema(pattern=...))` already
  exists; `BootTemplate` is a `Task` plus a `File` with provenance `template`;
  `ExternalDelivery("vault-agent")` gets a shipped `VaultAgent(gcp_role=..., kv_path=...)`
  component (10-18 §2.12) whose templates may only target files, never units.

### 8. Observability egress without DNS: `Observability(remote_write=RemoteWrite(proxy_ip=..., server_name=..., credentials=Secret.ref("metrics")))` (M)

- **What:** the flashbox pipeline: node/process exporters, Prometheus with a keep-list,
  recording rules to a `prefix:*` namespace, remote-write through a fixed IP with
  `tls_config.server_name`, SigV4 creds validated per field, config rendered only on
  success and the service `ConditionPathExists`-gated so a missing config never fails the
  boot (`flashbox-observability-setup`, `prometheus.service:7-11`).
- **Why:** production mode has no DNS and a strict egress list; metrics must still leave
  through one declared IP, and a broken metrics backend must never block a confidential
  service.
- **API:** `Observability` composes `Package("prometheus", "prometheus-node-exporter",
  "prometheus-process-exporter")`, a `Task("observability-setup")` reading the
  `Secrets` entry, `Service("prometheus", conditions=(Condition.path_exists(cfg),))`,
  and an `Egress` entry for `proxy_ip:443`. Lint `optional-service-unconditioned` (info)
  for a service whose config comes from a `required=False` secret but has no condition.

### 9. GCP platform options: Google's disk naming helper, hostname, forwarded-IP routes (S)

- **What:** `Gcp(disk_naming="google", hostname_from_metadata=True,
  forwarded_ip_routes=True)`: ship Google's `google_nvme_id` + `65-gce-disk-naming.rules`
  with `nvme-cli`/`xxd` instead of the simplified sysfs script
  (`tv:src/tundravm/platforms/gcp.py:60`), remove `/etc/nvme/hostid|hostnqn` in postinst,
  `set-hostname` from metadata (`fb:modules/l2/_gcp/mkosi.extra/usr/bin/set-hostname-gcp.sh`),
  and the `ptlb-routes-nanny` timer for passthrough load balancers.
- **Why:** `/dev/disk/by-id/google-<name>` is how a stable `Disk(device=)` is spelled on
  GCP; without Google's helper the names can differ for multi-controller local SSD and PD.
- **API:** platform units gated by `ConditionVirtualization=google` (already the pattern
  in `gcp-metadata-dns.service`), typed `Timer`.

### 10. Root tmpfs cap and fail-safe idioms in `TeeBase` (S)

- `TeeBase(root_max_size="4G")` → `mount -o remount,size=4G /` in the init pivot
  (flashbots L2 `sed -i '1a …' /init`); and document the "setup writes nothing, consumer
  has `Condition.path_exists`" idiom in `docs/concepts.md` with a `Task` + `Service`
  example (typed conditions exist since 10-18).

### 11. Boot probes derived from declarations (M; completes 10-18 §2.8)

- From `KernelHardening`/`KernelConfig(n)`: assert representative symbols absent in
  `/proc/kallsyms` and options absent in `/proc/config.gz` (flashbots'
  `verify-hardening.py:245-260` table is a ready seed); from `Firewall.egress`: a control
  connect that succeeds and a denied one that fails from inside the guest (the 4460
  test); from `Container`: the `hostkey_dir_readonly` attempts; from `Hardening`: a
  `systemd-analyze security <unit>` ceiling. All as `testing.boot(...).probes` so users
  never hand-maintain a checklist.

### 12. Verity root as a typed layout (M; 10-18 §2.11 still open)

- Now with the exact recipe: `Format=disk`, `SplitArtifacts=uki`, `Bootable=yes`,
  `Bootloader=uki`, `KernelModulesInitrd=no`, `systemd.volatile=overlay`, ESP 260 MB fixed
  UUID, `erofs` root `Verity=data` `Minimize=best` excluding `/boot /efi /usr/lib/modules`,
  `root-verity`, kernel options `DM_VERITY EROFS_FS{,_XATTR,_POSIX_ACL} OVERLAY_FS
  CRYPTO_SHA256`, and the cloud wrappers reusing the `.raw`. Map to
  `Debian(boot="uki", root=VerityRoot(fs="erofs", writable="overlay"))`.

### 13. Small, cheap

- Build report table (`manifest.md`: component, version, built/cached, size, duration) in
  `bake` output and evidence (10-18 §2.12).
- `DevTools(overlay=Path("dev-overlay"))` gitignored extra tree + `custom.postinst.d`.
- Lint `snapshot-missing` (error on `release` variants) — flashbots' `tdx-dummy` is the
  counter-example.
- Release notes generator in `init --ci github --release`: the "reproduce the image
  hashes" and "attest your running VM" sections of
  `fb:.github/workflows/_build-and-publish-image.yaml:300-325` are a good template; grep
  shows no `sigstore`/`actions/attest` in `tv:src` yet, so 10-18 §2.10 is still open.
- `Azure` output: re-verify `bake --verify-reproducible` on the VHD and fix `mformat
  -N`/`mcopy -D o` + fixed mtimes if it fails (10-18 §4 item, unverified).
- Confirm and document the one-shot semantics of the built-in `ssh-key` delivery
  (`tv:src/tundravm/_modules/secret_delivery.py:43`): exactly one key, exactly once, and
  the port closes afterwards (tdx-init's rule, `flashbox-l1/readme.md:186`).

### 14. Keep as is (flashbots is worse here)

Toolchain pinning by lockfile rather than a patched unreleased mkosi; host-side `fetch`
with commit pins instead of in-sandbox clones by tag; typed hardened units instead of
root everywhere; `.osrel` measured rather than stripped (as long as `IMAGE_VERSION` stays
out of os-release — add a lint for it); tested docs; `evidence`/`witness` instead of a
manual two-runner reprotest.

---

## 7. What in the 10-18 doc is now stale

Because flashbots moved only two commits, almost nothing on the flashbots side is stale:

- Values: `Snapshot=20261012T085607Z` → `20261018T143805Z`; `KERNEL_GIT_SHA
  17ad2612…` → `2ff600d2…`; `02-disable-unneeded` now also turns `MTIP32XX` off.
- `fb:flake.nix` line references in §1 and §2.1 of the 10-18 doc still resolve
  (`:34-76`, `:116-145`, `:149-156`) after the `perSystem` refactor.
- Everything the 10-18 doc described as "flashbots does X" remains true at `09c7576`.

What is stale on the **tundravm side** (implemented since, so the doc's "tundravm today"
paragraphs no longer hold): §2.1 toolchain lock and Rosetta recording; §2.2 set-valued,
firmware-labelled measurements and the GCP firmware table; §2.3 one UKI with
`ConditionVirtualization` gating (`concepts.md:206`); §2.4 `Kernel(base=DebianConfig,
patches=, fragments=)`, `KernelConfig`, `KernelPatch`, `KernelHardening` incl. BadAML;
§2.5 `pwck --sort` + `-` backup removal, `Debloat(keep_modules=)`, lint
`debloat-removes-modules`; §2.6 `CargoBuild(auditable=, reproducible=)` with
`CARGO_RUSTFLAGS`, embedded-binary SBOM components; §2.8 `tundravm.testing.boot` with
markers, expected units, port forwards and `wait_http`; §2.9 `TeeBase`, `Network`,
`Firewall`, `SshAccess`. Still open from that doc: §2.7 (attested endpoint), §2.10
(Sigstore release job), §2.11 (verity root), §2.12 (measurement metric, `VaultAgent`,
build report, dev overlay), and §4's GCP helper/ESP/VHD notes. The §2.4 proposal's
"`Tdxs()` contributes `TDX_GUEST_DRIVER`/`TSM_REPORTS`, `Disk` contributes `DM_CRYPT`"
is the part item 1 above completes: it is not enough to *request* those options; under
`modules=False` the whole Debian driver set has to be promoted or re-stated.
