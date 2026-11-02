# What flashbots-images shows Tundra still lacks

Date: 2026-11-02. Studied `flashbots/flashbots-images` at `09c7576` (paths `fb:`) against
master `21e05ed`. Most safe items of `flashbots-lessons-2026-10-18.md` and
`research-2026-10-30/flashbots-report.md` have landed; this note is what is left.

## Verdict

`tdx-dummy` is expressible today in about 12 lines, and our simulator issuer already does
its job. `verity-root` is `rootfs=RootFS.verity()`; the cloud and devtools profiles are
`targets=` and `dev=DevAccess(...)`.

The L2 images are expressible with three substitutions: `secrets deliver` for Vault, flags
fixed per profile for flags rendered from Vault, and `Observability` for the ops agent. One
file with a profile per variant is about 100 lines, most of them op-rbuilder's 40 flags. An
unattended fleet still needs gap 3.

`flashbox-l1` is not expressible: its guarantee is a tenant container (1), network modes (2)
and a restricted login (5). Its programs are `CargoBinary` services, the example's
`Lighthouse`, the attested endpoint and `OperatorKey()`. With the three gaps closed it is
about 90 lines.

## 1. The tenant container

They: `fb:modules/flashbox/common/mkosi.extra/usr/bin/init-container.sh` runs rootless podman
(passt, uidmap, fuse-overlayfs, `vfs`, a custom `seccomp.json`) on `ubuntu:24.04` by tag and
`apt-get install`s sshd at every start. It `nsenter`s the container's netns to add DROPs,
because pasta makes container traffic leave as a host process. The same network facts live
in three files.

We: nothing; a `Service` is one program. mkosi 27.1 builds `Format=directory|portable|oci`
subimages and pulls from no registry; systemd 257's nspawn has `PrivateUsers=pick` and
`VirtualEthernet=`. `importctl` pulling OCI images is newer than 257 (unsure: systemd NEWS for
v258) and would fetch at boot anyway.

```python
Container("searcher", packages=["openssh-server"], argv=["/usr/sbin/sshd", "-D"], expose=[TCP(10022)], binds=[data.directory("searcher")], egress=Egress(hosts=["3.136.107.142:443"]))
```

It lowers to a `Format=directory` subimage from the same snapshot, read-only at
`/var/lib/machines/searcher`, and `systemd-nspawn@searcher.service`. The veth crosses the
host's forward chain, so `egress=` is the container's whole allow-list: one declaration, not
three. The guest makes the container's host key, read-only to it, and the quote binds it.

Refused: a tag, a registry pull, podman: a Debian root built with the image is in the
snapshot and the SBOM, a foreign userland is not. Size L. Proof: a `fixture-boot` row where
the container answers SSH and cannot reach 169.254.169.254, and a golden of the subimage.

## 2. Network modes

They: `init-firewall.sh` builds default-DROP chains `ALWAYS_*`, `MAINTENANCE_*` and
`PRODUCTION_*` behind a `MODE_SELECTOR` jump and boots in maintenance. Maintenance opens
container SSH, p2p, DNS and the web; production allows only the order-flow IPs on 443
(`fb:modules/flashbox/flashbox-l1/mkosi.extra/etc/bob/firewall-config`). `toggle` cycles
production, stopped, maintenance, waits 5 minutes after production, checks the container's
DROPs, and kills old flows with `conntrack -D` (`toggle-config`).

Only `searcher` toggles, through `searchersh` and sudo. The mode is in no quote: attested
code enforces the cycle. The cooldown reads `date +%s`, which the host sets, and a host reset
boots into maintenance with no cooldown. `cloud-provision.conf` only orders the firewall
after Azure's ready report; `/etc/devmode` has no reader (clone, `flashbots/tdx-init`).

We: `Firewall` renders one static ruleset per profile (`src/tundravm/components/_base.py`),
and `tundravm run mode` already names the security mode.

```python
Firewall(modes={"maintenance": Mode(expose=[TCP(10022)], egress=Egress(ports=[TCP(443)])), "production": Mode(egress=Egress(hosts=["3.136.107.142:443"]))}, cooldown="5m")
```

Every mode's chain is written at build time; the switch, a `tools/` program, only selects
one. Each flow carries its mode's `ct mark` and the established rule accepts only the current
one, so a switch drops old flows without a conntrack flush. The image boots `stopped` and
counts the cooldown on `CLOCK_BOOTTIME`, which neither a reset nor the host clock skips. The
operator runs `tundravm run switch INSTANCE production` (`run mode` is taken) over the
endpoint.

Each switch extends RTMR3 with the mode, so a verifier replays this boot's history; `build`
prints RTMR3 unchecked for want of such an event schema. tdxs cannot extend an RTMR today.

Refused: modes as scripts, and a switch not signed by the operator key the endpoint took.
Size L. Proof: a `fixture-boot` row that sees an old flow dropped, and a verifier test
replaying RTMR3.

## 3. Secrets from a broker

They: `vault-agent.service` renders its config with gomplate from GCE metadata and logs in
with Vault's `gcp` auth, `type = "gce"`, on the instance identity JWT
(`fb:modules/l2/_common/mkosi.extra/etc/vault-agent/gomplate/`). Templates write the JWT
secret, signing keys, TLS pair and genesis, and render `op-rbuilder.service` with its flags.

No quote is checked: any VM with that identity gets the secrets, and the host, writing the
metadata, picks the Vault. `tdx-quote-provider` is unrelated: a root HTTP quote service on
the loopback for op-rbuilder's on-chain attestation. The data disk is unencrypted, mode 0777.

We: `BrokerKey(url, audience=)` and `Secret(source=)` exist without a client; the image waits
as for an operator (`src/tundravm/_image/_secrets.py`). `quotes=True` and `InstanceKey()`
already do tdx-quote-provider's job behind the `tdx` group.

```python
Secret("builder-key", source=BrokerKey("https://kbs.example", audience="op-rbuilder"))
```

The client, a `tools/` program in `broker-pull.service` after the network and
`tdxs.socket`, makes an ephemeral key and takes a nonce from the broker. tdxs quotes the
endpoint's `Binding` over nonce, ephemeral key, instance key and audience, and the client
posts it to the URL in the image over TLS. The broker answers with values encrypted to the
ephemeral key, written where the endpoint writes, so the existing `credential-<name>.service`
waits release the services. A cloud identity token never suffices alone (section 8).

Refused: units or argv rendered from secrets, a broker address from metadata, Vault in the
library. Shipping a reference broker is open for Franco. Size M. Proof: a `test` run against
a fake broker, then a `fixture-boot` row with one on the host.

## 4. Private sources

They: `scripts/make_git_package.sh` and `build_rust_package.sh` splice `$BUILDDIR/.ghtoken`
into the clone URL inside the sandbox, under `set +x`, and clone by tag.

We: there is no refusal to undo. `fetch_source` and `_ls_remote` run the host's git as the
user with `GIT_TERMINAL_PROMPT=0` (`src/tundravm/_source.py`, near lines 1486 and 2339), so a
credential helper or SSH agent already reaches a private repository. Missing: a token in the
URL is accepted and lands in the lock, manifest and SBOM, and the CI template cannot pass
one. Whether `cargo fetch` uses the helper is unsure (`CARGO_NET_GIT_FETCH_WITH_CLI`).

```python
Git("https://github.com/org/private", "v1.2.0", auth="GH_TOKEN")
```

`auth=` names an environment variable read on the host at lock, fetch and prefetch, given to
git as `http.<url>.extraHeader` through `GIT_CONFIG_*`, never in argv. Refused: a credential
in a URL (a new check code), a token anywhere in the image, lock or build directory. Size S.
Proof: unit tests with a fake git that gets the header and no argv token.

## 5. A restricted login

They: `searcher` logs in to dropbear (`-s -w -g -j -k`: keys only, no root, no forwarding)
with `searchersh.c`, a forced-command menu (`toggle`, `status`, `logs N`, `reboot`,
`initialize`). Privileged entries go through `etc/sudoers.d/99-searcher`; root is locked and
`su` root-only; `ssh-pubkey-server` serves the host keys on the attested port. The L2
`_devtools_*` modules add a sudo user and lock root, in devtools only.

We: `SshAccess` admits root by key only (`src/tundravm/declarative/utils.py`) and
`DevTools(network_login=)` serves dev profiles; the endpoint binds the host keys. Nothing
gives a non-root login, forced commands or sudo rules.

```python
SshAccess(from_delivery=True, login=Login("searcher", commands={"toggle": ["tundravm-switch", "next"], "logs": ["journalctl", "-u", "app", "-n", "{lines:int}"]}))
```

The shell is a `tools/` menu reading the generated list; sshd gets `ForceCommand` and no
forwarding, each privileged entry one exact-argv sudoers line, and root login closes.
Refused: a free shell, hand-written sudo rules, passwords, each a way past the menu. Size M. Proof: a `fixture-boot`
row where an allowed command runs and `ssh searcher@vm sh` is refused.

## 6. A release workflow

They: `fb:.github/workflows/_build-and-publish-image.yaml`, on a release tag, builds,
measures and writes `SHA256SUMS` over the artifacts, SBOM and measurements. `actions/attest`
signs the sums (Sigstore provenance from GitHub's OIDC) and the SBOM, `gh attestation verify`
checks both, and a draft release says how to reproduce. `build.yaml` can rebuild on a second
runner.

We: `tundravm new --ci` writes `.github/workflows/tundravm.yml` (`src/tundravm/templates.py`)
with `check` and an opt-in `build --reproduce` plus `verify latest`. Builds carry the
manifest, measurements, an SPDX SBOM and optional SSH signatures; nothing attests or
publishes.

```console
tundravm new demo --ci --release
```

It adds a tag job: `build --reproduce`, `verify latest`, `SHA256SUMS` over the artifacts and
manifest, `actions/attest` on the sums and the SBOM, `gh attestation verify`, a draft
release; another runner runs `verify --rebuild`. The provenance binds workflow and commit to
the digests, the manifest id the lock, tree and toolchain. Our SBOM lacks the Go modules and
`cargo auditable` data inside binaries (S). Size M. Proof: the template's golden and one tag
on a scratch repository.

## 7. Observability

They: flashbox's node and process exporters listen on the loopback, recording rules reduce
them to `flashbox:*` booleans, and only those leave, through the fixed-IP proxy with SigV4
credentials from Vault (`fb:modules/flashbox/observability/`). `flashbox-image-measurement.rs`
exports the UKI's Authenticode digest from the CCEL, and `print-measurements` logs RTMR0-3.
L2 ships the GCP ops agent and unauthenticated exporters on 0.0.0.0.

We: `PrometheusRemoteWrite` scrapes only the node exporter and sends every series; `JournalExport`
uploads the journal (`src/tundravm/components/_observability.py`).

```python
PrometheusRemoteWrite(url, credential=token, scrape=[node.metrics], export=["up", "node_memory_MemAvailable_bytes"])
```

`scrape=` takes exported endpoints; `export=` is the keep-list, empty sending nothing, since
raw series from a TEE are a side channel. The Authenticode digest becomes a metric,
RTMR0-3 a boot log line. Refused: SigV4 and cloud agents (a proxy can sign), public exporter
ports, configuration from metadata. Size M. Proof: the probe's `observability:` rows see only
listed series reach the in-guest sink.

## 8. Smaller findings

Each is proven by the golden of the tree it changes, plus a probe row where it boots.

Kernel: they compile `gregkh/linux` at a pinned SHA from Debian's sha256-pinned cloud config
plus fragments, without module loading. We boot Debian's modular kernel, `Kernel.build`
takes one whole config, and `KernelHardening` is not public. `ATTACK_SURFACE` lacks their
`KSM`, `PROFILING`, `KALLSYMS_ALL`, `MPTCP`, `NETLABEL`, `RD_*` decompressors and debug info;
flashbox adds `IPV6=n`. Stricter there. Size M for `Kernel.build(base=DebianConfig(...),
fragments=, hardening=True)`.

Command line: they set `mitigations=auto,nosmt spec_store_bypass_disable=on`; we add
nothing. Their `nospectre_v2` disables a mitigation and `transparent_hugepage=madvise` is
tuning, yet `Kernel`'s docstring teaches `cmdline=["nospectre_v2"]`. Size S.

DNS: they resolve over TLS with systemd-resolved; `TeeBase` writes a plain `resolv.conf`, so
the host reads and can forge every lookup. Size S.

Metadata: every process of our cloud images reaches 169.254.169.254, the identity token's
source (`examples/attested-node/mkosi/payload/mkosi.extra/etc/nftables.conf`); flashbox blocks
it for the tenant. `Service(egress=)` as `IPAddressDeny=any` plus `IPAddressAllow=` scopes it.
Size S.

Operator key: `tdx-init` keeps the searcher's key in the LUKS header, so after a reboot only
that key gets in. Our endpoint takes the first key delivered each boot
(`tools/pkg/endpoint/server.go`), so whoever reaches it first after a reboot gets root.
Pinning it in a LUKS2 token of the `OperatorKey()` volume closes that. Size M.

Tests: `fb:tests/integration/test_flashbox_l1.py` boots on GCP TDX with negative tests
(host-key directory read-only, NTS closed to the tenant) that should become our probes; its
deploy workflow is not in the clone.

Even or better: journald, chrony, verity, the tmpfs cap, sysctls, debloat (we lack
`/var/lib/ucf`, `/.cache`), the mkosi and tools-tree pins, commit-pinned fetches, sandboxed
units, quotes behind a group.
