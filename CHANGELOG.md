# Changelog

## Unreleased

tundravm v2 makes mkosi accessible for confidential VMs by making the choices for you:
Debian trixie, systemd, a measured UKI, Intel TDX. You declare one Python `Image`; what
every confidential image needs is implied, each default has one typed escape hatch, and the
compiler writes one plain mkosi 27.1 tree you can read and build without tundravm. Twelve
commands take it from `new` to a locked, reproducible, signed build that runs, deploys and
attests. Nothing keeps a compatibility alias. Real TDX quotes and the attested delivery
channel are not done yet: until then `attest` and `secrets` say `unverified first contact`.
Breaking changes are marked **Breaking**.

### SDK

- **Breaking:** `Image`, `Service`, `Component` and `Profile` replace `Recipe`, `Variant` and `Fragment` as the public surface.
- **Breaking:** every public name has one home (`tundravm`, `.ports`, `.systemd`, `.components`, `.storage`, `.secrets`, `.sources`, `.errors`, `.testing`); flat imports are gone.
- **Breaking:** `profile` replaces `variant` everywhere, `--json` output and `tundravm.testing` included; `show --json` says `image` and `components`.
- `Image` implies the TEE baseline, a curated kernel, a firewall that opens only exposed ports and 8745, one hardened user per service, and `tdxs`.
- `Service` takes an argv; `requires=` waits for real readiness (`Notify`, `TcpProbe`, `HttpProbe`); `expose=` is the only way to open a port.
- A timer service keeps every keyword and refuses the ones only a long-lived service has.
- `Hardening.preset(NAME)` names the runtime class (`static`, `jit`, `node`, `python`, `dotnet`), each with a `systemd-analyze security` ceiling.
- Every development profile compiles in a boot probe derived from its declarations (services, ports, volumes, egress, sandboxes, kernel, host keys); a production profile carries none.
- `Component` is a dataclass with `compose()`, typed exports (`Endpoint`, `ServiceRef`, `ServiceReady`) and an optional `check()`.
- `Profile(extends=, add=, changes=, production=)` with `value.ref.edit(...)`, which takes the value's own constructor keywords, and `value.ref.remove()`.
- `Volume` with `OperatorKey`, `TpmSeal` or `BrokerKey`, `Secret` as a systemd credential, `AttestedTLS` and `TLS` ingress, `RootFS.verity()`, `Observability`.
- `network=Network(egress=Egress(...))` drops every other outbound packet, and accepts the baseline's NTS time, resolvers and cloud metadata servers for you.
- `Kernel(cmdline=)` appends kernel parameters after the image's own and refuses the ones tundravm sets.
- `Mkosi(settings=, scripts=)` is the escape to mkosi, and refuses each setting the compiler writes, naming the slot that sets it.
- The shipped library: `TeeBase`, `Network`, `Firewall`, `Tdxs`, `SshAccess`, `DevAccess`, `Observability`, `Nethermind`, `TaikoClient`, `Raiko`.
- `tundravm.testing`: `plan(image)` or `plan(component)`, `assert_clean`, `assert_diagnostic`, `assert_tree`, `read_lock`, `FakeMkosi`, `FakeSources`, `boot`, `assert_probe`, `run_cli`, `image_file`.
- **Breaking:** the API speaks the public model: `Resolved.targets` and `.components`, `class X(Component, name=)`, `ImageFile.component`, `image_file`; the internal-model test helpers and the `recipe` and `compiled` fixtures are gone.

### CLI

- **Breaking:** twelve commands: `new`, `check`, `lock`, `build`, `show`, `run`, `deploy`, `attest`, `secrets`, `verify`, `convert`, `doctor`.
- `new NAME [--template service|prover] [--ci]` writes a project whose printed loop, README, tests and workflow run as written.
- A new project's `pyproject.toml` sets `[tool.ruff] line-length = 100`, at which its files are `ruff format` clean.
- The `service` template builds your own Go program from `app/`; the prover template adds a volume, a secret, attested TLS and `policies/`.
- The `--ci` workflow checks the image, the lock and the committed tree, and builds with `TUNDRA_TOOLS_TOKEN` when the repository sets it.
- `show` leads with the image and its file, then production; with a build, with the build's report; `--diff` compares two builds.
- `run --mode simulated|tdx` is always explicit; a production build boots fail-closed without TDX, and `--dev` runs the dev build's services.
- `run --probe` and `run probe INSTANCE` wait for the boot probe, print its table, write `probe.json` and exit 1 on a failed check; `show BUILD` lists the checks.
- `deploy azure|gcp --build BUILD` never guesses the build; `upgrade` and `rollback` run the one replacement workflow on GCP.
- `attest` verifies fresh evidence and delivers nothing; `secrets` attests again before it delivers or unlocks.
- `doctor` prints one row per capability with its fix; `build:go` blocks without `go` (every image's `Tdxs` is Go), `build:rustup` only when the image needs it.
- **Breaking:** `convert --from systemd` prints an `Image` file that loads, and `--mode` is gone; `check` names each program it runs that the image does not ship.
- `convert --from mkosi` writes Python that parses, converts a tundravm tree's payload, and turns its cloud wrappers into `targets=`.
- Every error prints its code and a hint naming the command that gets past it; a closed pipe exits 141 quietly; a misspelt verb gets a "did you mean".

### Build and lock

- `build` publishes `build/<image>/production/<id>/` once, read-only, with a canonical `manifest.json` whose digest is `<id>`.
- **Breaking:** a production build needs `build/tundravm.lock` (`lock-missing`); a development build writes a missing one and re-pins `Local` sources.
- **Breaking:** lockfile version 6 names a profile's sections `profiles.<name>`; an older lock is refused: run `tundravm lock`.
- `lock --check` never writes or fetches, and exits 1 on drift, a missing lockfile included; `--json` lists no drift when clean.
- `build` ends with a report: artifacts, sources and toolchains, time per phase, the UKI's Authenticode digest, predicted registers.
- Every build carries `measurements.json` with a `tundravm.policy/2` policy per record, an SPDX SBOM, and `observations.json`.
- `build --reproduce`, `--sign KEY`, `--offline` and `verify [--rebuild]` check the bytes anywhere.
- The ESP is a fixed 128 MiB FAT32; a UKI over 126 MiB fails with both sizes and `RootFS.verity()` as the fix.

### Sources and toolchains

- `Local("app")` builds a directory beside the image file, pinned by its content digest; an edit is `local-changed`.
- `Git` and `Http` sources are fetched on the host into the workspace; the build sandbox never fetches. `show BUILD` names each toolchain.
- A Go source builds with the toolchain its `go.mod` names, fetched by the host's `go` 1.21+; `Tdxs` is one, so every image needs it.
- A Cargo source builds with the toolchain its `rust-toolchain.toml` pins, installed by the host's `rustup`; an unpinned channel is refused.

### Lint

- Every code `check` reports is a registered rule with its severities, what it checks and how to fix it.
- `check --explain CODE` prints a rule, an image's own component's included, and a text report ends with the `--explain` line for its codes.
- **Breaking:** a component declares its codes in `rules = (Rule(...),)`; an undeclared code is `rule-undeclared`.
- **Breaking:** `component-conflict`, `component-requires-missing` and `image-arch-unsupported` replace the `fragment-` and `recipe-` codes.
- **Breaking:** `service-program-missing` (error) replaces the `service-command-not-shipped` warning: a service's program must be a file, tree or build the image ships, or a program a listed package provides.
- `egress-unfiltered` (warning, a `Firewall` rule): a production profile without `Egress`, or with one allowing `0.0.0.0/0` or `::/0`, sends anywhere; `Resolved.production` tells any component's check which profiles are exempt.
- `volume-tpm-on-cloud` (info): a volume sealed to Azure's or GCP's vTPM names that platform's trust boundary and points at `OperatorKey()` and `BrokerKey()`.
- A finding several profiles share prints once (`[dev, production]`), `[image]` marks an image-wide one, and a shared hint prints once.
- **Breaking:** `source-unpinned` is image-wide with the subject `sources.NAME`, as lock drift reports a source.
- A hint names the lock command that runs where you are: `tundravm lock`, or `tundravm lock FILE` for an image the project does not configure; `local-changed` says `build --dev` re-pins.

### Docs

- `README.md` is one screen, `docs/guide.md` the one guide in the order of the loop, `docs/trust.md` the threat model and recovery.
- `docs/reference/` (commands, API, check codes) is generated from the code; the prose's examples run in the tests.

### Removed

- **Breaking:** the 23 old verbs, without aliases: `init` is `new`; `inspect`, `explain` and `status` are `show`; `lint` and `diff` are `check`.
- **Breaking:** `compile` is `build --tree-only`, `bake` is `build`, `deploy qemu` is `run`, `import` and `units convert` are `convert`, `clean` is `build --clean-cache`.
- **Breaking:** the per-variant tree layouts and the `nethermind-v1` dialect; byte parity with the 2024 nethermind-tdx tree is dropped.
- **Breaking:** the nix and in-process backends; `TUNDRAVM_BACKEND` takes `local` or `lima`.
- **Breaking:** `bake-result.json`, the evidence, release and witness bundles, and placeholder measurements.
- **Breaking:** `check --format` and the Markdown renderers; `check` prints text, `--json` or GitHub annotations.
- **Breaking:** `tundravm.testing.fake_bake` and `bake_in_process`; use `FakeMkosi` and `FakeSources`.
- The pre-v2 docs: the tutorial, concepts, module-authoring, testing, policy and reproducibility pages.
