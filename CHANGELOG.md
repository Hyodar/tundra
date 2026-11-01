# Changelog

## Unreleased

tundravm v2 makes mkosi accessible for confidential VMs by making the choices: Debian
trixie, systemd, a measured UKI, Intel TDX. You declare one Python `Image`, the compiler
writes one plain mkosi 27.1 tree, and twelve commands take it to a locked, reproducible,
signed build that runs, deploys and attests. No compatibility alias is kept. Until real TDX
quotes and the attested channel land, `attest` and `secrets` say `unverified first contact`.

### SDK

- **Breaking:** `Image`, `Service`, `Component` and `Profile` replace `Recipe`, `Variant` and `Fragment`.
- **Breaking:** every public name has one home (`tundravm`, `.ports`, `.systemd`, `.components`, `.storage`, `.secrets`, `.sources`, `.errors`, `.testing`); flat imports are gone.
- **Breaking:** `profile` replaces `variant` everywhere, `--json` and `tundravm.testing` included; `show --json` says `image`, `components` and `extends`.
- **Breaking:** the API speaks the public model (`Resolved.targets`, `.components`, `.production`, `class X(Component, name=)`, `ImageFile.component`); the internal-model test helpers and fixtures are gone.
- **Breaking:** `Image(volumes=, secrets=)` are gone: the image collects each volume and secret from what binds it.
- **Breaking:** a profile edit that binds or unbinds a credential, or gives a service whose state is on a volume another `user=`, is refused.
- **Breaking:** `tundravm.bake` is `tundravm.build`, the command's verb, without an alias.
- `Image` implies the TEE baseline, a curated kernel, a firewall that opens only exposed ports (and 8745 where the SSH key delivery listens), one hardened user per service, and `tdxs`.
- `Service` takes an argv; `expose=` alone opens a port; `requires=` waits for real readiness (`Notify`, `TcpProbe`, `HttpProbe`, `service.endpoint(port)` or a component's `self.service(name).endpoint(port)`, or `setup.output(path)`, which skips the service when its setup task wrote nothing); `hardening="python"` names the sandbox per runtime (`static`, `jit`, `node`, `python`, `dotnet`, `relaxed`), `Hardening.preset(NAME, ...)` a deviation; `quotes=True` joins the `tdx` group and requires the evidence service.
- `Component` is a dataclass with `compose()`, typed exports and an optional `check()`; `Profile(extends=, add=, changes=, production=)` edits with `value.ref.edit(...)` and `.remove()`.
- `Volume` (at `/persistent/<name>`; `unlock=` has no default) with `OperatorKey`, `TpmSeal` or `BrokerKey`; `Secret` as a systemd credential, bound by naming it in argv or `environment=`; `AttestedTLS` and `TLS`; `RootFS.verity()` and `RootFS.initramfs(max_size=)`; `Kernel(cmdline=)`; `Mkosi(settings=, scripts=)`, refusing what the compiler writes.
- The shipped library: `TeeBase`, `Network`, `Firewall`, `Tdxs`, `SshAccess`, `DevAccess`, `Observability`, `Nethermind`, `TaikoClient`, `Raiko`; `tundravm.testing` with `plan`, the asserts, `FakeMkosi`, `FakeSources`, `boot`, `assert_probe` and `run_cli`.

### Network and clouds

- `Network(egress=Egress(...))` drops every other outbound packet and accepts the baseline's NTS time, resolvers and cloud metadata; `Egress(hosts=)` takes `HOST:PORT`.
- `Component.outbound()` says where a component sends, and a filtered image accepts it in each profile that holds it, each rule naming who asked: the shipped components cover their sinks, peers and collateral, `TaikoClient(l1=)` and `Raiko(l1=)` name their L1, and `Observability(proxy=)` sends through one proxy.
- The SSH key delivery on 8745 takes one key once per boot, then the firewall drops the port.
- The GCP glue names NVMe disks as Google does, sets the hostname and routes forwarded IPs; the Azure glue installs the `curl` its ready report needs.

### CLI

- **Breaking:** twelve commands: `new`, `check`, `lock`, `build`, `show`, `run`, `deploy`, `attest`, `secrets`, `verify`, `convert`, `doctor`.
- `new NAME [--template service|prover] [--ci]` writes a project whose printed loop, README, tests and workflow run as written, `ruff format` clean; the `service` template builds your Go program from `app/`, the prover adds a volume, a secret, attested TLS and `policies/`.
- The `--ci` workflow checks the image, the lock and the committed tree, and builds when `TUNDRAVM_BACKEND` is set.
- **Breaking:** `show IMAGE` lists the components the image file wrote, each with what it composed (`--json`: `{"name", "components"}`), and a source build only under sources.
- **Breaking:** `show --dev` is always the latest development build; an image file's dev profile is `--profile dev`, and `show IMAGE --dev` is refused.
- `show` leads with the image, then production; with a build, its report and boot probe checks; `--diff` compares two builds.
- `run --mode simulated|tdx` is explicit, and a production build boots fail-closed without TDX; `run --probe` and `run probe INSTANCE` read the boot probe every development profile carries, write `probe.json` and exit 1 on a failed check.
- With `Observability`, the probe checks each exporter runs (or retries an absent sink) on the configuration its program took, blocks no service and has its sinks' egress loaded.
- `deploy azure|gcp --build BUILD` never guesses the build; `upgrade` and `rollback` replace a GCP instance and move its volumes.
- `attest` verifies fresh evidence and delivers nothing; `secrets` attests again first.
- `doctor` prints one row per capability with its fix; `build:go` blocks without `go`.
- **Breaking:** `convert --from systemd` prints an `Image` file, without `--mode`, with `hardening="<name>"` where a unit's sandbox is exactly a preset; `convert --from mkosi` converts a tundravm tree's payload and its cloud wrappers into `targets=`, dropping the packages every image implies; both write double-quoted Python.
- Every error prints its code and a hint naming the command that gets past it; a closed pipe exits 141 quietly; a misspelt verb gets a "did you mean".

### Build and lock

- `build` publishes `build/<image>/production/<id>/` once, read-only, with a canonical `manifest.json` whose digest is `<id>`, `measurements.json` (a `tundravm.policy/2` policy per record), an SPDX SBOM, `observations.json` and a report of artifacts, sources, toolchains, phases and predicted registers.
- **Breaking:** the manifest is `tundravm.manifest/2`: `image_digest` replaces `recipe_digest`, in `show BUILD --json` and `lock --json` too; an older build directory is refused, so build again.
- **Breaking:** a production build needs `build/tundravm.lock` (`lock-missing`); a development build writes a missing one and re-pins `Local` sources.
- **Breaking:** lockfile version 6 names profile sections `profiles.<name>`; an older lock is refused.
- **Breaking:** the default Debian snapshot moves from `20251113T083151Z` to `20261018T082309Z`; `Debian(snapshot=)` keeps the old one.
- `lock` prints the snapshot, each source's ref and pin and the toolchain; `build --tree-only` without a lock says its tree will drift.
- `lock --check` never writes or fetches and exits 1 on drift; `build --reproduce`, `--sign`, `--offline` and `verify` check the bytes anywhere.
- The ESP is a fixed 128 MiB; a UKI over 126 MiB fails, naming `RootFS.verity()`.
- `Kernel.build` compiles in the image and fails when a driver a target needs is not `=y` or a denied one is on, keeping its configs in the build.
- `examples/nethermind_base.py`'s kernel config builds in every driver a target needs; a weekly workflow builds it and Debian's 6.12.111.
- `Local("app")` sources are pinned by content digest (an edit is `local-changed`); `Git` and `Http` are fetched on the host, never in the sandbox.
- `GoBinary` builds the module root unless `package=` names a sub-command.
- **Breaking:** `Tdxs` and the volume and delivery tools build from `tools/` of this repository (`Git(url, sdk_commit, subdir="tools")`, or the checkout's `tools/` as a `Local`), not `Hyodar/tundra-tools` master; `Git(subdir=)` must name a directory of the commit.
- Go builds with the toolchain `go.mod` names, fetched by the host's `go` 1.21+, which every image needs (`Tdxs` is Go); Cargo with the one `rust-toolchain.toml` pins, installed by the host's `rustup`, and an unpinned channel is refused.

### Lint

- Every code is a registered rule with its severities and a fix in public spelling; `check --explain CODE` prints one, a component's own included.
- **Breaking:** a component declares its codes in `rules = (Rule(...),)`; an undeclared code is `rule-undeclared`.
- **Breaking:** `component-conflict`, `component-requires-missing` and `image-arch-unsupported` replace the `fragment-` and `recipe-` codes.
- **Breaking:** `service-program-missing` (error) replaces `service-command-not-shipped`: a service's program must be something the image ships.
- **Breaking:** `source-unpinned` is image-wide, subject `sources.NAME`.
- New: `egress-unfiltered`, `volume-tpm-on-cloud`, `unit-written-at-runtime`.
- A finding several profiles share prints once, `[image]` marks an image-wide one, the report ends with its `--explain` line, and a hint names the lock command that runs where you are.

### Docs

- `README.md`, `docs/guide.md` in the order of the loop, `docs/trust.md` for the threat model and recovery, and `docs/reference/` generated from the code; the prose's examples run in the tests.

### Removed

- **Breaking:** the 23 old verbs, without aliases: `init` is `new`; `inspect`, `explain` and `status` are `show`; `lint` and `diff` are `check`.
- **Breaking:** `compile` is `build --tree-only`, `bake` is `build`, `deploy qemu` is `run`, `import` and `units convert` are `convert`, `clean` is `build --clean-cache`.
- **Breaking:** the per-variant tree layouts and the `nethermind-v1` dialect.
- **Breaking:** the nix and in-process backends; `TUNDRAVM_BACKEND` takes `local` or `lima`.
- **Breaking:** `bake-result.json`, the evidence, release and witness bundles, and placeholder measurements.
- **Breaking:** `check --format` and the Markdown renderers.
- **Breaking:** `tundravm.testing.fake_bake` and `bake_in_process`; use `FakeMkosi` and `FakeSources`.
- The pre-v2 docs pages.
- `tundravm.deploy.qemu.QemuDeployAdapter`, which no command reached: `run` launches QEMU.
