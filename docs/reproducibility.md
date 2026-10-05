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

`snapshot` is the snapshot ID, written as mkosi's `Snapshot=`; mkosi reads it from `https://snapshot.debian.org` unless `mirror` names another root. `mirror` and `tools_mirror` are mirror roots that mkosi completes (`<root>/debian`, `<root>/archive/debian/<snapshot>`), never full archive URLs. `EfiStub` takes the same ID (or a snapshot archive URL), and its `version` must be one that snapshot carries: the `cloud` and `prover` templates pair `20251113T083151Z` with `257.8-1~deb13u1`. `Backports` follows the recipe's mirror and snapshot, and pins backports to 200 and sid to 100 so the release's packages win.

## Committed trees

Commit the compiled tree and check it in CI:

```bash
tundravm compile node.py --out mkosi            # after every recipe change
tundravm compile node.py --out mkosi --check    # in CI: exit 1 and list the stale files
```

In tests, `assert_tree(compile(recipe, lock=locked), "mkosi")` compares every path, byte, exec bit and symlink, and `TUNDRAVM_UPDATE_GOLDEN=1` rewrites the golden tree (see [testing](testing.md#golden-trees)).

The [`surge-tdx-prover`](../examples/surge-tdx-prover/) recipe is held to this standard: it compiles byte-for-byte to the committed nethermind-tdx tree for all four variants (`tundravm compile examples/surge-tdx-prover/image.py --out examples/surge-tdx-prover/mkosi --check`, and `tests/compiler/test_surge_golden.py`).

## The lockfile

`tundravm lock RECIPE` writes `build/tundravm.lock` (JSON, version 3):

| Key | Holds |
|---|---|
| `recipe_digest` | SHA-256 of the canonical recipe payload of the locked variants |
| `sections` | One SHA-256 per section: `base`, `arch`, `default_profile`, `init_scripts`, and `variants.<variant>.<section>` for `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `debloat`, `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets` (and `extends` for a variant with a parent) |
| `recipe` | The payload itself, so drift can name the changed items |
| `dependencies` | The package list of each variant |
| `fetches` | One pin per source build and, in the current dialect, per built kernel (`kernel`, or `kernel-<variant>` when a variant's kernel source differs): `name`, `kind` (`git`/`http`), `source` URL, requested `ref`, resolved `digest` (commit or sha256) |

Version 2 lockfiles named the per-variant sections `profiles.<variant>.<section>`; they still load, with the names mapped and the digests unchanged, so they check clean without re-locking.

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

`compile`, `diff`, `fetch` and `bake` read `build/tundravm.lock`: the build gets exactly the pinned commit, or the download with the pinned hash. In Python, `compile(recipe, lock=locked)` applies the pins and `compile(recipe)` uses the refs.

- `lock` keeps every existing pin whose source is unchanged; `--update NAME` re-resolves one source (`--update kernel` or `--update kernel-<variant>` for a kernel).
- `lock` tries every source and writes nothing unless all resolve; one `E_LOCKFILE` error lists each failure as `<name>: git <url> @ <ref>: <reason>` (`ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>`, `timed out after 60s`). A dead upstream ref therefore shows up at `lock` together with every other failure, not one per run.
- `lock --offline` reuses the pins and lists every source without one in a single error: `Cannot lock offline: N sources need the network to resolve:`.
- An unpinned build is the `source-unpinned` lint warning. `lock --check` never uses the network; drift shows as `+ sources.<name>: source <name> is not pinned` or `~ sources.<name>: <old> -> <new>`.
- A `Git` ref that is already a 40-hex commit, or an `Http` source with `sha256`, is immutable and needs no resolution.

## Sources fetched on the host

In the current dialect the build sandbox never fetches a source. `tundravm fetch RECIPE`, which `bake` runs first, checks every pinned source build and built kernel out on the host, as the invoking user, into `build/.sources/<name>-<pin12>/`: git at the pinned commit, http verified against the pinned sha256. A marker records each completed checkout, so fetching again touches nothing, and the directory name carries the pin, so a new pin gets a new checkout. The backend mounts `build/.sources` into the build and each hook copies its checkout; a kernel's is copied without `.git`. `inspect` shows each source's `pinned=` commit.

That makes an air-gapped bake possible: run `tundravm fetch RECIPE` on a host with network access, copy `build/` (lockfile and `.sources/`) to the builder, and run `tundravm bake RECIPE --no-fetch` there. A missing or incomplete checkout fails before mkosi runs with `E_STATE`, naming the `tundravm fetch` to run. This covers sources only: mkosi still needs its package mirror, and `EfiStub` downloads from its snapshot.

A hook whose source has no pin fails in the build with `run tundravm lock, then tundravm fetch`, so an unpinned source never builds from whatever the ref points to that day. `nethermind-v1` recipes, such as `surge-tdx-prover`, keep the historical tree's in-sandbox clones and have no kernel pins.

A lockfile written for a current-dialect recipe with a source build or a built kernel before sources moved to the host needs re-locking: run `tundravm lock RECIPE` once and commit the result.

## Frozen bakes

`tundravm bake` is frozen whenever `build/tundravm.lock` exists (or `--lockfile` is given): a recipe that drifted from the lock fails at the `verify lockfile` step with `E_LOCKFILE` and the list of drifted sections. The check follows the subset rule above, so `bake --variant NAME` works against the lock of every variant. The Python `bake()` always takes a `Lock`. Each `Artifact` records the recipe digest, the lockfile digest and the tree digest it was built from.

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
