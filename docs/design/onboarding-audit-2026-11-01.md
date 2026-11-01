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

### 2.4 `convert --from systemd` keeps the most common unit as text (M)

Fixed later on `97e6245`: a unit without `Type=` converts to a `Service` in an `Image` file.
As first found: a unit with `ExecStart=`, `User=`, `Restart=always` and no `Type=` is "kept as text,
blocked by Service.Type": the implied `Type=simple` blocks the typed shape. The output is
a `tundravm.declarative.Unit`, which no `Image` slot takes. Proposal: treat a missing
`Type=` as `simple`, and print a `Service(...)` sketch beside the text for what maps.
`_units/parse.py`, `convert.py`.

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
  reference. Fixed later on `component-rules`: a component declares its codes on its class,
  and `--explain` finds an image's own through the image.
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
- Fixed later on `sdk-truth`, through the end of this list: `convert --from mkosi` broke a
  quoted package name at its hyphen (2.2); names never break and strings are double-quoted.
- On a tundravm tree it converted mkosi's `default-initrd` (2.3); it converts the payload and
  notes the QEMU disk, the cloud wrappers and the generated trees, and surge passes `check`.
- `show` led with the dev profile and never named the image (2.5); its first line is the image
  and its file, then production, then the rest as declared.
- `Service(resources=...)` was reported as set "through the extra= escape hatch": typed
  keywords are quiet, and `unit-extra` names the keyword a drop-in or raw directive duplicates.
- A `Service(timer=...)` dropped `environment=`, `resources=`, `shutdown=`, `unit=` and more:
  it keeps them, and refuses `readiness=`, `expose=`, `credentials=` and a volume `state=`.

## 4. What held up

`doctor` agrees with every refusal it covers (`run` without QEMU, `run --mode tdx`), a
misspelt command gets a "did you mean", and the mkosi tree, `lock --check`, `verify` and
the fake-build loop behave as the guide says.

## 5. Second pass

Base: master at `c73d730`, after the eight landings and `component-rules`. Branch:
`walkthrough-2`. Host: no QEMU, no `go`, no user namespaces, sudo only with a password;
`rustup` and tundra-tools read access.

### What was run

In a scratch directory, as a user: `new demo`, `new prov --template prover --ci` (and
`new vault`, to catch names baked into templates), `uv sync`, `check` (text, `--json`,
`--explain` known, unknown and a component's, `--tree` before and after the tree,
`--profile`), `lock`, `lock --check` (text, `--json`, with an edited `app/`), `build
--tree-only`, the generated `pytest`, `show` (image, `--dev`, `--profile`, `--units`,
`--tree`, `--json`, `--measurements`, `latest`, `--diff`), `doctor` and each `--for`, in and
outside a project, `build` and `build --dev` (both refuse: no host `go`, naming Go 1.21 and
`doctor --for build`; a stale `Local` names `tundravm lock`), `run` (no mode, simulated,
tdx), `run status|logs|stop|ssh|mode`, `attest`, `secrets deliver|unlock`, `deploy gcp`
with and without `--build`, `deploy status`, `verify`, `convert --from mkosi` on the demo's
tree, `convert --from systemd` on a unit with a timer, and surge through `check --tree`
and `show`. The generated workflow was parsed as YAML.

### Fixed on this branch (the commits carry the why)

- `verify latest-development` without a build said `tundravm build`: it says `build --dev`.
- `show --dev` in a project means the dev build: its refusal and `--help` name
  `show --profile dev` for the image's dev profile.
- A missing instance offered only `run`; it offers `deploy` too, for `deploy status`.
- The Go host error ("its toolchain" of several sources), the doctor row ("no go on PATH,
  which fetches"), the stale-lock hint ("but to") and `under build.` read as written.
- `show --json` provenance said profile `common` and `fragments`: `image`, `components`.
- `lock --check --json` on a clean lock listed "lock is up to date" as drift: `[]`.
- `lock --help` said a build never changes the lock; `build --dev` writes and re-pins it.
- `show --help` promised provenance the text view lacks and omitted the build report.
- `policies/README.md` hardcoded `build/prover/` and `prover-gcp.json`: it names the project.
- The `--ci` build job had no read access to tundra-tools, so every build failed: it reads
  `TUNDRA_TOOLS_TOKEN`, and the header says to add it.
- The App docstrings said "Component name"; the project README names its build directory.
- Guide: one ESP limit (126 MiB, not "about 125"), the order of the loop, the lock and
  `build:go` rules said once, `show --dev` corrected, "recipe digest" gone.
- README: the starter is a Go program in `app/`, not a busybox page.
- CHANGELOG: one release note, grouped, every break marked.
- A converted python unit fails `check` until `python3` ships: `service-program-missing`.
- Lock hints name the command that runs here; `local-changed` says `build --dev` re-pins.
- `source-unpinned` is image-wide, `[image] sources.NAME`, as lock drift reports a source.
- The API reference speaks the public model: `Resolved.components`, `plan(component)`.
- `new` writes `[tool.ruff] line-length = 100`, at which the project formats clean.
- `doctor` without an image blocks on a missing `go`: every image's `Tdxs` is Go.
- Components contribute their own egress (`Component.outbound()`): Nethermind's peers,
  the driver's L1 port, raiko's collateral and the Observability sinks, so the surge
  prover declares `Egress()` and no longer reports `egress-unfiltered`.
- Fixed later on `egress-profiles`, through the end of this list: a component a `Profile`
  (or `dev=`) adds contributed no egress, since the firewall was one ruleset for the whole
  image; each profile's ruleset is the image's plus its own components' `outbound()`, in
  its overlay when it differs, and its boot probe reads it.
- `TaikoClient` and `Raiko` could only send to TCP 443 anywhere for their L1: `l1=["HOST:PORT"]`
  names the endpoints their secrets hold, and the 443 fallback stays without it.
- `Observability(proxy="IP:PORT")` reaches every sink through one fixed-address HTTP
  CONNECT proxy, the only egress it then needs (plan 6.4), and the remote write's queue
  and retries are bounded in its config.

### Not fixed, ranked

7. `show` names the probe unit `app-ready` beside `app.service`, and its Hooks lines are
   shell internals (S; `_describe.py`).
8. `lock` prints the toolchain but not what it pinned; `build --tree-only` before `lock`
   writes a tree that drifts once locked, without a word (S; `cli.py`).
9. `convert --from mkosi` of a tundravm tree lists implied packages (`chrony`, `nftables`)
   as the image's own (S; `convert.py`).
10. `Observability(proxy=)` is plain HTTP CONNECT: a proxy that speaks TLS itself needs
    its name to resolve to its fixed address, an `/etc/hosts` entry the image does not
    write (GCP's glue owns that file), since neither `systemd-journal-upload` nor
    Prometheus takes the proxy's TLS name apart from its URL (S; `_observability.py`).

The guide's word budget (once item 10) was cut on `walkthrough-3` (section 6); the default
snapshot (once item 11) moved to `20261018T082309Z` on `ec5e339`.

## 6. Third pass

Base: master at `e42ccdf`, after fourteen landings (component rules through the API
review). Branch: `walkthrough-3`. Host: no sudo without a password, no user namespaces, no
QEMU, no `go`; `rustup` and tundra-tools read access.

### What was run

In a scratch directory: `new demo` (its `app/` is Go; with no `go` here, tundravm's own
`go vet` test of the template skips), `new prov --template prover --ci` (the workflow
parsed as YAML, its token step run under bash), `uv sync`, `check` (text, `--json`,
`--explain` known, a component's and misspelt, `--tree` before and after the tree), `lock`,
`lock --check` (text and `--json`), `build --tree-only`, the generated `pytest`, `show`
(image, `--profile dev`, `--units`, `--json`, `--dev`, `IMAGE --dev`), `doctor` and each
`--for`, in and outside a project, `build` and `build --dev` (both refuse without `go`,
naming `doctor --for build`), and through a test of the project using `FakeMkosi` and
`FakeSources`: `build --dev`, `build`, `show BUILD` (text and `--json`), `verify`, `run
--probe` of a production build (refused, naming the dev build), `run --mode tdx`, `run
status`, `attest`, `deploy gcp` without the target. `convert --from systemd` on a timer and
a `Restart=always` python unit, then `check` of the result; `convert --from mkosi` on the
demo's and surge's trees; surge through `check --tree` and `show`.

### Fixed on this branch (the commits carry the why)

- `run`'s summary omitted `probe`; `doctor --help` called `build:go` optional without an
  image; `show --help` left out the boot probe checks a build prints.
- `service-program-missing`, `hardening-disabled`, `service-runs-as-root`,
  `port-unreachable` and `unit-written-at-runtime` named internal spellings
  (`File(PATH, Path)`, `Hardening.strict`, `Firewall(collect=)`, `Unit(dropin=)`).
- `show --json` per profile said `fragments` and `inherits`: `components`, `extends`.
- `convert --from systemd` single-quoted its strings when ruff was not importable.
- The prover docstring's `GoBinary` lacked `package="."`; `policies/README.md` ran a bare
  `tundravm`; the `--ci` token step went on silently to a build that cannot fetch.
- A git source's `source-unpinned` hint was 55 words of fix and background.
- Guide: 7995 to 7551 words (the doctor sample two rows short, the Imports table, the
  toolchain and convert paragraphs, the profile edit inventory); "gates 1, 4 and 5" after
  gate 6; Testing before Build; the explain sample says it is the template's `App`.
- CHANGELOG: 1755 to 1131 words, one bullet per change, 21 breaking marks; README 399.
- Trust: the weekly `kernel` workflow; `docs/design/status-2026-11-01.md` is new.
- Item 11: every `lint.md` fix and hint names the public API, or says tundravm sets it.
- Item 12: the 8745 rule comes from `secret-delivery` and is gone where nothing listens.
- Item 13: the manifest is `tundravm.manifest/2` with `image_digest`; old ones are refused.
- Item 14: `show IMAGE` lists the components the file wrote, what they composed nested.
- Item 16: `show --dev` is always a build; a dev profile is `--profile dev`.

### Not fixed, ranked (items 7 to 10 above still stand)

15. `egress-unfiltered` puts its fix in the message and prints no `hint:` (S;
    `components/_base.py`).
17. The API reference: `File(mode=420)` in decimal, `Firewall.check(resolved:
    dm.Resolved)`, `FakeMkosi.invoke`/`verbs` and `FakeSources.*` without a line
    (S; `_reference.py`, `components/_base.py`, `testing/`).
18. `tundravm.bake` keeps the old verb in the API beside `build` (S, breaking).
