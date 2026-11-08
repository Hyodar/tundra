# The guide

This tutorial builds an Ethereum node on the Hoodi testnet that signs its answers inside an
Intel TDX trust domain. Each of its six stages is a complete image file under
[`examples/attested-node/stages/`](../examples/attested-node/stages/), adding one idea to
the stage before. Words in bold are defined in [Terms](terms.md), and the README's [first
run](../README.md#first-run) comes first.

## Before you start

The stages live in the tundravm repository, so clone it and run every command from the
example's directory:

```console
$ git clone https://github.com/Hyodar/tundra
$ cd tundra/examples/attested-node
$ tundravm check stages/01-boot.py
```

`check` reads an image without building it, and until you lock it notes that no lockfile exists yet.
The outputs below are excerpts of `tundravm show --diff OLD NEW`, which prints what one
stage adds to another, also without building.

## 1. The boot

A single `Image` value describes the whole OS, and its docstring lists what the call
implies. `tundravm new` writes this file:

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

Boot its `dev` profile:

```console
$ tundravm dev stages/01-boot.py
```

`dev` builds the profile, boots it under QEMU without TDX and follows the console until you
press Ctrl-C. The console shows the boot probe:

```text
tundravm-probe: mode ok simulated
tundravm-probe: rtmr skip no TDX: a simulated guest has no registers to compare with measurements.json
tundravm-probe: socket:tdxs.socket ok active
tundravm-probe: egress:192.0.2.1:9 ok refused by the output chain (Operation not permitted)
tundravm-probe: kernel:modules ok 12 loaded, each curated or held by one
tundravm-probe: key:instance ok root:root 600, pub 034c2400bf28bbc43c8bd283bc0c56d1c426399d0d763b162c2e01e8392146214e
tundravm-probe: done 5 ok 0 fail
```

`mode ok simulated` means the guest found no TDX, which is also why the `rtmr` check has
nothing to compare. A development build runs its services anyway, where a production build
would start none.

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

The image file imports `Nethermind` from `components/nethermind.py`, a class that shows the
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

- The fields are its configuration, because a component is a frozen, keyword-only
  dataclass.
- `compose()` yields what the component stands for, here a `Build` and the `Service` that
  runs it.
- `rpc` and `engine_api` are exports: endpoints that carry the service's readiness.
- `outbound()` says what a filtered image lets it send, and `check()` is a lint rule whose
  codes `rules` declares.

### Its storage

Nethermind's database must survive a reboot, so `data` is a **volume**. With
`unlock=OperatorKey()`, an operator sends its passphrase after checking the quote, and
Nethermind waits until then. The first key to sign a write becomes the volume's **owner**.

The JWT that Nethermind shares with its consensus client is a **secret** too, but one the
guest generates at each boot, so `Generated("jwt")` needs no operator.

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

Port 8745 is the attested endpoint and 22 is SSH, while 30303 is what `outbound()` asked
for.

### The root filesystem

`RootFS.verity()` makes the root a read-only disk whose hash travels on the measured kernel
command line, so the host cannot change the OS at rest.

## 3. Lighthouse, wired by an export

Components connect through their exports. Lighthouse, the consensus client, drives
Nethermind to the head of the chain:

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

Because `execution=` takes `nethermind.engine_api`, Lighthouse starts once Nethermind
answers. `components/lighthouse.py` has the same shape as the Nethermind component, but it
downloads a pinned release instead of building one, and its `outbound()` opens its peers and
the checkpoint host:

```console
$ tundravm show --diff stages/02-nethermind.py stages/03-lighthouse.py
```

```text
  Firewall
    + out  443/tcp  lighthouse (checkpoint-sync.hoodi.ethpandaops.io)
    + out  9000/tcp, 9000/udp, 9001/udp  lighthouse
```

## 4. The signer, your own program

A `Service` can also run a program you build from source. The directory `signer/` holds a
Go proxy that signs Nethermind's block responses with the **instance key**:

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
+program = GoBinary(Local("../signer"))
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

`GoBinary` builds that directory, `credentials=` hands the service the key, and `requires=`
holds it back until Nethermind is ready.

```console
$ tundravm show --diff stages/03-lighthouse.py stages/04-signer.py
```

```text
Services:
  + signer
    + program  /usr/bin/signer -listen 127.0.0.1:8547 -upstream http://127.0.0.1:8545/
    + source  GoBinary(Local("../signer"))
    + hardening  static
    + credentials  InstanceKey()
    + probe  yes, TcpProbe(8547)
```

The signer listens on the loopback, so nothing outside the guest can reach it yet.

## 5. The attested ingress

An **ingress** is the one way in from outside, here attested TLS on port 8546 in front of
the signer:

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

With `quotes=True` the signer starts only once the evidence service has issued a quote over
its key. A client fetches that quote with `GET /quote?nonce=HEX` before it trusts the
signatures.

The quote also binds the certificate of the listener on 8546, and either command checks it:

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

`staging` edits one file, and `bench` adds `fio` but stays out of production builds because
of `production=False`. The rest of the stage:

- `Observability` ships the journal and the node metrics. No sink names a `url=` yet, so
  both go to a receiver inside the guest, and with no `export=` the metrics **keep-list** is
  the `health` preset. `check` warns about each until you set it.
- `sync-status` runs hourly and logs Lighthouse's sync status.
- `targets=` adds Azure and GCP disks that boot the same OS.

## Next

When you are ready, lock and build for production:

```console
$ tundravm lock
$ tundravm build
$ tundravm verify latest-production
```

`build` refuses a production build without the lock, and `verify` checks the result against
its manifest, offline. From here:

- [How do I](howto.md) deploys, attests and delivers secrets.
- The [manual](manual.md) describes every slot and rule.
- The [trust page](trust.md) says what the image defends against.
