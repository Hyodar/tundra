#!/usr/bin/env bash
set -euo pipefail

KERNEL_CACHE="${BUILDDIR:-$BUILDROOT/build}/kernel-6.13.12-4efb76d1c52f"
KERNEL_VERSION="6.13.12"

if [ -d "$KERNEL_CACHE/done" ]; then
    echo "Using cached kernel build: kernel-6.13.12-4efb76d1c52f"
else
    rm -rf "$KERNEL_CACHE"
    mkdir -p "$KERNEL_CACHE"

    echo 'tundravm: kernel 6.13.12 is not pinned: run tundravm lock, then tundravm fetch' >&2
    exit 1

    cp payload/kernel/kernel.config "$KERNEL_CACHE/src/.config"
    cd "$KERNEL_CACHE/src"

    # Reproducibility environment
    export KBUILD_BUILD_TIMESTAMP="1970-01-01"
    export KBUILD_BUILD_USER="tundravm"
    export KBUILD_BUILD_HOST="tundravm"

    make olddefconfig
    make -j"$(nproc)" bzImage ARCH=x86_64

    mkdir -p "$KERNEL_CACHE/done"
fi

# Install kernel to destination
INSTALL_DIR="${DESTDIR}/usr/lib/modules/${KERNEL_VERSION}"
mkdir -p "$INSTALL_DIR"
cp "$KERNEL_CACHE/src/arch/x86/boot/bzImage" "$INSTALL_DIR/vmlinuz"

# Export for downstream phases
export KERNEL_IMAGE="$INSTALL_DIR/vmlinuz"
export KERNEL_VERSION="6.13.12"
