# CLI

`tundravm` (also `python -m tundravm`) runs the lifecycle on a recipe file. Every recipe command loads the `Recipe` the file binds and runs one step on the selected variants; `measure` and `deploy` read the manifest a bake wrote.

```
tundravm init [DIR] [--name NAME] [--template minimal|service|cloud|prover] [--base BASE] [--backend KIND]
              [--ci github|none] [--with-tests | --no-tests] [--force] [--no-doctor]
tundravm init --list-templates
tundravm inspect RECIPE [--variant NAME]... [--format text|json|markdown | --json] [--diff-variants A B] [--lockfile PATH]
tundravm inspect RECIPE --variant NAME --why SUBJECT [--format text|json|markdown | --json] [--lockfile PATH]
tundravm lint    RECIPE [--variant NAME]... [--format auto|text|json|github|markdown | --json] [--strict] [--lockfile PATH]
tundravm compile RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--check] [--format auto|text|markdown|github]
tundravm diff    RECIPE [--variant NAME]... [--against DIR] [--lockfile PATH] [--format auto|text|stat|markdown|github | --stat] [--color auto|always|never]
tundravm lock    RECIPE [--variant NAME]... [--lockfile PATH] [--update SOURCE]... [--check | --offline] [--explain] [--format auto|text|github|markdown]
tundravm fetch   RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--force]
tundravm bake    RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--backend KIND] [--no-fetch] [--offline] [--verify-reproducible] [-v | -q | --json-logs] [--color auto|always|never]
tundravm measure MANIFEST [--variant NAME] [--scheme rtmr|azure|gcp] [--json] [--allow-placeholder] [--export-policy FILE]
tundravm deploy  MANIFEST [--variant NAME] --target qemu|azure|gcp [--param KEY=VALUE]... [--attach] [--allow-simulated-artifact]
tundravm attest  --endpoint URL --policy FILE [--nonce HEX] [--format text|json|markdown]
tundravm doctor  [RECIPE] [--backend KIND]
tundravm ci      RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--format auto|text|github]
tundravm status  RECIPE [--variant NAME]... [--out DIR] [--lockfile PATH] [--verify] [--format text|json|markdown]
tundravm clean   [RECIPE] [--out DIR] [--sources] [--tree] [--artifacts] [--state] [--all] [--lockfile PATH] [--dry-run]
tundravm config  [RECIPE] [--out DIR] [--tree DIR] [--lockfile PATH] [--backend KIND] [--format text|json]
tundravm completion bash|zsh|fish
```

`RECIPE` may be omitted wherever it appears when a `pyproject.toml` with a `[tool.tundravm]` table is found (see [Project configuration](#project-configuration)).

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
| `inspect` | Dry run per variant: parent, fragments, packages, files with digests, users, units, hooks, runtime-init steps, sources, targets (see [Inspect](#inspect)). `--format json` adds the recipe `digest` the lockfile records. `--diff-variants A B` lists what differs between two variants instead. `--variant NAME --why SUBJECT` explains one emitted object (see [Explaining one object](#explaining-one-object)). | |
| `lint` | Every diagnostic: resolution, fragment checks, compiler rules. The compiler rules see the source pins of `--lockfile` (default `build/tundravm.lock` when it exists), so a pinned source never reports `source-unpinned`. A selected lockfile, `--lockfile` or the table's `lockfile`, also adds its drift diagnostics (`lock-*`), as `lint(recipe, lock=...)` does. | an error (with `--strict`, a warning) |
| `compile` | Writes the mkosi tree to `--out` (default `build/mkosi`), one directory per variant, then prints `variants:`, `recipe_digest:` (the digest `lock` records) and `tree_digest:` (`Tree.digest`). `--check` writes nothing and reports the stale files. | `--check` and the tree is stale |
| `diff` | Unified diff from the tree at `--against` (default `build/mkosi`) to what the recipe compiles to. `--stat` lists changed files. | the trees differ |
| `lock` | Writes the lockfile to `--lockfile` (default `build/tundravm.lock`) for the selected variants: version 4, with the `distribution`, `compiler`, `variants.<name>.kernel` and `variants.<name>.debloat` sections. A build whose source differs between variants is pinned per variant as `<variant>/<name>` (drift line `sources.<variant>.<name>`); `--update <variant>/<name>` re-resolves one variant's pin, `--update <name>` every variant's. `--check` prints the drift instead (see [Lockfile drift](#lockfile-drift)); a version 3 lockfile drifts as `~ version: 3 -> 4` until locked again. | `--check` and the lock is stale |
| `fetch` | Checks the source builds, built kernels' sources and `EfiStub` package (`efi-stub`) of the selected variants out on this host, as the invoking user, into `OUT/.sources/<name>-<pin12>-<id8>` (`--out`, default `build`; `id8` hashes the url, subdirectory and submodules) at the pins of `--lockfile` (default `build/tundravm.lock` when it exists; unpinned sources are resolved first). Complete checkouts are verified and kept: git `HEAD` is the pin, `git status` is empty (untracked and ignored files included) and requested submodules are initialised; an http checkout matches the sha256 manifest written at fetch time. A modified one fails with `E_SOURCE` (`source <name> checkout modified/incomplete: run tundravm fetch --force`); `--force` checks every source out again and refills the dependency caches. Outside `nethermind-v1`, `bake` runs it first and mounts `OUT/.sources` into the build; `bake --no-fetch` builds from the checkouts already there and fails with `E_STATE` when one is missing. It also prefetches each `Go`, `Cargo` and `Dotnet` build's dependencies with the host's toolchain into `OUT/.sources/deps/{go,cargo,nuget}` and lists the outcome in a `deps:` column: `go: prefetched`, `go: kept`, `go: prefetched (was incomplete: ...)`, `go: incomplete (...)`, `skipped (no host go)`, `skipped (offline)` or `failed (...)` (see [Fetch](#fetch)). | |
| `bake` | Compiles and builds every selected variant into `--out` (default `build`), then writes `OUT/bake-result.json`. `--verify-reproducible` builds them a second time into `OUT/.reproduce` and fails with `E_REPRODUCIBILITY` unless every artifact's sha256 matches (see [Bake](#bake)). | |
| `measure` | Expected measurements of one baked variant. `--export-policy FILE` also writes the verifier policy `Tdxs.from_policy` reads (see [Measure and deploy](#measure-and-deploy)). | |
| `deploy` | Deploys one baked variant's artifact. | |
| `attest` | Asks a running image's `tdxs` issuer (`--endpoint`) for a quote bound to a nonce and checks its MRTD and RTMR0..RTMR3 against a verifier policy (`--policy`, from `measure --export-policy`): one `match`/`mismatch`/`unchecked` line per register, the nonce check, then `verdict: trusted` or `verdict: untrusted`. It checks measurements only; collateral verification is done by a Tdxs validator (see [Attest](#attest)). | the verdict is `untrusted` |
| `sbom` | The software bill of materials of one baked variant (`MANIFEST` or `--out DIR`, `--variant`): mkosi's package manifest beside the artifact, the lockfile's source pins and the recipe metadata, merged into SPDX 2.3 JSON (default), CycloneDX 1.5 JSON, text or Markdown (`--format`); `--output FILE` writes it to a file (see [SBOM](#sbom)). | |
| `evidence` | An auditor's record of one bake (`--out DIR`, default `build`; `--variant`, default every baked variant): recipe and tree digests, the lockfile with its drift verdict, each artifact with a fresh sha256 check, the reproducibility verdict, the measurements policy, an SPDX SBOM, the lint summary, a provenance summary and the tool versions, written to `OUT/evidence/` with an `evidence.json` index; `--bundle FILE.tar.gz` and `--html FILE` package it (see [Evidence](#evidence)). | the verdict is `fail` |
| `doctor` | Python and tundravm versions, the backend's host tools (for `local` also the optional `ukify`, `systemd-repart`, `apt` and `pefile`, probed under the Python that runs mkosi, and with a `RECIPE` that has `azure` or `gcp` variants the optional `qemu-img` or `sgdisk` their disk conversion needs on the host; see [Local backend](#local-backend)), the optional measurement tools; with `RECIPE`, its lint summary. | a required tool is missing |
| `ci` | `lint --strict`, `compile --check`, `lock --check` in order; stops at the first failure. The one `--lockfile` (default `build/tundravm.lock`) is loaded once and shared: lint and the tree check apply its pins, the lock check compares against it. | any step fails, including a missing lockfile |
| `status` | Read-only, network-free report of where the project stands, one line per item with a verdict, then the single most useful next command (see [Project status](#project-status)). | never (exit 0) |
| `clean` | Removes the chosen parts of the build output directory: `--sources`, `--tree`, `--artifacts`, `--state`, or `--all`; the lockfile only when `--lockfile` names it. With no part flag it lists what `--all` would remove (see [Project status](#project-status)). | a path could not be removed |
| `config` | Prints the `recipe`, `out`, `tree`, `lockfile` and `backend` the commands resolve and where each came from: `flag`, `pyproject`, `default` or, for `backend`, `recipe` (the kind of the recipe file's `backend`); a recipe or lockfile that does not exist is marked (see [Project configuration](#project-configuration)). | |
| `completion` | Prints a bash, zsh or fish completion script for this version's verbs, flags and flag choices (see [Shell completion](#shell-completion)). | |

`inspect`, `lint`, `compile`, `diff`, `fetch` and `bake` read `build/tundravm.lock` when it exists and apply its source pins; `--lockfile PATH`, or the `lockfile` of a [`[tool.tundravm]` table](#project-configuration), names another one. `ci` needs the lockfile and loads it once for all three steps; `status` reports on it.

## Project configuration

`tundravm init` writes a `[tool.tundravm]` table into the `pyproject.toml` it creates (an existing `pyproject.toml` is kept; `init` prints the table to add):

```toml
[tool.tundravm]
recipe = "node.py"
out = "build"
tree = "mkosi"
lockfile = "build/tundravm.lock"
backend = "inprocess"
```

Every command that takes `RECIPE` then runs without it: `tundravm status`, `tundravm lint`, `tundravm compile --check`. The rules:

- The table is read from the nearest `pyproject.toml` that has one, starting in the working directory and walking up. Paths in it are relative to that file. Unknown keys and non-string values are `E_VALIDATION`.
- Precedence per value: an explicit argument or flag, then the table, then the built-in default (`build`, `build/mkosi`, `build/tundravm.lock`, the recipe file's `backend`).
- The table's paths apply when `RECIPE` is omitted or names the table's recipe; another explicit `RECIPE` uses the built-in defaults.
- `out` is `--out` of `fetch`, `bake`, `status` and `clean`; `tree` is `--out` of `compile` and `ci`, `--against` of `diff` and the tree `status` checks; `lockfile` is `--lockfile` everywhere except `clean`, drift diagnostics of `lint` included; `backend` is `bake --backend` and `doctor --backend`, and `bake` keeps the recipe file's own backend instance when it is of that kind.
- A configured `lockfile` stays selected before the file exists and is never replaced by `build/tundravm.lock`. Until `tundravm lock` writes it, `inspect`, `lint`, `compile`, `diff` and `fetch` print `` note: configured lockfile PATH does not exist; run `tundravm lock` to create it `` on stderr and run without pins, `status` reports `configured lockfile PATH does not exist; run tundravm lock` on its `lock` line, and `bake` fails with `E_LOCKFILE` instead of baking unfrozen.
- Without `RECIPE` and without a table, a recipe command exits 2 with `the following arguments are required: recipe (no pyproject.toml with a [tool.tundravm] table ...)`; `doctor`, `clean` and `config` keep working without one.

`tundravm config` prints each value as the commands resolve it: the recipe path, the `out`, `tree` and `lockfile` paths, whether the recipe and the lockfile exist and, without `--backend` or a table `backend`, the kind of the backend the recipe file binds (origin `recipe`; `config` imports the recipe file to find it, with `--attr` and `--pythonpath` as the other commands take them). `--format json` prints `{"pyproject", "values": {KEY: {"value", "origin"}}}`, plus `exists` for `recipe` and `lockfile`. Its `--out`, `--tree`, `--lockfile` and `--backend` show how flags override the table. In a project `init` just wrote:

```console
$ tundravm config
pyproject: /home/me/node/pyproject.toml
  recipe    node.py              pyproject
  out       build                pyproject
  tree      mkosi                pyproject
  lockfile  build/tundravm.lock  pyproject; does not exist; run `tundravm lock`
  backend   inprocess            pyproject
```

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
  1. tundravm lock my-node.py                 record recipe sections and pin source repositories/downloads in build/tundravm.lock
  2. tundravm compile my-node.py --out mkosi  write the mkosi tree; commit it
  3. uv run pytest tests                      run test_my_node.py against mkosi/
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
Policy: mutable_ref_policy=warn network_mode=online require_frozen_lock=no storage_safety=warn
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

### Explaining one object

`inspect RECIPE --variant NAME --why SUBJECT` explains one object of one variant. `SUBJECT` is an absolute image path, `unit:NAME`, `package:NAME`, `hook:NAME` or `init:NAME` (a bare name is tried as each). It prints the declarations that produce it (type and natural key), each with its resolution steps: `declared in common`, `added in variant X`, `replaced in variant Y`, `removed in variant Z`, with the fragment chain it came through (a Composite shows its class, `tdxs (Tdxs)`). Then the compiled-tree files that hold it, relative to the variant's directory, and the lines the compiler generated for it: `After=`/`Requires=runtime-init.service` for `after_init`, `systemctl enable` and `minimal.target.wants` links, drop-ins. The variant is compiled into a scratch directory; only temporary compilation files are written (under the system temp dir). An unknown subject is `E_VALIDATION` with close matches. For a recipe whose `prod` variant extends `dev` and replaces `/etc/app.conf`:

```console
$ tundravm inspect two.py --variant prod --why /usr/lib/systemd/system/app.service
why /usr/lib/systemd/system/app.service (variant prod)
  Service(app.service)
    declared in common via base > app
files (in prod/):
  mkosi.extra/usr/lib/systemd/system/app.service
generated:
  After=runtime-init.service (after_init=True)
  Requires=runtime-init.service (after_init=True)
  scripts/06-postinst.sh: mkosi-chroot systemctl enable app.service
  scripts/06-postinst.sh: minimal.target.wants/app.service link
$ tundravm inspect two.py --variant prod --why /etc/app.conf
why /etc/app.conf (variant prod)
  File(extra, /etc/app.conf)
    declared in common via base > app
    replaced in variant prod
files (in prod/):
  mkosi.extra/etc/app.conf
```

`--format json` prints `{"variant", "subject", "declarations": [{"declaration", "type", "key", "present", "origins": [{"action", "variant", "fragments": [{"name", "class"}]}]}], "fragments", "files", "generated"}`; `--format markdown` a table of declarations and steps. A removed declaration is listed with `present: false` and no files. The Python form is `tundravm.explain_why(recipe, variant, subject)`.

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

`~` changed, `+` only in the recipe, `-` only in the lockfile. Source builds drift as `+ sources.<name>` (not pinned yet) or `~ sources.<name>: <old> -> <new>`; a build pinned per variant drifts as `sources.<variant>.<name>`, here after the `dev` variant's `app` build moved from `main` to `main~1`:

```
~ sources.dev.app: 3791980 -> main~1
~ variants.dev.source_builds: ~app
```

A clean lock prints `lock is up to date`. With `--format github` each line is `::error file=build/tundravm.lock,title=lock drift::variants.default.packages changed: +htop. Run `tundravm lock` and commit the lockfile.`; with `--format markdown` it is a `Change | Section | Detail` table.

Sections are the recipe-wide `distribution` (base, arch, mirrors, snapshot, epoch), `compiler` (tundravm version, dialect, mkosi options), `default_profile` and `init_scripts`, and `variants.<variant>.<key>` per variant (`packages`, `files`, `users`, `services`, `hooks`, `source_builds`, `kernel`, `debloat`, `mkosi` where it differs from the default variant's, ...). The lockfile is version 4. A version 3 lockfile still loads; `lock --check` reports it until it is locked again, and a frozen `bake` refuses it with `E_LOCKFILE` (`Frozen bake needs a version 4 lockfile`):

```
~ version: 3 -> 4: lock again to record the distribution, compiler and kernel sections
- arch
- base
+ compiler
+ distribution
+ variants.default.kernel
+ variants.dev.kernel
```

A version 2 lockfile, which named the per-variant sections `profiles.<variant>.<key>`, loads with the names mapped. The recipe digest covers the `compiler` section, so upgrading tundravm drifts the `compiler` section until you lock again.

`lock --check --variant NAME` checks only the named variants' sections and the recipe-wide ones. A lockfile written for every variant is therefore up to date for any selection, and the lockfile's sections for the other variants are never reported. The whole-recipe digest (`recipe_digest`) is compared only when the selection is every variant the lockfile holds. A lockfile written with `lock --variant NAME` holds only that variant, so checking or baking more variants against it reports their sections as `+`.

`lock` keeps the existing lockfile's pins while their source is unchanged. `--update NAME` resolves that source again (`--update kernel` or `--update kernel-<variant>` for a built kernel's source in the current dialect; `--update dev/app` for the `dev` variant's pin of a build pinned per variant, `--update app` for every variant's); an unknown name is an error. `--offline` never touches the network and fails for a source with no pin. `--explain` prints the drift before writing.

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

`tundravm fetch RECIPE` checks out, on this host and as you, every source the selected variants build from: each `Build` source and, outside `nethermind-v1`, each built kernel's source (a `Kernel` with `config`), named `kernel` or `kernel-<variant>` where a variant's kernel source differs, and the `.deb` an `EfiStub` installs, an http source named `efi-stub` (`efi-stub-<variant>` where a variant's package differs). A git source is checked out at its pinned commit, an http source is downloaded and checked against its sha256. Each lands in `OUT/.sources/<name>-<pin12>-<id8>/`, `id8` being the first 8 hex of a sha256 over the url, kind and, for git, subdirectory and submodules, with a JSON `.tundravm-complete` marker that records the pin and that identity (and, for http, a sha256 manifest of every file). There is one checkout per (source, pin). A complete checkout is verified and kept, so a second run touches nothing: git `HEAD` must be the pin, `git status` must list nothing (untracked and ignored files included) and requested submodules must be at their recorded commits; an http checkout must match its manifest. A modified checkout fails with `E_SOURCE` until `fetch --force` checks every source out again:

```console
$ tundravm fetch node.py
[tundravm] fetch 2 sources ...
[tundravm] note default/app: dffeffe8aaf0 already fetched
[tundravm] fetch 2 sources ... FAILED (0.0s)
error [E_SOURCE]: source dev/app checkout modified/incomplete: run tundravm fetch --force
Hint: 1 file changed since the fetch: hi.sh. Run `tundravm fetch RECIPE --force` to check dev/app out again (it replaces the checkout).
  path: build/.sources/app-70c99ab8a545-2d2fd0fc
[exit 2]
```

Because it runs as you, your git credentials and SSH agent apply, so private repositories work.

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

A build whose source differs between variants is pinned and fetched once per variant, as `<variant>/<name>` (`default/app`, `dev/app`); both checkouts are `app-<pin12>-<id8>`.

`inspect` shows each source's pin (`hello  git file:///work/hello  ref=v1.0.0  pinned=cdd0a52`). In the current dialect a build hook never clones: it copies its checkout from the mounted `.sources` into `$BUILDROOT/build/<name>` (a kernel's without `.git`), and a hook whose source has no pin only fails, with `run tundravm lock, then tundravm fetch`. `lock --update kernel` (or `kernel-<variant>`) moves a kernel's pin like any other source.

### Dependencies

For each `Go`, `Cargo` and `Dotnet` build, `fetch` then prefetches the dependencies with the host's toolchain, as you, in a scratch copy of the checkout: `go mod download` into `OUT/.sources/deps/go`, `cargo fetch --locked` into `OUT/.sources/deps/cargo`, `dotnet restore --runtime RID --packages` into `OUT/.sources/deps/nuget`. A marker, `deps/<name>-<pin12>-<id8>.<cache>.json`, records the command and environment and a content list of what the build needs from the cache: each module directory and `.mod` file `go.sum` names, each `.crate` of `Cargo.lock` (size and sha256), each NuGet package directory of the restore's `project.assets.json`. The next fetch keeps the cache only while the content list matches; otherwise it prefetches again. `fetch --force` removes each dependency cache and its markers and prefetches it again. The result lists the outcome in a `deps:` column, and each build gets a `note` line (a `warning` for a failure). For a recipe with a `Go` build `app` and a `Cargo` build `app-rs` of the same repository, on a host with `cargo` but no `go`:

```console
$ tundravm fetch node.py
[tundravm] fetch 2 sources ...
[tundravm] note app: fetched git file:///work/app @ v1.0.0 at cd6fb81f6fb2
[tundravm] note app: deps: skipped (no host go)
[tundravm] note app-rs: copied from app-cd6fb81f6fb2-902554f9: git file:///work/app @ v1.0.0 at cd6fb81f6fb2
[tundravm] note app-rs: deps: cargo: prefetched
[tundravm] fetch 2 sources ... ok (0.1s)
fetched build/.sources
  app     cd6fb81f6fb2  fetched  deps: skipped (no host go)
  app-rs  cd6fb81f6fb2  fetched  deps: cargo: prefetched
$ tundravm fetch node.py
...
  app     cd6fb81f6fb2  kept     deps: skipped (no host go)
  app-rs  cd6fb81f6fb2  kept     deps: cargo: kept
```

| `deps:` | Meaning |
|---|---|
| `go: prefetched`, `cargo: prefetched`, `nuget: prefetched` | the host toolchain filled the cache for this pin |
| `go: kept`, ... | an earlier fetch's marker and its content list match; nothing ran |
| `go: prefetched (was incomplete: deps/go/<entry> is missing or changed)`, ... | an entry of the content list was gone or changed, or the marker was written by another prefetch command; the toolchain ran again and repaired the cache |
| `go: incomplete (deps/go/<entry> is missing or changed)`, ... | the cache is incomplete and cannot be prefetched again here (no host toolchain, or offline); a `warning`, and the build downloads its dependencies in the sandbox |
| `skipped (no host go)` (`cargo`, `dotnet`) | the toolchain is not on `PATH`; the build downloads its dependencies in the sandbox |
| `skipped (offline)` | `Policy(network_mode="offline")` or `bake --offline`: nothing is downloaded |
| `failed (cargo fetch: <last stderr line>)`, ... | the prefetch failed (a `warning`); the build downloads its dependencies in the sandbox |

Script builds, kernels and `efi-stub` have no dependency cache and no `deps:` column. In the current dialect a build hook copies its cache to `$BUILDROOT/build/.tundravm-deps/<cache>` and exports `GOMODCACHE`/`GOFLAGS=-mod=mod`, `CARGO_HOME` or `NUGET_PACKAGES` at it, adding `GOPROXY=off`, `CARGO_NET_OFFLINE=true` or .NET `RestoreSources` when the script runs without network; `bake --offline` requires the cache (see [Bake](#bake)). The host's and the image's toolchains must agree: both cargo 1.85 or newer, the same .NET SDK, and an image Go that satisfies `go.mod` (see [reproducibility](reproducibility.md#hermetic-builds)).

`efi-stub` is fetched like any http source and installed by the postinst hook from the mounted copy with `dpkg -i`, after a sha256 check. A lockfile written before `EfiStub` became a source drifts as `+ sources.efi-stub` until `tundravm lock` pins it; compiled without that pin, the hook downloads the package in the sandbox.

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
  error [E_STATE]: Source build 'hello' is not fetched: build/.sources/hello-cdd0a5257ba9-2c640da8 is missing or incomplete.
  Hint: Run `tundravm fetch RECIPE --out build` first, or bake without --no-fetch.
    pin: cdd0a5257ba950088a826755fa832f40e87fc5c3
  [exit 2]
  ```

- `--offline` (or `Policy(network_mode="offline")` in the recipe) gives the build sandbox no network: the mkosi command gets `--with-network=no`, so build and postinst scripts cannot download anything (mkosi still installs the distribution packages from the configured mirror). The fetch step only reuses complete checkouts (a missing one fails it with `E_POLICY`, `Cannot fetch 'NAME': policy network_mode is offline.`), and every `Go`, `Cargo` and `Dotnet` build must find the dependency cache `tundravm fetch` prefetched into `OUT/.sources/deps`; a missing one fails before mkosi runs with `E_STATE` naming the build and `tundravm fetch`, and an incomplete one with `E_STATE` naming the missing entry (`Source build 'app' has an incomplete go dependency cache: deps/go/<entry> is missing or changed, ...`; the hint adds that `--force` refills every cache). A `nethermind-v1` recipe with source builds fails with `E_POLICY`: its hooks fetch in the sandbox. For the `app`/`app-rs` recipe of [Dependencies](#dependencies), fetched on a host without `go`:

  ```console
  $ tundravm bake node.py --backend local --offline
  ...
  [tundravm] note app: deps: skipped (offline)
  [tundravm] note app-rs: cd6fb81f6fb2 already fetched
  [tundravm] note app-rs: deps: cargo: kept
  [tundravm] fetch 2 sources ... ok (0.0s)
  ...
  [default] failed after 0.0s
  error [E_STATE]: Source build 'app' has no prefetched go dependencies, and an offline bake gives the build no network to download them.
  Hint: Run `tundravm fetch RECIPE --out build` on a host with `go` on PATH (it fills build/.sources/deps/go), or bake without --offline.
    marker: build/.sources/deps/app-cd6fb81f6fb2-902554f9.go.json
  ```
- The bake is frozen when `--lockfile` is given, when the `[tool.tundravm]` table configures a `lockfile` (which must then exist: a missing one fails before `lint` with `E_LOCKFILE`, `Configured lockfile PATH does not exist.`, and a hint to run `tundravm lock`), or when `build/tundravm.lock` exists: a recipe that drifted from the lock fails at `verify lockfile` with `E_LOCKFILE`. Without a lockfile it bakes unpinned and prints a note.
- The lockfile is copied to `OUT/tundravm.lock` when that file is absent or identical. A different `OUT/tundravm.lock` is never overwritten: `bake --out OUT --lockfile PATH` bakes against `PATH` and leaves it alone.
- The frozen check compares like `lock --check` with the same `--variant` selection: a lockfile of every variant covers `bake --variant default`, while a lockfile written for fewer variants than the bake selects fails at `verify lockfile`.
- A recipe with error-level lint findings fails at `lint` with `E_LINT`.
- `OUT/bake-result.json` records, per variant and target, the artifact path and sha256, plus the lockfile digest and a `declarative` block with the recipe digest, the tree digest, `simulated` (true for the in-process backend) and, for a frozen bake, `lockfile`: the path of the lockfile it baked against, as given (`--lockfile PATH` or the table's `lockfile`, else `build/tundravm.lock`).
- `--verify-reproducible` bakes the same selection a second time into `OUT/.reproduce`, against the same lockfile and the same `OUT/.sources` checkouts (hard-linked there, so nothing is fetched again; mkosi's state is not shared, so the second build compiles and builds everything, tools tree included), then compares every artifact's sha256. `bake-result.json` records the outcome as `declarative.reproducible` (absent when the check did not run) and `status` shows it on each artifact line. When all match the second build is removed and the summary is followed by `reproducible: yes (N artifacts match a second build)`. A mismatch keeps it and fails with `E_REPRODUCIBILITY`, a table per artifact and a hint naming `tundravm diff` and the usual causes:

  ```console
  $ tundravm bake node.py --verify-reproducible -q
  error [E_REPRODUCIBILITY]: The bake is not reproducible: 1 of 2 artifact(s) differ between two builds.
    variant  target  first         second
    default  azure   0c5b4a2e9f13  0c5b4a2e9f13  match
    default  qemu    1fa043adea90  6d2e81c4b7a0  mismatch
  Hint: The second build is kept in build/.reproduce. Compare the compiled trees with `tundravm diff RECIPE --against build/.reproduce/mkosi` and the images with diffoscope. Usual causes: timestamps (set SOURCE_DATE_EPOCH), build ids, and packages installed without a snapshot (Recipe(snapshot=...)).
    second_build: build/.reproduce
    differ: default/qemu
  [exit 2]
  ```

  With the Lima backend the second build runs in its own VM (the instance name hashes the tree path). `clean --state` removes a kept `OUT/.reproduce`.
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

`measure MANIFEST` and `deploy MANIFEST` take `bake-result.json` or the directory holding it. Both hash the artifact first and fail with `E_ARTIFACT_CHANGED` when it is unreadable or no longer matches the sha256 the bake recorded:

```console
$ tundravm measure build --variant default --allow-placeholder
error [E_ARTIFACT_CHANGED]: Artifact build/default/disk.qcow2 changed since the bake.
Hint: bake-result.json records sha256 1fa043adea90; Bake the variant again to record the artifact it should be.
  variant: default
  target: qemu
  path: build/default/disk.qcow2
  recorded: 1fa043adea909e9ad008e0db46ed1c9935adf90aee9c49d20940d6316c5c2e79
  actual: e9f650ccebad2410588216a77dc68623887949de69b8165992133992d805053d
[exit 2]
```

- `measure --scheme rtmr` (default) runs `measured-boot` or `dstack-mr`. Without one, or for `azure`/`gcp`, it fails unless `--allow-placeholder`, which prints digest-derived values under a `PLACEHOLDER` banner on stderr. Simulated artifacts always need `--allow-placeholder`. `--json` prints `artifact`, `artifact_digest`, `scheme`, `tool`, `values`, `variant`.
- `measure --export-policy FILE` (rtmr only) also writes a verifier policy: `{"schema_version": 1, "scheme": "rtmr", "tool", "tool_version", "artifact": {"path", "sha256"}, "registers": {"RTMR0", "RTMR1", "RTMR2"}}` (`RTMR3` when measured). Missing `RTMR0`..`RTMR2` is `E_VALIDATION`; placeholder values are refused unless `--allow-placeholder`, and then carry a `"note"` that says they are placeholders. `Tdxs.from_policy(FILE)` turns it into a validator's `expected_measurements`.
- `deploy --target` picks the artifact of that target. `--param` keys are the target's settings: qemu `memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`, `forward` (`HOST:GUEST[,HOST:GUEST...]`); azure `storage_account` (required), `resource_group`, `location`, `vm_size`, `gallery`, `secure_boot`, `signed`; gcp `project` and `bucket` (required), `zone`, `machine_type`. `--attach` (qemu only, the same as `--param daemonize=false`) runs QEMU in the foreground with the serial console on the terminal: Ctrl-A X quits QEMU, Ctrl-A C toggles its monitor, and `deploy` prints its result once QEMU exits. Simulated artifacts are refused unless `--allow-simulated-artifact`.

### Attest

`tundravm attest --endpoint URL --policy FILE` checks what a running image measured. It sends the image's `tdxs` issuer an `issue` request with a nonce (`--nonce HEX`, default 32 random bytes), reads MRTD and RTMR0..RTMR3 from the TDX quote in the reply (DCAP quote v4 or v5), and compares them with the policy's `registers`: `match`, `mismatch` (both values printed), or `unchecked` for a register the policy does not hold (MRTD and RTMR3 unless it does). The quote's `report_data` must be `SHA-256(nonce)` followed by 32 zero bytes, as the issuer binds it, so a replayed quote fails the `nonce` line. The verdict is `trusted` only when the nonce matches and no register mismatches; exit 0 when trusted, 1 when untrusted, 2 on an SDK error. It checks measurements only: the quote's signature, certificate chain and collateral are verified by a Tdxs validator (`Tdxs.from_policy`), not here.

```console
$ ssh -p 2222 -N -L ./tdxs.sock:/var/tdxs.sock root@localhost &
$ tundravm attest --endpoint unix:./tdxs.sock --policy peer.json
attestation unix:./tdxs.sock (tdx, quote v4)
policy: peer.json
nonce  match      000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
MRTD   unchecked  1111...1111
RTMR0  match      2020...2020
RTMR1  mismatch   7777...7777
       policy     2121...2121
RTMR2  match      2222...2222
RTMR3  unchecked  2323...2323
note: checks measurements only; collateral verification is done by a Tdxs validator
verdict: untrusted
[exit 1]
```

- `--endpoint` is `unix:PATH` (or a bare path) or `tcp://HOST:PORT`. The `tdxs` service listens on the unix socket `/var/tdxs.sock` in the image and speaks JSON lines: one `{"method": "issue", "data": {"userData": HEX, "nonce": HEX}}` request, one `{"data": {"document": HEX}, "error": null}` reply, whose document is the hex of the JSON attestation document (`raw_quote`, `user_data`, `nonce` in base64, and `platform`). Reach it from the host by forwarding the socket over SSH as above, or with a forwarded TCP port (`deploy --param forward=7000:7000` and a `socat TCP-LISTEN:7000,fork UNIX-CONNECT:/var/tdxs.sock` in the image). tdxs serves no http, so an `http://` or `https://` endpoint fails with `E_VALIDATION`.
- A `simulator` issuer's quote is not a TDX quote: its registers come from the issuer's `metadata` reply (`(simulator, issuer metadata)` in the header), and its nonce check reads the quote's first 32 bytes.
- An unreachable endpoint or an issuer that replies with an `error` (`issuer error: tdx: failed to get raw quote: ...`) is `E_DEPLOYMENT`; a reply that is not a TDX quote is `E_MEASUREMENT`; a placeholder policy is refused with `E_MEASUREMENT`; a bad `--nonce` or policy file is `E_VALIDATION`.
- `--format json` prints `{"checks", "endpoint", "nonce": {"value", "report_data", "verdict"}, "platform", "policy", "quote_version", "registers": {NAME: {"actual", "expected", "verdict"}}, "source": "quote"|"metadata", "trusted", "verdict"}` with sorted keys; `--format markdown` a table of the registers under the verdict.

### Deploying to each target

A successful `deploy` means the target accepted the image: QEMU started, or the cloud created the VM. It does not mean the image booted, that its services run, or that it attests; each sequence below ends with those checks. These sequences have not been run end to end against QEMU, Azure or GCP: the command lines and the `deploy` output below are what the adapters in `src/tundravm/deploy/` run and print, captured with their tool calls stubbed out, and the checks after `deploy` are the platforms' own commands.

**QEMU.** Needs `qemu-system-x86_64` on `PATH` and KVM (`/dev/kvm`: the adapter always passes `-machine q35,accel=kvm -cpu host`). A bootable variant bakes to a UKI (`.efi`), which boots with `-kernel` on OVMF firmware: `OVMF_CODE.fd` (or `OVMF_CODE_4M.fd`) under `/usr/share/OVMF`, `/usr/share/edk2/ovmf` or `/usr/share/qemu`, plus a copy of the matching `OVMF_VARS` file made once at `OUT/<variant>/qemu-ovmf-vars.fd`, since the guest writes its variables there. A `.qcow2`, `.vhd` or raw disk boots as a virtio drive instead.

```console
$ tundravm bake image.py --variant default --backend local
$ tundravm measure build --variant default --export-policy peer.json
$ tundravm deploy build --variant default --target qemu --param memory=4G --param cpus=4 --param ssh_port=2222
deployed default to qemu
  deployment     qemu-default-030a44c0
  endpoint       ssh://localhost:2222
  artifact_path  build/default/output/node_0.1.0.efi
  cpus           4
  forward        8080:8080
  is_uki         true
  memory         4G
  monitor        /work/build/default/qemu.monitor
  pidfile        /work/build/default/qemu.pid
  serial_log     /work/build/default/qemu-serial.log
  ssh_port       2222
  tdx            false
```

That runs (the variant declares `Secrets`, whose delivery listens on port 8080):

```
qemu-system-x86_64 -machine q35,accel=kvm -cpu host -m 4G -smp 4 -no-reboot
  -drive file=/usr/share/OVMF/OVMF_CODE_4M.fd,if=pflash,format=raw,readonly=on
  -drive file=/work/build/default/qemu-ovmf-vars.fd,if=pflash,format=raw
  -kernel build/default/output/node_0.1.0.efi
  -netdev user,id=net0,hostfwd=tcp::2222-:22,hostfwd=tcp::8080-:8080 -device virtio-net-pci,netdev=net0
  -display none -serial file:/work/build/default/qemu-serial.log
  -monitor unix:/work/build/default/qemu.monitor,server,nowait
  -daemonize -pidfile /work/build/default/qemu.pid
```

- The endpoint is user-mode networking: host port `ssh_port` (default `2222`) to the guest's port 22. Every port a `Secrets` of the variant listens on (recorded by the bake in `bake-result.json`) is forwarded from the same host port, so the host can deliver secrets to `localhost:8080`; `--param forward=HOST:GUEST,...` adds forwards, and one naming a `Secrets` port as its guest port replaces that default (`forward=18080:8080`). Two forwards on one host port are refused.
- QEMU detaches by default (`daemonize=true`): the serial console goes to `serial_log` (`tail -f build/default/qemu-serial.log`), the monitor listens on the `monitor` socket (`socat - UNIX-CONNECT:build/default/qemu.monitor`, then `info status` or `quit`) and the pid to `pidfile` (`kill "$(cat build/default/qemu.pid)"`). The three live in `OUT/<variant>`, so a second `deploy` of a variant that is still running fails on its pid file. The socket path must fit a unix socket (107 bytes), or `deploy` fails before starting QEMU. `--attach` keeps QEMU in the foreground instead, with `-nographic -serial mon:stdio` and no log, socket or pid file.
- `tdx=true` boots TDVF, OVMF built with TDX support, through `-bios` (a TDX guest cannot run firmware from pflash): `/usr/share/ovmf/OVMF.fd`, `/usr/share/edk2/ovmf/OVMF.inteltdx.fd`, `/usr/share/OVMF/OVMF.fd` or `/usr/share/qemu/OVMF.fd`, the first found. It replaces the two pflash drives above with `-bios /usr/share/ovmf/OVMF.fd` and passes `-machine q35,accel=kvm,kernel-irqchip=split,confidential-guest-support=tdx0 -object tdx-guest,id=tdx0`, which needs a TDX host (kernel, KVM and QEMU with TDX support).
- Checks: `ssh -p 2222 root@localhost` once the image's `sshd` has a key for you (boot), `systemctl status` of your services there (application), and the quote checked against `peer.json` with `tundravm attest --endpoint unix:./tdxs.sock --policy peer.json` after forwarding the image's `/var/tdxs.sock` (see [Attest](#attest)), or by a verifier built with `Tdxs.from_policy("peer.json")`, which also verifies the collateral (attestation; needs `tdx=true` on a TDX host).

**Azure.** Needs `az` on `PATH` after `az login`, an existing resource group (`resource_group`, default `tdx-vms`) and a storage account (`storage_account`, required); the adapter creates the `tdx-images` container in it when missing. The blob upload passes no credentials, so `az` must be able to look up the account key (or read `AZURE_STORAGE_KEY`). A confidential VM boots only from an Azure Compute Gallery image whose definition supports it, so the adapter publishes the VHD as a gallery image version first. `vm_size` must be a TDX confidential size: the DCesv5/DCedsv5 series (default `Standard_DC2es_v5`) or the ECesv5/ECedsv5 series. Bake an `azure` variant, which converts its disk to a fixed VHD:

```console
$ tundravm bake image.py --variant azure --backend local
$ tundravm deploy build --variant azure --target azure --param storage_account=mystorage
deployed azure to azure
  deployment      azure-azure-336db4ce
  endpoint        azure://tdx-vms/tdx-azure-6fdd87
  artifact_path   build/azure/output/node_0.1.0.vhd
  blob            tdx-images/node_0.1.0-e79c907b.vhd
  image           /subscriptions/<sub>/resourceGroups/tdx-vms/providers/Microsoft.Compute/galleries/tdx_images/images/tdx-azure/versions/1.0.1791175553
  location        eastus
  resource_group  tdx-vms
  vm_name         tdx-azure-6fdd87
  vm_size         Standard_DC2es_v5
```

That runs:

```
az storage container create --account-name mystorage --name tdx-images --output json
az storage blob upload --account-name mystorage --container-name tdx-images --name node_0.1.0-<8 hex>.vhd
  --file build/azure/output/node_0.1.0.vhd --type page --output json
az storage account show --name mystorage --query id --output json
az sig create --resource-group tdx-vms --gallery-name tdx_images --location eastus --output json
az sig image-definition create --resource-group tdx-vms --gallery-name tdx_images --gallery-image-definition tdx-azure
  --location eastus --publisher tundravm --offer azure --sku azure --os-type Linux --os-state specialized
  --hyper-v-generation V2 --features SecurityType=ConfidentialVMSupported --output json
az sig image-version create --resource-group tdx-vms --gallery-name tdx_images --gallery-image-definition tdx-azure
  --gallery-image-version 1.0.<unix time> --location eastus --os-vhd-storage-account <storage account id>
  --os-vhd-uri https://mystorage.blob.core.windows.net/tdx-images/node_0.1.0-<8 hex>.vhd --query id --output json
az vm create --resource-group tdx-vms --name tdx-azure-<6 hex> --location eastus --size Standard_DC2es_v5
  --image <image version id> --specialized --security-type ConfidentialVM
  --os-disk-security-encryption-type VMGuestStateOnly --enable-vtpm true --enable-secure-boot false
  --public-ip-sku Standard --output json
```

- The image definition is `tdx-<variant>` in the gallery `gallery` (default `tdx_images`; letters, digits, `_` and `.` only); an existing one must already be a specialized V2 Linux definition with `SecurityType=ConfidentialVMSupported`. Each deploy adds an image version `1.0.<unix time>`.
- Secure Boot is off by default (`--enable-secure-boot false`): with it on, Azure's firmware boots only images signed with keys it trusts, and tundravm does not sign the UKI. `secure_boot=true` requires `signed=true`, your statement that the UKI was signed with such keys outside tundravm (`Azure(secure_boot=True, signed=True)`); without it `deploy` fails with `E_DEPLOYMENT` before uploading anything, and its hint names `--param secure_boot=false` and signing.
- The endpoint names the VM, not an address: `az vm show -d -g tdx-vms -n tdx-azure-6fdd87 --query publicIps -o tsv` prints its IP, and `az vm boot-diagnostics get-boot-log -g tdx-vms -n tdx-azure-6fdd87` its serial console. `az vm create` also creates a NIC, a public IP and a disk; deleting the VM leaves them, so deploy into a resource group of its own and tear down with `az group delete -n tdx-vms` (the gallery goes with it), then remove the blob named by `blob`: `az storage blob delete --account-name mystorage -c tdx-images -n node_0.1.0-e79c907b.vhd`.

**GCP.** Needs `gcloud` on `PATH` after `gcloud auth login`; `project` and `bucket` (an existing GCS bucket) are required. The gcloud must be current: one whose GA `gcloud compute instances create` takes `--confidential-compute-type=TDX`, which any release since Intel TDX on C3 became generally available (late 2024) does (`gcloud components update` updates an older one). The upload runs `gcloud storage cp` directly; `gsutil` is not used. Intel TDX instances run on the C3 machine series (default `c3-standard-4`) in zones that offer it. Bake a `gcp` variant, which packs its disk as a `tar.gz`:

```console
$ tundravm bake image.py --variant gcp --backend local
$ tundravm deploy build --variant gcp --target gcp --param project=my-project --param bucket=my-bucket
deployed gcp to gcp
  deployment     gcp-gcp-ef5e8a92
  endpoint       gcp://my-project/us-central1-a/tdx-gcp-c95cfe
  artifact_path  build/gcp/output/node_0.1.0.tar.gz
  blob           gs://my-bucket/tdx-images/tdx-gcp-45c3b6c0.tar.gz
  image_name     tdx-gcp-45c3b6c0
  machine_type   c3-standard-4
  project        my-project
  vm_name        tdx-gcp-c95cfe
  zone           us-central1-a
```

That runs:

```
gcloud storage cp build/gcp/output/node_0.1.0.tar.gz gs://my-bucket/tdx-images/tdx-gcp-<8 hex>.tar.gz
gcloud compute images create tdx-gcp-<8 hex> --project=my-project
  --source-uri=gs://my-bucket/tdx-images/tdx-gcp-<8 hex>.tar.gz --guest-os-features=UEFI_COMPATIBLE,GVNIC,TDX_CAPABLE
gcloud compute instances create tdx-gcp-<6 hex> --project=my-project --zone=us-central1-a
  --machine-type=c3-standard-4 --image=tdx-gcp-<8 hex> --confidential-compute-type=TDX
  --maintenance-policy=TERMINATE --format=json
```

A C3 instance has a gVNIC network interface and NVMe disks, so the image's kernel needs the `gve` and `nvme` drivers. `gcloud compute instances get-serial-port-output tdx-gcp-c95cfe --zone us-central1-a --project my-project` shows the boot. Tear down with `gcloud compute instances delete tdx-gcp-c95cfe --zone us-central1-a --project my-project`, `gcloud compute images delete tdx-gcp-45c3b6c0 --project my-project` and `gcloud storage rm gs://my-bucket/tdx-images/tdx-gcp-45c3b6c0.tar.gz`.

## SBOM

`tundravm sbom MANIFEST --variant NAME` (or `--out DIR` instead of `MANIFEST`; default `build`) says what one baked variant contains, merged from three sources into one document:

- **Packages**: the JSON package manifest mkosi writes next to the artifact (`ManifestFormat=json` is emitted for every variant, so a real bake leaves `OUT/<variant>/output/<variant>.manifest`; `<variant>.manifest.json` is read too). Each distribution package with its version and architecture, plus its repository when the manifest records one (mkosi 26 does not). Without a manifest, as after an in-process bake, the document says so (a `note:` line, `creationInfo.comment`, CycloneDX `metadata.properties`) and lists the packages the recipe declares, unversioned.
- **Sources**: the lockfile's pins for the variant: each source build (`<variant>/<name>` when pinned per variant) with its URL, ref, commit and the paths its `Install` steps write; the built kernel (`kernel`, `kernel-<variant>`); the `EfiStub` package (`efi-stub`) with its URL and sha256. `--lockfile FILE` names the lockfile; by default it is the one the bake recorded in `bake-result.json`, else `tundravm.lock` in the bake directory. Without one, sources are not listed.
- **Metadata**: variant, base, arch, snapshot and mirror (from the lockfile's `distribution` section, else the manifest's `config`), the recipe, tree and artifact digests from `bake-result.json`, the manifest path and the tundravm version.

| `--format` | Shape |
|---|---|
| `spdx-json` (default) | SPDX 2.3: `SPDXRef-DOCUMENT` `DESCRIBES` the image package (`SPDXRef-Image`, its artifact sha256 as `checksums`, the metadata as `comment`), which `CONTAINS` each distribution package and is `GENERATED_FROM` each source. Every package has `name`, `versionInfo` (a source's commit or sha256), `downloadLocation` (`git+URL@COMMIT`, an http URL, or `NOASSERTION`), `checksums` when a sha256 is known and a `purl` external reference. |
| `cyclonedx-json` | CycloneDX 1.5: the image is `metadata.component` (`operating-system`, metadata as `tundravm:*` properties); packages are `library` components, builds and kernels `application`; `externalReferences` (`vcs`/`distribution`), `hashes`, `tundravm:install`/`tundravm:ref` properties; one `dependencies` entry from the image to every component. |
| `text` | The metadata, then aligned `packages` and `sources` tables and the notes. |
| `markdown` | The same as tables, ready for `$GITHUB_STEP_SUMMARY`. |

Package URLs: `pkg:deb/debian/NAME@VERSION?arch=ARCH&distro=debian-trixie` for a package (the base with `/` as `-`; `rpm`/`alpm` for those manifests), `pkg:generic/NAME@COMMIT?vcs_url=git+URL@REF` for a git source and `pkg:generic/NAME@SHA256?checksum=sha256:SHA256&download_url=URL` for an http one, percent-encoded in the canonical form (`+` is `%2B`, `@` inside a qualifier `%40`). Every list is sorted and the document ids are derived from the content, so the same bake gives the same document; only the creation time changes, and `SOURCE_DATE_EPOCH` fixes it. Notes go to stderr as well for the JSON formats and with `--output FILE`, which writes the document instead of printing it.

```console
$ tundravm sbom build --variant default --format text
sbom default
  variant          default
  base             debian/trixie
  arch             x86_64
  snapshot         20251113T083151Z
  ...
packages (137)
  NAME        VERSION             ARCH   ORIGIN
  adduser     3.152               all    -
  ...
sources (5)
  NAME      KIND      PIN                                       REF     URL                                         INSTALLS
  tdxs      build     cbc33ec538aaa60e54e2e889eacebb9b0c5ab479  master  https://github.com/Hyodar/tundra-tools.git  /usr/bin/tdxs
  efi-stub  efi-stub  25b7576d142c...                           -       https://snapshot.debian.org/archive/...     -
$ tundravm sbom build --variant default --output build/default.spdx.json
wrote spdx-json build/default.spdx.json
```

## Evidence

`tundravm evidence [RECIPE]` gathers what an auditor asks about a bake into `OUT/evidence/` (`--out DIR`, default `build`, the directory holding `bake-result.json`), for every baked variant or the `--variant` names (a variant that was not baked is an `E_STATE` error):

| Member | Holds |
|---|---|
| `evidence.json` | The index: `schema_version`, `created` (ISO 8601 UTC; `SOURCE_DATE_EPOCH` when set), `recipe` (`name`, `path`, `file_sha256`, `digest` now, `baked_digest` the bake recorded, `matches_bake`), `bake` (`path`, `backend`, `simulated`), `lockfile` (`source`, `version`, `compiler_version`, `recipe_digest`, `sha256`, `drift`, `verdict`: `current` or `drifted`; `null` without one), `lint` (`errors`, `warnings`, `infos`, `codes`), `tools` (`tundravm`, `python`, `mkosi` as `mkosi --version` prints it on this host, `null` when absent), per variant its `artifacts` (`target`, `path`, `size`, `sha256`, `integrity`: `verified`, `mismatch` or `missing`, `simulated`), `tree_digest`, `tree_matches`, `reproducible` (what `bake --verify-reproducible` recorded, `null` when not checked), `policy`, `sbom` and `provenance` (declarations per fragment and resolution actions: `declared`, `added`, `replaced`, `removed`), the `verdict`, the `notes` and `members` (`sha256` and `size` of every other file) |
| `bake-result.json` | The bake manifest, verbatim |
| `tundravm.lock` | The lockfile, verbatim: `--lockfile FILE`, else the one the bake recorded, else `OUT/tundravm.lock` |
| `lint.json` | Every lint diagnostic of the selected variants, with the lockfile's pins applied |
| `variants/<variant>/sbom-<target>.spdx.json` | The SPDX 2.3 SBOM of each artifact, as `tundravm sbom` writes it |
| `variants/<variant>/policy.json` | The measurements policy: `--policy FILE` for every selected variant, else `OUT/<variant>/policy.json` when it exists; the index says whether it is a placeholder and whether its `artifact.sha256` is the artifact's |

The `verdict` holds `integrity` (`verified` when every artifact still hashes to the sha256 the bake recorded), `lock` (`current`, `drifted` or `missing`), `reproducible` (`reproducible`, `not reproducible` or `not checked`), `lint` (`clean`, `warnings` or `errors`) and `overall`: `pass` when integrity is verified, the lock current, lint has no errors and the bake was not recorded as not reproducible, else `fail`, and `evidence` exits 1. What could not be included (no lockfile, no policy, no mkosi manifest beside a simulated artifact) is a note in the index and in the output.

| Flag | Effect |
|---|---|
| `--out DIR` | The bake output directory (default `build`, or `out` of `[tool.tundravm]`) |
| `--lockfile FILE` | The lockfile to record and check for drift |
| `--policy FILE` | A `measure --export-policy` file for every selected variant |
| `--bundle FILE` | Also write a deterministic `tar.gz`: members sorted under `evidence/`, owned by `0:0` with mode `0644`, every mtime (and the gzip header's) `SOURCE_DATE_EPOCH`, else 0, so two runs over the same bake with the same `SOURCE_DATE_EPOCH` give the same bytes |
| `--html FILE` | Also write one self-contained HTML page (inline CSS, no scripts or external assets, every value escaped): a verdict banner (integrity, lock, reproducible, lint), the recipe, lockfile, lint and tools tables, a section per variant (artifacts, policy, SBOM, provenance), the notes and the members with their sha256 |
| `--format text\|json` | How the index is printed: a summary with every member's sha256 (default), or `evidence.json` itself |

```console
$ SOURCE_DATE_EPOCH=1700000000 tundravm evidence node.py --bundle evidence.tar.gz --html evidence.html
evidence 2023-11-14T22:13:20Z  verdict pass
  recipe     node.py  digest 92efc03f419a
  integrity  verified
  lock       current
  reproduce  not checked
  lint       clean
  default/qemu  build/default/disk.qcow2  sha256 1fa043adea90  verified
members:
  e10905461284...  bake-result.json
  ...
```

## Project status

`tundravm status RECIPE` answers "where is this project at?" without writing anything or touching the network. It prints one line per item, `LABEL VERDICT DETAIL`, for the selected variants (`--variant`, default all), then `next: COMMAND`:

| Line | Reports | Verdicts |
|---|---|---|
| `recipe` | path, recipe digest (as `inspect --format json`), variants, mkosi dialect, base and snapshot | `ok` |
| `lint` | diagnostic counts by level, linted with the `lock` line's lockfile (no pins when it is missing or unreadable) | `ok`, `error` when there are errors |
| `lock` | `--lockfile` or the table's `lockfile` (default `build/tundravm.lock`; a configured one that does not exist reads `configured lockfile PATH does not exist; run tundravm lock`): present, version, drifted sections (as `lock --check`), unpinned sources and kernels | `ok`, `stale`, `missing` |
| `source` | one per source build, built kernel and `EfiStub` package (`efi-stub`), by lock key: whether `OUT/.sources/<name>-<pin12>-<id8>` holds a complete, unmodified checkout of the locked pin (verified as `fetch` does) | `ok`, `stale` (another pin, an incomplete checkout, or one modified since the fetch: `checkout at PATH modified since the fetch (...); run tundravm fetch --force`), `missing`, `n/a` (unpinned; `nethermind-v1`; or the `inprocess` backend, which needs none) |
| `tree` | `OUT/mkosi` (`--out`, default `build`) against what the recipe compiles to now, as `compile --check` | `ok`, `stale`, `missing` |
| `artifact` | one per baked variant and target in `OUT/bake-result.json`: path, size, sha256 prefix, integrity (`unchecked`; with `--verify` the file is hashed: `verified` or `mismatch`), `reproducible` or `not reproducible` when `bake --verify-reproducible` recorded it (`"reproducible": true`, `false` or `null` in JSON), `simulated`, the lockfile the bake used; stale when the recipe digest or the tree digest it was baked from no longer matches, or on an integrity mismatch | `ok`, `stale`, `missing` |
| `backend` | the recipe file's `backend` and how many of its host tools `doctor` finds | `ok`, `missing`, `n/a` (no backend) |

`next` follows the lifecycle: `tundravm lint` when there are errors, else `tundravm lock`, `tundravm fetch`, `tundravm compile --out OUT/mkosi`, `tundravm bake` (or `tundravm doctor` when the backend lacks a tool), and `everything is up to date` once every line is `ok` or `n/a`. It repeats `--variant`, `--out` and `--lockfile` as given (values from `[tool.tundravm]` are left out, so a configured project gets `next: tundravm lock`); a checkout modified since the fetch makes it `tundravm fetch --force`. `status` always exits 0. `--format json` prints one object per section (`recipe`, `lint`, `lock`, `sources`, `tree`, `artifacts`, `backend`), each with a `verdict`, plus `next`; `sources` and `artifacts` hold `items`. `--format markdown` prints a table per section.

After `init --backend inprocess`, `lock` and `bake --backend inprocess`, adding a package to the recipe:

```console
$ tundravm status node.py
recipe    ok       node.py  digest 49222658d740  variants default, dev  dialect current  base debian/trixie
lint      ok       0 errors, 0 warnings, 0 infos
lock      stale    build/tundravm.lock  v4  2 sections drifted
source    n/a      no source builds or kernels
tree      stale    build/mkosi: 2 files differ from the recipe
artifact  stale    default/qemu  build/default/disk.qcow2  114 B  sha256 1fa043adea90  integrity unchecked (--verify hashes it)  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
artifact  stale    dev/qemu  build/dev/disk.qcow2  110 B  sha256 9cfb8b5c2f31  integrity unchecked (--verify hashes it)  simulated  lock build/tundravm.lock  recipe and tree changed since the bake
backend   ok       inprocess: no host tools needed
next: tundravm lock node.py
```

`status --verify` hashes each artifact; an artifact whose bytes changed since the bake is `stale`:

```console
$ tundravm status node.py --verify
...
artifact  stale    default/qemu  build/default/disk.qcow2  116 B  sha256 1fa043adea90  integrity mismatch  simulated  lock build/tundravm.lock
artifact  ok       dev/qemu  build/dev/disk.qcow2  110 B  sha256 9cfb8b5c2f31  integrity verified  simulated  lock build/tundravm.lock
...
next: tundravm bake node.py
```

`tundravm clean RECIPE` (or `clean --out DIR` without a recipe) removes build output by part: `--sources` (`OUT/.sources`), `--tree` (`OUT/mkosi`), `--artifacts` (each `OUT/<variant>/` and `OUT/bake-result.json`), `--state` (`OUT/.mkosi`, which holds the cached tools tree, the mkosi state next to the tree's config, and `OUT/.reproduce`, the second build a failed `bake --verify-reproducible` keeps), or `--all`. The lockfile is never removed unless `--lockfile PATH` names it. It prints `removed PATH` per path, or `would remove PATH` with `--dry-run`; with no part flag it lists what `--all` would remove and removes nothing:

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
| 1 | A check failed: `lint` findings, `compile --check`, `diff`, `lock --check`, `ci`, `doctor` missing a required tool, an `untrusted` `attest` verdict, or an `evidence` verdict of `fail`; or `clean` could not remove a path |
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

The steps share one loaded recipe and one lockfile (`--lockfile`, default `build/tundravm.lock`, read once): `lint --strict` and `compile --check` apply its pins, so a pinned source never reports `source-unpinned`, and the lock step compares against it. The compile step works on a copy of the recipe, so a recipe with runtime-init steps (keys, disks, secrets, `Init`) passes the lock step whenever `tundravm lock --check` passes on its own.

`tundravm init --ci github` writes a workflow that runs `uv sync`, appends `tundravm inspect RECIPE --format markdown` to `$GITHUB_STEP_SUMMARY`, then runs `tundravm ci RECIPE --out mkosi`. Commit `mkosi/` and `build/tundravm.lock`; the workflow checks both.
