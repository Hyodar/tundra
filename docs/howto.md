# How do I

Recurring tasks that the [guide](guide.md) does not walk through. Inside a project, run
each command with `uv run`; the blocks leave it out. Every flag is in the
[command reference](reference/cli.md).

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
it after locking and commit both. [The project](manual.md#the-project) lists each file.

## Update a pin

```console
$ tundravm lock --update tdxs
$ tundravm lock --check
```

A pin stays while its source is unchanged. `--update NAME` resolves that input again, and
`--update` alone resolves all of them. `--check` writes nothing and exits 1 on drift.
Write the tree again after a lock change. [The lock](manual.md#the-lock) has the rules.

## Add a profile

Add a `Profile` to `profiles=`, as [stage 6](guide.md#6-operating-it) does. `add=` adds
packages, files, services or components. `changes=` edits or removes something the image
declares. `production=False` makes its builds development builds. Build it with
`tundravm build --profile staging`. [Profiles](manual.md#profiles) lists what an edit may
change.

## Keep the mkosi tree committed

```console
$ tundravm build --tree-only mkosi
$ tundravm check --tree mkosi
```

The first writes the tree and prints the mkosi command for each profile. The second exits
1 when the committed tree differs from what the image compiles to.
[The mkosi tree](manual.md#the-mkosi-tree) says how to read it.

## Build for production and verify it

```console
$ tundravm build --target gcp --reproduce --sign ~/.ssh/release-key
$ tundravm verify latest --require-signature --allowed-signers allowed_signers
```

`--reproduce` builds again in a separate workspace and compares every artifact. `--sign`
signs the manifest with an SSH key. `verify` checks a build anywhere, offline.
[Build](manual.md#build) lists what the directory holds.

## Run a build by hand

```console
$ tundravm run --mode simulated --dev --probe
$ tundravm run status demo-1
$ tundravm run stop demo-1
```

`run` boots a build as a numbered instance and forwards its ports to `127.0.0.1`.
`run ssh` opens a shell on a dev build with SSH. [Run](manual.md#run) covers both modes and
the probe.

## Deploy

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=create:100GiB
$ tundravm deploy status attested-node-1
```

`deploy` publishes the build's disk, starts a TDX VM from it and writes a receipt. `--plan`
prints each step and cloud call, and runs none. Each volume needs a `--volume`. The cloud
settings come from `pyproject.toml` ([Deploy and attest](manual.md#deploy-and-attest)).

## Attest an instance

```console
$ tundravm attest attested-node-1 --policy policies/attested-node-gcp.json --output attest-1.json
```

`attest` asks the instance for a quote over a fresh nonce and checks it against the policy.
The policy is a build's `measurements.json`, or a copy with the platform registers you
approve. On success it writes `known_hosts` and hands over your SSH key. `--tls 8546` also
checks the certificate an `AttestedTLS` port serves. [The verdicts](manual.md#deploy-and-attest) say what each result means.

## Deliver a secret or unlock a volume

```console
$ tundravm secrets unlock attested-node-1 data
$ tundravm secrets deliver attested-node-1 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

Each command attests again on its own session and writes only on it. `unlock` reads the
passphrase from stdin, else a prompt. Values never go on argv, in a log or in a receipt.
[Delivering secrets](manual.md#delivering-secrets) says what each build and mode
releases on.

## Upgrade or roll back

```console
$ tundravm deploy upgrade attested-node-1 --build build/attested-node/production/<id2> --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
$ tundravm deploy rollback attested-node-1 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

`upgrade` starts a candidate from the new build on GCP. It attests it and delivers its
credentials, then stops the old instance, moves the volumes and unlocks them. `rollback` is
the same back to the instance it replaced. The [trust page](trust.md#recovery) has the
recovery rules.

## Convert an mkosi tree

```console
$ tundravm convert ./old-tree --from mkosi
```

It prints an image file, then one comment per setting. `# kept:` means the image resolves
it to the same value. `# mapped:` names the slot that carries it. `# defaulted:` names the
opinion that replaced it. `# refused:` says why nothing carries it. A tree with an
executable `mkosi.version` or `mkosi.configure` needs `--allow-scripts`.

## Convert systemd units

```console
$ tundravm convert ./units --from systemd > image.py
```

It prints one `Service` per service unit, its timer or socket folded in. Directives no
field covers go in `unit=DropIn(...)`. A `# note:` marks where the unit differs from the
defaults. Then `check` names each program the image does not ship yet
(`service-program-missing`).

## Explain a finding

```console
$ tundravm check --explain nethermind-volatile-state
```

```text
nethermind-volatile-state: warning
    declared by Nethermind in components/nethermind.py
    Nethermind keeps its database on the volatile root, so it resyncs at every boot.
    fix: Pass state=volume.directory('nethermind') on a Volume.
```

`check` ends with the command that explains its codes. The
[check codes](reference/lint.md) list every one.

## Run CI

`new --ci` writes `.github/workflows/image.yml`. Every push and pull request runs
`check --tree mkosi` and `lock --check`, then builds the production profile and verifies
it. A `v*` tag also rebuilds it on a second runner and releases the image with its
manifest, measurements and SBOM. Commit `uv.lock`, `build/tundravm.lock` and `mkosi/`
first. [The image pipeline](release.md#the-image-pipeline-new-writes) has the details.
