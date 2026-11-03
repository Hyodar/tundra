# Changelog

`## Unreleased` holds the next release's notes, one grouped note under `###` sections.
`scripts/release.py cut X.Y.Z` renames it `## X.Y.Z - <date>` and opens an empty one; the
`vX.Y.Z` tag's release body is that section, and a tag without one is refused
([docs/release.md](docs/release.md)).

## Unreleased

tundravm v2 makes mkosi accessible for confidential VMs by making the choices: Debian
trixie, systemd, a measured UKI, Intel TDX. You declare one Python `Image`, the compiler
writes one plain mkosi 27.1 tree, and thirteen commands take it to a locked, reproducible,
signed build that runs, deploys and attests. No compatibility alias is kept. The attested
endpoint has run simulated under QEMU only; no real TDX quote is recorded yet.

### SDK

- **Breaking:** `Image`, `Service`, `Component` and `Profile` replace `Recipe`, `Variant` and `Fragment`.
- **Breaking:** every public name has one home (`tundravm`, `.ports`, `.systemd`, `.components`, `.storage`, `.secrets`, `.sources`, `.errors`, `.testing`); flat imports are gone.
- **Breaking:** `profile` replaces `variant` everywhere, `--json` and `tundravm.testing` included; `show --json` says `image`, `components` and `profiles`.
- **Breaking:** the API speaks the public model (`Resolved.targets`, `.components`, `.production`, `class X(Component, name=)`, `ImageFile.component`); the internal-model test helpers and fixtures are gone.
- **Breaking:** `Image(volumes=, secrets=)` are gone: the image collects each volume and secret from what binds it.
- **Breaking:** a profile edit sets any `Service` keyword but `name=` (`readiness=`, `credentials=`, `unit=`, `timer=` and `quotes=` included) and builds what it implies, its probe, credential waits, drop-in, timer and firewall rule and its dependents' ordering; a credential bound through argv or `environment=`, another built binary, another `user=` for a directory on a volume and dropping a readiness another service requires are refused.
- A profile's `service.ref.remove()` drops everything the service lowers to: its unit and user, probe, drop-in, timer and firewall rules; removing a service another one requires is refused, and so is a `requires=` on the `.ready` of a service the image does not declare.
- **Breaking:** `tundravm.bake` is `tundravm.build`, the command's verb, without an alias; its directory keyword is `into=` (was `build=`).
- **Breaking:** a `Secrets` block and every `Image` with a volume or a secret run the attested endpoint (`tools/cmd/attested-endpoint`, `tundravm-endpoint.service`) instead of `secret-delivery`, which is gone with `tools/pkg/secrets`: its quote binds the nonce, its TLS key, the SSH host keys and the instance public key, and it writes `SecretFile` targets, an operator-key volume's passphrase and the operator's SSH key only on the session that fetched that evidence, once per boot; the built-in provider writes files, `secrets.ready` waits for the required ones, and `sshd` waits only for its host keys.
- **Breaking:** an `OperatorKey()` volume's LUKS2 header pins its operator: the first signed write to the attested endpoint stores the signer's SSH key as a `tundravm-owner` token (slot 2), `tundravm-volume-owner.service` (`disk-setup owner`) reads it before the endpoint starts and writes pins and rotations back, and the endpoint then takes credentials, passphrases and the SSH key only when signed by that key, so a reboot no longer hands root to whoever reaches it first; `disk-setup` creates and opens such a volume only for the boot's owner and closes it again when its header names another key than the owner copy it keeps inside (`/.tundravm/owner`; a volume made before this change gets the copy at its first unlock), and the dev probe's `volume:` row names it (`owner SHA256:...`) and skips while the volume waits for an unlock. `TpmSeal` and `BrokerKey` volumes pin no one.
- An `AttestedTLS` ingress brings the attested endpoint, whose quote also binds the certificate key of each attested TLS port, made at boot by `attested-tls-<port>-key.service` and read by the relay as credentials, after the instance key and in port order; `Secrets(attested_tls=)` names the ports in a declarative recipe, the dev probe's `attested-tls:` row reads each certificate back against the evidence, and an ingress relay starts only once its upstream's readiness holds.
- **Breaking:** `File(owner=)` and `Tree(owner=)` are applied at boot by the runtime-init step `owners`, which the services wait for, not by `/etc/tmpfiles.d/50-tundravm-owned.conf`, which nothing applied: the debloat masks boot-time tmpfiles.
- `Image` implies the TEE baseline, a curated kernel, a firewall that opens only exposed ports (and 8745 where the attested endpoint listens), one hardened user per service, and `tdxs`.
- **Breaking:** `Tdxs(issuer=)` is gone from `tundravm.components`: a development profile ships the `simulator` issuer beside the real one and takes it at boot where the execution gate finds no TDX, so a dev build attests `simulated` under QEMU; a production profile ships the real issuer only, and the declarative `Tdxs` refuses `simulator`.
- **Breaking:** `Service(egress=Egress(...))` accepts outbound traffic for that service's user alone (nftables `meta skuid`), beside the image-wide `Egress`; the cloud metadata servers (169.254.169.254, `fd20:ce::254`, Azure's wire server) take only what a service declaring `Egress(metadata=True)` and tundravm's cloud glue (root, each unit with a group of its own) send, filtered image or not, so no other process fetches the identity token; `Egress(hosts=)` naming them and an image-wide `metadata=True` are refused, `service-metadata` warns on a service that declares it, and the boot probe's `egress:metadata:` rows send to it as each service's user.
- `Service` takes an argv; `expose=` alone opens a port; `requires=` waits for real readiness (`Notify`, `TcpProbe`, `HttpProbe`, `service.endpoint(port)` or a component's `self.service(name).endpoint(port)`, or `setup.output(path)`, which skips the service when its setup task wrote nothing); `hardening="python"` names the sandbox per runtime (`static`, `jit`, `node`, `python`, `dotnet`, `relaxed`), `Hardening.preset(NAME, ...)` a deviation; `quotes=True` joins the `tdx` group and requires the evidence service.
- `TcpProbe` and `HttpProbe` read `interval=` as a systemd time span (`"500ms"` by default), as `timeout=` and `Every()` do; a span systemd does not read, zero or `infinity` is refused at construction.
- `HttpProbe(url)` refuses a URL that names no host (`"http://"`), which constructed and polled nothing.
- `Service.packaged(unit, package=, hardening=)` runs a Debian package's unit under the image's sandbox, which replaces the package's own; `convert --from systemd` writes it for drop-ins whose unit is not in the set.
- `Hardening(capabilities=["CAP_SYS_TIME"])` also drops `ProtectClock=` and allows `@clock` back, so a time daemon's grant takes effect; the boot probe's `service:` failure names a denied capability when the unit's status shows one.
- `Debian(repositories=[AptRepository(url, suite, key=)])` adds a signed archive, dated by the base's snapshot on snapshot.debian.org or pinned by `sha256=` of its `InRelease`, which the build checks; `convert --from mkosi` writes one for each signed, dated deb822 source of a `SandboxTrees=` tree.
- `lock` records each `AptRepository(sha256=)`'s `InRelease` digest and date, refusing an archive that moved; `lock --check` reports a pin that differs from the record, and `build --offline` hands the recorded digest to the build's check instead of fetching the file.
- An `AptRepository` keyed on a `debian-archive-keyring` keyring puts that package in the tools tree, where the build's apt reads it.
- `Build(command=[...])` runs one program in the build environment, beside `script=` and `using=`, and `Image(files=)` takes a `Build`, shipping what its `install=` names without a component.
- A list wherever a public value's docs show one type-checks: `Build(command=, packages=, install=)`, `User(groups=)`, `DropIn`'s lists, `Hardening(devices=, capabilities=, syscalls=)`, `GoBinary(tags=)` and `CargoBinary(features=)` take a `Sequence` and keep a tuple.
- `Component` is a dataclass with `compose()`, typed exports and an optional `check()`; `Profile(extends=, add=, changes=, production=)` edits with `value.ref.edit(...)` and `.remove()`.
- `Volume` (at `/persistent/<name>`; `unlock=` has no default) with `OperatorKey`, `TpmSeal` or `BrokerKey`; `Secret` as a systemd credential, bound by naming it in argv or `environment=`; `AttestedTLS` and `TLS`; `RootFS.verity()` and `RootFS.initramfs(max_size=)`; `Kernel(cmdline=)`; `Mkosi(settings=, scripts=)`, refusing what the compiler writes.
- Every image generates a fresh secp256k1 instance key at boot (tmpfs, root-only); `InstanceKey()` (`tundravm.secrets`) hands it to a service as a credential, `instance-key-exposed` refuses anything else naming it, and `run status` and `attest` print its public key while `show` says who takes it.
- `Generated(name)` (`tundravm.secrets`) is a secret the image writes at boot for the services that bind it, such as the Engine API JWT an execution client and its consensus client share.
- **Breaking:** `PrometheusRemoteWrite(export=)` is the keep-list of series that leave the TEE: series names and the `health` preset, dropped otherwise by `write_relabel_configs`; without it the agent keeps `health`, and `observability-export-unset` refuses a production profile that writes to a `url=`.
- The metrics export always carries the image's identity, which `image-metrics` (`tools/cmd/image-metrics`) writes for the node exporter: `tundravm_image_digest` (the UKI's Authenticode SHA-384 RTMR1's event log records, and the image's name), `tundravm_image_rtmr`, `tundravm_mode`; `health` also keeps `tundravm_system_state` and `tundravm_units_failed`.
- `JournalExport()` and `PrometheusRemoteWrite()` without `url=` ship to a receiver in the guest (`journal-sink`, Debian's `systemd-journal-remote`; `metrics-sink`, Prometheus's remote-write receiver), both on the loopback, so a dev boot shows each export working; the credential binds once `url=` names the deployment's receiver, and a production profile reports `observability-sink-unset` until then.
- **Breaking:** a relative host path in an image file (`File.copy(src, ...)`, `Tree(src, ...)`, `Kernel.build(config=, patches=)`, `AptRepository(key=Path(...))`) is beside the file that declares it, as `Local` is, not the working directory, so a build reads the same files from any directory.
- An image file imports modules from its own directory and its project's (the nearest `pyproject.toml`), from any working directory: a project carries its own components.
- `Mkosi(settings=)` writes each key in its mkosi section, and a key tundravm writes is refused with the public slot that sets it.
- **Breaking:** every error is one shape in the image file's words: what is wrong, naming the public object and keyword (`Service("app"): argv= is empty`), then a `fix:` line; `tundravm` prints it as one block with the image file's line and exits 2, as a crash or a usage error does, while a failed check exits 1; a misspelt keyword, a missing argument or a positional too many is a `ValidationError` with the closest keyword, and two services exposing one port and two services with one name are refused.
- The shipped library: `TeeBase`, `Network`, `Firewall`, `Tdxs`, `SshAccess`, `DevAccess`, `Observability`; `tundravm.testing` with `plan`, the asserts, `FakeMkosi`, `FakeSources`, `boot`, `assert_probe` and `run_cli`.

### Network and clouds

- `Network(egress=Egress(...))` drops every other outbound packet and accepts the baseline's NTS time, resolvers and cloud metadata; `Egress(hosts=)` takes `HOST:PORT`.
- `Component.outbound()` says where a component sends, and a filtered image accepts it in each profile that holds it, each rule naming who asked: the shipped components cover their sinks and collateral, and `Observability(proxy=)` sends through one proxy.
- `Observability(proxy=Proxy(address, port, tls_name=))` reaches a proxy that speaks TLS at `https://<tls_name>:<port>`, the name mapped to the address in `/etc/hosts`.
- The GCP glue names NVMe disks as Google does, sets the hostname and routes forwarded IPs; the Azure glue installs the `curl` its ready report needs.

### CLI

- **Breaking:** `new NAME [--ci]` writes stage 1 of the attested node, its tests and `pyproject.toml`, and nothing else: the `minimal`, `service`, `cloud` and `prover` templates and `--template` are gone. Its printed loop, README, tests and workflow run as written, `ruff format` clean, and end at `tundravm dev`.
- **Breaking:** thirteen commands: `new`, `check`, `lock`, `build`, `show`, `run`, `dev`, `deploy`, `attest`, `secrets`, `verify`, `convert`, `doctor`.
- `new --ci` writes `.github/workflows/image.yml` (was `tundravm.yml`): every push checks `uv.lock`, the image, the lock and the committed tree, then builds the production profile and uploads its manifest, measurements and SBOM; a `v*` tag rebuilds that build on a second runner with `verify --rebuild`, attests the provenance of every released file and creates a release with the image, the manifest, `measurements.json`, the SBOM and `SHA256SUMS`. Its runner installs the packages `doctor` names.
- **Breaking:** `show IMAGE` reads like the image file: components by class and keyword, services with their program, source, ports, hardening, volume, credentials and probe, profiles with what each changes, then what each profile builds (build and boot steps said by what they do), a profile beyond the one it extends; no internal fragment, probe unit or shell.
- **Breaking:** `show --dev` is the image's dev profile, as `build --dev`; the latest development build is `show latest-development`, and `show BUILD --dev` is refused.
- **Breaking:** `show IMAGE --json` mirrors the text: `image`, `file`, `digest`, `base`, `components`, `services`, `ingress`, `content`, `profiles`, `resolved` and `provenance`, which names each declaration as the image file wrote it (`GoBinary('server')`, `Service('app').readiness`) and the class of each component it came through.
- `show --diff OLD NEW` compares two image files too, and prints only the lines that differ, section by section; with a build, `show` prints its report and boot probe checks.
- `dev [IMAGE]` is the inner loop as one verb: `build --dev` when the image, the lock or a `Local` source's content changed since the latest development build (it says which, else reuses that build), then `run --mode simulated --probe` on it and the console until Ctrl-C stops the instance; `--no-follow` returns after the probe; `new`, the quickstart and the README print it.
- `run --mode simulated|tdx` is explicit, and a production build boots fail-closed without TDX; `run --probe` and `run probe INSTANCE` read the boot probe every development profile carries, write `probe.json` and exit 1 on a failed check.
- With `Observability`, the probe checks each exporter runs (or retries an absent sink) on the configuration its program took, blocks no service and has its sinks' egress loaded, and that a loopback metrics sink holds `tundravm_image_digest` and no series the keep-list drops (`observability:series`).
- `run` forwards, and `deploy`'s firewall line names, 8745 only where the image's firewall opens it.
- A local instance's QEMU monitor listens in `$XDG_RUNTIME_DIR/tundravm` (else `/tmp/tundravm-<uid>`, refused unless only you can enter it), named in the receipt, not under the project, so a deeply nested project can start one.
- `deploy azure|gcp --build BUILD` never guesses the build; `upgrade` and `rollback` replace a GCP instance and move its volumes.
- `deploy azure|gcp --build BUILD --plan` prints each step (upload with size and digest, image, instance and its confidential flags, volumes, firewall, the build's policy for that cloud, what `status` polls) and every `gcloud` or `az` call in order, without credentials, and runs none; `--json` gives the same as data. An Azure deploy records the address `az vm create` prints and adds no network security group rule (`--nsg-rule NONE`): the image's firewall decides what is open.
- **Breaking:** `attest`, `secrets deliver` and `secrets unlock` speak TLS to the attested endpoint instead of relaying over SSH: a fresh nonce, evidence for another nonce or TLS key refused, a simulator's quote checked by its tag (`simulated`, registers never compared), `known_hosts` from the bound host keys, the instance key printed as bound to the quote; `secrets` verifies again on its own session and writes on it alone, and loses `--identity`.
- **Breaking:** for an image with an `OperatorKey()` volume, `secrets deliver` and `secrets unlock` sign each write's nonce with the key the last `attest` used (`--identity`, recorded in the receipt; `ssh-keygen -Y sign`, namespace `tundravm-endpoint`) and refuse before sending when the instance is owned by another key or its volume came back without the owner the receipt names; `secrets rotate INSTANCE --identity KEY` hands the volumes to another key; `attest`, `secrets unlock`, the receipt and `run status` name the owner's fingerprint.
- `attest` prints what a `simulated` verdict does not prove, and `run status` and `run mode` name the tdxs issuer an instance uses.
- `attest --tls PORT` also connects to the `AttestedTLS` listener on that port and refuses unless its certificate carries the key the same evidence binds, printing the certificate's `sha256:` fingerprint in the text, `--json` and the receipt; `tools/pkg/verify` (one function, `AttestedTLS`) and `go run ./tools/cmd/attested-client https://HOST:PORT` give a relying party the same check and verdicts in Go, a TDX quote's chain checked against Intel's root.
- `tundravm.testing.FakeEndpoint` plays the endpoint, presenting each listener's certificate, with `relay`, `replay`, `wrong-key` and `wrong-certificate` misbehaviours.
- `doctor` prints one row per capability, then a `fix:` block with the one command that clears each row (`sudo apt install golang`, `sudo apt install qemu-system-x86 ovmf`, the user-namespace sysctl, the cloud login), in `--json` too; `build:go` blocks without `go`.
- `tundravm` with no arguments prints the first four commands, `doctor`, `new`, `check` and `dev`, and points at `--help`.
- `dev` and `run --mode simulated` ask for QEMU and OVMF before anything builds, and name every missing apt package in one command.
- Ctrl-C or SIGTERM stops the instance `dev` or `run` follows, even when the command was started as a script's `&` job, which inherits SIGINT ignored; SIGTERM used to leave QEMU running.
- `convert` breaks long lines as `ruff format` does where ruff is not installed, as in a `uv tool install`.
- **Breaking:** `convert --from systemd` prints an `Image` file, without `--mode`, with `hardening="<name>"` where a unit's sandbox is exactly a preset; both conversions write double-quoted Python.
- **Breaking:** `convert --from mkosi` ends with one line per setting the tree sets, `kept` (the image's tree resolves it to the same value), `mapped` (the slot, named), `defaulted` (the opinion, named) or `refused` (why, and what to do), the files mkosi read for no selection included; the `# unsupported:` lines are gone, and `--json` prints source and report as one object.
- `convert --from mkosi` maps `mkosi.profiles/` to `profiles=`, the units inside `mkosi.extra/` to `Service` values left out of the `Tree`, `KernelCommandLine=` and `KernelModules=` to `Kernel.default(cmdline=, modules=)`, `Snapshot=` to `Debian(snapshot=)`, `Repositories=` to an `AptRepository`, `VolatilePackages=` to packages, and `Locale=`, `Timezone=`, `Hostname=` and the like to `Mkosi(settings=)`; a tree naming no distribution is resolved for Debian and says so.
- `convert --from mkosi` on a tundravm tree converts the payload and its cloud wrappers into `targets=`, drops the packages every image implies, and refuses what the Image file generated.
- Every error prints its code and a hint naming the command that gets past it; a closed pipe exits 141 quietly; a misspelt verb gets a "did you mean".

### Build and lock

- `build` publishes `build/<image>/production/<id>/` once, read-only, with a canonical `manifest.json` whose digest is `<id>`, `measurements.json` (a `tundravm.policy/2` policy per record), an SPDX SBOM, `observations.json` and a report of artifacts, sources, toolchains, phases and predicted registers.
- **Breaking:** the manifest is `tundravm.manifest/2`: `image_digest` replaces `recipe_digest`, in `show BUILD --json` and `lock --json` too; an older build directory is refused, so build again.
- **Breaking:** a production build needs `build/tundravm.lock` (`lock-missing`); a development build writes a missing one and re-pins `Local` sources.
- A development build (`build --dev`, `dev`) is incremental: mkosi keeps the base tree after package install under `build/<image>/.work/.mkosi/incremental/`, with the Go and cargo caches, keyed on what its own cache manifest leaves out, and the next build of a changed file, unit or `Local` source reuses it and says so (`cached: packages (...)`); the manifest says `incremental`, and a production build never reads it.
- **Breaking:** lockfile version 7 names profile sections `profiles.<name>` and the payload and its digest `image` and `image_digest`, as the manifest does; an older lock is refused, so run `tundravm lock` again.
- **Breaking:** the default Debian snapshot moves from `20251113T083151Z` to `20261018T082309Z`; `Debian(snapshot=)` keeps the old one.
- `lock` prints the snapshot, each source's ref and pin and the toolchain; `build --tree-only` without a lock says its tree will drift.
- `lock --check` never writes or fetches and exits 1 on drift; `build --reproduce`, `--sign`, `--offline` and `verify` check the bytes anywhere.
- An initramfs disk's ESP is sized to hold `RootFS.initramfs(uki_max_size=)`, 256M by default (272 MiB); verity's stays 128 MiB, and a UKI past it fails naming `uki_max_size=`.
- `Kernel.build` compiles in the image and fails when a driver a target needs is not `=y` or a denied one is on, keeping its configs in the build.
- A kernel fixture with its own config builds in every driver a target needs; a weekly workflow builds it and Debian's newest 6.12 (6.12.113, from the security pool).
- `DebianConfig` reads the snapshot's main pool, then its security pool, which alone carries a kernel shipped as a security update.
- A dev profile's root password (`DevAccess(password=)`, `DevTools`) is set inside the image with `mkosi-chroot chpasswd -e`, so the build no longer fails locking `/etc/passwd`; it logs in on the console only, unless `DevTools(network_login=True)` opens SSH to it.
- `Local("app")` sources are pinned by content digest (an edit is `local-changed`); `Git` and `Http` are fetched on the host, never in the sandbox.
- `GoBinary` builds the module root unless `package=` names a sub-command.
- **Breaking:** `Tdxs` and the volume and delivery tools build from `tools/` of this repository (`Git(url, sdk_commit, subdir="tools")`, or the checkout's `tools/` as a `Local`), not `Hyodar/tundra-tools` master; `Git(subdir=)` must name a directory of the commit.
- The version is the release tag (`tundravm --version`; `<tag>.post1.dev<n>+g<sha>` between tags), and a wheel records the commit it was built from, so an installed release builds `tools/` at its tag's commit; the lock's compiler section records that version, so another tundravm is lock drift.
- Go builds with the toolchain `go.mod` names, fetched by the host's `go` 1.21+, which every image needs (`Tdxs` is Go); Cargo with the one `rust-toolchain.toml` pins, installed by the host's `rustup`, and an unpinned channel is refused.
- **Breaking:** `Dotnet` builds with the SDK `global.json` names: `lock` resolves it under the file's `rollForward` policy against the .NET release manifest and pins the release with its tarball's sha512 (lockfile version 8, `toolchains`), `build` downloads it on the host and restores the packages with its own `dotnet`, and the sandbox restores and publishes offline with it; `packages=` defaults to the SDK's Debian runtime (`libicu76`, `libssl3t64`, `libstdc++6`), no SDK package, since Debian ships none, and `Dotnet`, `Go` and `Cargo` are `tundravm.sources` names.
- **Breaking:** a credential in a `Git` or `Http` url (`https://user:token@`, `?token=`) is refused, and the lock, the manifest and the SBOM are never written holding one; an SSH url still reads the agent.
- `Git(url, ref, auth="GITHUB_TOKEN")` and `Http(url, auth=...)` read the token from that host variable at lock, fetch and prefetch and hand it to git (`GIT_CONFIG_*`, reaching Go and Cargo git dependencies on the same host) or the download as a header, never to disk, the sandbox or an artifact.
- `Git(auth=("VAR", "basic:USER"))` or `("VAR", "bearer")` states the header scheme for a host the variable alone guesses wrong: `("CI_JOB_TOKEN", "basic:gitlab-ci-token")` for a GitLab CI job token, `basic:x-access-token` for GitHub Enterprise; alone it is basic for github.com (`x-access-token`) and `gitlab.*` (`oauth2`), bearer elsewhere.

### Lint

- Every code is a registered rule with its severities and a fix in public spelling; `check --explain CODE` prints one, a component's own included.
- Every fix an `Image` file can hit names the keyword to change (`hardening="relaxed"`, `value.ref.edit(...)`, `Tree(exclude=[...])`); the 28 codes only the internal model reaches sit apart in the lint reference and `check --explain` marks them `internal`.
- **Breaking:** a component declares its codes in `rules = (Rule(...),)`; an undeclared code is `rule-undeclared`.
- **Breaking:** `component-conflict`, `component-requires-missing` and `image-arch-unsupported` replace the `fragment-` and `recipe-` codes.
- **Breaking:** `service-program-missing` (error) replaces `service-command-not-shipped`: a service's program must be something the image ships.
- **Breaking:** `source-unpinned` is image-wide, subject `sources.NAME`.
- New: `egress-unfiltered`, `volume-tpm-on-cloud`, `unit-written-at-runtime`, `observability-sink-unset`, `observability-export-unset`.
- A finding several profiles share prints once, `[image]` marks an image-wide one, the report ends with its `--explain` line, and a hint names the lock command that runs where you are.

### Docs

- `README.md`, `docs/guide.md` in the order of the loop, `docs/trust.md` for the threat model and recovery, and `docs/reference/` generated from the code; the prose's examples run in the tests.
- The API reference prints a file mode default in octal (`mode: int = 0o644`), as an image file writes it, not as `420`.
- **Breaking:** one example, `examples/attested-node/`, built in six stages the guide walks through: Nethermind on Hoodi as a component the example writes, Lighthouse, a Go proxy signing block responses with the instance key behind attested TLS that serves a quote over that key (`quotes=True`), and operating it, an hourly sync-status timer included. The six small examples and `examples/app/` are gone.
- The guide's stage section follows the stage files, and says why the example leaves out `TpmSeal`, `AptRepository`, `Kernel.build`, `Tdxs.from_policy` and `Service.packaged`.
- `docs/trust.md` says what an `AttestedTLS` certificate proves and does not, and the exact byte layout the quote binds; the guide's stage 5 names the two commands that check the certificate.
- `docs/release.md`: a `v*` tag tests, builds, installs and releases tundravm on GitHub, with the wheel and the sdist; install a release with `uv tool install git+https://github.com/Hyodar/tundra@vX.Y.Z`.
- `README.md` and `docs/release.md` name the four Debian or Ubuntu packages `dev` needs beside tundravm: `debian-archive-keyring`, `golang`, `qemu-system-x86`, `ovmf`.
- The README, the guide, the trust page and the release page read end to end as a first user after the day's landings: each stage section adds one idea, every term links to where it is defined, every claim about a command matches what it prints, and the old example's names are gone from the deploy, attest and recovery commands (`docs/design/walkthrough-4-2026-11-01.md`).
- The docs split by purpose: the README says what tundravm is and runs it in five commands, `docs/terms.md` defines each term once, the guide is the six stages with each one's command and output, `docs/howto.md` holds the recurring tasks and `docs/manual.md` the reference prose, in fewer words than the README and guide had (`docs/design/docs-clarity-2026-11-03.md`).
- The unit suite runs on every core: `pytest-xdist` is a dev dependency and `pytest -m "not integration"` spreads over `-n auto`; a run that may select the integration tests stays serial.

### Removed

- **Breaking:** `Nethermind`, `Lighthouse`, `TaikoClient` and `Raiko` leave `tundravm.components`: the library ships no application, and `examples/attested-node/components/` carries the two the example runs.
- **Breaking:** the 23 old verbs, without aliases: `init` is `new`; `inspect`, `explain` and `status` are `show`; `lint` and `diff` are `check`.
- **Breaking:** `compile` is `build --tree-only`, `bake` is `build`, `deploy qemu` is `run`, `import` and `units convert` are `convert`, `clean` is `build --clean-cache`.
- **Breaking:** the per-variant tree layouts and the `nethermind-v1` dialect.
- **Breaking:** the nix and in-process backends; `TUNDRAVM_BACKEND` takes `local` or `lima`.
- **Breaking:** `bake-result.json`, the evidence, release and witness bundles, and placeholder measurements.
- **Breaking:** `check --format` and the Markdown renderers.
- **Breaking:** `tundravm.testing.fake_bake` and `bake_in_process`; use `FakeMkosi` and `FakeSources`.
- **Breaking:** `Secret(rotation=)`, which promised nothing: the attested endpoint delivers each secret once per boot.
- The pre-v2 docs pages.
- `tundravm.deploy.qemu.QemuDeployAdapter`, which no command reached: `run` launches QEMU.
- `/usr/lib/tmpfiles.d/50-tundravm-runtime.conf`, which nothing ran: boot-time tmpfiles are masked.
