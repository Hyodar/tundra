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
| `explain RECIPE` | `--format text\|json\|markdown`, `--json` | Dry run. Prints `Image.summary()` per selected profile, or `{profile: Image.explain()}` as JSON; `--format markdown` prints one section per profile with tables for packages, files, users, services, units, hooks, sources and init scripts (tables over 20 rows collapse) |
| `digest RECIPE` | | Prints the 64-hex recipe digest used by lockfiles. Profile-sensitive; useful for CI diffs |
| `compile RECIPE` | `--out DIR`, `--force`, `--check`, `--format auto\|text\|markdown\|github` | Emits the mkosi tree (default `<build_dir>/mkosi`). Prints path, profiles, digest. `--check` writes nothing and exits 1 if the tree at `--out` is stale, listing the files that differ |
| `check RECIPE` | `--format auto\|text\|json\|github\|markdown`, `--json`, `--strict` | Lints the recipe (`Image.check()`): services running as undeclared users, relative file paths, shadowed files, platform/output-target mismatches, init priority collisions, missing backend. Exit 1 on errors, or on warnings with `--strict` |
| `diff RECIPE` | `--against DIR`, `--format auto\|text\|stat\|markdown\|github`, `--stat`, `--color auto\|always\|never` | Compiles to a temp dir and shows a unified diff against an existing tree (default `<build_dir>/mkosi`). Exit 1 when they differ |
| `lock RECIPE` | `--path FILE`, `--check`, `--explain`, `--format auto\|text\|github\|markdown` | Writes the lockfile (default `<build_dir>/tundravm.lock`). `--check` compares it with the recipe without writing: prints `lock is up to date` and exits 0, or one line per drifted section and exits 1. `--explain` prints the drifted sections, then writes the new lockfile; with `--check` it only reports |
| `bake RECIPE` | `--out DIR`, `--frozen`, `--lock`, `--force`, `-v/--verbose`, `-q/--quiet`, `--json-logs`, `--color auto\|always\|never` | Compile and build with the recipe's backend. `--lock` writes the lockfile first, then bakes with `--frozen` semantics. Prints per-profile artifacts, the `report.json` path, and a `next: tundravm deploy ...` line. Also writes `bake-result.json` (see below) |
| `measure RECIPE` | `--backend rtmr\|azure\|gcp`, `--json`, `--out DIR`, `--allow-placeholder` | Derives expected TDX measurements for one profile from `bake-result.json`. Prints a table, or `{schema_version, backend, values}` as JSON. `--out` reads the bake made with `bake --out DIR`. Values are real only when `measured-boot` or `dstack-mr` is on PATH (rtmr); azure/gcp are placeholders. Without a tool it fails with `E_MEASUREMENT` unless `--allow-placeholder`, which prints a `PLACEHOLDER` banner on stderr. The table has a `source:` line and JSON carries `source`, `tool_version`, `artifact` (schema 2) |
| `deploy RECIPE` | `--target qemu\|azure\|gcp`, `--out DIR`, `--memory 4G`, `--cpus N`, `--param KEY=VALUE` | Deploys one profile's baked artifact from `bake-result.json` and prints the deployment id, endpoint, and adapter metadata. `--param` is repeatable and passed to the adapter (e.g. `ssh_port`, `tdx=true`, `daemonize=false` for QEMU). `--out` reads the bake made with `bake --out DIR` |
| `doctor [RECIPE]` | `--attr NAME`, `--pythonpath DIR` | Prints Python and tundravm versions and probes backend tools (`limactl`, `nix`, `mkosi`, `sudo`/`unshare`). Without `RECIPE` it reports every real backend as available or unavailable. With `RECIPE` it probes only that recipe's backend, then prints the `check` summary line. Exit 1 when the recipe's backend is missing a required tool. Also lists the optional measurement tools (`measured-boot`, `dstack-mr`); they never change the exit code |
| `ci RECIPE` | `--out DIR`, `--lockfile FILE`, `--format auto\|text\|github` | Runs `check --strict` (all profiles), `compile --check` and `lock --check`; prints `ok`/`FAIL`/`skip` per step and exits 1 at the first failure |
| `init [DIR]` | `--name NAME`, `--base X`, `--backend lima\|nix\|local\|inprocess`, `--ci github\|none`, `--force` | Writes `NAME.py`, a `.gitignore` block for `build/` that keeps `build/tundravm.lock`, and with `--ci github` a `.github/workflows/tundravm.yml` (explain summary to the job page + `tundravm ci`). Refuses to overwrite without `--force` |
| `new PATH` | `--base X`, `--backend lima\|nix\|local\|inprocess`, `--force` | Writes a starter recipe file; refuses to overwrite without `--force` |

## Bake results

`bake` writes `<build_dir>/bake-result.json` (or `<--out>/bake-result.json`): per-profile artifacts `{target: path}`, the `report.json` path, the lock digest, the backend name, and a UTC timestamp. Paths inside the build directory are stored relative to it, so the file survives moving the checkout. `measure` and `deploy` (and `Image.measure()`/`Image.deploy()` via `Image.last_bake()`) read it in a new process; if it is missing they fail with `E_STATE` and the hint to bake first. `measure` and `deploy` look in the recipe's `build_dir`; pass `--out DIR` to read a bake made with `bake --out DIR`.

## Lockfile drift

The lockfile records one digest per recipe section: `base`, `arch`, `default_profile`, `init_scripts`, and `profiles.<name>.<section>`. `lock --check`, `lock --explain` and frozen bakes report drift one section per line:

```text
~ profiles.default.packages: +htop -jq
~ profiles.default.files: +/etc/issue
```

`~` is a changed section, `+` a section only in the recipe, `-` a section only in the lockfile. Item detail (`+htop -jq`) is shown when the lockfile's embedded recipe still matches the section digest.

`bake --frozen` (and `bake(frozen=True)`) fails with `E_LOCKFILE` and lists the drifted sections, at most 15, then points to `lock --check`. Run `tundravm lock RECIPE` to accept the changes, or revert them.

## Output formats

`check`, `diff`, `compile --check`, `lock --check` and `ci` take `--format`. Precedence: an explicit `--format`, then the `--json`/`--stat` shorthands (not combinable with `--format`), then `auto`, the default, which is `github` when `GITHUB_ACTIONS=true` and `text` otherwise. `github` prints workflow commands (`::error file=RECIPE,title=CODE::message (hint)`, `::warning` per changed file, one `::error` per drifted lock section) so findings show inline on the pull request; `markdown` prints tables (and a `diff` fence cut at 400 lines) for job summaries and PR descriptions. `explain --format markdown` does the same for the whole image.

A new project gets all of this from one command:

```bash
tundravm init . --name node --ci github
```

## Bake output

Progress goes to stderr, one line per step (`[azure] build via lima ... ok (4m12s)`), with a live timer on a TTY and a `still running` heartbeat every minute elsewhere. The summary table (profile, target, artifact, size, sha256, time) goes to stdout, followed by the `next:` hint. Backend output is hidden unless the build fails, in which case the last five lines are shown. `-v` echoes every backend line as `[profile] | line`, `-q` prints only the summary and errors, `--json-logs` writes one JSON event per line to stdout (no summary) for CI collectors, and `--color` follows the `diff` convention (`NO_COLOR` respected). Pass `reporter=` to `Image.bake()` for the same events in Python.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Unexpected exception; a failing `check`; a stale tree or lockfile for `diff`, `compile --check` and `lock --check`; a missing backend tool for `doctor RECIPE` |
| 2 | SDK error. stderr gets `error [E_CODE]: message` followed by the hint |
| 130 | Interrupted |

## Examples

```bash
tundravm explain examples/surge-tdx-prover/image.py --profile azure
tundravm digest recipe.py --all-profiles
tundravm lock recipe.py --check
tundravm compile recipe.py --out build/mkosi
tundravm bake recipe.py --lock --all-profiles
tundravm measure recipe.py --backend rtmr --json
tundravm deploy recipe.py --target qemu --memory 4G --cpus 4 --param ssh_port=2223
tundravm doctor recipe.py
tundravm check recipe.py --strict
tundravm diff recipe.py --against build/mkosi
tundravm compile examples/surge-tdx-prover/image.py --out examples/surge-tdx-prover/mkosi --check  # CI drift gate
tundravm new recipes/node.py --backend nix
```

## Review workflow

Commit the compiled tree next to the recipe. A recipe change then shows up twice in review: the Python diff and the resulting mkosi diff. `tundravm diff` previews the second part before you commit, and `tundravm compile --check` in CI fails the build when someone forgets to regenerate the tree.

`bake` runs `check` first and refuses to build a recipe with error-level findings. Run `tundravm check` to see them.
