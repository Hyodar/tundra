# Azure volumes: a data disk per volume, found by its LUN

Date: 2026-11-03. Closes the flashbox gap "`deploy azure` with a volume": Azure names a
data disk by the LUN it is attached at, never by a name the deploy chooses, so `deploy
azure` refused every image with a `Volume`.

## How a volume is found today

The image waits for `/dev/disk/by-id/*-tundravm-NAME` (`Volume.device`), one glob that
`disk-setup` matches on every target: QEMU's `serial=tundravm-NAME` gives
`virtio-tundravm-NAME`, GCP's device name `tundravm-NAME` gives `google-tundravm-NAME`
(Google's vendored udev rules). What the disk then is, is the LUKS2 header: the key opens
it or nothing does, and an `OperatorKey()` volume opens only for the owner its token pins.

## Spelling

Nothing new in the image file. `deploy azure --volume data=create:100GiB` and
`--volume data=existing:DISK` work as on GCP; `--plan` shows each volume's LUN.

## Lowering

- The volume at position N of `image.volumes` (first use, as the runtime manifest lists
  them) is attached at LUN N. An image that targets `azure` records `"lun": N` beside each
  volume in `/etc/tundravm/runtime.json`; the deploy reads it from there.
- `azure-vm-utils` (Debian trixie, 0.6.0-2) joins the Azure glue packages. It ships
  `80-azure-disk.rules` (`/dev/disk/azure/data/by-lun/N` for SCSI and NVMe data disks),
  its `azure-nvme-id` helper, a networkd file that leaves accelerated-networking VFs
  unmanaged, and initramfs-tools hooks nothing runs. It has no units and no maintainer
  scripts. Its rules match Microsoft's disk models only, so they are inert elsewhere.
- An image with volumes that targets `azure` ships
  `/usr/lib/udev/rules.d/81-tundravm-azure-volumes.rules`, one line per volume: the disk
  whose symlinks include `disk/azure/data/by-lun/N` also gets
  `disk/by-id/azure-tundravm-NAME`. `Volume.device` stays one glob on every target.
- The deploy creates each `create:SIZE` disk with `az disk create`, then passes every
  disk, in LUN order, to `az vm create --attach-data-disks` with
  `--data-disk-delete-option Detach`. azure-cli gives the i-th attached disk LUN i when
  nothing else takes a LUN (`normalize_disk_info`, azure-cli `18663606`), so the disks
  are there at first boot, before a `TpmSeal()` volume opens. `az vm show` then reads the
  LUNs back, and the receipt records them.

## Refusals

- A volume the image does not declare, or one it declares that has no disk: as on GCP.
- A size that is not whole GiB or TiB (`100GiB`, `100G`, `2TiB`).
- A build whose runtime manifest records no LUN (built before this change).
- Azure reports another disk at a volume's LUN: the deploy fails, receipt kept, and the
  hint says to delete the VM.
- In the guest, `disk-setup` refuses a LUKS2 volume whose init token names another
  volume. A swapped LUN with the same passphrase used to open the wrong data. The token
  is not authenticated: this stops operator mistakes, not the host. The key still decides.
- `deploy upgrade` and `rollback` stay GCP only.

## Proof

- Fakes: a table of `az` calls (create, attach existing, an undeclared volume refused, a
  disk Azure reports at another LUN refused) against recorded exchanges.
- A golden of the lowered rule and manifest entry for an azure image with two volumes.
- The `deploy azure --plan` snapshot, with the disk and its LUN.
- CI builds the fixture, which targets azure, so `azure-vm-utils` installs in trixie.
- Only a real Azure TDX VM shows the by-lun symlink and ours on a DCesv5 SCSI disk or a
  v6 NVMe namespace. QEMU has no Microsoft disk to match, and faking the by-lun link
  would prove nothing.
