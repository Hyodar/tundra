# Gate 4 evidence, part A: the concise SDK

What this records: the starter of plan section 3.1 as a file, the tree it compiles to, the
units in that tree, and the public name count before and after. The prover transcript of
section 3.1 needs the slots part B adds (`volumes`, `secrets`, `ingress`, `rootfs`,
`observability`) and is not evidence here; the surge example therefore still writes the
internal declarative API and still compiles (`compile --check` in CI).

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

## What this gate does not yet show

- The prover transcript of plan 3.1: `volumes`, `secrets`, `ingress`, `rootfs` and
  `observability` are part B, and so is `tundravm.components`.
- A boot. The starter compiles and its tree resolves under mkosi here; CI must bake it and
  boot it, and that run is what gate 4 is accepted on.
- Chained profiles. `Profile(extends="other")` resolves, and the compiler refuses by name to
  emit a chain it cannot express as a single mkosi profile overlay (so does a profile that
  removes a declaration). That restriction is the compiler's, not the model's, and plan 3.3
  leaves it to the same gate that rewrites the overlay emission.
