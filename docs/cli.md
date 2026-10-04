# CLI

`tundravm` (also `python -m tundravm`) runs the lifecycle on a recipe file. Every recipe command loads the `Recipe` the file binds and runs one step on the selected variants; `measure` and `deploy` read the manifest a bake wrote.

```
tundravm init [DIR] [--name NAME] [--template minimal|service|cloud|prover] [--base BASE] [--backend KIND]
              [--ci github|none] [--with-tests | --no-tests] [--force] [--no-doctor]
tundravm init --list-templates
tundravm inspect RECIPE [--variant NAME]... [--format text|json|markdown | --json] [--diff-variants A B]
tundravm lint    RECIPE [--variant NAME]... [--format auto|text|json|github|markdown | --json] [--strict]
tundravm compile RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--check] [--format auto|text|markdown|github]
tundravm diff    RECIPE [--variant NAME]... [--against DIR] [--lockfile PATH] [--format auto|text|stat|markdown|github | --stat] [--color auto|always|never]
tundravm lock    RECIPE [--variant NAME]... [--path PATH] [--update SOURCE]... [--check | --offline] [--explain] [--format auto|text|github|markdown]
tundravm bake    RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--backend KIND] [-v | -q | --json-logs] [--color auto|always|never]
tundravm measure MANIFEST [--variant NAME] [--scheme rtmr|azure|gcp] [--json] [--allow-placeholder]
tundravm deploy  MANIFEST [--variant NAME] --target qemu|azure|gcp [--param KEY=VALUE]... [--allow-placeholder]
tundravm doctor  [RECIPE] [--backend KIND]
tundravm ci      RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--format auto|text|github]
tundravm completion bash|zsh|fish
```

Every command that takes `RECIPE` (and `doctor`) also accepts `--attr NAME`, `--pythonpath DIR` (repeatable) and `--traceback`, which raises an SDK error with its Python traceback instead of printing `error [E_CODE]: message`. `KIND` is `lima`, `nix`, `local` or `inprocess`. `--version` prints the version.

With no arguments, `tundravm` prints its help followed by a quickstart and exits 0:

```
quickstart:
  tundravm init . --name node                 write node.py and check its build backend
  tundravm inspect node.py                    show what the image will contain
  tundravm lint node.py                       report every recipe diagnostic
  tundravm bake node.py --backend inprocess   simulated build, no VM or root needed
```

A usage error exits 2. An unknown verb or flag gets a did-you-mean suggestion:

```console
$ tundravm inpsect x.py
usage: tundravm [-h] [--version] COMMAND ...
tundravm: error: unknown command 'inpsect' (did you mean 'inspect'?)
[exit 2]
$ tundravm inspect node.py --fromat json
usage: tundravm inspect [-h] [--attr ATTR] [--pythonpath DIR] [--variant NAME]
                        [--format {text,json,markdown} | --json]
                        [--diff-variants A B]
                        recipe
tundravm inspect: error: unrecognized arguments: --fromat json (did you mean --format?)
[exit 2]
```

## Commands

| Command | Does | Exit 1 when |
|---|---|---|
| `init` | Scaffolds a project from a starter template, lints it, prints the next steps and probes the chosen backend (see [Init](#init)). | |
| `inspect` | Dry run per variant: parent, fragments, packages, files with digests, users, units, hooks, runtime-init steps, sources, targets (see [Inspect](#inspect)). `--format json` adds the recipe `digest` the lockfile records. `--diff-variants A B` lists what differs between two variants instead. | |
| `lint` | Every diagnostic: resolution, fragment checks, compiler rules. | an error (with `--strict`, a warning) |
| `compile` | Writes the mkosi tree to `--out` (default `build/mkosi`), one directory per variant. `--check` writes nothing and reports the stale files. | `--check` and the tree is stale |
| `diff` | Unified diff from the tree at `--against` (default `build/mkosi`) to what the recipe compiles to. `--stat` lists changed files. | the trees differ |
| `lock` | Writes the lockfile to `--path` (default `build/tundravm.lock`) for the selected variants. `--check` prints the drift instead (see [Lockfile drift](#lockfile-drift)). | `--check` and the lock is stale |
| `bake` | Compiles and builds every selected variant into `--out` (default `build`), then writes `OUT/bake-result.json`. | |
| `measure` | Expected measurements of one baked variant. | |
| `deploy` | Deploys one baked variant's artifact. | |
| `doctor` | Python and tundravm versions, the backend's host tools, the optional measurement tools; with `RECIPE`, its lint summary. | a required tool is missing |
| `ci` | `lint --strict`, `compile --check`, `lock --check` in order; stops at the first failure. | any step fails, including a missing lockfile |
| `completion` | Prints a bash, zsh or fish completion script for this version's verbs, flags and flag choices (see [Shell completion](#shell-completion)). | |

`compile`, `diff` and `bake` read `build/tundravm.lock` when it exists and apply its source pins; `--lockfile PATH` names another one.

## Init

`tundravm init [DIR]` scaffolds a project in `DIR` (default `.`). The recipe is named after the directory unless `--name NAME` is given.

| Flag | Effect |
|---|---|
| `--template minimal\|service\|cloud\|prover` | Starter recipe (default `service`). `--list-templates` prints the four with a one-line description and exits. |
| `--base BASE` | Base distribution (default `debian/trixie`). |
| `--backend KIND` | Backend the recipe binds to `backend` (default `lima`). |
| `--ci github\|none` | `github` also writes `.github/workflows/tundravm.yml` (default `none`). |
| `--with-tests` / `--no-tests` | Write `tests/test_NAME.py` (default) or skip it. |
| `--force` | Overwrite the recipe, tests module and workflow if they exist. |
| `--no-doctor` | Skip the backend probe. |

```console
$ tundravm init --list-templates
minimal  packages, one file and one verbatim unit; a single qemu variant
service  an App fragment (generated service, config, first-boot step, lint rule) plus a dev variant (default)
cloud    service plus azure and gcp variants, backports and an EFI stub pinned to a Debian snapshot
prover   cloud plus a TPM-sealed key, an encrypted disk, secrets and the tdxs attestation service
```

Every template installs the Debian kernel and the packages a UKI boots with (`linux-image-amd64`, `systemd`, `systemd-sysv`, `udev`, `kmod`, `systemd-boot-efi`), so it bakes a bootable UKI as written and passes `kernel-missing` (see [Lint](#lint)).

It writes:

- `NAME.py`, the recipe;
- `tests/test_NAME.py` (lint, compile and golden-tree tests), with every character of `NAME` that is not valid in an identifier replaced by `_`: `--name my-node` writes `tests/test_my_node.py`;
- `pyproject.toml` and `README.md`, only when absent; an existing one is kept, even with `--force`;
- a `build/` block in `.gitignore` that keeps `build/tundravm.lock`;
- with `--ci github`, the workflow (see [CI](#ci)).

Without `--force`, `init` refuses to overwrite the recipe, tests module or workflow. After the files it prints the recipe's lint summary, a numbered `next:` list, then the result of `tundravm doctor --backend KIND` for the chosen backend:

```console
$ tundravm init svc --name my-node --template service --backend inprocess --no-doctor
created svc/my-node.py
created svc/tests/test_my_node.py
kept svc/pyproject.toml (already exists)
created svc/README.md
created svc/.gitignore
note: svc/pyproject.toml already exists; add the dependencies with `uv add tundravm` and `uv add --dev pytest`
lint my-node.py: no findings
next (from svc):
  1. tundravm compile my-node.py --out mkosi  write the mkosi tree; commit it
  2. uv run pytest tests                      run test_my_node.py against mkosi/
  3. tundravm lock my-node.py                 pin packages and sources in build/tundravm.lock
  4. tundravm ci my-node.py --out mkosi       the lint, tree and lockfile checks CI runs
  5. tundravm bake my-node.py --out build     build the image
```

The `note:` line appears only when `pyproject.toml` already existed. A missing backend tool never fails `init`; the probe ends with a pointer to the in-process backend:

```
checking the nix backend (tundravm doctor --backend nix):
tundravm 0.1.0
python 3.12.3
backend nix_mkosi: unavailable
  missing nix — Install Nix with flakes enabled: https://nixos.org/download.html
measurement tools:
  missing (optional) measured-boot — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
  missing (optional) dstack-mr — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
the nix backend is not ready: install the missing tools above, or bake with --backend inprocess for a simulated build
```

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

### Comparing two variants

`inspect RECIPE --diff-variants A B` resolves both variants and lists the declarations `B` adds, removes or changes relative to `A`, matched by identity, plus the target when it differs. It takes `--format text|json|markdown` (or `--json`) and cannot be combined with `--variant` (`E_VALIDATION`). For a recipe whose `azure` variant extends `default` with `add=Fragment("azure", items=(Package("walinuxagent"),))`, `replace=(File("/etc/motd", "node on azure\n"),)`, `remove=(Package("curl"),)` and `target="azure"`:

```console
$ tundravm inspect node.py --diff-variants default azure
variants default -> azure: 1 added, 1 removed, 1 changed
  target: qemu -> azure
  + Package(walinuxagent, runtime)
  - Package(curl, runtime)
  ~ File(extra, /etc/motd): content
$ tundravm inspect node.py --diff-variants default azure --format markdown
# tundravm: `node.py` variants `default` → `azure`

Target: `qemu` → `azure`

| Change | Declaration | Fields |
|---|---|---|
| added | `Package(walinuxagent, runtime)` | — |
| removed | `Package(curl, runtime)` | — |
| changed | `File(extra, /etc/motd)` | content |
```

`--format json` prints `{"a", "b", "targets": {A: [...], B: [...]}, "added": [...], "removed": [...], "changed": [{"declaration", "fields"}]}`. A `~` line names the fields that differ.

## Lint

`lint` prints one line per finding, its hint indented under it, then a summary. A variant that boots but installs no kernel is `kernel-missing`, an error:

```console
$ tundravm lint node.py
error kernel-missing [default]: variant builds a bootable UKI but installs no kernel, so mkosi stops with 'A kernel must be installed in the image to build a UKI'
    hint: Add Package("linux-image-amd64") for the distribution kernel, declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) for a non-bootable image.
1 error, 0 warnings, 0 infos
[exit 1]
```

`bake` lints first, so this stops it with `E_LINT` before the backend runs mkosi. See [Concepts: Bootable images](concepts.md#bootable-images) for the three fixes; the [API reference](api.md#lint-rules) lists every code.

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

`lock` tries every source that needs resolving and writes nothing unless all of them resolve. Otherwise it exits 2 with one error that lists every failure:

```console
$ tundravm lock node.py
error [E_LOCKFILE]: Cannot lock: 2 sources could not be resolved:
  ghost: git https://example.invalid/ghost.git @ main: repository unreachable: fatal: unable to access 'https://example.invalid/ghost.git/': Could not resolve host: example.invalid
  tools: git https://github.com/Hyodar/tundra-tools.git @ does-not-exist: ref 'does-not-exist' not found
0 of 2 sources resolved; nothing written.
Hint: Fix the refs above, or drop NAME from --update to keep its existing pin, or pass --offline to reuse existing pins.
[exit 2]
```

Each line is `<name>: git <url> @ <ref>: <reason>`. The reason is `ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>` (an `Http` download) or `timed out after 60s`; git never prompts for credentials, so a private repository is unreachable. With `--format github` the same error is followed by one annotation per failure:

```
::error file=node.py,title=E_SOURCE::tools: git https://github.com/Hyodar/tundra-tools.git @ does-not-exist: ref 'does-not-exist' not found
```

`lock --offline` lists every source without a pin in one error, `Cannot lock offline: 2 sources need the network to resolve:`, one `<name>: git <url> @ <ref>: not pinned in the lockfile` line each. `lock --check` never uses the network; an unpinned source drifts as `+ sources.<name>: source <name> is not pinned`.

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
- The lockfile is copied to `OUT/tundravm.lock` when that file is absent or identical. A different `OUT/tundravm.lock` is never overwritten: `bake --out OUT --lockfile PATH` bakes against `PATH` and leaves it alone.
- The frozen check compares like `lock --check` with the same `--variant` selection: a lockfile of every variant covers `bake --variant default`, while a lockfile written for fewer variants than the bake selects fails at `verify lockfile`.
- A recipe with error-level lint findings fails at `lint` with `E_LINT`.
- `OUT/bake-result.json` records, per variant and target, the artifact path and sha256, plus the lockfile digest and a `declarative` block with the recipe digest, the tree digest, `simulated` (true for the in-process backend) and, for a frozen bake, `lockfile`: the path of the lockfile it baked against, as given (`--lockfile PATH`, else `build/tundravm.lock`).

## Measure and deploy

`measure MANIFEST` and `deploy MANIFEST` take `bake-result.json` or the directory holding it.

- `measure --scheme rtmr` (default) runs `measured-boot` or `dstack-mr`. Without one, or for `azure`/`gcp`, it fails unless `--allow-placeholder`, which prints digest-derived values under a `PLACEHOLDER` banner on stderr. Simulated artifacts always need `--allow-placeholder`. `--json` prints `artifact`, `artifact_digest`, `scheme`, `tool`, `values`, `variant`.
- `deploy --target` picks the artifact of that target. `--param` keys are the target's settings: qemu `memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`; azure `storage_account` (required), `resource_group`, `location`, `vm_size`; gcp `project` and `bucket` (required), `zone`, `machine_type`. Simulated artifacts are refused unless `--allow-placeholder`.

## Shell completion

`tundravm completion bash|zsh|fish` prints a static script generated from this version's verbs, flags, flag choices (`--format`, `--backend`, `--scheme`, `--target`, ...) and file arguments. Its header repeats the install line; regenerate it after upgrading.

| Shell | Install |
|---|---|
| bash | `source <(tundravm completion bash)` in `~/.bashrc` |
| zsh | `source <(tundravm completion zsh)` in `~/.zshrc` after `compinit`, or save the output as `_tundravm` in a directory on `$fpath` |
| fish | `tundravm completion fish > ~/.config/fish/completions/tundravm.fish` |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A check failed: `lint` findings, `compile --check`, `diff`, `lock --check`, `ci`, or `doctor` missing a required tool |
| 2 | An SDK error, printed as `error [E_CODE]: message` with a `Hint:` and context lines; also a usage error (unknown verb or flag, with a did-you-mean suggestion) |
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
