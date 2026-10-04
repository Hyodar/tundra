# CLI

`uv sync` installs a `tundravm` console script. `python -m tundravm` is equivalent.

```bash
tundravm <command> RECIPE [options]
tundravm --version
tundravm --help
```

## Recipe files

`RECIPE` is a Python file. The loader (`tundravm.load_recipe`) finds the `Image` in this order:

1. A module-level `Image` bound to `img`, `image`, `IMAGE`, `recipe`, or `RECIPE`.
2. A zero-argument callable named `build`, `build_image`, `make_image`, `create_image`, or `recipe` that returns an `Image`.
3. The only module-level `Image` instance.
4. The only zero-argument function whose name starts with `build` or whose return annotation is `Image`.

Anything else is `E_VALIDATION` with a hint. `--attr NAME` skips the search and uses that name.

Rules:

- The file runs with a private `__name__`, so `if __name__ == "__main__":` blocks do not run.
- The recipe's directory and the current directory are importable. `--pythonpath DIR` (repeatable) adds more.
- A factory must return the `Image`. It must not call `bake()` itself; a factory returning `None` is a distinct error.

```python
# recipe.py
from tundravm import Image
from tundravm.backends import LocalLinuxBackend
from tundravm.modules import KeyGeneration

img = Image(backend=LocalLinuxBackend())
img.install("curl")
with img.profile("dev"):
    img.ssh()
```

## Common options

Every command except `new` and `doctor` accepts the options below. `doctor` takes only `--attr` and `--pythonpath`:

| Option | Meaning |
| --- | --- |
| `--attr NAME` | Image instance or factory to use inside `RECIPE` |
| `-p NAME`, `--profile NAME` | Operate on this profile; repeatable |
| `--all-profiles` | Operate on every declared profile; exclusive with `--profile` |
| `--pythonpath DIR` | Extra import directory; repeatable |

With neither `--profile` nor `--all-profiles`, commands use the default profile. An unknown profile is `E_VALIDATION` and the message lists the declared profiles.

## Commands

| Command | Options | Does |
| --- | --- | --- |
| `explain RECIPE` | `--json` | Dry run. Prints `Image.summary()` per selected profile, or `{profile: Image.explain()}` as JSON |
| `digest RECIPE` | | Prints the 64-hex recipe digest used by lockfiles. Profile-sensitive; useful for CI diffs |
| `compile RECIPE` | `--out DIR`, `--force`, `--check` | Emits the mkosi tree (default `<build_dir>/mkosi`). Prints path, profiles, digest. `--check` writes nothing and exits 1 if the tree at `--out` is stale, listing the files that differ |
| `check RECIPE` | `--json`, `--strict` | Lints the recipe (`Image.check()`): services running as undeclared users, relative file paths, shadowed files, platform/output-target mismatches, init priority collisions, missing backend. Exit 1 on errors, or on warnings with `--strict` |
| `diff RECIPE` | `--against DIR`, `--stat`, `--color auto\|always\|never` | Compiles to a temp dir and shows a unified diff against an existing tree (default `<build_dir>/mkosi`). Exit 1 when they differ |
| `lock RECIPE` | `--path FILE` | Writes the lockfile (default `<build_dir>/tundravm.lock`) |
| `bake RECIPE` | `--out DIR`, `--frozen`, `--lock`, `--force` | Compile and build with the recipe's backend. `--lock` writes the lockfile first, then bakes with `--frozen` semantics. Prints per-profile artifacts, the `report.json` path, and a `next: tundravm deploy ...` line. Also writes `bake-result.json` (see below) |
| `measure RECIPE` | `--backend rtmr\|azure\|gcp`, `--json` | Derives expected TDX measurements for one profile from `bake-result.json`. Prints a table, or `{schema_version, backend, values}` as JSON |
| `deploy RECIPE` | `--target qemu\|azure\|gcp`, `--memory 4GiB`, `--cpus N`, `--param KEY=VALUE` | Deploys one profile's baked artifact from `bake-result.json` and prints the deployment id, endpoint, and adapter metadata. `--param` is repeatable and passed to the adapter (e.g. `ssh_port`, `tdx=true`, `daemonize=false` for QEMU) |
| `doctor [RECIPE]` | `--attr NAME`, `--pythonpath DIR` | Prints Python and tundravm versions and probes backend tools (`limactl`, `nix`, `mkosi`, `sudo`/`unshare`). Without `RECIPE` it reports every real backend as available or unavailable. With `RECIPE` it probes only that recipe's backend, then prints the `check` summary line. Exit 1 when the recipe's backend is missing a required tool |
| `new PATH` | `--base X`, `--backend lima\|nix\|local\|inprocess`, `--force` | Writes a starter recipe file; refuses to overwrite without `--force` |

## Bake results

`bake` writes `<build_dir>/bake-result.json` (or `<--out>/bake-result.json`): per-profile artifacts `{target: path}`, the `report.json` path, the lock digest, the backend name, and a UTC timestamp. Paths inside the build directory are stored relative to it, so the file survives moving the checkout. `measure` and `deploy` (and `Image.measure()`/`Image.deploy()` via `Image.last_bake()`) read it in a new process; if it is missing they fail with `E_STATE` and the hint to bake first. `measure` and `deploy` look in the recipe's `build_dir`, so a bake with a different `--out` is not picked up.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Unexpected exception |
| 2 | SDK error. stderr gets `error [E_CODE]: message` followed by the hint |
| 130 | Interrupted |

## Examples

```bash
tundravm explain examples/surge-tdx-prover/image.py --profile azure
tundravm digest recipe.py --all-profiles
tundravm compile recipe.py --out build/mkosi
tundravm bake recipe.py --lock --all-profiles
tundravm measure recipe.py --backend rtmr --json
tundravm deploy recipe.py --target qemu --memory 4GiB --cpus 4 --param ssh_port=2223
tundravm doctor recipe.py
tundravm check recipe.py --strict
tundravm diff recipe.py --against build/mkosi
tundravm compile examples/surge-tdx-prover/image.py --out examples/surge-tdx-prover/mkosi --check  # CI drift gate
tundravm new recipes/node.py --backend nix
```

## Review workflow

Commit the compiled tree next to the recipe. A recipe change then shows up twice in review: the Python diff and the resulting mkosi diff. `tundravm diff` previews the second part before you commit, and `tundravm compile --check` in CI fails the build when someone forgets to regenerate the tree.

`bake` runs `check` first and refuses to build a recipe with error-level findings. Run `tundravm check` to see them.
