# Gate 4 evidence: the concise SDK

Part A (below, first) records the starter of plan section 3.1 as a file, the tree it compiles
to, the units in that tree, and the public name count before and after. Part B (after it)
records the slots that make the prover of section 3.1 an `Image` file (`volumes`, `secrets`,
`ingress`, `rootfs`, `observability`), the `tundravm.components` library, the surge example
rewritten over it, and how the build backend is chosen.

Scope of part A: `Image`, `Service`, `Component`, `Profile` and the small values they take,
on `3009516` (gate 1 accepted).

## The starter

`demo.py`, 15 lines of declaration, which is what plan section 3.1 asks for:

```python
from tundravm import TCP, File, Image, Service

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
```

`tundravm init --template minimal` writes exactly this (plus the `backend = ...` line that
names where it bakes). `tests/image/test_starter.py` asserts the declaration is under 20
lines, loads it the way the CLI does, and checks every implied item below.

## The emitted tree

`tundravm compile demo.py --tree mkosi`, file by file:

```
mkosi.conf
mkosi.images/payload/mkosi.conf
mkosi.repart/00-esp.conf
payload/mkosi.build.d/50-build.sh
payload/mkosi.conf
payload/mkosi.conf.d/10-platform.conf
payload/mkosi.conf.d/20-tee-base.conf
payload/mkosi.conf.d/30-tdxs.conf
payload/mkosi.extra/etc/chrony/chrony.conf
payload/mkosi.extra/etc/machine-id
payload/mkosi.extra/etc/nftables.conf
payload/mkosi.extra/etc/resolv.conf
payload/mkosi.extra/etc/sysctl.d/50-tundravm-tee.conf
payload/mkosi.extra/etc/systemd/journald.conf.d/50-tundravm-tee.conf
payload/mkosi.extra/etc/systemd/network/50-tundravm.network
payload/mkosi.extra/etc/tdxs/config.yaml
payload/mkosi.extra/srv/www/index.html
payload/mkosi.extra/usr/lib/systemd/system/app.service
payload/mkosi.extra/usr/lib/systemd/system/systemd-networkd-wait-online.service.d/50-tundravm-network.conf
payload/mkosi.extra/usr/lib/systemd/system/systemd-networkd.service.d/50-tundravm-firewall.conf
payload/mkosi.extra/usr/lib/systemd/system/tdxs.service
payload/mkosi.extra/usr/lib/systemd/system/tdxs.socket
payload/mkosi.extra/usr/lib/udev/rules.d/65-tundravm-tdx-guest.rules
payload/mkosi.finalize.d/50-finalize.sh
payload/mkosi.postinst.d/50-postinst.sh
payload/mkosi.skeleton/etc/systemd/system/minimal.target
```

Two images, as gate 1 established: `payload` (the measured UKI) and `main` (the QEMU raw
disk around it). `mkosi --force summary --json` of the tree resolves, and the test asserts
the payload is bootable Debian trixie and the main image is a `disk`
(`tests/image/test_starter.py::test_the_starter_compiles_to_a_tree_mkosi_resolves`, using
the same `mkosi_summary` helper as the gate-1 fixture).

Nothing in the starter named the platform packages, the TEE baseline, the firewall, the
evidence service, the udev rule, the boot target or the build script: those 24 of the 26
files are what the image implies.

## The units

`payload/mkosi.extra/usr/lib/systemd/system/app.service`, generated from the one `Service`:

```ini
[Unit]
Description=app.service

[Service]
Type=exec
ExecStart=/usr/bin/busybox httpd -f -p 8080 -h /srv/www
User=app
Restart=on-failure
RestartSec=5s
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
PrivateDevices=yes
ProtectKernelLogs=yes
ProtectClock=yes
ProtectHostname=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
SystemCallFilter=@setuid
SystemCallErrorNumber=EPERM
CapabilityBoundingSet=
UMask=0077

[Install]
WantedBy=minimal.target
```

That block is `Hardening.preset("static")`, the default, which is byte for byte today's
`Hardening.strict()`. The other four presets differ only in what their runtime cannot live
without, and each one's rendered directives are asserted in
`tests/image/test_service.py::test_every_preset_renders_its_directives`:

| Preset | Deviation from `static` | Why |
|---|---|---|
| `static` | none | a static binary needs nothing more |
| `jit`, `node`, `python` | `MemoryDenyWriteExecute=` goes | a JIT maps code pages W then X; CPython needs the same for libffi closures (`ctypes`, cffi) |
| `dotnet` | the same, and `@resources` leaves the deny list | the .NET GC and thread pool size themselves with `sched_setaffinity` and `setrlimit` |

The other units in the tree are `tdxs.service` and `tdxs.socket` (the implied evidence
service), two drop-ins tying networkd to the firewall and bounding `wait-online`, and the
`minimal.target` the services are enabled into.

## The implied baseline, item by item

Each row is asserted in `tests/image/test_starter.py`; the escape hatch for each is in the
`Image` docstring.

| Implied | In the tree | Escape |
|---|---|---|
| Debian trixie from the locked snapshot `20251113T083151Z` | `payload/mkosi.conf.d/10-platform.conf`, `Snapshot=` in the summary | `base=Debian(release, snapshot=)` |
| the kernel and the curated module set (`tdx_guest`, virtio, NVMe, Hyper-V, gVNIC) | `linux-image-amd64` and `KernelModules=` | `kernel=Kernel.default(modules=...)` or `Kernel.build(...)` |
| a measured UKI, and a raw bootable disk around it for QEMU | `payload` + `main`, `mkosi.repart/00-esp.conf` | `targets=` |
| NTS time from two authenticated servers | `etc/chrony/chrony.conf` | a `Profile` change on the file, by reference |
| a volatile journal | `etc/systemd/journald.conf.d/50-tundravm-tee.conf` | the same |
| the sysctl hardening set | `etc/sysctl.d/50-tundravm-tee.conf` | the same |
| a static resolver | `etc/resolv.conf` | `network=Network(...)`, or the same |
| DHCP on `en*`/`eth*` | `etc/systemd/network/50-tundravm.network` | `network=Network(dhcp=False, static=...)` |
| a firewall that drops inbound except declared ports and `:8745` | `etc/nftables.conf` | `expose=`, `network=Network(egress=Egress(...))` |
| one generated system user per service, the preset sandbox, `Restart=on-failure` | `app.service` above | `Service(user=, hardening=, unit=DropIn(...))` |
| the evidence service on `/run/tdx/quote.sock` | `tdxs.service`, `tdxs.socket`, `etc/tdxs/config.yaml`, the udev rule | a `Profile` change removing it by reference |
| no apt timers, no console getty | `payload/mkosi.postinst.d/50-postinst.sh` | the same |
| the Azure and GCP glue, inert off-platform | added per target, `ConditionVirtualization=` | `targets=` |

The firewall the starter gets, in full:

```
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		ct state invalid drop
		iifname "lo" accept
		icmpv6 type { … } accept
		udp sport 67 udp dport 68 accept comment "DHCPv4 replies"
		ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept comment "DHCPv6 replies"
		tcp dport 8080 accept comment "app.service"
		tcp dport 8745 accept comment "allow"
	}
```

`8080` because the service exposed it, `8745` because every image reserves the management
ingress (plan 6.3); nothing listens on it until part B adds the listener, and the port is
declared from the start so the inbound surface does not change when it does.

## Public names, before and after

| Module | On `3009516` | Now |
|---|---|---|
| `tundravm` | 126 | **61** |
| `tundravm.declarative` | 104 | 104 (unchanged; internal, importable, gate 6 renames or deletes it) |

The 61 are frozen in `tests/unit/test_public_surface.py`, which also asserts that the names
reused from the internal model are the same objects, that the new values come from
`tundravm.image`, and that `Recipe`, `Variant`, `Fragment`, `Task`, `Unit`, `Disk`, `Key`,
`Secrets` and `Firewall` are gone from the top level while staying importable internally.

## What part A did not show

- The prover transcript of plan 3.1, and `tundravm.components`: part B, below.
- A boot. The starter compiles and its tree resolves under mkosi here; CI must bake it and
  boot it, and that run is what gate 4 is accepted on.
- Chained profiles. `Profile(extends="other")` resolved, and the compiler refused by name to
  emit a chain it could not express as a single mkosi profile overlay (so did a profile that
  removed a declaration). Lifted since, see "Profile semantics" below.

# Part B: the runtime slots, the component library, surge

On `4931008` (part A). Every claim below is asserted by `tests/image/test_runtime.py`,
`tests/image/test_library.py` or `tests/image/test_prover.py`, unless it says otherwise.

## The prover

The image file of plan section 3.1, as `tests/image/test_prover.py` writes and loads it
(the README's second screen and the `docs/api.md` `Image` section carry the same file).
Raiko reads its configuration as a delivered secret, since that file names the L1 and L2
endpoints and their provider keys, where the plan wrote a `jwt`:

```python
from tundravm import AttestedTLS, DevAccess, Image, OperatorKey, RootFS, Secret, Volume
from tundravm.components.taiko import Raiko

data = Volume("data", mount="/persistent", unlock=OperatorKey())
config = Secret("raiko-config", rotation="restart")
raiko = Raiko(config=config, state=data.directory("raiko"), listen="127.0.0.1:8080")

image = Image(
    "prover",
    components=[raiko],
    volumes=[data],
    secrets=[config],
    ingress=[AttestedTLS(443, upstream=raiko.http)],
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
```

Nine lines of declaration; no `Key`, `Disk`, `Secrets`, `RuntimeTools`, `Tdxs`, `cryptsetup`
or `socat` appears in it (`test_the_prover_is_a_short_image_file`).

### Its tree

`tundravm compile prover.py --tree mkosi`, 57 files:

```
mkosi.conf
mkosi.images/payload/mkosi.conf
mkosi.repart/00-esp.conf
mkosi.repart/10-root.conf
mkosi.repart/11-root-verity.conf
payload/mkosi.build.d/50-build.sh
payload/mkosi.conf
payload/mkosi.conf.d/10-platform.conf
payload/mkosi.conf.d/20-tee-base.conf
payload/mkosi.conf.d/30-tdxs.conf
payload/mkosi.conf.d/40-tundravm-runtime.conf
payload/mkosi.conf.d/50-firewall.conf
payload/mkosi.extra/etc/chrony/chrony.conf
payload/mkosi.extra/etc/machine-id
payload/mkosi.extra/etc/nftables.conf
payload/mkosi.extra/etc/resolv.conf
payload/mkosi.extra/etc/ssh/sshd_config.d/50-tundravm.conf
payload/mkosi.extra/etc/sysctl.d/50-tundravm-tee.conf
payload/mkosi.extra/etc/systemd/journald.conf.d/50-tundravm-tee.conf
payload/mkosi.extra/etc/systemd/network/50-tundravm.network
payload/mkosi.extra/etc/tdx/disk-setup.yaml
payload/mkosi.extra/etc/tdx/key-gen.yaml
payload/mkosi.extra/etc/tdxs/config.yaml
payload/mkosi.extra/etc/tdx/secrets.json
payload/mkosi.extra/etc/tdx/secrets.yaml
payload/mkosi.extra/etc/tundravm/runtime.json
payload/mkosi.extra/usr/lib/runtime-init/delivery-operator
payload/mkosi.extra/usr/lib/runtime-init/disks
payload/mkosi.extra/usr/lib/runtime-init/keys
payload/mkosi.extra/usr/lib/runtime-init/secrets
payload/mkosi.extra/usr/lib/runtime-init/volume-data
payload/mkosi.extra/usr/lib/systemd/system/attested-tls-443.service
payload/mkosi.extra/usr/lib/systemd/system/credential-raiko-config.service
payload/mkosi.extra/usr/lib/systemd/system/raiko-ready.service
payload/mkosi.extra/usr/lib/systemd/system/raiko.service
payload/mkosi.extra/usr/lib/systemd/system/runtime-init-delivery-operator.service
payload/mkosi.extra/usr/lib/systemd/system/runtime-init-disks.service
payload/mkosi.extra/usr/lib/systemd/system/runtime-init-keys.service
payload/mkosi.extra/usr/lib/systemd/system/runtime-init-secrets.service
payload/mkosi.extra/usr/lib/systemd/system/runtime-init.service
payload/mkosi.extra/usr/lib/systemd/system/runtime-init-volume-data.service
payload/mkosi.extra/usr/lib/systemd/system/ssh-host-keys.service
payload/mkosi.extra/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf
payload/mkosi.extra/usr/lib/systemd/system/systemd-networkd.service.d/50-tundravm-firewall.conf
payload/mkosi.extra/usr/lib/systemd/system/systemd-networkd-wait-online.service.d/50-tundravm-network.conf
payload/mkosi.extra/usr/lib/systemd/system/tdxs.service
payload/mkosi.extra/usr/lib/systemd/system/tdxs.socket
payload/mkosi.extra/usr/lib/systemd/system/tundravm-credentials.service
payload/mkosi.extra/usr/lib/tmpfiles.d/50-tundravm-runtime.conf
payload/mkosi.extra/usr/lib/udev/rules.d/65-tundravm-tdx-guest.rules
payload/mkosi.finalize.d/50-finalize.sh
payload/mkosi.postinst.d/50-postinst.sh
payload/mkosi.profiles/dev/mkosi.conf
payload/mkosi.profiles/dev/mkosi.postinst.d/50-postinst.sh
payload/mkosi.repart/10-root.conf
payload/mkosi.repart/11-root-verity.conf
payload/mkosi.skeleton/etc/systemd/system/minimal.target
```

`mkosi --force summary --json` resolves it: `default-initrd` (mkosi's initrd for the verity
root), `payload` (a `disk`: the erofs root and its hash partition beside the ESP) and `main`;
`--profile=dev` resolves `prover-dev`. The payload's packages include `cryptsetup`, `socat`,
`openssl` and `openssh-server`, none of which the file named
(`test_the_prover_compiles_to_a_tree_mkosi_resolves`).

### What each slot lowered onto

| Slot value | In the tree | Today's mechanism |
|---|---|---|
| `Volume("data", mount=, unlock=OperatorKey())` | `etc/tdx/key-gen.yaml` (`strategy: "pipe"`, `pipe_path: /run/tundravm/unlock/data`, `tpm: false`), `etc/tdx/disk-setup.yaml` (`pattern: /dev/disk/by-id/*-tundravm-data`, `format: on_initialize`), `runtime-init-keys/disks.service`, `cryptsetup` | `Key(strategy="pipe")` + LUKS2 `Disk` (tundra-tools `key-gen`, `disk-setup`) |
| `data.directory("raiko")` bound as `state=` | the `volume-data` step (`install -d -o raiko -g raiko -m 0750 /persistent/raiko`); `raiko.service` `After=`/`Requires=runtime-init-volume-data.service`, `WorkingDirectory=`, `ReadWritePaths=-`, `STATE_DIRECTORY=` | a `BootStep` after `disks` |
| `Secret("raiko-config")` bound as `credentials=` | `LoadCredential=raiko-config:/run/tundravm/credentials/raiko-config`, `--config-path=${CREDENTIALS_DIRECTORY}/raiko-config`, `credential-raiko-config.service` (a oneshot that finishes once the file exists), `etc/tdx/secrets.json` | `Secrets(entries=[Secret(..., SecretFile(...))], delivery=ExternalDelivery("operator", unit="tundravm-credentials.service"))` |
| (volume or secret present) | `etc/tdx/secrets.yaml` (`server_url: 0.0.0.0:8745`, no `store_at`), `ssh.service.d/10-tundravm-needs.conf`, `sshd_config.d/50-tundravm.conf`, `tmpfiles.d/50-tundravm-runtime.conf` | today's control-plane bootstrap: `secret-delivery` takes the operator's SSH key on the management port, `SshAccess(from_delivery=True)`; the evidence service waits for none of it (plan 5.3's deadlock is gone) |
| `AttestedTLS(443, upstream=raiko.http)` | `attested-tls-443.service` (`socat OPENSSL-LISTEN:443,... TCP:127.0.0.1:8080`, a per-start key pair from `openssl req`, its own user, `CAP_NET_BIND_SERVICE` only), `tcp dport 443 accept` | `socat` relay + firewall rule; gate 3 binds the key to the quote |
| `RootFS.verity()` | `mkosi.repart/10-root.conf`, `11-root-verity.conf`, the payload as a `disk` | `Mkosi(root="verity")` |
| every volume, secret and ingress | `etc/tundravm/runtime.json` | the manifest the delivering tools read |

The firewall accepts exactly `22` (control plane), `443` (the ingress) and `8745`
(management); the upstream's `8080` is not opened, and an image whose service exposes it, or
whose upstream is not on the loopback, is refused at construction
(`test_attested_tls_is_the_only_way_in`,
`test_an_ingress_that_would_not_be_the_only_path_is_refused`). `TpmSeal(platform=, pcrs=,
recovery=)` is refused for every target that boots with another vTPM
(`test_tpm_seal_is_refused_where_that_vtpm_does_not_boot`: azure with qemu, azure with gcp,
gcp with azure, swtpm with azure).

## The component library

`tundravm.components`: `TeeBase`, `Network`, `Firewall`, `Tdxs` (the baseline every image
holds, each replaced by passing one), `SshAccess`, `DevAccess`, `Observability` (with
`JournalExport`, `PrometheusRemoteWrite`), and the surge workloads `Nethermind`,
`TaikoClient`, `Raiko`. Each is a `Component` with exports and a `check()`; the exports
resolve across components (`TaikoClient` waits on `nethermind-ready.service` through
`execution.engine_api`, `Raiko` on `tdxs.service` through `EVIDENCE` and on Nethermind
through `l2_rpc`), see `test_endpoints_carry_their_readiness_across_components`.
`examples/fragments/` is gone; the library supersedes it. The starter's tree gains one file over
the part-A listing, `payload/mkosi.conf.d/40-firewall.conf`: the firewall is a component now,
so its `nftables` package sits in its own fragment's configuration.

## Surge, before and after

| | `4931008` | now |
|---|---|---|
| `examples/surge-tdx-prover/image.py` | 151 lines, internal `Recipe`/`Fragment`/`Key`/`Disk`/`Secrets`, plus `contents.py`, `files/`, `examples/fragments/` (347 more lines) and `nethermind_base.py` (152) | **33 lines**, an `Image` over `tundravm.components` |
| profiles | `default`, `azure`, `gcp`, `devtools` (variants) | `production`, `dev`; `targets=("qemu", "azure", "gcp")` |
| committed tree | 62 files | 80 files |

The tree diff, by kind: gone are the historical base (custom `/init`, `network-setup.service`,
static `resolv.conf` in the skeleton, the source-built 6.13 kernel and its config, backports,
openntpd, dropbear, the prometheus defaults, `99-surge.conf`, the two udev rules, the env
files and the three raw units `nethermind-surge`, `raiko`, `taiko-client`, the `devtools`
profile); new are the implied baseline (`20-tee-base`, `30-tdxs`, `50-firewall`, chrony,
nftables, journald and sysctl drop-ins, networkd), the runtime contracts (`40-tundravm-runtime`,
the volume step and its unit, three credential waits, `tundravm-credentials.service`, the
delivery step, `runtime.json`, the tmpfiles file), the typed units `nethermind`,
`taiko-client`, `raiko` with their probes and `attested-tls-443`, the verity partitions in
the QEMU disk and both wrappers, and the `dev` profile. Byte parity with the 2024
nethermind-tdx tree is dropped, as plan decision 9.1 proposed. `compile --check` is green
against the regenerated tree.

One finding the regeneration surfaced: the shared payload's debloat whitelist was the union
of what every compiled profile enables, so `production` compiled alone masked the `dev`
profile's `serial-getty@.service` while the whole tree left it unmasked (still disabled) in
production, and the production payload's bytes depended on which profiles were compiled with
it. Fixed since, see "Profile semantics" below.

## Profile semantics (plan 3.3)

tundravm resolves every profile's whole image, then writes what all of them build once, in
the shared payload, and what each builds beyond it in its own overlay
(`src/tundravm/compiler/_split.py`):

- **Every variant is lowered whatever a compile selects.** A variant that only adds extends
  the default profile as before; one that extends another profile, removes or replaces is
  lowered whole, as a standalone profile, from its resolved declarations (`extends` chain and
  `changes` applied). Only a variant with its own mkosi settings, kernel or source build is
  refused: those are the tree's.
- **The shared payload is the intersection.** Packages, accounts, enablements, unit states
  every profile has; each phase's commands as far as every profile runs the same ones in the
  same order; files, templates, skeleton files, repositories and service units at paths every
  profile ships (the default profile's content; an overlay whose content differs ships its
  own, which mkosi copies later). A declaration one profile removes, or an account it changes,
  moves into the overlay of every profile that keeps it, `production`'s included. The default
  profile's overlay is `payload/mkosi.profiles/<default>/`, included by
  `payload/mkosi.conf.d/99-<default>.conf` under `[Match] Profiles=` (empty: no profile
  selected), so a plain `mkosi build` builds it.
- **Debloat.** The shared payload masks all but the units every profile keeps and removes the
  paths every profile removes; an overlay unmasks the units only it keeps (before its own
  enables) and removes the paths only it removes. Profiles whose debloat configurations differ
  share none: each overlay runs its own. No shared script names a profile any more (the
  `$PROFILES` guards are gone). Kernel modules likewise: the shared `KernelModules=` is what
  every profile keeps, an overlay lists its own.
- **Tests.** `tests/compiler/test_profile_overlays.py`: for every example, surge, the gate 1
  fixture and the new `tests/fixtures/profile-chain/chain.py` (`staging` edits a file and a
  service, `bench` extends `staging` and removes `worker`), the tree outside
  `mkosi.profiles/` is byte-identical whichever profiles are selected, alone or left out;
  `mkosi summary --json` of each profile's selection is the shared payload's summary plus
  exactly that profile's overlay (its extra and skeleton trees, scripts, packages, kernel
  modules, `ImageId=`); in the chain, `worker.service` and its enablement are in the
  `production` and `staging` overlays and nowhere in `bench`'s resolved image.
- **The surge tree** changed in two scripts: the shared payload's debloat whitelist lost
  `serial-getty@.service`, and the `dev` overlay unmasks it before enabling the getty.

## Backend selection

An image file names no backend. `tundravm.recipe.load_file` picks `TUNDRAVM_BACKEND`
(`local`, `lima`, `inprocess`) when set, else this host's (`local` on Linux x86-64, `lima`
everywhere else, `capabilities.default_backend`), and records the origin (`recipe`, `env`,
`host`); a file that still binds `backend = ...` keeps it; the project file's `backend` and
`--backend` override both on the command line, as before; `init` writes `backend` into the
table only when `--backend` names one. Every template and example dropped its backend lines.
The unit suite sets `TUNDRAVM_BACKEND=inprocess` outside integration tests, so a test never
bakes for real by accident.

## The tundra-tools source

`https://github.com/Hyodar/tundra-tools.git` is a private repository, and every `Image`
implies the evidence service built from it (an image with volumes or secrets also builds
`key-gen`, `disk-setup` and `secret-delivery` from it). A CI token scoped to the tundra
repository cannot `lock` or bake any `Image`; locally it works with credentials that can read
it. The source is one explicit, pinnable input:
`components=[Tdxs(source=Git(mirror_or_checkout, ref))]` builds all four from it
(`test_an_explicit_tdxs_builds_the_evidence_and_the_volume_tools_from_its_source`), and
`tundravm lock` pins it. Unit tests never reach it: the hermetic resolver of
`tests/conftest.py` pins that one URL to a fixed commit (`test_the_prover_locks_without_the_network`).

## Public names

| Module | Part A | Now |
|---|---|---|
| `tundravm` | 61 | **73** |
| `tundravm.components` | (none) | 13 |

The twelve new top-level names are `AttestedTLS`, `TLS`, `Volume`, `VolumeDirectory`,
`OperatorKey`, `TpmSeal`, `BrokerKey`, `Secret`, `RootFS`, `Observability`, `JournalExport`
and `PrometheusRemoteWrite`; `Network` and `DevAccess` are now the components of the same
name. `tests/unit/test_public_surface.py` freezes both lists.

## What CI must prove, and what needs gate 3

- **Bake the prover and surge.** Both compile and their trees resolve under mkosi here; CI
  must bake them (the surge stack builds Nethermind, the taiko client and Raiko from source,
  with the network on) and boot them. The verity layout's boot is gate 1's evidence, not
  this gate's. Bakes need read access to tundra-tools, see above.
- **Boot to the waiting state.** A booted prover must reach: evidence service ready, the
  management port taking an SSH key, `credential-raiko-config.service` and
  `runtime-init-keys.service` waiting, Raiko not started. CI can assert that much.
- **Unlock and delivery need gate 3 and a TDX host.** Opening the volume with an operator's
  passphrase and delivering the credential over the attested channel are tundra-tools' gate-3
  work (the attested endpoint on :8745, channel-bound delivery, the LUKS-token passphrase
  path); today's stand-in is writing the FIFO and the credential file over the bootstrap
  SSH, which is unverified first contact. An attested request reaching Raiko through
  `AttestedTLS` needs the quote binding and a TDX host. None of that is evidence here.
