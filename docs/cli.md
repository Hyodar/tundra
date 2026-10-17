# CLI

`tundravm` (also `python -m tundravm`) runs the lifecycle on a recipe file. Every recipe command loads the `Recipe` the file binds and runs one step on the selected variants; `measure`, `deploy` and `sbom` read the `bake-result.json` a bake wrote into the build directory.

```
tundravm init [DIR] [--name NAME] [--template minimal|service|cloud|prover] [--base BASE] [--backend KIND]
              [--ci github|none] [--with-tests | --no-tests] [--force] [--no-doctor]
tundravm init --list-templates
tundravm inspect RECIPE [--variant NAME]... [--format text|json|markdown] [--diff-variants A B] [--lockfile PATH]
tundravm inspect RECIPE --variant NAME --why SUBJECT [--format text|json|markdown] [--lockfile PATH]
tundravm lint    RECIPE [--variant NAME]... [--format auto|text|json|github|markdown] [--strict] [--lockfile PATH]
tundravm compile RECIPE [--variant NAME]... [--tree DIR] [--lockfile PATH] [--check] [--format auto|text|markdown|github]
tundravm diff    RECIPE [--variant NAME]... [--tree DIR] [--lockfile PATH] [--format auto|text|stat|markdown|github | --stat] [--color auto|always|never]
tundravm lock    RECIPE [--variant NAME]... [--lockfile PATH] [--update SOURCE]... [--check | --offline] [--explain] [--format auto|text|github|markdown]
tundravm fetch   RECIPE [--variant NAME]... [--build DIR] [--lockfile PATH] [--force]
tundravm bake    RECIPE [--variant NAME]... [--build DIR] [--lockfile PATH] [--backend KIND] [--no-fetch] [--offline] [--verify-reproducible] [-v | -q | --json-logs] [--color auto|always|never]
tundravm measure [--build DIR] [--variant NAME] [--scheme rtmr|azure|gcp] [--format text|json|markdown] [--export-policy FILE] [--allow-placeholder]
tundravm deploy  TARGET [--build DIR] [--variant NAME] [--param KEY=VALUE]... [--attach] [--allow-simulated-artifact]
tundravm attest  --issuer ENDPOINT [--validator ENDPOINT] --policy FILE [--nonce HEX] [--allow-simulated] [--format text|json|markdown]
tundravm sbom    [--build DIR] [--variant NAME] [--lockfile PATH] [--format spdx-json|cyclonedx-json|text|markdown] [--output FILE] [--allow-unbound]
tundravm evidence [RECIPE] [--variant NAME]... [--build DIR] [--lockfile PATH] [--policy FILE] [--bundle FILE] [--html FILE] [--format text|json] [--require LIST] [--allow-unbound] [--sign KEY]
tundravm verify  EVIDENCE [--build DIR] [--require LIST] [--allowed-signers FILE] [--signer PRINCIPAL] [--format text|json] [--html FILE]
tundravm changes [RECIPE] --since EVIDENCE [--variant NAME]... [--build DIR] [--lockfile PATH] [--format text|json|markdown]
tundravm release [RECIPE] --version VERSION [--variant NAME]... [--sign KEY] [--require LIST] [--since PREVIOUS] [--releases DIR]
                 [--build DIR] [--lockfile PATH] [--backend KIND] [--resume | --restart] [--format text|json]
tundravm doctor  [RECIPE] [--backend KIND]
tundravm ci      RECIPE [--variant NAME]... [--tree DIR] [--lockfile PATH] [--format auto|text|github]
tundravm watch   RECIPE [--variant NAME]... [--build DIR] [--tree DIR] [--lockfile PATH] [--interval SECONDS] [--write]
tundravm status  RECIPE [--variant NAME]... [--build DIR] [--tree DIR] [--lockfile PATH] [--verify] [--format text|json|markdown]
tundravm clean   [RECIPE] [--build DIR] [--what sources,tree,artifacts,evidence,state,reproduce|all] [--lockfile PATH] [--dry-run]
tundravm config  [RECIPE] [--build DIR] [--tree DIR] [--lockfile PATH] [--backend KIND] [--format text|json]
tundravm completion bash|zsh|fish
```

`RECIPE` may be omitted wherever it appears when a `pyproject.toml` with a `[tool.tundravm]` table is found (see [Project configuration](#project-configuration)). `TARGET` is `qemu`, `azure` or `gcp`.

Destinations are named the same everywhere: `--tree DIR` is the compiled mkosi tree (default: the table's `tree`, else `build/mkosi`); `--build DIR` is the build directory, which holds `bake-result.json`, the sources in `BUILD/.sources`, the artifacts, the bake's own tree `BUILD/mkosi` and the second build `BUILD/.reproduce` (default: the table's `build`, else `build`); `--output FILE` is a document written. The named document flags stay as they are: `measure --export-policy FILE`, `evidence --bundle FILE` and `--html FILE`, and `attest --policy FILE`, which is read. `clean --what tree` names the `BUILD/mkosi` part, never the committed tree.

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
usage: tundravm inspect [-h] [--attr ATTR] [--pythonpath DIR] [--traceback]
                        [--variant NAME] [--why SUBJECT]
                        [--format {text,json,markdown}] [--lockfile LOCKFILE]
                        [--diff-variants A B]
                        [recipe]
tundravm inspect: error: unrecognized arguments: --fromat json (did you mean --format?)
[exit 2]
```

## Commands

| Command | Does | Exit 1 when |
|---|---|---|
| `init` | Scaffolds a project from a starter template, lints it, prints the next steps and probes the chosen backend (see [Init](#init)). | |
| `inspect` | Dry run per variant: parent, fragments, packages, files with digests, users, units, hooks, runtime-init steps, sources, targets (see [Inspect](#inspect)). `--format json` adds the recipe `digest` the lockfile records. `--diff-variants A B` lists what differs between two variants instead. `--variant NAME --why SUBJECT` explains one emitted object (see [Explaining one object](#explaining-one-object)). | |
| `lint` | Every diagnostic: resolution, fragment checks, compiler rules. The compiler rules see the source pins of `--lockfile` (default `build/tundravm.lock` when it exists), so a pinned source never reports `source-unpinned`. A selected lockfile, `--lockfile` or the table's `lockfile`, also adds its drift diagnostics (`lock-*`), as `lint(recipe, lock=...)` does. | an error (with `--strict`, a warning) |
| `compile` | Writes the mkosi tree to `--tree` (default: the table's `tree`, else `build/mkosi`), one directory per variant, then prints `variants:`, `recipe_digest:` (the digest `lock` records) and `tree_digest:` (`Tree.digest`). `--check` writes nothing and reports the stale files. | `--check` and the tree is stale |
| `diff` | Unified diff from the tree at `--tree` (default: the table's `tree`, else `build/mkosi`; `--tree BUILD/.reproduce/mkosi` is the second build of `bake --verify-reproducible`) to what the recipe compiles to. `--stat` lists changed files. | the trees differ |
| `lock` | Writes the lockfile to `--lockfile` (default `build/tundravm.lock`) for the selected variants: version 4, with the `distribution`, `compiler`, `variants.<name>.kernel` and `variants.<name>.debloat` sections. A build whose source differs between variants is pinned per variant as `<variant>/<name>` (drift line `sources.<variant>.<name>`); `--update <variant>/<name>` re-resolves one variant's pin, `--update <name>` every variant's. `--check` prints the drift instead (see [Lockfile drift](#lockfile-drift)); a version 3 lockfile drifts as `~ version: 3 -> 4` until locked again. | `--check` and the lock is stale |
| `fetch` | Checks the source builds, built kernels' sources and `EfiStub` package (`efi-stub`) of the selected variants out on this host, as the invoking user, into `BUILD/.sources/<name>-<pin12>-<id8>` (`--build`, default `build`; `id8` hashes the url, subdirectory and submodules) at the pins of `--lockfile` (default `build/tundravm.lock` when it exists; unpinned sources are resolved first). Complete checkouts are verified and kept: git `HEAD` is the pin, `git status` is empty (untracked and ignored files included) and requested submodules are initialised; an http checkout matches the sha256 manifest written at fetch time. A modified one fails with `E_SOURCE` (`source <name> checkout modified/incomplete: run tundravm fetch --force`); `--force` checks every source out again and refills the dependency caches. Outside `nethermind-v1`, `bake` runs it first and mounts `BUILD/.sources` into the build; `bake --no-fetch` builds from the checkouts already there and fails with `E_STATE` when one is missing. It also prefetches each `Go`, `Cargo` and `Dotnet` build's dependencies with the host's toolchain into `BUILD/.sources/deps/{go,cargo,nuget}` and lists the outcome in a `deps:` column: `go: prefetched`, `go: kept`, `go: prefetched (was incomplete: ...)`, `go: incomplete (...)`, `skipped (no host go)`, `skipped (offline)` or `failed (...)` (see [Fetch](#fetch)). | |
| `bake` | Compiles and builds every selected variant into `--build` (default `build`), then writes `BUILD/bake-result.json`. `--verify-reproducible` builds them a second time into `BUILD/.reproduce` and fails with `E_REPRODUCIBILITY` (exit 1) unless every artifact's sha256 matches (see [Bake](#bake)). | |
| `measure` | Expected measurements of one variant baked into `--build` (default `build`), as text, JSON or Markdown (`--format`). `--export-policy FILE` also writes the verifier policy `Tdxs.from_policy` reads (see [Measure and deploy](#measure-and-deploy)). | |
| `deploy` | Deploys the artifact of one variant baked into `--build` (default `build`) to `TARGET`: `qemu`, `azure` or `gcp`. | |
| `attest` | Asks a running image's `tdxs` issuer (`--issuer`) for a quote bound to a nonce, has a `tdxs` validator (`--validator`) judge its signature and collateral, and checks its MRTD and RTMR0..RTMR3 against a verifier policy (`--policy`, from `measure --export-policy`): the signature status, the nonce check, one `match`/`mismatch`/`unchecked` line per register, then the verdict. `trusted` needs the validator; without one the verdict is `measurements-match` or `measurements-mismatch` (see [Attest](#attest)). | the verdict is `untrusted`, `measurements-mismatch` or `simulated` (without `--allow-simulated`) |
| `sbom` | The software bill of materials of one baked variant (`--build DIR`, default `build`; `--variant`): mkosi's package manifest beside the artifact, the lockfile's source pins and the recipe metadata, merged into SPDX 2.3 JSON (default), CycloneDX 1.5 JSON, text or Markdown (`--format`); `--output FILE` writes it to a file (see [SBOM](#sbom)). | |
| `evidence` | An auditor's record of one bake (`--build DIR`, default `build`; `--variant`, default every baked variant): recipe and tree digests, the lockfile with its drift verdict, each artifact with a fresh sha256 check, the reproducibility verdict, the measurements policy, an SPDX SBOM, the lint summary, a provenance summary and the tool versions, written to `BUILD/evidence/` with an `evidence.json` index; `--bundle FILE.tar.gz` and `--html FILE` package it; `--require LIST` makes missing checks count (see [Evidence](#evidence)). | the verdict is `fail` or `incomplete` |
| `verify` | Checks an evidence bundle or directory on its own, with no recipe, checkout or bake directory: every member against the sha256 `evidence.json` lists, the index against its members, the artifacts in `--build DIR` re-hashed against the index, `evidence.json.sig` against `--allowed-signers FILE`, and the `--require` checks; one `pass`, `fail` or `missing` per check with a reason code (see [Verify](#verify)). | a check fails or is missing |
| `changes` | What changed since a previous evidence bundle (`--since EVIDENCE`): the recipe and per-variant tree digests, the lockfile sections, the source pins, the packages, the emitted files (each with the declarations behind it) and the measured registers, grouped by section with counts; `--format markdown` is meant for a pull request (see [Changes](#changes)). | |
| `explore` | Writes one self-contained HTML page that explains the recipe to someone who has not read its Python (`--build DIR`, default `build`; the page goes to `BUILD/explore.html` or `--output FILE`, and its path is printed): the settings, lint findings, lockfile state and the bake in `--build`, a declarations × variants matrix with provenance, each variant's compiled tree with contents and the declarations behind every file, and the hooks, runtime-init steps and source builds in run order, cross-linked; `--format json` prints the same model (see [Explore](#explore)). | |
| `release` | Cuts `RELEASES/<recipe>-<VERSION>/` from the locked recipe in nine resumable stages: lock check, fetch, a bake checked by a second build, measure, SBOMs, evidence (`--sign KEY`), verify, changes since the previous release, and `RELEASE.md`; a finished release is immutable (see [Release](#release)). | the evidence, its verification or a `--require` check fails |
| `doctor` | Python and tundravm versions, the backend's host tools (for `local` also the optional `ukify`, `systemd-repart`, `apt` and `pefile`, probed under the Python that runs mkosi, and with a `RECIPE` that has `azure` or `gcp` variants the optional `qemu-img` or `sgdisk` their disk conversion needs on the host; see [Local backend](#local-backend)), the optional measurement tools; with `RECIPE`, its lint summary. | a required tool is missing |
| `ci` | `lint --strict`, `compile --check` (against `--tree`) and `lock --check` in order; stops at the first failure. The one `--lockfile` (default `build/tundravm.lock`) is loaded once and shared: lint and the tree check apply its pins, the lock check compares against it. | any step fails, including a missing lockfile |
| `watch` | Polls the recipe file and every `.py` under its directory; on each change re-runs lint and the tree check against `--tree` (default: the table's `tree`, else `BUILD/mkosi`, with `--build` default `build`), or recompiles it with `--write`, printing one line (see [Watch](#watch)). | never (Ctrl-C exits 0) |
| `status` | Read-only, network-free report of where the project stands, one line per item with a verdict, then the single most useful next command (see [Project status](#project-status)). | never (exit 0) |
| `clean` | Removes the parts `--what LIST` names of the build directory (`--build`, default: the table's `build`, else `build`): `sources`, `tree`, `artifacts`, `evidence`, `state`, `reproduce`, or `all`; the lockfile only when `--lockfile` names it. Without `--what` it lists what `--what all` would remove (see [Project status](#project-status)). | a path could not be removed |
| `config` | Prints the `recipe`, `tree`, `build`, `lockfile` and `backend` the commands resolve and where each came from: `flag`, `pyproject`, `default` or, for `backend`, `recipe` (the kind of the recipe file's `backend`); a recipe or lockfile that does not exist is marked (see [Project configuration](#project-configuration)). | |
| `import` | Generates a recipe module from an existing mkosi tree (`TREE`: one variant directory or a directory of them): `--output FILE` writes it (`--force` overwrites), else it goes to stdout; `--name`, `--dialect`, `--format text\|json`. Reports the declared/verbatim coverage, notes for what it skipped or could not map, and whether the recipe compiles back to `TREE` (see [Import](#import)). | |
| `completion` | Prints a bash, zsh or fish completion script for this version's verbs, flags and flag choices (see [Shell completion](#shell-completion)). | |

`inspect`, `lint`, `compile`, `diff`, `fetch` and `bake` read `build/tundravm.lock` when it exists and apply its source pins; `--lockfile PATH`, or the `lockfile` of a [`[tool.tundravm]` table](#project-configuration), names another one. `ci` needs the lockfile and loads it once for all three steps; `status` reports on it.

## Project configuration

`tundravm init` writes a `[tool.tundravm]` table into the `pyproject.toml` it creates (an existing `pyproject.toml` is kept; `init` prints the table to add):

```toml
[tool.tundravm]
recipe = "node.py"
tree = "mkosi"
build = "build"
lockfile = "build/tundravm.lock"
backend = "inprocess"
```

Every command reads the table, and every command that takes `RECIPE` then runs without it: `tundravm status`, `tundravm lint`, `tundravm compile --check`; `measure`, `deploy` and `sbom`, which take no `RECIPE`, find the bake in the configured `build`. In a project with `build = "dist"`, bare `tundravm sbom`, `measure`, `status`, `evidence` and `clean` all use `dist/`. The rules:

- The table is read from the nearest `pyproject.toml` that has one, starting in the working directory and walking up. Paths in it are relative to that file. Unknown keys and non-string values are `E_VALIDATION`. The keys are `recipe`, `tree`, `build`, `lockfile` and `backend`; a table that still has `out`, the old name of `build`, fails with `E_VALIDATION` (`[tool.tundravm] key(s) out in PATH were renamed.`, hint `Rename out to build (e.g. build = "build").`).
- Precedence per value: an explicit argument or flag, then the table, then the built-in default (`build/mkosi`, `build`, `build/tundravm.lock`, the recipe file's `backend`). For `measure`, `deploy` and `sbom` the default build directory is `build` beside the `pyproject.toml` when a table is found without a `build`, else `./build`.
- For the commands that take `RECIPE`, the table's paths apply when `RECIPE` is omitted or names the table's recipe; another explicit `RECIPE` uses the built-in defaults.
- `tree` is `--tree` of `compile`, `diff`, `ci`, `status` and `watch` (the tree they write, compare or check); `build` is `--build` of `fetch`, `bake`, `measure`, `deploy`, `sbom`, `evidence`, `release`, `status`, `watch` and `clean`; `lockfile` is `--lockfile` everywhere except `clean`, drift diagnostics of `lint` included; `backend` is `bake --backend`, `release --backend` and `doctor --backend`, and `bake` and `release` keep the recipe file's own backend instance when it is of that kind.
- A configured `lockfile` stays selected before the file exists and is never replaced by `build/tundravm.lock`. Until `tundravm lock` writes it, `inspect`, `lint`, `compile`, `diff` and `fetch` print `` note: configured lockfile PATH does not exist; run `tundravm lock` to create it `` on stderr and run without pins, `status` reports `configured lockfile PATH does not exist; run tundravm lock` on its `lock` line, and `bake` fails with `E_LOCKFILE` instead of baking unfrozen.
- Without `RECIPE` and without a table, a recipe command exits 2 with `the following arguments are required: recipe (no pyproject.toml with a [tool.tundravm] table ...)`; `doctor`, `clean` and `config` keep working without one.

`tundravm config` prints each value as the commands resolve it: the recipe path, the `tree`, `build` and `lockfile` paths, whether the recipe and the lockfile exist and, without `--backend` or a table `backend`, the kind of the backend the recipe file binds (origin `recipe`; `config` imports the recipe file to find it, with `--attr` and `--pythonpath` as the other commands take them). `--format json` prints `{"schema": "tundravm.config/1", "pyproject", "values": {KEY: {"value", "origin"}}}`, plus `exists` for `recipe` and `lockfile`. Its `--tree`, `--build`, `--lockfile` and `--backend` show how flags override the table. In a project `init` just wrote:

```console
$ tundravm config
pyproject: /home/me/node/pyproject.toml
  recipe    node.py              pyproject
  tree      mkosi                pyproject
  build     build                pyproject
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
| `--ci github\|none` | `github` also writes `.github/workflows/tundravm.yml` (default `none`); on an existing project, only the workflow (see below). |
| `--recipe RECIPE` | With `--ci github`: the existing project's recipe, relative to `DIR`; only the workflow is written. |
| `--with-tests` / `--no-tests` | Write `tests/test_NAME.py` (default) or skip it. |
| `--force` | Overwrite the recipe, tests module and workflow if they exist; on an existing project, the workflow only. |
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
- `pyproject.toml` and `README.md`, only when absent; an existing one is kept, even with `--force`. The generated `pyproject.toml` depends on tundravm from git (see [Installing tundravm in the project](#installing-tundravm-in-the-project)), puts `pytest` in the `dev` group and sets `[tool.pytest.ini_options] pythonpath = ["."]`, so tests you add can import the recipe and its sibling modules (`from node import App`);
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
note: svc/pyproject.toml already exists; add the dependencies with `uv add "tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"` and `uv add --dev pytest`
note: add this table to it so commands run without RECIPE:
[tool.tundravm]
recipe = "my-node.py"
tree = "mkosi"
build = "build"
lockfile = "build/tundravm.lock"
backend = "inprocess"
lint my-node.py: no findings
next (from svc):
  1. tundravm lock my-node.py                  record recipe sections and pin source repositories/downloads in build/tundravm.lock
  2. tundravm compile my-node.py --tree mkosi  write the mkosi tree; commit it
  3. uv run pytest tests                       run test_my_node.py against mkosi/
  4. tundravm ci my-node.py --tree mkosi       the lint, tree and lockfile checks CI runs
  5. tundravm bake my-node.py --build build    build the image
```

To add CI to a project that already exists, run `tundravm init --ci github` in it. The project exists when `DIR/pyproject.toml` has a `[tool.tundravm]` table (its `recipe` names the recipe, its `tree` the `--tree` the workflow checks, default `mkosi`) or `--recipe RECIPE` names the recipe. Then `init` writes only `.github/workflows/tundravm.yml` and the `.gitignore` block when it is missing; the recipe, tests, `pyproject.toml` and `README.md` are never touched, and nothing is linted or probed. An existing workflow is refused with `E_VALIDATION`; `--force` overwrites the workflow only:

```console
$ tundravm init --ci github
adding CI for node.py to the existing project in .
created .github/workflows/tundravm.yml
kept .gitignore (already ignores build output)
next: `tundravm ci node.py --tree mkosi` runs the workflow's checks; commit mkosi/ and the lockfile they compare against
```

The `note:` lines appear only when `pyproject.toml` already existed: the first names the `uv add` that pins the tundravm commit `init` ran from (`COMMIT` above), the second the `[tool.tundravm]` table when the file has none; add `[tool.pytest.ini_options] pythonpath = ["."]` yourself if your tests import project modules. When `init` writes `pyproject.toml`, the `next:` list ends with `` `uv sync` installs the pinned dependency, tundravm at 258d089fdc10`` instead; when it cannot tell which commit is running, the dependency follows `master`, that line says `tundravm at master`, and a `note: could not tell which tundravm commit is running, so the project follows master; pin one with ...` precedes the lint summary. A missing backend tool never fails `init`; the probe ends with a pointer to the in-process backend:

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

### Installing tundravm in the project

tundravm is not on PyPI, so the generated `pyproject.toml` depends on it from git, pinned to the commit of the tundravm that ran `init`:

```toml
# tundravm is not on PyPI: this pins the commit `tundravm init` ran from.
# Move it with `uv add "tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"`; commit uv.lock.
dependencies = ["tundravm @ git+https://github.com/Hyodar/tundra@258d089fdc105803ce0080a40aaa35138e5f2cb6"]
```

`init` reads the commit from its own install metadata (`direct_url.json`): a git install (`uvx --from git+https://github.com/Hyodar/tundra tundravm init`, `uv add`, `pip install git+...`) records the commit and repository it came from. An editable install of a tundravm checkout pins the checkout's `HEAD` when a remote-tracking branch contains it (so a fresh machine can fetch it), with that remote's URL in `https://` form. Otherwise (a wheel or sdist, a commit no remote has) the dependency follows `master`, and the comment above it says so; pin a commit with `uv add "tundravm @ git+https://github.com/Hyodar/tundra@COMMIT"`. The project has no `[build-system]`, so uv installs its dependencies without building it and needs no `[tool.uv]` settings; `git` must be on `PATH` to fetch the dependency.

On a fresh machine or a CI runner, `uv sync` installs the pinned tundravm and `pytest`; `uv run tundravm ...` and `uv run pytest tests` run them. Commit the `uv.lock` that `uv sync` (or `uv lock`) writes: from then on the [generated workflow](#ci) installs exactly what it locks with `uv sync --frozen`. To work against an unreleased local checkout instead, `uv add --editable PATH/TO/tundravm` (the directory holding tundravm's `pyproject.toml`) replaces the git dependency.

## Import

`tundravm import TREE` reads an mkosi tree and writes a recipe module that compiles back to it. `TREE` is one variant directory (it holds `mkosi.conf`) or a directory of them as `compile` writes them. The first directory (`default` when there is one) becomes `Fragment("common", ...)` and its `Variant`; every other directory becomes a `Variant` that adds, replaces or removes only what differs (a whole `Fragment` with `parent=None` when an overlay cannot reproduce its order).

| Flag | Effect |
|---|---|
| `--output FILE` | Write the module to `FILE` (ruff formats it with `FILE`'s configuration); without it the module goes to stdout and the report to stderr. An existing `FILE` is refused unless `--force`. |
| `--name NAME` | Recipe name (default: the tree's directory name, or its parent's for `mkosi/`). |
| `--dialect current\|nethermind-v1` | Dialect `TREE` was written with (default: guessed; `nethermind-v1` when the tree spells something only that dialect writes, with a note). |
| `--format text\|json` | `text` prints `coverage: N declared, M verbatim, L lost`, then the notes grouped by reason (`unmapped file`, `lost declaration`, `skipped`: symlinks, empty directories, build state; `other`), one `note:` each; `json` prints the `Imported` fields (`recipe_source`, `recipe` name and variants, `notes`, `coverage`) with `"schema": "tundravm.import/1"`. |

What maps back:

- `mkosi.conf` and `mkosi.conf.d/*.conf`: `Distribution`/`Release`, `Architecture`, `Mirror`, `ToolsTreeMirror`, `Snapshot`, `SourceDateEpoch` become `Recipe(base=, arch=, mirror=, tools_mirror=, snapshot=, epoch=)`; `Packages=` and `BuildPackages=` become `Package(...)` (`role="build"`); the kernel comment and build script a `Kernel`; every other key a `Setting(section, key, values)`. Keys the compiler writes itself (`ImageId`, `ExtraTrees`, `ManifestFormat=json`, the script keys, ...) are dropped.
- `mkosi.extra/**` and `mkosi.skeleton/**`: a unit under `usr/lib/systemd/system` whose content is exactly what `Service(...)` renders becomes that `Service`, any other unit a `Unit(name, content)`; text files become `File(path, content, mode=)` (`stage="skeleton"` for the skeleton). Binary files and files over 64 KiB become `File(path, TREE / ...)`, read from the tree at compile time, with a note. Symlinks and empty directories are noted and not imported. The group write bit a `002` umask leaves is dropped from modes.
- The compiler's own lines become declarations again: the synthetic postinst (`User`, `Group`, unit enablement, masking and disabling, `Debloat`'s systemd minimisation), the finalize debloat block, `runtime-init` (`Init`), the cloud glue (`target="azure"`/`"gcp"`), backports (`Backports`) and the EFI stub (`EfiStub`, unpinned or locked; a locked one is read with the recipe's `Snapshot=`).
- Source build hooks in the build script become `Build(...)` again (see the guarantees below); the generated lines after a recognized hook are not imported.
- `scripts/*` and `mkosi.sync`, `mkosi.prepare`, `mkosi.build`, `mkosi.postinst`, `mkosi.finalize`, `mkosi.postoutput`, `mkosi.clean`: what is left becomes `Hook(name, phase, script)`, verbatim, without the `#!/usr/bin/env bash` / `set -euo pipefail` preamble the compiler adds (or the script's own shebang line), so importing tundravm's output does not nest preambles.
- `mkosi.sandbox`, `mkosi.cache`, `mkosi.tools`, `mkosi.builddir`, `mkosi.output` and `.sources` files are skipped with a note; anything else unread is noted `not mapped`.

Source builds, per dialect:

- `current`: every source build hook starts with `# tundravm build: <json>`, its declaration without a pin (`name`, `source`, `build`, `install`, `cache_key`). The import reads the `Build` back exactly as declared: `Git`/`Http` source, `recipe=Go(...)`/`Cargo(...)`/`Dotnet(...)` or `script=` with its `packages` and `env`, `install=`, `cache_key=`. The builds `Key`, `Disk` and `Secrets` generate (`key-generation`, `disk-encryption`, `secret-delivery`) come back as `Build(..., recipe=Go(...), install=(Install("build/key-gen", "/usr/bin/key-gen", artifact=True),))`, not as the declarations that generated them (their configs and `runtime-init` steps come back as `File` and `Init`). Unlocked, the import compiles back to the tree. Locked (the hooks copy pinned checkouts), the module carries the refs, not the pins: `tundravm lock` pins it again (the same pins when the refs still resolve to them) and it then compiles back to the tree; the round-trip note compares with the pinned hooks swapped for their unpinned form and names the pins (`round trip: the recipe compiles back to this tree once locked to the same pins (key-generation@a91272e9ebde, ...)`).
- `nethermind-v1`: hooks carry no marker. A hook whose URL, ref (or full commit when it was locked), cache key, install steps and build command parse into a `Build(..., script=...)` that renders the same line comes back as that `Build`; anything else stays in a `Hook`. A `lost declaration` note per build says what it cannot carry: the recipe kind (a `Go`, `Cargo` or `Dotnet` recipe comes back as its command, `script=`) and, for a locked hook, the ref it was pinned from (`Git(url, <commit>)` compiles to the same line).

`coverage` counts `declared` declarations, `verbatim` ones (`File`, `Unit` with content, `Hook`, `Setting`) and `lost`, the `lost declaration` notes.

The last note is the round trip: the module is executed and compiled, and either `round trip: the recipe compiles back to this tree` or the paths that compile differently (a foreign tree's `mkosi.conf` and `mkosi.postinst` always do, since the compiler writes its own layout: `scripts/06-postinst.sh` and its default keys). Every tree tundravm writes, including `examples/surge-tdx-prover/mkosi` under `nethermind-v1`, imports to a recipe whose `tundravm diff` against the tree is empty.

```console
$ tundravm init --name node --template service --backend local --no-doctor .
$ tundravm compile node.py --tree mkosi
...
$ tundravm import mkosi --name node --output node_imported.py
wrote node_imported.py
coverage: 24 declared, 3 verbatim, 0 lost
other (1):
  note: round trip: the recipe compiles back to this tree
$ tundravm diff node_imported.py --tree mkosi
tree is up to date with the recipe
```

The generated `node_imported.py` binds `recipe`:

```python
recipe = Recipe(
    "node",
    Fragment(
        "common",
        items=(
            Package("ca-certificates"),
            ...
            User("app", home="/var/lib/app"),
            File("/etc/app/app.conf", APP_CONF),
            Service(
                "app",
                "/usr/bin/app --config /etc/app/app.conf",
                description="app 0.1.0",
                user="app",
                working_dir="/var/lib/app",
                restart="on-failure",
            ),
            Init("runtime-init", "mkdir -p /var/lib/app && chown app:app /var/lib/app"),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant(
            "dev",
            add=Fragment(
                "dev",
                items=(
                    Package("apt"),
                    ...
                    Unit("serial-console.service", content=SERIAL_CONSOLE_SERVICE),
                    Hook("postinst", "postinst", POSTINST_SCRIPT),
                ),
            ),
            target="qemu",
        ),
    ),
)
```

What the template expressed as an `App` composite and a `DevTools` fragment comes back as the declarations they expand to: the import recovers what the tree holds, not how the recipe was factored.

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

`lint`, `diff`, `compile --check`, `lock --check` and `ci` take `--format`. Precedence: an explicit `--format`, then the shorthand flag (`diff --stat`; not combinable with `--format`), then `auto`, the default, which picks `github` when the `GITHUB_ACTIONS` environment variable is `true` and `text` otherwise.

| Format | Shape |
|---|---|
| `text` | One line per finding (`error disk-key-undefined [default] data: ...`; `[common]` for an empty location, as in `github`) and a summary line |
| `json` | `{"schema": "tundravm.lint/1", "diagnostics": [...], "summary": {...}}` (`lint`; each diagnostic has `code`, `severity`, `message`, `subject`, `location`, `hint` and `context`: `location` is the variant, `""` for the recipe as a whole, `hint` is `null` when there is none, `context` an object of strings); `{"schema": "tundravm.inspect/1", "digest": ..., "variants": {NAME: {...}}}` (`inspect`) |
| `github` | `::error file=...,title=...::message` workflow commands that annotate the pull request |
| `markdown` | Tables, ready for `$GITHUB_STEP_SUMMARY` or a PR comment (`lint`: `\| Severity \| Code \| Location \| Subject \| Message \| Hint \|`) |
| `stat` | Changed files only (`diff`) |

`inspect` takes `text`, `json` or `markdown` (no `auto`).

Every `--format json` report carries a top-level `"schema": "tundravm.<verb>/1"` key that names its shape and version: `inspect` (also `--why` and `--diff-variants`), `lint`, `status`, `config`, `attest`, `evidence` (`evidence.json` verbatim, which carries the key next to its `schema_version`), `import` and `measure`. `sbom` writes its native SPDX or CycloneDX document unchanged.

When an SDK error (an `E_*` code) ends a command run with `--format json`, stderr gets exactly one JSON line instead of the `error [E_...]: ...` block, and the exit code is 2:

```
{"schema":"tundravm.error/1","code":"E_VALIDATION","message":"Unknown variant(s): nope.","hint":"Declared variants: default","context":{"recipe":"image.py"}}
```

`hint` is `null` when there is none.

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

`inspect RECIPE --diff-variants A B` resolves both variants and lists the declarations `B` adds, removes or changes relative to `A`, matched by identity, plus the target when it differs. It takes `--format text|json|markdown` and cannot be combined with `--variant` (`E_VALIDATION`). For a recipe whose `azure` variant extends `default` with `add=Fragment("azure", items=(Package("walinuxagent"),))`, `replace=(File("/etc/motd", "node on azure\n"),)`, `remove=(Package("curl"),)` and `target="azure"`:

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

`--format json` prints `{"schema": "tundravm.inspect/1", "a", "b", "targets": {A: [...], B: [...]}, "added": [...], "removed": [...], "changed": [{"declaration", "fields"}]}`. A `~` line names the fields that differ.

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

`--format json` prints `{"schema": "tundravm.inspect/1", "variant", "subject", "declarations": [{"declaration", "type", "key", "present", "origins": [{"action", "variant", "fragments": [{"name", "class"}]}]}], "fragments", "files", "generated"}`; `--format markdown` a table of declarations and steps. A removed declaration is listed with `present: false` and no files. The Python form is `tundravm.explain_why(recipe, variant, subject)`.

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

`tundravm fetch RECIPE` checks out, on this host and as you, every source the selected variants build from: each `Build` source and, outside `nethermind-v1`, each built kernel's source (a `Kernel` with `config`), named `kernel` or `kernel-<variant>` where a variant's kernel source differs, and the `.deb` an `EfiStub` installs, an http source named `efi-stub` (`efi-stub-<variant>` where a variant's package differs). A git source is checked out at its pinned commit, an http source is downloaded and checked against its sha256. Each lands in `BUILD/.sources/<name>-<pin12>-<id8>/` (`--build DIR`, default `build`), `id8` being the first 8 hex of a sha256 over the url, kind and, for git, subdirectory and submodules, with a JSON `.tundravm-complete` marker that records the pin and that identity (and, for http, a sha256 manifest of every file). There is one checkout per (source, pin). A complete checkout is verified and kept, so a second run touches nothing: git `HEAD` must be the pin, `git status` must list nothing (untracked and ignored files included) and requested submodules must be at their recorded commits; an http checkout must match its manifest. A modified checkout fails with `E_SOURCE` until `fetch --force` checks every source out again:

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

For each `Go`, `Cargo` and `Dotnet` build, `fetch` then prefetches the dependencies with the host's toolchain, as you, in a scratch copy of the checkout: `go mod download` into `BUILD/.sources/deps/go`, `cargo fetch --locked` into `BUILD/.sources/deps/cargo`, `dotnet restore --runtime RID --packages` into `BUILD/.sources/deps/nuget`. A marker, `deps/<name>-<pin12>-<id8>.<cache>.json`, records the command and environment and a content list of what the build needs from the cache: each module directory and `.mod` file `go.sum` names, each `.crate` of `Cargo.lock` (size and sha256), each NuGet package directory of the restore's `project.assets.json`. The next fetch keeps the cache only while the content list matches; otherwise it prefetches again. `fetch --force` removes each dependency cache and its markers and prefetches it again. The result lists the outcome in a `deps:` column, and each build gets a `note` line (a `warning` for a failure). For a recipe with a `Go` build `app` and a `Cargo` build `app-rs` of the same repository, on a host with `cargo` but no `go`:

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
next: tundravm deploy qemu --build build --variant default
```

- Progress goes to stderr as `[variant] step ... ok (1.2s)` lines (a live timer on a terminal), ending `baked N variants`; the summary table (one row per variant and target) and the `next:` line go to stdout. `next:` leaves out `--build DIR` when the build directory came from `[tool.tundravm]`.
- `-v`/`--verbose` echoes every backend output line. `-q`/`--quiet` prints only the summary and errors. `--json-logs` writes progress events to stdout as JSON lines (`kind`, `message`, `variant`, `elapsed_s`, `extra`) and no summary.
- Backend output is hidden by default, but its last lines are shown when the build fails.
- Outside `nethermind-v1`, a bake with a real backend first runs `fetch` (a `fetch` progress step) at the pins it builds, then mounts `BUILD/.sources` into the build: the mkosi command gets `--build-sources=BUILD/.sources:tundravm-sources` (absolute paths) (so `$SRCDIR/tundravm-sources` in the scripts), the config directory again as the first `--build-sources` (unless the recipe sets `BuildSources`), and `--build-sources-ephemeral=yes` (unless the recipe sets `BuildSourcesEphemeral`), so nothing a script writes there reaches the host. The build sandbox never fetches a source. The in-process backend fetches nothing.
- `--no-fetch` skips the fetch and builds from the checkouts already in `BUILD/.sources`, for example ones copied to an air-gapped host. A missing or incomplete checkout fails after `compile`, before mkosi runs:

  ```console
  $ tundravm bake node.py --backend local --no-fetch
  ...
  [default] failed after 0.0s
  error [E_STATE]: Source build 'hello' is not fetched: build/.sources/hello-cdd0a5257ba9-2c640da8 is missing or incomplete.
  Hint: Run `tundravm fetch RECIPE --build build` first, or bake without --no-fetch.
    pin: cdd0a5257ba950088a826755fa832f40e87fc5c3
  [exit 2]
  ```

- `--offline` (or `Policy(network_mode="offline")` in the recipe) gives the build sandbox no network: the mkosi command gets `--with-network=no`, so build and postinst scripts cannot download anything (mkosi still installs the distribution packages from the configured mirror). The fetch step only reuses complete checkouts (a missing one fails it with `E_POLICY`, `Cannot fetch 'NAME': policy network_mode is offline.`), and every `Go`, `Cargo` and `Dotnet` build must find the dependency cache `tundravm fetch` prefetched into `BUILD/.sources/deps`; a missing one fails before mkosi runs with `E_STATE` naming the build and `tundravm fetch`, and an incomplete one with `E_STATE` naming the missing entry (`Source build 'app' has an incomplete go dependency cache: deps/go/<entry> is missing or changed, ...`; the hint adds that `--force` refills every cache). A `nethermind-v1` recipe with source builds fails with `E_POLICY`: its hooks fetch in the sandbox. For the `app`/`app-rs` recipe of [Dependencies](#dependencies), fetched on a host without `go`:

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
  Hint: Run `tundravm fetch RECIPE --build build` on a host with `go` on PATH (it fills build/.sources/deps/go), or bake without --offline.
    marker: build/.sources/deps/app-cd6fb81f6fb2-902554f9.go.json
  ```
- The bake is frozen when `--lockfile` is given, when the `[tool.tundravm]` table configures a `lockfile` (which must then exist: a missing one fails before `lint` with `E_LOCKFILE`, `Configured lockfile PATH does not exist.`, and a hint to run `tundravm lock`), or when `build/tundravm.lock` exists: a recipe that drifted from the lock fails at `verify lockfile` with `E_LOCKFILE`. Without a lockfile it bakes unpinned and prints a note.
- The lockfile is copied to `BUILD/tundravm.lock` when that file is absent or identical. A different `BUILD/tundravm.lock` is never overwritten: `bake --build BUILD --lockfile PATH` bakes against `PATH` and leaves it alone.
- The frozen check compares like `lock --check` with the same `--variant` selection: a lockfile of every variant covers `bake --variant default`, while a lockfile written for fewer variants than the bake selects fails at `verify lockfile`.
- A recipe with error-level lint findings fails at `lint` with `E_LINT`.
- `BUILD/bake-result.json` records, per variant and target, the artifact path and sha256 and the sha256 of mkosi's package manifest beside it (`manifest_digests`, `null` when there is none), plus the lockfile digest, `lock_sha256` (the sha256 of the lockfile the bake read, `null` when it read none; `sbom` and `evidence` refuse another) and a `declarative` block with the recipe digest, the tree digest, `simulated` (true for the in-process backend) and, for a frozen bake, `lockfile`: the path of the lockfile it baked against, as given (`--lockfile PATH` or the table's `lockfile`, else `build/tundravm.lock`).
- `--verify-reproducible` bakes the same selection a second time into `BUILD/.reproduce`, against the same lockfile and the same `BUILD/.sources` checkouts (hard-linked there, so nothing is fetched again; mkosi's state is not shared, so the second build compiles and builds everything, tools tree included), then compares every artifact's sha256. The first build's `bake-result.json` is complete (artifacts, digests, the `declarative` block) before the second build starts, so a failed or interrupted second build never loses it. Its `declarative.reproducibility` (absent when the check did not run) is rewritten atomically at each step as `{"state", "second_build", "differ", "error"}`: `running` while the second build runs, then `matched` (`second_build` is `null`: it was removed), `different` (`differ` lists the `variant/target` artifacts that differ, `second_build` is `.reproduce`, relative to `BUILD`), `error` (the second build failed; `error` holds the exception's first line) or `interrupted` (Ctrl-C). `status` shows it on each artifact line and `evidence` checks it. When all match the summary is followed by `reproducible: yes (N artifacts match a second build)`. A mismatch keeps the second build and fails with `E_REPRODUCIBILITY` and exit 1 (a completed comparison with a negative result; a second build that fails to run exits 2 with its own error), a table per artifact and a hint naming `tundravm diff` and the usual causes:

  ```console
  $ tundravm bake node.py --verify-reproducible -q
  error [E_REPRODUCIBILITY]: The bake is not reproducible: 1 of 2 artifact(s) differ between two builds.
    variant  target  first         second
    default  azure   0c5b4a2e9f13  0c5b4a2e9f13  match
    default  qemu    1fa043adea90  6d2e81c4b7a0  mismatch
  Hint: The second build is kept in build/.reproduce. Compare the compiled trees with `tundravm diff RECIPE --tree build/.reproduce/mkosi` and the images with diffoscope. Usual causes: timestamps (set SOURCE_DATE_EPOCH), build ids, and packages installed without a snapshot (Recipe(snapshot=...)).
    second_build: build/.reproduce
    differ: default/qemu
  [exit 2]
  ```

  With the Lima backend the second build runs in its own VM (the instance name hashes the tree path). `clean --what reproduce` removes a kept `BUILD/.reproduce`.
- After the build, `bake` reads the compiled tree for its digest. A path it cannot read (a root-owned leftover, say) is skipped with a `warning` line instead of failing the bake, and mkosi's state next to its config (`mkosi.tools`, `mkosi.cache`, `mkosi.builddir`, ...) is never tree content; `diff` and `compile --check` ignore it too.

### Local backend

`--backend local` runs the host's `mkosi` (v25+), by default under `sudo`, with absolute `--directory` and `--output-dir` paths. For the `minimal` template (`tundravm init --template minimal --backend local`, then `tundravm bake node.py`), mkosi 26 on Ubuntu 24.04 builds a 47.2 MiB UKI:

```
variant  target  artifact                          size      sha256        time
default  qemu    build/default/output/default.efi  47.2 MiB  b0be0e718b25  1m24s
next: tundravm deploy qemu --variant default
```

- A bootable variant's artifact is the UKI `BUILD/<variant>/output/<variant>.efi`, next to the other files mkosi writes there; `bake-result.json` records it with its sha256.
- mkosi's workspace, package cache and tools tree live in `BUILD/.mkosi/` (`workspace/`, `cache/`, `mkosi.tools`), never in the compiled tree. A recipe that sets `WorkspaceDirectory` or `CacheDirectory` keeps its own.
- Under `sudo`, what mkosi wrote (`BUILD/<variant>/output/`, `BUILD/.mkosi/`) is chowned back to you, so the build directory holds no root-owned files. If that fails, a `warning` names the `sudo chown -R UID:GID BUILD` to run.
- mkosi builds a UKI with the host's `ukify`, and reads the installed kernel with Python's `pefile` in any build that is not `Bootable=no` (directory builds included), unless it uses a tools tree. When the host lacks one of them (`pefile` is probed under the Python that runs mkosi) and the recipe sets no `ToolsTree`, the backend adds `--tools-tree=default` for that variant to the mkosi command (the compiled tree is unchanged) and says so on a `note` line, which `-q` hides:

  ```
  [default] note ukify not found on the host; building with mkosi's default tools tree (--tools-tree=default). Install systemd-ukify to use the host tools.
  ```

- The first bake builds the tools tree; a later bake into the same `BUILD` passes `--tools-tree=BUILD/.mkosi/mkosi.tools` (the note then says `reusing` and the path) instead of building it again. In the run above, a second bake took 32s against 1m24s for the first.
- An `azure` variant's postoutput script lays out a disk with `parted` and converts it to a VHD with `qemu-img`; a `gcp` variant's partitions a raw disk with `sgdisk` and packs it as a `tar.gz`. In a build with mkosi's default tools tree those scripts run inside it: the backend adds `--tools-tree-package=qemu-utils,gdisk,parted` and reuses a cached `BUILD/.mkosi/mkosi.tools` only when it already holds `qemu-img`, `sgdisk` and `parted`, so the host needs none of them. Only a build on host tools needs `qemu-img` or `sgdisk` on the host; `bake` checks for them before mkosi runs and fails with `E_BACKEND_EXECUTION` (`Variant 'azure' needs `qemu-img` on the host for its cloud disk image.`, hint `install qemu-utils, or bake --variant default`).
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

`measure` and `deploy` take no `RECIPE`: they read the `bake-result.json` in `--build DIR` (default: the `build` of `[tool.tundravm]`, else `build` beside its `pyproject.toml`, else `./build`) and pick the artifact of `--variant NAME`, which is required when the bake holds more than one variant. Both hash the artifact first and fail with `E_ARTIFACT_CHANGED` when it is unreadable or no longer matches the sha256 the bake recorded:

```console
$ tundravm measure --variant default --allow-placeholder
error [E_ARTIFACT_CHANGED]: Artifact build/default/disk.qcow2 changed since the bake.
Hint: bake-result.json records sha256 1fa043adea90; Bake the variant again to record the artifact it should be.
  variant: default
  target: qemu
  path: build/default/disk.qcow2
  recorded: 1fa043adea909e9ad008e0db46ed1c9935adf90aee9c49d20940d6316c5c2e79
  actual: e9f650ccebad2410588216a77dc68623887949de69b8165992133992d805053d
[exit 2]
```

- `measure --scheme rtmr` (default) runs `measured-boot` or `dstack-mr`. Without one, or for `azure`/`gcp`, it fails unless `--allow-placeholder`, which prints digest-derived values under a `PLACEHOLDER` banner on stderr. Simulated artifacts always need `--allow-placeholder`. `--format json` prints `{"schema": "tundravm.measure/1", "artifact", "artifact_digest", "scheme", "tool", "values", "variant"}`; `--format markdown` a heading, a `Source:` line and a `Register`/`Value` table.
- `measure --export-policy FILE` (rtmr only) also writes a verifier policy: `{"schema_version": 1, "scheme": "rtmr", "tool", "tool_version", "artifact": {"path", "sha256"}, "registers": {"RTMR0", "RTMR1", "RTMR2"}}` (`RTMR3` when measured). Missing `RTMR0`..`RTMR2` is `E_VALIDATION`; placeholder values are refused unless `--allow-placeholder`, and then carry a `"note"` that says they are placeholders. It is the dict `Measurements.to_policy()` returns, and `Tdxs.from_policy(FILE)` turns it into a validator's `expected_measurements`.
- `deploy TARGET` (`qemu`, `azure` or `gcp`) picks the artifact of that target. `--param` keys are the target's settings: qemu `memory`, `cpus`, `ssh_port`, `tdx`, `daemonize`, `forward` (`HOST:GUEST[,HOST:GUEST...]`); azure `storage_account` (required), `resource_group`, `location`, `vm_size`, `gallery`, `secure_boot`, `signed`; gcp `project` and `bucket` (required), `zone`, `machine_type`. `--attach` (qemu only, the same as `--param daemonize=false`) runs QEMU in the foreground with the serial console on the terminal: Ctrl-A X quits QEMU, Ctrl-A C toggles its monitor, and `deploy` prints its result once QEMU exits. Simulated artifacts are refused unless `--allow-simulated-artifact`.

### Attest

`tundravm attest --issuer ENDPOINT --validator ENDPOINT --policy FILE` checks what a running image measured and whether its quote is genuine. It sends the image's `tdxs` issuer an `issue` request with a nonce (`--nonce HEX`, default 32 random bytes), reads MRTD and RTMR0..RTMR3 from the TDX quote in the reply (DCAP quote v4 or v5), and compares them with the policy's `registers`: `match`, `mismatch` (both values printed), or `unchecked` for a register the policy does not hold (MRTD and RTMR3 unless it does). The quote's whole 64-byte `report_data` must be `SHA-256(nonce)` followed by 32 zero bytes, as the issuer binds it, so a replayed quote fails the `nonce` line. With `--validator`, the attestation document and the nonce go to a `tdxs` validator, which checks the quote's signature, certificate chain and collateral; its answer decides authenticity.

| Verdict | When | Exit |
|---|---|---|
| `trusted` | the validator accepted the document, the nonce matches and no register mismatches | 0 |
| `untrusted` | the validator rejected the document, or (with a validator) the nonce or a register mismatches | 1 |
| `measurements-match` | no `--validator` (`signature_status: unchecked`), the nonce matches and no register mismatches | 0 |
| `measurements-mismatch` | no `--validator`, and the nonce or a register mismatches | 1 |
| `simulated` | a `simulator` issuer whose nonce and registers match; never `trusted` | 1, or 0 with `--allow-simulated` |

An SDK error (unreachable issuer or validator, not a TDX quote, a bad or placeholder policy) exits 2. `reasons` lists why a verdict is not positive: `validator-rejected`, `nonce-mismatch`, `register-mismatch`, `simulated`.

```console
$ ssh -p 2222 -N -L ./tdxs.sock:/var/tdxs.sock root@localhost &
$ tundravm attest --issuer unix:./tdxs.sock --validator tcp://127.0.0.1:7001 --policy peer.json
attestation unix:./tdxs.sock (tdx, quote v4)
policy: peer.json
signature valid (tcp://127.0.0.1:7001)
nonce  match      000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
MRTD   unchecked  1111...1111
RTMR0  match      2020...2020
RTMR1  mismatch   7777...7777
       policy     2121...2121
RTMR2  match      2222...2222
RTMR3  unchecked  2323...2323
reasons: register-mismatch
verdict: untrusted
[exit 1]
```

- `--issuer` and `--validator` are `unix:PATH` (or a bare path) or `tcp://HOST:PORT`. The `tdxs` service listens on the unix socket `/var/tdxs.sock` in the image and speaks JSON lines: one `{"method": "issue", "data": {"userData": HEX, "nonce": HEX}}` request, one `{"data": {"document": HEX}, "error": null}` reply, whose document is the hex of the JSON attestation document (`raw_quote`, `user_data`, `nonce` in base64, and `platform`). Reach it from the host by forwarding the socket over SSH as above, or with a forwarded TCP port (`deploy --param forward=7000:7000` and a `socat TCP-LISTEN:7000,fork UNIX-CONNECT:/var/tdxs.sock` in the image). tdxs serves no http, so an `http://` or `https://` endpoint fails with `E_VALIDATION` (`Endpoint 'http://x' is http, which tdxs does not serve.`) before anything is contacted.
- The validator is a `tdxs` with a validator configured, run on the verifying side (not in the image under test). It gets `{"method": "validate", "data": {"document": HEX, "nonce": HEX}}` and answers `{"data": {"userData": HEX, "valid": true}, "error": null}` to accept; a `validator error: ...` reply is a rejection (printed as `signature invalid (ENDPOINT): ...`, reason `validator-rejected`).
- Without `--validator` the output says `signature unchecked (no --validator)` and ends with a `note:` line; the verdict is never `trusted`.
- A `simulator` issuer's quote is not a TDX quote: its registers come from the issuer's `metadata` reply (`(simulator, issuer metadata)` in the header), and its nonce check reads the quote's first 32 bytes followed by 32 zero bytes. Its verdict is at best `simulated`, which exits 1 unless `--allow-simulated`.
- An unreachable issuer or validator, an issuer that replies with an `error` (`issuer error: tdx: failed to get raw quote: ...`), or a validator that cannot judge (`validator not configured`, a reply without `data.valid`) is `E_ATTESTATION`; a reply that is not a TDX quote is `E_MEASUREMENT`; a placeholder policy is refused with `E_MEASUREMENT`; a bad `--nonce` or policy file is `E_VALIDATION`.
- `--format json` prints `{"schema": "tundravm.attest/1", "checks", "issuer", "validator", "signature_status": "valid"|"invalid"|"unchecked", "validator_error", "nonce": {"value", "report_data", "expected_report_data", "verdict"}, "platform", "policy", "quote_version", "registers": {NAME: {"actual", "expected", "verdict"}}, "source": "quote"|"metadata", "reasons", "verdict", "trusted", "passed"}` with sorted keys; `--format markdown` a table of the registers under the verdict.

### Deploying to each target

A successful `deploy` means the target accepted the image: QEMU started, or the cloud created the VM. It does not mean the image booted, that its services run, or that it attests; each sequence below ends with those checks. These sequences have not been run end to end against QEMU, Azure or GCP: the command lines and the `deploy` output below are what the adapters in `src/tundravm/deploy/` run and print, captured with their tool calls stubbed out, and the checks after `deploy` are the platforms' own commands.

**QEMU.** Needs `qemu-system-x86_64` on `PATH` and KVM (`/dev/kvm`: the adapter always passes `-machine q35,accel=kvm -cpu host`). A bootable variant bakes to a UKI (`.efi`), which boots with `-kernel` on OVMF firmware: `OVMF_CODE.fd` (or `OVMF_CODE_4M.fd`) under `/usr/share/OVMF`, `/usr/share/edk2/ovmf` or `/usr/share/qemu`, plus a copy of the matching `OVMF_VARS` file made once at `BUILD/<variant>/qemu-ovmf-vars.fd`, since the guest writes its variables there. A `.qcow2`, `.vhd` or raw disk boots as a virtio drive instead.

```console
$ tundravm bake image.py --variant default --backend local
$ tundravm measure --variant default --export-policy peer.json
$ tundravm deploy qemu --variant default --param memory=4G --param cpus=4 --param ssh_port=2222
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
- QEMU detaches by default (`daemonize=true`): the serial console goes to `serial_log` (`tail -f build/default/qemu-serial.log`), the monitor listens on the `monitor` socket (`socat - UNIX-CONNECT:build/default/qemu.monitor`, then `info status` or `quit`) and the pid to `pidfile` (`kill "$(cat build/default/qemu.pid)"`). The three live in `BUILD/<variant>`, so a second `deploy` of a variant that is still running fails on its pid file. The socket path must fit a unix socket (107 bytes), or `deploy` fails before starting QEMU. `--attach` keeps QEMU in the foreground instead, with `-nographic -serial mon:stdio` and no log, socket or pid file.
- `tdx=true` boots TDVF, OVMF built with TDX support, through `-bios` (a TDX guest cannot run firmware from pflash): `/usr/share/ovmf/OVMF.fd`, `/usr/share/edk2/ovmf/OVMF.inteltdx.fd`, `/usr/share/OVMF/OVMF.fd` or `/usr/share/qemu/OVMF.fd`, the first found. It replaces the two pflash drives above with `-bios /usr/share/ovmf/OVMF.fd` and passes `-machine q35,accel=kvm,kernel-irqchip=split,confidential-guest-support=tdx0 -object tdx-guest,id=tdx0`, which needs a TDX host (kernel, KVM and QEMU with TDX support).
- Checks: `ssh -p 2222 root@localhost` once the image's `sshd` has a key for you (boot), `systemctl status` of your services there (application), and the quote checked against `peer.json` with `tundravm attest --issuer unix:./tdxs.sock --validator ENDPOINT --policy peer.json` after forwarding the image's `/var/tdxs.sock` (see [Attest](#attest)), or by a verifier built with `Tdxs.from_policy("peer.json")`, which also verifies the collateral (attestation; needs `tdx=true` on a TDX host).

**Azure.** Needs `az` on `PATH` after `az login`, an existing resource group (`resource_group`, default `tdx-vms`) and a storage account (`storage_account`, required); the adapter creates the `tdx-images` container in it when missing. The blob upload passes no credentials, so `az` must be able to look up the account key (or read `AZURE_STORAGE_KEY`). A confidential VM boots only from an Azure Compute Gallery image whose definition supports it, so the adapter publishes the VHD as a gallery image version first. `vm_size` must be a TDX confidential size: the DCesv5/DCedsv5 series (default `Standard_DC2es_v5`) or the ECesv5/ECedsv5 series. Bake an `azure` variant, which converts its disk to a fixed VHD:

```console
$ tundravm bake image.py --variant azure --backend local
$ tundravm deploy azure --variant azure --param storage_account=mystorage
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
$ tundravm deploy gcp --variant gcp --param project=my-project --param bucket=my-bucket
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

`tundravm sbom --variant NAME` reads the bake in `--build DIR` (default: the `build` of `[tool.tundravm]`, else `build` beside its `pyproject.toml`, else `./build`) and says what one baked variant contains, merged from three sources into one document:

- **Packages**: the JSON package manifest mkosi writes next to the artifact (`ManifestFormat=json` is emitted for every variant, so a real bake leaves `BUILD/<variant>/output/<variant>.manifest`; `<variant>.manifest.json` is read too). Each distribution package with its version and architecture, plus its repository when the manifest records one (mkosi 26 does not). Without a manifest, as after an in-process bake, the document says so (a `note:` line, `creationInfo.comment`, CycloneDX `metadata.properties`) and lists the packages the recipe declares, unversioned.
- **Sources**: the lockfile's pins for the variant: each source build (`<variant>/<name>` when pinned per variant) with its URL, ref, commit and the paths its `Install` steps write; the built kernel (`kernel`, `kernel-<variant>`); the `EfiStub` package (`efi-stub`) with its URL and sha256. `--lockfile FILE` names the lockfile; by default it is the one the bake recorded in `bake-result.json`, else `tundravm.lock` in the bake directory. Without one, sources are not listed.
- **Metadata**: variant, base, arch, snapshot and mirror (from the lockfile's `distribution` section, else the manifest's `config`), the recipe, tree and artifact digests from `bake-result.json`, the artifact and manifest paths relative to the bake directory (moving `build/` elsewhere gives the same document), `bound` and the tundravm version.

The document is bound to the bake. `sbom` first hashes the artifact and fails with `E_ARTIFACT_CHANGED` when it changed since the bake. The bake records in `bake-result.json` the sha256 of the lockfile it read (`lock_sha256`, `null` without one) and of the manifest beside each artifact (`manifest_digests`, `null` when there was none); a lockfile that hashes to something else (or any lockfile when the bake read none), or a manifest that changed or appeared since, also fails with `E_ARTIFACT_CHANGED`, naming both sha256 prefixes. `--allow-unbound` writes the document anyway: `bound` reads `no (incomplete)` and a note says what is unbound. A manifest the bake recorded that is gone is a note; the declared packages are listed instead.

| `--format` | Shape |
|---|---|
| `spdx-json` (default) | SPDX 2.3: `SPDXRef-DOCUMENT` `DESCRIBES` the image package (`SPDXRef-Image`, its artifact sha256 as `checksums`, the metadata as `comment`), which `CONTAINS` each distribution package and is `GENERATED_FROM` each source. Every package has `name`, `versionInfo` (a source's commit or sha256), `downloadLocation` (`git+URL@COMMIT`, an http URL, or `NOASSERTION`), `checksums` when a sha256 is known and a `purl` external reference. |
| `cyclonedx-json` | CycloneDX 1.5: the image is `metadata.component` (`operating-system`, metadata as `tundravm:*` properties); packages are `library` components, builds and kernels `application`; `externalReferences` (`vcs`/`distribution`), `hashes`, `tundravm:install`/`tundravm:ref` properties; one `dependencies` entry from the image to every component. |
| `text` | The metadata, then aligned `packages` and `sources` tables and the notes. |
| `markdown` | The same as tables, ready for `$GITHUB_STEP_SUMMARY`. |

Package URLs: `pkg:deb/debian/NAME@VERSION?arch=ARCH&distro=debian-trixie` for a package (the base with `/` as `-`; `rpm`/`alpm` for those manifests), `pkg:generic/NAME@COMMIT?vcs_url=git+URL@REF` for a git source and `pkg:generic/NAME@SHA256?checksum=sha256:SHA256&download_url=URL` for an http one, percent-encoded in the canonical form (`+` is `%2B`, `@` inside a qualifier `%40`). Every list is sorted and the document ids are derived from the content, so the same bake gives the same document; only the creation time changes, and `SOURCE_DATE_EPOCH` fixes it. Notes go to stderr as well for the JSON formats and with `--output FILE`, which writes the document instead of printing it. `--allow-unbound` accepts a lockfile or manifest the bake did not record (see above).

```console
$ tundravm sbom --variant default --format text
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
$ tundravm sbom --variant default --output build/default.spdx.json
wrote spdx-json build/default.spdx.json
```

## Evidence

`tundravm evidence [RECIPE]` gathers what an auditor asks about a bake into `BUILD/evidence/` (`--build DIR`, default `build`, the directory holding `bake-result.json`), for every baked variant or the `--variant` names (a variant that was not baked is an `E_STATE` error):

| Member | Holds |
|---|---|
| `evidence.json` | The index: `schema` (`tundravm.evidence/1`), `schema_version`, `created` (ISO 8601 UTC; `SOURCE_DATE_EPOCH` when set), `recipe` (`name`, `path`, `file_sha256`, `digest` now, `baked_digest` the bake recorded, `matches_bake`), `bake` (`path`, `backend`, `simulated`, `reproducibility`: the recorded state object or `null`), `lockfile` (`source`, `version`, `compiler_version`, `recipe_digest`, `sha256`, `recorded_sha256` (the `lock_sha256` the bake recorded), `bound`, `drift`, `verdict`: `current` or `drifted`; `null` without one), `lint` (`errors`, `warnings`, `infos`, `codes`), `tools` (`tundravm`, `python`, `mkosi` as `mkosi --version` prints it on this host, `null` when absent), per variant its `artifacts` (`target`, `path`, `size`, `sha256`, `integrity`: `verified`, `mismatch` or `missing`, `simulated`), `tree_digest`, `tree_matches`, `reproducibility` (the state `bake --verify-reproducible` recorded for the variant: `matched`, `different`, `running`, `error`, `interrupted`, or `null` when not checked), `policy`, `sbom` (per artifact: `target`, `member`, `packages`, `sources`, `manifest_found`, `bound`; an artifact that changed or is gone gets no SBOM, and a note) and `provenance` (declarations per fragment and resolution actions: `declared`, `added`, `replaced`, `removed`), the `verdict`, the `notes` and `members` (`sha256` and `size` of every other file) |
| `bake-result.json` | The bake manifest, verbatim |
| `tundravm.lock` | The lockfile, verbatim: `--lockfile FILE`, else the one the bake recorded, else `BUILD/tundravm.lock` |
| `lint.json` | Every lint diagnostic of the selected variants, with the lockfile's pins applied |
| `variants/<variant>/sbom-<target>.spdx.json` | The SPDX 2.3 SBOM of each verified artifact, as `tundravm sbom` writes it |
| `variants/<variant>/policy.json` | The measurements policy: `--policy FILE` for every selected variant, else `BUILD/<variant>/policy.json` when it exists; the index says whether it is a placeholder and whether its `artifact.sha256` is the artifact's |
| `variants/<variant>/tree.json` | Every entry of the variant's compiled tree as the recipe compiles now (`schema` `tundravm.tree/1`): `files`, each with `path` (relative to the variant's directory), `type` (`file`, `symlink`, `directory`), `mode` (octal, as the tree digest sees it: `0755` or `0644` by exec bit), `sha256` and `size` for a file, `target` for a symlink; `variant_digest` over those rows, `tree_digest` and `compiled_variants` of the whole tree compiled for the baked variants (what the bake records when `tree_matches`). `changes` compares it with the current tree |
| `evidence.json.sig` | With `--sign KEY` only: the SSH signature of the exact bytes of `evidence.json` (`ssh-keygen -Y sign -n tundravm-evidence`); it covers every member through their sha256 in the index. It is not listed in `members`, and an unsigned run removes a previous run's |

The `verdict` holds each check's state: `integrity` (`verified` when every artifact still hashes to the sha256 the bake recorded, else `mismatch` or `missing`), `lock` (`current`, `drifted` or `missing`), `reproducible` (`matched`, `different`, `running`, `error`, `interrupted` or `not checked`), `lint` (`clean`, `warnings` or `errors`), `recipe` and `tree` (`matches`, `changed` or `unknown`), `policy` (`verified`, `invalid`, `placeholder`, `mismatch`, `unbound` or `missing`), `artifacts` (`real` or `simulated`), `required` (the `--require` names), `reasons` and `overall`. Each reason is `{"severity", "code", "subject", "message"}`; `subject` is a variant, a `variant/target` or `null`. `overall` is `fail` when any reason is a contradiction, `incomplete` when the only reasons are required checks that are missing, else `pass`; `evidence` exits 1 unless it is `pass`.

| Reason code | Severity | When |
|---|---|---|
| `recipe-mismatch` | fail | The recipe's digest is not the one `bake-result.json` records (`recipe.matches_bake` is false) |
| `tree-mismatch` | fail | The variant's compiled tree differs from the one it was baked from (`tree_matches` is false) |
| `integrity-mismatch` | fail | An artifact no longer hashes to its recorded sha256 |
| `artifact-missing` | fail | An artifact is gone or unreadable |
| `lock-drifted` | fail | The recipe drifted from the lockfile |
| `lint-errors` | fail | Lint reports errors |
| `not-reproducible` | fail | `bake --verify-reproducible` recorded `different` |
| `policy-invalid` | fail | The policy file is not a valid `measure --export-policy` policy |
| `policy-placeholder` | fail | The policy holds placeholder registers |
| `policy-artifact-mismatch` | fail | The policy's `artifact.sha256` is not the variant's artifact |
| `lock-missing` | incomplete with `--require lock` | No lockfile |
| `reproducibility-unchecked` | incomplete with `--require reproducible` | The bake did not run `--verify-reproducible` |
| `reproducibility-incomplete` | incomplete with `--require reproducible` | The second build is `running`, ended in `error` or was `interrupted` |
| `policy-missing`, `policy-unbound` | incomplete with `--require measured` | No policy for the variant, or one naming no artifact sha256 |
| `simulated-artifact` | incomplete with `--require real` | The artifacts are simulated (in-process backend) |
| `lock-unbound`, `sbom-unbound` | incomplete | `--allow-unbound` recorded a lockfile or an SBOM that is not bound to the bake |

Without `--require`, a missing check (no lockfile, no reproducibility check, no policy, simulated artifacts) is a note in the index and in the output, as is what could not be included (no mkosi manifest beside a simulated artifact).

| Flag | Effect |
|---|---|
| `--build DIR` | The build directory (default: the `build` of `[tool.tundravm]`, else `build`) |
| `--lockfile FILE` | The lockfile to record and check for drift |
| `--policy FILE` | A `measure --export-policy` file for every selected variant |
| `--bundle FILE` | Also write a deterministic `tar.gz`: members sorted under `evidence/`, owned by `0:0` with mode `0644`, every mtime (and the gzip header's) `SOURCE_DATE_EPOCH`, else 0, so two runs over the same bake with the same `SOURCE_DATE_EPOCH` give the same bytes |
| `--html FILE` | Also write one self-contained HTML page (inline CSS, no scripts or external assets, every value escaped): a verdict banner (integrity, lock, reproducible, lint, recipe, tree, policy) listing every reason, the recipe, lockfile, lint and tools tables, a section per variant (artifacts, policy, SBOM, provenance), the notes and the members with their sha256 |
| `--format text\|json` | How the index is printed: a summary with every member's sha256 (default), or `evidence.json` itself, verbatim (it carries `"schema": "tundravm.evidence/1"`) |
| `--allow-unbound` | Record a lockfile that does not hash to the `lock_sha256` the bake recorded, and SBOMs over an unbound lockfile or manifest, instead of failing with `E_ARTIFACT_CHANGED`; adds the `incomplete` reasons `lock-unbound` and `sbom-unbound` |
| `--require LIST` | Comma-separated checks that must be present: `reproducible` (a recorded `matched` second build), `measured` (a valid, non-placeholder policy bound to each artifact), `real` (no simulated artifact), `lock` (a lockfile). A missing one is an `incomplete` reason (exit 1) instead of a note |
| `--sign KEY` | Sign `evidence.json` with the SSH private key `KEY` (`ssh-keygen -Y sign -f KEY -n tundravm-evidence`; a key without a passphrase, or a `.pub` whose private key is in `ssh-agent`) and write `evidence.json.sig` beside it and into `--bundle`. Without `ssh-keygen` on `PATH` it fails with `E_STATE` and a hint; a key it cannot use fails with `E_VALIDATION` |

After the [tutorial](tutorial.md)'s bake and `bake --verify-reproducible` (three variants, no `policy.json` beside them):

```console
$ SOURCE_DATE_EPOCH=1700000000 tundravm evidence node.py --html report.html --bundle evidence.tar.gz
wrote build/evidence
wrote evidence.tar.gz
wrote report.html
evidence 2023-11-14T22:13:20Z  verdict pass
  recipe     node.py  digest 2f0041479c07  matches
  tree       matches
  integrity  verified
  lock       current
  reproduce  matched
  policy     missing
  artifacts  simulated
  lint       clean
  debug/qemu  build/debug/disk.qcow2  sha256 b50a5a5aeaac  verified
  default/qemu  build/default/disk.qcow2  sha256 1fa043adea90  verified
  dev/qemu  build/dev/disk.qcow2  sha256 9cfb8b5c2f31  verified
members:
  d57a9242a7b3cc26e9e2eea57cbbb970e1e2c083d6a050a3f8d42b8af22bb305  bake-result.json
  37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570  lint.json
  259d38ee530784c61ae5514f21c8dd11670025d41ece1992946c7b77224179e8  tundravm.lock
  4ab24482ebf081dad68030859aa3d93267b0e7dc5d39a6a70695776b8b8ff7a1  variants/debug/sbom-qemu.spdx.json
  719a977d74f34a4a599eb06fd75aad12abd6955b824a5b9129ee39c01c595995  variants/debug/tree.json
  89a263d917ce3261d9af02cd8e2c6954b148818140c4dc8bb37b8c36833a95ec  variants/default/sbom-qemu.spdx.json
  bc5c9c0a302345e7ae5fb7c03197dd453f8caf1b8d77a9f55bac0082ad2eacd4  variants/default/tree.json
  06077054ac56197a591a653d43a6fd455677e26cb5b50bb1e141a54c21012d7a  variants/dev/sbom-qemu.spdx.json
  a893b4401f1b6cca4e405d682a639df2d47b215bccdf03381dbe50afb00debd0  variants/dev/tree.json
note: debug/qemu sbom: the artifact is simulated (in-process backend): nothing was installed
note: debug/qemu sbom: no mkosi package manifest at debug/debug.manifest: listing the packages the recipe declares, without versions
note: no measurements policy for variant debug (build/debug/policy.json does not exist); write one with `tundravm measure --build build --variant debug --export-policy build/debug/policy.json`
note: default/qemu sbom: the artifact is simulated (in-process backend): nothing was installed
note: default/qemu sbom: no mkosi package manifest at default/default.manifest: listing the packages the recipe declares, without versions
note: no measurements policy for variant default (build/default/policy.json does not exist); write one with `tundravm measure --build build --variant default --export-policy build/default/policy.json`
note: dev/qemu sbom: the artifact is simulated (in-process backend): nothing was installed
note: dev/qemu sbom: no mkosi package manifest at dev/dev.manifest: listing the packages the recipe declares, without versions
note: no measurements policy for variant dev (build/dev/policy.json does not exist); write one with `tundravm measure --build build --variant dev --export-policy build/dev/policy.json`
note: the artifacts are simulated (in-process backend): nothing was built
[exit 0]
```

The `wrote` lines go to stderr, the summary to stdout. Running it again with the same `SOURCE_DATE_EPOCH` writes a byte-identical `evidence.tar.gz`. With one byte appended to `build/default/disk.qcow2`, the summary reads `integrity  mismatch`, the artifact line ends in `mismatch`, `verdict fail` with the reason `fail        integrity-mismatch  default/qemu: build/default/disk.qcow2 changed since the bake`, and the command exits 1. Requiring what the in-process bake cannot give makes it incomplete instead:

```console
$ tundravm evidence node.py --require reproducible,lock,real,measured
evidence 2023-11-14T22:13:20Z  verdict incomplete
  ...
  required   reproducible, measured, real, lock
  ...
reasons:
  incomplete  policy-missing  debug: no measurements policy for debug (required: measured)
  incomplete  policy-missing  default: no measurements policy for default (required: measured)
  incomplete  policy-missing  dev: no measurements policy for dev (required: measured)
  incomplete  simulated-artifact  debug/qemu, default/qemu, dev/qemu: the artifacts are simulated (in-process backend): nothing was built (required: real)
members:
  ...
[exit 1]
```

## Verify

`tundravm verify EVIDENCE` checks what `evidence` wrote without the recipe, the checkout or the bake directory: `EVIDENCE` is the `--bundle` file, the `BUILD/evidence` directory or its `evidence.json`. A bundle is read in memory (nothing is extracted, a member outside `evidence/` or that is not a plain file fails the check) and a directory is walked without following links. Each check is `pass`, `fail` or `missing`; `verify` exits 0 only when every one passes.

| Check | Passes when | Reason code otherwise |
|---|---|---|
| `member` | Every member hashes to the `sha256` (and has the `size`) the index lists | `member-hash-mismatch` (fail), `member-missing` (missing); a file the index does not list is `index-inconsistent` |
| `index` | The index agrees with its members: the lockfile hashes to the index's `lockfile.sha256` and, as its `bound` says, to the `lock_sha256` `bake-result.json` records; each artifact sha256 is the one `bake-result.json` records; each SBOM describes (its image package's SHA256 checksum) the artifact sha256 the index lists; each policy's `artifact.sha256`, placeholder flag and registers are what the index says; each `tree.json`'s rows hash to its `variant_digest`, all variants' rows to its `tree_digest`, and that agrees with `tree_matches` | `index-inconsistent` |
| `artifact` | With `--build DIR` (a build directory, or a [release](#release) directory): each artifact found under `DIR` (at its path in `bake-result.json`, `<variant>/<target>/<file>`, `<variant>/<file>`, or `<file>` when no other artifact has that name) hashes to the index's sha256 | `artifact-hash-mismatch` (fail), `artifact-missing` (missing) |
| `signature` | With `--allowed-signers FILE`: `evidence.json.sig` verifies over `evidence.json` with `ssh-keygen -Y verify -n tundravm-evidence` for `--signer PRINCIPAL`, else for the principals `FILE` lists for the signing key (`ssh-keygen -Y find-principals`) | `signature-invalid` (fail), `signature-missing` (missing: the evidence is unsigned) |
| `verdict` | The stored verdict is the one the recorded facts give (recomputed as `evidence` computes it), and those facts give `pass` under the recorded and the `--require` requirements | `index-inconsistent` for a stored verdict the facts do not give; otherwise each reason with the [evidence code](#evidence) it has there (`fail` reasons fail, `incomplete` ones are missing) |

Without `--allowed-signers` the signature is not checked and a note says so: `unsigned` (no `evidence.json.sig`) or `signature-unchecked`; without `--build`, `artifacts-unchecked`. `--format json` prints `{"schema": "tundravm.verify/1", "evidence", "verdict", "passed", "counts", "required", "signer", "recorded": {"created", "recipe", "verdict"}, "checks": [{"kind", "subject", "state", "code", "message"}], "notes": [{"code", "message"}]}`; `verdict` is `fail` when a check failed, `incomplete` when one is missing, else `pass`. `--html FILE` also writes one self-contained page: the verdict banner with the counts, the problems, every check and the notes.

Signing a release's evidence and checking it elsewhere, with a throwaway key:

```console
$ ssh-keygen -q -t ed25519 -N '' -C release -f release-key
$ echo "release@example.com namespaces=\"tundravm-evidence\" $(cat release-key.pub)" > allowed_signers
$ SOURCE_DATE_EPOCH=1700000000 tundravm evidence node.py --sign release-key --bundle v1.tar.gz
wrote build/evidence
signed evidence.json with release-key: evidence.json.sig
wrote v1.tar.gz
$ tundravm verify v1.tar.gz --allowed-signers allowed_signers --build build
verify v1.tar.gz  verdict pass  (13 pass, 0 fail, 0 missing)
  evidence   2023-11-14T22:13:20Z  recipe node  recorded verdict pass
  member     5/5 pass
  index      4/4 pass
  artifact   1/1 pass
  signature  1/1 pass, signed by release@example.com
  verdict    2/2 pass
[exit 0]
```

The same evidence directory with a byte appended to `lint.json`, `tree.json` deleted, the stored `verdict.lint` edited, and an artifact copy with a byte appended:

```console
$ tundravm verify tampered --allowed-signers allowed_signers --build artifacts --require reproducible
verify tampered  verdict fail  (6 pass, 4 fail, 2 missing)
  evidence   2023-11-14T22:13:20Z  recipe node  recorded verdict pass
  member     3/5 pass
  index      3/3 pass
  artifact   0/1 pass
  signature  0/1 pass
  verdict    0/2 pass
  required   reproducible
problems:
  fail     member-hash-mismatch  lint.json: sha256 b07781657559, the index lists 37517e5f3dc6
  missing  member-missing  variants/default/tree.json: listed in the index, not in the evidence
  fail     artifact-hash-mismatch  default/qemu: artifacts/default/disk.qcow2 hashes to 19480efbd829, the index lists 1fa043adea90
  fail     signature-invalid  evidence.json.sig: does not verify against allowed_signers: Signature verification failed: incorrect signature
  fail     index-inconsistent  recorded: the recorded verdict disagrees with the recorded facts: lint errors (facts: clean)
  missing  reproducibility-unchecked  default: reproducibility of default was not checked (bake --verify-reproducible) (required: reproducible)
[exit 1]
```

## Changes

`tundravm changes [RECIPE] --since EVIDENCE` compares a previous release's evidence (a bundle or directory, as `verify` reads it) with the recipe and lockfile as they are now, for the evidence's variants the recipe still declares (`--variant` picks some). A variant only the evidence has is reported as removed; one only the recipe declares is added, unless the evidence's lockfile holds it (it existed but was not baked), which is a note: `variant azure is not in the evidence (it was not baked): not compared`. It compiles the current tree once, reads nothing from the network and exits 0.

| Section | Compares |
|---|---|
| Recipe and tree | The recipe digest (`recipe.digest` of the index against the recipe now) and each variant's `variant_digest` (its `tree.json` against the current tree) |
| Lockfile sections | The section digests of the evidence's `tundravm.lock` against the current lockfile's (`--lockfile`, default `build/tundravm.lock` when it exists; without one, against the recipe's sections now), named as [lock drift](#lockfile-drift) names them, with the same item detail (`+htop -jq`, `~/etc/motd`); other variants' sections are left out |
| Source pins | Each pin by name (or URL): `ref`, then the old and new commit or sha256; pins only one side has are added or removed |
| Packages | The packages each variant's SBOM in the evidence lists against the current ones: the SBOM of the bake in `--build DIR` (mkosi's manifest, with versions) when given, else the packages the recipe declares, unversioned (a note says so). Added and removed by name; a version change only when both sides have versions |
| Emitted files | Each variant's `tree.json` rows against the current tree: added, removed and changed files (`content`, `mode 0644 -> 0755`, a type or symlink target), each added or changed file under `mkosi.extra` or `mkosi.skeleton` with the declarations that produce it, as `inspect --why` finds them (the first 25; a note says when more were left unexplained) |
| Measurements | The registers of the evidence's `policy.json` against `BUILD/<variant>/policy.json` of `--build DIR`, when both exist |

Text groups the changes by section with a count line per section, then `+`, `-` or `~` lines; `--format markdown` is a heading, a count table and one table per non-empty section, with the notes folded in a `<details>` block, ready to paste into a pull request; `--format json` prints `{"schema": "tundravm.changes/1", "since", "created", "recipe", "variants", "counts", "changes": [{"section", "change", "subject", "variant", "old", "new", "detail", "origins"}], "notes"}`. Evidence made before `tree.json` existed gives no file comparison, with a note.

After the signed release above, swapping `jq` for `htop` and changing `/etc/motd`, then locking again:

```console
$ tundravm changes node.py --since v1.tar.gz
changes since evidence 2023-11-14T22:13:20Z (v1.tar.gz)  recipe node  variants default
  recipe        2
  lockfile      2
  pins          0
  packages      2
  files         2
  measurements  0
recipe (2):
  ~ recipe digest  11901e376769 -> 1417b0ce38ee
  ~ default tree  d2776de270d1 -> 1eeb3a76e374
lockfile (2):
  ~ variants.default.files  (~/etc/motd)
  ~ variants.default.packages  (+htop -jq)
packages (2):
  + default  htop
  - default  jq
files (2):
  ~ default  mkosi.conf  (content)
  ~ default  mkosi.extra/etc/motd  (content)  from File(extra, /etc/motd) (declared in common)
note: current packages are the recipe's declared ones, unversioned (pass --build DIR for the versions a bake installed)
note: default: package versions are not compared where the evidence has none
[exit 0]
```

## Explore

`tundravm explore [RECIPE] [--variant NAME ...] [--lockfile FILE] [--build DIR] [--output FILE] [--format html|json]` writes one HTML page that lets someone understand a recipe without reading the Python, and prints its path: `BUILD/explore.html` (`--build`, default: the table's `build`, else `build`) unless `--output FILE` names another. It compiles the selected variants (default: every one) in memory, reads `BUILD/bake-result.json` when there is one, and never touches the network or bakes. The page is self-contained: inline CSS, a small inline script and no external assets (source URLs appear as text, never as links to fetch), and every value from the recipe is escaped.

| Tab | Shows |
|---|---|
| Overview | The recipe's name, base, arch, snapshot, mirror, dialect, layout and recipe digest; the lint summary with every finding (severity, code, location, subject, message, hint), as `lint` reports them with the lockfile's pins applied; the lockfile's state (`current`, `drifted` with one line per drifted section, or `missing`), its recipe digest, compiler version, section digests and source pins (`--lockfile`, default `build/tundravm.lock` when it exists); and, when `--build` holds a bake, its backend, the simulated flag, the reproducibility state `bake --verify-reproducible` recorded and every artifact with its sha256 |
| Variants | Each variant's parent, targets, declaration count and resolved fragments, then a matrix with one row per declaration and one column per variant: `●` present (from `common`), `+` added by a variant overlay (its own or an ancestor's), `~` replaced (with the fields that differ from the first variant that has it), `−` removed, `·` absent. Expanding a declaration shows its key fields, and per variant the resolution steps (`declared in common via base > app`, `replaced in variant prod`), the tree files it produced and the lines the compiler generated for it (enablement links, runtime-init ordering) |
| Emitted tree | Per variant, every path of its compiled tree grouped by directory, with its mode, size and sha256 prefix and the declarations that produced it (`declared by File(extra, /etc/app.conf)`, as `inspect --why` finds them; a compiler-generated file says so). Each file expands to its content: text up to 64 KiB (a longer file is truncated with a note; the page shows at most 4 MiB of content in all), a binary file is noted, not shown |
| Hooks & builds | Per variant, the build hooks in stage order (`sync` to `boot`) with their scripts, each named after its `Hook` declaration or marked as generated; the runtime-init steps in the order they run, by priority (the built-in `keys`, `disks` and `secrets` steps run at 10, 20 and 30), each named after its `Init`; the source builds with their URL, ref, pin (`unpinned` without one) and install destinations; the kernel and its source |

A declaration's name links to the files it produced and each file's "declared by" links back to the declaration; following a link switches to its tab, opens the collapsed entry and highlights it. The Variants tab has a filter box and an "only declarations that differ" switch; the Emitted tree tab has a path filter. Without JavaScript the tabs read as one long page, entries expand as native `<details>` and the links still jump.

`--format json` prints the model the page renders from instead (to `FILE` with `--output`): `{"schema": "tundravm.explore/1", "recipe", "lint", "lock", "bake", "variants", "declarations": [{"declaration", "type", "key", "fields", "variants": {NAME: {"state", "origins", "changed", "files", "generated"}}}], "trees": {NAME: {"files": [{"path", "image_path", "type", "mode", "size", "sha256", "binary", "truncated", "text", "declarations"}]}}, "scripts": {NAME: {"hooks", "init", "sources", "kernel"}}, "notes"}`. `Exploration.from_dict(data).html()` renders it again.

## Release

`tundravm release [RECIPE] --version VERSION` cuts one release directory, `RELEASES/<recipe>-<VERSION>/` (`--releases DIR`, default `releases` beside the `pyproject.toml`, else `./releases`), from the locked recipe. It runs nine stages in order and prints one line per stage, then the release path and its verdict:

| Stage | Does |
|---|---|
| `lock-check` | Compares the recipe with the lockfile (`--lockfile`, default `build/tundravm.lock`, which must exist) and refuses drift with `E_LOCKFILE`. A release never updates the lock: run `tundravm lock`, commit, release again |
| `fetch` | `tundravm fetch` into `--build DIR` (default `build`); skipped for the in-process backend, which builds nothing |
| `bake` | `bake --verify-reproducible` with `--backend` (default: the recipe file's `backend`), then copies each artifact to `RELEASE/<variant>/<file>` (a copy, not a hard link: a later bake rewrites `BUILD` in place). A second build that differs is recorded, and the evidence fails on it |
| `measure` | Exports `BUILD/<variant>/policy.json` (rtmr, as `measure --export-policy`) and copies it to `RELEASE/<variant>/policy.json`. Simulated artifacts are never measured, and placeholders are never exported: the stage is skipped with a note, and with `--require measured` the release fails. Without the measuring tools it is skipped unless `measured` is required (then `E_MEASUREMENT`) |
| `sbom` | `RELEASE/<variant>/sbom-<target>.spdx.json` per artifact, bound to the bake as `tundravm sbom` binds it |
| `evidence` | `tundravm evidence` with `--require LIST`, signed with `--sign KEY`, written as `RELEASE/evidence.tar.gz` and `RELEASE/evidence.html` |
| `verify` | `tundravm verify` of the bundle just written, with `--build RELEASE` (the copies re-hashed), the same `--require`, and with `--sign` the key's public half as the `release` principal |
| `changes` | `tundravm changes` against `--since PREVIOUS` (a release directory or an evidence bundle; default: the newest finished release of the recipe in `RELEASES`, by when it finished, then by version); skipped for the first release |
| `notes` | `RELEASE/RELEASE.md`: the version, variants, recipe digest, lockfile sha256, the artifacts with their sha256, the reproducibility state, the measured registers, the verify verdict with every reason, and the changes report |

The directory then holds the artifacts, `evidence.tar.gz` (with `evidence.json.sig` inside when signed), `evidence.html`, `<variant>/policy.json` (when measured), `<variant>/sbom-<target>.spdx.json`, `RELEASE.md` and `release.json`: `{"schema": "tundravm.release/1", "name", "recipe", "version", "created", "tundravm", "recipe_digest", "lock_sha256", "variants", "backend", "simulated", "reproducibility", "required", "since", "signer": {"principal", "public_key"} | null, "artifacts": [{"variant", "target", "path", "sha256"}], "stages": [{"name", "status": "done" | "skipped", "resumed", "started", "finished", "seconds", "inputs", "outputs": {PATH: SHA256}, "summary"}], "verdict", "reasons": [{"source": "evidence" | "verify", "severity", "code", "subject", "message"}], "finished"}`. `--format json` prints it to stdout and moves the stage lines to stderr.

The verdict is `pass` only when the evidence and its verification pass, every `--require` check included. The in-process backend's artifacts are simulated: the release says `(simulated)`, `RELEASE.md` says so, and `--require real` fails it. Exit 0 when it passes, 1 when it fails, 2 on an SDK error.

**Resuming.** Until the release passes, its directory holds `release.state.json` (`schema` `tundravm.release-state/1`): each completed stage with the sha256 of its inputs (its own settings plus everything the stages before it produced), the sha256 of each file it wrote, and the files outside the release it relies on (`bake-result.json` and the artifacts in `BUILD`). Running the release again resumes (`--resume` says so explicitly, and fails when there is nothing to resume): a stage whose inputs are unchanged and whose files still hash as recorded is kept (`kept`, no time); the first stage whose inputs changed runs, and so does every stage after it. So a changed `--require` or `--sign` reruns from `evidence`, a changed `--since` from `changes`, a new lockfile from `lock-check`, and a tampered SBOM from `sbom`. `--restart` discards the state and the files. An SDK error mid-release ends with `resume with: COMMAND` on stderr (under `--format json`, the error line's `context.resume`); so does a failing verdict, since a failed release stays resumable.

**Immutability.** A release that passed drops its state and is final: `release --version` of it again fails with `E_STATE` (`Hint: Bump the version (--version) to cut a new release.`), `--restart` included.

```console
$ tundravm release --version 1.0
[1/9] lock-check  done      0.0s  lock current (build/tundravm.lock), sha256 fda99a724906
[2/9] fetch       skipped   0.0s  in-process backend: nothing is built, nothing to fetch
[3/9] bake        done      0.0s  2 artifacts, reproducible: matched, simulated
[4/9] measure     skipped   0.0s  simulated artifacts: nothing to measure
[5/9] sbom        done      0.0s  2 SPDX documents, 27 packages
[6/9] evidence    done      0.1s  verdict pass
[7/9] verify      done      0.0s  verdict pass: 18/18 checks pass
[8/9] changes     skipped   0.0s  no earlier release of this recipe to compare with
[9/9] notes       done      0.0s  RELEASE.md, verdict pass
release releases/node-1.0: pass (simulated)
[exit 0]
```

After adding `Package("htop")` and locking again, a release that requires real artifacts fails, stays resumable, and resumes without baking again once the requirement is dropped:

```console
$ tundravm release --version 1.1 --require real
[1/9] lock-check  done      0.0s  lock current (build/tundravm.lock), sha256 b8ea35658807
[2/9] fetch       skipped   0.0s  in-process backend: nothing is built, nothing to fetch
[3/9] bake        done      0.0s  2 artifacts, reproducible: matched, simulated
[4/9] measure     skipped   0.0s  simulated artifacts: nothing to measure
[5/9] sbom        done      0.0s  2 SPDX documents, 29 packages
[6/9] evidence    done      0.1s  verdict incomplete
[7/9] verify      done      0.0s  verdict incomplete: 17/18 checks pass
[8/9] changes     done      0.0s  since node-1.0: 9 changes
[9/9] notes       done      0.0s  RELEASE.md, verdict fail
  evidence  simulated-artifact  default/qemu, dev/qemu: the artifacts are simulated (in-process backend): nothing was built (required: real)
  verify    simulated-artifact  default/qemu, dev/qemu: the artifacts are simulated (in-process backend): nothing was built (required: real)
release releases/node-1.1: fail (simulated)
resume with: tundravm release --version 1.1 --require real
[exit 1]
$ tundravm release --version 1.1
[1/9] lock-check  kept            lock current (build/tundravm.lock), sha256 b8ea35658807
[2/9] fetch       skipped         in-process backend: nothing is built, nothing to fetch
[3/9] bake        kept            2 artifacts, reproducible: matched, simulated
[4/9] measure     skipped         simulated artifacts: nothing to measure
[5/9] sbom        kept            2 SPDX documents, 29 packages
[6/9] evidence    done      0.1s  verdict pass
[7/9] verify      done      0.0s  verdict pass: 18/18 checks pass
[8/9] changes     done      0.0s  since node-1.0: 9 changes
[9/9] notes       done      0.0s  RELEASE.md, verdict pass
release releases/node-1.1: pass (simulated)
[exit 0]
$ ls -R releases/node-1.1
releases/node-1.1:
RELEASE.md  default  dev  evidence.html  evidence.tar.gz  release.json

releases/node-1.1/default:
disk.qcow2  sbom-qemu.spdx.json

releases/node-1.1/dev:
disk.qcow2  sbom-qemu.spdx.json
```

`RELEASE.md` ends with the `changes --format markdown` report since `node-1.0`, its headings one level down. To check a release elsewhere: `tundravm verify releases/node-1.1/evidence.tar.gz --build releases/node-1.1`, with `--allowed-signers FILE` naming the signer's key when it was signed.

## Watch

`tundravm watch RECIPE` keeps the edit loop short. Every `--interval SECONDS` (default `1`) it polls the mtime and size of everything the verdict depends on:

- the recipe file and every `.py` under its directory (hidden directories, `__pycache__`, installed packages and the build output are left out);
- every host path the recipe's declarations read ([`recipe_inputs`](api.md#recipe-inputs)): `File`, `Unit` and `Template` sources given as a `Path`, `Kernel.config`, the policy file of a `Tdxs.from_policy`, and each `Directory` source tree, entry by entry, so adding, removing or editing a member counts;
- the selected lockfile (so `tundravm lock` in another terminal refreshes the drift counts);
- `pyproject.toml`, whose `[tool.tundravm]` table is read again on every check (flags still win), so changing `tree`, `build` or `lockfile` there retargets the watch;
- the checked tree, entry by entry, so an external `tundravm compile` refreshes the verdict. What `--write` itself writes does not count as a change.

A recipe that fails to load keeps the inputs of the last one that loaded. It checks once at start, then once per change: the recipe is loaded again (modules it imports from its directory are re-imported from their current source), linted as `tundravm lint` lints it, and compared with the tree at `--tree DIR` (default: the `tree` of `[tool.tundravm]`, else `BUILD/mkosi`, where `--build DIR` defaults to `build`) without writing, as `compile --check` does. The build directory is left out of polling. The lockfile is chosen as `lint` chooses it: `--lockfile`, else a `[tool.tundravm]` `lockfile`, applies its pins and reports drift against it (`lock-changed` and the other [lock codes](#lockfile-drift)); with neither, `build/tundravm.lock` only pins the sources when it exists. So the counts are always `lint`'s.

Each check prints one line, `HH:MM:SS lint N errors N warnings; TREE`, where `TREE` is `tree up to date`, `tree stale (N files)` or `tree missing (N files to write)`; with `--write` a stale tree is recompiled into the `--tree` and the line ends `wrote TREE` (the tree's path). When lockfile drift is every error, `; lock drifted (N sections)` comes before `TREE`. Two failures print a different single line, and `watch` keeps polling after both:

- Lint errors that keep the recipe from lowering, such as an error-level fragment check (`App`'s `app-privileged-port`), print `HH:MM:SS lint N errors (CODE: MESSAGE)` with the first error's code and message. No tree is checked.
- A recipe that fails to load (a syntax or import error, an unknown `--variant`, an unreadable `--lockfile`) prints `HH:MM:SS error [E_CODE] message`.

Ctrl-C stops it with exit 0.

```console
$ tundravm watch node.py
watching node.py every 1s (Ctrl-C stops)
14:02:11 lint 0 errors 0 warnings; tree up to date
14:02:40 lint 4 errors 0 warnings; lock drifted (4 sections); tree stale (4 files)
14:02:58 lint 2 errors (app-privileged-port: app runs as user app and cannot bind port 80)
14:03:05 error [E_VALIDATION] Recipe node.py failed to load: syntax error at node.py:96: invalid syntax
14:03:12 lint 0 errors 0 warnings; tree stale (1 file)
^Cstopped
```

## Project status

`tundravm status RECIPE` answers "where is this project at?" without writing anything or touching the network. It prints one line per item, `LABEL VERDICT DETAIL`, for the selected variants (`--variant`, default all), then `next: COMMAND`:

| Line | Reports | Verdicts |
|---|---|---|
| `recipe` | path, recipe digest (as `inspect --format json`), variants, mkosi dialect, base and snapshot | `ok` |
| `lint` | diagnostic counts by severity, linted with the `lock` line's lockfile (no pins when it is missing or unreadable) | `ok`, `error` when there are errors |
| `lock` | `--lockfile` or the table's `lockfile` (default `build/tundravm.lock`; a configured one that does not exist reads `configured lockfile PATH does not exist; run tundravm lock`): present, version, drifted sections (as `lock --check`), unpinned sources and kernels | `ok`, `stale`, `missing` |
| `source` | one per source build, built kernel and `EfiStub` package (`efi-stub`), by lock key: whether `BUILD/.sources/<name>-<pin12>-<id8>` holds a complete, unmodified checkout of the locked pin (verified as `fetch` does) | `ok`, `stale` (another pin, an incomplete checkout, or one modified since the fetch: `checkout at PATH modified since the fetch (...); run tundravm fetch --force`), `missing`, `n/a` (unpinned; `nethermind-v1`; or the `inprocess` backend, which needs none) |
| `tree` | the tree at `--tree` (default: the table's `tree`, else `BUILD/mkosi`) against what the recipe compiles to now, as `compile --check` | `ok`, `stale`, `missing` |
| `artifact` | one per baked variant and target in `BUILD/bake-result.json` (`--build`, default `build`): path, size, sha256 prefix, integrity (`unchecked`; with `--verify` the file is hashed: `verified` or `mismatch`), `reproducibility STATE` when `bake --verify-reproducible` recorded it (`"reproducibility": "matched"`, `"different"`, `"running"`, `"error"`, `"interrupted"` or `null` in JSON; `different` only for an artifact that differed), `simulated`, the lockfile the bake used; stale when the recipe digest or the tree digest it was baked from no longer matches, or on an integrity mismatch | `ok`, `stale`, `missing` |
| `manifest` | one per baked variant: mkosi's package manifest beside the artifact (`<variant>.manifest`), which `sbom` and `evidence` read | `ok`, `missing` (the SBOM lists the declared packages only), `n/a` (a simulated artifact: nothing was installed) |
| `backend` | the recipe file's `backend` and how many of its host tools `doctor` finds | `ok`, `missing`, `n/a` (no backend) |

`next` follows the lifecycle: `tundravm lint` when there are errors, else `tundravm lock`, `tundravm fetch --build DIR`, `tundravm compile --tree DIR`, `tundravm bake --build DIR` (or `tundravm doctor` when the backend lacks a tool), and `everything is up to date` once every line is `ok` or `n/a` (a `manifest` line does not change `next`). It repeats `--variant`, `--build` and `--lockfile` as given, and `--tree` unless the tree came from the table (values from `[tool.tundravm]` are left out, so a configured project gets `next: tundravm lock`); a checkout modified since the fetch makes it `tundravm fetch --force`. `status` always exits 0. `--format json` prints `"schema": "tundravm.status/1"` and one object per section (`recipe`, `lint`, `lock`, `sources`, `tree`, `artifacts`, `backend`), each with a `verdict`, plus `manifests`, a list of `{"variant", "path", "present"}` per baked variant, and `next`; `sources` and `artifacts` hold `items`. `--format markdown` prints a table per section.

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
manifest  n/a      build/default/default.manifest  simulated: nothing was installed
manifest  n/a      build/dev/dev.manifest  simulated: nothing was installed
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

`tundravm clean [RECIPE] --what LIST` removes parts of the build directory `--build DIR`; with neither `RECIPE` nor `--build` it cleans the configured `build`, else `build`. `LIST` is comma-separated:

| Part | Removes |
|---|---|
| `sources` | `BUILD/.sources`, the fetched checkouts and dependency caches |
| `tree` | `BUILD/mkosi`, the bake's own tree (never the committed one `--tree` names elsewhere) |
| `artifacts` | Each `BUILD/<variant>/` (artifacts, reports, a `measure --export-policy` written there) and `BUILD/bake-result.json` |
| `evidence` | `BUILD/evidence`, what `tundravm evidence` writes |
| `state` | `BUILD/.mkosi` (the cached tools tree) and the mkosi state next to the tree's config |
| `reproduce` | `BUILD/.reproduce`, the second build a failed `bake --verify-reproducible` keeps |
| `all` | Every part above |

The lockfile is never removed unless `--lockfile PATH` names it, and [releases](#release) live outside the build directory. It prints `removed PATH` per path, or `would remove PATH` with `--dry-run`; without `--what` it lists what `--what all` would remove and removes nothing:

```console
$ tundravm clean node.py
would remove build/mkosi
would remove build/default
would remove build/dev
would remove build/bake-result.json
would remove build/evidence
pass --what all to remove them, or --what with some of sources, tree, artifacts, evidence, state, reproduce (the lockfile stays unless --lockfile names it)
```

A path the user cannot delete (mkosi output a `local` bake left root-owned) is removed with `sudo rm -rf` when `sudo` is available; otherwise `clean` prints `not removed PATH: ...` and a hint, and exits 1.

## Shell completion

`tundravm completion bash|zsh|fish` prints a static script generated from this version's verbs, flags, flag choices (`--format`, `--backend`, `--scheme`, ...), `deploy`'s `TARGET` and file arguments. Its header repeats the install line; regenerate it after upgrading.

| Shell | Install |
|---|---|
| bash | `source <(tundravm completion bash)` in `~/.bashrc` |
| zsh | `source <(tundravm completion zsh)` in `~/.zshrc` after `compinit`, or save the output as `_tundravm` in a directory on `$fpath` |
| fish | `tundravm completion fish > ~/.config/fish/completions/tundravm.fish` |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A check failed: `lint` findings, `compile --check`, `diff`, `lock --check`, `ci`, `doctor` missing a required tool, an `attest` verdict other than `trusted` or `measurements-match` (`simulated` passes with `--allow-simulated`), an `evidence` verdict of `fail` or `incomplete`, a `verify` check that fails or is missing, a `release` whose evidence, verification or `--require` check fails, or a `bake --verify-reproducible` whose second build differs (`E_REPRODUCIBILITY`); or `clean` could not remove a path |
| 2 | An SDK error, printed as `error [E_CODE]: message` with a `Hint:` and context lines (one `tundravm.error/1` JSON line on stderr under `--format json`, see [Output formats](#output-formats)); also a usage error (unknown verb or flag, with a did-you-mean suggestion) |
| 130 | Interrupted |

## CI

`tundravm ci RECIPE --tree mkosi` is the whole gate:

```console
$ tundravm ci node.py --tree mkosi
ok lint: no findings
ok compile: mkosi is up to date
ok lock: build/tundravm.lock is up to date
```

A failing step prints its report, then `FAIL step: ...` and `skip` for the remaining steps, and exits 1. Under GitHub Actions the reports are workflow annotations. With `--variant`, every step checks only those variants, the lock step as `lock --check --variant` does.

The steps share one loaded recipe and one lockfile (`--lockfile`, default `build/tundravm.lock`, read once): `lint --strict` and `compile --check` apply its pins, so a pinned source never reports `source-unpinned`, and the lock step compares against it. The compile step works on a copy of the recipe, so a recipe with runtime-init steps (keys, disks, secrets, `Init`) passes the lock step whenever `tundravm lock --check` passes on its own.

`tundravm init --ci github` writes `.github/workflows/tundravm.yml` with two jobs. `check` runs on every push to `main` and pull request: `uv sync` (`uv sync --frozen` once `uv.lock` is committed; see [Installing tundravm in the project](#installing-tundravm-in-the-project)), `tundravm inspect RECIPE --format markdown` appended to `$GITHUB_STEP_SUMMARY`, then `tundravm ci RECIPE --tree mkosi`. Commit `mkosi/` and `build/tundravm.lock`; the job checks both. `bake` is opt-in: it runs after `check` only when the repository variable `TUNDRAVM_BAKE_BACKEND` is set (`if: vars.TUNDRAVM_BAKE_BACKEND != ''`) to the backend kind to bake with: `local` or `inprocess`. `lima` and `nix` need `limactl` or Nix, which `ubuntu-latest` lacks; add a step that installs one before using it. It installs `bubblewrap`, `debian-archive-keyring` and `python3-pefile` from apt and mkosi `v26` from git with `pipx` (a pinned tag, so the bake's toolchain does not move between runs; edit the tag to move it), syncs the same way, runs `tundravm fetch RECIPE --build build`, `tundravm bake RECIPE --backend $TUNDRAVM_BAKE_BACKEND --build build --verify-reproducible` and `tundravm evidence RECIPE --build build --require LIST --bundle evidence.tar.gz --html evidence.html`, then uploads `evidence.tar.gz`, `evidence.html`, `build/bake-result.json`, the second build's `build/.reproduce/**/bake-result.json` (kept when it differs or fails; `include-hidden-files: true`), mkosi's artifacts (`build/*/output/*`) and the in-process backend's (`build/*/*.qcow2`, `build/*/*.vhd`, `build/*/*.tar.gz`) as the `tundravm-bake` workflow artifact. The evidence and upload steps run with `if: ${{ !cancelled() && hashFiles('build/bake-result.json') != '' }}`, so a bake that fails after writing its record (a second build that differs or fails) still leaves its evidence; the job fails on the bake's exit status, or on the evidence verdict. `--require` is `reproducible,lock,real` on `local`: the bake ran `--verify-reproducible`, `check` already requires the committed lockfile, and `local` builds real artifacts. On `inprocess` it drops `real` (`reproducible,lock`), since the in-process backend only simulates. `measured` is not required: no step exports a measurements policy; add `tundravm measure --build build --variant <variant> --export-policy build/<variant>/policy.json` per variant and `measured` to require one.
