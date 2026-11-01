# Gate 1 evidence: the mkosi-native tree

Gate 1 (Substrate) of `docs/design/plan-v2-2026-10-30.md` section 8 keeps the fixture tree
with its INI contents and the resolved `mkosi summary --json` of every image node. This page
is generated from the committed evidence; the tests keep it honest:

- `tests/fixtures/native-tree/recipe.py`: the fixture recipe (a `greeter` service and its
  component `Greeter`, a `web` service serving a page over `Network`, a `boot-probe` task
  printing the command line and what the root is, a `dev` variant adding SSH by public key,
  `azure` and `gcp` output variants, Debian trixie at snapshot `20261018T082309Z`), with
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
- The ESP is a fixed `SizeMinBytes=128M`, `SizeMaxBytes=128M` (section 3.6 says
  `SizeMinBytes=64M`, minimized) formatted with
  `Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"` in the main image and each
  wrapper. The size rule, and why:
  - *FAT32 by cluster count.* UEFI types a FAT by its cluster count (FAT32 needs 65525), and
    repart (systemd 257, offline) formats the ESP `mkfs.vfat -F 32 -S 4096`: 4096-byte
    sectors, one per cluster, gave the fixture's ESP 29158 clusters (verity) and the service
    template's 38260, so OVMF saw a FAT16 it could not mount, found no
    `EFI/BOOT/BOOTX64.EFI` and fell through PXE and HTTP boot to its shell. mkfs.vfat takes
    the last `-S`: `-S512` (the disk's own sectors) and `-s1` (one sector per cluster) make
    every ESP from 34 MiB up FAT32 by count; `-F 32` refuses a smaller one instead of writing
    a FAT16 (dosfstools: "Not enough or too many clusters").
  - *Not minimized.* `Minimize=guess` sizes a vfat by formatting a 1 TiB sparse file, copying
    the files in and taking 1.5 times the bytes written (`context_minimize`, repart.c). On
    1 TiB with 512-byte sectors mkfs.fat writes 256 MiB of FATs, so the 65 MiB initramfs UKI
    got a 481 MiB ESP (504885248 bytes, 123013 clusters) and the 43 MiB verity one 449 MiB
    (470765568 bytes), and `azure.vhd`, a fixed VHD, carried all of it. `-s1` cannot be
    combined with it either: 1 TiB of 512-byte clusters is more than FAT32 can count, and
    mkfs.fat refuses the first format.
  - *128 MiB.* At that size the FAT32 has 258078 clusters and 126 MiB of data
    (`test_the_esp_is_a_fixed_fat32_by_cluster_count` formats it as repart does), room for
    every UKI tundravm ships: the fixture's 65 MiB (initramfs) and 43 MiB (verity), the
    templates' at most 72 MiB, surge's verity UKI (kernel and initrd only). A UKI over about
    125 MiB fails the disk's build (`mcopy`: disk full); `RootFS.verity()` keeps the OS out of
    the UKI. The size is the same on every build, whatever the UKI's, and the converters grow
    the disk around it as before.
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
Snapshot=20261018T082309Z

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
ToolsTreeSnapshot=20261018T082309Z
ToolsTreePackages=qemu-utils
History=no
WithNetwork=no
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"

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
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"

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
SizeMinBytes=128M
SizeMaxBytes=128M
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
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"

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
SizeMinBytes=128M
SizeMaxBytes=128M
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
SizeMinBytes=128M
SizeMaxBytes=128M
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
| payload | uki | payload | - | - | fixture | 6a5f3c1e | 20261018T082309Z |
| main | disk | qemu | payload | - | fixture | 6a5f3c1e | 20261018T082309Z |
| tools | directory | mkosi.tools | - | - | - | - | 20261018T082309Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: (none)
- payload `SplitArtifacts`: uki, kernel, initrd; `Verity`: auto; `Bootloader`: systemd-boot
- payload `ExtraTrees`: <tree>/payload/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 -s1 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

### Resolved summary, every node (`--profile=dev --dependency=azure --dependency=gcp`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| payload | uki | payload | - | dev | fixture-dev | 6a5f3c1e | 20261018T082309Z |
| azure | disk | azure | payload | dev | fixture | 6a5f3c1e | 20261018T082309Z |
| gcp | disk | gcp | payload | dev | fixture | 6a5f3c1e | 20261018T082309Z |
| main | disk | qemu | payload, azure, gcp | dev | fixture | 6a5f3c1e | 20261018T082309Z |
| tools | directory | mkosi.tools | - | - | - | - | 20261018T082309Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, openssh-server, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: (none)
- payload `SplitArtifacts`: uki, kernel, initrd; `Verity`: auto; `Bootloader`: systemd-boot
- payload `ExtraTrees`: <tree>/payload/mkosi.extra, <tree>/payload/mkosi.profiles/dev/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- azure `PostOutputScripts`: <tree>/mkosi.images/azure/mkosi.postoutput.d/vhd
- gcp `PostOutputScripts`: <tree>/mkosi.images/gcp/mkosi.postoutput.d/tar
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 -s1 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

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
Snapshot=20261018T082309Z

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
ToolsTreeSnapshot=20261018T082309Z
ToolsTreePackages=qemu-utils
History=no
WithNetwork=no
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"

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
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"

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
SizeMinBytes=128M
SizeMaxBytes=128M
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
Environment=SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"

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
SizeMinBytes=128M
SizeMaxBytes=128M
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
SizeMinBytes=128M
SizeMaxBytes=128M
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
| default-initrd | cpio | initrd | - | - | - | - | 20261018T082309Z |
| payload | disk | payload | default-initrd | - | fixture | 6a5f3c1e | 20261018T082309Z |
| main | disk | qemu | payload | - | fixture | 6a5f3c1e | 20261018T082309Z |
| tools | directory | mkosi.tools | - | - | - | - | 20261018T082309Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: root=/dev/mapper/root systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
- payload `SplitArtifacts`: uki, partitions; `Verity`: hash; `Bootloader`: uki
- payload `ExtraTrees`: <tree>/payload/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI, <tree>/mkosi.output/payload.root.raw:/payload/root.raw, <tree>/mkosi.output/payload.root-verity.raw:/payload/root-verity.raw
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 -s1 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

### Resolved summary, every node (`--profile=dev --dependency=azure --dependency=gcp`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| default-initrd | cpio | initrd | - | - | - | - | 20261018T082309Z |
| payload | disk | payload | default-initrd | dev | fixture-dev | 6a5f3c1e | 20261018T082309Z |
| azure | disk | azure | payload | dev | fixture | 6a5f3c1e | 20261018T082309Z |
| gcp | disk | gcp | payload | dev | fixture | 6a5f3c1e | 20261018T082309Z |
| main | disk | qemu | payload, azure, gcp | dev | fixture | 6a5f3c1e | 20261018T082309Z |
| tools | directory | mkosi.tools | - | - | - | - | 20261018T082309Z |

- payload `Packages`: busybox, curl, dmidecode, kmod, linux-image-amd64, openssh-server, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: root=/dev/mapper/root systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
- payload `SplitArtifacts`: uki, partitions; `Verity`: hash; `Bootloader`: uki
- payload `ExtraTrees`: <tree>/payload/mkosi.extra, <tree>/payload/mkosi.profiles/dev/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- azure `PostOutputScripts`: <tree>/mkosi.images/azure/mkosi.postoutput.d/vhd
- gcp `PostOutputScripts`: <tree>/mkosi.images/gcp/mkosi.postoutput.d/tar
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI, <tree>/mkosi.output/payload.root.raw:/payload/root.raw, <tree>/mkosi.output/payload.root-verity.raw:/payload/root-verity.raw
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `Environment`: SYSTEMD_REPART_MKFS_OPTIONS_VFAT=-S512 -s1 (each wrapper too), `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

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
  `test_qemu_disk_esp_is_a_fat32_the_firmware_reads`): run
  [41148955043](https://github.com/Hyodar/tundra/actions/runs/41148955043) (branch
  `ci/compiler-fixes`), with the fixed 128M ESP and `SYSTEMD_REPART_MKFS_OPTIONS_VFAT="-S512 -s1"`:
  the ESP is `FatLayout(bytes_per_sector=512, sectors_per_cluster=1, total_sectors=262144,
  clusters=258078, fat32_layout=True)`, 134217728 bytes, for both layouts (UKIs of 68019200
  bytes, initramfs, and 45276672, verity). `qemu.raw` is 135286784 bytes (initramfs) and
  262017024 (verity), `azure.vhd` 262144512 (verity); in run 41140855928, under
  `Minimize=guess` and `-S512` alone, they were 505954304, 598564864 and 598737408, the ESP
  `clusters=123013` (initramfs, 504885248 bytes) and `clusters=114697` (verity, 470765568
  bytes). `fixture-boot`, `verity`, `verity-cloud` and `reproduce` are green on it, and the
  `reproduce` digests match. Before the 512-byte sectors it was 4096-byte sectors with 38260
  clusters (service template, run 41135443949) and 29158 (verity), and OVMF answered
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

### A built kernel (plan 5.2 item 1, the `kernel` workflow)

`.github/workflows/kernel.yml` builds `tests/fixtures/kernel/recipe.py`: gregkh/linux
`v6.12.57` (`97a5dacb9050`) with `config.amd64_none_cloud-amd64` of Debian's
`linux-config-6.12` 6.12.57-1, `CONFIG_MODULES=n` and a fragment that only drops debug info
(`DEBUG_INFO_NONE=y`, no BTF), the composed kernel `Kernel.build` lowers to. Run
41299630516 (head `12e91cf`, platform snapshot `20261018T082309Z`) took 21m30s: 1m11s to
fetch the source and the config, 19m14s for the mkosi build that compiles it on the
runner's 4 cores without ccache (the build script has none), 11s to boot. Run 41299024098,
on the earlier snapshot `20251113T083151Z`, took 27m42s. The tree took 5 GB of disk.

- **The config diff**: Debian's config has 1136 `=m` options; after `olddefconfig` all
  1136 are `=y` and none dropped, so Kconfig promotes `m` to `y` under `CONFIG_MODULES=n`
  for this config and `mod2yesconfig` is not needed. Beyond that, only module-only options
  leave (`MODULE_SIG_*`, `MODPROBE_PATH`), and the toolchain lines change.
- **The drivers check**: all 30 required options (`CloudDrivers` of qemu, azure and gcp,
  the encrypted volume, the firewall and the verity root) are `=y` from Debian's config
  alone; none needed a fragment. `BLK_DEV_PCIESSD_MTIP32XX`, the denied one, is unset
  there already.
- **The boot**: `qemu.raw` boots through its ESP and the probe prints `release 6.12.57`,
  `module-files 0`, `drivers virtio_net virtio_blk virtio_scsi nvme gve mana` and
  `filesystems ext4 erofs overlay`. The Hyper-V drivers register only on Hyper-V, so no
  boot here shows them.
- The first run, 41295881308, found that a composed kernel had never compiled: `make` ran
  in mkosi's tools tree, which has no compiler. It now runs through `mkosi-chroot`.

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
- **The built kernel on a cloud**: no job boots it on Azure or GCP, nor under TDX.

## Bumping the snapshot

`LOCKED_SNAPSHOT` (`src/tundravm/_image/_values.py`) is the archive every `Debian()` without
`snapshot=` reads, for the packages and the tools tree alike. It moves in a tundravm
release, at least once per Debian point release, never by date in `check`:

1. Pick the newest `debian` timestamp at least two weeks old.
   `curl -sI https://snapshot.debian.org/archive/debian/<ts>/` answers 200 and
   `.../archive/debian-security/<ts>/` 200 or a 302 to an earlier one (mkosi reads both at
   the same ID); each `dists/trixie{,-security}/main/binary-amd64/Packages.xz` has the
   sha256 its `InRelease` lists.
2. Set it in `LOCKED_SNAPSHOT`, the cloud template's `SNAPSHOT` (`templates.py`), the
   fixture recipe, `test_mkosi_smoke.py`, `test_templates_bake.py` and the hint examples.
   Move the template's `EFI_STUB_VERSION` to the `systemd-boot-efi` that snapshot ships:
   the pool keeps only what a suite lists, so the old `.deb` is usually gone.
3. Check that each package the curated sets name (`BOOT_PACKAGES`, `KERNEL_BUILD_PACKAGES`,
   the shipped components, the templates) is in the new `Packages`; fix a renamed one.
4. Regenerate the goldens (`TUNDRAVM_UPDATE_GOLDEN=1 uv run pytest
   tests/compiler/test_native_tree.py`), the surge tree (`tundravm build
   examples/surge-tdx-prover/image.py --tree-only examples/surge-tdx-prover/mkosi`) and this
   page's INI and summary excerpts; run the gate.
5. Push a scratch branch: all seven jobs green, `reproduce` matching for both cloud
   profiles, the finalize check finding every curated module. Note fixture-boot's
   `payload.efi` sizes beside master's in the commit, then delete the branch.
6. Name the old and the new snapshot in the changelog: every lock reports `lock-changed`.

## Acceptance

Gate 1 was accepted on master `eebdcfa` (2026-10-30) with CI run 41126669505 green on all
three jobs then defined: `test`, `boot` (the minimal and service templates booted under QEMU
from `payload.efi`) and `reproduce`. Run 41140855928 adds the verity layout built end to end
and booted from dm-verity, the converters keeping its partitions, and `qemu.raw` booted
through its ESP. Run 41148955043 boots both layouts from the fixed 128M ESP (the size rule
is in "Deviations from section 3.6"). Still open: the templates' boots (repository access),
the GCE import, a reproduction of the verity artifacts, and the surge tree build.
