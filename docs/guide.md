# The guide

This tutorial builds an Ethereum node on the Hoodi testnet. The node signs its answers
inside an Intel TDX trust domain. The tutorial has six stages. Each stage is a complete
image file under [`examples/attested-node/stages/`](../examples/attested-node/stages/), and
each adds one idea to the one before. Words in bold are defined in [Terms](terms.md).
Finish the README's [first run](../README.md#first-run) before you start.

## Before you start

The stages live in the tundravm repository, so clone it first:

```console
$ git clone https://github.com/Hyodar/tundra
$ cd tundra/examples/attested-node
$ tundravm check stages/01-boot.py
```

Run every command from that directory. `check` reads an image without building it, and it
prints one warning, `source-unpinned`, until you lock. `tundravm show --diff OLD NEW` prints
what one stage adds to another, also without building anything. Unless the text says
otherwise, the outputs below are excerpts of that command.

## 1. The boot

In this stage a single `Image` value describes a whole OS. `tundravm new` writes this
file:

<!-- listing: examples/attested-node/stages/01-boot.py -->
```python
"""attested-node: the boot, stage 1 of the guide.

Debian trixie from one snapshot, with systemd, the TEE baseline (NTS time, a volatile
journal, sysctl hardening), DHCP, a firewall that drops inbound traffic nothing exposes,
filtered egress, and a dev profile with a serial console and root password "tdx".
"""

from tundravm import Debian, Image
from tundravm.components.access import DevAccess
from tundravm.components.network import Egress, Network

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    network=Network(egress=Egress()),
    dev=DevAccess(password="tdx"),
)
```

The docstring lists what the call implies. Boot the `dev` profile of this image:

```console
$ tundravm dev stages/01-boot.py
```

`dev` builds the profile and boots it under QEMU without TDX. It prints the boot probe and
then follows the console until you press Ctrl-C. The `boot` job of the CI boots this stage on
every push. Its console shows:

```text
tundravm-probe: mode ok simulated
tundravm-probe: rtmr skip no TDX: a simulated guest has no registers to compare with measurements.json
tundravm-probe: socket:tdxs.socket ok active
tundravm-probe: egress:192.0.2.1:9 ok refused by the output chain (Operation not permitted)
tundravm-probe: kernel:modules ok 12 loaded, each curated or held by one
tundravm-probe: key:instance ok root:root 600, pub 034c2400bf28bbc43c8bd283bc0c56d1c426399d0d763b162c2e01e8392146214e
tundravm-probe: done 5 ok 0 fail
```

The line `mode ok simulated` says that the guest found no TDX. A development build runs its
services anyway, while a production build would start none. The `egress:` line shows the
firewall refusing traffic that nothing allows.

## 2. Nethermind, a component of your own

Your application becomes a component that your project writes. This stage adds the most, so
it comes in four parts.

<!-- diff: examples/attested-node/stages/01-boot.py examples/attested-node/stages/02-nethermind.py -->
```diff
--- stages/01-boot.py
+++ stages/02-nethermind.py
@@
 
-from tundravm import Debian, Image
+from components.nethermind import Nethermind
+
+from tundravm import Debian, Image, RootFS
 from tundravm.components.access import DevAccess
 from tundravm.components.network import Egress, Network
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

The image file imports `Nethermind` from `components/nethermind.py`. That class shows the
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
            environment={"DOTNET_BUNDLE_EXTRACT_BASE_DIR": datadir},
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

- The fields are configuration, because the class is a frozen, keyword-only dataclass.
- `compose()` yields the pieces the component stands for, which are a `Build` and the
  `Service` that runs it.
- `using=Dotnet(...)` builds the program offline, with the SDK named in its `global.json`.
- `rpc` and `engine_api` are exports. Each is an endpoint that carries the readiness of the
  service.
- `outbound()` says what a filtered image lets the component send, here its peers on 30303.
- `check()` is a lint rule that runs for each profile, and `rules` declares each code it
  reports.

### Its storage

The database of Nethermind must survive a reboot, so `data` is a **volume**, which is an
encrypted disk. With `unlock=OperatorKey()`, an operator sends the passphrase after checking
the quote, and Nethermind waits until then. The first key to sign a write becomes the
**owner** of the volume. The instance refuses writes that any other key signs.
`Generated("jwt")` is a **secret** that the guest makes at each boot.

### What the volume brings

A volume needs an operator, so the image now runs the **control plane**:

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

Port 8745 is the attested endpoint and port 22 is SSH. The `outbound()` method opened
30303.

### The root filesystem

The build is large, so the image protects its root. `RootFS.verity()` makes the root a
read-only disk, and the hash of that disk travels on the measured kernel command line.

## 3. Lighthouse, wired by an export

Components connect to each other through their exports. Lighthouse, the consensus client,
drives Nethermind to the head of the chain.

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
answers. Lighthouse shares the same generated secret. The file `components/lighthouse.py`
has the same shape as the one for Nethermind, except that it uses a pinned download instead
of a build. Its `outbound()` opens its peers and the checkpoint host:

```console
$ tundravm show --diff stages/02-nethermind.py stages/03-lighthouse.py
```

```text
  Firewall
    + out  443/tcp  lighthouse (checkpoint-sync.hoodi.ethpandaops.io)
    + out  9000/tcp, 9000/udp, 9001/udp  lighthouse
```

## 4. The signer, your own program

A `Service` can run your own program, built from source. The directory `signer/` holds a Go
proxy that signs the block responses of Nethermind with the **instance key**.

<!-- diff: examples/attested-node/stages/03-lighthouse.py examples/attested-node/stages/04-signer.py -->
```diff
--- stages/03-lighthouse.py
+++ stages/04-signer.py
@@
 
-from tundravm import Debian, Image, RootFS
+from tundravm import Debian, Image, RootFS, Service
 from tundravm.components.access import DevAccess
 from tundravm.components.network import Egress, Network
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
hands the key to the service. `requires=[nethermind.rpc]` makes the service wait until
Nethermind is ready.

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

The signer listens on the loopback, so nothing outside the guest can reach it yet.

## 5. The attested ingress

An **ingress** is the one way in from outside. Here it is attested TLS on port 8546, in
front of the signer:

<!-- diff: examples/attested-node/stages/04-signer.py examples/attested-node/stages/05-attested.py -->
```diff
--- stages/04-signer.py
+++ stages/05-attested.py
@@
 from tundravm.components.network import Egress, Network
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

Three ports are open now. They are 8546, and 8745 and 22 from the control plane. With
`quotes=True`, the signer asks the evidence service for a quote over its key, and it starts
only once the service has issued one. A client can request a quote with
`GET /quote?nonce=HEX`, so it can check the key it verifies signatures with.

The quote of the endpoint also binds the certificate of the listener. Either command checks
it:

```console
$ tundravm attest attested-node-1 --policy FILE --tls 8546
$ go -C ../../tools run ./cmd/attested-client --dev https://127.0.0.1:8546
```

## 6. Operating it

This stage adds what running the node for real takes. The file `image.py` is this stage with
`Local("signer")`.

<!-- diff: examples/attested-node/stages/05-attested.py examples/attested-node/stages/06-operate.py -->
```diff
--- stages/05-attested.py
+++ stages/06-operate.py
@@
 
-from tundravm import Debian, Image, RootFS, Service
+from tundravm import Debian, File, Image, Profile, RootFS, Service
 from tundravm.components.access import DevAccess
 from tundravm.components.network import Egress, Network
+from tundravm.components.observability import JournalExport, Observability, PrometheusRemoteWrite
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

`Observability` ships the journal and the node metrics. Neither sink names a `url=`, so the
export goes to a receiver in the guest until you set one, and a production profile warns
`observability-sink-unset` until then. No `export=` names the **keep-list** of the metrics,
so the keep-list holds the `health` preset. Every profile warns `observability-export-unset`
until you pass `export=`. The service `sync-status` runs hourly and logs the sync status of
Lighthouse. The `staging` profile edits one file. The `bench` profile adds `fio`, and
`production=False` keeps it out of production builds. `targets=` adds Azure and GCP disks
that boot the same OS.

## Next

When you are ready, lock and build for production:

```console
$ tundravm lock
$ tundravm build
$ tundravm verify latest-production
```

`lock` pins every input, and `build` refuses to make a production build without the lock.
`verify` checks the build against its manifest, offline. [How do I](howto.md) shows how to
deploy, attest and deliver secrets. The [manual](manual.md) describes every slot and rule.
The [trust page](trust.md) says what the image defends against.
