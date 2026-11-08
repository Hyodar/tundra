# The flashbox example, one component per part

Date: 2026-11-08. Status: landed in the commits that follow. Franco: "I would like it if we modularized a bit more this flashbox
recipe. E.g. making components for the important parts."

After the split `image.py` reads like the attested node's: one call per part, and the
`Image` composing them.

## The split

`examples/flashbox/components/` becomes a package. `nethermind.py` and `lighthouse.py` are
file symlinks to the attested node's, so `from components.nethermind import Nethermind`
still means the guide's class; the modules below sit beside them and import each other
relatively (`from .orderflow import EGRESS, MODES`).

- `orderflow.py`, plain constants: the peers `TX_STREAM`, `BUNDLES` and `BUILDERNET` as
  `Host` values, `EGRESS` (what the searcher reaches in every mode: the bundle endpoint)
  and `MODES` (production: the tx stream and BuilderNet; maintenance: DNS, DoT and the
  web). Peers are facts, so a class would only add a name.
- `searcher.py`, `Searcher(rpc=, state=, input=, delayed=, delay=, ssh_port=10022)`: `rpc`
  an `Endpoint`, the three directories `VolumeDirectory` values, `delay` the seconds the
  log lags. `compose()` yields `Container("bob", ...)` with `ContainerSsh(ssh_port,
  owner=True)`, `Bind(input, writable=True)`, `egress=EGRESS`, `modes=MODES` and
  `requires=[rpc]`, then the `delay-pipe` and `input-proxy` services, each
  `GoBinary(Local("channels"), package=...)` with a `description=`, in the container's
  `group`. It exports `ssh` (what
  `Mode(ingress=)` takes), `unit` (`bob.service`) and `logs` (`delayed / "output.log"`),
  each through a property that builds the container from the fields, and `metrics`, the
  container's selectors.
- `health.py`, `health_rules(searcher, volume) -> list[RecordingRule]`: the seven rules in
  one place, reading `searcher.metrics` and `volume.metrics` with no doubled braces. Two
  change meaning: the OOM count is the container's cgroup's, and the 10 percent rule reads
  the volume, since `/` is the read-only verity root. The network rule leaves out
  `ve-.*`. The `PrometheusRemoteWrite` stays in `image.py`, since its URL and credential
  are the deployment's.

The menu and the firewall modes stay in `image.py`: they are the composition, each line
naming another part (`firewall.switch`, `searcher.logs`, `searcher.ssh`). The kernel stays
too, with one comment for fix 26.

`socat` stays, with a comment: it is BoB's stand-in listener in the workflow's check.

## Alternatives

- `Searcher(volume=data)`: rejected; the image places each directory, as for Nethermind.
- `Searcher(egress=, modes=)`: rejected; the peers are flashbox's, not a deployment's.
- `Health(...)` as a component: rejected; rules are not declarations.
- `components/operator.py`: rejected for the reason above.
- `L1Node` around Nethermind and Lighthouse: rejected; it hides the guide's two components.
- Re-export modules loading the attested node's files by path: rejected; a file symlink
  re-exports with no code.

## SDK gaps

One fixed, with a table test. A relative `Local` or `File` path that `compose()` writes is
beside the file constructing the component, as the docs say, but only the lowering's own
pass anchored there: an export, the egress walk and every other `composed()` call did not,
so `Local("channels")` in `Searcher.compose` resolved beside `components/searcher.py`.
`composed()` now anchors each run at the component's line, and the tree keeps
`"path":"channels"`.

A second fix, also with a table test: an image file imports its project's packages, but
`sys.modules` kept the first `components` a process imported, so the tests, which load
both examples, gave flashbox the attested node's. While an image file runs, a module of a
name its directories provide that came from elsewhere is hidden, then put back.

The rest works: a property forwards a nested container's `unit` and `ssh`, a nested `Service` binds a `VolumeDirectory` the composite received, and
the nested `Container`'s `check()` runs.

Two needed a decision, now taken:

- mypy. One `mypy .` cannot check `image.py` against its own `components`: the attested
  node roots the top-level `components`, so `components.searcher` is not found.
  `pyproject.toml` excludes `examples/flashbox/image.py` from the root run, and CI and the
  gate add `MYPYPATH=examples/flashbox mypy examples/flashbox/image.py
  examples/flashbox/components`.
- Fix 24 adds `expose_p2p` to the attested node's `Nethermind`, which `docs/guide.md`
  lists verbatim, so the guide's listing follows.

## The tree

Beyond the rule, 30303 and menu changes, `50-bob.conf` becomes `50-searcher.conf` (the
same content, named after the outer component), and the two services' lines move ahead of
`image-metrics`.

## `image.py`

```python
"""flashbox: flashbots-images' flashbox-l1, the BoB searcher sandbox, as one image.

An L1 node, Nethermind and Lighthouse on mainnet, runs beside the searcher's container,
and both keep their state on one volume whose unlock the searcher's key signs. The
firewall boots into production mode; maintenance also opens the container's SSH, and
every switch lands in RTMR3. What leaves the image is the records the health rules
export and the image's identity series, signed for Amazon Managed Prometheus.
"""

from components.health import health_rules
from components.lighthouse import Lighthouse
from components.nethermind import Nethermind
from components.searcher import Searcher

from tundravm import Debian, DebianConfig, Image, Kernel, KernelFragment, RootFS
from tundravm.components.access import Command, DevAccess, Int, Login, SshAccess, Sudo
from tundravm.components.network import Egress, Firewall, Mode, Network
from tundravm.components.observability import Observability, PrometheusRemoteWrite, Proxy, SigV4
from tundravm.secrets import Generated, Secret
from tundravm.sources import Git
from tundravm.storage import OperatorKey, Volume

data = Volume("data", unlock=OperatorKey())
jwt = Generated("jwt")
nethermind = Nethermind(
    jwt=jwt, config="mainnet", state=data.directory("nethermind"), expose_p2p=True
)
lighthouse = Lighthouse(
    execution=nethermind.engine_api,
    jwt=jwt,
    network="mainnet",
    checkpoint_sync_url="https://mainnet.checkpoint.sigp.io",
    state=data.directory("lighthouse"),
    expose_p2p=True,
    disable_quic=True,
    disable_optimistic_finalized_sync=True,
    state_cache_size=32,
)
searcher = Searcher(
    rpc=nethermind.rpc,
    state=data.directory("searcher"),
    input=data.directory("input"),
    # The searcher's login reads the delayed log through its own group, without root.
    delayed=data.directory("delayed", group="searcher"),
    delay=300,
)

firewall = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[searcher.ssh])})
ssh = SshAccess(
    login=Login(
        "searcher",
        owner=True,
        commands={
            "status": firewall.status,
            "logs": Command("/usr/bin/tail", "--lines", Int("lines", 1, 10000), searcher.logs),
            "toggle": firewall.switch,
            "reboot": Sudo("/usr/bin/systemctl", "reboot"),
            "shell": Command("/usr/bin/bash", dev=True),
        },
    )
)
metrics = PrometheusRemoteWrite(
    url="https://metrics.flashbox.example/api/v1/write",
    credential=SigV4("us-east-2", Secret("metrics-aws")),
    rules=health_rules(searcher, data),
)

# flashbots-images builds gregkh's 6.18.57 on the config of Debian's 6.18.15 backport;
# olddefconfig gives each option added since then its default.
kernel = Kernel.build(
    "6.18.57",
    Git("https://github.com/gregkh/linux", "2ff600d29ee478d0a8d46ef8a58afb5e038fe692"),
    base=DebianConfig(
        "6.18.15-1~bpo13+1",
        snapshot="20261003T143013Z",
        sha256="7008d38386b9c9552fc347e5ea3a6f85e4e8ce9333a9e4c6f09ad3055348d959",
    ),
    fragments=[
        KernelFragment(
            {"CONFIG_DEFAULT_FQ": "y", "CONFIG_TCP_CONG_BBR": "y", "CONFIG_DEFAULT_BBR": "y"},
            reason="fq paces TCP and BBR is flashbox's congestion control",
        )
    ],
    cmdline=["transparent_hugepage=madvise"],
)

image = Image(
    "flashbox",
    base=Debian("trixie", snapshot="20261018T143805Z"),
    sysctl={"vm.max_map_count": 2097152},
    components=[nethermind, lighthouse, searcher, ssh, firewall],
    network=Network(egress=Egress()),
    kernel=kernel,
    rootfs=RootFS.verity(),
    observability=Observability(metrics=metrics, proxy=Proxy("10.88.0.100", 443, passthrough=True)),
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
```

It is 98 lines, not 60: the Lighthouse flags, the kernel and the `Image` keep theirs.
