# Reproducibility

tundravm makes two promises and gives you a check for each:

| Promise | Check |
|---|---|
| The same recipe compiles to the same mkosi tree, byte for byte | `tundravm compile --check`, `assert_tree`, `Tree.digest` |
| The same recipe and lockfile fetch the same inputs | `tundravm lock --check`, frozen `bake` |

Byte-identical *disk images* additionally depend on the Debian archive, the build backend and mkosi itself; pin the first with a snapshot mirror and run the same backend.

## Reproducible output

`Recipe.epoch` controls the time and identity settings the compiler emits:

| `epoch` | Emitted |
|---|---|
| `0` (default) | `SourceDateEpoch=0`, `Environment=SOURCE_DATE_EPOCH=0`, a stable `Seed=` (deterministic partition UUIDs), and a finalize hook that strips `IMAGE_VERSION` from `os-release` |
| any other integer | the same, with `SourceDateEpoch=` and `SOURCE_DATE_EPOCH` set to that value |
| `None` | none of the above: the build is not reproducible |

Every `mkosi.conf` also gets `ManifestFormat=json` and `CleanPackageMetadata=true`. Lowering never touches the network, and `Path` contents are read at compile time, so the tree depends only on the recipe, the files it reads, and the lock.

Pin the archive with snapshot mirrors, and the EFI stub with the `EfiStub` fragment:

```python
from tundravm.declarative import Fragment, Recipe
from tundravm.declarative.utils import EfiStub

SNAPSHOT = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"

recipe = Recipe(
    name="node",
    mirror=SNAPSHOT,
    tools_mirror=SNAPSHOT,
    common=Fragment("node", items=(EfiStub(snapshot=SNAPSHOT, version="257.9-1~bpo12+1"),)),
)
```

## Committed trees

Commit the compiled tree and check it in CI:

```bash
tundravm compile node.py --out mkosi            # after every recipe change
tundravm compile node.py --out mkosi --check    # in CI: exit 1 and list the stale files
```

In tests, `assert_tree(compile(recipe, lock=locked), "mkosi")` compares every path, byte, exec bit and symlink, and `TUNDRAVM_UPDATE_GOLDEN=1` rewrites the golden tree (see [testing](testing.md#golden-trees)).

The [`surge-tdx-prover`](../examples/surge-tdx-prover/) recipe is held to this standard: it compiles byte-for-byte to the committed nethermind-tdx tree for all four variants (`python -m examples.surge-tdx-prover compile --check`, and `tests/compiler/test_surge_golden.py`).

## The lockfile

`tundravm lock RECIPE` writes `build/tundravm.lock` (JSON, version 3):

| Key | Holds |
|---|---|
| `recipe_digest` | SHA-256 of the canonical recipe payload of the locked variants |
| `sections` | One SHA-256 per section: `base`, `arch`, `default_profile`, `init_scripts`, and `variants.<variant>.<section>` for `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `debloat`, `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets` (and `extends` for a variant with a parent) |
| `recipe` | The payload itself, so drift can name the changed items |
| `dependencies` | The package list of each variant |
| `fetches` | One pin per source build: `name`, `kind` (`git`/`http`), `source` URL, requested `ref`, resolved `digest` (commit or sha256) |

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

`compile`, `diff` and `bake` read `build/tundravm.lock` and fetch exactly the pinned commit or verify the pinned hash. In Python, `compile(recipe, lock=locked)` applies the pins and `compile(recipe)` uses the refs.

- `lock` keeps every existing pin whose source is unchanged; `--update NAME` re-resolves one source.
- `lock` tries every source and writes nothing unless all resolve; one `E_LOCKFILE` error lists each failure as `<name>: git <url> @ <ref>: <reason>` (`ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>`, `timed out after 60s`). A dead upstream ref therefore shows up at `lock` together with every other failure, not one per run.
- `lock --offline` reuses the pins and lists every source without one in a single error: `Cannot lock offline: N sources need the network to resolve:`.
- An unpinned build is the `source-unpinned` lint warning. `lock --check` never uses the network; drift shows as `+ sources.<name>: source <name> is not pinned` or `~ sources.<name>: <old> -> <new>`.
- A `Git` ref that is already a 40-hex commit, or an `Http` source with `sha256`, is immutable and needs no resolution.

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
