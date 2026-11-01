# Onboarding audit: the README and the guide, run as a new user

Date: 2026-11-01. Base: master at `fcd667f`. Branch: `walkthrough`. Host: x86-64 Linux, no
QEMU, no KVM, no TDX, no cloud CLIs; sudo for mkosi, network and read access to
tundra-tools. The question: does a first user get from `tundravm new` to a booted image
with every failure naming its fix?

## 1. What was run

Every command the README, the guide, the trust page and the command reference show, in a
scratch project:

- `new demo`, `new prov --template prover --ci`; the generated `pytest`; `ruff format` on
  the generated project.
- `check` (text, `--json`, under `GITHUB_ACTIONS=true`, `--profile`, `--tree` with and
  without a tree, with drift), `check --explain` (known, misspelt, a component's code),
  `show` (`--profile`, `--dev`, `--units`, `--tree`, `--json`), `lock`, `lock --check`,
  `lock` without tundra-tools credentials, `doctor` and each `--for`, `--version`, a
  misspelt command and flag.
- A real `build` (it ran mkosi under sudo and failed in the tdxs build, 2.1 below). Then
  `build`, `build --dev`, `verify`, `show BUILD`, `run`, `deploy gcp|azure` against
  `FakeMkosi` builds; `run status|logs|ssh|mode`, `attest`, `secrets deliver|unlock`
  without an instance.
- `convert --from mkosi` on `examples/surge-tdx-prover/mkosi`, `convert --from systemd`
  (both modes) on a plain `Restart=always` unit.
- `examples/surge-tdx-prover/image.py` through `check --tree`, `show` and `show --tree`:
  all clean (14 `source-unpinned` warnings, the tree up to date).

## 2. Findings not fixed, ranked

### 2.1 Every image fails to build from tundra-tools master (M)

`Tdxs` builds `github.com/Hyodar/tundra-tools.git @ master` with `go build` and Debian's
`golang`. master's `go.mod` now says `go 1.25.0`; trixie ships Go 1.24, so Go tries to
download the 1.25 toolchain inside the build sandbox, which has no network (`go: download
go1.25.0: ... dial tcp: lookup proxy.golang.org`), and `build` ends in `E_BACKEND_EXECUTION`
whose hint names mkosi's log, not a fix. `lock` pins master's HEAD, so a fresh `new` +
`build` fails. CI does not see it while the template bakes skip without
`TUNDRA_TOOLS_TOKEN`.

Proposal: (a) the default `Tdxs` source names a tundra-tools commit known to build with
trixie's Go, moved deliberately, never `master`; (b) every tundra-tools `Build` sets
`GOTOOLCHAIN=local`, so a mismatch fails as `go.mod requires go >= 1.25.0` instead of a DNS
error, and never downloads an unpinned toolchain when the sandbox has network; (c) a CI job
that bakes the starter whenever the token exists. Files: `declarative/utils.py`,
`_modules/tdxs.py` and the goldens.

### 2.2 `convert --from mkosi` writes Python that does not parse (S)

`convert.py:237` wraps the package list with `textwrap.wrap`, which breaks on hyphens
inside a quoted name: `'systemd-` / `cryptsetup'` is a `SyntaxError`, so the converted
surge tree cannot be loaded. Fix: `break_on_hyphens=False, break_long_words=False`, and
double quotes as `ruff format` writes them. Off-limits here (`convert.py`).

### 2.3 `convert --from mkosi` converts the wrong image of a tundravm tree (M)

On a tree tundravm wrote, it converts the root QEMU disk (the initrd-ish package list with
regexes such as `'^libtss2-rc0(t64)?$'`) and reports the OS as `subimage payload
(Format=disk): no Image slot`. A tree with `mkosi.images/payload/` should convert the
payload and recognise the main image as the disk tundravm builds itself. `convert.py`.

### 2.4 `convert --from systemd` keeps the most common unit as text (M)

A unit with `ExecStart=`, `User=`, `Restart=always` and no `Type=` is "kept as text,
blocked by Service.Type": the implied `Type=simple` blocks the typed shape. The output is
a `tundravm.declarative.Unit`, which no `Image` slot takes. Proposal: treat a missing
`Type=` as `simple`, and print a `Service(...)` sketch beside the text for what maps.
`_units/parse.py`, `convert.py`.

### 2.5 `show` leads with the dev profile and never names the image (S)

`show` prints `Image: debian/trixie (x86_64)  profile=dev` first and production second,
and the image's name appears nowhere in the text; `show BUILD` says
`demo production (production)`. Proposal: production first, then the other profiles in
declaration order; the first line `demo  profile=production  debian/trixie x86_64`. `show.py`.

### 2.6 Lock findings speak the lockfile's internal vocabulary (M)

`lock-changed [image] variants.dev.files: variants.dev.files changed since the lock`: the
subject is the lockfile section key, which says `variants`, a word the public API dropped
for profiles, and the finding carries no profile. Proposal: findings name
`profile dev: files`, set `variant=` from the section, and lock v3 renames the keys to
`profiles.*` (a lockfile migration, so M).

### 2.7 `--format` and the Markdown table exist only in code (S)

`check.render_as` and `formats.py` still document `--format auto|text|json|github|markdown`,
`compile --check` and `ci`; the CLI has `--json` and switches to annotations under
`GITHUB_ACTIONS=true`. `render_markdown` is unreachable from the CLI. Proposal: delete the
Markdown renderer and the stale docstrings, or give `check` the one `--summary` flag that
writes the Markdown table into `$GITHUB_STEP_SUMMARY` (and use it in the `--ci` workflow).

### 2.8 Small output papercuts (S each)

- `show --json | head` ends in a `BrokenPipeError` traceback; `main` should exit quietly on
  `BrokenPipeError`.
- `check --explain CODE ...` takes `nargs="+"`, so `check --explain tree-drift demo.py`
  reads `demo.py` as a code. `--explain` could take one code per flag (`append`).
- The text summary counts profiles (`2 warnings`) under one printed line; say
  `2 warnings (1 line)` or count lines.
- `E_SOURCE` repeats its fix: the message ends `run tundravm build --clean-cache` and the
  hint says it again. `_source.py`.
- `run --mode tdx` nests parentheses: `(unavailable: no Intel TDX host (kvm_intel tdx is
  not enabled))`.
- `No production build under build.` reads as a verb; `under build/` or the absolute path.
- `doctor` reports `build ready (sudo ...)` when `sudo --version` runs, not when sudo will
  run without a prompt; a CI runner without passwordless sudo learns it at build time.
  Probing `sudo -n` is not enough either (this host refuses `sudo -n true` and still ran the
  build), so the row should say `sudo (may ask for a password)` unless `-n` succeeds.
- `lock --check` without a lockfile is `E_LOCKFILE` (exit 2), where the guide says drift
  exits 1; a first CI run on a project that has not locked reads as a crash.
- The README's Python block is the minimal image, while the commands under it run on the
  `App` project `new` wrote; left as is to keep the README near 400 words.

## 3. Fixed on this branch (the commits carry the why)

- A finding's `at:` named the console script (`../../.venv/bin/tundravm:10`) when tundravm
  ran through a relative path: frame paths are normalized before the skip test.
- `lock` without tundra-tools access suggested `--offline`, which `lock` does not have; it
  says the repository is private, how to reach it, or `Tdxs(source=Git(mirror, ref))`.
- `check --tree` without a tree printed 34 "added relative to" errors; it is one finding,
  and each drifted file says missing, extra or modified.
- Image-wide findings were labelled `[common]`; they are `[image]`, and the guide says so.
- `check --explain UNKNOWN` dumped eighty codes; it names the closest and the lint
  reference, and says a Component's own codes are not registered.
- Lock drift findings had no hint; they say to review and lock again.
- `deploy` of a target the build lacks said `build --target`, which refuses an undeclared
  target; it says to add it to `targets=` first.
- A missing instance said `Start one with tundravm run`, which fails without `--mode`; it
  gives the full command, and `run` without `--mode` says which mode starts services.
- `new` printed `build` (production) then `run --dev`; it prints the loop that works from a
  fresh checkout, shared with the project README, all through `uv run`.
- The generated image was not `ruff format` clean; its docstring now builds `--dev` before
  running `--dev`; its comments say volatile journal and what the dev password is for.
- The `--ci` workflow piped `show` into the job summary as Markdown (columns collapse); it
  fences it, checks out without credentials and uses the actions tundravm's CI pins.
- `show --dev` help said "the latest development build" without saying the image's dev
  profile is `--profile dev`.
- README: the quickstart builds and boots the dev profile through `uv run` (production
  boots fail-closed), and links the lint reference.
- Guide: the first steps lock and write `mkosi/` before `pytest`, since the generated tests
  and CI compare against it; the Markdown table no command prints is gone.

## 4. What held up

`doctor` agrees with every refusal it covers (`run` without QEMU, `run --mode tdx`), a
misspelt command gets a "did you mean", and the mkosi tree, `lock --check`, `verify` and
the fake-build loop behave as the guide says.
