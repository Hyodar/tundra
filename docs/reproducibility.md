# Reproducibility

tundravm makes two promises and gives you a check for each:

| Promise | Check |
|---|---|
| The same recipe compiles to the same mkosi tree, byte for byte | `tundravm compile --check`, `assert_tree`, `Tree.digest` |
| The same recipe and lockfile fetch the same inputs | `tundravm lock --check`, frozen `bake` |

Byte-identical *disk images* additionally depend on the Debian archive, the build backend and mkosi itself; pin the first with `Debian(snapshot=...)` and run the same backend.

## Reproducible output

The platform's `epoch` (`Debian(epoch=...)`) controls the time and identity settings the compiler emits:

| `epoch` | Emitted |
|---|---|
| `0` (default) | `SourceDateEpoch=0`, `Environment=SOURCE_DATE_EPOCH=0`, a stable `Seed=` (deterministic partition UUIDs), and a finalize hook that strips `IMAGE_VERSION` from `os-release` |
| any other integer | the same, with `SourceDateEpoch=` and `SOURCE_DATE_EPOCH` set to that value |
| `None` | none of the above: the build is not reproducible |

Every `mkosi.conf` also gets `ManifestFormat=json` and `CleanPackageMetadata=true`. Lowering never touches the network, and `Path` contents are read at compile time, so the tree depends only on the recipe, the files it reads, and the lock.

Pin the archive with the platform's `snapshot`, and the EFI stub with its `efi_stub`:

```python
from tundravm.declarative import Debian, Recipe

recipe = Recipe(
    name="node",
    platform=Debian("trixie", snapshot="20251113T083151Z", efi_stub="257.8-1~deb13u1"),
)
```

`snapshot` is the snapshot ID, written as mkosi's `Snapshot=`; mkosi reads it from `https://snapshot.debian.org` unless `mirror` names another root. `mirror` and `tools_mirror` are mirror roots that mkosi completes (`<root>/debian`, `<root>/archive/debian/<snapshot>`), never full archive URLs. `efi_stub` adds an `EfiStub` from that same snapshot, so the stub and the packages cannot disagree, and its version must be one that snapshot carries: the `cloud` and `prover` templates pair `20251113T083151Z` with `257.8-1~deb13u1`. `Backports` follows the platform's mirror and snapshot, and pins backports to 200 and sid to 100 so the release's packages win. Outside `nethermind-v1`, `tundravm lock` also pins the `EfiStub` package's sha256 as the source `efi-stub`, and `fetch` downloads it on the host (see [Hermetic builds](#hermetic-builds)).

## What the current dialect normalizes

With an `epoch` set, current-dialect trees also normalize what mkosi leaves to the build host. `nethermind-v1` keeps its pinned scripts, so the surge tree is unchanged.

| What | How | Why |
|---|---|---|
| Account databases | Every finalize script ends with a step that sorts `/etc/passwd` and `/etc/group` by numeric id (then name), puts `/etc/shadow` and `/etc/gshadow` in that same order (what `pwck --sort` and `grpck --sort` do; entries with no account go last, by name), sorts `/etc/subuid` and `/etc/subgid` by first id, and deletes the `/etc/*-` backups (`passwd-`, `group-`, `shadow-`, `gshadow-`, ...). It runs on the host with `sort` and `awk`, after debloat and finalize hooks, and rewrites each file in place, so `shadow` keeps its mode and group | `useradd`, `groupadd` and package maintainer scripts append in the order they run, which changes with package versions and declaration order (Debian #963788). The backups are the previous versions of the same files |
| Azure VHD and GCP disk: the ESP | Sized to the UKI plus 32 MiB, rounded up to whole MiB (64 MiB at least, so FAT32 fits); `mkfs.vfat -F 32 --invariant` with a volume serial from the first 8 hex digits of the UKI's sha256; `EFI/BOOT/BOOTX64.EFI` copied with `mcopy -m` from a tree that `touch` dates to `SOURCE_DATE_EPOCH`, under `TZ=UTC` and with `SOURCE_DATE_EPOCH` exported for mtools; one entry per directory, so directory order is fixed | `mformat` and `mkfs.vfat` pick a random serial, and mtools stamps entries with the wall clock in local time. A fixed 500 MB ESP grew with nothing and wasted upload size |
| The disk around it | `sgdisk` with fixed disk and partition GUIDs (`12345678-…`, `87654321-…`), the ESP at 1 MiB; the Azure disk is the ESP plus 2 MiB (whole MiB, as Azure requires), the GCP disk rounds up to whole GiB | `parted` writes random GUIDs |
| Azure VHD footer | `qemu-img convert -O vpc` writes the wall clock and a random unique id into the 512-byte footer; the script pins the timestamp to `SOURCE_DATE_EPOCH`, sets the unique id to the first 16 bytes of the disk's sha256, and recomputes the footer checksum (`python3`) | Without this no two VHDs match, even from the same UKI |
| GCP `tar.gz` | `tar --format=oldgnu --sparse --hole-detection=raw --sort=name --mtime=@$SOURCE_DATE_EPOCH --owner=0 --group=0 --numeric-owner`, piped to `gzip -n` | The sparse map otherwise depends on the build filesystem's extents, the owner on the build user, and gzip's header on the time |

**The UKI's `.osrel` and `.uname` are left alone.** flashbots-images strips `.osrel` after the build ("systemd-stub > 255 merges .osrel section into initrd at runtime"). From systemd 257 the stub does pass `.osrel` to the kernel as `/.extra/os-release` in a cpio it generates at boot, but it does not measure that cpio (`tpm_pcr = UINT32_MAX` in `src/boot/stub.c`, v257): it measures the `.osrel` section itself into PCR 11, and the firmware measures the whole UKI into PCR 4 / RTMR1. The section is not taken from the stub: mkosi passes `ukify --os-release @<image>/usr/lib/os-release --uname <kver>`, so `.osrel` is the image's own os-release and `.uname` the kernel version, both fixed by the image. The only build-dependent field, `IMAGE_VERSION`, is already stripped from os-release at finalize (above), before mkosi builds the UKI. Stripping `.osrel` would therefore change no measurement's reproducibility, only drop a section systemd tools read (`bootctl`, sd-boot menus). A different stub version does change the UKI and its measurements, through the stub's own code; pin it with `efi_stub`.

## Committed trees

Commit the compiled tree and check it in CI:

```bash
tundravm compile node.py --tree mkosi           # after every recipe change
tundravm compile node.py --tree mkosi --check   # in CI: exit 1 and list the stale files
```

In tests, `assert_tree(compile(recipe, lock=locked), "mkosi")` compares every path, byte, exec bit and symlink, and `TUNDRAVM_UPDATE_GOLDEN=1` rewrites the golden tree (see [testing](testing.md#golden-trees)).

The [`surge-tdx-prover`](../examples/surge-tdx-prover/) recipe is held to this standard: it compiles byte-for-byte to the committed nethermind-tdx tree for all four variants (`tundravm compile examples/surge-tdx-prover/image.py --tree examples/surge-tdx-prover/mkosi --check`, and `tests/compiler/test_surge_golden.py`).

## The lockfile

`tundravm lock RECIPE` writes `build/tundravm.lock` (JSON, version 5):

| Key | Holds |
|---|---|
| `recipe_digest` | SHA-256 of the canonical recipe payload of the locked variants |
| `sections` | One SHA-256 per section (below) |
| `recipe` | The payload itself, so drift can name the changed items |
| `dependencies` | The package list of each variant |
| `fetches` | One pin per source build and, in the current dialect, per built kernel (`kernel`, or `kernel-<variant>` when a variant's kernel source differs) per `EfiStub` package (`efi-stub`, or `efi-stub-<variant>`), and per download a composed kernel reads (`kernel-config` for a `DebianConfig` package, `kernel-patch-<name>` for an `Http` patch, `-<variant>` appended when a variant's differs): `name`, `kind` (`git`/`http`), `source` URL, requested `ref`, resolved `digest` (commit or sha256) |

Version 4 covers every input that defines the image:

| Section | Covers |
|---|---|
| `distribution` | `base`, `arch`, `mirror`, `tools_mirror`, `snapshot`, `epoch` |
| `compiler` | the tundravm version, the mkosi dialect and the recipe's mkosi options (sandbox files by sha256) |
| `default_profile`, `init_scripts` | the default variant's name; its `runtime-init` steps (step name and script sha256) in run order: the topological order of their `after`/`before` edges, the built-ins first and declared steps in declaration order among the steps ready together. A variant that adds steps of its own records its merged sequence as `variants.<name>.init_scripts`, so reordering steps drifts the lock |
| `variants.<name>.<section>` | `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets`, and `extends` for a variant with a parent |
| `variants.<name>.kernel` | the kernel's version, source identity, cmdline, `tdx` flag and the sha256 of its config bytes (`null` when the variant builds no kernel); a composed kernel adds `base` (the `DebianConfig` package URL and member, or a local base's sha256), the requested `options`, its `patches` (name and URL, or a local patch's sha256) and `modules` |
| `variants.<name>.mkosi` | the variant's mkosi options, only where they differ from the default variant's |
| `variants.<name>.debloat` | the complete debloat configuration: enabled, paths removed and skipped, per-variant paths, systemd minimisation, units and binaries kept, cleaned `/var` directories |

The recipe-wide `base` and `arch` sections of version 3 are gone (they live in `distribution`). File entries (`files`, `skeleton_files`) record each path's `kind` (`file`, `symlink` or `directory`), mode and content sha256. Because `compiler` records the tundravm version, the recipe digest changes with it: lock again after upgrading tundravm.

The lockfile is version 5 (it adds the build `toolchain`; see [reproducibility](reproducibility.md#pinned-toolchain)). An older lockfile does not load: every command that reads it fails with `E_LOCKFILE` (`Lockfile version 4 is not supported; this tundravm reads version 5.`) and the hint to run `tundravm lock RECIPE` again.

Version 2 lockfiles named the per-variant sections `profiles.<variant>.<section>`; they load with the names mapped.

The sections explain a mismatch; a frozen bake of every variant also compares the whole-recipe digest.

```console
$ tundravm lock node.py --check
~ variants.default.packages: +htop
~ variants.dev.packages: +htop
[exit 1]
```

A lock of every variant covers any subset: `lock --check --variant NAME`, a frozen `bake --variant NAME` and `lock_status(recipe, locked, variants=(NAME,))` compare only the selected variants' sections and the recipe-wide ones, and skip the whole-recipe digest. A lock written with `lock --variant NAME` covers only that variant.

`~` changed, `+` only in the recipe, `-` only in the lockfile. In Python, `lock_status(recipe, read_lock(path))` returns the same drift as `lock-changed`/`lock-added`/`lock-removed` diagnostics (`lock-stale` when only the whole-recipe digest differs), and `lint(recipe, lock=locked)` includes them. `lock_status(..., resolver=...)` also reports git refs that moved since the lock.

## Pinned toolchain

Pinned inputs are not enough: two builders with the same lockfile must also run the same tools. `tundravm lock` records the build toolchain beside the recipe (lockfile version 5; an older lockfile fails with `E_LOCKFILE`: lock again). The `toolchain` section is not part of the recipe digest:

```json
"toolchain": {
  "tundravm": "0.1.0+aa9d9dcd0516",
  "mkosi": {"version": "25.3", "source": "nix", "rev": "56c02bc00adcf003215cc4bd996d6efaf4cff188"},
  "systemd": "257",
  "nixpkgs": {"rev": "56c02bc00adcf003215cc4bd996d6efaf4cff188", "narHash": "sha256-...", "lastModified": 1787498568},
  "measure": {"gcp-firmware-table": "dstack-mr-gcp@b16e08b", "tundravm-measure": "1.0+attest-12f1e29.dstack-mr-gcp-b16e08b"}
}
```

`measure` is the offline register replay `tundravm measure` runs and the GCP firmware table it embeds ([policy](policy.md#sources)); both move only with the tundravm version.

- **nix and lima backends: the lock decides.** `lock` resolves `nixos-unstable` to a rev and narHash (keeping the previous lock's pin unless `--update toolchain`) and reads that nixpkgs' mkosi and systemd versions. The generated flake takes nixpkgs from `github:NixOS/nixpkgs/<rev>` with a `flake.lock` holding the narHash, refuses to evaluate when that nixpkgs' mkosi is not the locked version, and runs mkosi with a `PATH` of Nix tools only (no host passthrough). The nix backend enters that flake even from inside another Nix shell; an ambient mkosi never runs. When nixpkgs cannot be resolved (`--offline`, no `nix` on this host) the lock says why in `unpinned` and `lock` warns `toolchain not pinned: ...`; the build then follows `nixos-unstable`.
- **local backend: the lock records, tundravm compares.** `mkosi` comes from tundravm's Python environment (`source: "pip"`, with the commit of a git install) or from `PATH` (`"host"`); systemd is `ukify --version`.
- **Drift.** `lock --check` and `lint --lockfile` report `toolchain-drift`, one finding per field the running toolchain differs on (`toolchain mkosi: locked 25.3 (host), running 26 (host)`). It is a warning; `--strict`, or `strict_toolchain = true` in `[tool.tundravm]`, makes it an error.
- **Rosetta.** The Lima VM runs without Rosetta: x86-64 builds under Rosetta on Apple silicon are not reproducible. `LimaMkosiBackend(rosetta=True)` opts in; `doctor` warns, and the bake, its evidence and the lock record `reproducible: not guaranteed (rosetta)`.
- **Bake, evidence, witness.** A bake records the toolchain it ran in `bake-result.json`; the nix and lima backends ask the pinned shell (on the host, in the VM) which mkosi it ran and records that (`mkosi.path`, the `/nix/store` executable, and the version it reports), failing the bake when the version is not the locked one; `evidence` shows it; `witness` records its own and prints `toolchain: same` or `toolchain: different (mkosi 26 (nix) vs 25 (nix), ...)`. A witness that matches on a different toolchain strengthens the release. `verify --min-witnesses N` counts witnesses on the release's toolchain by default (`--same-toolchain`: what the lockfile claims), or one per distinct toolchain with `--distinct-toolchains`.

## Pinned sources

The recipe records what you asked for (`Git(url, "master")`), never the commit, so locking does not make its own lockfile stale. `tundravm lock` resolves:

- every `Git` ref to a commit, with `git ls-remote`;
- every `Http` source without `sha256` to the sha256 of its download.

`compile`, `diff`, `fetch` and `bake` read `build/tundravm.lock`: the build gets exactly the pinned commit, or the download with the pinned hash. In Python, `compile(recipe, lock=locked)` applies the pins and unpinned current-dialect builds emit failure hooks (`compile(recipe)` without a lock).

- `lock` keeps every existing pin whose source is unchanged; `--update NAME` re-resolves one source (`--update kernel` or `--update kernel-<variant>` for a kernel).
- A build whose source differs between variants (a `dev` variant that replaces `app` with another ref, say) is pinned once per variant under `<variant>/<name>` (`default/app`, `dev/app`); its drift line is `sources.<variant>.<name>`. `--update dev/app` re-resolves the `dev` pin only, `--update app` every variant's.
- `lock` tries every source and writes nothing unless all resolve; one `E_LOCKFILE` error lists each failure as `<name>: git <url> @ <ref>: <reason>` (`ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>`, `timed out after 60s`). A dead upstream ref therefore shows up at `lock` together with every other failure, not one per run.
- `lock --offline` reuses the pins and lists every source without one in a single error: `Cannot lock offline: N sources need the network to resolve:`.
- An unpinned build is the `source-unpinned` lint warning. `lock --check` never uses the network; drift shows as `+ sources.<name>: source <name> is not pinned` or `~ sources.<name>: <old> -> <new>`.
- A `Git` ref that is already a 40-hex commit, or an `Http` source with `sha256`, is immutable and needs no resolution.

## Sources fetched on the host

In the current dialect the build sandbox never fetches a source. `tundravm fetch RECIPE`, which `bake` runs first, checks every pinned source build and built kernel out on the host, as the invoking user, into `build/.sources/<name>-<pin12>-<id8>/`: git at the pinned commit, http verified against the pinned sha256. `id8` is the first 8 hex of a sha256 over what the checkout holds besides the pin (url and kind, and for git the subdirectory and submodules), so a new pin or a changed declaration gets a new checkout.

A JSON marker, `.tundravm-complete`, records each completed checkout: its pin, that source identity and, for http, a manifest of every file's sha256 (and exec bit) or symlink target. Before reusing a checkout, `fetch`, `bake --no-fetch` and `status` verify it: the marker names the pin and the same source; for git, `HEAD` is the pin, `git status` lists nothing (untracked and ignored files included) and requested submodules sit at their recorded commits; for http, the files match the manifest. A checkout without a marker for the pin is fetched again. A modified one fails with `E_SOURCE` (`source <name> checkout modified/incomplete: run tundravm fetch --force`), and `tundravm fetch RECIPE --force` checks every source out again, replacing the checkouts.

Outside `nethermind-v1`, a source build caches its output under `<namespace>-<fingerprint16>`: `cache_key` (default: the build's name) is the namespace, and the fingerprint is the first 16 hex of a sha256 over the source pin (with git `subdir` and `submodules`), the build script or recipe, the install steps, the toolchain (recipe kind and packages) and the distribution the build compiles against: base, `arch`, mirror, snapshot, the variant's repositories and build packages, and its mkosi `Build` settings (environment, sandbox trees and files, verbatim `Build` keys such as `ToolsTree`). Changing any of them rebuilds instead of reusing a stale result, so a persistent `BuildDirectory` never restores a binary built against another distribution or toolchain. A cached directory artifact is stored and restored whole (`cp -a "$src/."`: dotfiles, symlinks, modes and empty directories). `nethermind-v1` keeps its historical keys.

The backend mounts `build/.sources` into the build and each hook copies its checkout; a kernel's is copied without `.git`. `inspect` shows each source's `pinned=` commit.

A missing or incomplete checkout fails `bake --no-fetch` (and `bake --offline`) before mkosi runs with `E_STATE`, naming the `tundravm fetch` to run. [Hermetic builds](#hermetic-builds) extends this to dependencies and the build sandbox's network.

A hook whose source has no pin fails in the build with `run tundravm lock, then tundravm fetch`, so an unpinned source never builds from whatever the ref points to that day. `nethermind-v1` recipes, such as `surge-tdx-prover`, keep the historical tree's in-sandbox clones and have no kernel pins.

A lockfile written for a current-dialect recipe with a source build or a built kernel before sources moved to the host needs re-locking: run `tundravm lock RECIPE` once and commit the result.

## Source builds

In the current dialect the `Go` and `Cargo` recipes (and `GoBinary`, `CargoBinary`) build reproducibly and auditably by default, after flashbots-images' `scripts/build_rust_package.sh` and its Go builds:

- **Go:** `go build -trimpath -buildvcs=false -ldflags "-s -w -buildid="` with `CGO_ENABLED=0` (no host C toolchain or libc in the binary; `cgo=True` or a `CGO_ENABLED` in `env` keeps cgo). `-trimpath` drops host paths, `-buildvcs=false` the VCS stamp of the checkout, `-buildid=` the content-derived build id. Go always embeds its module list (`go version -m`), which `tundravm sbom --binaries` reads.
- **Rust:** `cargo auditable build --release --frozen` (the `cargo-auditable` build package is added), which embeds the crate graph in a `.dep-v0` section, with `-C target-cpu=generic` (no build-host CPU features), `-C link-arg=-Wl,--build-id=none`, `-C symbol-mangling-version=v0` and `--remap-path-prefix=<source tree>=/build`, passed as `--config 'build.rustflags=[...]'` so they merge with the project's own `.cargo/config.toml` rustflags (an exported `RUSTFLAGS` would discard those; a `RUSTFLAGS` in `env` gets the flags prepended instead). `CARGO_PROFILE_<PROFILE>_CODEGEN_UNITS=1` and `..._INCREMENTAL=false` remove parallel-codegen and incremental nondeterminism. Symbols are not stripped and `lto`/`panic` stay the project's choice.
- Both export `SOURCE_DATE_EPOCH` (the recipe's `Debian(epoch=)`, which mkosi passes to the build, else `0`) for build scripts and C compilers that stamp dates.

`Go(reproducible=False)` and `Cargo(reproducible=False, auditable=False)` render the plain commands. The fields default to `None`, which the `nethermind-v1` dialect reads as off, so its historical hooks (the surge tree) are unchanged, and which leaves the recipe payload, and so the lockfile, as before; the mounted hook's cache fingerprint covers the decided flags, so an upgrade rebuilds instead of reusing a binary built without them.

## Hermetic builds

`tundravm fetch` also prefetches the dependencies of every `Go`, `Cargo` and `Dotnet` build, with the host's toolchain, into `build/.sources/deps/`: `go mod download` into `deps/go` (a `GOMODCACHE`), `cargo fetch --locked` into `deps/cargo` (a `CARGO_HOME`) and `dotnet restore --runtime RID --packages deps/nuget`. Each runs as you in a scratch copy of the checkout, and a marker `deps/<name>-<pin12>-<id8>.<cache>.json` records the command and environment it ran and a content list of what the build needs from the cache: each Go module directory and `.mod` file `go.sum` names, each `.crate` of `Cargo.lock` (size and sha256), each NuGet package directory the restore's `project.assets.json` lists. The caches are verified by content: a later fetch keeps a cache only while every listed entry is still there and unchanged, prefetches it again otherwise (`deps: go: prefetched (was incomplete: deps/go/<entry> is missing or changed)`), and warns `deps: go: incomplete (...)` when it cannot (no host toolchain, or offline). `tundravm fetch --force` removes the caches and prefetches them again. In the current dialect the build hook copies the cache into the build and points the toolchain at it (`GOMODCACHE`, `CARGO_HOME`, `NUGET_PACKAGES`; with `GOPROXY=off`, `CARGO_NET_OFFLINE=true` or .NET `RestoreSources` when the script has no network). A host without the toolchain skips the prefetch with a notice (`deps: skipped (no host go)`) and a failed prefetch is a warning (`deps: failed (<reason>)`); that build then downloads its dependencies in the sandbox, as it did before.

`EfiStub` is a source too: outside `nethermind-v1`, `lock` pins its `.deb`'s sha256 as `efi-stub` (`efi-stub-<variant>` where a variant's package differs), `fetch` downloads it on the host, and the postinst hook checks the mounted copy against the pin and installs it with `dpkg -i`. Unpinned, the hook downloads it in the sandbox as before; `nethermind-v1` keeps its `curl`. A current-dialect lockfile written before `EfiStub` became a source drifts as `+ sources.efi-stub` under `lock --check`: run `tundravm lock RECIPE` once and commit the result.

`tundravm bake --offline`, or `Policy(network_mode="offline")` in the recipe, then builds without network: mkosi runs with `--with-network=no`, so build and postinst scripts reach nothing, and nothing is downloaded before it either: the fetch step reuses complete checkouts and fails with `E_POLICY` (`Cannot fetch 'app': policy network_mode is offline.`) when one, the `EfiStub` package included, is missing, and the bake fails with `E_STATE` before mkosi runs when a language build's dependency cache is missing (`Source build 'app' has no prefetched go dependencies, and an offline bake gives the build no network to download them.`, with the `tundravm fetch` to run on a host that has `go`) or incomplete (`Source build 'app' has an incomplete go dependency cache: deps/go/<entry> is missing or changed, ...`, naming the missing entry). With `--no-fetch`, a missing checkout is `E_STATE` as well. A `nethermind-v1` recipe with source builds is refused offline (`E_POLICY`): its hooks clone inside the sandbox. mkosi still installs the distribution packages from the configured mirror, so the builder needs that mirror (or a local copy of the snapshot) and nothing else.

An air-gapped bake is then: run `tundravm fetch RECIPE` on a host with network access and the toolchains, copy `build/` (lockfile and `.sources/`, `deps/` included) to the builder, and run `tundravm bake RECIPE --offline` there.

Three caveats follow from the cache being filled by the host's toolchain and read by the image's:

- **Cargo.** Cargo 1.85 renamed the registry cache's directories, so the host's and the image's cargo must both be 1.85 or newer for the build to find the cache.
- **.NET.** The host's .NET SDK must match the image's: the restore resolves package versions (runtime packs included) for the SDK that ran it.
- **Go.** A `go.mod` whose `go` or `toolchain` line asks for a newer Go than the image's makes the build download a toolchain, which the module cache does not cover and an offline bake cannot do.

## Frozen bakes

`tundravm bake` is frozen whenever `build/tundravm.lock` exists (or `--lockfile` is given): a recipe that drifted from the lock fails at the `verify lockfile` step with `E_LOCKFILE` and the list of drifted sections. The check follows the subset rule above, so `bake --variant NAME` works against the lock of every variant. The Python `bake()` always takes a `Lock`. Each `Artifact` records the recipe digest, the lockfile digest and the tree digest it was built from, and `bake-result.json` its sha256 (and, for a disk, the path and sha256 of the UKI it boots, which `measure` hashes too before measuring through it): `measure` and `deploy` (and `verify_artifact`, `status --verify`) hash the file first and refuse one that changed since the bake with `E_ARTIFACT_CHANGED`.

## Proving and recording a bake

The checks above pin the inputs; `tundravm bake --verify-reproducible` checks the output. It bakes the same selection a second time into `BUILD/.reproduce`, from the same lockfile and the same `BUILD/.sources` checkouts, compares every artifact's sha256, and records the outcome as `declarative.reproducibility` in `bake-result.json`, for the bake and for each variant it baked (a state: `running`, then `matched`, `different`, `error` or `interrupted`; `status` shows it per artifact): `reproducible: yes (N artifacts match a second build)`, or `E_REPRODUCIBILITY` with the second build kept for `tundravm diff RECIPE --tree BUILD/.reproduce/mkosi` and diffoscope (see [CLI: Bake](cli.md#bake)). `tundravm sbom` is the record of what that bake contains: mkosi's package manifest (every installed package and version from the snapshot), the lockfile's source pins and the recipe metadata and digests, as SPDX 2.3 or CycloneDX 1.5 with a package URL per component (the purl rules are in [CLI: SBOM](cli.md#sbom)). Its lists are sorted and its ids derive from the content, so with `SOURCE_DATE_EPOCH` set a reproducible bake gives the same document too, ready to publish next to the measurements.

## mkosi

The `local` backend needs mkosi v25 or newer (v26 recommended) on `PATH` and checks the version before building. The `lima` and `nix` backends bring their own, from the nixpkgs the lockfile pins (see [Pinned toolchain](#pinned-toolchain)).

```bash
pip install 'mkosi @ git+https://github.com/systemd/mkosi.git@v26'
```

## CI

```bash
tundravm ci node.py --tree mkosi    # lint --strict, compile --check, lock --check
```

Add `Policy(require_frozen_lock=True, mutable_ref_policy="error")` to refuse unpinned bakes and unpinned sources outright (see [policy](policy.md)).

Emitted trees are umask-independent: files are written `0644` (generated scripts `0755`, declared files at their declared mode) and directories `0755`, and `Tree.digest` hashes each file's exec bit rather than its full mode.

In the current dialect a pinned kernel's cache key is `kernel-<version>-<cfg12>-<pin12>-<dist16>`, where `dist16` hashes the variant's build distribution (base, arch, mirror, snapshot, repositories, build packages, Build settings), so a snapshot, mirror or toolchain change rebuilds the kernel; for a composed kernel `cfg12` covers its base (a local file's bytes, or the Debian package's URL and pin), the merged fragment, every patch (bytes or URL and pin) and `modules`, so changing any of them rebuilds; `nethermind-v1` keeps `kernel-<version>-<h>`. In the per-directory layout a build a variant inherits is keyed by that variant's own distribution, so a child that adds a build package or repository rebuilds it.
