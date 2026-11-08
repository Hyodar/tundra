# flashbox

flashbox-l1 is Flashbots' searcher sandbox (BoB) for TDX: a searcher's bot runs in a
container beside an L1 node, on a volume the operator's passphrase opens once the instance
has attested, with the searcher's key signing the unlock. The firewall boots into
production, where the container reaches the bundle endpoint, the transaction stream and
BuilderNet. A maintenance window opens the container's SSH and lets it reach DNS and any
host on the web ports, and the bundle endpoint stays reachable in both modes.

`image.py` ports it from flashbots-images at `09c7576` and composes the parts in
`components/`. `searcher.py` is the container with flashbox's two channels, `orderflow.py`
the pinned peers and what each mode may reach, and `health.py` the recording rules. The
attested node's `nethermind.py` and `lighthouse.py` are linked there file by file.
`channels/` holds the Go programs flashbox ships as `delay-pipe` and `input-only-proxy`.

## Where each piece went

| flashbots-images | tundravm |
|---|---|
| podman, `init-container.sh` | `Searcher(...)`, which composes `Container("bob", ssh=, expose=, egress=, modes=, binds=)` |
| `init-firewall.sh`, `toggle`, `firewall-config` | `Firewall(modes=)`, `Mode(ingress=[searcher.ssh])`, `firewall.switch` as the menu's `toggle` |
| `searchersh.c`, `99-searcher`, dropbear | `Login("searcher", owner=True, commands={...})`: `Command(...)` as the login, `Sudo(...)` as root, `dev=True` in development builds only |
| tdx-init, `persistent-mount.service` | `Volume("data", unlock=OperatorKey())` |
| `lighthouse.service` | `Lighthouse(disable_quic=, ...)`, `Generated("jwt")` |
| the container's execution client | `Nethermind(expose_p2p=True)` on the host, `Searcher(rpc=nethermind.rpc)` |
| `recording_rules.yml`, SigV4, the 443 proxy | `health_rules(searcher, data)` over `.metrics` selectors, `SigV4`, `Proxy(passthrough=True)` |
| static hosts in `after-init` | `Host(...)` pins in `orderflow.py` |
| `delay-pipe`, `input-only-proxy` | `channels/`, two `Service`s in the container's group |
| `config.d/` kernel and BBR | `Kernel.build(..., base=DebianConfig(...))` and a fragment |

The attested endpoint, the image digest metric, CPU mitigations, DNS over TLS, the closed
metadata server, the cloud glue and the kernel hardening come with every image. The
execution client runs on the host rather than in the container, because the engine API's
jwt cannot be bound into a container, so only a host client can share it with Lighthouse;
the searcher reads its RPC at `127.0.0.1:8545`.

## What tundravm refuses

- podman's registry tag: the container's root is Debian packages from the snapshot.
- `stopped`, and a cooldown on the host's clock: the image boots into production, and a
  window closes after 30 minutes of boot time.
- `initialize`: the passphrase goes by `secrets unlock` over the attested endpoint.
- Root logins, and port 22 in a mode: the menu answers on 22 in every mode, and only the
  container's 10022 opens and closes.
- `CONFIG_IPV6=n`: the firewall is one `inet` table, which needs IPv6.
- A sandbox of the searcher's choosing: the container's sshd runs under tundravm's, so
  `hardening=` is refused.

## Build, deploy, switch

```console
$ cd examples/flashbox
$ tundravm lock
$ tundravm check --tree mkosi
$ tundravm dev
$ tundravm build --target gcp
$ tundravm deploy gcp --build build/flashbox/production/ID --volume data=create:2048GiB
$ tundravm attest flashbox-1 --policy POLICY --identity ~/.ssh/searcher
$ tundravm secrets unlock flashbox-1 data
$ tundravm secrets deliver flashbox-1 --policy POLICY metrics-aws=@aws-credentials
$ ssh -o UserKnownHostsFile=.tundravm/instances/flashbox-1/known_hosts searcher@HOST toggle maintenance
$ ssh -o UserKnownHostsFile=.tundravm/instances/flashbox-1/known_hosts -p 10022 bob@HOST
```

From a checkout of this repository, run each command as `uv run --project ../.. tundravm`.
A bare `uv run` takes this directory's `pyproject.toml`, which only names the image file,
for a project of its own, and finds no `tundravm` in it. `deploy` names the instance after
the image, `flashbox-1` for the first, and `POLICY` is the build's `measurements.json`.

The searcher attests first, so its key owns the volume and logs in both as `searcher`,
whose menu offers `status`, `logs N` (the delayed copy), `toggle` and `reboot`, and as
`bob` inside the container. `attest` pins both host keys in the instance's `known_hosts`,
so neither login asks to trust a key on first use. CI boots the dev profile and drives it
this way (`.github/workflows/flashbox.yml`).

## What tundravm still lacks

- A writable container layer, so the searcher cannot install tools. It needs a uid-range
  user namespace, which is Franco's decision.
- The jwt in a container, so the searcher cannot run its own execution client.
- Lighthouse's logs in the container, which flashbox mounts read-only: the example
  Lighthouse has no log-directory keyword.
- Egress that excludes: maintenance allows 443 everywhere, where flashbox drops the tx
  stream.
- TLS in `input-proxy`: flashbox's pins the searcher's key, and this one relays plain bytes.
