# CLI

`tundravm` (also `python -m tundravm`) runs the lifecycle on a recipe file. Every recipe command loads the `Recipe` the file binds and runs one step on the selected variants; `measure` and `deploy` read the manifest a bake wrote.

```
tundravm init [DIR] [--name NAME] [--base BASE] [--backend KIND] [--ci github|none] [--force]
tundravm inspect RECIPE [--variant NAME]... [--format text|json|markdown | --json]
tundravm lint    RECIPE [--variant NAME]... [--format auto|text|json|github|markdown | --json] [--strict]
tundravm compile RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--check] [--format auto|text|markdown|github]
tundravm diff    RECIPE [--variant NAME]... [--against DIR] [--lockfile PATH] [--format auto|text|stat|markdown|github | --stat] [--color auto|always|never]
tundravm lock    RECIPE [--variant NAME]... [--path PATH] [--update SOURCE]... [--check | --offline] [--explain] [--format auto|text|github|markdown]
tundravm bake    RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--backend KIND] [-v | -q | --json-logs] [--color auto|always|never]
tundravm measure MANIFEST [--variant NAME] [--scheme rtmr|azure|gcp] [--json] [--allow-placeholder]
tundravm deploy  MANIFEST [--variant NAME] --target qemu|azure|gcp [--param KEY=VALUE]... [--allow-placeholder]
tundravm doctor  [RECIPE] [--backend KIND]
tundravm ci      RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--format auto|text|github]
```

Every command that takes `RECIPE` (and `doctor`) also accepts `--attr NAME` and `--pythonpath DIR` (repeatable). `KIND` is `lima`, `nix`, `local` or `inprocess`.

## Commands

| Command | Does | Exit 1 when |
|---|---|---|
| `init` | Writes `NAME.py` from the starter template, appends a `build/` block to `.gitignore` that keeps `build/tundravm.lock`, and with `--ci github` writes `.github/workflows/tundravm.yml`. Refuses to overwrite without `--force`. | |
| `inspect` | Dry run per variant: parent, fragments, packages, files with digests, users, units, hooks, runtime-init steps, sources, targets (see [Inspect](#inspect)). `--format json` adds the recipe `digest` the lockfile records. | |
| `lint` | Every diagnostic: resolution, fragment checks, compiler rules. | an error (with `--strict`, a warning) |
| `compile` | Writes the mkosi tree to `--out` (default `build/mkosi`), one directory per variant. `--check` writes nothing and reports the stale files. | `--check` and the tree is stale |
| `diff` | Unified diff from the tree at `--against` (default `build/mkosi`) to what the recipe compiles to. `--stat` lists changed files. | the trees differ |
| `lock` | Writes the lockfile to `--path` (default `build/tundravm.lock`) for the selected variants. `--check` prints the drift instead (see [Lockfile drift](#lockfile-drift)). | `--check` and the lock is stale |
| `bake` | Compiles and builds every selected variant into `--out` (default `build`), then writes `OUT/bake-result.json`. | |
| `measure` | Expected measurements of one baked variant. | |
| `deploy` | Deploys one baked variant's artifact. | |
| `doctor` | Python and tundravm versions, the backend's host tools, the optional measurement tools; with `RECIPE`, its lint summary. | a required tool is missing |
| `ci` | `lint --strict`, `compile --check`, `lock --check` in order; stops at the first failure. | any step fails, including a missing lockfile |

`compile`, `diff` and `bake` read `build/tundravm.lock` when it exists and apply its source pins; `--lockfile PATH` names another one.

## Recipe files

`RECIPE` is a Python file. The CLI imports it under a private module name (so an `if __name__ == "__main__":` block does not run) with the file's directory, the working directory and every `--pythonpath DIR` importable, so sibling helper modules resolve.

The recipe is found in this order:

1. With `--attr NAME`: that attribute. It may be a `Recipe` or a zero-argument function returning one.
2. A module-level `Recipe` bound to `recipe` or `RECIPE`.
3. A zero-argument callable named `build` or `recipe`, called.
4. The only public module-level `Recipe` value.
5. The only public zero-argument function defined in the file whose name starts with `build` or whose return annotation is `Recipe`.

Several candidates at step 4 or 5, a factory that needs arguments, or one that returns `None` is an `E_VALIDATION` error with a hint.

A module-level `backend` holding a backend instance (`LimaMkosiBackend(...)`, `NixMkosiBackend()`, `LocalLinuxBackend()`, `InProcessBackend()`) is what `bake` and `doctor RECIPE` use. `bake --backend KIND` overrides it.

## Variants

`--variant NAME` is repeatable. Without it, a recipe command runs on every declared variant, in declaration order. An unknown name is `E_VALIDATION` and lists the declared variants. Duplicates are ignored.

`measure` and `deploy` take one `--variant`; without it the manifest must hold exactly one baked variant.

## Output formats

`lint`, `diff`, `compile --check`, `lock --check` and `ci` take `--format`. Precedence: an explicit `--format`, then the shorthand flag (`lint --json`, `diff --stat`; not combinable with `--format`), then `auto`, the default, which picks `github` when the `GITHUB_ACTIONS` environment variable is `true` and `text` otherwise.

| Format | Shape |
|---|---|
| `text` | One line per finding (`error disk-key-undefined [default] data: ...`) and a summary line |
| `json` | `{"diagnostics": [...], "summary": {...}}` (`lint`; each diagnostic has `code`, `level`, `message`, `hint`, `subject`, `variant`); `{"digest": ..., "variants": {NAME: {...}}}` (`inspect`) |
| `github` | `::error file=...,title=...::message` workflow commands that annotate the pull request |
| `markdown` | Tables, ready for `$GITHUB_STEP_SUMMARY` or a PR comment |
| `stat` | Changed files only (`diff`) |

`inspect` takes `text`, `json` or `markdown` (no `auto`).

## Inspect

The text form prints one block per variant:

```
Image: debian/trixie (x86_64)  variant=dev  reproducible=yes
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no
Parent: default
Fragments (2): node dev
Packages (4): curl jq strace systemd
...
Targets: qemu
```

`Parent:` is the variant's parent (`base` for `Recipe.common`, or another variant's name; a standalone variant has no `Parent:` line) and `Fragments:` the fragments it includes, in resolution order. In `--format json` each entry under `variants` carries the same facts as keys, among them `variant`, `parent` (`null` when standalone), `fragments`, `packages`, `files`, `users`, `units`, `hooks`, `runtime_init`, `sources` and `targets`. `--format markdown` renders a `## Variant` section per variant, with a **Parent** / **Fragments** line and a table per category.

## Lockfile drift

`lock --check` and the `ci` lock step print one line per drifted section:

```
~ variants.default.packages: +htop
~ variants.dev.packages: +htop
```

`~` changed, `+` only in the recipe, `-` only in the lockfile. Source builds drift as `+ sources.<name>` (not pinned yet) or `~ sources.<name>: <old> -> <new>`. A clean lock prints `lock is up to date`. With `--format github` each line is `::error file=build/tundravm.lock,title=lock drift::variants.default.packages changed: +htop. Run `tundravm lock` and commit the lockfile.`; with `--format markdown` it is a `Change | Section | Detail` table.

Sections are the recipe-wide `base`, `arch`, `default_profile` and `init_scripts`, and `variants.<variant>.<key>` per variant (`packages`, `files`, `users`, `services`, `hooks`, `source_builds`, ...). The lockfile is version 3; a version 2 lockfile, which named the per-variant sections `profiles.<variant>.<key>`, still loads and checks clean.

`lock --check --variant NAME` checks only the named variants' sections and the recipe-wide ones. A lockfile written for every variant is therefore up to date for any selection, and the lockfile's sections for the other variants are never reported. The whole-recipe digest (`recipe_digest`) is compared only when the selection is every variant the lockfile holds. A lockfile written with `lock --variant NAME` holds only that variant, so checking or baking more variants against it reports their sections as `+`.

`lock` keeps the existing lockfile's pins while their source is unchanged. `--update NAME` resolves that source again; an unknown name is an error. `--offline` never touches the network and fails for a source with no pin. `--explain` prints the drift before writing.

## Bake

```
[tundravm] lint ... ok (0.0s)
[tundravm] verify lockfile ... ok (0.0s)
[tundravm] compile ... ok (0.0s)
[default] prepare inprocess ... ok (0.0s)
[default] build via inprocess ... ok (0.0s)
[default] artifact qemu build/default/disk.qcow2 (114 B)
...
[tundravm] baked 2 variants in 0.0s

variant  target  artifact                  size   sha256        time
default  qemu    build/default/disk.qcow2  114 B  1fa043adea90  0.0s
dev      qemu    build/dev/disk.qcow2      110 B  9cfb8b5c2f31  0.0s
next: tundravm deploy build/bake-result.json --variant default --target qemu
```

- Progress goes to stderr as `[variant] step ... ok (1.2s)` lines (a live timer on a terminal), ending `baked N variants`; the summary table (one row per variant and target) and the `next:` line go to stdout.
- `-v`/`--verbose` echoes every backend output line. `-q`/`--quiet` prints only the summary and errors. `--json-logs` writes progress events to stdout as JSON lines (`kind`, `message`, `variant`, `elapsed_s`, `extra`) and no summary.
- Backend output is hidden by default, but its last lines are shown when the build fails.
- The bake is frozen when `--lockfile` is given or `build/tundravm.lock` exists: a recipe that drifted from the lock fails at `verify lockfile` with `E_LOCKFILE`. Without a lockfile it bakes unpinned and prints a note.
- The frozen check compares like `lock --check` with the same `--variant` selection: a lockfile of every variant covers `bake --variant default`, while a lockfile written for fewer variants than the bake selects fails at `verify lockfile`.
- A recipe with error-level lint findings fails at `lint` with `E_LINT`.
- `OUT/bake-result.json` records, per variant and target, the artifact path and sha256, plus the lockfile digest and a `declarative` block with the recipe digest, the tree digest and `simulated` (true for the in-process backend).

## Measure and deploy

`measure MANIFEST` and `deploy MANIFEST` take `bake-result.json` or the directory holding it.

- `measure --scheme rtmr` (default) runs `measured-boot` or `dstack-mr`. Without one, or for `azure`/`gcp`, it fails unless `--allow-placeholder`, which prints digest-derived values under a `PLACEHOLDER` banner on stderr. Simulated artifacts always need `--allow-placeholder`. `--json` prints `artifact`, `artifact_digest`, `scheme`, `tool`, `values`, `variant`.
- `deploy --target` picks the artifact of that target. `--param` keys are the target's settings: qemu `memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`; azure `storage_account` (required), `resource_group`, `location`, `vm_size`; gcp `project` and `bucket` (required), `zone`, `machine_type`. Simulated artifacts are refused unless `--allow-placeholder`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A check failed: `lint` findings, `compile --check`, `diff`, `lock --check`, `ci`, or `doctor` missing a required tool |
| 2 | An SDK error, printed as `error [E_CODE]: message` with a `Hint:` and context lines; also argparse usage errors |
| 130 | Interrupted |

## CI

`tundravm ci RECIPE --out mkosi` is the whole gate:

```console
$ tundravm ci node.py --out mkosi
ok lint: no findings
ok compile: mkosi is up to date
ok lock: build/tundravm.lock is up to date
```

A failing step prints its report, then `FAIL step: ...` and `skip` for the remaining steps, and exits 1. Under GitHub Actions the reports are workflow annotations. With `--variant`, every step checks only those variants, the lock step as `lock --check --variant` does.

The steps share one loaded recipe, and the compile step works on a copy of it, so a recipe with runtime-init steps (keys, disks, secrets, `Init`) passes the lock step whenever `tundravm lock --check` passes on its own.

`tundravm init --ci github` writes a workflow that runs `uv sync`, appends `tundravm inspect RECIPE --format markdown` to `$GITHUB_STEP_SUMMARY`, then runs `tundravm ci RECIPE --out mkosi`. Commit `mkosi/` and `build/tundravm.lock`; the workflow checks both.
