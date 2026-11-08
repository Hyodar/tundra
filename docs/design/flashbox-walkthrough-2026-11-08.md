# Walkthrough: the flashbox example, read as a stranger

Date: 2026-11-08. Base: master at `28485b96`. Run from `examples/flashbox/`: `check`,
`check --tree mkosi`, `check --explain` of each code, `show` (plain, `--json`, `--dev`,
`--profile`), `show --diff ../attested-node/image.py image.py`, `lock --check`,
`convert --from mkosi mkosi`, `build --tree-only` into a scratch directory, `doctor`,
`deploy --help`. Then `image.py` line by line and the README paragraph by paragraph.

In-flight and landed work, so nobody fixes a finding twice. `pinned-hosts` (in flight)
owns `BUNDLES`, `TX_STREAM`, `BUILDERNET`, `HOSTS`, the `f"{a}:443"` hosts and the
container's `/etc/hosts`. `metrics-rules` landed in `3ec1469c`/`913852f5`: the `local:`
prefix and `EXPORT` are gone. `login-firewall` and `image-sysctl` landed. Findings that
touch their ground carry a tag.

## The image file

- Lines 17-40: nine import statements from nine modules (`tundravm`, `.components.access`,
  `.container`, `.network`, `.observability`, `.ports`, `.secrets`, `.sources`, `.storage`).
  The image file should read `from tundravm import Bind, Container, ContainerSsh, ...`. SDK.
- Lines 45 and 112: `DELAY = 300`, then `str(DELAY)`. The argv refuses an int: `argv= holds
  300, which is not a word.` The argv should take `DELAY` as it is. SDK.
- Line 121: `f"0.0.0.0:{INPUT_TCP}"` repeats the `expose=[TCP(INPUT_TCP)]` five lines down.
  It should read `port = TCP(27018)`, then `[..., port.address, ...]` and `expose=[port]`. SDK.
- Line 84: `data.directory("input", mode=0o755)` carries a raw octal mode only so the
  container can traverse the directory. A directory that a `Bind` names should get that
  mode from the bind. SDK.
- Line 88: `Container("searcher-container", ...)`. The name leaks into the account the
  operator types (`ssh -p 10022 searcher-container@HOST`), the unit names, a truncated veth
  `ve-searche-0bd8` and a user `searcher-container-endpoint-8545`. Read `Container("bob",
  ...)`, which gives `bob.service`, `ve-bob` and `bob@HOST`. Example fix. The SDK half of
  the fix: name the relay unit after the endpoint (`bob-nethermind-rpc`), not its port.
- Line 89: `socat` is in the production container only because the CI's input test
  listens with it. Drop it, or say so in a comment. Example.
- Line 92: `ContainerSsh(..., keys=Secret("searcher-keys"))`. The searcher already owns the
  instance through `Login("searcher", owner=True)`, and the CI delivers the same key a
  second time. It should read `ContainerSsh(port=10022, owner=True)`. SDK.
- Lines 95-99 and 130-133: the mode names `"production"` and `"maintenance"` are typed in
  two places. The container's SSH port is typed twice too (`SSH_PORT` in `ContainerSsh`
  and in `Mode(ingress=[TCP(SSH_PORT), ...])`). It should read
  `Mode(ingress=[searcher.ssh])`. SDK.
- Line 98: `Egress(ports=[TCP(53), UDP(53), TCP(80), TCP(443), TCP(853)])` means "the
  web", spelled with a type the API page calls "An inbound TCP port". Read `Egress.web()`.
  SDK.
- Line 102: `Bind(input_dir.path, writable=True)`. `Bind` takes a string, so the
  `VolumeDirectory` is unwrapped by hand. It should read `Bind(input_dir, writable=True)`.
  SDK.
- Lines 106-116: `"delay-pipe"` appears three times: the service, the `GoBinary` and
  `package="./cmd/delay-pipe"`. The same holds for `input-proxy`. It should read
  `GoBinary(channels, package="./cmd/delay-pipe")`, with the name taken from the path. SDK.
- Lines 114 and 124: `user=searcher.run_user` puts two host services on the tenant's uid,
  with no comment (the tree section has the cost).
- Line 132: `TCP(30303), UDP(30303)` in maintenance. In flashbox-l1 the execution client
  lived in the container. Here it opens the host's Nethermind to inbound peers only during
  a window. It should read `Nethermind(expose_p2p=True)` (a new keyword, like
  Lighthouse's), or the port should go. Example (`components/nethermind.py`).
- Lines 142 and 145: `Sudo(["/usr/bin/tail", "--lines", "{lines:int:1-10000}", ...])` and
  `Sudo(["/usr/bin/systemctl", "reboot"])`: binary paths and a placeholder mini-language
  beside `firewall.status`. `logs` is root only because delay-pipe writes 0600. Read
  `ssh.reboot` and a non-root `["tail", ...]` over a group-readable directory. SDK.
- Lines 150-180: the PromQL is f-strings with doubled braces,
  `f'max(tundravm_unit_active{{unit="{searcher.unit}"}})'` and
  `f'node_filesystem_avail_bytes{{mountpoint="{data.mount}"}} > bool {128 * 1024**3}'`.
  The first form came from `913852f5`. Selectors would remove them:
  `f"max({searcher.metrics.active})"` and `f"{data.metrics.available} > bool 128Gi"`. SDK
  (`metrics-rules` ground, after that work).
- Line 165: `flashbox:container_oom_kills_count` is `node_vmstat_oom_kill`, which counts
  the whole VM's OOM kills. Rename it `flashbox:oom_kills_count`. Example.
- Lines 177-178: `device!~"lo"` also counts the container's veth, so traffic between the
  container and the host keeps `network_is_up` true. It should read `device!~"lo|ve-.*"`.
  Example.
- Line 168: `mountpoint="/"` on a volatile overlay root measures RAM. Say if that is meant.
  Example.
- Lines 188-192: kernel `"6.18.57"` on `DebianConfig("6.18.15-1~bpo13+1")`. Nothing says
  why the base config is 42 releases older. Add one comment. Example.
- Line 214: `network=Network(egress=Egress())`. An empty `Egress()` looks like "nothing
  goes out". In fact it turns the filter on, and `show` lists it as a component. Filtering
  should be the default, or the flag should be named `Network(filter_egress=True)`. SDK.
- Docstring, lines 7 and 9-11. "Only recorded health booleans leave the image" is false:
  `tundravm_image_digest`, `tundravm_image_rtmr` and `tundravm_mode` leave too, and one
  record is a count. The jwt paragraph explains an SDK limit and repeats the README
  word for word. Example.

## The outputs

`check image.py`:

- `warning source-unpinned [image] sources.attested-endpoint: source build
  'attested-endpoint' (local tundravm:tools) is not pinned in the lockfile`. Six of the ten
  warnings are tundravm's own tools, which the image never declared. Fold them into one
  line: "tundravm's tools (6): pinned by `tundravm lock`".
- The hints are grouped wrongly. `hint (all 8 above)` follows `mode-switch`, then
  `nethermind` gets its own hint, then `tdxs` repeats the first hint alone.
- `note: no lockfile at build/tundravm.lock: a production build needs one (lock-missing)`.
  But `--explain lock-missing` prints `lock-missing: error`.
- `info hardening-relaxed [image] nethermind.service: ... MemoryDenyWriteExecute,
  RestrictAddressFamilies` with `at: image.py:49` and `keep only the ones the program
  needs`. The relaxation is the Nethermind component's `dotnet` preset, and the user cannot
  act on it. It should say "the dotnet preset: the JIT needs W^X off, .NET needs AF_NETLINK".
- `lock --check` prints `run tundravm lock and commit it`. The repository's `.gitignore`
  ignores `build/`, so the example cannot commit its lock. Every stranger sees ten warnings.

`check --explain source-unpinned`: `error when the policy refuses moving refs`. "Policy"
is defined nowhere on that page, and elsewhere it means `measurements.json`.

`show image.py`:

- The Firewall section (16 rows) has no row for the container's 27017/udp, the DNAT of
  10022, the bundle hosts, the production tx stream or the maintenance ports. That is half
  of the ruleset (lines 62-99 of `nftables.conf`).
- `"status": Sudo(["/usr/bin/mode-switch", "status"])` is printed for `firewall.status`
  (landed by `login-firewall`, but `show` does not speak it). The same goes for
  `login=[Login(...)]` for `login=Login(...)`, `execution=Endpoint("http://127.0.0.1:8551/")`
  for `nethermind.engine_api`, and `Volume("data", unlock=OperatorKey()).directory(...)`
  three times. The help promises "the image as its file wrote it".
- `Build options: kernel_cmdline=mitigations=auto,nosmt,spec_store_bypass_disable=on,transparent_hugepage=madvise root=verity`.
  Three parameters are joined by commas, and one of them holds a comma.
- `in 30303/tcp ... maintenance: allow`: "allow" says nothing about who asked.
- `out 123/udp, @host4_time.cloudflare.com:4460/tcp, ...  tee-base`: nft set names and
  component class names (`tee-base`, `network`, `observability`) stand where host names and
  units should be.
- `searcher-container  Container / hardening custom`: "custom" names nothing.
- `probe no` on `searcher-container`, while the probe has `container:searcher-container:*`
  rows (the workflow filters two of them).
- `runs tee-base-resolv-conf (postinst)` is a script id among prose lines.
- `Built into dev:` now repeats all 118 production lines. At `b0a5afba` it said
  `Built into dev, beyond production:`, a 12-line delta.
- The dev profile still lists `secret metrics-aws  the operator, over the endpoint` and
  `credential-metrics-aws.service`. The in-guest export it now uses never reads that
  secret, but the CI delivers an AWS file anyway.

`show --diff ../attested-node/image.py image.py`: `Built into staging:` prints about 100
`-` lines for a profile that only one side has. It should print one line. Program lines
are cut before the difference: `--JsonRpc.Ena...` against `--JsonRpc.E...`.

`convert --from mkosi mkosi`:

- Ten `refused:` lines carry the same reason, `generated by the Image file that wrote this
  tree (...); convert that file's image, not its tree`. One header line would do.
- `refused: mkosi.images/searcher-container=Format=portable: a subimage; tundravm's tree
  holds the payload, its QEMU disk and one wrapper per cloud target`. That reason is false:
  tundravm wrote this subimage.
- `kept: ManifestFormat=json: the tree's images are payload (the UKI), qemu ...` gives a
  reason that has nothing to do with the setting, and `Output=` and `SplitArtifacts=` reuse
  it.
- The converted file has 11 packages that the components imply, and no hint that the round
  trip loses everything else.

`doctor`:

- `build:go blocked: ... (every image's Tdxs is one)`. It names a class instead of this
  image's `delay-pipe` and `input-proxy`.
- `verify:qemu ready (built-in TDX replay 1.0+attest-12f1e29.dstack-mr-gcp-b16e08b)`
  prints an internal version string.

`deploy --help`: `Azure is refused: its disks have no name the image can find` is stale.
`deploy/azure.py` attaches volumes by LUN, and the image ships
`81-tundravm-azure-volumes.rules`.

`Service("x", ["/usr/bin/true", 300])`: `fix: Pass strings, an Endpoint a component
exported, or a Secret.` The hint leaves out `GoBinary` and `VolumeDirectory / "file"`, which
this image uses.

## The generated tree

- Host egress is unscoped. `tcp dport 9000 accept comment "lighthouse"`, `tcp dport 30303
  accept comment "nethermind"`, `ip daddr 10.88.0.100 tcp dport 443 accept comment
  "observability"` and the checkpoint and DoT rules accept any host process. That includes
  the two services on the tenant's uid. The metadata rules show that `meta skuid`/`skgid`
  scoping already exists.
- The tenant's uid runs host processes. `delay-pipe` and `input-proxy` run as
  `User=searcher-container`. The container has `MountAPIVFS=yes` and no `PrivatePIDs=`, so
  it can list and signal them, and read their `/proc` entries. An operator would ask
  for `PrivatePIDs=yes`, or for a separate account in a shared group.
- `UMask=0077` makes `output.log` 0600, which is why the menu's `logs` needs a sudoers line
  for `tail` as root.
- `searcher-container-net.service` is root with every capability. It runs
  `ExecStart=/bin/sh -c 'echo 1 >/proc/sys/net/ipv4/ip_forward'`. Since `image-sysctl`
  landed, this belongs in `sysctl.d`.
- `searcher-container-loop.service` runs `modprobe loop` with `TimeoutStartSec=5min`,
  `After=runtime-init.service` and no sandbox. On a built kernel with `loop` built in, its
  condition always skips it. It should not be emitted at all.
- `metrics-export` is a Prometheus server (rules need it) on `127.0.0.1:9090`, with an open
  query API. Any host process reads every raw series that the keep-list holds back.
- The keep regex ends in `|tundravm_image_digest|tundravm_image_rtmr|tundravm_mode`, and
  `show` does not list those three series.
- The rules group is `name: tundravm` and should carry the image's name.
- `observability-hosts` (production profile) appends to `/etc/hosts` at start as root
  (`ExecStartPre=+`), so the running file is not the 69-byte `/etc/hosts` that `show` lists
  (`pinned-hosts`).
- Every `minimal.target.wants/` link points at `/etc/systemd/system/<unit>`, where no unit
  file lives (`payload/mkosi.postinst.d/50-postinst.sh`). systemd loads by link name, so the image boots.
  But the links dangle, and no comment says why. `mode-switch.service` says
  `WantedBy=multi-user.target` and is linked into `minimal.target.wants`.
- `searcher-keys` lands twice. One copy is `/run/tundravm/credentials/searcher-keys`
  (0600), which `runtime.json` lists with `"units": []`. Three files (`secrets.yaml`,
  `secrets.json`, `runtime.json`) each describe the secrets differently.
- `Description=delay-pipe.service`: a `Service` has no `description=`.
- `mkosi.profiles/dev/mkosi.extra/etc/nftables.conf` is a second 137-line ruleset. It
  differs from production's in one line (the proxy rule), so a review cannot see that one
  line.

## The README

- "SSH on 22 in production ... the menu stays open in every mode instead" contradicts
  itself and `show`, which lists `in 22/tcp ssh.service` in every mode. The refusal is
  "22 as a mode port".
- "Production mode reaches only the order flow". The bundles are reachable in every mode,
  and maintenance also opens 30303 and lets the container reach 443 anywhere.
- "Only the searcher's key opens the disk". A passphrase opens it, and the searcher's key
  signs the unlock.
- The console block runs `tundravm check --tree mkosi` before any lock, so the first thing
  a stranger sees is ten warnings. `dev` writes a missing lock, so the `lock` line after
  it reads as redundant.
- `ssh searcher@HOST` and `ssh -p 10022 searcher-container@HOST` skip the known_hosts file
  that `attest` writes (`.tundravm/instances/flashbox-1/known_hosts`), which the CI passes.
  A stranger gets a trust-on-first-use prompt instead of the pinned key. `flashbox-1` is
  never introduced.
- `uv run tundravm` from `examples/flashbox/` fails with `Failed to spawn: tundravm` and
  leaves a `.venv` and a `uv.lock`: uv takes the `pyproject.toml` without `[project]` as its
  own project.
- "What tundravm still lacks". The removed bullet about dev metrics is right to be gone,
  but dev still requires `metrics-aws`. The other five bullets are still true. One is
  missing: a per-unit OOM count, so the "container" OOM record counts the VM.

## Ranked fixes

SDK first. S means under a day, M means a few days.

1. `show` lists the container's forwarded rules (DNAT in, per-mode egress) under Firewall. M, `src/tundravm/_describe.py`.
2. Host egress rules carry the declaring unit's `meta skuid`/`skgid`, as metadata rules do. M, `src/tundravm/components/_network.py`.
3. `show` prints references as the file wrote them: `data.directory(...)`, `nethermind.engine_api`, `firewall.status`, `login=Login(...)`. M, `src/tundravm/_describe.py`.
4. `ContainerSsh(owner=True)` takes the owner's key, so `Secret("searcher-keys")` and its second delivery go. S, `src/tundravm/components/_container.py`.
5. `Mode(ingress=[searcher.ssh])` takes a container's sshd, so the port is typed once. S, `src/tundravm/components/_network.py`.
6. `Bind` takes a `VolumeDirectory` and sets the mode the container needs. S, `src/tundravm/components/_container.py`.
7. Host services beside a container: `PrivatePIDs=yes` on the container, and a `group=` directory whose files are group-readable (UMask 0027), so `logs` needs no sudo. M, `src/tundravm/components/_container.py`, `src/tundravm/_image/_service.py`.
8. A development profile that exports in the guest stops requiring the production sink's credential. S, `src/tundravm/components/_observability.py`.
9. The image-file vocabulary is re-exported from `tundravm`, one import line. S, `src/tundravm/__init__.py`.
10. Argv words: an int as decimal, `TCP(p).address` as `0.0.0.0:p`; the error hint lists every accepted kind. S, `src/tundravm/_image/_service.py`.
11. `GoBinary(channels, package="./cmd/x")` names itself `x`. S, `src/tundravm/_source.py`.
12. Metric selectors (`container.metrics.active`, `.cpu_seconds`, `volume.metrics.available`), the rules group named after the image, and `show` naming the identity series that leave. M, `src/tundravm/components/_observability.py` (after `metrics-rules`).
13. `check`: one line for `tundravm:tools` sources, hints grouped right, `lock-missing` with one severity, a `hardening-relaxed` text that names the preset and why. S, `src/tundravm/check.py`.
14. The container's loop loader is dropped under a kernel with `loop=y`; `ip_forward` goes in `sysctl.d`; the namespace unit gets a sandbox. S, `src/tundravm/components/_container.py`.
15. `convert` of a tundravm-written tree: one header line, a true subimage reason, per-setting reasons. S, `src/tundravm/convert.py`.
16. `show`: the cmdline joined by spaces, build steps in prose, host names over set names, units over class names, the dev delta restored. S, `src/tundravm/_describe.py`.
17. `show --diff`: a profile present on one side prints one line, and the program is cut after the first difference. S, `src/tundravm/diff.py`.
18. `deploy --help` stops saying Azure volumes are refused. S, `src/tundravm/cli.py`.
19. `doctor` names the image's Go sources and drops the replay version string. S, `src/tundravm/capabilities.py`.
20. The wants links point at the unit files, and `mode-switch` gets `WantedBy=minimal.target`. S, `src/tundravm/compiler/emit_mkosi.py`.
21. `Service(description=)`, defaulting to the docstring of the component. S, `src/tundravm/_image/_service.py`.
22. `Network(egress=Egress())` turns into a default filter or a named flag. S, `src/tundravm/components/_network.py`.
23. `Container("bob")`, with the relay unit named after the endpoint. S, `examples/flashbox/image.py`, `src/tundravm/components/_container.py`.
24. `Nethermind(expose_p2p=True)`, and 30303 drops out of maintenance. S, `examples/attested-node/components/nethermind.py`, `examples/flashbox/image.py`.
25. Rules: `flashbox:oom_kills_count`, `device!~"lo|ve-.*"`, a comment on `/`. S, `examples/flashbox/image.py`.
26. Docstring: what leaves is stated correctly, the SDK paragraph goes, a kernel comment is added, and `socat` is dropped or explained. S, `examples/flashbox/image.py`.
27. README: the refusal bullets corrected, the known_hosts and instance name shown, `uv run` working (a `[project]` table or a note). S, `examples/flashbox/README.md`, `examples/flashbox/pyproject.toml`.
28. The example commits its lock (`!examples/flashbox/build/tundravm.lock`), so `check` is quiet. S, `.gitignore`.
