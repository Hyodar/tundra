# Gate 1 evidence: the mkosi-native tree

Gate 1 (Substrate) of `docs/design/plan-v2-2026-10-30.md` section 8 keeps the fixture tree
with its INI contents and the resolved `mkosi summary --json` of every image node. This page
is generated from the committed evidence; the tests keep it honest:

- `tests/fixtures/native-tree/recipe.py`: the fixture recipe (a `greeter` service and its
  component `Greeter`, a `web` service serving a page over `Network`, a `boot-probe` task
  printing the command line and what the root is, a `dev` variant adding SSH by public key,
  `azure` and `gcp` output variants, Debian trixie at snapshot `20251113T083151Z`), with
  `Mkosi(root="initramfs")` (`recipe`) and `Mkosi(root="verity")` (`verity_recipe`, which
  `tundravm bake ... --attr verity_recipe` selects).
- `tests/fixtures/native-tree/tree-initramfs/`, `tree-verity/`: what it compiles to, every
  file asserted byte for byte (`test_fixture_compiles_to_the_committed_tree`).
- `tests/fixtures/native-tree/summary-initramfs.json`, `summary-verity.json`: `mkosi --force
  summary --json` of each tree with mkosi 27.1, for the default selection and for every node
  (`--profile=dev --dependency=azure --dependency=gcp`), normalised (tree paths as `<tree>`,
  host-dependent keys left out), asserted by `test_summary_of_every_node_is_the_committed_evidence`.
- `tests/compiler/test_native_tree.py` also checks: each `mkosi_argv` selection resolves to
  the images it names, a stale `.mkosi-private/history/latest.json` changes nothing, every
  emitted key is an mkosi 27.1 setting in its own section, the main-image-only key list
  equals mkosi's scopes, every example sets the reproducibility knobs, and the converted
  surge tree resolves every node.

`summary` runs unprivileged, so all of this is proven on any host; building and booting are
proven in CI (section "What CI proves" below).

## Deviations from section 3.6

- `mkosi.sandbox/` is at the tree root, not under `payload/`: `SandboxTrees=` is a
  `universal` setting (`mkosi/config.py` `SETTINGS`, `scope=SettingScope.universal`), and
  `ParseContext.parse_config_one` skips a prohibited setting's `mkosi.sandbox` path suffix in
  a subimage (`if self.setting_prohibited(s): continue`), so a payload-level sandbox would be
  silently ignored. A profile's own repositories go to `mkosi.profiles/<variant>/mkosi.sandbox/`
  at the root, which the main image parses for the same `--profile`.
- `BuildSources=`, `SourceDateEpoch=`, `WithNetwork=`, `Snapshot=` and every `ToolsTree*=` are
  in the root `mkosi.conf` (multiversal, universal or tools scope: a subimage that sets them
  dies with "Setting ... cannot be configured in ... image"). Verbatim `Setting`s are routed
  the same way (`MAIN_IMAGE_SETTINGS`).
- `Seed=` is written in every image (main, payload, each wrapper): it is `local` scope, and a
  subimage without one gets a random seed per parse.
- `OutputDirectory=mkosi.output` is always written: `%O` expands when `ExtraTrees=` is parsed
  (`ParseContext.expand_specifiers`), and an unset `OutputDirectory` drops the specifier with a
  warning. `--output-directory` on the command line still wins.
- `ImageId=<recipe>` in the root (inherit scope): the dev profile's `ImageId=%i-dev` needs `%i`
  set when the profile is parsed.
- The verity payload is `SplitArtifacts=uki,partitions` (section 3.6 says `uki`): the disks
  copy the split `payload.root.raw` and `payload.root-verity.raw` (`SplitName=root`,
  `root-verity`) with `CopyBlocks=`, which needs the partitions split out. The copied
  partitions get fixed UUIDs (uuid5 of the seed) and the UKI names them with
  `systemd.verity_root_data=`/`systemd.verity_root_hash=`, since a copied partition does not
  keep the UUIDs repart derives from the root hash.
- `/etc/machine-id` ships `uninitialized` in the payload's extra tree (`MachineId=` takes a
  UUID only: `config_parse_uuid` refuses `uninitialized`).
- `ToolsTreePackages=qemu-utils`: the Azure converter runs `qemu-img`, which the default
  Debian tools tree does not name; the GCP converter and both GPT resizes use python3 only.
- `Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512` in the main image and each wrapper:
  repart (systemd 257, offline) formats the ESP FAT32 with 4096-byte sectors, one per
  cluster, and `Minimize=guess` sizes the file system to the UKI, not to the partition, so
  the fixture's ESP held 29158 clusters (verity) and the service template's 38260. UEFI types
  a FAT by its cluster count (FAT32 needs 65525), so OVMF saw a FAT16 it could not mount,
  found no `EFI/BOOT/BOOTX64.EFI` and fell through PXE and HTTP boot to its shell. A larger
  `SizeMinBytes=` does not help (the partition grows, the minimized file system does not:
  512M partition, still 29158 clusters); mkfs.vfat takes the last `-S`, and 512-byte sectors,
  the disk's own, give the same file system eight times the clusters.
- The verity UKI also names `root=/dev/mapper/root`: the device systemd-veritysetup-generator
  opens for `roothash=`, so the initrd mounts the root through dm-verity whatever the boot
  path (`-kernel` sets no `LoaderDevicePartUUID`, which `gpt-auto` needs) and never mounts the
  bare erofs partition.
- The verity payload's `KernelModules=` adds `dm_verity`, `erofs`, `overlay` and
  `crc32c_generic` to the curated set: Debian builds them as modules, mkosi's kernel-modules
  initrd holds only what the payload keeps, and the initrd needs them to open, mount and
  overlay the root (`crc32c_generic` for libcrc32c, whose softdep mkosi skips).

## Root layout `initramfs`

### Tree

```
mkosi.conf
mkosi.images/
  azure/
    mkosi.conf
    mkosi.postoutput.d/
      vhd
    mkosi.repart/
      00-esp.conf
  gcp/
    mkosi.conf
    mkosi.postoutput.d/
      tar
    mkosi.repart/
      00-esp.conf
  payload/
    mkosi.conf
mkosi.repart/
  00-esp.conf
payload/
  mkosi.conf
  mkosi.conf.d/
    10-platform.conf
    20-greeter.conf
    30-web.conf
    40-cloud-glue.conf
  mkosi.extra/
    etc/
      greeter/
        greeting
      hosts
      machine-id
      systemd/
        network/
          50-tundravm.network
    srv/
      www/
        index.html
    usr/
      bin/
        azure-complete-provisioning
      lib/
        systemd/
          system/
            azure-complete-provisioning.service
            boot-probe.service
            gcp-metadata-dns.service
            greeter.service
            systemd-networkd-wait-online.service.d/
              50-tundravm-network.conf
            web.service
        tundravm/
          boot-probe
          gcp/
            resolv.conf
        udev/
          google_nvme_id
          rules.d/
            65-gce-disk-naming.rules
  mkosi.finalize.d/
    50-finalize.sh
  mkosi.postinst.d/
    50-postinst.sh
  mkosi.profiles/
    dev/
      mkosi.conf
      mkosi.extra/
        etc/
          ssh/
            authorized_keys.d/
              root
            sshd_config.d/
              50-tundravm.conf
        usr/
          lib/
            systemd/
              system/
                ssh-host-keys.service
                ssh.service.d/
                  10-tundravm-needs.conf
      mkosi.postinst.d/
        50-postinst.sh
  mkosi.skeleton/
    etc/
      systemd/
        system/
          minimal.target
```

### INI contents

`mkosi.conf`

```ini
[Config]
MinimumVersion=27.1
Dependencies=payload

[Distribution]
Distribution=debian
Release=trixie
Architecture=x86-64
Snapshot=20251113T083151Z

[Output]
Format=disk
Output=qemu
ImageId=fixture
OutputDirectory=mkosi.output
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70

[Build]
ToolsTree=default
ToolsTreeDistribution=debian
ToolsTreeRelease=trixie
ToolsTreeSnapshot=20251113T083151Z
ToolsTreePackages=qemu-utils
History=no
WithNetwork=no
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512

[Content]
SourceDateEpoch=0
Bootable=no
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI

[Runtime]
Firmware=uefi
```

`mkosi.images/azure/mkosi.conf`

```ini
[Config]
Dependencies=payload

[Output]
Format=disk
Output=azure
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70

[Build]
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512

[Content]
Bootable=no
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
```

`mkosi.images/azure/mkosi.repart/00-esp.conf`

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
```

`mkosi.images/gcp/mkosi.conf`

```ini
[Config]
Dependencies=payload

[Output]
Format=disk
Output=gcp
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70

[Build]
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512

[Content]
Bootable=no
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
```

`mkosi.images/gcp/mkosi.repart/00-esp.conf`

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
```

`mkosi.images/payload/mkosi.conf`

```ini
[Include]
Include=../../payload
```

`mkosi.repart/00-esp.conf`

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
```

`payload/mkosi.conf`

```ini
[Output]
Format=uki
Output=payload
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70
ManifestFormat=json

[Content]
CleanPackageMetadata=yes
KernelModules=
    efivarfs
    gve
    hv_netvsc
    hv_storvsc
    hv_utils
    hv_vmbus
    mana
    nvme
    pci_hyperv
    sd_mod
    tdx_guest
    virtio_blk
    virtio_net
    virtio_scsi
Packages=
    passwd
```

`payload/mkosi.conf.d/10-platform.conf`

```ini
[Content]
Packages=
    kmod
    linux-image-amd64
    systemd
    systemd-boot-efi
    systemd-sysv
    udev
```

`payload/mkosi.conf.d/20-greeter.conf`

```ini
[Content]
Packages=
    curl
```

`payload/mkosi.conf.d/30-web.conf`

```ini
[Content]
Packages=
    busybox
```

`payload/mkosi.conf.d/40-cloud-glue.conf`

```ini
[Content]
Packages=
    dmidecode
```

`payload/mkosi.profiles/dev/mkosi.conf`

```ini
[Output]
ImageId=%i-dev

[Content]
Packages=
    openssh-server
```

### Resolved summary, default selection (`mkosi --force summary --json`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| payload | uki | payload | - | - | fixture | 6a5f3c1e | 20251113T083151Z |
| main | disk | qemu | payload | - | fixture | 6a5f3c1e | 20251113T083151Z |
| tools | directory | mkosi.tools | - | - | - | - | 20251113T083151Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: (none)
- payload `SplitArtifacts`: uki, kernel, initrd; `Verity`: auto; `Bootloader`: systemd-boot
- payload `ExtraTrees`: <tree>/payload/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

### Resolved summary, every node (`--profile=dev --dependency=azure --dependency=gcp`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| payload | uki | payload | - | dev | fixture-dev | 6a5f3c1e | 20251113T083151Z |
| azure | disk | azure | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| gcp | disk | gcp | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| main | disk | qemu | payload, azure, gcp | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| tools | directory | mkosi.tools | - | - | - | - | 20251113T083151Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, openssh-server, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: (none)
- payload `SplitArtifacts`: uki, kernel, initrd; `Verity`: auto; `Bootloader`: systemd-boot
- payload `ExtraTrees`: <tree>/payload/mkosi.extra, <tree>/payload/mkosi.profiles/dev/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- azure `PostOutputScripts`: <tree>/mkosi.images/azure/mkosi.postoutput.d/vhd
- gcp `PostOutputScripts`: <tree>/mkosi.images/gcp/mkosi.postoutput.d/tar
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

Full normalised summary: `tests/fixtures/native-tree/summary-initramfs.json`.

## Root layout `verity`

### Tree

```
mkosi.conf
mkosi.images/
  azure/
    mkosi.conf
    mkosi.postoutput.d/
      vhd
    mkosi.repart/
      00-esp.conf
      10-root.conf
      11-root-verity.conf
  gcp/
    mkosi.conf
    mkosi.postoutput.d/
      tar
    mkosi.repart/
      00-esp.conf
      10-root.conf
      11-root-verity.conf
  payload/
    mkosi.conf
mkosi.repart/
  00-esp.conf
  10-root.conf
  11-root-verity.conf
payload/
  mkosi.conf
  mkosi.conf.d/
    10-platform.conf
    20-greeter.conf
    30-web.conf
    40-cloud-glue.conf
  mkosi.extra/
    etc/
      greeter/
        greeting
      hosts
      machine-id
      systemd/
        network/
          50-tundravm.network
    srv/
      www/
        index.html
    usr/
      bin/
        azure-complete-provisioning
      lib/
        systemd/
          system/
            azure-complete-provisioning.service
            boot-probe.service
            gcp-metadata-dns.service
            greeter.service
            systemd-networkd-wait-online.service.d/
              50-tundravm-network.conf
            web.service
        tundravm/
          boot-probe
          gcp/
            resolv.conf
        udev/
          google_nvme_id
          rules.d/
            65-gce-disk-naming.rules
  mkosi.finalize.d/
    50-finalize.sh
  mkosi.postinst.d/
    50-postinst.sh
  mkosi.profiles/
    dev/
      mkosi.conf
      mkosi.extra/
        etc/
          ssh/
            authorized_keys.d/
              root
            sshd_config.d/
              50-tundravm.conf
        usr/
          lib/
            systemd/
              system/
                ssh-host-keys.service
                ssh.service.d/
                  10-tundravm-needs.conf
      mkosi.postinst.d/
        50-postinst.sh
  mkosi.repart/
    10-root.conf
    11-root-verity.conf
  mkosi.skeleton/
    etc/
      systemd/
        system/
          minimal.target
```

### INI contents

`mkosi.conf`

```ini
[Config]
MinimumVersion=27.1
Dependencies=payload

[Distribution]
Distribution=debian
Release=trixie
Architecture=x86-64
Snapshot=20251113T083151Z

[Output]
Format=disk
Output=qemu
ImageId=fixture
OutputDirectory=mkosi.output
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70

[Build]
ToolsTree=default
ToolsTreeDistribution=debian
ToolsTreeRelease=trixie
ToolsTreeSnapshot=20251113T083151Z
ToolsTreePackages=qemu-utils
History=no
WithNetwork=no
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512

[Content]
SourceDateEpoch=0
Bootable=no
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
ExtraTrees=%O/payload.root.raw:/payload/root.raw
ExtraTrees=%O/payload.root-verity.raw:/payload/root-verity.raw

[Runtime]
Firmware=uefi
```

`mkosi.images/azure/mkosi.conf`

```ini
[Config]
Dependencies=payload

[Output]
Format=disk
Output=azure
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70

[Build]
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512

[Content]
Bootable=no
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
ExtraTrees=%O/payload.root.raw:/payload/root.raw
ExtraTrees=%O/payload.root-verity.raw:/payload/root-verity.raw
```

`mkosi.images/azure/mkosi.repart/00-esp.conf`

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
```

`mkosi.images/azure/mkosi.repart/10-root.conf`

```ini
[Partition]
Type=root
CopyBlocks=/payload/root.raw
UUID=9a94e2e0-42d7-582e-baec-aa6600df7a19
```

`mkosi.images/azure/mkosi.repart/11-root-verity.conf`

```ini
[Partition]
Type=root-verity
CopyBlocks=/payload/root-verity.raw
UUID=b79bc3b9-65fa-5287-852a-34759dffe29f
```

`mkosi.images/gcp/mkosi.conf`

```ini
[Config]
Dependencies=payload

[Output]
Format=disk
Output=gcp
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70

[Build]
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512

[Content]
Bootable=no
ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
ExtraTrees=%O/payload.root.raw:/payload/root.raw
ExtraTrees=%O/payload.root-verity.raw:/payload/root-verity.raw
```

`mkosi.images/gcp/mkosi.repart/00-esp.conf`

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
```

`mkosi.images/gcp/mkosi.repart/10-root.conf`

```ini
[Partition]
Type=root
CopyBlocks=/payload/root.raw
UUID=9a94e2e0-42d7-582e-baec-aa6600df7a19
```

`mkosi.images/gcp/mkosi.repart/11-root-verity.conf`

```ini
[Partition]
Type=root-verity
CopyBlocks=/payload/root-verity.raw
UUID=b79bc3b9-65fa-5287-852a-34759dffe29f
```

`mkosi.images/payload/mkosi.conf`

```ini
[Include]
Include=../../payload
```

`mkosi.repart/00-esp.conf`

```ini
[Partition]
Type=esp
Format=vfat
CopyFiles=/efi:/
SizeMinBytes=64M
Minimize=guess
```

`mkosi.repart/10-root.conf`

```ini
[Partition]
Type=root
CopyBlocks=/payload/root.raw
UUID=9a94e2e0-42d7-582e-baec-aa6600df7a19
```

`mkosi.repart/11-root-verity.conf`

```ini
[Partition]
Type=root-verity
CopyBlocks=/payload/root-verity.raw
UUID=b79bc3b9-65fa-5287-852a-34759dffe29f
```

`payload/mkosi.conf`

```ini
[Output]
Format=disk
Output=payload
Seed=6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70
ManifestFormat=json
SplitArtifacts=uki,partitions

[Content]
Bootable=yes
Bootloader=uki
CleanPackageMetadata=yes
KernelCommandLine=root=/dev/mapper/root systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
KernelModules=
    crc32c_generic
    dm_verity
    efivarfs
    erofs
    gve
    hv_netvsc
    hv_storvsc
    hv_utils
    hv_vmbus
    mana
    nvme
    overlay
    pci_hyperv
    sd_mod
    tdx_guest
    virtio_blk
    virtio_net
    virtio_scsi
Packages=
    passwd

[Validation]
Verity=hash
```

`payload/mkosi.conf.d/10-platform.conf`

```ini
[Content]
Packages=
    kmod
    linux-image-amd64
    systemd
    systemd-boot-efi
    systemd-sysv
    udev
```

`payload/mkosi.conf.d/20-greeter.conf`

```ini
[Content]
Packages=
    curl
```

`payload/mkosi.conf.d/30-web.conf`

```ini
[Content]
Packages=
    busybox
```

`payload/mkosi.conf.d/40-cloud-glue.conf`

```ini
[Content]
Packages=
    dmidecode
```

`payload/mkosi.profiles/dev/mkosi.conf`

```ini
[Output]
ImageId=%i-dev

[Content]
Packages=
    openssh-server
```

`payload/mkosi.repart/10-root.conf`

```ini
[Partition]
Type=root
Format=erofs
CopyFiles=/
Verity=data
VerityMatchKey=root
SplitName=root
Minimize=best
```

`payload/mkosi.repart/11-root-verity.conf`

```ini
[Partition]
Type=root-verity
Verity=hash
VerityMatchKey=root
SplitName=root-verity
Minimize=best
```

### Resolved summary, default selection (`mkosi --force summary --json`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| default-initrd | cpio | initrd | - | - | - | - | 20251113T083151Z |
| payload | disk | payload | default-initrd | - | fixture | 6a5f3c1e | 20251113T083151Z |
| main | disk | qemu | payload | - | fixture | 6a5f3c1e | 20251113T083151Z |
| tools | directory | mkosi.tools | - | - | - | - | 20251113T083151Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: root=/dev/mapper/root systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
- payload `SplitArtifacts`: uki, partitions; `Verity`: hash; `Bootloader`: uki
- payload `ExtraTrees`: <tree>/payload/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI, <tree>/mkosi.output/payload.root.raw:/payload/root.raw, <tree>/mkosi.output/payload.root-verity.raw:/payload/root-verity.raw
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

### Resolved summary, every node (`--profile=dev --dependency=azure --dependency=gcp`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| default-initrd | cpio | initrd | - | - | - | - | 20251113T083151Z |
| payload | disk | payload | default-initrd | dev | fixture-dev | 6a5f3c1e | 20251113T083151Z |
| azure | disk | azure | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| gcp | disk | gcp | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| main | disk | qemu | payload, azure, gcp | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| tools | directory | mkosi.tools | - | - | - | - | 20251113T083151Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, openssh-server, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: root=/dev/mapper/root systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
- payload `SplitArtifacts`: uki, partitions; `Verity`: hash; `Bootloader`: uki
- payload `ExtraTrees`: <tree>/payload/mkosi.extra, <tree>/payload/mkosi.profiles/dev/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- azure `PostOutputScripts`: <tree>/mkosi.images/azure/mkosi.postoutput.d/vhd
- gcp `PostOutputScripts`: <tree>/mkosi.images/gcp/mkosi.postoutput.d/tar
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI, <tree>/mkosi.output/payload.root.raw:/payload/root.raw, <tree>/mkosi.output/payload.root-verity.raw:/payload/root-verity.raw
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

Full normalised summary: `tests/fixtures/native-tree/summary-verity.json`.

## What CI proves

Nothing here builds an image on the host that produced this page (no privileges or user
namespaces there); CI (`.github/workflows/ci.yml`, on every push) does. The gate 1 list, for
both root layouts:

1. `mkosi build` of the fixture tree succeeds with `ToolsTree=default` from the pinned
   snapshot, producing `payload.efi`, `qemu.raw`, `azure.vhd` and `gcp.tar.gz`.
2. Two independent builds give identical digests for each of those artifacts.
3. The ESP holds the UKI under `RepartOffline=yes` and is a FAT32 firmware reads.
4. The wrappers' Debian base (a `Format=disk`, `Bootable=no` image with no packages) builds.
5. Verity: repart accepts `UUID=` on `CopyBlocks=` partitions, the UKI's initrd carries
   dm-verity and erofs, the raw disk boots with `systemd.volatile=overlay`, and the root and
   hash partitions survive both converters.
6. The converters: `azure.vhd` and `gcp.tar.gz` are well formed and GCE imports the archive.
7. `qemu.raw` boots through its ESP.

### Proven

Run [41140855928](https://github.com/Hyodar/tundra/actions/runs/41140855928) (branch
`ci/gate1-boot`, on master `99632e1`): `fixture-boot`, `verity`, `verity-cloud` and
`reproduce` green.

- **Items 1 and 4, initramfs** (`reproduce`): the fixture's `azure` and `gcp` variants built
  twice through `tundravm bake --backend local`. **Item 2, initramfs**: their `payload.efi`,
  `qemu.raw`, `azure.vhd` and `gcp.tar.gz` sha256 match (`reproduce-digests`).
- **Item 1, verity** (`verity`, `verity-cloud`): `tundravm bake tests/fixtures/native-tree/recipe.py
  --attr verity_recipe` builds the `default` variant and the `azure` and `gcp` variants
  (`default-initrd`, the `Format=disk` payload split into `payload.efi`, `payload.root.raw`
  and `payload.root-verity.raw`, each disk, each converter).
- **Item 3** (`test_fixture_boot.py`, `test_verity_boot.py`
  `test_qemu_disk_esp_is_a_fat32_the_firmware_reads`): with
  `SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512` the ESP is `FatLayout(bytes_per_sector=512,
  sectors_per_cluster=8, clusters=123013)` (initramfs, 504885248 bytes) and `clusters=114697`
  (verity, 470765568 bytes). Before that change it was 4096-byte sectors with 38260 clusters
  (service template, run 41135443949) and 29158 (verity), and OVMF answered
  `BdsDxe: failed to load Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x3,0x0): Not Found`,
  tried PXE and HTTP boot and stopped at `UEFI Interactive Shell v2.2` (runs 41131910330,
  41135443949, 41137178661).
- **Item 5** (`verity`, `verity-cloud`): repart accepts the `UUID=` on the copied partitions
  (`test_qemu_disk_carries_the_payload_partitions_block_for_block`: the GPT is ESP, root,
  root-verity, the last two under the UUIDs the UKI names, holding the split partitions'
  bytes); `test_azure_vhd_carries_the_verity_partitions` and
  `test_gcp_archive_carries_the_verity_partitions` check the same on `azure.vhd` and on the
  sparse `disk.raw` inside `gcp.tar.gz`. The disk boots from dm-verity, console excerpt:

  ```
  BdsDxe: starting Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x3,0x0)
  [    0.015321] Kernel command line: roothash=784caec014f3bd189650e94296c2387150370f4463ea6616f2bda33fba3a54ef root=/dev/mapper/root systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay console=ttyS0 ...
  [    2.139676] systemd[1]: Finished systemd-veritysetup@root.service.
  [    3.033622] erofs: (device dm-0): mounted with root inode @ nid 36.
  [    2.684745] systemd[1]: Mounted sysroot.mount.
  [    2.729340] systemd[1]: Finished systemd-volatile-root.service.
  [    3.028926] systemd[1]: Switching root.
  [    3.786063] sh[322]: boot-probe: mount / overlay overlay rw,lowerdir=/sysroot,upperdir=/run/systemd/overlay-sysroot/upper,workdir=/run/systemd/overlay-sysroot/work,uuid=on
  [    3.812617] sh[322]: boot-probe: dm root dm-0 uuid=CRYPT-VERITY-629384db01ff4c718d3e8f2550bd6602-root ro=1
  [    3.820078] sh[322]: boot-probe: usr-bin write=accepted
  [    3.912770] systemd[1]: Started web.service.
  [    3.960306] systemd[1]: Reached target minimal.target.
  ```

  The initrd opens `root` as a read-only dm-verity target, the kernel mounts erofs from it,
  `systemd-volatile-root` stacks a tmpfs overlay on it, and the host fetches the fixture's
  page through a QEMU forward. After the switch the erofs leaves the mount table with the
  initrd: `/` is the overlay (`lowerdir=/sysroot`). So a write under `/usr/bin` is
  accepted, and it lands in the volatile tmpfs upper layer, not on the read-only verity root.
- **Item 7** (`fixture-boot`, `verity`): `qemu.raw` boots with no `-kernel`, for the initramfs
  fixture and the verity fixture alike. The harness's argv
  (`tundravm.testing.boot(..., via="disk")`, the same disk path `deploy --target qemu` takes
  without TDX):

  ```
  qemu-system-x86_64 -machine q35,accel=kvm -cpu host -m 4G -smp 2 -no-reboot -display none
    -monitor none -serial file:<log>
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd
    -drive if=pflash,format=raw,file=<copy of OVMF_VARS_4M.fd>
    -drive file=<build>/default/output/qemu.raw,format=raw,if=virtio,snapshot=on
    -smbios type=11,value=io.systemd.stub.kernel-cmdline-extra=console=ttyS0 systemd.show_status=yes systemd.status_unit_format=name systemd.log_color=no systemd.journald.forward_to_console=1 panic=-1
    -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:<port>-:8080
  ```

  The initramfs fixture reaches `minimal.target` with `web.service` up and its page served
  through both `-kernel` (7.5s) and the ESP (7.9s). Both kernels print the UKI's command
  line with the SMBIOS words appended (the fixture's UKI carries no `.cmdline` of its own),
  and `/proc/cmdline`, which `boot-probe` reads, is the same line:

  ```
  BdsDxe: starting Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x3,0x0)
  [    0.017154] Kernel command line: console=ttyS0 systemd.show_status=yes systemd.status_unit_format=name systemd.log_color=no systemd.journald.forward_to_console=1 panic=-1
  [    1.768323] sh[187]: boot-probe: mount / rootfs rootfs rw,size=1974300k,nr_inodes=493575,inode64
  [    1.851231] systemd[1]: Started web.service.
  [    1.967380] systemd[1]: Reached target minimal.target.
  ```

  The verity disk boots in 10.1s, and its kernel and `/proc/cmdline` carry the UKI's line,
  `roothash=` included.

### Still open

- **The templates' boots** (`boot` job, `test_templates_boot.py`) are blocked on repository
  access, not on the boot path. Every `Image` implies the evidence service, which builds
  tundra-tools from `https://github.com/Hyodar/tundra-tools.git`, a private repository that
  the Actions token cannot read (`repository 'https://github.com/Hyodar/tundra-tools.git/'
  not found`, at lock, before any build). Before the templates became `Image` files, run
  41137178661 booted the minimal and service templates through `-kernel` with the UKI's
  command line asserted. Their `qemu.raw` boot through the ESP is the same path the fixture
  proves above. The `test` job's `test_templates_bake.py` fails on the same fetch.
- **Item 6, the GCE import**: `gcp.tar.gz` is checked to hold a GPT disk with every
  partition intact, but no job imports it into Compute Engine.
- **Item 2, verity**: the verity artifacts (`payload.root.raw`, `payload.root-verity.raw`
  and the disks) are built once per job. The two independent builds `reproduce` runs cover
  the initramfs layout only. The root hash matched across runs 41139713901 and 41140855928
  (`784caec0...`), but no job compares the digests.
- **The surge tree** is not built by any job.
- **The ESP size**: with 512-byte sectors, repart's `Minimize=guess` makes the ESP
  470-505MB for a 45MB UKI (it was 120-157MB with 4096-byte sectors). `qemu.raw` stays sparse,
  but `azure.vhd` is a fixed VHD of that size.

## Acceptance

Gate 1 was accepted on master `eebdcfa` (2026-10-30) with CI run 41126669505 green on all
three jobs then defined: `test`, `boot` (the minimal and service templates booted under QEMU
from `payload.efi`) and `reproduce`. Run 41140855928 adds the verity layout built end to end
and booted from dm-verity, the converters keeping its partitions, and `qemu.raw` booted
through its ESP. Still open: the templates' boots (repository access), the GCE import, a
reproduction of the verity artifacts, and the surge tree build.
