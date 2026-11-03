# The manual

Every slot, rule and file, by topic, after [Terms](terms.md) and the [guide](guide.md).
The generated [reference](reference/cli.md) lists every flag.

## What is proven today

Gates 1, 4, 5 and 6 of [the v2 plan](design/plan-v2-2026-10-30.md) are implemented, and so
are gate 3's `run --mode` separation and attested endpoint. Gate 2, a real TDX quote, is
not.

| Area | State |
|---|---|
| The mkosi tree, `payload.efi`, `qemu.raw`, `azure.vhd`, `gcp.tar.gz`, both root layouts | built in CI from a fixture; `qemu.raw` boots through its ESP and from dm-verity; two builds match, `azure.vhd` included |
| An `Image` and its slots | CI builds stage 1 of the example and boots its dev build; every slot compiles under mkosi in the tests |
| The thirteen commands, build directories, signed manifests | `verify` checked a signed manifest from a second checkout |
| `run --mode simulated --probe` | CI runs the fixture's dev build: every probe check passes and its page is fetched |
| The fail-closed production profile | CI boots it without TDX: `tdx-evidence.service` refuses and no service starts |
| `Kernel.build` | a weekly CI workflow compiles Debian's 6.12 with every target's drivers built in and boots it |
| A real TDX quote, raw-disk measurements matched against it (gate 2) | not done |
| The attested endpoint: quote-bound TLS, channel-bound delivery and unlock | CI attests the fixture's dev build (simulated) and delivers to it; `BrokerKey`'s pull client is not built |
| `deploy` to GCP or Azure, `attest` against a real quote | tested against fakes only |

## The project

`new NAME` writes these files. A `-` in NAME is `_` in their names.

| File | Holds |
|---|---|
| `NAME.py` | the image: [stage 1](guide.md#1-the-boot) of the example |
| `tests/test_NAME.py` | lint, compile and committed-tree tests |
| `pyproject.toml` | tundravm from git at a commit, pytest, and `[tool.tundravm]` |
| `README.md` | the commands of the loop |
| `.gitignore` | ignores `/.tundravm/` and `/build/*`, keeps `build/tundravm.lock` |
| `.github/workflows/image.yml` | with `--ci`: [the image pipeline](release.md#the-image-pipeline-new-writes) |

Commit `uv.lock`. `uv add --editable PATH/TO/tundravm` works against a local checkout.

```toml
[tool.tundravm]
image = "demo.py"
build = "build"
```

Each setting resolves from the command line, then the environment, then the table, then
the default. Commands use the nearest `pyproject.toml` with the table, walking up.
Relative paths are relative to that file.

| Key | Environment | Default |
|---|---|---|
| `image` | `TUNDRAVM_IMAGE` | none: name it here or on the command line |
| `build` | `TUNDRAVM_BUILD` | `build` |
| `backend` | `TUNDRAVM_BACKEND` | `local` on Linux x86-64, `lima` elsewhere |

The host picks the backend; an image file names none.

| Backend | Host | Needs |
|---|---|---|
| `local` | Linux x86-64 | Python, apt and dpkg with `debian-archive-keyring`, and unprivileged user namespaces or `sudo` |
| `lima` | macOS, other Linux | Lima; the guest is x86-64 Debian running the same pinned mkosi |

`local` runs mkosi 27.1 in-process, never from `PATH`. Every other build tool comes from
mkosi's tools tree, pinned to the same Debian snapshot. A build never switches backend on
failure. `doctor --for build` names what a build here lacks.

## The image

`Image` is a frozen dataclass. Every slot is optional and takes small typed values.
The [API reference](reference/api.md) lists each namespace.

### What is implied

| Implied | Escape hatch |
|---|---|
| Debian trixie from snapshot `20261018T082309Z` | `base=Debian("trixie", snapshot="...")`; another release is untested |
| a measured UKI (kernel, initrd and command line in one PE file) with a curated module set: `tdx_guest`, virtio, NVMe, Hyper-V, gVNIC | `kernel=Kernel.default(modules=[...])` to keep more, `kernel=Kernel.build(...)` to compile one |
| NTS time from two authenticated servers, a volatile journal, sysctl hardening, DNS over TLS, CPU mitigations on the command line, no apt timers, no console getty (`TeeBase`) | pass `TeeBase(...)` in `components=`, or remove a piece in a profile |
| DHCP on `en*` and `eth*` (`Network`) | `network=Network(dhcp=False, static=StaticAddress(...))` |
| an nftables firewall that drops inbound traffic except exposed ports; the [control plane](#the-control-plane) opens 8745 and 22 (`Firewall`) | `expose=` opens a port; `network=Network(egress=Egress(...))` filters outbound |
| one generated system user per service, the `static` sandbox, `Restart=on-failure` | `Service(user=, hardening=, unit=DropIn(...))` |
| the evidence service `tdxs` from `tools/`, socket-activated on `/var/tdxs.sock` (`Tdxs`) | `components=[Tdxs(source=Git(url, commit, subdir="tools"))]` for a mirror |
| `tdx-evidence.service`, which every service requires: without TDX a production profile starts none, a development one runs simulated ([trust](trust.md#threat-model)) | none: a dev profile is the way to run without TDX |
| for each cloud in `targets=`, its provisioning glue, inert elsewhere through `ConditionVirtualization=`, so one payload boots on every target | `targets=` |
| an orderly poweroff on the ACPI power button: services stop and volumes unmount first | none |
| a raw bootable disk for QEMU | `targets=("qemu", "azure", "gcp")` adds the cloud disks |

Passing a baseline component in `components=` replaces the implied one.

### The slots

| Slot | Takes |
|---|---|
| `base` | `Debian(release, snapshot=, efi_stub=, repositories=[AptRepository(...)])` |
| `packages` | package names, or `Package(name, backports=True)`: runtime packages only |
| `files` | `File.text(path, text, mode=, owner=)`, `File.copy(src, dst)`, `Tree(src, dst, exclude=)`, a `Build` |
| `services` | `Service` |
| `components` | `Component` instances |
| `ingress` | `AttestedTLS` or `TLS`: see [Ingress](#ingress) |
| `network` | `Network(dhcp=, static=, egress=, interfaces=, dns=)` |
| `users`, `groups` | `User`, `Group` for resources several services share |
| `kernel` | `Kernel.default(modules=, cmdline=)` or `Kernel.build(base=DebianConfig(...), fragments=, patches=, cmdline=)`: see [The kernel](#the-kernel) |
| `rootfs` | `RootFS.initramfs()` (default) or `RootFS.verity()` |
| `observability` | `Observability(logs=JournalExport(...), metrics=PrometheusRemoteWrite(...))` |
| `targets` | a subset of `("qemu", "azure", "gcp")`; `qemu` is always built |
| `dev` | `DevAccess(ssh_keys=, password=)`: the non-production `dev` profile |
| `profiles` | `{"name": Profile(...)}` |
| `mkosi` | `Mkosi(settings={...}, scripts=[...])`: see [The escape to mkosi](#the-escape-to-mkosi) |

`AptRepository(url, suite, components, key=)` adds an archive for the build's apt. `key=`
is a host keyring `Path`, or one under `/usr/share/keyrings/`. A
`https://snapshot.debian.org/archive/NAME` URL is pinned by the image's snapshot (or
`snapshot=`). Any other is pinned by `sha256=` of its `dists/SUITE/InRelease`, which
`lock` records.

`File.copy` and `Tree` read host files when the image lowers. A relative path is beside
the image file, as `Local` is. A file is root-owned unless `owner=` says otherwise; the
runtime-init step `owners` applies it at boot.

`cmdline=` on either `Kernel` appends parameters (`name` or `name=value`) to the UKI's
measured command line, after the image's own:
`Kernel.default(cmdline=["transparent_hugepage=madvise"])`. Every UKI boots `TeeBase`'s CPU
mitigations first, `mitigations=auto,nosmt` (every vulnerability mitigated, SMT off where
that needs it) and `spec_store_bypass_disable=on` (Spectre v4 off for every process).
A parameter tundravm sets (`root=`, `init=`, `console=` and their kin), another value of
those two and one that turns a mitigation off (`nospectre_v2`, `mds=off`) are refused,
naming what sets it. `Kernel.build` is for a driver built in or a symbol turned off.

### The kernel

`Kernel.build(base=DebianConfig("6.12.113-1"))` compiles the stable release that Debian
package carries, from gregkh/linux, with Debian's `cloud-amd64` config and every driver
built in. `config=Path(...)` starts from a config of your own instead. Tundra's hardening
merges first: io_uring, userfaultfd, kexec, KSM, 32-bit syscalls, debug info, and file
systems and protocols no image uses go off; lockdown, zeroed memory and stack canaries go
on. The table, a reason per option, is `src/tundravm/declarative/_kernel_hardening.py`.
`fragments=`, `.config` files or `KernelFragment({...})`, merge after it in order, then
each component's `kernel()`, the verity root's options, a `Container`'s `loop` and `erofs`,
and the gcp target's ACPI patch. A fragment that sets a hardening option itself wins. `hardening=False` is the
`kernel-unhardened` error in a production profile. The build checks every option survived
`olddefconfig`.

## Services

A `Service` is an argv, never a command line. A bare `Service(name, argv)` gets
`Type=exec`, `Restart=on-failure` with `RestartSec=5s`, a system user named after the
service, `hardening="static"`, and `WantedBy=minimal.target`. Each argv element is quoted
the way systemd parses it. The one spelling for a shell is
`["/bin/sh", "-eu", "-c", script]`. A built binary is a valid argv element too
([Sources and toolchains](#sources-and-toolchains)).

Every image generates an instance key at boot: a fresh secp256k1 key on tmpfs, root-only.
A service takes it with `credentials=[InstanceKey()]`, and nothing else may name it.
`show` says which service takes it; `run status` and `attest` print its public key.

`expose=[TCP(8080)]` (or `UDP(53)`) opens that port in the firewall. The only other open
ports are an [ingress](#ingress)'s and the [control plane](#the-control-plane)'s. `expose=`
does not protect the requests: an ingress fronts `app.endpoint(8080)`, the loopback
address with the service's readiness.

### Readiness and order

`requires=[other.ready]` is a real dependency: `Requires=` and `After=` on whatever proves
the other service ready. `After=` alone is never generated.

| Readiness | What it does |
|---|---|
| none | ready once the program is exec'd; a `requires=` on it is refused, since nothing proves it |
| `Notify()` | `Type=notify`: ready when the program sends `READY=1` |
| `TcpProbe(port)` | a bounded probe unit polls the port; installs nothing |
| `HttpProbe(url)` | a bounded probe unit waits for a 2xx; installs `curl` |

### The sandbox

`hardening="NAME"` picks a sandbox per runtime class. Each preset is the strict sandbox
minus what that runtime cannot live without:

| Preset | Differs from `static` | Because |
|---|---|---|
| `static` | nothing | a static binary needs nothing more |
| `jit`, `node`, `python` | no `MemoryDenyWriteExecute=` | a JIT maps pages writable, then executable; CPython needs it for libffi |
| `dotnet` | the same, and `@resources` syscalls allowed | the .NET runtime sizes itself with `sched_setaffinity` and `setrlimit` |

The `static` sandbox is `ProtectSystem=strict`, private `/tmp` and devices, no new
privileges, an empty capability bounding set, the kernel and namespace protections, the
`@system-service` syscall filter and `UMask=0077`.
`Hardening.preset("static", devices=["/dev/tdx_guest"])` replaces `PrivateDevices=yes`
with an allow list. `hardening="relaxed"` and `Hardening.none()` are for programs you
cannot fix.

### Timers, sockets and the rest

| Need | Write |
|---|---|
| run periodically | `timer=Every("15m")` or `timer=Calendar("daily")` |
| socket activation | `socket=Listen(8080)` or `Listen("/run/app.sock")` |
| persistent state | `state=volume.directory("app")`; a bare name is a volatile `StateDirectory=` |
| a shared group | `groups=["tdx"]` |
| anything systemd knows and no field covers | `unit=DropIn(...)` |

A timer runs a scheduled oneshot. It binds secrets and a volume `state=`, waiting for
them as a long-lived service does, and refuses what only one has: `socket=`,
`readiness=` and `expose=`. Writing a unit under `/etc/systemd/system` at boot is the
`unit-written-at-runtime` error: declare it instead.

`Service.packaged("prometheus-node-exporter.service")` runs the unit a Debian package ships
and installs the package (`package=` names another). Its sandbox is `hardening=`, `static`
unless said, which replaces the package's own. Beside it only `requires=`, a probe
`readiness=`, `expose=` and `unit=DropIn(...)` apply. A capability the daemon needs is a
`Hardening.preset("static", capabilities=["CAP_SYS_TIME"])` deviation. One it lacks shows
at boot, as the probe's `service:` failure naming it.

## Components

A `Component` groups declarations and exports typed values: [stage
2](guide.md#the-component) writes one. Each code `check()` reports is declared in `rules`:
`--explain` prints it, and an undeclared code is a `rule-undeclared` error. `outbound()`
says where it sends, and `kernel()` the `KernelFragment` and `KernelPatch` values a built
kernel needs for it.

`tundravm.components` ships the baseline and the ways in. Application components are the
project's.

| Component | What it is | Exports |
|---|---|---|
| `TeeBase` | the confidential-VM baseline (implied) | none |
| `Network` | systemd-networkd, DHCP or a static address (implied) | `online` |
| `Firewall` | nftables, inbound drop by default (implied) | none |
| `Tdxs` | the evidence service (implied) | `EVIDENCE`, `socket`, `group` |
| `SshAccess` | root SSH by public key, keys baked or delivered; `login=` for others | `port` |
| `DevAccess` | debug access, only in the `dev` profile | none |
| `Observability` | journal and metrics export | `service("journal-export")`, `service("metrics-export")` |
| `Container` | a program in a Debian root of its own, with no network | `unit`, `image` |

`SshAccess(login=Login(name, keys=, owner=, commands={...}, shell=))` lets another account
in ([a task](howto.md#log-in-as-a-tenant)). `commands=` maps a word to an argv. A list runs
as the login, `Sudo([...])` as root. `{lines:int}` (0-65535), `{lines:int:1-1000}` and
`{mode:choice:a|b}` are whole-word arguments. `login-menu` is the account's shell and
forced command. It lists the menu for an empty request and refuses anything else. It runs
no shell and no terminal. Each root entry gets exact lines in
`/etc/sudoers.d/<name>`; an int becomes a digits-only sudoers pattern.
`owner=True` gives the login the key `attest` hands over. A `Login` in `SshAccess` takes
root's delivered key away unless `from_delivery=True`. Root without a key never logs in
(`PermitRootLogin no`). A port no mode names stays open in every mode, so a menu login
that must always answer leaves 22 out of `modes=`.

A service that asks for quotes (`quotes=True`) joins the `tdx` group and requires
`EVIDENCE`. The evidence service depends on no volume or secret: the evidence unlocks
them. `Tdxs` and the volume and delivery tools build from `tools/` of tundravm's repository
at the installed commit. `Tdxs(source=Git(mirror, commit, subdir="tools"))` points every
one of them at a mirror. `Tdxs.from_policy(policy)` also validates quotes against the
registers of an image you trust.

`Container("web", argv, packages=, files=, state=, user=, expose=)`
([a task](howto.md#run-a-container)) builds its root as the subimage `mkosi.images/web/`
from the image's snapshot. The payload ships it as `/usr/lib/tundravm/containers/web.raw`,
and the manifest records its sha256. The program runs from that root as its own user, in
a network namespace holding only `lo`. An OCI reference, a non-empty `egress=` and a shell
in production (`container-shell`) are refused.

## Profiles

`dev=DevAccess(...)` adds the `dev` profile: debugging tools, a serial console and root
SSH by public key only. `password=` is a root password for that console, never a network
login. Any other profile is a `Profile`:

| Field | Meaning |
|---|---|
| `extends` | the profile it starts from; `None` is `production` |
| `add` | more content: packages, files, services, components |
| `changes` | `value.ref.edit(...)` or `value.ref.remove()` on something the image declares |
| `production` | `False` makes its builds development builds |

A change names a declaration that exists, or the image does not resolve. Removing a
service removes its unit, its user, its firewall rule and the dependencies nothing else
uses. A component's service is `component.service("name").ref`. An edit takes the keywords
of the value's own constructor and is checked like a new value. Refused: another built
binary, a credential bound through argv or `environment=` (bind it in `credentials=`),
another `user=` for a directory on a volume, dropping a required readiness.

What every profile builds goes once in the tree's `payload/`. What one builds beyond that,
or keeps of what another removes, goes in `payload/mkosi.profiles/<name>/`. A component a profile adds opens its
`outbound()` in that profile's firewall only. `build --profile staging` is
`mkosi --profile=staging`.

## Volumes, secrets and ingress

The image collects each volume and secret from the services that bind it.

### Volumes

`Volume(name, unlock=)` is a LUKS2 volume with ext4 inside, mounted at `/persistent/<name>`
unless `mount=` says otherwise. Its size and disk are deployment configuration. The image
finds the disk by name through `/dev/disk/by-id/*-tundravm-<name>`, never by probe order.
It formats a volume only while it carries no initialisation token: an unlock failure never
permits formatting.

`unlock=` is the trust decision and has no default:

| Unlock | Means | State |
|---|---|---|
| `OperatorKey()` | an operator sends a passphrase each boot with `secrets unlock` | over the attested endpoint, which hands it to `key-gen` |
| `TpmSeal(platform=, pcrs=, recovery="none")` | a random key sealed to one stated vTPM: `azure`, `gcp` or `swtpm` | `key-gen` seals without a PCR policy today; `pcrs` is recorded, enforced in gate 3 |
| `BrokerKey(url, audience=)` | a key broker releases the key after attestation | no pull client yet: the image waits as for an operator |

`TpmSeal` is refused for `targets=` that boot with another vTPM: a key sealed on one
platform does not open on another.

`data.directory("nethermind")` is a directory on the volume. A service binds it with
`state=`. The image creates it, owned by that service, once the volume is open, and the
service waits for it. `directory(name, group=)` shares it through a group.

### Secrets

`Secret(name)` is delivered after attestation to `/run/tundravm/credentials/<name>`, a
root-owned tmpfs file, mode 0600. A service binds it as an argv element or an
`environment=` value, where it renders as the path the service reads. `credentials=[secret]`
binds it for a program that finds it otherwise. Either renders
`LoadCredential=<name>:/run/tundravm/credentials/<name>`, and systemd copies it into the
unit's read-only `$CREDENTIALS_DIRECTORY` at start.

| Option | Meaning |
|---|---|
| `required=True` | every bound service and the boot target wait for it (default) |
| `required=False` | nothing waits: the boot completes and the service starts without it, reading one newline |
| `source=BrokerKey(...)` | a broker delivers it instead of an operator (no pull client yet) |

The endpoint delivers each secret once per boot.
`Generated(name)` needs no delivery: each boot a root oneshot writes 32 random bytes as
hex to `/run/tundravm/secrets/<name>`.

### Ingress

`AttestedTLS(8546, upstream=signer.endpoint(8547))` opens 8546. The upstream listens on
the loopback and exposes no port, so the ingress is the only path to it. The relay is
`socat`'s OpenSSL listener under its own user. It serves a certificate made at boot, whose
key the attested endpoint's quote binds ([trust](trust.md#what-first-contact-proves)).
`attest --tls 8546` checks that binding. `TLS(port, upstream=, certificate=secret)` is
ordinary HTTPS with a delivered certificate. Nothing about it is attested.

### The root filesystem

| Layout | Use |
|---|---|
| `RootFS.initramfs()` | the OS in the UKI's initrd: small services, one file to measure (default); `max_size="4G"` caps its tmpfs, else half the memory |
| `RootFS.verity()` | a read-only erofs root, its dm-verity hash partition beside the ESP and root hash on the command line, under a volatile overlay |

Choose verity when the payload is large. The ESP holds a UKI of
`RootFS.initramfs(uki_max_size=)` (256M by default), verity's 128 MiB: a bigger UKI fails
the build with both sizes. The manifest records the layout and the cap.

### Network and observability

`network=Network(egress=Egress(ports=[TCP(443)], hosts=["10.0.0.0/8"]))` filters outbound
traffic. It accepts what the baseline (NTS time, resolvers) and each component's
`outbound()` send. `Service(egress=Egress(...))` accepts more for that service's user
alone. The cloud metadata server and its identity token take only tundravm's cloud glue
and a service with `Egress(metadata=True)` (`service-metadata` warns). A host may be
`HOST:PORT`. `"logs.example:443"` opens 443 only to the addresses the name resolves to,
which `egress-resolve` asks over DNS over TLS at boot and every 30s; until then it is
unreachable. A service whose own egress names a host starts after the first resolution.
A production profile without a filter warns `egress-unfiltered`.

`TeeBase` resolves through `systemd-resolved` over strict DNS over TLS, with no fallback,
LLMNR or mDNS. The host can neither read nor forge a lookup.
`Network(dns=["9.9.9.9#dns.quad9.net"])` names the resolvers as `ADDRESS#TLS-NAME`,
Cloudflare's pair by default: no target cloud's resolver speaks TLS. The firewall accepts TCP 853 to them and no plain DNS.

`Observability(logs=JournalExport(url=, credential=), metrics=PrometheusRemoteWrite(url=,
credential=, export=))` ships the journal and node metrics. Without `url=` each goes to a
receiver in the guest (`journal-sink`, `metrics-sink`). Once `url=` is set, the delivered
credential authenticates the export. An `http://` sink warns. Only the series on the
`export=` keep-list leave, with the image's identity. `rules=[RecordingRule("box:up",
"min(up)")]` computes series in the guest, so `export=` can name booleans in place of raw
series ([a task](howto.md#ship-booleans-not-raw-series)). A record name has a colon, and
`promtool check rules` checks the expressions at build time. With rules Prometheus runs in
server mode, its query API on the loopback. `image-metrics` logs the digest and RTMR0-3 at
boot.
`Observability(proxy="IP:PORT")` reaches every sink through that one address.
`proxy=Proxy("10.88.0.100", 443, passthrough=True)` is a TCP relay: each sink's name maps to
the address in `/etc/hosts`, so its TLS, SNI and `Host` stay the sink's.
`credential=SigV4("us-east-2", Secret("metrics-aws"))` signs each write with the AWS key
file the secret holds, as Amazon Managed Prometheus requires.

### The firewall and its modes

`Firewall()` opens what services expose, an ingress's port and 8745. `Firewall(modes=)`
makes it switchable ([a task](howto.md#open-a-maintenance-window)). A port a mode names
opens in that mode only. A mode's `egress=` adds to the image's. The image boots into
`production`. A switch goes over the attested endpoint, or through a login's `mode-switch
to`. `mode-switch` extends RTMR3 with it, then swaps the ruleset, and old flows drop on
their next packet. A window closes
itself after `lasts` (30 minutes by default) of boot time. The probe's `mode:firewall` row
checks the history against RTMR3. Refused: 22 in `production`, a mode named `tdx` or
`simulated`, and a switch on a production build without an owner. On TDX the extend
needs Linux 6.16, so a 6.12 kernel refuses every switch.

### The control plane

An image with a secret, `AttestedTLS`, firewall modes or a volume an operator or broker
unlocks runs the attested endpoint on port 8745. The endpoint needs nothing on a volume
([trust](trust.md#what-first-contact-proves)). The image also runs SSH on 22, for the key
`attest` hands over: root's, or a `Login(owner=True)`'s. On TDX that key comes only from the
[owner](terms.md#stages-2-to-6), so an image without an `OperatorKey()` volume has no
delivered SSH there.
`/etc/tundravm/runtime.json` records every volume, secret and ingress.

## Sources and toolchains

`GoBinary`, `CargoBinary` and `Build` (from `tundravm.sources`) are valid argv elements:
they pull their build and install into the image, and `binary.executable` is the installed
path. Go builds the module root (`package="./cmd/server"` for a sub-command) with
`-trimpath -buildvcs=false` and `CGO_ENABLED=0`. Rust builds with
`cargo auditable build --frozen` and reproducible `rustflags`, so the SBOM can read the
crate graph from the binary.

`Build(name, source, command=["hugo", "--minify"], packages=["hugo"], install=[...])` runs
one program in the build environment, never in the image. `files=[build]` ships what its
`install=` names.

A build takes one source, which `tundravm lock` pins: `Local("app")`, a directory beside
the image file, by the digest of its files; `Git(url, ref)` by commit; `Http(url, sha256=)`
by sha256. `Git(url, ref, auth="GITHUB_TOKEN")` builds a private repository
([a task](howto.md#use-a-private-source)). The token is read from that host variable and
never written anywhere. A credential pasted into a URL is refused. git sends the token as
an `Authorization` header. A host other than github.com or `gitlab.*` names its scheme:

| Host | `auth=` |
|---|---|
| github.com | `"GITHUB_TOKEN"` (basic, user `x-access-token`) |
| `gitlab.*` | `"GITLAB_TOKEN"` (basic, user `oauth2`) |
| GitLab CI | `("CI_JOB_TOKEN", "basic:gitlab-ci-token")` |
| GitHub Enterprise, self-hosted GitLab | `("TOKEN", "basic:x-access-token")`, `("TOKEN", "basic:oauth2")` |
| any other | `"TOKEN"` (bearer), or `("TOKEN", "basic:USER")` |

A `Local` path stays inside the project. Its digest covers the path, sha256, exec bit and
symlink target of what `git ls-files --cached --others --exclude-per-directory=.gitignore`
lists. An edit is `local-changed`.

The build sandbox never fetches a source. `build` checks each one out on the host, as you,
into `build/<image>/.work/`, verifies a reused checkout against its pin, and mounts the
checkouts into the build. Dependencies are prefetched there, so `build --offline` runs
without network. A modified checkout is `E_SOURCE`; `build --clean-cache` starts
the workspace afresh. The manifest records each source's toolchain; `show BUILD` prints it.

A Go source builds with the toolchain its `go.mod` names (the `toolchain` line, else the
`go` line: `go 1.22` is `go1.22.0`), fetched with the modules by the host's `go` 1.21+;
Debian's `golang` only bootstraps it. A release the host cannot fetch is `E_SOURCE`; a
`go.mod` older than 1.21 builds with Debian's `go`.

A Cargo source builds with the channel its `rust-toolchain.toml` (or `rust-toolchain`)
pins, installed into the workspace by the host's `rustup`; its `cargo` fetches the crates,
and the offline build runs it over Debian's linker. The channel must be a release
(`1.91.0`) or dated (`nightly-2026-09-01`): a moving one is `E_SOURCE`, and no file means
Debian's `cargo`. `build:rustup` blocks only with a pinned source.

A Dotnet source builds with the SDK its `global.json` names: `lock` resolves it under its
`rollForward` policy, within that .NET release, and pins its tarball's sha512; `build`
downloads it; its `dotnet` restores the packages and builds offline over Debian's ICU,
OpenSSL and libstdc++. The host needs no `dotnet`; no `global.json` is `E_SOURCE`.

## The lock

`lock` writes `build/tundravm.lock`: a digest per section of the resolved image, a pin per
source, and the toolchain (mkosi, the tools tree, tundravm, the measurement tools). Commit
it. Packages and the tools tree share the snapshot this release pins; `Debian(snapshot=)`
overrides it.

- A profile's sections are `profiles.<name>.<key>`; `check` reports their drift as that
  profile's `lock-changed`.
- An older tundravm's lockfile is refused, never migrated: lock again.
- A production build refuses a missing (`lock-missing`) or stale lock and builds every
  source at its pin. A development build writes a missing lock and re-pins `Local` sources.
- `build --offline` builds only what the lock pins, and checks an `InRelease` against the
  lock's digest instead of fetching it.

## The mkosi tree

Everything tundravm builds is an mkosi 27.1 tree you can read, commit and build without
tundravm. The starter's tree:

```text
mkosi.conf                     the QEMU disk: Format=disk, Dependencies=payload, the ESP
mkosi.repart/00-esp.conf       a 272M FAT32 ESP sized to hold the UKI
mkosi.images/payload/mkosi.conf  Include=../../payload
payload/mkosi.conf             the OS: Format=uki, packages, kernel modules
payload/mkosi.conf.d/          one file per concern: 10-platform, 20-tee-base, 30-firewall
payload/mkosi.extra/           the files and the generated units
payload/mkosi.skeleton/        minimal.target, which services are enabled into
payload/mkosi.build.d/  mkosi.postinst.d/  mkosi.finalize.d/
payload/mkosi.profiles/dev/    the dev overlay: the boot probe and the root password
```

- `targets=` adds `mkosi.images/azure/` and `mkosi.images/gcp/`. Each depends on the
  payload, carries the same ESP and converts the disk in `mkosi.postoutput.d/`.
- `RootFS.verity()` makes the payload a `Format=disk` with root and verity partitions, and
  every disk copies them beside the ESP.
- The root `mkosi.conf` pins `MinimumVersion=27.1`, `Snapshot=`, `ToolsTree=default` with
  its snapshot, `SourceDateEpoch=0`, `Seed=` and `History=no`, so a second build gives
  the same bytes.

### The escape to mkosi

`mkosi=Mkosi(settings={"Locale": "C.UTF-8"}, scripts=[("postinst", "...")])` adds
verbatim settings, each in the section mkosi 27.1 reads it from. It adds scripts at an
mkosi stage (`prepare`, `build`, `postinst`, `finalize`, `postoutput`). The manifest
records them. A setting the compiler writes, or one that would break a guarantee, is
refused with the slot that sets it (`KernelCommandLine` names `Kernel(cmdline=)`).

## Testing a project

`tundravm.testing` reads an image as values without building it, as the generated
`tests/test_NAME.py` does:

```python
from tundravm import Image
from tundravm.components import Egress, Network
from tundravm.testing import assert_clean, plan

image = Image("node", network=Network(egress=Egress()))


def test_lints_clean() -> None:
    # The evidence service's source stays unpinned until `tundravm lock`.
    assert_clean(plan(image), allow=("source-unpinned",))


def test_egress_is_filtered() -> None:
    nftables = plan(image).file("/etc/nftables.conf").content.decode()
    assert "hook output priority filter; policy drop" in nftables

```

`plan()` compiles one profile in memory. `assert_clean` fails on any finding it does not
allow. `FakeMkosi` and `FakeSources` stand in for mkosi and the fetches. `boot` boots a
built disk under QEMU. The [API reference](reference/api.md#tundravmtesting) lists every
helper.

## Build

`build` builds one profile against the lock: `production` by default, `--profile P` or
`--dev`. It writes:

```text
build/demo/
  production/<id>/
    payload.efi          the UKI, the measured payload
    qemu.raw             the raw GPT disk OVMF boots through its ESP (always built)
    azure.vhd            with the azure target
    gcp.tar.gz           with the gcp target
    payload.*.raw        the verity root and hash partitions, with RootFS.verity()
    packages.json        mkosi's package manifest
    measurements.json    per target and boot path: predictions, unknowns, a policy
    sbom.spdx.json       the SBOM, bound to the lock and the package manifest
    inputs/              tundravm.lock, summary.json, sources.json, toolchain.json, mkosi-argv.json
    tree/                the mkosi project it was built from
    manifest.json        the identity; <id> is its digest
    manifest.sig         with --sign
    observations.json    timestamps, runner, reproduction: not identity
  development/<id>/      builds of a non-production profile
  latest-production -> production/<id>
  latest-development -> development/<id>
  .work/                 the workspace: fetched sources, mkosi's cache and tools tree
```

The directory is published with one rename, read-only, and never written again. Building
the same identity twice leaves it alone. `tundravm.build(image, lock=, backend=, into=BUILD)`
does it from Python. `build` ends with a report, which `show BUILD` prints again. CI's
`reproduce` job built the fixture on 2026-11-02 (digests cut short):

```text
built build/fixture/production/3c80bc1c00281082
  payload.efi    73.2 MiB  sha256 38e86d81ef69be0e  ESP 272.0 MiB, 194.6 MiB left
  qemu.raw      273.0 MiB  sha256 519c30519c392f83
  azure.vhd     274.0 MiB  sha256 18e95bdaef94885e
source               kind   ref           pin           toolchain  checkout  deps
  git-codereview     git    6c2c97b0f412  6c2c97b0f412  go1.26.0   fetched   fetched
  rust-probe         local  -             75f057a5f22d  1.91.0     fetched   fetched
time  checkouts 0.5s  prefetch 16.4s  mkosi 2m43s  measurements 2.3s  total 6m05s
measured  payload.efi authenticode sha384 96aedaa878d3f72d…
  qemu   uki   RTMR1      8b8cd17330c531e3…
  qemu   disk  RTMR1      9e9aead6b4ef17f8…
  azure  disk  -          no TDX register predicted (show --measurements)
  unchecked: RTMR3: outside the policy: no event schema is defined for RTMR3
reproduction: matched
```

The `authenticode` line is the UKI's identity in RTMR1's event log. The register rows are
predictions. [What `build` prints](trust.md#what-build-prints) says which value is which.

`manifest.json` (schema `tundravm.manifest/2`) is canonical JSON. Its `id` is the first 16
hex digits of the sha256 of the document without `id`. It binds every artifact's digest,
the UKI and its command line, the root hash, the tree, the lock, the sources and their
toolchains, the packages, the SBOM and the measurements. Its `unchecked` names what the
build could not check. `observations.json` holds what is not identity: when, where, how
long.

`--reproduce` builds again in `.reproduce/` and compares every artifact; a difference
exits 1, and the build is still published. `--sign KEY` signs the manifest with an SSH
key (namespace `tundravm-manifest`). `verify` checks each file against the manifest, and
that nothing else is there. `verify --rebuild` builds again; it runs the project's code,
so it is never implied.

## Run

`dev` is `build --dev` then `run --mode simulated --probe`: the probe's table, then the
console until Ctrl-C stops the instance. It reuses the latest development build while the
image, lock and `Local` sources are unchanged, and says why it builds; a rebuild reuses
mkosi's base tree and compiler caches, never in production. `--no-follow` returns after the
probe.

`run` boots `qemu.raw` through OVMF on a snapshot and forwards the image's ports from
`127.0.0.1`. `--mode simulated` boots without TDX, using KVM when it can. `--mode tdx`
needs `doctor --for tdx` ready. A production build boots `simulated` fail-closed, and
`run status` says so.

The instance is `.tundravm/instances/<image>-<n>/`: `instance.json`, `events.jsonl` (one
record per action, never a value), `console.log`, a sparse 16 GiB disk per volume.

### The boot probe

A development profile carries `tundravm-probe.service`. It prints
`tundravm-probe: <check> ok|fail|skip <detail>` per check; a `fail` says what to do:

| Check | Holds when |
|---|---|
| `mode` | `/run/tundravm/mode` is `simulated` or `tdx` |
| `cmdline` | `/proc/cmdline` holds every parameter the image appends, the CPU mitigations first |
| `dns` | a resolver's name resolves, and `systemd-resolved` is connected to port 853 |
| `rtmr` | always `skip`: the guest reads its registers only from a quote, which `attest` checks |
| `service:`, `socket:`, `timer:` | each `Service`, socket or timer is active (or exited 0) |
| `port:` | each `expose=` port has a listener |
| `volume:` | each `Volume` is mounted, ext4, on dm-crypt |
| `egress:` | with `Egress`, a datagram no rule accepts is refused, and each host name a rule accepts holds the addresses it resolved to |
| `observability:` | exporters run or retry, took their configuration, block nothing, egress loaded |
| `hardening:` | a preset's `systemd-analyze security` score is under its ceiling |
| `kernel:` | no disabled symbol in `/proc/kallsyms` (`Kernel.build`), else no uncurated module |
| `host-keys` | with SSH, the host key is root-owned, read-only to others |
| `key:instance` | the instance key is `root:root 0600` and its public key file matches |
| `owner:` | each `owner=` path has that owner and mode |
| `endpoint:` | the attested endpoint answers evidence for the nonce it was sent |
| `attested-tls:` | each `AttestedTLS` port's certificate carries the key that evidence binds |

`--probe` waits for it (up to `--timeout`), prints the table, writes `probe.json`, and
exits 1 on a failure. `run probe INSTANCE` reads a running instance's.

## Deploy and attest

The [command reference](reference/cli.md#tundravm-deploy) lists `deploy`'s settings
tables, `[tool.tundravm.gcp]` and `[tool.tundravm.azure]`. GCP attaches each volume as
device `tundravm-NAME`. Azure attaches each volume's disk at a LUN: its position in the
image's volumes, which `runtime.json` records. The image names the disk at that LUN
`/dev/disk/by-id/azure-tundravm-NAME`, from `azure-vm-utils`' by-lun link. `deploy`
reads the LUNs back and fails when Azure put another disk there. The receipt records the
VM, its address, each volume's disk and the firewall rule the management port needs,
which tundravm does not create.

`attest`'s quote binds a fresh nonce, the TLS session's key, the SSH host keys, the
instance public key and each `AttestedTLS` certificate key. A `tundravm.policy/2` record in
`policies/` holds the platform registers you approve (`platform_approval`: MRTD, RTMR0).

| Verdict | Means |
|---|---|
| `trusted` | a `tdxs` validator (`--validator`) accepted the quote, the nonce is bound, and MRTD and RTMR0 to RTMR2 all matched |
| `partial` | nothing disagreed, but one of the four had no accepted value |
| `measurements-match` | the registers matched; no validator checked the signature |
| `measurements-mismatch` | a register or the nonce did not match; no validator checked |
| `simulated` | the simulator: binding checked, registers not; its key is published |
| `untrusted` | the validator rejected the quote, a validated quote did not match, or a production build's TD has the `DEBUG` attribute |

A dev build attests `simulated` by default: without TDX it takes the simulator issuer,
which production never ships. An Azure policy is refused: its quote measures the
paravisor, not the image.

### Delivering secrets

`secrets deliver` and `secrets unlock` write only on a session they attested, once per
name per boot. What they release on:

| Instance | Releases on |
|---|---|
| production build on TDX | `trusted` only, with no `DEBUG` attribute and no simulated evidence |
| development build on TDX | `trusted` or `measurements-match` |
| development build, simulated | `simulated` only; tundravm cannot tell a test value from a real one |
| production build, simulated | nothing: refused before contact, since its services never start |

## When something fails

Every error prints `error [CODE]: message`, `fix:`, context and `at:`, and exits 2. A
failed check exits 1, an interrupt 130. An unknown command gets a "did you mean"; an
unknown flag, the usage line. With `--json`, stderr gets one JSON line instead
(`tundravm.error/1`). `--traceback` raises with the Python traceback.

| Code | Usually | Do |
|---|---|---|
| `E_VALIDATION` | a malformed declaration, an unknown profile | `tundravm check` lists everything |
| `E_LINT` | `build` refused error-level findings | `tundravm check` |
| `E_LOCKFILE` | `build` met a stale lock, or a production build none (`lock-missing`) | `tundravm lock --check`, then `tundravm lock` |
| `E_SOURCE` | a source did not resolve, or a checkout changed | `tundravm build --clean-cache` |
| `E_POLICY` | `build --offline` met a source the workspace never fetched, or a tree that builds with the network | build once online, then `--offline` |
| `E_BACKEND_EXECUTION` | mkosi failed | the error names mkosi's log; `build -v` echoes it; `doctor --for build` |
| `E_STATE` | no build, instance or host tool where one was expected | the hint names the command |
| `E_ARTIFACT_CHANGED` | an artifact's bytes changed | `tundravm verify BUILD` names the file |
| `E_REPRODUCIBILITY` | two builds that should match did not | compare `observations.json` |
| `E_MEASUREMENT` | an artifact that is not a UKI or a disk booting one | rebuild it with `tundravm build` |
| `E_ATTESTATION` | the endpoint, issuer or validator failed, or evidence was not bound to the session | `run logs`, `journalctl -u tundravm-endpoint -u tdxs` |
| `E_DEPLOYMENT` | a missing tool, no TDX for `--mode tdx`, a refused upgrade | the hint names it |

A `check` finding is `SEVERITY CODE [PROFILES] SUBJECT: message` and its hint. `[image]`
marks one about the whole image, and profiles sharing a finding share its line. `--json`
and the GitHub annotations keep one finding per profile and subject.
