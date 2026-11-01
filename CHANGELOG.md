# Changelog

## Unreleased

tundravm v2 makes mkosi accessible for confidential VMs by making the choices: Debian
trixie, systemd, a measured UKI, Intel TDX. You declare one Python `Image`, the compiler
writes one plain mkosi 27.1 tree, and twelve commands take it to a locked, reproducible,
signed build that runs, deploys and attests. No compatibility alias is kept. The attested
endpoint has run simulated under QEMU only; no real TDX quote is recorded yet.

### SDK

- **Breaking:** `Image`, `Service`, `Component` and `Profile` replace `Recipe`, `Variant` and `Fragment`.
- **Breaking:** every public name has one home (`tundravm`, `.ports`, `.systemd`, `.components`, `.storage`, `.secrets`, `.sources`, `.errors`, `.testing`); flat imports are gone.
- **Breaking:** `profile` replaces `variant` everywhere, `--json` and `tundravm.testing` included; `show --json` says `image`, `components` and `extends`.
- **Breaking:** the API speaks the public model (`Resolved.targets`, `.components`, `.production`, `class X(Component, name=)`, `ImageFile.component`); the internal-model test helpers and fixtures are gone.
- **Breaking:** `Image(volumes=, secrets=)` are gone: the image collects each volume and secret from what binds it.
- **Breaking:** a profile edit sets any `Service` keyword but `name=` (`readiness=`, `credentials=`, `unit=`, `timer=` and `quotes=` included) and builds what it implies, its probe, credential waits, drop-in, timer and firewall rule and its dependents' ordering; a credential bound through argv or `environment=`, another built binary, another `user=` for a directory on a volume and dropping a readiness another service requires are refused.
- A profile's `service.ref.remove()` drops everything the service lowers to: its unit and user, probe, drop-in, timer and firewall rules; removing a service another one requires is refused.
- **Breaking:** `tundravm.bake` is `tundravm.build`, the command's verb, without an alias; its directory keyword is `into=` (was `build=`).
- **Breaking:** a `Secrets` block and every `Image` with a volume or a secret run the attested endpoint (`tools/cmd/attested-endpoint`, `tundravm-endpoint.service`) instead of `secret-delivery`, which is gone with `tools/pkg/secrets`: its quote binds the nonce, its TLS key, the SSH host keys and the instance public key, and it writes `SecretFile` targets, an operator-key volume's passphrase and the operator's SSH key only on the session that fetched that evidence, once per boot; the built-in provider writes files, `secrets.ready` waits for the required ones, and `sshd` waits only for its host keys.
- `Image` implies the TEE baseline, a curated kernel, a firewall that opens only exposed ports (and 8745 where the attested endpoint listens), one hardened user per service, and `tdxs`.
- `Service` takes an argv; `expose=` alone opens a port; `requires=` waits for real readiness (`Notify`, `TcpProbe`, `HttpProbe`, `service.endpoint(port)` or a component's `self.service(name).endpoint(port)`, or `setup.output(path)`, which skips the service when its setup task wrote nothing); `hardening="python"` names the sandbox per runtime (`static`, `jit`, `node`, `python`, `dotnet`, `relaxed`), `Hardening.preset(NAME, ...)` a deviation; `quotes=True` joins the `tdx` group and requires the evidence service.
- `Service.packaged(unit, package=, hardening=)` runs a Debian package's unit under the image's sandbox, which replaces the package's own; `convert --from systemd` writes it for drop-ins whose unit is not in the set.
- `Hardening(capabilities=["CAP_SYS_TIME"])` also drops `ProtectClock=` and allows `@clock` back, so a time daemon's grant takes effect; the boot probe's `service:` failure names a denied capability when the unit's status shows one.
- `Debian(repositories=[AptRepository(url, suite, key=)])` adds a signed archive, dated by the base's snapshot on snapshot.debian.org or pinned by `sha256=` of its `InRelease`, which the build checks; `convert --from mkosi` writes one for each signed, dated deb822 source of a `SandboxTrees=` tree.
- An `AptRepository` keyed on a `debian-archive-keyring` keyring puts that package in the tools tree, where the build's apt reads it.
- `Build(command=[...])` runs one program in the build environment, beside `script=` and `using=`, and `Image(files=)` takes a `Build`, shipping what its `install=` names without a component.
- A list wherever a public value's docs show one type-checks: `Build(command=, packages=, install=)`, `User(groups=)`, `DropIn`'s lists, `Hardening(devices=, capabilities=, syscalls=)`, `GoBinary(tags=)` and `CargoBinary(features=)` take a `Sequence` and keep a tuple.
- `Component` is a dataclass with `compose()`, typed exports and an optional `check()`; `Profile(extends=, add=, changes=, production=)` edits with `value.ref.edit(...)` and `.remove()`.
- `Volume` (at `/persistent/<name>`; `unlock=` has no default) with `OperatorKey`, `TpmSeal` or `BrokerKey`; `Secret` as a systemd credential, bound by naming it in argv or `environment=`; `AttestedTLS` and `TLS`; `RootFS.verity()` and `RootFS.initramfs(max_size=)`; `Kernel(cmdline=)`; `Mkosi(settings=, scripts=)`, refusing what the compiler writes.
- Every image generates a fresh secp256k1 instance key at boot (tmpfs, root-only); `InstanceKey()` (`tundravm.secrets`) hands it to a service as a credential, `instance-key-exposed` refuses anything else naming it, and `run status`, `attest` and `show` print its public key and who takes it.
- `Generated(name)` (`tundravm.secrets`) is a secret the image writes at boot for the services that bind it: `Nethermind(jwt=Generated("jwt"))` and its consensus client need no delivered JWT.
- `Lighthouse(execution=, jwt=, checkpoint_sync_url=)` runs the v8.3.2 release as a beacon node on `hoodi` (or another network it ships) beside `Nethermind`, dialing its peers without accepting any unless `expose_p2p=True`.
- `Mkosi(settings=)` writes each key in its mkosi section, and a key tundravm writes is refused with the public slot that sets it.
- The shipped library: `TeeBase`, `Network`, `Firewall`, `Tdxs`, `SshAccess`, `DevAccess`, `Observability`, `Nethermind`, `TaikoClient`, `Raiko`; `tundravm.testing` with `plan`, the asserts, `FakeMkosi`, `FakeSources`, `boot`, `assert_probe` and `run_cli`.

### Network and clouds

- `Network(egress=Egress(...))` drops every other outbound packet and accepts the baseline's NTS time, resolvers and cloud metadata; `Egress(hosts=)` takes `HOST:PORT`.
- `Component.outbound()` says where a component sends, and a filtered image accepts it in each profile that holds it, each rule naming who asked: the shipped components cover their sinks, peers and collateral, `TaikoClient(l1=)` and `Raiko(l1=)` name their L1, and `Observability(proxy=)` sends through one proxy.
- `Observability(proxy=Proxy(address, port, tls_name=))` reaches a proxy that speaks TLS at `https://<tls_name>:<port>`, the name mapped to the address in `/etc/hosts`.
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
- `run` forwards, and `deploy`'s firewall line names, 8745 only where the image's firewall opens it.
- `deploy azure|gcp --build BUILD` never guesses the build; `upgrade` and `rollback` replace a GCP instance and move its volumes.
- **Breaking:** `attest`, `secrets deliver` and `secrets unlock` speak TLS to the attested endpoint instead of relaying over SSH: a fresh nonce, evidence for another nonce or TLS key refused, a simulator's quote checked by its tag (`simulated`, registers never compared), `known_hosts` from the bound host keys, the instance key printed as bound to the quote; `secrets` verifies again on its own session and writes on it alone, and loses `--identity`.
- `tundravm.testing.FakeEndpoint` plays the endpoint, with `relay`, `replay` and `wrong-key` misbehaviours.
- `doctor` prints one row per capability with its fix; `build:go` blocks without `go`.
- **Breaking:** `convert --from systemd` prints an `Image` file, without `--mode`, with `hardening="<name>"` where a unit's sandbox is exactly a preset; `convert --from mkosi` converts a tundravm tree's payload and its cloud wrappers into `targets=`, dropping the packages every image implies; both write double-quoted Python.
- Every error prints its code and a hint naming the command that gets past it; a closed pipe exits 141 quietly; a misspelt verb gets a "did you mean".

### Build and lock

- `build` publishes `build/<image>/production/<id>/` once, read-only, with a canonical `manifest.json` whose digest is `<id>`, `measurements.json` (a `tundravm.policy/2` policy per record), an SPDX SBOM, `observations.json` and a report of artifacts, sources, toolchains, phases and predicted registers.
- **Breaking:** the manifest is `tundravm.manifest/2`: `image_digest` replaces `recipe_digest`, in `show BUILD --json` and `lock --json` too; an older build directory is refused, so build again.
- **Breaking:** a production build needs `build/tundravm.lock` (`lock-missing`); a development build writes a missing one and re-pins `Local` sources.
- **Breaking:** lockfile version 7 names profile sections `profiles.<name>` and the payload and its digest `image` and `image_digest`, as the manifest does; an older lock is refused, so run `tundravm lock` again.
- **Breaking:** the default Debian snapshot moves from `20251113T083151Z` to `20261018T082309Z`; `Debian(snapshot=)` keeps the old one.
- `lock` prints the snapshot, each source's ref and pin and the toolchain; `build --tree-only` without a lock says its tree will drift.
- `lock --check` never writes or fetches and exits 1 on drift; `build --reproduce`, `--sign`, `--offline` and `verify` check the bytes anywhere.
- The ESP is a fixed 128 MiB; a UKI over 126 MiB fails, naming `RootFS.verity()`.
- `Kernel.build` compiles in the image and fails when a driver a target needs is not `=y` or a denied one is on, keeping its configs in the build.
- `examples/nethermind_base.py`'s kernel config builds in every driver a target needs; a weekly workflow builds it and Debian's newest 6.12 (6.12.113, from the security pool).
- `DebianConfig` reads the snapshot's main pool, then its security pool, which alone carries a kernel shipped as a security update.
- A dev profile's root password (`DevAccess(password=)`, `DevTools`) is set inside the image with `mkosi-chroot chpasswd -e`, so the build no longer fails locking `/etc/passwd`; it logs in on the console only, unless `DevTools(network_login=True)` opens SSH to it.
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
