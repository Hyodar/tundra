# The guide

This tutorial builds an Ethereum node on the Hoodi testnet that signs its answers inside
a TDX trust domain. It takes six stages, each a complete image file under
[`examples/attested-node/stages/`](../examples/attested-node/stages/) that adds one idea.
Words in bold are defined in [Terms](terms.md). Finish the README's
[first run](../README.md#first-run) before you start.

## Before you start

The stages live in tundravm's repository:

```console
$ git clone https://github.com/Hyodar/tundra
$ cd tundra/examples/attested-node
$ tundravm check stages/01-boot.py
```

Run every command from there. `check` reads an image without building it. It
prints one warning, `source-unpinned`, until you lock. `tundravm show --diff OLD NEW`
prints what a stage adds without building anything. The outputs below are excerpts of it
unless they say otherwise.

## 1. The boot

The idea: one `Image` value is a whole OS. `tundravm new` writes this file:

<!-- listing: examples/attested-node/stages/01-boot.py -->
```python
"""attested-node: the boot, stage 1 of the guide.

Debian trixie from one snapshot, with systemd, the TEE baseline (NTS time, a volatile
journal, sysctl hardening), DHCP, a firewall that drops inbound traffic nothing exposes,
filtered egress, and a dev profile with a serial console and root password "tdx".
"""

from tundravm import Debian, Image
from tundravm.components import DevAccess, Egress, Network

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    network=Network(egress=Egress()),
    dev=DevAccess(password="tdx"),
)
```

The docstring lists what the call implies. Boot its `dev` profile:

```console
$ tundravm dev stages/01-boot.py
```

`dev` builds the profile and boots it under QEMU without TDX. It prints the boot probe,
then follows the console until Ctrl-C. CI's `boot` job boots this stage on every push. Its
console shows:

```text
tundravm-probe: mode ok simulated
tundravm-probe: rtmr skip no TDX: a simulated guest has no registers to compare with measurements.json
tundravm-probe: socket:tdxs.socket ok active
tundravm-probe: egress:192.0.2.1:9 ok refused by the output chain (Operation not permitted)
tundravm-probe: kernel:modules ok 12 loaded, each curated or held by one
tundravm-probe: key:instance ok root:root 600, pub 034c2400bf28bbc43c8bd283bc0c56d1c426399d0d763b162c2e01e8392146214e
tundravm-probe: done 5 ok 0 fail
```

`mode ok simulated` says the guest found no TDX. A development build runs its services
anyway, and a production build would start none. The `egress:` line shows the firewall
refusing traffic nothing allows.

## 2. Nethermind, a component of your own

The idea: your application is a component your project writes. This stage adds the most,
so it comes in four parts.

<!-- diff: examples/attested-node/stages/01-boot.py examples/attested-node/stages/02-nethermind.py -->
```diff
--- stages/01-boot.py
+++ stages/02-nethermind.py
@@
 
-from tundravm import Debian, Image
+from components.nethermind import Nethermind
+
+from tundravm import Debian, Image, RootFS
 from tundravm.components import DevAccess, Egress, Network
+from tundravm.secrets import Generated
+from tundravm.storage import OperatorKey, Volume
+
+data = Volume("data", unlock=OperatorKey())
+jwt = Generated("jwt")
+nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
 
@@
     packages=["ca-certificates"],
+    components=[nethermind],
     network=Network(egress=Egress()),
+    rootfs=RootFS.verity(),
     dev=DevAccess(password="tdx"),
```

### The component

The image file imports `Nethermind` from `components/nethermind.py`. The class is the
whole Component API:

<!-- listing: examples/attested-node/components/nethermind.py Nethermind -->
```python
class Nethermind(Component):
    """Nethermind on *config*, its JSON-RPC and Engine API on the loopback."""

    jwt: Secret | Generated
    state: VolumeDirectory | None = None
    config: str = "hoodi"
    rpc_port: int = 8545
    engine_port: int = 8551

    @property
    def rpc(self) -> Endpoint:
        return self.service("nethermind").endpoint(self.rpc_port)

    @property
    def engine_api(self) -> Endpoint:
        return self.service("nethermind").endpoint(self.engine_port)

    def outbound(self) -> Egress:
        return Egress(ports=[TCP(30303), UDP(30303)])

    def compose(self) -> Iterator[object]:
        yield Build(
            "nethermind",
            SOURCE,
            using=Dotnet(
                project="src/Nethermind/Nethermind.Runner",
                output="nethermind",
                properties=PROPERTIES,
            ),
            install=INSTALL,
            cache_namespace=f"nethermind-{SOURCE.ref}",
        )
        for package in RUNTIME:
            yield Package(package)
        datadir = "/var/lib/nethermind" if self.state is None else self.state.path
        yield Service(
            "nethermind",
            [
                f"{HOME}/nethermind",
                f"--config={self.config}",
                f"--datadir={datadir}",
                "--JsonRpc.Enabled=true",
                "--JsonRpc.Host=127.0.0.1",
                f"--JsonRpc.Port={self.rpc_port}",
                "--JsonRpc.EngineHost=127.0.0.1",
                f"--JsonRpc.EnginePort={self.engine_port}",
                f"--JsonRpc.JwtSecretFile={self.jwt.path}",
            ],
            readiness=TcpProbe(self.rpc_port, timeout="10min"),
            state="nethermind" if self.state is None else self.state,
            credentials=[self.jwt],
            hardening="dotnet",
        )

    rules = (
        Rule(
            "nethermind-volatile-state",
            "warning",
            "Nethermind keeps its database on the volatile root, so it resyncs at every boot.",
            "Pass state=volume.directory('nethermind') on a Volume.",
        ),
    )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        if self.state is not None:
            return ()
        message = "Nethermind keeps its database on the volatile root: it resyncs every boot"
        return (Diagnostic("nethermind-volatile-state", "warning", message),)
```

- Fields are configuration: the class is a frozen, keyword-only dataclass.
- `compose()` yields what it stands for: a `Build` and the `Service` running it.
- `using=Dotnet(...)`: its `global.json`'s SDK builds it offline.
- `rpc` and `engine_api` are exports: endpoints carrying the service's readiness.
- `outbound()` is what a filtered image lets it send: its peers on 30303.
- `check()` is a lint rule per profile; `rules` declares each code it reports.

### Its storage

Nethermind's database must survive a reboot. `data` is a **volume**, an encrypted disk.
`unlock=OperatorKey()` means an operator sends its passphrase after checking the quote.
Nethermind waits until then. The first key to sign a write becomes the volume's **owner**.
The instance refuses writes signed by any other key. `Generated("jwt")` is a **secret** the
guest makes at each boot.

### What the volume brings

A volume needs an operator, so the image runs the **control plane**:

```console
$ tundravm show --diff stages/01-boot.py stages/02-nethermind.py
```

```text
  Firewall
    - in  nothing beyond replies and DHCP
    + in  22/tcp  ssh.service
    + in  8745/tcp  attested-endpoint
    + out  30303/tcp, 30303/udp  nethermind
```

8745 is the attested endpoint and 22 is SSH. `outbound()` opened 30303.

### The root filesystem

The build is large. `RootFS.verity()` makes the root a read-only disk whose hash the
measured kernel command line carries.

## 3. Lighthouse, wired by an export

The idea: components connect through their exports. Lighthouse, the consensus client,
drives Nethermind to the chain's head.

<!-- diff: examples/attested-node/stages/02-nethermind.py examples/attested-node/stages/03-lighthouse.py -->
```diff
--- stages/02-nethermind.py
+++ stages/03-lighthouse.py
@@
 
+from components.lighthouse import Lighthouse
 from components.nethermind import Nethermind
@@
 nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
+lighthouse = Lighthouse(
+    execution=nethermind.engine_api,
+    jwt=jwt,
+    checkpoint_sync_url="https://checkpoint-sync.hoodi.ethpandaops.io",
+    state=data.directory("lighthouse"),
+)
 
@@
     packages=["ca-certificates"],
-    components=[nethermind],
+    components=[nethermind, lighthouse],
     network=Network(egress=Egress()),
```

`execution=nethermind.engine_api` is an export, so Lighthouse starts once Nethermind
answers. It shares the same generated secret. `components/lighthouse.py` has the same
shape as Nethermind's, with a pinned download in place of a build. Its `outbound()` opens
its peers and the checkpoint host:

```console
$ tundravm show --diff stages/02-nethermind.py stages/03-lighthouse.py
```

```text
  Firewall
    + out  443/tcp  lighthouse (checkpoint-sync.hoodi.ethpandaops.io)
    + out  9000/tcp, 9000/udp, 9001/udp  lighthouse
```

## 4. The signer, your own program

The idea: a `Service` runs your program, built from source. `signer/` is a Go proxy. It
signs Nethermind's block responses with the **instance key**.

<!-- diff: examples/attested-node/stages/03-lighthouse.py examples/attested-node/stages/04-signer.py -->
```diff
--- stages/03-lighthouse.py
+++ stages/04-signer.py
@@
 
-from tundravm import Debian, Image, RootFS
+from tundravm import Debian, Image, RootFS, Service
 from tundravm.components import DevAccess, Egress, Network
-from tundravm.secrets import Generated
+from tundravm.secrets import Generated, InstanceKey
+from tundravm.sources import GoBinary, Local
 from tundravm.storage import OperatorKey, Volume
+from tundravm.systemd import Resources, TcpProbe
 
@@
 )
+program = GoBinary("signer", Local("../signer"))
+signer = Service(
+    "signer",
+    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc],
+    credentials=[InstanceKey()],
+    requires=[nethermind.rpc],
+    readiness=TcpProbe(8547),
+    resources=Resources(memory_max="256M"),
+    hardening="static",
+)
 
@@
     components=[nethermind, lighthouse],
+    services=[signer],
     network=Network(egress=Egress()),
```

`GoBinary("signer", Local("../signer"))` builds that directory. `credentials=[InstanceKey()]`
hands the service the key. `requires=[nethermind.rpc]` waits for Nethermind's readiness.

```console
$ tundravm show --diff stages/03-lighthouse.py stages/04-signer.py
```

```text
Services:
  + signer
    + program  /usr/bin/signer -listen 127.0.0.1:8547 -upstream http://127.0.0.1:8545/
    + source  GoBinary("signer", Local("../signer"))
    + hardening  static
    + credentials  InstanceKey()
    + probe  yes, TcpProbe(8547)
```

It listens on the loopback, so nothing outside reaches it yet.

## 5. The attested ingress

The idea: an **ingress** is the one way in from outside. Attested TLS
on 8546 fronts the signer:

<!-- diff: examples/attested-node/stages/04-signer.py examples/attested-node/stages/05-attested.py -->
```diff
--- stages/04-signer.py
+++ stages/05-attested.py
@@
 from tundravm.components import DevAccess, Egress, Network
+from tundravm.ports import AttestedTLS
 from tundravm.secrets import Generated, InstanceKey
@@
     "signer",
-    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc],
+    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc, "-quotes"],
     credentials=[InstanceKey()],
+    quotes=True,
     requires=[nethermind.rpc],
@@
     services=[signer],
+    ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
     network=Network(egress=Egress()),
```

```console
$ tundravm show --diff stages/04-signer.py stages/05-attested.py
```

```text
  Firewall
    + in  8546/tcp  attested-tls-8546.service

Ingress:
  + AttestedTLS(8546, upstream=Endpoint("http://127.0.0.1:8547/"))
```

Three ports are open now: 8546, and the control plane's 8745 and 22. `quotes=True` lets
the signer ask the evidence service for a quote over its key. It starts only once one is
issued. `GET /quote?nonce=HEX` answers one, so a client can check the key it verifies
with.

The endpoint's quote binds the listener's certificate too. Either command checks it:

```console
$ tundravm attest attested-node-1 --policy FILE --tls 8546
$ go -C ../../tools run ./cmd/attested-client --dev https://127.0.0.1:8546
```

## 6. Operating it

The idea: what running it for real takes. `image.py` is this stage with `Local("signer")`.

<!-- diff: examples/attested-node/stages/05-attested.py examples/attested-node/stages/06-operate.py -->
```diff
--- stages/05-attested.py
+++ stages/06-operate.py
@@
 
-from tundravm import Debian, Image, RootFS, Service
-from tundravm.components import DevAccess, Egress, Network
+from tundravm import Debian, File, Image, Profile, RootFS, Service
+from tundravm.components import (
+    DevAccess,
+    Egress,
+    JournalExport,
+    Network,
+    Observability,
+    PrometheusRemoteWrite,
+)
 from tundravm.ports import AttestedTLS
-from tundravm.secrets import Generated, InstanceKey
+from tundravm.secrets import Generated, InstanceKey, Secret
 from tundravm.sources import GoBinary, Local
 from tundravm.storage import OperatorKey, Volume
-from tundravm.systemd import Resources, TcpProbe
+from tundravm.systemd import Every, Resources, TcpProbe
 
@@
 )
+motd = File.text("/etc/motd", "attested-node: Nethermind and Lighthouse on Hoodi\n")
+sync = Service(
+    "sync-status",
+    ["/usr/bin/curl", "-fsS", f"http://{lighthouse.http.address}/eth/v1/node/syncing"],
+    requires=[lighthouse.http],
+    timer=Every("1h"),
+)
+logs = JournalExport(credential=Secret("logs-client"))
+metrics = PrometheusRemoteWrite(credential=Secret("metrics-token"))
 
@@
     base=Debian("trixie", snapshot="20261018T082309Z"),
-    packages=["ca-certificates"],
+    packages=["ca-certificates", "curl"],
+    files=[motd],
     components=[nethermind, lighthouse],
-    services=[signer],
+    services=[signer, sync],
     ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
@@
     rootfs=RootFS.verity(),
+    observability=Observability(logs=logs, metrics=metrics),
+    targets=("qemu", "azure", "gcp"),
+    profiles={
+        "staging": Profile(changes=[motd.ref.edit(content="attested-node: staging\n")]),
+        "bench": Profile(add=["fio"], production=False),
+    },
     dev=DevAccess(password="tdx"),
```

```console
$ tundravm show --diff stages/05-attested.py stages/06-operate.py
```

```text
Profiles:
  + staging     extends production, production
    + edits File("/etc/motd"): content="attested-node: staging\n"
  + bench       extends production, non-production
    + adds Package("fio")
```

`Observability` ships the journal and the node metrics. Neither sink names a `url=`, so
the export goes to a receiver in the guest until you set one. A production profile warns
`observability-sink-unset` until then. No `export=` names the metrics' **keep-list**, so it
holds the `health` preset. Every profile warns `observability-export-unset` until you pass
`export=`. `sync-status` runs hourly and logs Lighthouse's sync status. `staging` edits one
file. `bench` adds `fio`, and `production=False` keeps it out of production builds.
`targets=` adds Azure and GCP disks that boot the same OS.

## Next

Lock and build for production:

```console
$ tundravm lock
$ tundravm build
$ tundravm verify latest-production
```

`lock` pins every input. `build` refuses to make a production build without the lock.
`verify` checks the build against its manifest, offline. [How do I](howto.md) deploys,
attests and delivers secrets. The [manual](manual.md) has every slot and rule. The
[trust page](trust.md) says what the image defends against.
