# Gate 1 evidence: the mkosi-native tree

Gate 1 (Substrate) of `docs/design/plan-v2-2026-10-30.md` section 8 keeps the fixture tree
with its INI contents and the resolved `mkosi summary --json` of every image node. This page
is generated from the committed evidence; the tests keep it honest:

- `tests/fixtures/native-tree/recipe.py`: the fixture recipe (one service, one component
  `Greeter`, a `dev` variant adding SSH by public key, `azure` and `gcp` output variants,
  Debian trixie at snapshot `20251113T083151Z`), with `Mkosi(root="initramfs")` and
  `Mkosi(root="verity")`.
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

`summary` runs unprivileged, so all of this is proven on any host; building is not.

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
    30-cloud-glue.conf
  mkosi.extra/
    etc/
      greeter/
        greeting
      hosts
      machine-id
    usr/
      bin/
        azure-complete-provisioning
      lib/
        systemd/
          system/
            azure-complete-provisioning.service
            gcp-metadata-dns.service
            greeter.service
        tundravm/
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

`payload/mkosi.conf.d/30-cloud-glue.conf`

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

- payload `Packages`: curl, dmidecode, kmod, linux-image-amd64, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: (none)
- payload `SplitArtifacts`: uki, kernel, initrd; `Verity`: auto; `Bootloader`: systemd-boot
- payload `ExtraTrees`: <tree>/payload/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

### Resolved summary, every node (`--profile=dev --dependency=azure --dependency=gcp`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| payload | uki | payload | - | dev | fixture-dev | 6a5f3c1e | 20251113T083151Z |
| azure | disk | azure | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| gcp | disk | gcp | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| main | disk | qemu | payload, azure, gcp | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| tools | directory | mkosi.tools | - | - | - | - | 20251113T083151Z |

- payload `Packages`: curl, dmidecode, kmod, linux-image-amd64, openssh-server, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: (none)
- payload `SplitArtifacts`: uki, kernel, initrd; `Verity`: auto; `Bootloader`: systemd-boot
- payload `ExtraTrees`: <tree>/payload/mkosi.extra, <tree>/payload/mkosi.profiles/dev/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- azure `PostOutputScripts`: <tree>/mkosi.images/azure/mkosi.postoutput.d/vhd
- gcp `PostOutputScripts`: <tree>/mkosi.images/gcp/mkosi.postoutput.d/tar
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

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
    30-cloud-glue.conf
  mkosi.extra/
    etc/
      greeter/
        greeting
      hosts
      machine-id
    usr/
      bin/
        azure-complete-provisioning
      lib/
        systemd/
          system/
            azure-complete-provisioning.service
            gcp-metadata-dns.service
            greeter.service
        tundravm/
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
KernelCommandLine=systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
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

`payload/mkosi.conf.d/30-cloud-glue.conf`

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

- payload `Packages`: curl, dmidecode, kmod, linux-image-amd64, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
- payload `SplitArtifacts`: uki, partitions; `Verity`: hash; `Bootloader`: uki
- payload `ExtraTrees`: <tree>/payload/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI, <tree>/mkosi.output/payload.root.raw:/payload/root.raw, <tree>/mkosi.output/payload.root-verity.raw:/payload/root-verity.raw
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

### Resolved summary, every node (`--profile=dev --dependency=azure --dependency=gcp`)

| image | format | output | dependencies | profiles | image id | seed | snapshot |
|---|---|---|---|---|---|---|---|
| default-initrd | cpio | initrd | - | - | - | - | 20251113T083151Z |
| payload | disk | payload | default-initrd | dev | fixture-dev | 6a5f3c1e | 20251113T083151Z |
| azure | disk | azure | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| gcp | disk | gcp | payload | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| main | disk | qemu | payload, azure, gcp | dev | fixture | 6a5f3c1e | 20251113T083151Z |
| tools | directory | mkosi.tools | - | - | - | - | 20251113T083151Z |

- payload `Packages`: curl, dmidecode, kmod, linux-image-amd64, openssh-server, passwd, systemd, systemd-boot-efi, systemd-sysv, udev
- payload `KernelCommandLine`: systemd.verity_root_data=/dev/disk/by-partuuid/9a94e2e0-42d7-582e-baec-aa6600df7a19 systemd.verity_root_hash=/dev/disk/by-partuuid/b79bc3b9-65fa-5287-852a-34759dffe29f systemd.volatile=overlay
- payload `SplitArtifacts`: uki, partitions; `Verity`: hash; `Bootloader`: uki
- payload `ExtraTrees`: <tree>/payload/mkosi.extra, <tree>/payload/mkosi.profiles/dev/mkosi.extra
- payload scripts: <tree>/payload/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh, <tree>/payload/mkosi.finalize.d/50-finalize.sh
- azure `PostOutputScripts`: <tree>/mkosi.images/azure/mkosi.postoutput.d/vhd
- gcp `PostOutputScripts`: <tree>/mkosi.images/gcp/mkosi.postoutput.d/tar
- main `ExtraTrees`: <tree>/mkosi.output/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI, <tree>/mkosi.output/payload.root.raw:/payload/root.raw, <tree>/mkosi.output/payload.root-verity.raw:/payload/root-verity.raw
- main `SourceDateEpoch=0`, `History=False`, `WithNetwork=False`, `MinimumVersion=27.1`, `RepartDirectories`: <tree>/mkosi.repart

Full normalised summary: `tests/fixtures/native-tree/summary-verity.json`.

## What CI must still prove

Nothing here builds an image (no privileges or user namespaces on the host that produced it).
The gate 1 row stays open until CI shows, for both root layouts:

1. `mkosi build` of the fixture tree and of the converted surge tree succeeds with
   `ToolsTree=default` from the pinned snapshot, producing `payload.efi`, `qemu.raw`,
   `azure.vhd` and `gcp.tar.gz` (`--dependency=azure --dependency=gcp`).
2. Two independent builds give identical digests for each of those four artifacts (and the
   verity `payload.root.raw`/`payload.root-verity.raw`).
3. The ESP: `Minimize=guess` on a `vfat` partition sizes it to the UKI under
   `RepartOffline=yes` (a large initramfs UKI does not fit `SizeMinBytes=64M`).
4. The wrappers' Debian base: a `Format=disk`, `Bootable=no` image with no packages still
   installs the distribution's essential set; acceptable but slow, and visible in the timings.
5. Verity: repart accepts `UUID=` on `CopyBlocks=` partitions, the UKI's default initrd
   carries dm-verity and erofs, and the raw disk boots under `mkosi vm` with
   `systemd.volatile=overlay`; the root and hash partitions survive both converters.
6. The converters: `azure.vhd` is a whole number of MiB with a valid backup GPT after the
   resize, `gcp.tar.gz` holds a sparse `disk.raw` of a whole number of GiB that GCE imports.
7. `mkosi vm` boots `qemu.raw` (UEFI firmware from the tools tree) for the default and the
   `dev` profile.

Which CI jobs cover them (`.github/workflows/ci.yml`):

- `reproduce` covers items 1 and 2 for the fixture recipe with the `initramfs` root: it bakes
  the `azure` and `gcp` variants (payload, QEMU disk and each wrapper) twice through
  `tundravm bake --backend local`, into two independent build directories, and fails
  unless the sha256 of `payload.efi`, `qemu.raw`, `azure.vhd` and `gcp.tar.gz` match; the
  two digest sets are uploaded as `reproduce-digests`. A successful run also proves item 4
  and the `qemu-img`, GPT-resize and tar steps of item 6 (not the GCE import). The verity
  layout and the surge tree are not built by any job yet.
- `test` runs `tests/integration/test_mkosi_smoke.py`, which builds the default and the
  cloud selection once and checks each disk's GPT holds one ESP (item 3 for a small UKI).
- `boot` bakes the `minimal` and `service` templates and boots their `payload.efi` under
  QEMU with `-kernel`; booting `qemu.raw` itself through its ESP (item 7) is not run yet.
