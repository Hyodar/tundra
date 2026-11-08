# flashbox

flashbox-l1 is Flashbots' searcher sandbox (BoB) for TDX. A searcher's bot runs in a
container beside an L1 node. Production mode reaches only the order flow; maintenance
opens the container's SSH. Only the searcher's key opens the disk.

`image.py` ports it from flashbots-images at `09c7576`. `components/` links to the
attested node's components. `channels/` holds the two small Go programs flashbox ships as
`delay-pipe` and `input-only-proxy`.

## Where each piece went

| flashbots-images | tundravm |
|---|---|
| podman, `init-container.sh` | `Container(ssh=, expose=, egress=, modes=, binds=, files=)` |
| `init-firewall.sh`, `toggle`, `firewall-config` | `Firewall(modes=)`, `firewall.switch` as the menu's `toggle` |
| `searchersh.c`, `99-searcher`, dropbear | `SshAccess(login=Login("searcher", owner=True, commands={...}))`: `Sudo("/usr/bin/tail", "--lines", Int("lines", 1, 10000), ...)` as root, `Command("/usr/bin/bash", dev=True)` in development builds only |
| tdx-init, `persistent-mount.service` | `Volume("data", unlock=OperatorKey())` |
| `lighthouse.service` | `Lighthouse(disable_quic=, ...)`, `Generated("jwt")` |
| the container's execution client | `Nethermind` on the host, `Container(requires=[nethermind.rpc])` |
| `recording_rules.yml`, SigV4, the 443 proxy | `rules=`, `export=`, `SigV4`, `Proxy(passthrough=True)` |
| static hosts in `after-init` | `Host(...)` pins in the container's `egress=` and `modes=` |
| `delay-pipe`, `input-only-proxy` | `channels/`, two `Service`s on the container's account |
| `config.d/` kernel and BBR | `Kernel.build(..., base=DebianConfig(...))` and a fragment |

Implied: the attested endpoint, the image digest metric, CPU mitigations, DNS over TLS,
the closed metadata server, cloud glue and kernel hardening.

The execution client runs on the host. The engine API's jwt cannot be bound into a
container, so only a host client shares it with Lighthouse. The searcher reads its RPC at
`127.0.0.1:8545`.

## What tundravm refuses

- podman's registry tag: the container's root is Debian packages from the snapshot.
- `stopped` and the cooldown on the host's clock: the image boots into production, and a
  window closes after 30 minutes of boot time.
- `initialize`: the passphrase goes by `secrets unlock` over the attested endpoint.
- SSH on 22 in production, and root logins: the menu stays open in every mode instead.
- `CONFIG_IPV6=n`: the firewall is one `inet` table, which needs IPv6.
- A sandbox for the searcher's programs: sshd's is tundravm's, so `hardening=` is refused.

## Build, deploy, switch

```console
$ cd examples/flashbox
$ tundravm check --tree mkosi
$ tundravm dev
$ tundravm lock
$ tundravm build --target gcp
$ tundravm deploy gcp --build build/flashbox/production/ID --volume data=create:2048GiB
$ tundravm attest flashbox-1 --policy POLICY --identity ~/.ssh/searcher
$ tundravm secrets unlock flashbox-1 data
$ tundravm secrets deliver flashbox-1 --policy POLICY searcher-keys=@searcher.pub metrics-aws=@aws-credentials
$ ssh searcher@HOST toggle maintenance
$ ssh -p 10022 bob@HOST
```

`POLICY` is the build's `measurements.json`. The searcher attests first, so its key owns
the volume. `ssh searcher@HOST` lists the menu: `status`, `logs N` (the delayed copy),
`toggle`, `reboot`.
CI boots the dev profile (`.github/workflows/flashbox.yml`).

## What tundravm still lacks

- A writable container layer: the searcher cannot install tools. It needs a uid-range
  user namespace, which is Franco's decision.
- The jwt in a container, so the searcher cannot run its own execution client.
- Lighthouse's logs in the container, which flashbox mounts read-only: the example
  Lighthouse has no log-directory keyword.
- Egress that excludes: maintenance allows 443 everywhere, flashbox drops the tx stream.
- TLS in `input-proxy`: flashbox's pins the searcher's key; this one relays plain bytes.
