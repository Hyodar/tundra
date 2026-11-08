# The manual

This page is the reference prose: every slot, rule and file, by topic, for a reader who has
done [Terms](terms.md) and the [guide](guide.md). The generated [command](reference/cli.md),
[API](reference/api.md) and [check code](reference/lint.md) references list every flag,
signature and code.

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

`new NAME` writes a project, where a `-` in NAME becomes `_` in the file names:

| File | Holds |
|---|---|
| `NAME.py` | the image: [stage 1](guide.md#1-the-boot) of the example |
| `tests/test_NAME.py` | lint, compile and committed-tree tests |
| `pyproject.toml` | tundravm from git at a commit, pytest, and `[tool.tundravm]` |
| `README.md` | the commands of the loop |
| `.gitignore` | ignores `/.tundravm/` and `/build/*`, keeps `build/tundravm.lock` |
| `.github/workflows/image.yml` | with `--ci`: [the image pipeline](release.md#the-image-pipeline-new-writes) |

Commit `uv.lock` with them. `uv add --editable PATH/TO/tundravm` points the project at a
local checkout instead.

```toml
[tool.tundravm]
image = "demo.py"
build = "build"
```

A setting resolves from the command line first, then the environment, then the table, then
its default. A command reads the nearest `pyproject.toml` that has the table, walking up
from where it runs, and resolves relative paths against that file.

| Key | Environment | Default |
|---|---|---|
| `image` | `TUNDRAVM_IMAGE` | none: name it here or on the command line |
| `build` | `TUNDRAVM_BUILD` | `build` |
| `backend` | `TUNDRAVM_BACKEND` | `local` on Linux x86-64, `lima` elsewhere |

The backend is the host's choice, and an image file never names one:

| Backend | Host | Needs |
|---|---|---|
| `local` | Linux x86-64 | Python, apt and dpkg with `debian-archive-keyring`, and unprivileged user namespaces or `sudo` |
| `lima` | macOS, other Linux | Lima; the guest is x86-64 Debian running the same pinned mkosi |

`local` runs mkosi 27.1 in-process, never from `PATH`, and takes every other build tool from
mkosi's tools tree, pinned to the same snapshot. A build never switches backend when it
fails, and `doctor --for build` names what the host lacks.

## The image

`Image` is a frozen dataclass whose slots are all optional and take small typed values.
What an image gets without naming it is implied, and the [API reference](reference/api.md)
lists each namespace.

### What is implied

Every image gets these, and each has its escape hatch:

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
| `files` | `File.text(path, text, mode=, owner=)`, `File.copy(src, dst, mode=, owner=)`, `Tree(src, dst, exclude=, owner=)`, a `Build` |
| `sysctl` | `{"vm.swappiness": 10}` in `/etc/sysctl.d/60-<name>.conf`, refusing `TeeBase`'s keys |
| `services` | `Service`: see [Services](#services) |
| `components` | `Component` instances: see [Components](#components) |
| `ingress` | `AttestedTLS` or `TLS`: see [Ingress](#ingress) |
| `network` | `Network(dhcp=, static=, egress=, interfaces=, dns=)` |
| `users`, `groups` | `User`, `Group` for resources several services share |
| `kernel` | `Kernel.default(modules=, cmdline=)` or `Kernel.build(...)`: see [The kernel](#the-kernel) |
| `rootfs` | `RootFS.initramfs()` (default) or `RootFS.verity()` |
| `observability` | `Observability(logs=JournalExport(...), metrics=PrometheusRemoteWrite(...))` |
| `targets` | a subset of `("qemu", "azure", "gcp")`; `qemu` is always built |
| `dev` | `DevAccess(ssh_keys=, password=)`: the non-production `dev` profile |
| `profiles` | `{"name": Profile(...)}` |
| `mkosi` | `Mkosi(settings={...}, scripts=[...])`: see [The escape to mkosi](#the-escape-to-mkosi) |

`AptRepository(url, suite, components, key=)` adds a signed archive to the build's apt, where
`key=` is a host keyring `Path` or one under `/usr/share/keyrings/`. A
`https://snapshot.debian.org/archive/NAME` URL is dated by the image's snapshot, or by its
own `snapshot=`. Any other is pinned by `sha256=` of its `dists/SUITE/InRelease`, which `lock`
records, refusing the archive once it moves.

`File.copy` and `Tree` read host files when the image lowers, and a relative path is beside
the image file, as `Local` is. A file is root-owned unless `owner=` names another account,
which the runtime-init step `owners` applies at boot.

### The kernel

The kernel is `Kernel.default()`, Debian's `linux-image-amd64` with the curated module set,
unless the image compiles one with `Kernel.build(...)` for a driver built in or a symbol
turned off.

`cmdline=` on either form appends parameters (`name` or `name=value`) to the UKI's measured
command line, after the image's own: `Kernel.default(cmdline=["transparent_hugepage=madvise"])`.
Every UKI boots `TeeBase`'s CPU mitigations first, `mitigations=auto,nosmt`, which turns SMT
off where a mitigation needs it, and `spec_store_bypass_disable=on`, which closes Spectre v4
for every process. A parameter tundravm sets itself (`root=`, `console=` and their kin) is
refused, and so is one that turns a mitigation off (`nospectre_v2`, `mitigations=off`,
`mds=off`); repeating a mitigation is the `kernel-cmdline-repeated` note.

`Kernel.build(base=DebianConfig("6.12.113-1"))` compiles the stable release that Debian
package carries, from gregkh/linux at its locked commit, with Debian's `cloud-amd64` config
and every driver built in. `version=` and `source=` pick another tree, and
`config=Path(...)` starts from a config of your own, which then needs both. The options merge
in this order:

1. Tundra's hardening: io_uring, userfaultfd, kexec, KSM, BPF for users, 32-bit syscalls,
   debug info, and the file systems, protocols and decompressors no image uses go off;
   lockdown, zeroed memory and stack canaries go on. The table, with a reason per option,
   is `src/tundravm/declarative/_kernel_hardening.py`.
2. `fragments=`, `.config` files or `KernelFragment({...})`, in order.
3. Each component's `kernel()`.
4. The verity root's options, a `Container`'s `loop` and `erofs`, and the gcp target's ACPI
   patch.

A fragment that sets a hardening option itself wins over the hardening. `patches=` apply
with `patch -p1` in the same order, and `modules=True` builds loadable modules, leaving
lockdown out because it would refuse them unsigned. `hardening=False` is the
`kernel-unhardened` error in a production profile and a warning in a development one. The
build checks that every requested option survived `olddefconfig` and that every target's
drivers are built in.

## Services

A `Service` is one program the image runs as a systemd unit, under its own user and inside
a sandbox. Its argv is a list, never a command line, and the one shell is
`["/bin/sh", "-eu", "-c", script]`. A word is a string, an int, an endpoint, a secret,
`directory / "file"` or `port.address`, each quoted as systemd parses it, and a [built
binary](#sources-and-toolchains) goes first.

A bare `Service(name, argv)` gets `Type=exec`, `Restart=on-failure` with `RestartSec=5s`, a
system user named after the service, `hardening="static"` and `WantedBy=minimal.target`.
The keywords change the rest:

| Keyword | Takes |
|---|---|
| `requires=` | what must be ready first: see [Readiness and order](#readiness-and-order) |
| `readiness=` | `Notify()`, `TcpProbe(port)` or `HttpProbe(url)` |
| `expose=` | `TCP(8080)` or `UDP(53)`, opened in the firewall |
| `egress=` | `Egress(...)`: what this service alone may send ([the network](#network-and-observability)) |
| `credentials=` | secrets for a program that finds them itself: see [Secrets](#secrets) |
| `environment=` | per variable, text, a secret (rendered as its path) or an endpoint (its URL) |
| `state=` | `volume.directory("app")`, persistent, or a bare name, a volatile `StateDirectory=`; also the working directory |
| `hardening=` | a preset name or a `Hardening`: see [The sandbox](#the-sandbox) |
| `timer=` | `Every("15m")` or `Calendar("daily")` |
| `socket=` | `Listen(8080)` or `Listen("/run/app.sock")`: socket activation |
| `resources=` | `Resources(memory_max=, cpu_quota=, tasks_max=, ...)` |
| `shutdown=` | `Shutdown(timeout=, signal=, kill_mode=)` |
| `user=`, `groups=` | another account, more groups (`["tdx"]`) |
| `quotes=` | `True`: it asks the evidence service for quotes ([Components](#components)) |
| `working_directory=`, `logs=`, `runtime=` | `WorkingDirectory=`, and directories under `/var/log` and `/run` |
| `description=` | `Description=`; a component's service defaults to the first sentence of its docstring |
| `unit=` | `DropIn(...)`, for anything systemd knows and no keyword covers |

`expose=` is the only way a service opens a port; the only other open ports are an
[ingress](#ingress)'s and the [control plane](#the-control-plane)'s. It does not protect the
requests, so an ingress fronts `app.endpoint(8080)` instead, the loopback address that
carries the service's readiness.

### Readiness and order

`requires=[other.ready]` is a real dependency: `Requires=` and `After=` on whatever proves
the other service ready, while `After=` alone is never generated. An endpoint of another
service in argv or `environment=` (`[signer, "-upstream", nethermind.rpc]`) implies the same
wait when that service declares a readiness, and the `requires-implied` note names each wait
it added. `requires=` also takes a setup task's `task.output(path)`, which starts the service
after the task and only if it wrote *path*.

| Readiness | What it does |
|---|---|
| none | ready once the program is exec'd; a `requires=` on it is refused, since nothing proves it |
| `Notify()` | `Type=notify`: ready when the program sends `READY=1` |
| `TcpProbe(port)` | a bounded probe unit polls the port; installs nothing |
| `HttpProbe(url)` | a bounded probe unit waits for a 2xx; installs `curl` |

Both probes poll every `interval=` until `timeout=`, each a systemd time span (`"500ms"` and
`"90s"` by default).

### The sandbox

`hardening="NAME"` picks a sandbox per runtime class, each the strict sandbox minus what
that runtime cannot live without:

| Preset | Differs from `static` | Because |
|---|---|---|
| `static` | nothing | a static binary needs nothing more |
| `jit`, `node`, `python` | no `MemoryDenyWriteExecute=` | a JIT maps pages writable, then executable; CPython needs it for libffi |
| `dotnet` | the same, with the `@resources` syscalls and `AF_NETLINK` allowed | the .NET runtime sizes itself with `sched_setaffinity` and `setrlimit`, and lists interfaces over netlink |

The `static` sandbox is `ProtectSystem=strict`, private `/tmp` and devices, no new
privileges, an empty capability bounding set, the kernel and namespace protections, the
`@system-service` syscall filter and `UMask=0077`. A deviation is
`Hardening.preset("static", ...)`: `devices=["/dev/tdx_guest"]` replaces `PrivateDevices=yes`
with an allow list, `network="inet+netlink"` adds netlink, and `capabilities=["CAP_SYS_TIME"]`
grants one while allowing back what it needs (`ProtectClock=` goes, `@clock` returns).
`hardening="relaxed"` and `Hardening.none()` are for programs you cannot fix.

### Timers, sockets and the rest

A timer runs the service as a scheduled oneshot that keeps every other keyword. It binds
secrets and a volume `state=`, waiting for them as a long-lived service does, and refuses
what only a long-lived service has: `socket=`, `readiness=` and `expose=`. Writing a unit
under `/etc/systemd/system` at boot is the `unit-written-at-runtime` error, so declare it
instead.

`Service.packaged("prometheus-node-exporter.service")` runs the unit a Debian package ships
and installs the package, or the one `package=` names. Its sandbox is `hardening=`, `static`
unless said, and replaces the package's own. Beside it only `requires=`, a probe
`readiness=`, `expose=` and `unit=DropIn(...)` apply, so a capability the daemon needs is a
`Hardening.preset("static", capabilities=["CAP_SYS_TIME"])` deviation, and one it lacks
shows at boot in the probe's `service:` failure.

## Components

A `Component` is a class that groups declarations and exports typed values, such as an
endpoint, for the rest of the image to wire: [stage 2](guide.md#the-component) writes one.
`tundravm.components` holds the API for writing one, its submodules ship the library below,
and application components are the project's.

| Member | Says |
|---|---|
| the fields | its configuration: a component is a frozen, keyword-only dataclass |
| `compose()` | the declarations it stands for |
| `outbound()` | where it sends, which a filtered image accepts from its users alone (`senders()`) |
| `kernel()` | the `KernelFragment` and `KernelPatch` values a built kernel needs for it |
| `check()`, `rules` | its own lint: each code `check()` reports is declared in `rules`, `--explain` prints it, and an undeclared one is a `rule-undeclared` error |

| Component | What it is | Exports |
|---|---|---|
| `tee.TeeBase` | the confidential-VM baseline (implied) | none |
| `network.Network` | systemd-networkd, DHCP or a static address (implied) | `online` |
| `network.Firewall` | nftables, inbound drop by default (implied) | `status`, `switch` |
| `tee.Tdxs` | the evidence service (implied) | `EVIDENCE`, `socket`, `group` |
| `access.SshAccess` | root SSH by public key, keys baked or delivered; `login=` for others | `port` |
| `access.DevAccess` | debug access, only in the `dev` profile | none |
| `observability.Observability` | journal and metrics export | `service("journal-export")`, `service("metrics-export")` |
| `container.Container` | a program in a Debian root of its own | `unit`, `image`, `group`, `endpoint(port)`, `metrics` |

A service with `quotes=True` asks for quotes: it joins the `tdx` group, requires `EVIDENCE`,
and is refused as `user="root"` or a user named `endpoint`, whose tag is the attested
endpoint's. The evidence service depends on no volume or secret, since the evidence is what
unlocks them. `Tdxs` and the volume and delivery tools build from `tools/` of tundravm's
repository at the installed commit, and `Tdxs(source=Git(mirror, commit, subdir="tools"))`
points every one of them at a mirror. `Tdxs.from_policy(policy)` also validates quotes
against the registers of an image you trust.

### Logins

`SshAccess(login=Login(...))` lets an account other than root in over SSH, with no password
and no forwarding ([a task](howto.md#log-in-as-a-tenant)). Each account belongs to one
declaration, so a `Login` and a `Container` of one name, or a service that runs as either's
account, are refused.

| `Login` | Means |
|---|---|
| `name` | the account |
| `keys=` | its public keys: baked, or a `Secret` the attested endpoint writes to `/etc/ssh/authorized_keys.d/<name>` |
| `owner=True` | the owner's key, the one `attest` hands over, in place of keys of its own |
| `commands={...}` | its menu: a word mapped to a `Command(...)`, run as the login, or a `Sudo(...)`, run as root |
| `shell=` | the escape hatch: a login shell the image ships, with a terminal and no sudo; `ssh-login-shell` warns in production |
| `home=True` | `/home/<name>`, handed to the login at boot |

A login takes exactly one of `commands=` and `shell=`. A command's words are literal
strings, `Int("lines", 1, 10000)` or `Choice("mode", "a", "b")`, which the client fills in.
`modes=` offers it in those firewall modes only, and `login-mode-undeclared` catches a mode
the `Firewall` lacks; `dev=True` offers it in development builds only. `firewall.status` and
`firewall.switch` are two such commands, run as root.

`login-menu` is the account's shell and sshd's forced command: an empty request lists the
menu, a request runs exactly the argv it names, and anything else is refused. Each `Sudo`
command gets exact `/etc/sudoers.d/<name>` lines. A menu gets no terminal, through two locks
(`PermitTTY no` and `restrict` keys), except in a development build of a login with
`dev=True` commands, which lifts both (`PermitTTY yes`, `restrict,pty`) so that `ssh -t`
gives whichever command the client asks for a terminal.

A `Login` in `SshAccess` takes root's delivered key away unless `from_delivery=True`, and
root without a key never logs in (`PermitRootLogin no`). sshd bounds unauthenticated
connections per source (`PerSourceMaxStartups 4`, `MaxSessions 1`), so a menu login can face
the network, and a login that must always answer leaves 22 out of every mode, which keeps
it open in all of them ([the modes](#the-firewall-and-its-modes)).

### Containers

`Container("web", argv, ...)` runs a program in a Debian root of its own, built from the
image's snapshot and shipped in the measured payload ([a task](howto.md#run-a-container)).
OCI references are refused, and so is a shell as a production container's program
(`container-shell`).

| Keyword | Takes |
|---|---|
| `argv` | the program, left out when sshd is the program |
| `packages=`, `files=` | what its root holds |
| `state=` | a volume directory |
| `user=` | the account it runs as: its own by default, its uid pinned from its name |
| `expose=` | TCP or UDP ports forwarded to it |
| `egress=` | where it may send; `modes={"maintenance": Egress(...)}` adds what it sends in one mode |
| `requires=` | host endpoints relayed into its namespace at the same address (`[nethermind.rpc]`) |
| `serves=`, `probe=` | ports the host reaches at `container.endpoint(port)`, ready once `probe=TcpProbe(port)` connects |
| `ssh=` | `ContainerSsh(port=, owner=True)` or `ContainerSsh(port=, keys=)`: its own sshd |
| `hardening=` | a `Service`'s sandbox levels, `static` by default |
| `binds=` | `Bind(path, at=, writable=, file=)`: host paths it sees, read-only unless `writable=True` |

`expose=`, `egress=`, `modes=` and `serves=` give it the veth `ve-web`, which the firewall
forwards. `ContainerSsh` runs sshd as the container's user, letting it in by key alone, on a
host key made each boot that every quote binds; `attest` pins that key, and
`Mode(ingress=[web.ssh])` opens its port in that mode only.

The container runs in a PID namespace of its own, so it sees no host process. It writes
group-readable files, and a host service joins `container.group` to read them.
`Bind(data.directory("in"), writable=True)` gives a volume directory the container's group,
`0750` or `0770` when writable, and the container waits for the volume. The control plane
under `/run/tundravm`, the kernel's interfaces and whole system trees are refused as binds.

## Profiles

A profile is a variant of the image, which is itself the `production` profile.
`dev=DevAccess(...)` adds the `dev` profile: debugging tools, a serial console and root SSH
by public key only, where `password=` is a root password for that console, never a network
login. Any other profile is a `Profile`:

| Field | Meaning |
|---|---|
| `extends` | the profile it starts from; `None` is `production` |
| `add` | more content: packages, files, services, components |
| `changes` | `value.ref.edit(...)` or `value.ref.remove()` on something the image declares |
| `production` | `False` makes its builds development builds |

A change must name a declaration that exists, and a component's service is
`component.service("name").ref`. An edit takes the value's own keywords and is checked like
a new value, while removing a service removes its unit, its user, its firewall rule and the
dependencies nothing else uses. A change is refused when it would:

- build another binary;
- bind a credential through argv or `environment=` (bind it in `credentials=`);
- give a directory on a volume another `user=`;
- drop a readiness, or remove a service, that another service requires.

What every profile builds goes once in the tree's `payload/`, and what one builds beyond
that, or keeps of what another removes, goes in `payload/mkosi.profiles/<name>/`. A
component a profile adds opens its `outbound()` in that profile's firewall only, and
`build --profile staging` is `mkosi --profile=staging`.

## Volumes, secrets and ingress

The image collects each volume and secret from what binds it, so neither has a slot.

### Volumes

`Volume(name, unlock=)` is a LUKS2 volume with ext4 inside, mounted at `/persistent/<name>`
unless `mount=` says otherwise. Its size and disk are deployment configuration. The image
finds the disk through `/dev/disk/by-id/*-tundravm-<name>`, never by probe order, refuses
one whose token names another volume, and formats it only while it carries no
initialisation token, so an unlock failure never permits formatting.

`unlock=` is the trust decision and has no default:

| Unlock | Means | State |
|---|---|---|
| `OperatorKey()` | an operator sends a passphrase each boot with `secrets unlock` | over the attested endpoint, which hands it to `key-gen` |
| `TpmSeal(platform=, pcrs=, recovery="none")` | a random key sealed to one stated vTPM: `azure`, `gcp` or `swtpm` | `key-gen` seals without a PCR policy today; `pcrs` is recorded, enforced in gate 3 |
| `BrokerKey(url, audience=)` | a key broker releases the key after attestation | no pull client yet: the image waits as for an operator |

An `OperatorKey()` volume has one [owner](terms.md#stages-2-to-6): the first signed write
pins its key, and every later write must be signed by it
([trust](trust.md#what-first-contact-proves)). `TpmSeal` is refused for `targets=` that boot
another vTPM, because a sealed key opens on one platform only.

`data.directory("nethermind")` is a directory on the volume, bound with `state=`: the image
creates it once the volume is open, owned by that service, and the service waits for it.
`directory(name, group=)` shares it through a group, and `state / "output.log"` is the path
of a file in it.

### Secrets

A secret is a value the image needs and never contains. It comes in three kinds:

| Kind | Made by | Lands at |
|---|---|---|
| `Secret(name)` | an operator's `secrets deliver` after attestation, or with `source=BrokerKey(...)` a broker (no pull client yet) | `/run/tundravm/credentials/<name>`, a root-owned tmpfs file, mode 0600 |
| `Generated(name)` | a root oneshot at each boot: 32 random bytes as hex | `/run/tundravm/secrets/<name>` |
| `InstanceKey()` | each boot: a fresh secp256k1 key | `/run/tundravm/instance-key.pem`, root-only, its public key beside it |

A service binds a secret as an argv word or an `environment=` value, where it renders as the
path the service reads, or through `credentials=[secret]` for a program that finds it
otherwise. Either renders `LoadCredential=<name>:/run/tundravm/credentials/<name>`, and
systemd copies the value into the unit's read-only `$CREDENTIALS_DIRECTORY` at start.

With `required=True`, the default, every bound service and the boot target wait for the
secret. With `required=False` nothing waits: the boot completes and the service starts
without it, reading one newline. Each profile takes, once per boot, only the secrets its own
units read, and `secrets deliver` refuses a name the instance's profile does not read.

Every image generates the instance key. A service takes it with
`credentials=[InstanceKey()]`, anything else that names it is the `instance-key-exposed`
error, `show` names its taker, and `run status` and `attest` print its public key.

### Ingress

`AttestedTLS(8546, upstream=signer.endpoint(8547))` opens 8546 and relays it to the
upstream, which listens on the loopback and exposes no port, so the ingress is the only path
to it. The relay is `socat`'s OpenSSL listener under its own user, starting once the
upstream is ready. It serves a certificate made at boot, whose key the attested endpoint's
quote binds ([trust](trust.md#what-first-contact-proves)) and `attest --tls 8546` checks.
`TLS(port, upstream=, certificate=secret)` is ordinary HTTPS with a delivered certificate,
and nothing about it is attested.

### The root filesystem

| Layout | Use |
|---|---|
| `RootFS.initramfs()` | the OS in the UKI's initrd: small services, one file to measure (default); `max_size="4G"` caps its tmpfs, else half the memory |
| `RootFS.verity()` | a read-only erofs root, its dm-verity hash partition beside the ESP and root hash on the command line, under a volatile overlay |

Choose verity when the payload is large. The ESP holds a UKI of up to
`RootFS.initramfs(uki_max_size=)`, 256M by default, or 128 MiB under verity, and a bigger
UKI fails the build naming both sizes. The manifest records the layout and the cap.

### Network and observability

`network=Network(egress=Egress(ports=[TCP(443)], hosts=["10.0.0.0/8"]))` filters outbound
traffic, dropping every packet no rule accepts, and a production profile without the filter
warns `egress-unfiltered`. Each rule matches who asked for it:

- The baseline (NTS time, the resolvers) and each component's `outbound()` send from the
  component's users alone.
- `Service(egress=Egress(...))` accepts more for that service's user alone.
- The image's `Egress` and a firewall mode's `egress=` accept for every process, the only
  rules that do.
- A `Container`'s `egress=` and `modes=` apply in its own namespace.

The cloud metadata server and its identity token take only tundravm's cloud glue and a
service with `Egress(metadata=True)`, filtered image or not, and `service-metadata` warns
about each such service.

A host name such as `"logs.example:443"` opens 443 to the addresses the name resolves to,
which `egress-resolve` asks over DNS over TLS at boot and every 30s. Until the first answer
the name is unreachable and its services wait, and it keeps its last addresses while no
answer comes. `Host("api.example", ["203.0.113.7"], port=443, aliases=[...])` pins a peer
instead: the firewall accepts those addresses alone, and the name resolves to them in
`/etc/hosts`, which `systemd-resolved` reads first (`ReadEtcHosts=yes` by default). The
image's, a service's and a mode's pins fill the image's file, a container's fill its
root's, and a name is pinned once, never also left to DNS. `Egress(ports=[TCP(443)],
deny=[peer])` reaches the web but never `peer`: `deny=` drops ahead of every accept of its
chain, a `Host` at every port.

`TeeBase` resolves through `systemd-resolved` over strict DNS over TLS, with no fallback,
LLMNR or mDNS, so the host can neither read nor forge a lookup.
`Network(dns=["9.9.9.9#dns.quad9.net"])` names the resolvers as `ADDRESS#TLS-NAME`,
Cloudflare's pair by default, since no target cloud's resolver speaks TLS. The firewall
accepts TCP 853 to them and no plain DNS.

`Observability(logs=JournalExport(url=, credential=), metrics=PrometheusRemoteWrite(url=,
credential=, rules=, export=))` ships the journal and the node metrics. A development
profile sends each to a receiver in the guest (`journal-sink`, `metrics-sink`), as a
production one does until `url=` names the deployment's, which `observability-sink-unset`
reports. Otherwise the delivered credential authenticates each write, an `http://` sink
warns, and a remote exporter waits for NTS time so the sink's certificate is judged by a
synced clock.

The metrics keep-list says which series leave the guest. `export=` names raw series and
presets, the `health` preset when neither it nor a rule is given, and the image's identity
(its digest, RTMRs and mode) always leaves. Each series leaves labelled `instance=` the
boot's instance key, which `attest` prints as bound to the quote, so replicas never merge at
the sink. `rules=[RecordingRule("box:up", f"max({box.metrics.active})")]` computes series
in the guest ([a task](howto.md#ship-booleans-not-raw-series)) from the `.metrics`
selectors of services, containers and volumes, and a rule's series leaves unless
`export=False`. A record carries a colon, and `promtool check rules` checks each at build.
With rules Prometheus runs in server mode, and its API on `127.0.0.1:9090` refuses every
request. `image-metrics` also logs the digest and RTMR0-3 at boot.

| Sink option | Does |
|---|---|
| `Observability(proxy="IP:PORT")` | reaches every sink through one proxy address |
| `proxy=Proxy(address, port, tls_name=)` | reaches a proxy that speaks TLS at `https://<tls_name>:<port>`, the name mapped to the address in `/etc/hosts` |
| `proxy=Proxy("10.88.0.100", 443, passthrough=True)` | a TCP relay: each sink's name is pinned to the address, so its TLS, SNI and `Host` stay the sink's |
| `credential=SigV4("us-east-2", Secret("metrics-aws"))` | signs each write with the AWS key file the secret holds, as Amazon Managed Prometheus requires |

### The firewall and its modes

`Firewall()` opens what services expose, an ingress's port and 8745 where the attested
endpoint listens. `Firewall(modes={...})` makes it switchable between named sets of open
ports ([a task](howto.md#open-a-maintenance-window)):

- A port a mode names opens in that mode only, and one no mode names stays open in every mode.
- A mode's `egress=` adds to the image's.
- The image boots into `production`, and any other mode closes after its `lasts=`, 30
  minutes by default.

A switch goes over the attested endpoint, signed by the owner, or through a login's
`firewall.switch`. `mode-switch` extends RTMR3 with it before it swaps the ruleset, so old
flows drop on their next packet and the probe's `mode:firewall` row can check the history
against RTMR3. Refused: 22 in `production`, `lasts=` on `production`, a mode named `tdx` or
`simulated`, and a switch on a production build without an owner. On TDX the extend needs
Linux 6.16, so a 6.12 kernel refuses every switch and `firewall-modes-kernel` warns.

### The control plane

The control plane is the attested endpoint on port 8745 and SSH on port 22. An image runs it
once it has something to receive: a secret, an `AttestedTLS` ingress, firewall modes, or a
volume an operator or broker unlocks. The endpoint needs nothing on a volume
([trust](trust.md#what-first-contact-proves)), and `/etc/tundravm/runtime.json` records
every volume, secret and ingress.

SSH takes the key `attest` hands over, root's or a `Login(owner=True)`'s. On TDX that key
comes only from the [owner](terms.md#stages-2-to-6), once an `OperatorKey()` volume confirmed
it this boot, so an image without such a volume has no delivered SSH there; a simulated boot
takes the first key sent.

## Sources and toolchains

A source is software a build compiles, which `lock` pins. `GoBinary`, `CargoBinary` and
`Build` (`tundravm.sources`) are argv words that add their build, and a binary is named
after its package (`package="./cmd/server"` is `server`), else its source directory, with
`name=` for a clash.

| Source | Is | Pinned by |
|---|---|---|
| `Local("app")` | a directory beside the image file, inside the project | the digest of its files; an edit is `local-changed` |
| `Git(url, ref)` | a repository at a tag, branch or commit | commit |
| `Http(url, sha256=)` | a download | sha256 |

A `Local` digest covers the path, sha256, exec bit and symlink target of what
`git ls-files --cached --others --exclude-per-directory=.gitignore` lists. Go builds with
`-trimpath -buildvcs=false` and `CGO_ENABLED=0` unless `cgo=True`. Rust builds with
`cargo auditable build --frozen` and reproducible `rustflags`, so the SBOM reads the crate
graph from the binary. `Build(name, source, command=["hugo", "--minify"], packages=["hugo"],
install=[...])` runs one program in the build environment, never in the image, and
`files=[build]` ships what its `install=` names.

`Git(url, ref, auth="GITHUB_TOKEN")` builds a private repository ([a
task](howto.md#use-a-private-source)), and `Http(url, auth=)` fetches the same way. The token
is read from that host variable, never written anywhere, and sent as an `Authorization`
header, while a credential pasted into a URL is refused. A host other than github.com or
`gitlab.*` names its scheme:

| Host | `auth=` |
|---|---|
| github.com | `"GITHUB_TOKEN"` (basic, user `x-access-token`) |
| `gitlab.*` | `"GITLAB_TOKEN"` (basic, user `oauth2`) |
| GitLab CI | `("CI_JOB_TOKEN", "basic:gitlab-ci-token")` |
| GitHub Enterprise, self-hosted GitLab | `("TOKEN", "basic:x-access-token")`, `("TOKEN", "basic:oauth2")` |
| any other | `"TOKEN"` (bearer), or `("TOKEN", "basic:USER")` |

The build sandbox never fetches a source. `build` checks each one out on the host, as you,
into `build/<image>/.work/`, verifies a reused checkout against its pin, prefetches its
dependencies there and mounts the checkouts into the build, so `build --offline` runs
without network. A modified checkout is `E_SOURCE`, which `build --clean-cache` clears by
starting the workspace afresh. The manifest records each source's toolchain, and
`show BUILD` prints it.

| Source | Toolchain | Fetched by | Without one |
|---|---|---|---|
| Go | the `toolchain` line of `go.mod`, else its `go` line (`go 1.22` is `go1.22.0`) | the host's `go` 1.21+, or tundravm, checked against sum.golang.org | a `go.mod` older than 1.21 builds with Debian's `go` |
| Cargo | the channel `rust-toolchain.toml` (or `rust-toolchain`) pins: a release (`1.91.0`) or dated (`nightly-2026-09-01`) | the host's `rustup`, into the workspace; its `cargo` fetches the crates | Debian's `cargo` |
| Dotnet | the SDK `global.json` names, which `lock` resolves under its `rollForward` policy within that .NET release and pins by its tarball's sha512 | `build` downloads it; its `dotnet` restores the packages | `E_SOURCE` |

A Go release that cannot be fetched is `E_SOURCE`, and so is a moving Rust channel. tundravm
fetches Go for linux/amd64 hosts only, into `$XDG_CACHE_HOME/tundravm/toolchains/`. The
offline build runs Cargo over Debian's linker and Dotnet over Debian's ICU, OpenSSL and
libstdc++, so the host needs no `dotnet`, and `build:rustup` blocks only with a pinned
source.

## The lock

`lock` writes `build/tundravm.lock`: a digest per image section, a pin per source, and the
toolchain (mkosi, its tools tree, tundravm, measurement tools). Commit it. Packages and the
tools tree share this release's snapshot unless `Debian(snapshot=)` names another.

- `lock --check` never writes or fetches and exits 1 on drift; `lock --update NAME` resolves
  one input again.
- Drift in `profiles.<name>.<key>` is that profile's `lock-changed`.
- An older lockfile is refused: lock again.
- A released tundravm records its version, and another version is drift. From a checkout it
  records `checkout`, and the `tools/` digest stands in for the version.
- A production build refuses a missing (`lock-missing`) or stale lock and builds every
  source at its pin. A development build writes a missing lock, re-pinning `Local` sources.
- Without a lock `check` notes once what `lock` would pin; with one, `source-unpinned` names
  each source the lock lacks.
- `build --offline` checks an `InRelease` against the lock instead of fetching it.

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

`mkosi=Mkosi(settings={"Locale": "C.UTF-8"}, scripts=[("postinst", "...")])` adds verbatim
settings, each written in the section mkosi 27.1 reads it from, and scripts at an mkosi
stage (`prepare`, `build`, `postinst`, `finalize`, `postoutput`); the manifest records both.
A setting the compiler writes, or one that would break a guarantee, is refused naming the
slot that sets it (`KernelCommandLine` names `Kernel(cmdline=)`).

## Testing a project

`tundravm.testing` reads an image as values without building it, as the generated
`tests/test_NAME.py` does:

```python
from tundravm import Image
from tundravm.components.network import Egress, Network
from tundravm.testing import assert_clean, plan

image = Image("node", network=Network(egress=Egress()))


def test_lints_clean() -> None:
    assert_clean(plan(image))


def test_egress_is_filtered() -> None:
    nftables = plan(image).file("/etc/nftables.conf").content.decode()
    assert "hook output priority filter; policy drop" in nftables

```

`plan()` compiles one profile in memory, and `assert_clean` fails on any finding it does not
allow. `FakeMkosi` and `FakeSources` stand in for mkosi and the fetches, and `boot` boots a
built disk under QEMU. The [API reference](reference/api.md#tundravmtesting) lists every
helper.

## Build

`build` builds one profile against the lock, `production` unless `--profile P` or `--dev`
names another, and writes:

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

The directory is published with one rename, read-only, and never written again, so building
the same identity twice leaves it alone. Every local build shares one package cache,
`$XDG_CACHE_HOME/tundravm/packages`. `tundravm.build(image, lock=, backend=, into=BUILD)`
does the same from Python. `build` ends with a report, which `show BUILD` prints again; CI's
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

The `authenticode` line is the UKI's identity in RTMR1's event log, and the register rows
are predictions. [What `build` prints](trust.md#what-build-prints) says which value is which.

`manifest.json` (schema `tundravm.manifest/2`) is canonical JSON, and its `id` is the first
16 hex digits of the sha256 of the document without `id`. It binds every artifact's digest,
the UKI and its command line, the root hash, the tree, the lock, the sources and their
toolchains, the packages, the SBOM and the measurements, and its `unchecked` names what the
build could not check. `observations.json` holds what is not identity: when, where, how
long.

`--reproduce` builds again in `.reproduce/` and compares every artifact; a difference exits
1, and the build is still published. `--sign KEY` signs the manifest with an SSH key
(namespace `tundravm-manifest`). `verify` checks each file against the manifest and that
nothing else is there, while `verify --rebuild` builds again and, since it runs the
project's code, is never implied.

## Run

`dev` is `build --dev` then `run --mode simulated --probe`: the probe's table, then the
console until Ctrl-C stops the instance. It reuses the latest development build while the
image, the lock and the `Local` sources are unchanged, and otherwise says what changed. A
development build is incremental, reusing mkosi's base tree and the compiler caches, which
a production build never reads. `--no-follow` returns after the probe, and `--mode tdx`
boots with TDX where `doctor --for tdx` is ready.

`run` boots `qemu.raw` through OVMF on a snapshot and forwards the image's ports from
`127.0.0.1`. `--mode simulated` boots without TDX, using KVM when it can, and `--mode tdx`
needs `doctor --for tdx` ready. A production build boots `simulated` fail-closed, and
`run status` says so.

The instance is `.tundravm/instances/<image>-<n>/`: `instance.json`, `events.jsonl` (one
record per action, never a value), `console.log`, and a sparse 16 GiB disk per volume. Its
QEMU monitor listens in `$XDG_RUNTIME_DIR/tundravm`.

### The boot probe

A development profile carries `tundravm-probe.service`, which prints
`tundravm-probe: <check> ok|fail|skip <detail>` per check, and a `fail` says what to do:

| Check | Holds when |
|---|---|
| `mode` | `/run/tundravm/mode` is `simulated` or `tdx` |
| `cmdline` | `/proc/cmdline` holds every parameter the image appends, the CPU mitigations first |
| `root`, `rootfs:` | `/` is `root:root 0755`, and is the initrd's tmpfs at its cap or the overlay on the verity root |
| `time:nts` | chrony selected an NTS source |
| `dns` | a resolver's name resolves, and `systemd-resolved` is connected to port 853 |
| `rtmr` | always `skip`: the guest reads its registers only from a quote, which `attest` checks |
| `service:`, `socket:`, `timer:` | each `Service`, socket or timer is active (or exited 0) |
| `port:` | each `expose=` port has a listener |
| `volume:` | each `Volume` is mounted, ext4, on dm-crypt, and names its owner |
| `egress:` | with `Egress`, a datagram no rule accepts is refused, each host name holds the addresses it resolved to, and the metadata server answers only the users allowed it |
| `observability:` | exporters run or retry, took their configuration, block nothing, have their egress loaded, and a loopback sink holds only what the keep-list keeps |
| `hardening:` | a preset's `systemd-analyze security` score is under its ceiling |
| `kernel:` | no disabled symbol in `/proc/kallsyms` (`Kernel.build`), else no uncurated module |
| `host-keys` | with SSH, the host key is root-owned, read-only to others |
| `login:`, `menu:`, `ssh:root` | each login's sshd block, keys and sudoers hold, its menu lists and refuses as declared, and sshd refuses root |
| `container:`, `container-ssh:` | each container runs in its root, its egress, endpoints and ports work, and its sshd's host key is the one evidence binds |
| `mode:firewall` | the mode in force and its switches replay against RTMR3 |
| `key:instance` | the instance key is `root:root 0600` and its public key file matches |
| `owner:` | each `owner=` path has that owner and mode |
| `endpoint:` | the attested endpoint answers evidence for the nonce it was sent |
| `attested-tls:` | each `AttestedTLS` port's certificate carries the key that evidence binds |

`--probe` waits for it (up to `--timeout`), prints the table, writes `probe.json`, and exits
1 on a failure. `run probe INSTANCE` reads a running instance's. A row behind a locked volume skips; once
the volume opens, `tundravm-probe-again.service` runs the probe again and `run probe` reads
that run.

## Deploy and attest

`deploy azure|gcp --build BUILD` starts a TDX VM from the build, and `--plan` prints each
step and every cloud call without running one. The [command
reference](reference/cli.md#tundravm-deploy) lists its settings tables,
`[tool.tundravm.gcp]` and `[tool.tundravm.azure]`. GCP attaches each volume as device
`tundravm-NAME`. Azure attaches each volume's disk at a LUN, its position in the image's
volumes as `runtime.json` records it, and the image names that disk
`/dev/disk/by-id/azure-tundravm-NAME` from `azure-vm-utils`' by-lun link; `deploy` reads the
LUNs back and fails when Azure put another disk there. The receipt records the VM, its
address, each volume's disk and the firewall rule the management port needs, which
tundravm does not create.

`attest`'s quote binds a fresh nonce, the TLS session's key, the SSH host keys, the instance
public key and each `AttestedTLS` certificate key. A `tundravm.policy/2` record in
`policies/` holds the platform registers you approve (`platform_approval`: MRTD, RTMR0).

| Verdict | Means |
|---|---|
| `trusted` | a `tdxs` validator (`--validator`) accepted the quote, the nonce is bound, and MRTD and RTMR0 to RTMR2 all matched |
| `partial` | nothing disagreed, but one of the four had no accepted value |
| `measurements-match` | the registers matched; no validator checked the signature |
| `measurements-mismatch` | a register or the nonce did not match; no validator checked |
| `simulated` | the simulator: binding checked, registers not; its key is published |
| `untrusted` | the validator rejected the quote, a validated quote did not match, or a production build's TD has the `DEBUG` attribute |

A dev build attests `simulated` by default, because without TDX it takes the simulator
issuer, which production never ships. An Azure policy is refused, since its quote measures
the paravisor, not the image.

### Delivering secrets

`secrets deliver` and `secrets unlock` write only on a session they attested, once per
name per boot. What they release on depends on the instance:

| Instance | Releases on |
|---|---|
| production build on TDX | `trusted` only, with no `DEBUG` attribute and no simulated evidence |
| development build on TDX | `trusted` or `measurements-match` |
| development build, simulated | `simulated` only; tundravm cannot tell a test value from a real one |
| production build, simulated | nothing: refused before contact, since its services never start |

## When something fails

Every error prints `error [CODE]: message`, `fix:`, context and `at:`, and exits 2, while a
failed check exits 1, an interrupt 130 and a closed pipe 141. An unknown command gets a "did
you mean", and an unknown flag the usage line. With `--json`, stderr gets one JSON line
instead (`tundravm.error/1`), and `--traceback` raises with the Python traceback.

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
marks one about the whole image, and profiles sharing a finding share its line, while
`--json` and the GitHub annotations keep one finding per profile and subject.
