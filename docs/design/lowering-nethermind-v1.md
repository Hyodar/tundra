# Lowering spec: `Mkosi(dialect="nethermind-v1")`

What the `nethermind-v1` dialect must emit to reproduce `examples/surge-tdx-prover/mkosi/default/`
byte for byte from the declarations of `declarative-api.md` section 3. Derived from
`src/tundravm/compiler/emit_mkosi.py`, `src/tundravm/image.py`, the built-in modules, the
example modules and the committed tree. Literal contents live in
`examples/surge-tdx-prover/contents.py` (checked by `tests/test_surge_contents.py`).
Section 7 lists every place where the natural lowering of a declaration differs from the golden bytes.

## 1. Tree layout and modes

One directory per variant (`layout="directories"`), named after the variant; each is a complete
image (parent merged in). Per variant:

| Path | Source | Mode |
|---|---|---|
| `mkosi.conf` | section 2 | not chmod'ed (umask: 664 in the tree) |
| `kernel/kernel.config` | `Kernel.config`, read and written as UTF-8 text | not chmod'ed (664) |
| `mkosi.skeleton/init` | `File("/init", stage="skeleton")`, written first | 0755 |
| `mkosi.skeleton/<path>` | `File(stage="skeleton")` | `File.mode` (0644) |
| `mkosi.skeleton/etc/systemd/system/minimal.target` | `Debloat(minimize_systemd=True)`, only if no skeleton file already wrote it | not chmod'ed (664) |
| `mkosi.extra/<path>` | `File(stage="extra")`, generated configs | `File.mode` (0644) |
| `mkosi.extra/usr/lib/systemd/system/<name>` | `Unit` with content | 0644 |
| `mkosi.extra/usr/bin/runtime-init`, `.../runtime-init.service` | `Init` aggregate (section 5) | 0755, 0644 |
| `scripts/NN-<phase>.sh` | section 3 | 0755 |
| `scripts/{azure,gcp}-postoutput.sh` | `Variant.target` (section 6) | 0755 |

Directories are created by `mkdir(parents=True)` (umask: 775). `minimal.target` is
`MINIMAL_TARGET_UNIT` and has no trailing newline. `Tree.Entry.mode` for the umask-dependent
rows must be decided explicitly (the committed tree shows 664/775 under umask 002).

## 2. `mkosi.conf`

Sections in this order, each followed by one blank line; keys in exactly this order. Trailing
`# ...` annotations are not emitted; the two lines that start with `#` are:

```
[Distribution]
Distribution=debian                  # Recipe.base before "/"
Release=trixie                       # after "/"
Architecture=x86-64                  # x86_64 -> x86-64, aarch64 -> arm64
Mirror=<Recipe.mirror>               # only if set

[Output]
Format=uki
ImageId=<variant name>
ManifestFormat=json
OutputDirectory=build                # Setting("Output", "OutputDirectory")
Seed=630b5f72-...                    # Setting("Output", "Seed"); only when reproducible (epoch set)

[Build]
Environment=SOURCE_DATE_EPOCH=0      # KEY=VALUE pairs sorted by key; SOURCE_DATE_EPOCH from epoch
Environment=KERNEL_IMAGE             # passthrough names, declaration order, deduped;
Environment=KERNEL_VERSION           #   a Kernel with config adds these two
ToolsTreeMirror=<Recipe.tools_mirror>
WithNetwork=true
SandboxTrees=mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources
PackageCacheDirectory=mkosi.cache    # Setting("Build", "PackageCacheDirectory")

[Content]
SourceDateEpoch=0                    # only when reproducible
CleanPackageMetadata=true
Packages=                            # runtime Package names, sorted, deduped, "    " indent
    bubblewrap
    ...
BuildPackages=                       # build Package names, same format
    bc
    ...
Bootable=yes                         # Kernel and Format=uki
KernelCommandLine=<Kernel.cmdline>
# KernelVersion=6.13.12
# TDX-enabled kernel required        # Kernel.tdx
ExtraTrees=mkosi.extra
SkeletonTrees=mkosi.skeleton

SyncScripts=scripts/01-sync.sh       # one line per emitted phase script, PHASE_ORDER
BuildScripts=scripts/04-build.sh
PostInstallationScripts=scripts/06-postinst.sh
FinalizeScripts=scripts/07-finalize.sh
PostOutputScripts=scripts/azure-postoutput.sh   # cloud targets only, after the phase keys
```

The file ends with exactly one `\n`. Implicit packages: `Key(persist_in_tpm=True)` adds runtime
`tpm2-tools`; any `Disk` adds runtime `cryptsetup` (unconditionally, as `DiskEncryption` does); `tdxs()` and each runtime tool add build
`golang`, `git`, `build-essential`; each `Build` from `Git` adds build `git`. The package lists are
sorted sets, so these only change bytes when the recipe does not already declare them.

## 3. Phase scripts

Script name is `f"{index:02d}-{phase}.sh"` with the 1-based index in `PHASE_ORDER`
(`sync` 01, `skeleton` 02, `prepare` 03, `build` 04, `extra` 05, `postinst` 06, `finalize` 07,
`postoutput` 08, `clean` 09, `repart` 10, `boot` 11). Every script starts with
`#!/usr/bin/env bash\nset -euo pipefail\n\n` and ends with a single `\n` after its last line.
A hook renders as its script verbatim (one "line" that may contain newlines); `env` becomes a
`K=<shlex.quote(v)>` prefix sorted by key, `cwd` wraps as `(cd <quoted> && ...)`. A hook script
that ends in `\n` therefore leaves a blank line.

**01-sync.sh**: sync hooks in registration order. `backports()` (no mirror/release) is:

```
MIRROR=$(jq -r .Mirror "$BUILDDIR/config.json" 2>/dev/null || echo "")
if [ -z "$MIRROR" ] || [ "$MIRROR" = "null" ]; then
    MIRROR="http://deb.debian.org/debian"
fi
cat > "$BUILDDIR/debian-backports.sources" <<EOF
...two deb822 stanzas: Suites ${RELEASE}-backports and Suites sid...
EOF
```

plus the `SandboxTrees=` setting above (`mirror=` gives `MIRROR="<url>"`, `release=` appends
`RELEASE="<r>"`).

**04-build.sh** with a `Kernel` that has a config: `_render_kernel_build_script(kernel).rstrip()`,
`\n\n`, the build hooks joined by `\n`, final `\n`. Without a kernel config it is a plain script.
Kernel script facts:

- cache key `kernel-<version>-<h>`, `h = sha256(sha256(config bytes).hexdigest()).hexdigest()[:12]`
  (`4efb76d1c52f`); a missing config file hashes `str(path)` instead.
- clone line: `git clone --depth 1 --branch "v${KERNEL_VERSION}" \` then
  `        https://github.com/gregkh/linux "$KERNEL_CACHE/src"`: the ref is always `v<version>`
  and only the URL comes from the source.
- `cp kernel/kernel.config "$KERNEL_CACHE/src/.config"`, `KBUILD_BUILD_TIMESTAMP="1970-01-01"`,
  user/host `tundravm`, `make olddefconfig`, `make -j"$(nproc)" bzImage ARCH=x86_64`, installs
  `${DESTDIR}/usr/lib/modules/${KERNEL_VERSION}/vmlinuz`, ends `export KERNEL_VERSION="6.13.12"`.

Build hooks are one line each, in registration order: `tdxs`, `key-generation`,
`disk-encryption`, `secret-delivery`, `raiko`, `taiko-client`, `nethermind`. Shape:

```
if ! ([ -d "$BUILDDIR/<K>" ] && [ "$(ls -A "$BUILDDIR/<K>" 2>/dev/null)" ]); then <FETCH> && mkosi-chroot bash -c '<INNER>' && mkdir -p "$BUILDDIR/<K>" && <STORE...>; fi && <RESTORE...>
```

- `FETCH` (unpinned git): `git clone --depth=1 -b <ref> <url> "$BUILDROOT/build/<name>"` (ref and url
  `shlex.quote`d; `--recurse-submodules --shallow-submodules` with submodules).
- workdir `W = /build/<name>[/<subdir>]`. `INNER` per language (single quotes escaped as `'\''`):
  - Go (tundra-tools): `cd W && mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" -o ./build/<out> ./cmd/<out>`
  - Go (taiko-client): `cd W && GO111MODULE=on CGO_CFLAGS="-O -D__BLST_PORTABLE__" CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" go build -trimpath -ldflags "-s -w -buildid=" -o bin/taiko-client cmd/main.go`
    (env as a command prefix after `cd`, no `export`, no `mkdir`)
  - Cargo: `export RUSTFLAGS="..." CARGO_HOME=/build/.cargo ... CARGO_TERM_COLOR=never && cd W && cargo fetch && cargo build --release --frozen --features tdx --package raiko-host`
  - Dotnet: `export DOTNET_CLI_TELEMETRY_OPTOUT=1 ... NUGET_PACKAGES=/tmp/nuget && cd W && dotnet restore <proj> --runtime linux-x64 --disable-parallel --force && dotnet publish <proj> --configuration Release --runtime linux-x64 --self-contained true --output /build/nethermind/publish -p:Deterministic=true -p:ContinuousIntegrationBuild=true -p:PublishSingleFile=true ... -p:PublishRepositoryUrl=true`
  - env values: bare when they match `^[A-Za-z0-9_@%+=:,./-]*$`, else `"..."` with `\ " $` escaped;
    pairs keep declaration order.
- cache key `K`: default `<name>-<sha256(url)[:12]>-<ref>` (`tdxs-2bddc6a617e7-master`, same hash for
  `key-generation`, `disk-encryption`, `secret-delivery`); the three app builds use historical labels
  `raiko-feat_tdx`, `taiko-client-feat_tdx-proving`, `nethermind-1.32.3-linux-x64` (`/` -> `_`).
- per `Install`, in order, cache name `n = basename(dest)`:
  - file: store `install -D -m <mode:04o> "$BUILDROOT/build/<W rel>/<source>" "$BUILDDIR/<K>"/<n>`,
    restore `install -D -m <mode> "$BUILDDIR/<K>"/<n> "$DESTDIR/<dest without leading />"`.
  - directory: store `mkdir -p "$BUILDDIR/<K>"/<n> && cp -r "$BUILDROOT/build/<W rel>/<source>"/* "$BUILDDIR/<K>"/<n>/`,
    restore `mkdir -p "$DESTDIR/<dest>" && cp -r "$BUILDDIR/<K>"/<n>/* "$DESTDIR/<dest>"/`.
  - store and restore steps are each joined with ` && `.
- no `# unpinned: <ref>` marker line (every surge build has `mark_unpinned=False`).

**06-postinst.sh** is emitted when it has any line or debloat minimizes systemd. Order:

1. `mkosi-chroot groupadd [--system] [--gid N] <name>` per `GroupSpec` (none in surge).
2. `mkosi-chroot useradd [--system] [--home-dir H --create-home] [--shell S] [--uid N] [--gid G] [--groups a,b] <name>` per `UserSpec` (none in surge).
3. `mkosi-chroot systemctl enable <unit>` per enabled unit, declaration order (`foo` -> `foo.service`
   unless it has a `.`), with `runtime-init.service` appended last by the init pass.
4. `mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"`, then per enabled unit, same order,
   `ln -sf "/etc/systemd/system/<unit>" "$BUILDROOT/etc/systemd/system/minimal.target.wants/"`
   (the link target is under `/etc`, although the units ship in `/usr/lib`).
5. postinst hooks, registration order; a variant's hooks follow its parent's.
6. `mkosi-chroot systemctl disable <u1> <u2>` then `mkosi-chroot systemctl mask <u1> <u2>`, one line
   each, units deduped in declaration order.
7. Debloat block: blank line, `# Debloat: remove unwanted systemd binaries`,
   `systemd_bin_whitelist=("journalctl" "systemctl" "systemd" "systemd-tty-ask-password-agent")`
   (sorted, quoted), the `dpkg-query -L systemd | grep -E '^/usr/bin/'` loop; blank line,
   `# Debloat: mask unwanted systemd units`, `systemd_svc_whitelist=(...)` (13 default units plus
   `keep_units`, sorted), `SYSTEMD_DIR=...`, `mkdir -p "$SYSTEMD_DIR"`, the unit-mask loop; blank
   line, `# Set default systemd target`, `ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"`.

Golden step-5 lines, in order: the `efi_stub()` block (`EFI_SNAPSHOT_URL="<snapshot>"`,
`EFI_PACKAGE_VERSION="255.4-1"`, `DEB_URL=...`, `WORK_DIR=$(mktemp -d)`, `curl -sSfL ...`,
`cp ... "$BUILDROOT/tmp/"`, `mkosi-chroot dpkg -i /tmp/systemd-boot-efi.deb`,
`cp ...systemd-bootx64.efi ...linuxx64.efi.stub 2>/dev/null || true`, `rm -rf ...`), then

```
mkosi-chroot groupadd --system tdx
mkosi-chroot useradd --system --home-dir /home/tdxs --shell /usr/sbin/nologin --gid tdx tdxs
mkosi-chroot groupadd -r eth
mkosi-chroot useradd --system --home-dir /home/raiko --shell /usr/sbin/nologin --gid tdx raiko
mkosi-chroot useradd --system --home-dir /home/taiko-client --shell /usr/sbin/nologin --groups eth taiko-client
mkosi-chroot useradd --system --home-dir /home/nethermind-surge --shell /usr/sbin/nologin --groups eth nethermind-surge
mkosi-chroot usermod -a -G tdx nethermind-surge
```

all of which are raw hooks today, not `GroupSpec`/`UserSpec`.

**07-finalize.sh**: `# Debloat: clean files in var directories` with
`find "$BUILDROOT/var/cache" -type f -delete` and `.../var/log` (sorted); blank line,
`# Debloat: remove unnecessary paths` and one `rm -rf "$BUILDROOT<path>"` per default path plus
`extra_remove` minus `keep_paths`, sorted (30 lines); then blank line,
`# User-defined finalize commands` and the finalize hooks. The only one is the `IMAGE_VERSION` strip,
registered first when the recipe is reproducible, with a trailing space before the newline:
`sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release" `.

## 4. Units and `after_init`

- `Unit(content=None, enabled=True)`: enable line + wants link only, no file.
- `Unit(enabled=False, masked=True)`: one entry each in the disable and mask lines.
- `Unit(name, content)`: content verbatim at `mkosi.extra/usr/lib/systemd/system/<name>`.
- `after_init=True` on a hand-written unit: in the `[Unit]` section (up to the first blank line),
  `runtime-init.service` becomes the first value of `After=` and `Requires=`; a missing line is
  appended to the end of `[Unit]`, `After=` before `Requires=`. Golden results:
  `After=runtime-init.service tdxs.service` / `Requires=runtime-init.service tdxs.service`
  (raiko), and `After=runtime-init.service` / `Requires=runtime-init.service` inserted after
  `Description=` (taiko-client, nethermind-surge). `tests/test_surge_contents.py::after_init` is
  the reference implementation.
- Today the injection happens in two places. Generated units (`img.service()`) get
  `runtime-init.service` prepended to their `after`/`requires` tuples by `Image._apply_init` at
  compile time (every non-`.target` service of a profile that runs init). Hand-written units get it
  from `resolve_after()` at module-apply time, only if init fragments were already registered,
  which is why `tdxs.service`/`tdxs.socket` (applied before `KeyGeneration`) carry none. The
  dialect replaces both with the explicit flag: `tdxs(after_init=False)`.

## 5. Runtime init

When any `Init` fragment exists (keys 10, disks 20, secrets 30): fragments are deduped by
`(priority, script)` and stably sorted by priority. The script is
`"\n".join(["#!/bin/bash\nset -euo pipefail\n", *scripts])`, so each fragment (which ends in `\n`)
is preceded by a blank line. Fragments:

```
/usr/bin/key-gen setup /etc/tdx/key-gen.yaml
/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml
if [ -e /dev/mapper/crypt_disk_disk_persistent ]; then
    cryptsetup rename crypt_disk_disk_persistent cryptroot
fi
/usr/bin/secret-delivery setup /etc/tdx/secrets.yaml
```

(the rename block only when `Disk.mapper` differs from `crypt_disk_<name>`; paths `shlex.quote`d).
`runtime-init.service` is fixed text: `Description=Runtime Init`,
`After=network.target network-setup.service`, `Requires=network-setup.service`, `Type=oneshot`,
`ExecStart=/usr/bin/runtime-init`, `RemainAfterExit=yes`, `WantedBy=minimal.target`, trailing
newline. It is enabled last (section 3, step 3). Generated configs (trailing newline each):
`/etc/tdx/key-gen.yaml` (`strategy: "random"`, `tpm: true`, `size: 64`, `output_path`),
`/etc/tdx/disk-setup.yaml` (`strategy: "largest"` for `device=None`, `format`, `mount_at`,
`dirs: ["ssh", "data", "logs"]` via `json.dumps`, `encryption_key_path` = key output),
`/etc/tdx/secrets.yaml`, `/etc/tdx/secrets.json` (`"method": "http_post"`, sorted keys, 2-space
indent), `/etc/tdxs/config.yaml`.

## 6. Variant targets

`target="qemu"` adds nothing. Variant scripts follow their parent's within each phase.

- `azure`: runtime `dmidecode`; `mkosi.extra/usr/bin/azure-complete-provisioning` (0755) and
  `.../azure-complete-provisioning.service` (0644); postinst hooks
  `mkosi-chroot systemctl enable azure-complete-provisioning.service`,
  `mkosi-chroot mkdir -p /etc/systemd/system/minimal.target.wants`,
  `mkosi-chroot ln -sf /usr/lib/systemd/system/azure-complete-provisioning.service /etc/systemd/system/minimal.target.wants/azure-complete-provisioning.service`
  (after `usermod`, before `disable`); `scripts/azure-postoutput.sh` and
  `PostOutputScripts=scripts/azure-postoutput.sh`.
- `gcp`: runtime `udev`; `mkosi.extra/etc/hosts`, `mkosi.extra/etc/resolv.conf` (in addition to
  the skeleton one), `mkosi.extra/usr/lib/udev/rules.d/65-gce-disk-naming.rules`,
  `mkosi.extra/usr/lib/udev/google_nvme_id` (0755); `scripts/gcp-postoutput.sh` and
  `PostOutputScripts=scripts/gcp-postoutput.sh`.
- `devtools()` today: packages, `serial-console.service` file, then the postinst hooks
  `mkosi-chroot systemctl enable serial-console.service` and the root-password block (ends in
  `\n`, so a blank line follows).
- `mkosi.version` (`MKOSI_VERSION_SCRIPT`, 0755, at the emission root) only with
  `generate_version_script`; surge does not emit it.

## 7. Where the natural lowering differs from the golden bytes

| Declaration | Natural lowering | Golden bytes | Port choice |
|---|---|---|---|
| `Group("eth")` | `groupadd --system eth` in the step-1 prelude | `groupadd -r eth` as a step-5 hook after `tdxs` | dialect: groups render as hooks in declaration order, `-r` spelling for this group; or `Hook` |
| `Group("tdx")` (from `tdxs()`) | step-1 prelude | `groupadd --system tdx` as a step-5 hook after `efi_stub` | `tdxs()` lowering emits it at its declaration position |
| `User(...)` | `_useradd_command`: `--home-dir H --create-home --shell S ...` in step 2 | no `--create-home`; step-5 position; `--gid`/`--groups` last | dialect user spelling without `--create-home`, at declaration position |
| `User("nethermind-surge", groups=("eth", "tdx"))` | `--groups eth,tdx` | `--groups eth` + later `mkosi-chroot usermod -a -G tdx nethermind-surge` | dialect splits secondary groups into `usermod`, or `groups=("eth",)` + `Hook` |
| `User("raiko", primary_group="tdx")` | `--gid tdx` | `--gid tdx` | matches |
| `Build` env | one rule for all builds | cargo/dotnet: `export ... && cd W && ...`; taiko Go: `cd W && K=V ... go build` | taiko `env=()` with the prefix in `script`; others use `env` |
| nethermind `Build.script` | `--output "$PWD/publish"` (section 3 of the design) | `--output /build/nethermind/publish` | write the absolute workdir in the script |
| `Build` cache key | `<name>-<sha256(url)[:12]>-<ref>`, e.g. `raiko-c2fc7dafd8f0-feat_tdx` | `raiko-feat_tdx`, `taiko-client-feat_tdx-proving`, `nethermind-1.32.3-linux-x64` | dialect table of historical labels per build name |
| `Build` unpinned | `SourceBuild` default prefixes `# unpinned: <ref>` | no marker | dialect never emits it |
| runtime tools | build names from tool binaries (`key-gen`, `disk-setup`) | names `key-generation`, `disk-encryption`, `secret-delivery`; clone dirs and cache keys use them | `RuntimeTools` lowering uses the module names |
| `Kernel(source=Git(url, "v6.13.12"))` | clone `Git.ref` | `--branch "v${KERNEL_VERSION}"` from the version, URL from source | dialect ignores `Git.ref` (or requires `ref == "v" + version`) |
| `Kernel.config` path | any path | content copied as text; hash is of the bytes | matches when the file exists |
| `Setting("Build", "Environment", (KERNEL_IMAGE, KERNEL_VERSION))` | appended at the end of `[Build]`, possibly duplicating the kernel's auto passthrough | right after `SOURCE_DATE_EPOCH`, once each | dialect slots settings into fixed key order and dedupes |
| `Setting("Output", "OutputDirectory"/"Seed")`, `Setting("Build", "PackageCacheDirectory")` | appended | fixed slots shown in section 2 | same fixed key order |
| `backports()` | a `Hook` | hook plus a `SandboxTrees=` key between `WithNetwork` and `PackageCacheDirectory` | `backports()` returns both a `Hook` and a `Setting` |
| `Key(persist_in_tpm=True)` | nothing extra | runtime `tpm2-tools` in `Packages=` | key lowering adds the package (the design's package list omits it) |
| `Unit(..., enabled=True)` for `runtime-init.service` | not declared | enabled last, after `dropbear.service` | init pass appends it after all declared units |
| `tdxs(after_init=False)` units | `after_init` default | no runtime-init lines; `Requires=tdxs.socket` only | explicit flag, as in the design |
| `efi_stub()` | `Hook("postinst")` | first step-5 line, before the tdxs account lines | keep `efi_stub()` before `tdxs()` in `common.items` |
| reproducible strip | none declared | finalize hook with a trailing space | dialect adds it whenever `epoch is not None` |
| `File("/init", ..., stage="skeleton")` | an ordinary skeleton file | written before other skeleton files, 0755 | same bytes either way; mode 0o755 required |
| `Debloat()` | ordinary declaration | also writes `minimal.target` (no trailing newline) unless a skeleton file provides it | dialect owns the file |
| devtools `serial-console.service` | `Unit(enabled=True)` adds an enable line and a wants link | raw hook `mkosi-chroot systemctl enable serial-console.service`, no link | `Hook` if the devtools tree is ever pinned (not committed today) |
| azure enablement | `Unit(enabled=True)` | three raw `mkosi-chroot` hooks, link target under `/usr/lib` | target lowering emits the raw hooks |
| tree modes | `Entry.mode` per declaration | `mkosi.conf`, `kernel.config`, `minimal.target` and directories follow umask | `assert_tree` must not depend on umask for these |
