#!/usr/bin/env bash
set -euo pipefail

# Kernel modules: mkosi kept KernelModules= and their dependencies (tundravm module sets
# v3); each must be in the image
kdirs=("$BUILDROOT"/usr/lib/modules/*/)
if [ ! -d "${kdirs[0]}" ]; then
    echo "tundravm: no kernel modules under /usr/lib/modules" >&2
    exit 1
fi
for kdir in "${kdirs[@]}"; do
    present=$({
        find "$kdir" -name '*.ko*' -printf '%f\n'
        sed 's|.*/||' "${kdir}modules.builtin" 2>/dev/null || true
    } | sed 's/\.ko.*//' | tr _ -)
    for module in erofs loop; do
        if ! grep -qxF "${module//_/-}" <<<"$present"; then
            echo "tundravm: kernel module $module is not in kernel $(basename "$kdir"):" \
                "drop it from Debian(kernel_modules=...) or pick a kernel that ships it" >&2
            exit 1
        fi
    done
    # modules.dep is still the kernel package's (mkosi runs no depmod when it only keeps
    # modules): its line for a listed module names every module that loads first.
    files=$(find "$kdir" -name '*.ko*' -printf '%P\n')
    for module in erofs loop; do
        line=$(grep -E "(^|/)${module//[_-]/[_-]}\.ko[^:/]*:" "${kdir}modules.dep" || true)
        for dep in ${line#*:}; do
            if ! grep -qxF "$dep" <<<"$files"; then
                echo "tundravm: kernel module $module needs $dep, which is not in the image" >&2
                exit 1
            fi
        done
    done
    # A "pre:" softdep of a kept module names a module or an alias (libcrc32c's crc32c),
    # which mkosi does not follow: one module in the image must provide it.
    while read -r _ owner rest; do
        grep -qxF "${owner//_/-}" <<<"$present" || continue
        pre=${rest#*pre:}
        [ "$pre" != "$rest" ] || continue
        for wanted in ${pre%%post:*}; do
            found=
            for candidate in "$wanted" $(awk -v a="$wanted" '$2 == a {print $3}' \
                "${kdir}modules.alias"); do
                if grep -qxF "${candidate//_/-}" <<<"$present"; then found=1; fi
            done
            if [ -z "$found" ]; then
                echo "tundravm: kernel module $owner loads $wanted first (softdep), and no" \
                    "module in the image provides it: add one to Debian(kernel_modules=...)" >&2
                exit 1
            fi
        done
    done < <(grep '^softdep ' "${kdir}modules.softdep" 2>/dev/null || true)
    # Index only the kept modules, so modprobe and systemd do not chase pruned ones.
    if [ -x "$BUILDROOT/usr/sbin/depmod" ]; then
        mkosi-chroot depmod -a "$(basename "$kdir")"
    fi
done
