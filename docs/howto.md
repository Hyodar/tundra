# How do I

Tasks the [guide](guide.md) does not walk through. Inside a project, the blocks leave out
`uv run`. Every flag is in the [command reference](reference/cli.md).

## Start a project

```console
$ tundravm new demo --ci
$ cd demo
$ uv sync
$ tundravm lock
$ tundravm build --tree-only mkosi
$ uv run pytest
```

`new` writes `demo.py`, its tests, `pyproject.toml` and, with `--ci`, a workflow. The tests
compare the image with the committed `mkosi/` tree. The tree depends on the lock, so write
it after locking and commit both.
[The image pipeline](release.md#the-image-pipeline-new-writes) is the workflow.

## Update a pin

```console
$ tundravm lock --update tdxs
$ tundravm build --tree-only mkosi
$ tundravm check --tree mkosi
```

A pin stays while its source is unchanged. `--update NAME` resolves that input again, and
`--update` alone resolves all of them. Write the tree again after a lock change.
`check --tree` exits 1 while the committed tree differs from what the image compiles to.
[The lock](manual.md#the-lock) has the rules.

## Add a profile

Add a `Profile` to `profiles=`, as [stage 6](guide.md#6-operating-it) does. `add=` adds
packages, files, services or components. `changes=` edits or removes something the image
declares. `production=False` makes its builds development builds. Build it with
`tundravm build --profile staging`.

## Build a hardened kernel

<!-- fragment -->
```python
kernel=Kernel.build(
    base=DebianConfig("6.12.113-1"),
    fragments=[KernelFragment({"CONFIG_XFS_FS": "y"}, reason="the data disk is XFS")],
)
```

Pass it to `Image(kernel=...)`. It compiles 6.12.113 from Debian's config, with every
driver built in. The first build takes about half an hour. Tundra's hardening merges
first. A fragment keeps on what it turns off. `check` refuses `hardening=False` in
production. [The kernel](manual.md#the-kernel) has the order.

## Use a private source

<!-- fragment -->
```python
app = GoBinary("app", Git("https://github.com/acme/app", "v1.4.0", auth="GITHUB_TOKEN"))
```

`auth=` names the host variable that holds the token. `lock` and `build` read it and hand
it to git as a header. Nothing writes it to disk or into the build. Another host may need
another scheme: [Sources and toolchains](manual.md#sources-and-toolchains) has the table.

## Build for production and verify it

```console
$ tundravm build --target gcp --reproduce --sign ~/.ssh/release-key
$ tundravm verify latest --require-signature --allowed-signers allowed_signers
```

`--reproduce` builds again in a separate workspace and compares every artifact. `--sign`
signs the manifest with an SSH key. `verify` checks a build anywhere, offline.

## Run a build by hand

```console
$ tundravm run --mode simulated --dev --probe
$ tundravm run status demo-1
$ tundravm run stop demo-1
```

`run` boots a build as a numbered instance and forwards its ports to `127.0.0.1`.
`run ssh` opens a shell on a dev build with SSH. [Run](manual.md#run) covers both modes.

## Deploy

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=create:100GiB
$ tundravm deploy status attested-node-1
```

`deploy` publishes the build's disk, starts a TDX VM from it and writes a receipt. `--plan`
prints each step and cloud call, and runs none. Each volume needs a `--volume`. The cloud
settings come from `pyproject.toml`. [Recovery](trust.md#recovery) upgrades and replaces an
instance.

## Attest an instance

```console
$ tundravm attest attested-node-1 --policy policies/attested-node-gcp.json
```

`attest` asks the instance for a quote over a fresh nonce and checks it against the policy,
a build's `measurements.json` or your approved copy. `--tls 8546` also checks the
certificate an `AttestedTLS` port serves. On success `attest` writes `known_hosts` and
hands over your SSH key. On TDX the image takes that key only from its owner, after an
unlock: attest again then. [The verdicts](manual.md#deploy-and-attest) say what each
result means.

## Deliver a secret or unlock a volume

```console
$ tundravm secrets unlock attested-node-1 data
$ tundravm secrets deliver attested-node-1 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

Each command attests again on its own session and writes only on it. `unlock` reads the
passphrase from stdin, else a prompt. [Delivering secrets](manual.md#delivering-secrets)
says what each build and mode releases on.

## Pin the operator key

```console
$ tundravm secrets rotate attested-node-1 --identity ~/.ssh/new-operator
```

An image with an `OperatorKey()` volume has one owner: the SSH key of the first signed
write. The volume's header pins it. `attest --identity KEY` records the key, and every
`secrets` command signs with it. The instance refuses a write signed by any other key.
`rotate` hands the volumes to a new key, signed by the old one. A lost key means a new
volume.

## Log in as a tenant

<!-- fragment -->
```python
ssh = SshAccess(login=Login(
    "tenant",
    owner=True,
    commands={
        "status": ["/usr/bin/app-status"],
        "logs": Sudo(["/usr/bin/journalctl", "-u", "app", "-n", "{lines:int:1-1000}"]),
        "switch": Sudo(["/usr/bin/mode-switch", "to", "{mode:choice:production|maintenance}"]),
    },
))
```

Pass it in `components=`. `ssh tenant@HOST` lists the menu, and `ssh tenant@HOST logs 50`
runs one entry. The account has no password, no shell and no forwarding. `owner=True`
takes the key `attest` hands over, and root then takes none. `keys=` takes baked keys or a
`Secret` instead.

## Run a container

<!-- fragment -->
```python
web = Container(
    "web",
    ["/usr/bin/busybox", "httpd", "-f", "-p", "8081"],
    packages=["busybox"],
    expose=[TCP(8081)],
)
```

Pass it in `components=`. The build makes its root from the image's snapshot, and the
measured payload ships it. The program runs in that root with no network but the loopback.
Each exposed port is relayed in from the host. [Components](manual.md#components) lists
what is refused.

## Ship booleans, not raw series

<!-- fragment -->
```python
disk = 'node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}'
net = 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m]))'
metrics = PrometheusRemoteWrite(
    url="https://metrics.example/api/v1/write",
    credential=Secret("metrics-token"),
    rules=[
        RecordingRule("box:disk_free_over_10_percent", f"{disk} > bool 0.1"),
        RecordingRule("box:network_is_up", f"{net} > bool 0"),
        RecordingRule("box:container_alive", 'max(tundravm_unit_active{unit="box.service"})'),
    ],
    export=["box:disk_free_over_10_percent", "box:network_is_up", "box:container_alive"],
)
```

The guest evaluates the rules; only the `export=` names leave, with the image's identity
(`tundravm_image_digest`). A rule left out of `export=` stays in. `tundravm_unit_active`
and `tundravm_unit_cpu_seconds_total` are each service's state and CPU time, a container's
included. `export=` also takes raw
names and the `"health"` preset, its default. A production profile with a `url=` and no
`export=` fails `observability-export-unset`.

## Open a maintenance window

```python
from tundravm.components import Firewall, Mode
from tundravm.ports import TCP

window = Mode(ingress=[TCP(22)], lasts="30m")
firewall = Firewall(modes={"production": Mode(), "maintenance": window})
```

Pass it in `components=`. The image boots into `production`, which may not open 22. Attest
first, then open, use and close it:

```console
$ tundravm run switch demo-1 maintenance
$ tundravm run ssh demo-1
$ tundravm run switch demo-1 production
```

Use `deploy switch` in a cloud. A production build switches only for its owner. Each
switch is extended into RTMR3, so `attest` prints `modes  production; 2 switches since
boot: ...`. A policy with `"modes": ["production"]` refuses the instance until it reboots.
On TDX a switch needs Linux 6.16 ([limits](trust.md#platform-limits)).

## Convert an mkosi tree or systemd units

```console
$ tundravm convert ./old-tree --from mkosi
$ tundravm convert ./units --from systemd > image.py
```

Each prints an image file. From a tree, a comment per setting says what became of it:
`kept`, `mapped`, `defaulted` or `refused`. From units, each service unit becomes a
`Service`, its timer or socket folded in. Directives no field covers go in
`unit=DropIn(...)`.
