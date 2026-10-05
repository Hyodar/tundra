# Reproducibility

tundravm makes two promises and gives you a check for each:

| Promise | Check |
|---|---|
| The same recipe compiles to the same mkosi tree, byte for byte | `tundravm compile --check`, `assert_tree`, `Tree.digest` |
| The same recipe and lockfile fetch the same inputs | `tundravm lock --check`, frozen `bake` |

Byte-identical *disk images* additionally depend on the Debian archive, the build backend and mkosi itself; pin the first with `Recipe.snapshot` and run the same backend.

## Reproducible output

`Recipe.epoch` controls the time and identity settings the compiler emits:

| `epoch` | Emitted |
|---|---|
| `0` (default) | `SourceDateEpoch=0`, `Environment=SOURCE_DATE_EPOCH=0`, a stable `Seed=` (deterministic partition UUIDs), and a finalize hook that strips `IMAGE_VERSION` from `os-release` |
| any other integer | the same, with `SourceDateEpoch=` and `SOURCE_DATE_EPOCH` set to that value |
| `None` | none of the above: the build is not reproducible |

Every `mkosi.conf` also gets `ManifestFormat=json` and `CleanPackageMetadata=true`. Lowering never touches the network, and `Path` contents are read at compile time, so the tree depends only on the recipe, the files it reads, and the lock.

Pin the archive with `Recipe.snapshot`, and the EFI stub with the `EfiStub` fragment:

```python
from tundravm.declarative import Fragment, Recipe
from tundravm.declarative.utils import EfiStub

SNAPSHOT = "20251113T083151Z"

recipe = Recipe(
    name="node",
    snapshot=SNAPSHOT,
    common=Fragment("node", items=(EfiStub(snapshot=SNAPSHOT, version="257.8-1~deb13u1"),)),
)
```

`snapshot` is the snapshot ID, written as mkosi's `Snapshot=`; mkosi reads it from `https://snapshot.debian.org` unless `mirror` names another root. `mirror` and `tools_mirror` are mirror roots that mkosi completes (`<root>/debian`, `<root>/archive/debian/<snapshot>`), never full archive URLs. `EfiStub` takes the same ID (or a snapshot archive URL), and its `version` must be one that snapshot carries: the `cloud` and `prover` templates pair `20251113T083151Z` with `257.8-1~deb13u1`. `Backports` follows the recipe's mirror and snapshot, and pins backports to 200 and sid to 100 so the release's packages win. Outside `nethermind-v1`, `tundravm lock` also pins the `EfiStub` package's sha256 as the source `efi-stub`, and `fetch` downloads it on the host (see [Hermetic builds](#hermetic-builds)).

## Committed trees

Commit the compiled tree and check it in CI:

```bash
tundravm compile node.py --out mkosi            # after every recipe change
tundravm compile node.py --out mkosi --check    # in CI: exit 1 and list the stale files
```

In tests, `assert_tree(compile(recipe, lock=locked), "mkosi")` compares every path, byte, exec bit and symlink, and `TUNDRAVM_UPDATE_GOLDEN=1` rewrites the golden tree (see [testing](testing.md#golden-trees)).

The [`surge-tdx-prover`](../examples/surge-tdx-prover/) recipe is held to this standard: it compiles byte-for-byte to the committed nethermind-tdx tree for all four variants (`tundravm compile examples/surge-tdx-prover/image.py --out examples/surge-tdx-prover/mkosi --check`, and `tests/compiler/test_surge_golden.py`).

## The lockfile

`tundravm lock RECIPE` writes `build/tundravm.lock` (JSON, version 4):

| Key | Holds |
|---|---|
| `recipe_digest` | SHA-256 of the canonical recipe payload of the locked variants |
| `sections` | One SHA-256 per section (below) |
| `recipe` | The payload itself, so drift can name the changed items |
| `dependencies` | The package list of each variant |
| `fetches` | One pin per source build and, in the current dialect, per built kernel (`kernel`, or `kernel-<variant>` when a variant's kernel source differs) and per `EfiStub` package (`efi-stub`, or `efi-stub-<variant>`): `name`, `kind` (`git`/`http`), `source` URL, requested `ref`, resolved `digest` (commit or sha256) |

Version 4 covers every input that defines the image:

| Section | Covers |
|---|---|
| `distribution` | `base`, `arch`, `mirror`, `tools_mirror`, `snapshot`, `epoch` |
| `compiler` | the tundravm version, the mkosi dialect and the recipe's mkosi options (sandbox files by sha256) |
| `default_profile`, `init_scripts` | the default variant's name; its `runtime-init` steps (priority and script sha256) in the order the script runs them: lowest priority first, equal priorities in the order `Init.after` resolves. A variant that adds steps of its own records its merged sequence as `variants.<name>.init_scripts`, so reordering steps drifts the lock |
| `variants.<name>.<section>` | `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets`, and `extends` for a variant with a parent |
| `variants.<name>.kernel` | the kernel's version, source identity, cmdline, `tdx` flag and the sha256 of its config bytes (`null` when the variant builds no kernel) |
| `variants.<name>.mkosi` | the variant's mkosi options, only where they differ from the default variant's |
| `variants.<name>.debloat` | the complete debloat configuration: enabled, paths removed and skipped, per-variant paths, systemd minimisation, units and binaries kept, cleaned `/var` directories |

The recipe-wide `base` and `arch` sections of version 3 are gone (they live in `distribution`). File entries (`files`, `skeleton_files`) record each path's `kind` (`file`, `symlink` or `directory`), mode and content sha256. Because `compiler` records the tundravm version, the recipe digest changes with it: lock again after upgrading tundravm.

A version 3 lockfile still loads. `lock --check` reports it until you lock again, and a frozen bake refuses it with `E_LOCKFILE` (`Frozen bake needs a version 4 lockfile`):

```console
$ tundravm lock node.py --check
~ version: 3 -> 4: lock again to record the distribution, compiler and kernel sections
- arch
- base
+ compiler
+ distribution
+ variants.default.kernel
+ variants.dev.kernel
[exit 1]
```

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

`tundravm bake` is frozen whenever `build/tundravm.lock` exists (or `--lockfile` is given): a recipe that drifted from the lock fails at the `verify lockfile` step with `E_LOCKFILE` and the list of drifted sections. The check follows the subset rule above, so `bake --variant NAME` works against the lock of every variant. The Python `bake()` always takes a `Lock`. Each `Artifact` records the recipe digest, the lockfile digest and the tree digest it was built from, and `bake-result.json` its sha256: `measure` and `deploy` (and `verify_artifact`, `status --verify`) hash the file first and refuse one that changed since the bake with `E_ARTIFACT_CHANGED`.

## Proving and recording a bake

The checks above pin the inputs; `tundravm bake --verify-reproducible` checks the output. It bakes the same selection a second time into `OUT/.reproduce`, from the same lockfile and the same `OUT/.sources` checkouts, compares every artifact's sha256, and records the outcome as `declarative.reproducible` in `bake-result.json` (`status` shows it per artifact): `reproducible: yes (N artifacts match a second build)`, or `E_REPRODUCIBILITY` with the second build kept for `tundravm diff` and diffoscope (see [CLI: Bake](cli.md#bake)). `tundravm sbom` is the record of what that bake contains: mkosi's package manifest (every installed package and version from the snapshot), the lockfile's source pins and the recipe metadata and digests, as SPDX 2.3 or CycloneDX 1.5 with a package URL per component (the purl rules are in [CLI: SBOM](cli.md#sbom)). Its lists are sorted and its ids derive from the content, so with `SOURCE_DATE_EPOCH` set a reproducible bake gives the same document too, ready to publish next to the measurements.

## mkosi

The `local` backend needs mkosi v25 or newer (v26 recommended) on `PATH` and checks the version before building. The `lima` and `nix` backends bring their own.

```bash
pip install 'mkosi @ git+https://github.com/systemd/mkosi.git@v26'
```

## CI

```bash
tundravm ci node.py --out mkosi    # lint --strict, compile --check, lock --check
```

Add `Policy(require_frozen_lock=True, mutable_ref_policy="error")` to refuse unpinned bakes and unpinned sources outright (see [policy](policy.md)).

Emitted trees are umask-independent: files are written `0644` (generated scripts `0755`, declared files at their declared mode) and directories `0755`, and `Tree.digest` hashes each file's exec bit rather than its full mode.

In the current dialect a pinned kernel's cache key is `kernel-<version>-<cfg12>-<pin12>-<dist16>`, where `dist16` hashes the variant's build distribution (base, arch, mirror, snapshot, repositories, build packages, Build settings), so a snapshot, mirror or toolchain change rebuilds the kernel; `nethermind-v1` keeps `kernel-<version>-<h>`. In the per-directory layout a build a variant inherits is keyed by that variant's own distribution, so a child that adds a build package or repository rebuilds it.
