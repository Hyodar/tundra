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

Every command except `new` accepts:

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
| `bake RECIPE` | `--out DIR`, `--frozen`, `--lock`, `--force` | Compile and build with the recipe's backend. `--lock` writes the lockfile first, then bakes with `--frozen` semantics. Prints per-profile artifacts and `report.json` path |
| `new PATH` | `--base X`, `--backend lima\|nix\|local\|inprocess`, `--force` | Writes a starter recipe file; refuses to overwrite without `--force` |

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
tundravm check recipe.py --strict
tundravm diff recipe.py --against build/mkosi
tundravm compile examples/surge-tdx-prover/image.py --out examples/surge-tdx-prover/mkosi --check  # CI drift gate
tundravm new recipes/node.py --backend nix
```

## Review workflow

Commit the compiled tree next to the recipe. A recipe change then shows up twice in review: the Python diff and the resulting mkosi diff. `tundravm diff` previews the second part before you commit, and `tundravm compile --check` in CI fails the build when someone forgets to regenerate the tree.

`bake` runs `check` first and refuses to build a recipe with error-level findings. Run `tundravm check` to see them.
