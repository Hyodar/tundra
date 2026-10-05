# CLI

`tundravm` (also `python -m tundravm`) runs the lifecycle on a recipe file. Every recipe command loads the `Recipe` the file binds and runs one step on the selected variants; `measure` and `deploy` read the manifest a bake wrote.

```
tundravm init [DIR] [--name NAME] [--template minimal|service|cloud|prover] [--base BASE] [--backend KIND]
              [--ci github|none] [--with-tests | --no-tests] [--force] [--no-doctor]
tundravm init --list-templates
tundravm inspect RECIPE [--variant NAME]... [--format text|json|markdown | --json] [--diff-variants A B] [--lockfile PATH]
tundravm lint    RECIPE [--variant NAME]... [--format auto|text|json|github|markdown | --json] [--strict]
tundravm compile RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--check] [--format auto|text|markdown|github]
tundravm diff    RECIPE [--variant NAME]... [--against DIR] [--lockfile PATH] [--format auto|text|stat|markdown|github | --stat] [--color auto|always|never]
tundravm lock    RECIPE [--variant NAME]... [--path PATH] [--update SOURCE]... [--check | --offline] [--explain] [--format auto|text|github|markdown]
tundravm fetch   RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH]
tundravm bake    RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--backend KIND] [--no-fetch] [-v | -q | --json-logs] [--color auto|always|never]
tundravm measure MANIFEST [--variant NAME] [--scheme rtmr|azure|gcp] [--json] [--allow-placeholder]
tundravm deploy  MANIFEST [--variant NAME] --target qemu|azure|gcp [--param KEY=VALUE]... [--allow-placeholder]
tundravm doctor  [RECIPE] [--backend KIND]
tundravm ci      RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--format auto|text|github]
tundravm status  RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--format text|json|markdown]
tundravm clean   [RECIPE] [--out DIR] [--sources] [--tree] [--artifacts] [--state] [--all] [--lockfile PATH] [--dry-run]
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
| `fetch` | Checks the source builds and built kernels' sources of the selected variants out on this host, as the invoking user, into `OUT/.sources/<name>-<pin12>` (`--out`, default `build`) at the pins of `--lockfile` (default `build/tundravm.lock` when it exists; unpinned sources are resolved first). Complete checkouts are kept. Outside `nethermind-v1`, `bake` runs it first and mounts `OUT/.sources` into the build; `bake --no-fetch` builds from the checkouts already there and fails with `E_STATE` when one is missing (see [Fetch](#fetch)). | |
| `bake` | Compiles and builds every selected variant into `--out` (default `build`), then writes `OUT/bake-result.json`. | |
| `measure` | Expected measurements of one baked variant. | |
| `deploy` | Deploys one baked variant's artifact. | |
| `doctor` | Python and tundravm versions, the backend's host tools (for `local` also the optional `ukify`, `systemd-repart`, `apt` and `pefile`, probed under the Python that runs mkosi, and with a `RECIPE` that has `azure` or `gcp` variants the optional `qemu-img` or `sgdisk` their disk conversion needs on the host; see [Local backend](#local-backend)), the optional measurement tools; with `RECIPE`, its lint summary. | a required tool is missing |
| `ci` | `lint --strict`, `compile --check`, `lock --check` in order; stops at the first failure. | any step fails, including a missing lockfile |
| `status` | Read-only, network-free report of where the project stands, one line per item with a verdict, then the single most useful next command (see [Project status](#project-status)). | never (exit 0) |
| `clean` | Removes the chosen parts of the build output directory: `--sources`, `--tree`, `--artifacts`, `--state`, or `--all`; the lockfile only when `--lockfile` names it. With no part flag it lists what `--all` would remove (see [Project status](#project-status)). | a path could not be removed |
| `completion` | Prints a bash, zsh or fish completion script for this version's verbs, flags and flag choices (see [Shell completion](#shell-completion)). | |

`compile`, `diff`, `fetch` and `bake` read `build/tundravm.lock` when it exists and apply its source pins; `--lockfile PATH` names another one.

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
- `pyproject.toml` and `README.md`, only when absent; an existing one is kept, even with `--force`. The generated `pyproject.toml` depends on `tundravm`, puts `pytest` in the `dev` group and sets `[tool.pytest.ini_options] pythonpath = ["."]`, so tests you add can import the recipe and its sibling modules (`from node import App`);
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
note: svc/pyproject.toml already exists; add the dependencies with `uv add tundravm` and `uv add --dev pytest` (tundravm is not on PyPI yet: `uv add --editable PATH/TO/tundravm`)
lint my-node.py: no findings
next (from svc):
  1. tundravm compile my-node.py --out mkosi  write the mkosi tree; commit it
  2. uv run pytest tests                      run test_my_node.py against mkosi/
  3. tundravm lock my-node.py                 pin packages and sources in build/tundravm.lock
  4. tundravm ci my-node.py --out mkosi       the lint, tree and lockfile checks CI runs
  5. tundravm bake my-node.py --out build     build the image
  tundravm is not on PyPI yet: `uv add --editable PATH/TO/tundravm` uses a local checkout
```

The `note:` line appears only when `pyproject.toml` already existed; add `[tool.pytest.ini_options] pythonpath = ["."]` to that file yourself if your tests import project modules. Until tundravm's first release, install it from a checkout: `uv add --editable PATH/TO/tundravm` in the project, with `PATH/TO/tundravm` the directory holding tundravm's `pyproject.toml`. A missing backend tool never fails `init`; the probe ends with a pointer to the in-process backend:

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

A recipe file that fails to load (a syntax error, a missing import, an exception while the file or its factory runs) is also `E_VALIDATION`, exit 2. The context names the failing `location` (`file:line`: for a syntax error the offending line, otherwise the innermost frame outside tundravm, which can be a sibling helper module) and the original `error` as `Type: message`:

```console
$ tundravm lint node.py
error [E_VALIDATION]: Recipe node.py failed to load: syntax error at node.py:27: '(' was never closed
Hint: Run `python node.py` to see the full traceback, or pass --traceback.
  recipe: /home/me/node/node.py
  location: node.py:27
  error: SyntaxError: '(' was never closed
[exit 2]
```

A misspelt name reads `Recipe node.py failed to load: NameError at node.py:40: name 'Pakcage' is not defined`. `--traceback` raises the error with the Python traceback instead (exit 1, as any uncaught exception). Every load compiles the recipe file from its current source and writes no bytecode for it, so an edit is never hidden by a stale `__pycache__`.

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

`lock` keeps the existing lockfile's pins while their source is unchanged. `--update NAME` resolves that source again (`--update kernel` or `--update kernel-<variant>` for a built kernel's source in the current dialect); an unknown name is an error. `--offline` never touches the network and fails for a source with no pin. `--explain` prints the drift before writing.

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

## Fetch

`tundravm fetch RECIPE` checks out, on this host and as you, every source the selected variants build from: each `Build` source and, outside `nethermind-v1`, each built kernel's source (a `Kernel` with `config`), named `kernel` or `kernel-<variant>` where a variant's kernel source differs. A git source is checked out at its pinned commit, an http source is downloaded and checked against its sha256. Each lands in `OUT/.sources/<name>-<pin12>/` with a completion marker; a complete checkout is kept, so a second run touches nothing, and there is one checkout per (source, pin). Because it runs as you, your git credentials and SSH agent apply, so private repositories work.

Pins come from `--lockfile` (default `build/tundravm.lock` when it exists). A source the lockfile does not pin is resolved first, as `lock` would, unless the recipe's `Policy(mutable_ref_policy="error")` forbids it. For a recipe whose `hello` build is `Git("file:///work/hello", "v1.0.0")`, before and after `tundravm lock`:

```console
$ tundravm fetch node.py
[tundravm] fetch 1 source ...
[tundravm] note hello: fetched git file:///work/hello @ v1.0.0 at cdd0a5257ba9
[tundravm] fetch 1 source ... ok (0.0s)
fetched build/.sources
  hello  cdd0a5257ba9  fetched
note: no lockfile at build/tundravm.lock; fetched the refs as they resolve now
$ tundravm lock node.py
locked build/tundravm.lock
$ tundravm fetch node.py
[tundravm] fetch 1 source ...
[tundravm] note hello: cdd0a5257ba9 already fetched
[tundravm] fetch 1 source ... ok (0.0s)
fetched build/.sources
  hello  cdd0a5257ba9  kept
```

`inspect` shows each source's pin (`hello  git file:///work/hello  ref=v1.0.0  pinned=cdd0a52`). In the current dialect a build hook never clones: it copies its checkout from the mounted `.sources` into `$BUILDROOT/build/<name>` (a kernel's without `.git`), and a hook whose source has no pin only fails, with `run tundravm lock, then tundravm fetch`. `lock --update kernel` (or `kernel-<variant>`) moves a kernel's pin like any other source.

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
- Outside `nethermind-v1`, a bake with a real backend first runs `fetch` (a `fetch` progress step) at the pins it builds, then mounts `OUT/.sources` into the build: the mkosi command gets `--build-sources=OUT/.sources:tundravm-sources` (absolute paths) (so `$SRCDIR/tundravm-sources` in the scripts), the config directory again as the first `--build-sources` (unless the recipe sets `BuildSources`), and `--build-sources-ephemeral=yes` (unless the recipe sets `BuildSourcesEphemeral`), so nothing a script writes there reaches the host. The build sandbox never fetches a source. The in-process backend fetches nothing.
- `--no-fetch` skips the fetch and builds from the checkouts already in `OUT/.sources`, for example ones copied to an air-gapped host. A missing or incomplete checkout fails after `compile`, before mkosi runs:

  ```console
  $ tundravm bake node.py --backend local --no-fetch
  ...
  [default] failed after 0.0s
  error [E_STATE]: Source build 'hello' is not fetched: build/.sources/hello-cdd0a5257ba9 is missing or incomplete.
  Hint: Run `tundravm fetch RECIPE --out build` first, or bake without --no-fetch.
    pin: cdd0a5257ba950088a826755fa832f40e87fc5c3
  [exit 2]
  ```

- The bake is frozen when `--lockfile` is given or `build/tundravm.lock` exists: a recipe that drifted from the lock fails at `verify lockfile` with `E_LOCKFILE`. Without a lockfile it bakes unpinned and prints a note.
- The lockfile is copied to `OUT/tundravm.lock` when that file is absent or identical. A different `OUT/tundravm.lock` is never overwritten: `bake --out OUT --lockfile PATH` bakes against `PATH` and leaves it alone.
- The frozen check compares like `lock --check` with the same `--variant` selection: a lockfile of every variant covers `bake --variant default`, while a lockfile written for fewer variants than the bake selects fails at `verify lockfile`.
- A recipe with error-level lint findings fails at `lint` with `E_LINT`.
- `OUT/bake-result.json` records, per variant and target, the artifact path and sha256, plus the lockfile digest and a `declarative` block with the recipe digest, the tree digest, `simulated` (true for the in-process backend) and, for a frozen bake, `lockfile`: the path of the lockfile it baked against, as given (`--lockfile PATH`, else `build/tundravm.lock`).
- After the build, `bake` reads the compiled tree for its digest. A path it cannot read (a root-owned leftover, say) is skipped with a `warning` line instead of failing the bake, and mkosi's state next to its config (`mkosi.tools`, `mkosi.cache`, `mkosi.builddir`, ...) is never tree content; `diff` and `compile --check` ignore it too.

### Local backend

`--backend local` runs the host's `mkosi` (v25+), by default under `sudo`, with absolute `--directory` and `--output-dir` paths. For the `minimal` template (`tundravm init --template minimal --backend local`, then `tundravm bake node.py`), mkosi 26 on Ubuntu 24.04 builds a 47.2 MiB UKI:

```
variant  target  artifact                          size      sha256        time
default  qemu    build/default/output/default.efi  47.2 MiB  b0be0e718b25  1m24s
next: tundravm deploy build/bake-result.json --variant default --target qemu
```

- A bootable variant's artifact is the UKI `OUT/<variant>/output/<variant>.efi`, next to the other files mkosi writes there; `bake-result.json` records it with its sha256.
- mkosi's workspace, package cache and tools tree live in `OUT/.mkosi/` (`workspace/`, `cache/`, `mkosi.tools`), never in the compiled tree. A recipe that sets `WorkspaceDirectory` or `CacheDirectory` keeps its own.
- Under `sudo`, what mkosi wrote (`OUT/<variant>/output/`, `OUT/.mkosi/`) is chowned back to you, so the build directory holds no root-owned files. If that fails, a `warning` names the `sudo chown -R UID:GID OUT` to run.
- mkosi builds a UKI with the host's `ukify`, and reads the installed kernel with Python's `pefile` in any build that is not `Bootable=no` (directory builds included), unless it uses a tools tree. When the host lacks one of them (`pefile` is probed under the Python that runs mkosi) and the recipe sets no `ToolsTree`, the backend adds `--tools-tree=default` for that variant to the mkosi command (the compiled tree is unchanged) and says so on a `note` line, which `-q` hides:

  ```
  [default] note ukify not found on the host; building with mkosi's default tools tree (--tools-tree=default). Install systemd-ukify to use the host tools.
  ```

- The first bake builds the tools tree; a later bake into the same `OUT` passes `--tools-tree=OUT/.mkosi/mkosi.tools` (the note then says `reusing` and the path) instead of building it again. In the run above, a second bake took 32s against 1m24s for the first.
- An `azure` variant's postoutput script lays out a disk with `parted` and converts it to a VHD with `qemu-img`; a `gcp` variant's partitions a raw disk with `sgdisk` and packs it as a `tar.gz`. In a build with mkosi's default tools tree those scripts run inside it: the backend adds `--tools-tree-package=qemu-utils,gdisk,parted` and reuses a cached `OUT/.mkosi/mkosi.tools` only when it already holds `qemu-img`, `sgdisk` and `parted`, so the host needs none of them. Only a build on host tools needs `qemu-img` or `sgdisk` on the host; `bake` checks for them before mkosi runs and fails with `E_BACKEND_EXECUTION` (`Variant 'azure' needs `qemu-img` on the host for its cloud disk image.`, hint `install qemu-utils, or bake --variant default`).
- Source builds and built kernels are fetched on the host and mounted into the build (see [Fetch](#fetch) and [Bake](#bake)).

Measured on this host (Ubuntu 24.04, mkosi 26, no `ukify`, so every build used the default tools tree) with the `init` templates as written:

| Template | Variant | Artifact | Size | Time |
|---|---|---|---|---|
| `minimal` | `default` | UKI | 47.2 MiB | 1m24s (building the tools tree), 32s reusing it |
| `service` | `default` | UKI | 48 MiB | |
| `cloud` | `default` | UKI | 55 MiB | 35s |
| `cloud` | `azure` | VHD (502 MiB) and UKI | | 40s |
| `cloud` | `gcp` | `tar.gz` (55.6 MiB) and UKI | | 46s |
| `prover` | `default` | UKI, with `tdxs`, `key-gen`, `disk-setup` and `secret-delivery` built from source and in the initrd | 64 MiB | 2m09s |

The `cloud` times are with a cached tools tree. CI bakes the `service` and `cloud` templates in directory format on every push (see [testing](testing.md#template-bakes)).

`tundravm doctor --backend local` probes `ukify`, `systemd-repart`, `apt` and `pefile` as optional tools, the ones mkosi takes from the host when there is no tools tree. `doctor RECIPE` adds the cloud tools its `azure` and `gcp` variants convert their disks with; for the `cloud` template:

```console
$ tundravm doctor node.py
tundravm 0.1.0
python 3.12.3
backend local_linux: available
  ok mkosi mkosi 26
  ok sudo Sudo version 1.9.15p5
  missing (optional) ukify — mkosi can use its own tools tree: add Setting("Build", "ToolsTree", ("default",)) to the recipe, or install systemd-ukify
  ok systemd-repart systemd 255 (255.4-1ubuntu8.17)
  ok apt apt 2.8.3 (amd64)
  ok pefile 2023.2.7
cloud image tools:
  missing (optional) qemu-img — The azure variant converts its disk to a VHD with it: install qemu-utils, or bake --variant default
  ok sgdisk GPT fdisk (sgdisk) version 1.0.10
measurement tools:
  missing (optional) measured-boot — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
  missing (optional) dstack-mr — Real RTMR measurements need it. Install measured-boot or dstack-mr and make sure it is on PATH.
lint: no findings
[exit 0]
```

A missing `ukify` or `pefile` needs no action, since the backend then adds the tools tree itself, and with a tools tree (as here) a missing `qemu-img` or `sgdisk` needs none either. With `ukify` and `pefile` present but `systemd-repart` or `apt` missing, add the `ToolsTree` setting the hint names.

## Measure and deploy

`measure MANIFEST` and `deploy MANIFEST` take `bake-result.json` or the directory holding it.

- `measure --scheme rtmr` (default) runs `measured-boot` or `dstack-mr`. Without one, or for `azure`/`gcp`, it fails unless `--allow-placeholder`, which prints digest-derived values under a `PLACEHOLDER` banner on stderr. Simulated artifacts always need `--allow-placeholder`. `--json` prints `artifact`, `artifact_digest`, `scheme`, `tool`, `values`, `variant`.
- `deploy --target` picks the artifact of that target. `--param` keys are the target's settings: qemu `memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`; azure `storage_account` (required), `resource_group`, `location`, `vm_size`; gcp `project` and `bucket` (required), `zone`, `machine_type`. Simulated artifacts are refused unless `--allow-placeholder`.

## Project status

`tundravm status RECIPE` answers "where is this project at?" without writing anything or touching the network. It prints one line per item, `LABEL VERDICT DETAIL`, for the selected variants (`--variant`, default all), then `next: COMMAND`:

| Line | Reports | Verdicts |
|---|---|---|
| `recipe` | path, recipe digest (as `inspect --format json`), variants, mkosi dialect, base and snapshot | `ok` |
| `lint` | diagnostic counts by level | `ok`, `error` when there are errors |
| `lock` | `--lockfile` (default `build/tundravm.lock`): present, version, drifted sections (as `lock --check`), unpinned sources and kernels | `ok`, `stale`, `missing` |
| `source` | one per source build and built kernel: whether `OUT/.sources/<name>-<pin12>` holds a complete checkout of the locked pin | `ok`, `stale` (another pin or an incomplete checkout), `missing`, `n/a` (unpinned; `nethermind-v1`; or the `inprocess` backend, which needs none) |
| `tree` | `OUT/mkosi` (`--out`, default `build`) against what the recipe compiles to now, as `compile --check` | `ok`, `stale`, `missing` |
| `artifact` | one per baked variant and target in `OUT/bake-result.json`: path, size, sha256 prefix, `simulated`, the lockfile the bake used; stale when the recipe digest or the tree digest it was baked from no longer matches | `ok`, `stale`, `missing` |
| `backend` | the recipe file's `backend` and how many of its host tools `doctor` finds | `ok`, `missing`, `n/a` (no backend) |

`next` follows the lifecycle: `tundravm lint` when there are errors, else `tundravm lock`, `tundravm fetch`, `tundravm compile --out OUT/mkosi`, `tundravm bake` (or `tundravm doctor` when the backend lacks a tool), and `everything is up to date` once every line is `ok` or `n/a`. It repeats `--variant`, `--out` and `--lockfile` as given. `status` always exits 0. `--format json` prints one object per section (`recipe`, `lint`, `lock`, `sources`, `tree`, `artifacts`, `backend`), each with a `verdict`, plus `next`; `sources` and `artifacts` hold `items`. `--format markdown` prints a table per section.

After `init --backend inprocess`, `lock` and `bake --backend inprocess`, adding a package to the recipe:

```console
$ tundravm status node.py
recipe    ok       node.py  digest 837071dff8c5  variants default, dev  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      stale    build/tundravm.lock  v3  2 sections drifted
source    n/a      no source builds or kernels
tree      stale    build/mkosi: 2 files differ from the recipe
artifact  stale    default/qemu  build/default/disk.qcow2  114 B  sha256 1fa043adea90  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
artifact  stale    dev/qemu  build/dev/disk.qcow2  110 B  sha256 9cfb8b5c2f31  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
backend   ok       inprocess: no host tools needed
next: tundravm lock node.py
```

`tundravm clean RECIPE` (or `clean --out DIR` without a recipe) removes build output by part: `--sources` (`OUT/.sources`), `--tree` (`OUT/mkosi`), `--artifacts` (each `OUT/<variant>/` and `OUT/bake-result.json`), `--state` (`OUT/.mkosi`, which holds the cached tools tree, and the mkosi state next to the tree's config), or `--all`. The lockfile is never removed unless `--lockfile PATH` names it. It prints `removed PATH` per path, or `would remove PATH` with `--dry-run`; with no part flag it lists what `--all` would remove and removes nothing:

```console
$ tundravm clean node.py
would remove build/mkosi
would remove build/default
would remove build/dev
would remove build/bake-result.json
pass --all to remove them, or --sources, --tree, --artifacts or --state for some (the lockfile stays unless --lockfile names it)
```

A path the user cannot delete (mkosi output a `local` bake left root-owned) is removed with `sudo rm -rf` when `sudo` is available; otherwise `clean` prints `not removed PATH: ...` and a hint, and exits 1.

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
| 1 | A check failed: `lint` findings, `compile --check`, `diff`, `lock --check`, `ci`, or `doctor` missing a required tool; or `clean` could not remove a path |
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
