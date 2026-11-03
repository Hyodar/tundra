# Boot basics

Date: 2026-11-03. Status: design, then landed in the commits that follow it. Gaps c1 to c3
of `flashbox-parity-2026-11-03.md`: what an image needs to format its volume, stop cleanly
and start a container on a built kernel.

## The programs a tool runs

Premise checked: trixie's `e2fsprogs` is `Priority: important`, not `Essential: yes`, and
mkosi installs only the essential set. A `Volume` added `cryptsetup` alone, so
`disk-setup` had no `mkfs.ext4` to format with.

`TOOL_PROGRAMS` (`tundravm._modules.base`) lists, for each `tools/` command an image runs,
the programs it executes and the package that ships each. `disk-setup` runs `cryptsetup`,
`mkfs.ext4`, `mount` and `umount`, so a volume now adds `cryptsetup`, `e2fsprogs` and
`mount`. `key-gen` runs `tpm2_*` for a TPM-sealed key alone (`TPM_PROGRAMS`), which adds
`tpm2-tools`.

Two tests hold the table to the code. One reads `tools/` (each command, the packages it
imports, their `Run(ctx, "prog", ...)` and `exec.Command("prog", ...)` calls) and requires
the table to name exactly those programs. The other walks the fixture's two trees and the
attested node's: each `Exec*=` program of each unit, each `/usr/bin` or `/usr/sbin` command
of a script a unit runs, and each program of a tool a unit runs must be a file of the tree,
a build's output, or a program of a listed or essential package (check's package table).

## Shutdown

The debloat masked every `systemd` unit but its keep list, so `systemctl poweroff` and
`reboot` had no target: a cloud stop was a power cut. The keep list now holds
`shutdown.target`, `umount.target`, `final.target`, `poweroff.target`,
`systemd-poweroff.service`, `reboot.target` and `systemd-reboot.service`. `halt`, `kexec`,
`soft-reboot`, `exit` and `ctrl-alt-del.target` stay masked. flashbox unmasks the reboot
path and `ctrl-alt-del.target`, and leaves poweroff masked.

The power button: flashbox has no handler (logind is masked, no acpid), so a cloud stop
cuts power after the grace period. `systemd-logind` needs D-Bus, which no image runs.
Every image now runs `tundravm-power-button.service`: Debian's `acpid` on the kernel's ACPI
netlink events, with one rule, `button/power` runs `systemctl poweroff`. The package's
units are masked. The ACPI `button` driver joins the curated modules. Azure's shutdown
request reaches `/sbin/poweroff` through `hv_utils`, which `systemd-sysv` ships.

Proof: fixture-boot sends `system_powerdown` (QEMU's ACPI power button) on the dev
instance's monitor, which `tundravm run` already opens. QEMU must exit by itself, and the
console must show the volume unmounted, `systemd-shutdown` detaching the dm-crypt mapping
and the kernel's `reboot: Power down`. The volume is closed by systemd, not by a
`systemd-cryptsetup@` unit: `disk-setup` opens it with `cryptsetup`, and its mount unit
stops in `umount.target`.

## loop on a built kernel

Premise checked: `<name>-loop.service` runs `modprobe loop`, and a built kernel has no
`/usr/lib/modules`, so modprobe fails and the container never starts. The loader now
skips when `/dev/loop-control` exists: devtmpfs makes it when a built-in `loop` registers.
The lines are `leftovers-11`'s, so the two branches merge cleanly.

A built kernel of an image that declares a `Container` gets `CONTAINER_KERNEL`, a fragment
with `CONFIG_BLK_DEV_LOOP=y` and `CONFIG_EROFS_FS=y`. Any profile counts: every profile
boots the one kernel. The drivers check already required both, and now the config asks for
them.
