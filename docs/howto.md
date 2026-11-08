# How do I

This page covers the tasks the [guide](guide.md) does not walk through. Inside a project the
blocks leave out `uv run`, and the [command reference](reference/cli.md) lists every flag.

## Start a project

```console
$ tundravm new demo --ci
$ cd demo
$ uv sync
$ tundravm lock
$ tundravm build --tree-only mkosi
$ uv run pytest
```

`new` writes `demo.py`, its tests and `pyproject.toml`, plus [a
workflow](release.md#the-image-pipeline-new-writes) with `--ci`. The tests compare the image
with the committed `mkosi/` tree, which depends on the lock, so write the tree after you
lock and commit both.

## Update a pin

```console
$ tundravm lock --update tdxs
$ tundravm build --tree-only mkosi
$ tundravm check --tree mkosi
```

A pin holds while its source is unchanged. `--update NAME` resolves that one input again,
and `--update` alone resolves them all. Write the tree again afterwards, because
`check --tree` fails while the committed tree differs from what the image compiles to.
[The lock](manual.md#the-lock) has the rules.

## Add a profile

Add a `Profile` to `profiles=`, as [stage 6](guide.md#6-operating-it) does, and build it
with `tundravm build --profile staging`.

- `add=` adds packages, files, services or components.
- `changes=` edits or removes something the image declares.
- `production=False` makes its builds development builds.

## Build a hardened kernel

<!-- fragment -->
```python
kernel=Kernel.build(
    base=DebianConfig("6.12.113-1"),
    fragments=[KernelFragment({"CONFIG_XFS_FS": "y"}, reason="the data disk is XFS")],
)
```

Pass it to `Image(kernel=...)`. It compiles Linux 6.12.113 from Debian's config with every
driver built in, which takes about half an hour the first time. tundravm's hardening merges
first, and a fragment that names one of its options overrides it. [The
kernel](manual.md#the-kernel) gives the merge order.

## Use a private source

<!-- fragment -->
```python
app = GoBinary(Git("https://github.com/acme/app", "v1.4.0", auth="GITHUB_TOKEN"))
```

`auth=` names the host variable that holds the token. `lock` and `build` hand it to git as a
header and never write it to disk or into the build. Hosts other than GitHub and GitLab may
need another scheme, which [Sources and toolchains](manual.md#sources-and-toolchains) lists.

## Build for production and verify it

```console
$ tundravm build --target gcp --reproduce --sign ~/.ssh/release-key
$ tundravm verify latest --require-signature --allowed-signers allowed_signers
```

- `--reproduce` builds the image again in a separate workspace and compares every artifact.
- `--sign` signs the manifest with an SSH key.
- `verify` checks the build anywhere, offline.

## Run a build by hand

```console
$ tundravm run --mode simulated --dev --probe
$ tundravm run status demo-1
$ tundravm run stop demo-1
```

`run` boots a build as a numbered instance and forwards its ports to `127.0.0.1`, and
`run ssh` opens a shell on a dev build that has SSH. [Run](manual.md#run) covers both modes.

## Deploy

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=create:100GiB
$ tundravm deploy status attested-node-1
```

`deploy` publishes the build's disk, starts a TDX VM from it and writes a receipt, and
`--plan` prints each step and cloud call without running any. Each volume needs a
`--volume`. [Recovery](trust.md#recovery) covers upgrading and replacing an instance.

## Attest an instance

```console
$ tundravm attest attested-node-1 --policy policies/attested-node-gcp.json
```

`attest` asks the instance for a quote over a fresh nonce and checks it against the policy,
which is a build's `measurements.json` or your approved copy of it. `--tls 8546` also checks
the certificate an `AttestedTLS` port serves.

On success `attest` writes `known_hosts` and hands over your SSH key. On TDX the image takes
that key only from its owner after an unlock, so attest again once the volume is open. [The
verdicts](manual.md#deploy-and-attest) explain each result.

## Deliver a secret or unlock a volume

```console
$ tundravm secrets unlock attested-node-1 data
$ tundravm secrets deliver attested-node-1 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

Each command attests again in a session of its own and writes only in that session. `unlock`
reads the passphrase from stdin, or asks for it. [Delivering
secrets](manual.md#delivering-secrets) says what each build and mode releases.

## Pin the operator key

```console
$ tundravm secrets rotate attested-node-1 --identity ~/.ssh/new-operator
```

An image with an `OperatorKey()` volume takes writes only from its owner, the SSH key of the
first signed write, which the volume's header pins. `attest --identity KEY` records that key
for the `secrets` commands to sign with. `rotate` hands the volumes to a new key with the old
key's signature, so a lost key means a new volume.

## Log in as a tenant

<!-- fragment -->
```python
firewall = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[TCP(22)])})
ssh = SshAccess(login=Login(
    "tenant",
    owner=True,
    commands={
        "status": Command("/usr/bin/app-status"),
        "logs": Command("/usr/bin/journalctl", "-u", "app", "-n", Int("lines", 1, 1000), sudo=True),
        "toggle": firewall.commands.switch,
        "shell": Command("/usr/bin/bash", dev=True),
    },
))
```

Pass both in `components=`. `ssh tenant@HOST` lists the menu, and `ssh tenant@HOST logs 50`
runs one command. A `Command` runs as the login, with `sudo=True` as root.
`modes=["maintenance"]` limits a command to that mode, and `dev=True` keeps `shell` out of
production builds.
`owner=True` gives the account the key `attest` hands over.

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

Pass it in `components=`. The build makes the container's root from the image's snapshot,
and the measured payload ships it. Each exposed port is reachable on the same port of the
host, and the container sends traffic only to what `egress=` names.
[Components](manual.md#components) has the rest.

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
)
```

The guest evaluates the rules, and each rule's series leaves together with the image's
identity, `tundravm_image_digest`. A rule with `export=False` stays inside, for later rules to
read. `tundravm_unit_active` and `tundravm_unit_cpu_seconds_total` report each service's
state and CPU time, a container's included. `export=` adds raw series names and presets, and
the `"health"` preset is its default only when there are no rules.

## Open a maintenance window

```python
from tundravm.components.network import Firewall, Mode
from tundravm.ports import TCP

window = Mode(ingress=[TCP(22)], lasts="30m")
firewall = Firewall(modes={"production": Mode(), "maintenance": window})
```

Pass it in `components=`. Attest first, then open the window, use it and close it:

```console
$ tundravm run switch demo-1 maintenance
$ tundravm run ssh demo-1
$ tundravm run switch demo-1 production
```

In a cloud, use `deploy switch`. Only the owner can switch a production build, and each
switch is extended into RTMR3, so a policy with `"modes": ["production"]` refuses the
instance until it reboots. On TDX a switch needs
[Linux 6.16](trust.md#platform-limits).

## Convert an mkosi tree or systemd units

```console
$ tundravm convert ./old-tree --from mkosi
$ tundravm convert ./units --from systemd > image.py
```

Each command prints an image file. From a tree, a comment on every setting says whether it
was `kept`, `mapped`, `defaulted` or `refused`. From units, each service unit becomes a
`Service` with its timer or socket folded in, and directives no field covers go in
`unit=DropIn(...)`.
