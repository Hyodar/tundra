# Container network: egress, host endpoints, UDP

Date: 2026-11-03. Status: design, then landed in the commits that follow it. Gaps 1, 3 and
4 of flashbox-l1 (`flashbots-images-gaps-2026-11-02.md`): the searcher's bot reaches order
flow on an allow-list, the host's Nethermind by declared endpoint, and takes UDP on its
input channels. It replaces the "Egress" section of `container-2026-11-03.md`.

## The spelling

```python
Container("bot", ["/usr/bin/bot"], packages=["bot"],
          expose=[TCP(10022), UDP(9999)],
          egress=Egress(hosts=["203.0.113.7:443"]),
          requires=[nethermind.rpc, nethermind.engine_api])
```

`requires=` takes `Endpoint`s, as `Service(requires=)` does: each must carry readiness and
be on the loopback. `expose=` takes `TCP` and `UDP`. `egress=` is what the container alone
may send in every mode, `modes={"maintenance": Egress(...)}` what it sends in that mode
only, the rule `Firewall(modes={"maintenance": Mode(egress=...)})` already follows for the
host: what every mode keeps, plus the mode's own, so production narrows by holding only its
own. `serves=[TCP(8551)]` is the reverse direction: host processes reach that port at the
container's veth address, `bot.endpoint(8551)` hands it to a host service
(`Lighthouse(execution=bot.endpoint(8551))`), and `probe=TcpProbe(8551)` is what that
service's `requires=` waits for. With none of these the container keeps a namespace of `lo`
only, as before.

## What it lowers to

- `<name>-net.service`, a root oneshot (`DefaultDependencies=no`, after `nftables.service`)
  that runs `ip netns add <name>`, brings its `lo` up and, with `expose=` or `egress=`, adds
  the veth `ve-<name>` (`host0` inside) on a /30 of 169.254.0.0/16 with the default route
  through the host's side, then sets `net.ipv4.ip_forward=1`. The /30 comes from the
  name's sha256, never in 169.254.0.0/24, .169.0/24 or .255.0/24; two containers on one /30
  are refused. A name over 12 characters names its veth `ve-<first 7>-<4 hex>`, as nspawn
  does. `/etc/systemd/network/10-tundravm-ve-<name>.network` keeps networkd off it.
- The unit joins it: `NetworkNamespacePath=/run/netns/<name>` instead of
  `PrivateNetwork=yes`. The image gains `iproute2`, the modules `veth`, `nft_nat`,
  `nft_chain_nat` and `nft_masq`, and `Container.kernel()` asks a built kernel for them.
- `nftables.conf`, for each container with a veth: in `input`, `iifname "ve-<name>"`
  is refused, so the host's own ports are not its destinations; in `forward`, what it sends
  is accepted for exactly `egress_rules()`' destinations, the metadata servers refused
  unless `Egress(metadata=True)`, the rest refused; then each exposed port, DNATed in a
  `prerouting` nat chain to the container's address, is accepted (`ct status dnat`) going
  out of its veth. A port a mode names jumps to `mode_input` there, so `Firewall(modes=)`
  opens and closes it as it does a host port (modes key ports by number and protocol, and
  the DNAT keeps the number). A `modes=` destination is
  `meta mark set <mode's mark> meta mark @current_mode ct mark set meta mark accept`: the
  set `mode-switch` already swaps decides, and only an accepted flow takes the mark, so it
  drops after a switch as a moded port's does, while a refused one keeps mark 0 and its
  reset leaves; `mode-switch` changes nothing. With egress, `postrouting` masquerades its address.
  A host name in its egress matches the host sets `egress-resolve` fills, as the host's do;
  the container resolves nothing itself, so its `files=` carries an `/etc/hosts`.
- Per served port: the output chain accepts `oifname "ve-<name>"` to its address and port,
  and the container's unit takes `readiness=` the probe at that address.
- Per host endpoint: `<name>-endpoint-<port>.socket`, listening on the endpoint's own
  loopback address inside the container's namespace (`NetworkNamespacePath=` on the
  socket), activating `systemd-socket-proxyd` in the host's namespace, which connects to the
  endpoint and requires its readiness. The container's unit requires the endpoint too.

The endpoint relay listens inside the namespace rather than on the veth's host address:
the host service keeps listening where it does, no host port opens, no input rule or
`route_localnet` is needed, and the container calls the same URL the host does. A socket
in its namespace is reachable from that container only.

Refusals (`fix:` lines): an exposed port another service or container exposes; a
non-loopback or unready endpoint; a networked container in a profile without a
`Firewall`; a `modes=` name the `Firewall` does not declare, or one reaching the metadata
server; `endpoint(port)` or a `probe=` on a port `serves=` does not name.

Rejecting, not dropping, what the container sends: the TCP reset and the ICMP
admin-prohibited reach only the container, and its program fails at once.

## The probe

`container:<name>` keeps its digest; without a veth it still proves no route. With one:
`container:<name>:egress` (an allowed TCP host:port connects, a denied one and
169.254.169.254:80 are refused), `container:<name>:endpoint:<port>` (the endpoint answers
from inside), `container:<name>:serves:<port>` (the host connects to its veth address),
`container:<name>:udp:<port>` (a datagram sent from a probe namespace to the
host's port lands in the container's `Udp InDatagrams`). CI's maintenance window opens a
moded container port and the return to production closes it.
