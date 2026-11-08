# Command reference

Generated from the `tundravm` argument parser by `uv run python -m tundravm._reference docs/reference` (Python 3.12). Do not edit it by hand: `tests/docs/test_reference.py` fails when it is stale.

| Command | Does |
|---|---|
| [`new`](#tundravm-new) | Create a project: an image file, its tests, pyproject.toml. |
| [`check`](#tundravm-check) | Check the image: declarations, lint, lock drift, tree drift. |
| [`lock`](#tundravm-lock) | Resolve or check the lock: snapshot, sources, toolchain. |
| [`build`](#tundravm-build) | Build into an immutable build directory, with its manifest. |
| [`show`](#tundravm-show) | Show the image as written and what it builds, or a build; --diff two of either. |
| [`run`](#tundravm-run) | Run a build locally as a named instance; status\|logs\|stop\|ssh\|mode\|switch\|probe. |
| [`dev`](#tundravm-dev) | Build the dev profile, boot it, probe it, follow its console; Ctrl-C stops it. |
| [`deploy`](#tundravm-deploy) | Deploy a build to Azure or GCP; status\|logs\|mode\|switch\|upgrade\|rollback. |
| [`attest`](#tundravm-attest) | Verify an instance's evidence against a policy; delivers nothing. |
| [`secrets`](#tundravm-secrets) | Deliver credentials or unlock a volume after a fresh attestation. |
| [`verify`](#tundravm-verify) | Check a build directory against its manifest. |
| [`convert`](#tundravm-convert) | Turn an mkosi tree or systemd unit files into source. |
| [`doctor`](#tundravm-doctor) | Report what this host can build, run, deploy and verify. |

## tundravm

```text
usage: tundravm [-h] [--version] COMMAND ...

Build, run and verify confidential VM images declared as one Image.

positional arguments:
  COMMAND
    new       Create a project: an image file, its tests, pyproject.toml.
    check     Check the image: declarations, lint, lock drift, tree drift.
    lock      Resolve or check the lock: snapshot, sources, toolchain.
    build     Build into an immutable build directory, with its manifest.
    show      Show the image as written and what it builds, or a build; --diff two of either.
    run       Run a build locally as a named instance; status|logs|stop|ssh|mode|switch|probe.
    dev       Build the dev profile, boot it, probe it, follow its console; Ctrl-C stops it.
    deploy    Deploy a build to Azure or GCP; status|logs|mode|switch|upgrade|rollback.
    attest    Verify an instance's evidence against a policy; delivers nothing.
    secrets   Deliver credentials or unlock a volume after a fresh attestation.
    verify    Check a build directory against its manifest.
    convert   Turn an mkosi tree or systemd unit files into source.
    doctor    Report what this host can build, run, deploy and verify.

options:
  -h, --help  show this help message and exit
  --version   show program's version number and exit

IMAGE defaults to $TUNDRAVM_IMAGE, else [tool.tundravm] image in the nearest pyproject.toml; the
build root to $TUNDRAVM_BUILD, else its `build` key (default: build); the backend to
$TUNDRAVM_BACKEND, else its `backend` key (default: local on Linux x86-64, lima elsewhere).
```

## tundravm new

Create a project: an image file, its tests, pyproject.toml.

```text
usage: tundravm new [-h] [--traceback] [--ci] NAME

Write NAME/NAME.py, stage 1 of the guide's attested node (the boot: Debian from one
snapshot, the TEE baseline, filtered egress and a dev profile), tests/test_NAME.py,
pyproject.toml with [tool.tundravm] image = "NAME.py", README.md and .gitignore;
--ci adds the image's GitHub workflow: check, lock --check and a production build on
every push; a v* tag also rebuilds on a second runner (verify --rebuild), attests
provenance and releases the image, manifest, measurements and SBOM.

positional arguments:
  NAME         Project directory and image name.

options:
  -h, --help   show this help message and exit
  --traceback  Raise errors with the Python traceback.
  --ci         Also write .github/workflows/image.yml.
```

## tundravm check

Check the image: declarations, lint, lock drift, tree drift.

```text
usage: tundravm check [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback] [--profile P]
                      [--tree DIR] [--explain CODE]
                      [IMAGE]

Everything short of a build: resolution and declaration errors, every lint rule,
drift from BUILD/tundravm.lock when it exists, the toolchain against the lock's,
and with --tree a committed mkosi tree against what the image compiles to.
Exit 1 on an error (or a drifted lock or tree); warnings are reported only.
In GitHub Actions the findings are workflow annotations. --explain CODE says what
a finding's code checks and how to fix it, an image's own component code through
the image (docs/reference/lint.md lists the rest).

positional arguments:
  IMAGE             The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

options:
  -h, --help        show this help message and exit
  --json            Print a JSON report.
  --traceback       Raise errors with the Python traceback.
  --profile P       Check this profile (repeatable; default: every profile).
  --tree DIR        A committed tree to compare.
  --explain CODE    Print what this finding code checks and how to fix it (repeatable); checks
                    nothing.

loading the image file:
  --attr NAME       The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR  Extra import directory for the file (repeatable); the working directory is
                    one.
```

## tundravm lock

Resolve or check the lock: snapshot, sources, toolchain.

```text
usage: tundravm lock [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback]
                     [--update [INPUT ...] | --check]
                     [IMAGE]

Write BUILD/tundravm.lock: the image's section digests, a pin per source, the
toolchain. Pins already locked are kept while their source is unchanged; --update
resolves the named inputs again (every one when none is named; `toolchain` records
the running toolchain). --check never writes and never touches the network: it
prints the drift and exits 1 when the lock is stale or missing. A production build
never changes the lock; a development build writes a missing one and re-pins
changed Local sources.

positional arguments:
  IMAGE                 The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --update [INPUT ...]  Resolve these inputs again (no name: all of them).
  --check               Report drift; exit 1 when stale.

loading the image file:
  --attr NAME           The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR      Extra import directory for the file (repeatable); the working directory is
                        one.
```

## tundravm build

Build into an immutable build directory, with its manifest.

```text
usage: tundravm build [-h] [--attr NAME] [--pythonpath DIR] [--traceback] [--profile P | --dev]
                      [--target T[,T]] [--reproduce] [--tree-only DIR] [--offline] [--sign KEY]
                      [--clean-cache] [-v]
                      [IMAGE]

Build one profile into BUILD/<image>/production/<id>/ (development/ for a
non-production profile): payload.efi, qemu.raw and each --target's disk,
measurements.json, sbom.spdx.json, inputs/, tree/, manifest.json (the identity;
<id> is its digest), manifest.sig with --sign, observations.json. The directory
appears atomically and is never written again; latest-production points at the
newest. A production build needs the lock (`tundravm lock`) and refuses a stale
one; a development build writes a missing one and re-pins changed Local sources.
--tree-only writes the standalone mkosi project instead and builds nothing.

positional arguments:
  IMAGE             The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

options:
  -h, --help        show this help message and exit
  --traceback       Raise errors with the Python traceback.
  --profile P       The profile (default: production).
  --dev             The dev profile (DevAccess): non-production, incremental.
  --target T[,T]    Build these of the image's targets= (default: all of them); qemu is always
                    built.
  --reproduce       Build again in an independent workspace and record the comparison; exit 1 when
                    an artifact differs.
  --tree-only DIR   Only write the mkosi tree to DIR.
  --offline         No network: build only what the lock pins.
  --sign KEY        Sign the manifest with this SSH key.
  --clean-cache     Drop the image's workspace first (fetched sources, mkosi's tools tree, its
                    caches).
  -v, --verbose     Echo mkosi's output.

loading the image file:
  --attr NAME       The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR  Extra import directory for the file (repeatable); the working directory is
                    one.
```

## tundravm show

Show the image as written and what it builds, or a build; --diff two of either.

```text
usage: tundravm show [-h] [--json] [--traceback]
                     [--units | --tree | --measurements | --diff OLD NEW] [--profile P] [--dev]
                     [--attr NAME]
                     [BUILD]

Without BUILD: the image as its file wrote it (components with their keywords,
services with their program, source, ports, hardening, volume, credentials and
probe, profiles with what each changes), then what each profile builds: packages,
sources, files, units, firewall, build steps; a profile beyond the one it extends.
--json has the same sections and where each declaration came from. With BUILD (a
build directory, an id prefix, `latest` or `latest-development`): the report
`build` printed, the manifest's provenance, unchecked guarantees and boot probe
checks. --units, --tree and --measurements narrow it. --diff OLD NEW prints the
lines that differ: of two image files, section by section; of two builds, their
packages, files, units, boot payload, measurements and policy coverage.

positional arguments:
  BUILD           A build, or an image file.

options:
  -h, --help      show this help message and exit
  --json          Print a JSON report.
  --traceback     Raise errors with the Python traceback.
  --units         The unit files.
  --tree          The tree's files.
  --measurements  The measurement records (a build).
  --diff OLD NEW  Compare two image files or two builds.
  --profile P     Profiles of the image (repeatable; default: all).
  --dev           The image's dev profile (DevAccess), as build --dev.
  --attr NAME     The image's name in its file.
```

## tundravm run

Run a build locally as a named instance; status|logs|stop|ssh|mode|switch|probe.

```text
usage: tundravm run [-h] [--json] [--traceback] [--mode {simulated,tdx}] [--dev] [--detach]
                    [--probe] [--timeout SECONDS] [--memory MEMORY] [--cpus CPUS] [--identity KEY]
                    [BUILD|ACTION] [INSTANCE] [MODE]

tundravm run [BUILD] --mode simulated|tdx [--dev] [--detach] [--probe]
    boot qemu.raw through OVMF from its ESP on a snapshot (the build is never
    written), forward every TCP port the image's firewall accepts or its services
    declare (:8745 only where the key delivery listens) from 127.0.0.1, and write
    .tundravm/instances/<image>-<n>/.
    simulated: no TDX (KVM if usable, else TCG), verdicts are `simulated`; a
      production build boots fail-closed (tdx-evidence.service refuses, no service
      starts, nothing is delivered), a --dev build runs its services.
    tdx: TDVF and a tdx-guest object; refused unless `doctor --for tdx` is ready.
    --probe: wait for the dev build's boot probe, print its table, exit 1 on a
      failed check (a production build carries no probe).
tundravm run status INSTANCE   the process, control-plane and application readiness
tundravm run logs INSTANCE     the serial console
tundravm run stop INSTANCE     quit QEMU
tundravm run ssh INSTANCE      a shell, only on a development build that runs SSH
tundravm run mode INSTANCE     the security mode and what it means for evidence
tundravm run switch INSTANCE MODE
    put a firewall mode in force over the attested endpoint (signed by the pinned
    operator); the switch is extended into RTMR3 and a window closes on its own
tundravm run probe INSTANCE    the boot probe's checks; writes probe.json

positional arguments:
  BUILD|ACTION          A build, or an action.
  INSTANCE              The instance of an action.
  MODE                  The firewall mode of `switch`.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --mode {simulated,tdx}
                        Security mode (required to start).
  --dev                 The latest development build (default: production).
  --detach              Return once QEMU runs (default: show the console).
  --probe               Wait for the boot probe and print its checks (implies --detach).
  --timeout SECONDS     How long --probe waits (default: 900.0).
  --memory MEMORY       Guest memory (default: 2G).
  --cpus CPUS           Guest CPUs (default: 2).
  --identity KEY        SSH key for `run ssh`.
```

## tundravm dev

Build the dev profile, boot it, probe it, follow its console; Ctrl-C stops it.

```text
usage: tundravm dev [-h] [--attr NAME] [--pythonpath DIR] [--traceback] [--mode {simulated,tdx}]
                    [--no-follow]
                    [IMAGE]

The inner loop as one verb: `build --dev` (a missing lock written, changed Local
sources re-pinned, the report printed), then `run --mode simulated --probe` on the
build: the probe's table, then the console until Ctrl-C, which stops the instance
and keeps the build. The latest development build is run as it is while nothing
it was built from changed (the image, the lock, a Local source's content); one
line says which changed, or which build it reuses. A build is incremental: mkosi
keeps the base tree and the compiler caches under build/, and the next build
says what it reused (`cached: packages (...)`). Exit 1 on a failed check, 0
after the stop. --no-follow returns right after the probe, the instance running
(scripts, CI); --mode tdx boots with TDX where `doctor --for tdx` is ready.

positional arguments:
  IMAGE                 The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --mode {simulated,tdx}
                        Security mode (default: simulated).
  --no-follow           Return after the probe, the instance running (`tundravm run stop` ends
                        it).

loading the image file:
  --attr NAME           The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR      Extra import directory for the file (repeatable); the working directory is
                        one.
```

## tundravm deploy

Deploy a build to Azure or GCP; status|logs|mode|switch|upgrade|rollback.

```text
usage: tundravm deploy [-h] [--json] [--traceback] [--build BUILD]
                       [--volume NAME=create:SIZE|existing:ID] [--policy FILE] [--identity KEY]
                       [--validator ENDPOINT] [--plan]
                       azure|gcp|ACTION [ARG ...]

tundravm deploy azure|gcp --build BUILD [--volume NAME=create:SIZE|existing:ID] [--plan]
    publish the build's disk and start a TDX VM from it, with a receipt; every
    volume the image declares needs --volume (GCP attaches it as tundravm-NAME;
    Azure is refused: its disks have no name the image can find). Settings come
    from [tool.tundravm.gcp] (project, bucket, zone, machine_type) and
    [tool.tundravm.azure] (storage_account, resource_group, location, vm_size,
    gallery), or TUNDRAVM_GCP_* / TUNDRAVM_AZURE_*. BUILD is never guessed.
    --plan prints each step and every cloud call in order and runs none of them.
tundravm deploy status|logs|mode INSTANCE
tundravm deploy switch INSTANCE MODE
    put a firewall mode in force, as `run switch` does
tundravm deploy upgrade INSTANCE --build BUILD --policy FILE [NAME=@FILE...]
    the replacement workflow (GCP): launch the candidate, attest it and deliver its
    credentials, stop the old one, move the volumes, unlock, wait for readiness
tundravm deploy rollback INSTANCE --policy FILE [NAME=@FILE...]
    the same back to the instance it replaced

positional arguments:
  azure|gcp|ACTION      Where to deploy, or an action.
  ARG                   INSTANCE, then NAME=@FILE credentials.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --build BUILD         The build to deploy (required).
  --volume NAME=create:SIZE|existing:ID
                        A disk for the image's volume NAME (repeatable).
  --policy FILE         The verifier policy (upgrade, rollback).
  --identity KEY        The operator's SSH key.
  --validator ENDPOINT  A tdxs validator.
  --plan                Print what the deploy would do; run nothing.
```

## tundravm attest

Verify an instance's evidence against a policy; delivers nothing.

```text
usage: tundravm attest [-h] [--json] [--traceback] --policy FILE [--tls PORT] [--output REPORT]
                       [--identity KEY] [--issuer ENDPOINT] [--validator ENDPOINT]
                       INSTANCE

Ask the instance's attested endpoint (:8745) for evidence bound to a fresh nonce and
check MRTD and RTMR0..2 against FILE: an operator-approved tundravm.policy/2 file, or
a build's measurements.json (the record for the instance's platform). With
--validator a tdxs validator judges the quote's signature and collateral; only then
can it be `trusted`. Verdicts: trusted, partial, measurements-match,
measurements-mismatch, simulated (accepted for a simulated instance), untrusted.
Exit 0 when it passes.

Channel: attested endpoint: a TLS session whose key the quote binds. The quote binds
the nonce, the session's TLS key, the SSH host keys, the instance public key and each
AttestedTLS port's certificate key; evidence with another nonce or key is refused. On
success the host keys become known_hosts and the operator's SSH public key
(--identity) goes over that session. --tls PORT also connects to the AttestedTLS
listener on guest PORT and refuses unless the certificate it presents carries the key
this same evidence binds for it; the verdict then shows the certificate's fingerprint.
--issuer checks a quote from a tdxs issuer the operator relays instead, bound to no
session.

positional arguments:
  INSTANCE

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --policy FILE         The verifier policy.
  --tls PORT            An AttestedTLS port: check the certificate it presents against the
                        binding.
  --output REPORT       Write the record (tundravm.attestation/3).
  --identity KEY        The SSH key to hand over on success (default: ~/.ssh/id_ed25519 if
                        present).
  --issuer ENDPOINT     unix:PATH or tcp://HOST:PORT instead of the attested endpoint.
  --validator ENDPOINT  A tdxs validator (unix:PATH or tcp://HOST:PORT).
```

## tundravm secrets

Deliver credentials or unlock a volume after a fresh attestation.

```text
usage: tundravm secrets [-h] [--traceback] deliver|unlock|rotate ...

tundravm secrets deliver INSTANCE --policy FILE NAME=@FILE...
    verify again, then write each value where the image declares it (a Secret:
    /run/tundravm/credentials/NAME, 0600); a production build needs `trusted`.
tundravm secrets unlock INSTANCE VOLUME
    verify again with the policy of the last `attest`, then hand the passphrase
    (typed, or piped on stdin) to the OperatorKey() volume.
tundravm secrets rotate INSTANCE --identity KEY
    hand the OperatorKey() volumes to another SSH key, signed by the owner's.

Channel: attested endpoint: a TLS session whose key the quote binds, verified afresh
by each command. Values travel in that session's request bodies only: never on argv,
in a log, a tree, a manifest or a receipt. An image with an OperatorKey() volume takes
writes signed by one SSH key, its owner, pinned in the volume's header by the first
signed write: each command signs with the key the last `attest` used (--identity).

positional arguments:
  deliver|unlock|rotate
    deliver             Deliver NAME=@FILE credentials.
    unlock              Unlock an OperatorKey() volume.
    rotate              Hand the OperatorKey() volumes to another SSH key.

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
```

### tundravm secrets deliver

Deliver NAME=@FILE credentials.

```text
usage: tundravm secrets deliver [-h] [--traceback] --policy FILE [--validator ENDPOINT]
                                INSTANCE NAME=@FILE [NAME=@FILE ...]

Deliver NAME=@FILE credentials. Channel: attested endpoint: a TLS session whose key the quote
binds.

positional arguments:
  INSTANCE
  NAME=@FILE            A secret and the file holding it.

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --policy FILE         The verifier policy.
  --validator ENDPOINT  A tdxs validator.
```

### tundravm secrets unlock

Unlock an OperatorKey() volume.

```text
usage: tundravm secrets unlock [-h] [--traceback] [--validator ENDPOINT] INSTANCE VOLUME

Unlock an OperatorKey() volume. Channel: attested endpoint: a TLS session whose key the quote
binds.

positional arguments:
  INSTANCE
  VOLUME

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --validator ENDPOINT  A tdxs validator.
```

### tundravm secrets rotate

Hand the OperatorKey() volumes to another SSH key.

```text
usage: tundravm secrets rotate [-h] [--traceback] --identity KEY [--validator ENDPOINT] INSTANCE

Hand the OperatorKey() volumes to another SSH key. Channel: attested endpoint: a TLS session whose
key the quote binds.

positional arguments:
  INSTANCE

options:
  -h, --help            show this help message and exit
  --traceback           Raise errors with the Python traceback.
  --identity KEY        The new owner's SSH private key (KEY.pub beside it).
  --validator ENDPOINT  A tdxs validator.
```

## tundravm verify

Check a build directory against its manifest.

```text
usage: tundravm verify [-h] [--json] [--traceback] [--require-signature] [--allowed-signers FILE]
                       [--rebuild] [--attr NAME] [--pythonpath DIR]
                       BUILD

Offline: the manifest is canonical and its id is its digest and the directory's
name; every artifact, input and the tree hash to what it records, and nothing else
is there; the UKI and its root hash are the recorded ones. Each check is ok, failed,
missing or unchecked. A signature is checked with --allowed-signers (an OpenSSH
allowed-signers file, namespace tundravm-manifest); --require-signature fails
without a good one. --rebuild builds the recorded inputs again in an independent
workspace and compares (it runs the project's code). Exit 1 when a check fails.

positional arguments:
  BUILD                 A build directory, an id prefix, or `latest`.

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --require-signature   Fail without a valid signature.
  --allowed-signers FILE
                        Who may sign.
  --rebuild             Rebuild independently and compare.
  --attr NAME           --rebuild: the image's name in its file.
  --pythonpath DIR      --rebuild: extra import directory for the image file (repeatable).
```

## tundravm convert

Turn an mkosi tree or systemd unit files into source.

```text
usage: tundravm convert [-h] [--json] [--traceback] --from {mkosi,systemd} [--allow-scripts] PATH

--from systemd: unit files (a file, or a directory) as an Image file, one Service per
service with its drop-ins, timer or socket; where a unit differs from what the image
implies a note says so, and what no slot holds is reported. --from mkosi: the tree
resolved by mkosi itself, as an Image file, then a report with one line per setting
the tree sets: kept (the image's tree resolves it to the same value), mapped (the
slot that carries it), defaulted (the opinion that replaced it) or refused (why
nothing carries it). Save the file beside the tree: its paths are relative to it.
Parsing a tree runs its executable mkosi.version or mkosi.configure: refused unless
--allow-scripts. --json prints the source and the report as one object.

positional arguments:
  PATH

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --from {mkosi,systemd}
                        What PATH is.
  --allow-scripts       Let mkosi run the tree's scripts while parsing.
```

## tundravm doctor

Report what this host can build, run, deploy and verify.

```text
usage: tundravm doctor [-h] [--attr NAME] [--pythonpath DIR] [--json] [--traceback]
                       [--for {build,run,tdx,azure,gcp}]
                       [IMAGE]

One row per capability: ready, blocked, optional, unavailable or unsupported, then
a fix: block, the one command that clears each row (apt packages on Debian or
Ubuntu). --for limits it to one: build, run (simulated), tdx, azure, gcp. Exit 1
when a build row (or, with --for, a row of that area) is blocked. build:go never
blocks (without go, tundravm fetches the toolchain each go.mod names); build:rustup
blocks only when IMAGE has a Cargo source that pins a Rust toolchain.

positional arguments:
  IMAGE                 The image file (default: $TUNDRAVM_IMAGE, else [tool.tundravm] image).

options:
  -h, --help            show this help message and exit
  --json                Print a JSON report.
  --traceback           Raise errors with the Python traceback.
  --for {build,run,tdx,azure,gcp}
                        One capability.

loading the image file:
  --attr NAME           The name the image (or a zero-argument factory) is bound to in the file.
  --pythonpath DIR      Extra import directory for the file (repeatable); the working directory is
                        one.
```

## Shell completion

There is no `completion` command: `python -m tundravm.completion SHELL` prints a script generated from this parser.

| Shell | Install |
|---|---|
| bash | add `source <(python -m tundravm.completion bash)` to ~/.bashrc, or save it as ~/.local/share/bash-completion/completions/tundravm |
| zsh | add `source <(python -m tundravm.completion zsh)` to ~/.zshrc after compinit, or save the output as _tundravm in a directory on $fpath |
| fish | save the output as ~/.config/fish/completions/tundravm.fish |
