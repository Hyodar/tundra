# Parity: the flashbox example against flashbox-l1

Date: 2026-11-03. Compares `examples/flashbox/image.py` and its committed tree
`examples/flashbox/mkosi/` (current: `tundravm check --tree` reports it up to date) with the
effective mkosi config of flashbots-images `09c7576` for `images/flashbox-l1.conf`:
`shared/` + `modules/flashbox/{common,flashbox-l1,observability}` + profiles `azure,gcp`,
both on at once (`Profiles=azure,gcp`), so the gcp kernel fragment and patch are in the one
image. Paths `fb:` are in that clone. Background: `flashbots-images-gaps-2026-11-02.md`,
`container-2026-11-03.md`, `firewall-modes-2026-11-03.md`,
`operator-key-token-2026-11-03.md`.

Classes: (a) same behaviour, other mechanism; (b) ours stricter on purpose (refusal cited);
(c) ours lacks something flashbox-l1's job needs, a library gap; (d) the example declares it
wrong or not at all although the library supports it.

## Verdict

The OS layer matches: same distribution and release, a snapshot six hours apart, the same
Debian kernel config package (same sha256), the same cmdline mitigations, chrony file, NTS
servers, journald cap, DoT resolvers, sysctls (ours is a superset), debloat list, GCP NVMe
naming, Azure ready report and reproducibility settings. Ours is stricter in most places it
differs: lockdown and a kernel deny list, verity root, metadata scoped per unit, no build
network, a container with no network at all, modes in RTMR3.

The searcher-facing layer is far off. flashbox-l1 is a box a searcher works in; the example
boots the L1 node and a container that cannot yet be logged into, reach order flow, take
input or show delayed logs, and in production the searcher has no channel at all. Two
generated-tree problems (the container's loop loader under a built kernel, and the masked
shutdown path) and a probably missing `e2fsprogs` are new here and want a dev boot to
confirm. The kernel source is 42 stable releases older than flashbox's. (Status after the features landed: the last section.)

## Distribution and pinning

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Distribution | debian trixie x86-64 (`fb:shared/mkosi.conf`) | same | a |
| Snapshot | `20261018T143805Z` (`fb:images/flashbox-l1.conf`) | `20261018T082309Z` (the locked default) | d, optional |
| Backports | `trixie-backports` in sources (`fb:shared/mkosi.sync.d/10-setup-apt.sh`), used for Go/Rust build deps | none | a |
| Tools | nix flake on the host | `ToolsTree=default` at the same snapshot | b (pinned tools) |
| Build network | `WithNetwork=true`; git clone by tag, curl+sha256 | `WithNetwork=no`; host fetches, lockfile pins | b |
| Source pins | kernel by SHA; tdx-init, ssh-pubkey-server, delay-pipe, input-only-proxy, lighthouse by tag | tag/ref until `tundravm lock` (check warns `source-unpinned` 8x) | a once locked |
| Seed, epoch | fixed `Seed=`, `SourceDateEpoch=0`, `IMAGE_VERSION` stripped | same three | a |
| SBOM | syft CycloneDX (`fb:shared/mkosi.postoutput.d/95-gen-sbom.sh`) | SPDX from the manifest | a |

## Packages

Theirs: `fb:shared/mkosi.conf`, `fb:modules/flashbox/*/mkosi.conf`, `gcp` profile. Ours:
the 22 of `tundravm show`.

| Package | Role | Theirs | Ours | Class |
|---|---|---|---|---|
| kmod systemd systemd-resolved systemd-boot-efi udev chrony ca-certificates openssl iproute2 curl sudo nvme-cli | base, cloud glue | yes | yes | a |
| busybox, util-linux, procps, perl | shell tools; perl runs `print-measurements` | yes | no (util-linux is essential anyway) | a |
| e2fsprogs | `mkfs.ext4` for the data disk | yes | **not listed**; `tools/pkg/disks/filesystem.go` runs `mkfs.ext4`, and mkosi installs only `?essential` | c, verify |
| cryptsetup | LUKS2 | v2.8.8 built, kernel backend | Debian's | a |
| podman catatonit runc uidmap passt fuse-overlayfs libdevmapper libjson-c5 | rootless container runtime; **privileged**: `newuidmap`/`newgidmap` setuid; passt is the container's network stack | yes | no: systemd `RootImage=` + `PrivateUsers=` | b (`container-2026-11-03.md`: podman refused) |
| dropbear (+ openssh-sftp-server) | **network-facing root** SSH, port 22 | yes | openssh-server (ssh.service), port 22 in maintenance | a |
| iptables conntrack netfilter-persistent | firewall | yes | nftables | a |
| socat jq logrotate gettext-base xxd | script helpers, log rotation, `envsubst` | yes | no | a |
| libsnappy1v5, zlib1g | lighthouse runtime | snappy | zlib1g | a |
| prometheus, prometheus-node-exporter | metrics | yes | yes (agent mode) | a |
| prometheus-process-exporter | container CPU and liveness | yes | no | c (with recording rules) |
| attested-tls-proxy v2.0.5 (deb, sha256) | **network-facing** 0.0.0.0:8745 | yes | attested-endpoint, 8745 | a |
| tdx-init v0.3.2 | **network-facing root** :8080 key registration, LUKS | yes | key-gen, disk-setup, endpoint | a |
| ssh-pubkey-server | host keys on 127.0.0.1:5001 | yes | endpoint evidence | a |
| input-only-proxy v0.0.3 | **network-facing root** 0.0.0.0:27018 | yes | no | c |
| delay-pipe v0.0.3 | 5-minute log delay | yes | no | c |
| searchersh | forced-command menu | yes | `Login(command=)` | c (known) |
| lighthouse | CL | v8.2.3 from source, features gnosis/slasher/jemalloc | v8.3.2 release tarball, sha256 | a |
| nethermind 1.32.3, libssl3t64 libstdc++6 | EL on the host | no: the searcher's EL runs in the container | yes, P2P out on 30303 | a by design (README) |
| dmidecode, passwd, systemd-sysv | cloud glue, shadow, `reboot` links | no | yes | a |
| tdxs, mode-switch, image-metrics | quote socket, modes, identity metric | no | yes; mode-switch is root with `CAP_NET_ADMIN` and no network | b |

## Kernel

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Source | gregkh/linux `2ff600d29ee4` = **v6.18.57** (`KERNEL_GIT_SHA`) | gregkh/linux tag `v6.18.15` (= `df0dc1b06fb6`), derived from the `DebianConfig` version | d |
| Base config | `linux-config-6.18_6.18.15-1~bpo13+1`, sha256 `7008d383...`, `cloud-amd64` | same file, same sha256 | a |
| Modules | `# CONFIG_MODULES is not set` | same | a |
| GCP ACPI sandbox patch | gcp profile, so every target of the one image | every target | a (identical patch) |
| Merge | `merge_config.sh` base + fragments, `olddefconfig` | same, plus a check that each requested option survived and each target's driver is `=y` | b |
| Deny list | `01-sane-defaults`, `02-disable-unneeded`, `10-bob` | `HARDENING` covers every `n` of theirs, plus `RD_GZIP`/`RD_BZIP2`, `DEVMEM`, `PROC_KCORE`, `KEXEC*`, `MODIFY_LDT`, `X86_IOPL_IOPERM`, `MAGIC_SYSRQ`, `DEBUG_INFO_BTF` | b |
| Hardening on | none | lockdown (confidentiality, early), `INIT_ON_ALLOC/FREE`, kstack randomisation, `BPF_UNPRIV_DEFAULT_OFF`, `BPF_JIT_ALWAYS_ON`, hardened usercopy, fortify, `STACKPROTECTOR_STRONG`, freelist randomisation | b |
| `CONFIG_IPV6=n` | `10-bob` | IPv6 on | b (README: the `inet` table needs IPv6) |
| TCP congestion | `DEFAULT_TCP_CONG="bbr"`, `DEFAULT_BBR=y` | cubic (Debian's default; BBR built in only because `m` becomes `y`) | d |
| Qdisc | `DEFAULT_FQ=y` | `DEFAULT_FQ=y` (the example's fragment) | a |
| AF_ALG for cryptsetup | `USER_API_HASH/SKCIPHER=y`, `AEAD/RNG` off, `AUTHENCESN` off | `SKCIPHER` required, `AEAD/RNG/AUTHENCESN` off, `HASH` from Debian's `m` | a |
| Drivers (virtio, Hyper-V, MANA, GVE, TDX guest, TSM, dm-verity/crypt, nft) | named `=y` in fragments | `driver_required` per target in `10-kernel.sh` | a |
| `TUN`, `KVM`, `CDROM`, `FAT`, `IP_NF_*` legacy | `=y` | Debian's `m` becomes `y` | a (TUN only served pasta) |

## Kernel command line

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Mitigations | `mitigations=auto,nosmt spec_store_bypass_disable=on` | same | a |
| `nospectre_v2` | set | refused | b (`declarative/utils.py`: turns a mitigation off) |
| `transparent_hugepage=madvise` | set | set | a |
| `console=tty0 console=ttyS0,115200n8` | set | not set; the image owns the console | b (`Kernel` refuses console parameters) |
| `systemd.unit=minimal.target` | set | `default.target` links `minimal.target` | a |
| Root | initramfs (`Format=uki`, `fb:shared/mkosi.extra/init` pivot workaround) | `root=/dev/mapper/root systemd.verity_root_*` + `systemd.volatile=overlay` | a |

## systemd units

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Unit whitelist | `fb:shared/mkosi.postinst.d/90-debloat-systemd.sh` | same list plus `systemd-resolved`, `timers.target` and every target an enabled unit orders against | a |
| Reboot path | `fb:modules/flashbox/common/unmask-systemd.sh` unmasks `shutdown/reboot/final/umount/ctrl-alt-del.target`, `systemd-reboot.service` | still masked: no enabled unit names them, so the ordering scan keeps none | c, verify |
| Enabled | chrony, resolved, networkd(+wait-online), print-measurements, wait-for-key, dropbear, ssh-pubkey-server, attested-tls-proxy, searcher-firewall, persistent-mount, searcher-container, input-only-proxy, delay-pipe, logrotate.timer, lighthouse, observability setup/measurement, prometheus, node/process exporter, azure ready report | chrony, resolved, networkd, tdxs.socket, nftables, mode-switch, tdx-evidence, runtime-init(+keys/disks/secrets/volume), endpoint, instance key, volume owner, ssh-host-keys, ssh, credentials, generated jwt, nethermind(+ready), lighthouse(+ready), container(+loop, forward socket), node exporter, image-metrics, metrics-export, azure ready report, gcp hostname, gcp forwarded IPs | a |
| Masked | `ssh.service ssh.socket` (dropbear takes 22) | `apt-daily*`, `dpkg-db-backup`, `getty@tty1`, `prometheus` (package unit), `nvmf-*`, `uuidd*` | a |
| Container loop loader | none | `searcher-container-loop.service` runs `modprobe loop`, which the container `Requires=`; with `Kernel.build` `/usr/lib/modules` is gone and `loop` is built in, so modprobe has nothing to look it up in | c, verify |
| Unit sandboxing | none on lighthouse, exporters, proxies | `static`/`dotnet` hardening on every service | b |

## Users and groups

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Searcher | uid 1000, home `/home/searcher`, shell `/usr/bin/searchersh`, owns rootless podman | `searcher`, shell `/bin/sh`, `ForceCommand` | a |
| Container identity | container root = host uid 1000 (subuid map) | `searcher-container` system user, `PrivateUsers=yes` | a |
| Services | `lighthouse` in group `eth`; `prometheus` | one system user per service and forward | b |
| Root | `passwd -l root`; `su` mode 700 | Debian's locked root; root SSH by key (the owner) | c (known: owner login without root) |
| Determinism | `pwck --sort`, `grpck --sort` | sort by id, then shadow/gshadow alike | a |

## Sysctls and journald

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| `fs.protected_symlinks/hardlinks=1`, `kernel.perf_event_paranoid=3` | yes | yes | a |
| KSPP set (`kptr_restrict`, `dmesg_restrict`, `unprivileged_bpf_disabled`, `bpf_jit_harden`, `kexec_load_disabled`, `ptrace_scope=2`, `sysrq=0`, `suid_dumpable=0`, `protected_fifos/regular`, redirects, `rp_filter`, syncookies) | no | yes | b |
| `vm.max_map_count` | 2097152 (`fb:modules/flashbox/common/mkosi.extra/etc/sysctl.d/sysctl.conf`) | the default (65530, or 1048576 where systemd ships `10-map-count.conf`) | d |
| Journal | volatile, 2 x 128M files | volatile, `RuntimeMaxUse=128M`, no console forwarding | a |

## Debloat

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Paths (`/usr/share/{doc,man,info,locale,...}`, hwdb, catalog, user units, pcrlock, credstore, machine-id, host keys, `/etc/*-`, logs, caches) | `fb:shared/mkosi.finalize.d/90-debloat.sh` | same | a |
| `/usr/lib/tmpfiles.d` | four files | the whole directory, and `/usr/lib/systemd/network` | b |
| `/var/lib/ucf`, `/.cache`, `/nix` | removed | kept (the gaps note's remainder) | a |
| systemd binaries | keeps `systemctl journalctl systemd systemd-tty-ask-password-agent systemd-tmpfiles` | same less `systemd-tmpfiles` | a |

## Firewall, per mode

Theirs: iptables, `fb:modules/flashbox/common/mkosi.extra/usr/bin/init-firewall.sh` and
`fb:modules/flashbox/flashbox-l1/mkosi.extra/etc/bob/firewall-config`, IPv4 only, the
container's traffic leaving as a host process (pasta) and trimmed by DROPs in its netns.
Ours: `etc/nftables.conf`, one `inet` table, the container in an empty netns. P = production,
M = maintenance.

| Port / destination | Dir | Theirs P | Theirs M | Ours P | Ours M | Class |
|---|---|---|---|---|---|---|
| tcp 22 control SSH | in | open | open | closed | open | c (known: no non-root menu to leave open) |
| tcp 8080 key registration | in | closed | open | n/a: owner key over 8745 | n/a | b (signed, attested first) |
| tcp 8745 attested TLS | in | open | open | open | open | a |
| tcp 10022 container SSH | in | closed | open | closed | open | a |
| udp 27017 input channel | in | open | open | none | none | c (known: UDP) |
| tcp 27018 input-only-proxy | in | open | open | none | none | c |
| tcp+udp 9000 CL P2P | in, out | open | open | open | open | a |
| udp 9001 CL QUIC | in, out | closed (`--disable-quic`) | closed | open | open | d |
| tcp+udp 30303 EL P2P | in | closed | open | closed | closed | a (EL on the host dials out) |
| tcp+udp 30303 EL P2P | out | closed | open | open | open | a (trusted host EL, not the tenant's) |
| udp 123, tcp 4460 NTP/NTS | out | open | open | open | open | a; tenant blocked by its netns DROP vs no route: b |
| 18.221.59.61, 3.15.88.156 :443 bundles | out | open | open | none | none | c (known: container egress) |
| 3.136.107.142, 3.149.14.12 :443 tx stream | out | open | DROP | none | none | c (known) |
| six BuilderNet IPs :443 | out | open | DROP | none | none | c (known) |
| 10.88.0.100 metrics proxy | out | :443 | :443 | :3128 | :3128 | c (proxy kind, below) |
| tcp/udp 53 DNS | out | closed | open | closed | closed | a |
| tcp 853 DoT | out | closed | any | 1.1.1.1, 1.0.0.1 | same | a |
| tcp 80 any | out | closed | open | closed | closed | a for the host; tenant: c (known) |
| tcp 443 any | out | closed | open | **open** (lighthouse checkpoint host, lowered to the port) | open | c |
| 169.254.169.254 | out | closed (tenant DROP) | :80 via the HTTP rule, :123 | per unit by `skgid`, :123; else DROP | same | b |
| 168.63.129.16 Azure wire server | out | closed | :80 | `azure-complete-provisioning` only | same | b |
| loopback, 127/8 spoof DROP | both | explicit | explicit | `lo` accept; kernel drops martians | same | a |
| ICMPv6, DHCPv4/6 | both | n/a (no IPv6) | n/a | listed types | same | b (README) |
| FORWARD | | DROP | DROP | DROP | DROP | a |

## Mode switching

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Modes | production, stopped, maintenance | production, maintenance | b (`firewall-modes-2026-11-03.md`) |
| Boot mode | maintenance | production | b (same) |
| Cooldown | 5 min on `date +%s` (host-set) before maintenance | maintenance closes after 30 min of `CLOCK_BOOTTIME` | b |
| Who switches | `searcher` via searchersh + sudo `toggle` | the owner's signed `tundravm deploy switch` over 8745 | b |
| Old flows | `conntrack -D` per endpoint/port (`toggle-config`) | `ct mark` per mode | a |
| Pre-check | container netns DROPs present before production | nothing to check: the netns is empty | b |
| Evidence | none (state file in `/etc`) | RTMR3 extend per switch, `modes` policy | b |
| Reboot | `/usr/local/bin/reboot`, maintenance only | none in-guest (see reboot path) | c |

## Container

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Root | `docker.io/library/ubuntu:24.04` by tag, `apt-get install openssh-server logrotate cron` at every start, writable `vfs` layer in RAM | Debian erofs subimage from the snapshot, `openssh-server`, read-only, in the measured root | b (`container-2026-11-03.md`: tags and registries refused) |
| Writable root | yes: the searcher installs tools (the test apt-installs curl and puts reth in `/usr/local/bin`) | none outside `state=` | c |
| Runtime | rootless podman, runc, `--init`, custom `seccomp.json` | `RootImage=`, `PrivateUsers=yes`, `@system-service` minus `@privileged @resources`, `MemoryDenyWriteExecute`, empty capability set | b, but see the next row |
| Sandbox knob | n/a | none on `Container`: MDWE stops JIT runtimes and the empty capability set stops sshd's privilege separation | c |
| Network | pasta; host firewall plus netns DROPs (metadata, proxy, 9000, 123, 4460, input reply ports) | empty netns, `lo` only | b |
| Ports in | 10022->22, 27017/udp, 8551 (EL engine API to the host CL), 30303 tcp+udp | 10022 via `systemd-socket-proxyd` | c (known: UDP, host endpoints) |
| Mounts | `/persistent/searcher`, `/persistent/input`, host key dir ro, `searcher_logs`, logrotate conf ro, `lighthouse_logs` ro, `/tmp/jwt.hex` ro | `/persistent/data/searcher` | c (read-only binds of host paths; jwt is the known host-endpoint gap) |
| Static hosts | order-flow names in the container's `/etc/hosts` (production has no DNS) | none | d once egress lands (`files=` can carry it) |
| SSH inside | root with the searcher key, host key made on the host and bound ro | sshd as a system user, no account, no host key | c (known) |

## SSH and login

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Daemon | dropbear `-s -w -g -m -j -k`, IPv4 only | OpenSSH, keys only, `MaxAuthTries 3`, `DisableForwarding`, `PermitTTY no` for searcher | a |
| Host key | ed25519 per boot, published through ssh-pubkey-server on 8745 | `ssh-keygen -A` per boot, bound in the endpoint's evidence | a |
| Searcher's key | POST to :8080 at first boot, kept in the LUKS header | first signed write on the quote-bound session, kept in LUKS2 token 2 | b |
| Menu | `initialize`, `toggle`, `status`, `logs N`, `tail-the-logs`, `restart-lighthouse`, `reboot [--force]`, `input-cert` | one command: the container unit's journal | c (known: menu) |
| In production | control SSH stays open, menu only | no SSH | c (refines the known gap: the menu has to be safe to leave open) |
| Delayed logs | bot writes `/persistent/searcher_logs/bob.log`, delay-pipe copies it 300 s late, `logs`/`tail` read the copy, any mode | journal now, maintenance only | c |
| Root | locked; owner never root | owner gets root by key in maintenance | c (known) |

## Disk encryption and persistent mount

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Unlock | searcher types a passphrase into `initialize` (tdx-init set-passphrase) each boot | `tundravm secrets unlock` over the quote-bound session | a |
| Owner pinning | key in the LUKS header, one key ever | token 2 plus an in-volume copy | a |
| Mount | `/persistent` | `/persistent/data`, one directory per component | a |
| Filesystem | ext4 | ext4 (`mkfs.ext4`: see e2fsprogs) | a |
| Device | tdx-init's discovery, same on GCP and Azure | `/dev/disk/by-id/*-tundravm-data` (GCP name) | c (known: Azure volumes) |

## Time and DNS

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| chrony | cloudflare + netnod NTS, two metadata clocks, `maxdistance 1.0`, `makestep 1 3` | same, plus `ntsdumpdir` | a |
| Resolver | resolved, DoT to 1.1.1.1/1.0.0.1; DHCP DNS for `~internal` | resolved, strict DoT to the same two, no DHCP DNS, no fallback, no LLMNR/mDNS | b |
| Metadata name | `~internal` via DHCP DNS | `metadata.google.internal` in `/etc/hosts` | a |

## Observability

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Exporters | node (selected collectors) and process on 127.0.0.1 | node exporter on 127.0.0.1 (Debian's collectors), image-metrics textfile | a |
| Container metrics | process-exporter group `searcher-container` | none | c |
| What leaves | seven `flashbox:*` recordings: image measurement, container alive, CPU < 80 % (spike guarded), OOM kills, `/` > 10 %, `/persistent` > 128 GB, network up | raw `node_filesystem_*`, `node_memory_*`, `node_vmstat_oom_kill`, `up`, boot time, `tundravm_*` identity, mode, failed units | c (known: recording rules; the raw volume and memory series are a side channel) |
| Instance label | `host=` from the GCE instance name | none | b (configuration from metadata refused, gaps note section 7) |
| Sink auth | SigV4 from Vault (GCE identity JWT) | bearer token delivered by `secrets deliver` | b (README: SigV4 and Vault refused) |
| Proxy | `https://10.88.0.100/...` with `tls_config.server_name`: a TLS passthrough on 443 | HTTP CONNECT at `10.88.0.100:3128` (`Observability(proxy=)`) | c |
| Image identity | UKI Authenticode from the CCEL (RTMR1 event) | `tundravm_image_digest`, `tundravm_image_rtmr` | a |
| Prometheus | full server, 24 h TSDB, config rendered at boot | agent mode, 1 h buffer, static config | a |

## Measured boot, formats and targets

| Item | flashbox-l1 | example | Class |
|---|---|---|---|
| Boot payload | one UKI holding the whole root (`Format=uki`), `.osrel` stripped | UKI + erofs root + verity hash partition, root hash on the measured cmdline | a |
| RTMR use | 1 = UKI, 2 = cmdline; 3 unused | same, 3 = mode switches | b |
| `print-measurements` | RTMR0-3 to the console at boot | evidence through the endpoint; nothing on the console | a |
| GCP | `.tar.gz` of `disk.raw`, fixed disk GUID, nvme-cli + `google_nvme_id` + `65-gce-disk-naming.rules` | same format, seeded GUIDs, newer naming rules (bare-metal branches) | a |
| Azure | fixed VHD, ready report before the firewall | fixed VHD, ready report with its own firewall rule | a |
| QEMU | the `.efi` | `qemu` disk | a |
| Dev | `devtools` profile: root password, serial bash, sshd with password | `dev`: serial getty, root password, probe | a |

## Run at boot by flashbox-l1, absent here

| Theirs | Ours | Class |
|---|---|---|
| `setup-input-directory.sh` + `input-only-proxy` | none | c |
| `delay-pipe`, `logrotate.timer` on delayed logs | none | c |
| container host key made on the host, ro bind, `/pubkey` | none | c (known) |
| in-netns DROPs after `podman run`; static hosts injected | empty netns | b; static hosts d later |
| `flashbox-observability-setup` (Vault, metadata) | `secrets deliver` | b |
| `lighthouse-init` (JWT in `/tmp`, world-readable) | `tundravm-generated-jwt`, credential | b |
| `unmask-systemd.sh` (reboot path) | none | c |

## Status of the gaps and fixes

The tables above describe the example before the library features landed (master `69a503cf`).
This one says what closed each earlier gap (c) and example fix (d).

| Item | Class | Status | By |
|---|---|---|---|
| Container egress, per mode | c | closed | `Container(egress=, modes=)` |
| Host endpoints in the container | c | closed | `Container(requires=[nethermind.rpc])` |
| UDP into the container | c | closed | `Container(expose=[UDP(27017)])` |
| A container endpoint for the host | c | closed, unused | `serves=`, `probe=`: the execution client stays on the host |
| Container SSH account and host key | c | closed | `Container(ssh=ContainerSsh(...))`, quote-bound key |
| Sandbox knob on `Container` | c | closed in part | without `argv` the program is sshd and its sandbox is tundravm's; `hardening=` is refused |
| Owner login without root | c | closed | `Login("searcher", owner=True)` |
| Command menu open in production | c | closed | `Login(commands=)`, 22 in no mode |
| Recording rules, container CPU and liveness | c | closed | `rules=`, `tundravm_unit_*` series |
| Metrics proxy kind, SigV4 | c | closed | `Proxy(passthrough=True)`, `SigV4` |
| Azure volumes | c | closed | `deploy azure --volume` |
| `e2fsprogs` for the data disk | c | closed | a volume brings it (in the package list) |
| Container loop loader under a built kernel | c | closed | `loop` is built in, the loader is skipped when `/dev/loop-control` exists |
| Masked shutdown and reboot targets | c | closed | the whitelist keeps them |
| Delayed log channel | c | closed | `channels/cmd/delay-pipe`, a `Service` on the container's account, the menu's `logs` |
| Input-only proxy | c | closed in part | `channels/cmd/input-proxy` on tcp 27018, a bind for the socket; plain TCP, no TLS |
| Writable container layer | c | open | needs a uid-range user namespace: Franco's decision |
| Jwt in the container | c | open | a control-plane secret cannot be bound |
| Lighthouse logs in the container | c | ~~open~~ | ~~no log-directory keyword on the example Lighthouse~~ (done: flashbox-lacks) |
| 443 to any address from the host | c | closed | a host name's egress is the addresses it resolves to (`egress-resolve`) |
| Egress that excludes (maintenance drops the tx stream) | c | ~~open~~ | ~~`Egress` only adds~~ (done: flashbox-lacks) |
| Kernel v6.18.57 at `2ff600d29ee4` | d | closed | `Kernel.build("6.18.57", Git(...), base=DebianConfig(...))` |
| BBR | d | closed | fragment `TCP_CONG_BBR`, `DEFAULT_BBR` |
| `vm.max_map_count` | d | closed | `Image(sysctl={"vm.max_map_count": 2097152})` (`9b90896a`), which writes the same `/etc/sysctl.d/60-flashbox.conf` |
| Lighthouse flags | d | closed | `disable_quic`, `disable_optimistic_finalized_sync`, `state_cache_size` |
| `ca-certificates` | d | closed | dropped from the image's packages |
| Snapshot `20261018T143805Z` | d | closed | `Debian("trixie", snapshot=...)` |
| Order-flow `/etc/hosts` | d | closed | `Container(files=)` |
| Dev profile points metrics at the in-guest sink | new | closed | a development profile ships observability to the in-guest receivers (`06001135`) |
| Nethermind's single-file bundle needs a writable extraction directory | new | closed | `DOTNET_BUNDLE_EXTRACT_BASE_DIR` in its data directory; found by the CI boot, where it crash-looped |
| Nethermind reads its configs and chainspecs beside its executable | new | closed | the component installs `configs`, `Data` and `chainspec` from the source tree |
| .NET lists network interfaces over netlink | new | closed | the `dotnet` preset allows `AF_NETLINK` |
| `/run/nologin` turns the searcher's login away | new | closed | a `Login` enables `systemd-user-sessions.service` |

## What the CI boot showed

The dev profile builds (the 6.18.57 kernel, Nethermind, Lighthouse) and boots under QEMU in
simulated mode, and the workflow drives it end to end: attest as the owner, deliver the keys,
unlock the volume; Nethermind, Lighthouse and the container start; the menu answers in
production and the container's SSH is closed; `toggle` opens maintenance and the operator
enters the container on 10022 with the host key the evidence binds; a line fed to the input
channel reaches the container; a log line is held back by the delay; closing the window shuts
the container's SSH again and the delayed log releases the line. Two probe rows fail on a
runner and the workflow names them: the bundle relays answer only flashbots' peers, and no
program listens on 27017/udp without BoB.
