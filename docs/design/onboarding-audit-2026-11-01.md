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
- A real `build` (it ran mkosi under sudo and failed in the tdxs build, since fixed:
  section 3). Then `build`, `build --dev`, `verify`, `show BUILD`, `run`,
  `deploy gcp|azure` against `FakeMkosi` builds; `run status|logs|ssh|mode`, `attest`,
  `secrets deliver|unlock` without an instance.
- `convert --from mkosi` on `examples/surge-tdx-prover/mkosi`, `convert --from systemd`
  (both modes) on a plain `Restart=always` unit.
- `examples/surge-tdx-prover/image.py` through `check --tree`, `show` and `show --tree`:
  all clean (14 `source-unpinned` warnings, the tree up to date).

## 2. Findings not fixed, ranked

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

### 2.8 Left as is

- The README's Python block is the minimal image, while the commands under it run on the
  `App` project `new` wrote; left as is to keep the README near 400 words.

## 3. Fixed on this branch (the commits carry the why)

- Every image failed to build from tundra-tools master (`go 1.25.0` on trixie's Go 1.24):
  fixed later on `go-toolchain`, a Go source builds with the toolchain its go.mod names.
- Fixed later on `rust-toolchain`: `CargoBuild` built with Debian's `cargo` whatever the
  source's `rust-toolchain.toml` pins, and the manifest recorded no Rust toolchain. The host's
  `rustup` installs the pinned toolchain into the workspace, the offline build runs it, the
  manifest and `show BUILD` name it, and an unpinned channel is refused.
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
- Fixed later on `lock-output`, through the next `lock --check` entry: lock findings said
  `variants.<name>.*` and were image-wide; lockfile version 6 says `profiles.<name>.*` (an
  older lock is refused: lock again), and a profile's drift is that profile's finding.
- A production build without a lockfile locked moving refs on the spot: it refuses with
  `lock-missing` (`tundravm lock IMAGE`); `build --dev` still writes a missing lock, and
  CI's fixture jobs lock first.
- `render_as`, the `--format` resolution and every Markdown renderer were reachable from
  no command: deleted; `check` keeps `--json` and the GitHub annotations.
- `show --json | head` printed a BrokenPipeError traceback: it exits 141 quietly.
- `check --explain tree-drift demo.py` read `demo.py` as a code: `--explain` takes one
  code per flag.
- The text summary counted profiles under fewer lines: it says `(1 line)` when they differ.
- `E_SOURCE` for a modified checkout said its fix twice: the hint alone says it.
- `run --mode tdx` nested parentheses: `cannot run here: no Intel TDX host: ...`.
- `No production build under build.`: it says `under build/.`.
- `doctor` called sudo ready without asking whether it prompts: the row says sudo may ask
  for a password unless `sudo -n true` succeeds.
- `lock --check` without a lockfile was `E_LOCKFILE` (exit 2): it is drift, exit 1, naming
  `tundravm lock`, and the guide's CI section says so.

## 4. What held up

`doctor` agrees with every refusal it covers (`run` without QEMU, `run --mode tdx`), a
misspelt command gets a "did you mean", and the mkosi tree, `lock --check`, `verify` and
the fake-build loop behave as the guide says.
