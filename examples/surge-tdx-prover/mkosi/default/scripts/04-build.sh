#!/usr/bin/env bash
set -euo pipefail

KERNEL_CACHE="${BUILDDIR}/kernel-6.13.12-48d53398a43c"
KERNEL_VERSION="6.13.12"

if [ -d "$KERNEL_CACHE/done" ]; then
    echo "Using cached kernel build: kernel-6.13.12-48d53398a43c"
else
    rm -rf "$KERNEL_CACHE"
    mkdir -p "$KERNEL_CACHE"

    git clone --depth 1 --branch "v${KERNEL_VERSION}" \
        https://github.com/gregkh/linux "$KERNEL_CACHE/src"

    cp kernel/kernel.config "$KERNEL_CACHE/src/.config"
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

if ! ([ -d "$BUILDDIR/tdxs-2bddc6a617e7-master" ] && [ "$(ls -A "$BUILDDIR/tdxs-2bddc6a617e7-master" 2>/dev/null)" ]); then git clone --depth=1 -b master https://github.com/Hyodar/tundra-tools.git "$BUILDROOT/build/tdxs" && mkosi-chroot bash -c 'cd /build/tdxs && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/tdxs ./cmd/tdxs' && mkdir -p "$BUILDDIR/tdxs-2bddc6a617e7-master" && install -D -m 0755 "$BUILDROOT/build/tdxs/build/tdxs" "$BUILDDIR/tdxs-2bddc6a617e7-master"/tdxs; fi && install -D -m 0755 "$BUILDDIR/tdxs-2bddc6a617e7-master"/tdxs "$DESTDIR/usr/bin/tdxs"
if ! ([ -d "$BUILDDIR/key-generation-2bddc6a617e7-master" ] && [ "$(ls -A "$BUILDDIR/key-generation-2bddc6a617e7-master" 2>/dev/null)" ]); then git clone --depth=1 -b master https://github.com/Hyodar/tundra-tools.git "$BUILDROOT/build/key-generation" && mkosi-chroot bash -c 'cd /build/key-generation && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/key-gen ./cmd/key-gen' && mkdir -p "$BUILDDIR/key-generation-2bddc6a617e7-master" && install -D -m 0755 "$BUILDROOT/build/key-generation/build/key-gen" "$BUILDDIR/key-generation-2bddc6a617e7-master"/key-gen; fi && install -D -m 0755 "$BUILDDIR/key-generation-2bddc6a617e7-master"/key-gen "$DESTDIR/usr/bin/key-gen"
if ! ([ -d "$BUILDDIR/disk-encryption-2bddc6a617e7-master" ] && [ "$(ls -A "$BUILDDIR/disk-encryption-2bddc6a617e7-master" 2>/dev/null)" ]); then git clone --depth=1 -b master https://github.com/Hyodar/tundra-tools.git "$BUILDROOT/build/disk-encryption" && mkosi-chroot bash -c 'cd /build/disk-encryption && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/disk-setup ./cmd/disk-setup' && mkdir -p "$BUILDDIR/disk-encryption-2bddc6a617e7-master" && install -D -m 0755 "$BUILDROOT/build/disk-encryption/build/disk-setup" "$BUILDDIR/disk-encryption-2bddc6a617e7-master"/disk-setup; fi && install -D -m 0755 "$BUILDDIR/disk-encryption-2bddc6a617e7-master"/disk-setup "$DESTDIR/usr/bin/disk-setup"
if ! ([ -d "$BUILDDIR/secret-delivery-2bddc6a617e7-master" ] && [ "$(ls -A "$BUILDDIR/secret-delivery-2bddc6a617e7-master" 2>/dev/null)" ]); then git clone --depth=1 -b master https://github.com/Hyodar/tundra-tools.git "$BUILDROOT/build/secret-delivery" && mkosi-chroot bash -c 'cd /build/secret-delivery && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/secret-delivery ./cmd/secret-delivery' && mkdir -p "$BUILDDIR/secret-delivery-2bddc6a617e7-master" && install -D -m 0755 "$BUILDROOT/build/secret-delivery/build/secret-delivery" "$BUILDDIR/secret-delivery-2bddc6a617e7-master"/secret-delivery; fi && install -D -m 0755 "$BUILDDIR/secret-delivery-2bddc6a617e7-master"/secret-delivery "$DESTDIR/usr/bin/secret-delivery"
if ! ([ -d "$BUILDDIR/raiko-feat_tdx" ] && [ "$(ls -A "$BUILDDIR/raiko-feat_tdx" 2>/dev/null)" ]); then git clone --depth=1 -b feat/tdx https://github.com/NethermindEth/raiko.git "$BUILDROOT/build/raiko" && mkosi-chroot bash -c 'export RUSTFLAGS="-C target-cpu=generic -C link-arg=-Wl,--build-id=none -C symbol-mangling-version=v0 -L /usr/lib/x86_64-linux-gnu" CARGO_HOME=/build/.cargo CARGO_PROFILE_RELEASE_LTO=thin CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 CARGO_PROFILE_RELEASE_PANIC=abort CARGO_PROFILE_RELEASE_INCREMENTAL=false CARGO_PROFILE_RELEASE_OPT_LEVEL=3 CARGO_TERM_COLOR=never && cd /build/raiko && cargo fetch && cargo build --release --frozen --features tdx --package raiko-host' && mkdir -p "$BUILDDIR/raiko-feat_tdx" && install -D -m 0755 "$BUILDROOT/build/raiko/target/release/raiko-host" "$BUILDDIR/raiko-feat_tdx"/raiko; fi && install -D -m 0755 "$BUILDDIR/raiko-feat_tdx"/raiko "$DESTDIR/usr/bin/raiko"
if ! ([ -d "$BUILDDIR/taiko-client-feat_tdx-proving" ] && [ "$(ls -A "$BUILDDIR/taiko-client-feat_tdx-proving" 2>/dev/null)" ]); then git clone --depth=1 -b feat/tdx-proving https://github.com/NethermindEth/surge-taiko-mono "$BUILDROOT/build/taiko-client" && mkosi-chroot bash -c 'cd /build/taiko-client/packages/taiko-client && GO111MODULE=on CGO_CFLAGS="-O -D__BLST_PORTABLE__" CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" go build -trimpath -ldflags "-s -w -buildid=" -o bin/taiko-client cmd/main.go' && mkdir -p "$BUILDDIR/taiko-client-feat_tdx-proving" && install -D -m 0755 "$BUILDROOT/build/taiko-client/packages/taiko-client/bin/taiko-client" "$BUILDDIR/taiko-client-feat_tdx-proving"/taiko-client; fi && install -D -m 0755 "$BUILDDIR/taiko-client-feat_tdx-proving"/taiko-client "$DESTDIR/usr/bin/taiko-client"
if ! ([ -d "$BUILDDIR/nethermind-1.32.3-linux-x64" ] && [ "$(ls -A "$BUILDDIR/nethermind-1.32.3-linux-x64" 2>/dev/null)" ]); then git clone --depth=1 -b 1.32.3 https://github.com/NethermindEth/nethermind.git "$BUILDROOT/build/nethermind" && mkosi-chroot bash -c 'export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 DOTNET_NOLOGO=1 DOTNET_CLI_HOME=/tmp/dotnet NUGET_PACKAGES=/tmp/nuget && cd /build/nethermind && dotnet restore src/Nethermind/Nethermind.Runner --runtime linux-x64 --disable-parallel --force && dotnet publish src/Nethermind/Nethermind.Runner --configuration Release --runtime linux-x64 --self-contained true --output /build/nethermind/publish -p:Deterministic=true -p:ContinuousIntegrationBuild=true -p:PublishSingleFile=true -p:BuildTimestamp=0 -p:Commit=0000000000000000000000000000000000000000 -p:PublishReadyToRun=false -p:DebugType=none -p:IncludeAllContentForSelfExtract=true -p:IncludePackageReferencesDuringMarkupCompilation=true -p:EmbedUntrackedSources=true -p:PublishRepositoryUrl=true' && mkdir -p "$BUILDDIR/nethermind-1.32.3-linux-x64" && install -D -m 0755 "$BUILDROOT/build/nethermind/publish/nethermind" "$BUILDDIR/nethermind-1.32.3-linux-x64"/nethermind && install -D -m 0644 "$BUILDROOT/build/nethermind/publish/NLog.config" "$BUILDDIR/nethermind-1.32.3-linux-x64"/NLog.config && mkdir -p "$BUILDDIR/nethermind-1.32.3-linux-x64"/plugins && cp -r "$BUILDROOT/build/nethermind/publish/plugins"/* "$BUILDDIR/nethermind-1.32.3-linux-x64"/plugins/; fi && install -D -m 0755 "$BUILDDIR/nethermind-1.32.3-linux-x64"/nethermind "$DESTDIR/usr/bin/nethermind" && install -D -m 0644 "$BUILDDIR/nethermind-1.32.3-linux-x64"/NLog.config "$DESTDIR/etc/nethermind-surge/NLog.config" && mkdir -p "$DESTDIR/etc/nethermind-surge/plugins" && cp -r "$BUILDDIR/nethermind-1.32.3-linux-x64"/plugins/* "$DESTDIR/etc/nethermind-surge/plugins"/
